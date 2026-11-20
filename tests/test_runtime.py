import os

from compose_broker.runtime import (PLACEHOLDER_DOCKERFILE, build_runtime, dockerfile_rel,
                                    read_project_file)
from compose_broker.util import BrokerError
from tests.helpers import Env


class RuntimeFileTests(Env):
    def test_reads_regular_dockerfile(self):
        self.assertEqual(read_project_file(self.p.dir, "Dockerfile"), "FROM alpine\n")

    def test_rejects_symlinked_file_and_dir(self):
        secret = self.root / "secret"
        secret.write_text("TOP SECRET")
        os.symlink(secret, self.proj_dir / "Evil.Dockerfile")
        with self.assertRaises(BrokerError) as ctx:
            read_project_file(self.p.dir, "Evil.Dockerfile")
        self.assertNotIn("TOP SECRET", str(ctx.exception))
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "Dockerfile").write_text("FROM x\n")
        os.symlink(outside, self.proj_dir / "linkdir")
        with self.assertRaises(BrokerError):
            read_project_file(self.p.dir, "linkdir/Dockerfile")

    def test_rejects_fifo(self):
        os.mkfifo(self.proj_dir / "fifo")
        with self.assertRaises(BrokerError):
            read_project_file(self.p.dir, "fifo")

    def test_dockerfile_rel(self):
        self.assertEqual(dockerfile_rel(self.p, "./svc/Dockerfile"), "svc/Dockerfile")
        self.assertEqual(dockerfile_rel(self.p, self.p.dir + "/svc/Dockerfile"), "svc/Dockerfile")
        for bad in ("../x", "/etc/passwd", "svc/../../x"):
            with self.assertRaises(BrokerError):
                dockerfile_rel(self.p, bad)

    def test_runtime_inlines_and_strips(self):
        rt = build_runtime(self.p, self.good_config(), for_build=True)
        self.assertEqual(rt["services"]["web"]["build"]["dockerfile_inline"], "FROM alpine\n")
        self.assertNotIn("dockerfile", rt["services"]["web"]["build"])
        self.assertNotIn("env_file", rt["services"]["db"])
        rt = build_runtime(self.p, self.good_config(), for_build=False)
        self.assertEqual(rt["services"]["web"]["build"]["dockerfile_inline"], PLACEHOLDER_DOCKERFILE)
