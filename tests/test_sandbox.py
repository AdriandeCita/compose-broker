import csv
import hashlib
import io
import json
import os
import socket
import tarfile
from pathlib import Path
from unittest import mock

from compose_broker import docker as docker_mod
from compose_broker import paths, sandbox
from compose_broker.docker import Docker
from compose_broker.sandbox import Endpoint, Sandbox, SandboxError, build_image
from compose_broker.util import atomic_write, pretty
from tests.helpers import FAKE_IMAGE_ID, Env, docker_calls, fake_sandbox


class SandboxEnv(Env):
    def setUp(self):
        super().setUp()
        self.sock_path = self.root / "d.sock"
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.bind(str(self.sock_path))
        self.addCleanup(self.sock.close)
        self.write_fake()
        self.docker = Docker(str(self.docker_bin))

    def write_fake(self, **cfg):
        cfg.setdefault("host", f"unix://{self.sock_path}")
        (self.home / "fake-docker.json").write_text(json.dumps(cfg))

    def record(self, **over):
        _, sha = sandbox.load_manifest()
        rec = {"image_id": FAKE_IMAGE_ID, "definition_sha256": sha,
               "versions": {"docker": "29.9.2", "compose": "5.6.1", "buildx": "0.38.1"}}
        rec.update(over)
        atomic_write(paths.sandbox_config(), pretty(rec))


class OpenTests(SandboxEnv):
    def test_opens_when_everything_matches(self):
        self.record()
        sb = Sandbox.open(self.docker)
        self.assertEqual(sb.image_id, FAKE_IMAGE_ID)
        self.assertEqual(sb.endpoint.socket, str(self.sock_path))
        self.assertFalse(sb.endpoint.rootless)

    def assert_refused(self, fragment):
        with self.assertRaises(SandboxError) as cm:
            Sandbox.open(self.docker)
        self.assertIn(fragment, str(cm.exception))

    def test_not_built(self):
        self.assert_refused("sandbox build")

    def test_manifest_changed(self):
        self.record(definition_sha256="0" * 64)
        self.assert_refused("definition (manifest or Dockerfile) changed")

    def test_image_missing(self):
        self.record()
        self.write_fake(images=[])
        self.assert_refused("is missing")

    def test_tool_version_mismatch(self):
        self.record()
        self.write_fake(versions={"docker": "29.9.2", "compose": "5.6.10", "buildx": "0.38.1"})
        self.assert_refused("compose reports")

    def test_recorded_versions_must_match_manifest(self):
        self.record(versions={"docker": "1.0", "compose": "5.6.1", "buildx": "0.38.1"})
        self.assert_refused("recorded docker version")

    def test_remote_endpoints_refused(self):
        self.record()
        for host in ("tcp://10.0.0.1:2376", "ssh://me@box", "npipe:////./pipe/docker_engine"):
            self.write_fake(host=host)
            self.assert_refused("unix socket")

    def test_tls_env_refused(self):
        self.record()
        with mock.patch.dict(os.environ, {"DOCKER_CERT_PATH": "/home/me/.docker/certs"}):
            self.assert_refused("DOCKER_CERT_PATH")

    def test_selinux_refused(self):
        self.record()
        self.write_fake(security_options=["name=seccomp,profile=builtin", "name=selinux"])
        self.assert_refused("SELinux")

    def test_rootless_detected(self):
        self.record()
        self.write_fake(security_options=["name=seccomp,profile=builtin", "name=rootless"])
        sb = Sandbox.open(self.docker)
        self.assertTrue(sb.endpoint.rootless)
        self.assertEqual(sb.user_args(), ["--user", "0:0"])


class ArgvTests(SandboxEnv):
    def test_rootful_user_and_socket_group(self):
        sb = Sandbox(self.docker, FAKE_IMAGE_ID, Endpoint(str(self.sock_path), rootless=False))
        gid = os.stat(self.sock_path).st_gid
        self.assertEqual(sb.user_args(),
                         ["--user", f"{os.getuid()}:{os.getgid()}", "--group-add", str(gid)])

    def test_file_mapping(self):
        sb = fake_sandbox(self.docker_bin)
        outside = self.root / "shared" / "base, \"odd\".yaml"
        outside.parent.mkdir()
        outside.write_text("services: {}\n")
        env = self.root / "empty.env"
        env.write_text("")
        sb.compose(self.p, [Path(self.p.dir) / "compose.yaml", outside], ["ps"], kind="ps",
                   env_file=env)
        run = docker_calls(self.home)[-1]
        inner = run[run.index(FAKE_IMAGE_ID) + 1:]
        files = [inner[i + 1] for i, a in enumerate(inner) if a == "-f"]
        self.assertEqual(files[0], f"{self.p.dir}/compose.yaml")
        self.assertEqual(files[1], '/cb/1/base, "odd".yaml')
        self.assertEqual(inner[inner.index("--env-file") + 1], "/cb/2/empty.env")
        mounts = [run[i + 1] for i, a in enumerate(run) if a == "--mount"]
        # CSV quoting keeps the comma and quotes inside one field each.
        parsed = [dict(x.split("=", 1) for x in next(csv.reader([m]))) for m in mounts]
        self.assertIn({"type": "bind", "src": str(outside), "dst": '/cb/1/base, "odd".yaml',
                       "readonly": "true"}, parsed)
        self.assertNotIn("--workdir", inner)
        self.assertEqual(run[run.index("--workdir") + 1], self.p.dir)

    def test_timeout_removes_container_before_kill(self):
        hang = self.root / "hang-docker"
        hang.write_text("#!/usr/bin/env python3\n"
                        "import json, os, signal, sys, time\n"
                        "args = sys.argv[1:]\n"
                        "open(os.environ['HOME'] + '/docker-calls.jsonl', 'a').write(json.dumps(args) + '\\n')\n"
                        "if args[:1] == ['run']:\n"
                        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                        "    time.sleep(30)\n")
        hang.chmod(0o755)
        sb = fake_sandbox(hang)
        with mock.patch.dict(docker_mod.TIMEOUTS, {"up": 0.3}), \
                mock.patch.object(docker_mod, "KILL_GRACE_S", 0.3):
            r = sb.compose(self.p, [self.root / "rt.json"], ["up"], kind="up")
        self.assertTrue(r.timed_out)
        calls = docker_calls(self.home)
        name = calls[0][calls[0].index("--name") + 1]
        self.assertEqual(calls[1], ["rm", "-f", name])  # SBX-8: before SIGKILL to the client
        self.assertEqual(calls[-1], ["rm", "-f", name])  # and confirmed after

    def test_cleanup_only_removes_own_image(self):
        sb = fake_sandbox(self.docker_bin)
        self.write_fake(containers=[
            {"id": "a", "name": "cb-broker-shop-up-1", "op": "up", "image": FAKE_IMAGE_ID},
            {"id": "b", "name": "lookalike", "op": "up", "image": "sha256:" + "0" * 64}])
        self.assertEqual(sb.cleanup(), 1)
        rms = [c for c in docker_calls(self.home) if c[:1] == ["rm"]]
        self.assertEqual(rms, [["rm", "-f", "cb-broker-shop-up-1"]])


class BuildTests(SandboxEnv):
    def setUp(self):
        super().setUp()
        self.dl = self.root / "dl"
        self.dl.mkdir()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"#!fake docker\n"
            info = tarfile.TarInfo("docker/docker")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        files = {"docker.tgz": buf.getvalue(), "compose": b"compose-bin", "buildx": b"buildx-bin"}
        for name, data in files.items():
            (self.dl / name).write_bytes(data)
        sha = {n: hashlib.sha256(d).hexdigest() for n, d in files.items()}
        self.manifest = {"version": 1, "tools": {
            "docker": {"version": "29.9.2", "file": "docker", "arch": {"amd64": {
                "url": (self.dl / "docker.tgz").as_uri(), "sha256": sha["docker.tgz"],
                "member": "docker/docker"}}},
            "compose": {"version": "5.6.1", "file": "docker-compose", "arch": {"amd64": {
                "url": (self.dl / "compose").as_uri(), "sha256": sha["compose"]}}},
            "buildx": {"version": "0.38.1", "file": "docker-buildx", "arch": {"amd64": {
                "url": (self.dl / "buildx").as_uri(), "sha256": sha["buildx"]}}}}}
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest))
        self.enterContext(mock.patch.object(sandbox, "MANIFEST_PATH", self.manifest_path))
        self.enterContext(mock.patch.object(sandbox, "host_arch", lambda: "amd64"))
        self.ca = self.root / "ca.pem"
        self.ca.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
        self.enterContext(mock.patch.object(sandbox, "CA_BUNDLE_CANDIDATES",
                                            ("/nonexistent/ca.crt", str(self.ca))))

    def test_build_records_image(self):
        rec = build_image(self.docker, say=lambda *_: None)
        self.assertEqual(rec["image_id"], FAKE_IMAGE_ID)
        stored = json.loads(paths.sandbox_config().read_text())
        self.assertEqual(stored["definition_sha256"], sandbox.load_manifest()[1])
        build = next(c for c in docker_calls(self.home) if c[:1] == ["build"])
        self.assertEqual(build[build.index("--network") + 1], "none")
        ctx = build[-1]
        self.assertTrue(ctx.startswith(str(paths.state_dir())))
        self.assertFalse(os.path.exists(ctx))  # build dir removed afterwards
        self.assertEqual(stored["ca_bundle"], str(self.ca))
        self.assertEqual(stored["ca_bundle_sha256"],
                         hashlib.sha256(self.ca.read_bytes()).hexdigest())
        Sandbox.open(self.docker)  # and the recorded image passes the startup check

    def test_checksum_mismatch_aborts(self):
        (self.dl / "compose").write_bytes(b"tampered")
        with self.assertRaises(SandboxError) as cm:
            build_image(self.docker, say=lambda *_: None)
        self.assertIn("checksum mismatch", str(cm.exception))
        self.assertFalse(paths.sandbox_config().exists())
        cached = list((paths.cache_dir() / "sandbox").iterdir())
        self.assertFalse([c for c in cached if c.name.startswith("compose-")], cached)
        self.assertFalse([c for c in docker_calls(self.home) if c[:1] == ["build"]])

    def test_no_ca_bundle_aborts(self):
        with mock.patch.object(sandbox, "CA_BUNDLE_CANDIDATES", ("/nonexistent/ca.crt",)):
            with self.assertRaises(SandboxError) as cm:
                build_image(self.docker, say=lambda *_: None)
        self.assertIn("CA certificate bundle", str(cm.exception))

    def test_vendored_manifest_is_complete(self):
        real = json.loads((sandbox.IMAGE_DIR / "manifest.json").read_text())
        for tool, spec in real["tools"].items():
            self.assertEqual(set(spec["arch"]), {"amd64", "arm64"}, tool)
            for a in spec["arch"].values():
                self.assertRegex(a["sha256"], r"^[0-9a-f]{64}$")
                self.assertTrue(a["url"].startswith("https://"))
