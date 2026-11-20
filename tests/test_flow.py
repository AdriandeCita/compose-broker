import contextlib
import io
import json

from compose_broker import paths
from compose_broker.broker import Broker
from compose_broker.mcp import Session
from compose_broker.registry import Registry, approved_path
from compose_broker.util import atomic_write, digest, now_iso, pretty
from tests.helpers import FAKE_IMAGE_ID, Env, docker_calls, fake_sandbox


class FlowBase(Env):
    """Session -> Broker -> fake sandbox, end to end (identity bypassed)."""

    def setUp(self):
        super().setUp()
        cfg = self.good_config()
        (self.proj_dir / "resolved.json").write_text(json.dumps(cfg))
        self.p.approved_sha256 = digest(cfg)
        self.p.approved_at = now_iso()
        Registry.save({"shop": self.p})
        atomic_write(approved_path("shop"), pretty(cfg))
        self.session = Session(Broker(fake_sandbox(self.docker_bin)), "shop", "test")

    def rpc(self, method, params=None, mid=1):
        return self.session.handle({"jsonrpc": "2.0", "id": mid, "method": method,
                                    "params": params or {}})

    def call(self, name, args=None):
        return self.rpc("tools/call", {"name": name, "arguments": args or {}})


class FlowTests(FlowBase):

    def test_initialize_and_list(self):
        r = self.rpc("initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
        self.assertEqual(names, ["stack_up", "stack_restart", "stack_down", "stack_status", "stack_logs"])

    def test_descriptions_say_docker_runs_on_host(self):
        # MCP-5: weak models check for docker locally and give up, or curl their own localhost.
        instr = self.rpc("initialize", {"protocolVersion": "2025-03-26"})["result"]["instructions"]
        self.assertIn("Docker runs on the host", instr)
        self.assertIn("host.docker.internal", instr)
        tools = {t["name"]: t for t in self.rpc("tools/list")["result"]["tools"]}
        self.assertIn("docker is not installed where you are", tools["stack_up"]["description"])
        self.assertIn("not in your environment", tools["stack_status"]["description"])

    def test_unidentified_session_has_no_tools(self):
        s = Session(self.session.broker, None, "test")
        r = s.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(r["result"]["tools"], [])
        r = s.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "stack_up", "arguments": {}}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_up_runs_approved_runtime_file(self):
        r = self.call("stack_up")["result"]
        self.assertFalse(r["isError"], r)
        self.assertIn("web: running", r["content"][0]["text"])
        up = next(c for c in docker_calls(self.home) if "up" in c)
        inner = up[up.index(FAKE_IMAGE_ID) + 1:]
        self.assertEqual(inner[:3], ["compose", "--project-name", "cb-shop"])
        # The runtime file is mounted under /cb; compose never sees its host path.
        f = inner[inner.index("-f") + 1]
        self.assertTrue(f.startswith("/cb/") and f.endswith("shop.up.json"), f)
        self.assertIn("--env-file", inner)

    def script_up(self, code, output):
        (self.proj_dir / "up-result.json").write_text(json.dumps({"code": code, "output": output}))

    def test_up_build_failure_says_previous_version_runs(self):
        self.script_up(1, "#7 [2/2] RUN make\n#7 ERROR: process did not complete\n"
                          "failed to solve: process did not complete\n")
        r = self.call("stack_up")["result"]
        text = r["content"][0]["text"]
        self.assertTrue(r["isError"])
        self.assertIn("PREVIOUS version", text)
        self.assertLess(text.index("PREVIOUS version"), text.index("--- services ---"))
        self.assertNotIn("Next:", text)
        self.assertTrue(text.startswith("FAILED: the image build failed"))

    def test_build_failure_is_remembered_until_next_success(self):
        # EXE-10b: later calls must not look healthy while old containers run.
        self.script_up(1, "#7 [2/2] RUN make\n#7 ERROR: make: *** [all] Error 2\n"
                          "failed to solve: process did not complete\n")
        self.call("stack_up")
        status = self.call("stack_status")["result"]["content"][0]["text"]
        self.assertTrue(status.startswith("WARNING: the last stack_up"))
        self.assertIn("make: *** [all] Error 2", status)
        logs = self.call("stack_logs")["result"]["content"][0]["text"]
        self.assertTrue(logs.startswith("WARNING: the last stack_up"))
        self.assertNotIn("Error 2", logs)
        restart = self.call("stack_restart")["result"]["content"][0]["text"]
        self.assertTrue(restart.startswith("WARNING: the last stack_up"))

        (self.proj_dir / "up-result.json").unlink()
        self.assertTrue(self.call("stack_up")["result"]["content"][0]["text"].startswith("SUCCESS"))
        status = self.call("stack_status")["result"]["content"][0]["text"]
        self.assertNotIn("WARNING", status)

    def test_up_timeout_is_remembered(self):
        from unittest import mock
        from compose_broker import docker as docker_mod
        fake = self.home / "fake-docker.json"
        fake.write_text(json.dumps({"sleep": {"up": 5}}))
        with mock.patch.dict(docker_mod.TIMEOUTS, {"up": 0.3}), \
                mock.patch.object(docker_mod, "KILL_GRACE_S", 0.3):
            r = self.call("stack_up")["result"]
        self.assertTrue(r["isError"])
        self.assertTrue(r["content"][0]["text"].startswith("FAILED: stack_up timed out"))
        fake.unlink()
        self.assertTrue(self.call("stack_status")["result"]["content"][0]["text"]
                        .startswith("WARNING: the last stack_up"))

    def test_down_clears_stale_warning(self):
        self.script_up(1, "#7 ERROR: boom\nfailed to solve: boom\n")
        self.call("stack_up")
        self.call("stack_down")
        self.assertNotIn("WARNING", self.call("stack_status")["result"]["content"][0]["text"])

    def test_start_failure_is_not_reported_as_stale(self):
        self.script_up(1, "dependency failed to start: container cb-shop-db-1 exited (1)\n")
        self.call("stack_up")
        self.assertNotIn("WARNING", self.call("stack_status")["result"]["content"][0]["text"])

    def test_up_start_failure_points_to_logs(self):
        self.script_up(1, "dependency failed to start: container cb-shop-db-1 exited (1)\n")
        text = self.call("stack_up")["result"]["content"][0]["text"]
        self.assertIn("did not start correctly", text)
        self.assertNotIn("PREVIOUS version", text)

    def test_up_success_has_no_failure_note(self):
        text = self.call("stack_up")["result"]["content"][0]["text"]
        self.assertNotIn("PREVIOUS version", text)
        self.assertNotIn("did not start correctly", text)
        self.assertIn("Next:", text)

    def test_up_refuses_on_drift_without_leaking(self):
        cfg = self.good_config()
        cfg["services"]["db"]["privileged"] = True
        cfg["services"]["db"]["environment"]["LEAK"] = "host-secret-value"
        (self.proj_dir / "resolved.json").write_text(json.dumps(cfg))
        r = self.call("stack_up")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("approve", r["content"][0]["text"])
        self.assertNotIn("host-secret-value", r["content"][0]["text"])

    def test_up_refuses_tampered_approved_file(self):
        cfg = self.good_config()
        cfg["services"]["db"]["privileged"] = True
        approved_path("shop").write_text(json.dumps(cfg))
        r = self.call("stack_up")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("integrity", r["content"][0]["text"])

    def test_logs_validation(self):
        self.assertFalse(self.call("stack_logs", {"service": "web", "tail": 50})["result"]["isError"])
        self.assertEqual(self.call("stack_logs", {"service": "nope"})["error"]["code"], -32602)
        self.assertEqual(self.call("stack_logs", {"tail": 99999})["error"]["code"], -32602)
        self.assertEqual(self.call("stack_logs", {"since": "1m; rm -rf /"})["error"]["code"], -32602)
        self.assertEqual(self.call("stack_logs", {"tail": True})["error"]["code"], -32602)
        self.assertEqual(self.call("stack_status", {"path": "/"})["error"]["code"], -32602)

    def test_reset_hidden_unless_allowed(self):
        self.assertEqual(self.call("stack_reset")["error"]["code"], -32602)
        Registry.update("shop", allow_reset=True)
        self.assertFalse(self.call("stack_reset")["result"]["isError"])

    def test_audit_log_written(self):
        self.call("stack_status")
        rec = json.loads(paths.audit_log().read_text().splitlines()[-1])
        self.assertEqual((rec["project"], rec["tool"], rec["outcome"]), ("shop", "stack_status", "ok"))

    def audit_records(self):
        return [json.loads(line) for line in paths.audit_log().read_text().splitlines()]

    def logged(self, fn):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fn()
        return err.getvalue()

    def test_tool_call_logged_to_host(self):
        out = self.logged(lambda: self.call("stack_logs", {"service": "web", "tail": 5}))
        self.assertRegex(out, r'\[shop\] "stack_logs" \{"service": "web", "tail": 5\} -> ok \([\d.]+s\)')

    def test_refusal_reason_logged_and_audited(self):
        cfg = self.good_config()
        cfg["services"]["db"]["privileged"] = True
        (self.proj_dir / "resolved.json").write_text(json.dumps(cfg))
        out = self.logged(lambda: self.call("stack_up"))
        self.assertIn('"stack_up" {} -> refused', out)
        self.assertIn("differs from the version the human approved", out)
        rec = self.audit_records()[-1]
        self.assertEqual(rec["outcome"], "refused")
        self.assertIn("differs from the version the human approved", rec["detail"])

    def test_long_calls_logged_at_start(self):
        out = self.logged(lambda: self.call("stack_up", {"wait": True}))
        lines = [l for l in out.splitlines() if '"stack_up"' in l]
        self.assertEqual(len(lines), 2, out)
        self.assertRegex(lines[0], r'\[shop\] "stack_up" \{"wait": true\} -> started$')
        self.assertRegex(lines[1], r'-> ok \([\d.]+s\)$')
        start, end = self.audit_records()[-2:]
        self.assertEqual((start["outcome"], end["outcome"]), ("started", "ok"))
        self.assertEqual(start["call"], end["call"])
        self.assertNotIn("duration_s", start)
        self.assertIn("duration_s", end)

    def test_short_calls_logged_once(self):
        out = self.logged(lambda: self.call("stack_status"))
        self.assertNotIn("started", out)
        self.assertEqual(len(self.audit_records()), 1)

    def test_invalid_and_unknown_calls_audited(self):
        self.logged(lambda: self.call("stack_logs", {"tail": 99999}))
        self.logged(lambda: self.call("rm_rf", {}))
        a, b = self.audit_records()[-2:]
        self.assertEqual((a["tool"], a["outcome"]), ("stack_logs", "invalid"))
        self.assertIn("tail must be", a["detail"])
        self.assertEqual((b["tool"], b["outcome"]), ("rm_rf", "invalid"))

    def test_agent_values_escaped_in_host_log(self):
        out = self.logged(lambda: self.call("\x1b[2Jevil", {"x": "\x1b]0;pwned\x07"}))
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("\\u001b[2Jevil", out)

    def test_session_start_logs_client(self):
        out = self.logged(lambda: self.rpc("initialize", {
            "protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-code", "version": "2.1"}}))
        self.assertIn('[shop] session started (test): client "claude-code" "2.1", protocol "2025-06-18"', out)

    def test_lock_rejects_concurrent_mutation(self):
        p = self.session.broker.project("shop")
        with self.session.broker.exclusive(p, "stack_up"):
            r = self.call("stack_down")["result"]
        self.assertTrue(r["isError"])
        text = r["content"][0]["text"]
        self.assertIn("stack_up is already running on this stack", text)
        self.assertIn("Do not call stack_up", text)


class SandboxFlowTests(FlowBase):
    """SBX-1, SBX-5..8 as seen through the tools."""

    def run_calls(self):
        return [c for c in docker_calls(self.home) if c[:1] == ["run"]]

    def test_every_compose_call_runs_in_sandbox(self):
        self.call("stack_up")
        self.call("stack_logs")
        self.call("stack_restart")
        calls = docker_calls(self.home)
        self.assertFalse([c for c in calls if c[:1] == ["compose"]], "compose ran on the host")
        kinds = [c[c.index("--label") + 3].split("=", 1)[1] for c in self.run_calls()]
        self.assertEqual(kinds, ["config", "up", "ps", "logs", "restart", "ps"])

    def test_sandbox_run_options(self):
        self.call("stack_status")
        run = self.run_calls()[0]
        opts = run[:run.index(FAKE_IMAGE_ID)]
        for flag in ("--rm", "--read-only", "--init"):
            self.assertIn(flag, opts)
        pairs = dict(zip(opts[1::1], opts[2::1]))
        self.assertEqual(pairs["--network"], "bridge")
        self.assertEqual(pairs["--cap-drop"], "ALL")
        self.assertEqual(pairs["--log-driver"], "none")
        self.assertEqual(pairs["--entrypoint"], "/usr/local/bin/docker")
        mounts = [opts[i + 1] for i, a in enumerate(opts) if a == "--mount"]
        binds = [m for m in mounts if "type=bind" in m]
        # Exactly: the daemon socket, the project (read-only, same path), the
        # runtime file and the empty env file (read-only, under /cb).
        self.assertEqual(len(binds), 4, binds)
        self.assertIn(f"type=bind,src={self.p.dir},dst={self.p.dir},readonly=true", binds)
        self.assertTrue(any("dst=/var/run/docker.sock" in m for m in binds))
        for m in binds:
            if "docker.sock" not in m:
                self.assertIn("readonly=true", m)
        self.assertFalse([m for m in binds if str(self.home) in m and "dst=/cb/" not in m])
        envs = [opts[i + 1] for i, a in enumerate(opts) if a == "--env"]
        self.assertIn("DOCKER_HOST=unix:///var/run/docker.sock", envs)
        self.assertIn("HOME=/tmp", envs)

    def write_fake(self, **cfg):
        (self.home / "fake-docker.json").write_text(json.dumps(cfg))

    def test_busy_when_mutating_sandbox_still_running(self):
        self.write_fake(containers=[{"id": "c1", "name": "cb-broker-shop-up-1", "op": "up",
                                     "image": FAKE_IMAGE_ID}])
        r = self.call("stack_down")["result"]
        self.assertTrue(r["isError"])
        self.assertIn("still running", r["content"][0]["text"])

    def test_lookalike_labels_do_not_block(self):
        # SBX-7: a container from another image carrying the label does not count.
        self.write_fake(containers=[{"id": "c1", "name": "evil", "op": "up",
                                     "image": "sha256:" + "0" * 64}])
        self.assertFalse(self.call("stack_down")["result"]["isError"])
        # A read-only sandbox (status/logs) running does not block either.
        self.write_fake(containers=[{"id": "c1", "name": "cb-broker-shop-logs-1", "op": "logs",
                                     "image": FAKE_IMAGE_ID}])
        self.assertFalse(self.call("stack_down")["result"]["isError"])

    def test_mutating_sandbox_removed_after_run(self):
        self.call("stack_restart")
        calls = docker_calls(self.home)
        run = next(c for c in calls if c[:1] == ["run"] and "com.compose-broker.op=restart" in c)
        name = run[run.index("--name") + 1]
        self.assertIn(["rm", "-f", name], calls)
