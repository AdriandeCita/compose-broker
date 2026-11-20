"""CLI commands that drive compose (`down`), on the fake docker."""
import contextlib
import io
import json
from unittest import mock

from compose_broker import cli
from compose_broker.registry import Project, Registry, approved_path
from compose_broker.util import atomic_write, digest, now_iso, pretty
from tests.helpers import FAKE_IMAGE_ID, Env, docker_calls, fake_sandbox


class DownTests(Env):
    def setUp(self):
        super().setUp()
        other_dir = self.root / "projects" / "blog"
        other_dir.mkdir()
        self.other = Project(name="blog", dir=str(other_dir.resolve()),
                             compose_files=["compose.yaml"], compose_project="cb-blog")
        idle_dir = self.root / "projects" / "idle"
        idle_dir.mkdir()
        self.idle = Project(name="idle", dir=str(idle_dir.resolve()),
                            compose_files=["compose.yaml"], compose_project="cb-idle")
        projects = {}
        for p, cfg in ((self.p, self.good_config()),
                       (self.other, {"name": "cb-blog", "services": {"app": {"image": "nginx"}}}),
                       (self.idle, {"name": "cb-idle", "services": {"app": {"image": "nginx"}}})):
            p.approved_sha256, p.approved_at = digest(cfg), now_iso()
            atomic_write(approved_path(p.name), pretty(cfg))
            projects[p.name] = p
        Registry.save(projects)
        patcher = mock.patch.object(cli, "_sandbox", lambda: fake_sandbox(self.docker_bin))
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake(self, **kw):
        (self.home / "fake-docker.json").write_text(json.dumps(kw))

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def compose_downs(self):
        """{compose project: compose argv} for every `down` that ran in the sandbox."""
        downs = {}
        for c in docker_calls(self.home):
            if c[:1] == ["run"] and "down" in c:
                inner = c[c.index(FAKE_IMAGE_ID) + 1:]
                downs[inner[inner.index("--project-name") + 1]] = inner
        return downs

    def test_down_named_project_runs_in_sandbox_on_approved_config(self):
        code, out, _ = self.run_cli("down", "shop")
        self.assertEqual(code, 0)
        self.assertIn("shop: stack cb-shop is down", out)
        downs = self.compose_downs()
        self.assertEqual(list(downs), ["cb-shop"])
        inner = downs["cb-shop"]
        self.assertEqual(inner[-2:], ["down", "--remove-orphans"])
        self.assertNotIn("--volumes", inner)
        f = inner[inner.index("-f") + 1]
        self.assertTrue(f.startswith("/cb/"), f)  # broker runtime file, not compose.yaml

    def test_down_all_only_touches_registered_projects_with_containers(self):
        self.fake(compose_projects=["cb-shop", "cb-shop", "cb-blog", "someone-else"])
        code, out, _ = self.run_cli("down", "--all")
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"taking down: (shop, blog|blog, shop)\n")
        self.assertEqual(sorted(self.compose_downs()), ["cb-blog", "cb-shop"])

    def test_down_all_with_nothing_running(self):
        code, out, _ = self.run_cli("down", "--all")
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.compose_downs(), {})

    def test_down_needs_names_or_all(self):
        self.assertNotEqual(self.run_cli("down")[0], 0)
        self.assertNotEqual(self.run_cli("down", "shop", "--all")[0], 0)

    def test_down_unknown_project(self):
        code, _, _ = self.run_cli("down", "nope")
        self.assertIn("not registered", str(code))
        self.assertFalse((self.home / "docker-calls.jsonl").exists())

    def test_down_skips_unapproved_but_continues(self):
        approved_path("shop").write_text("{}")  # integrity check fails
        code, _, err = self.run_cli("down", "shop", "blog")
        self.assertEqual(code, 1)
        self.assertIn("docker compose -p cb-shop down", err)
        self.assertIn("failed: shop", err)
        self.assertEqual(list(self.compose_downs()), ["cb-blog"])

    def test_down_refuses_while_server_operation_runs(self):
        self.fake(containers=[{"id": "c1", "name": "cb-broker-shop-up-1",
                               "image": FAKE_IMAGE_ID, "op": "up"}])
        code, _, err = self.run_cli("down", "shop")
        self.assertEqual(code, 1)
        self.assertIn("still running", err)
        self.assertEqual(self.compose_downs(), {})
