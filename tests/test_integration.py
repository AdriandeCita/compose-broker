"""End-to-end tests against a real Docker daemon, using examples/demo.

Opt-in, because they build images and start containers on the host:

    CB_INTEGRATION=1 python3 -m unittest -v tests.test_integration

Flow: the real CLI registers and approves a copy of the demo project (real
`docker compose config`), a real TCP server runs in-process, and the tests talk
MCP over a socket exactly as an agent would. Unix-socket identity (ID-2) is not
covered: it needs the caller to be inside a container.
"""
import contextlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from compose_broker import cli, paths
from compose_broker.broker import Broker
from compose_broker.docker import Docker
from compose_broker.registry import Registry, load_approved
from compose_broker.sandbox import Sandbox, resolve_live
from compose_broker.server import TCPServer
from tests.helpers import isolate_env, restore_env

DEMO = Path(__file__).resolve().parent.parent / "examples" / "demo"


def _docker_unavailable() -> str | None:
    if os.environ.get("CB_INTEGRATION") != "1":
        return "set CB_INTEGRATION=1 to run (builds images, starts containers)"
    if not shutil.which("docker"):
        return "docker CLI not found"
    for args in (["docker", "info"], ["docker", "compose", "version"]):
        if subprocess.run(args, capture_output=True).returncode != 0:
            return f"`{' '.join(args)}` failed"
    return None


SKIP_REASON = _docker_unavailable()


class McpClient:
    """Minimal MCP client speaking the broker's TCP transport (ID-5)."""

    def __init__(self, port: int, token: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=600)
        self.f = self.sock.makefile("rwb")
        self.f.write(f"AUTH {token}\n".encode())
        self.next_id = 0

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        self.f.write(json.dumps(msg).encode() + b"\n")
        self.f.flush()
        return json.loads(self.f.readline())

    def call(self, tool: str, args: dict | None = None) -> tuple[str, bool]:
        r = self.request("tools/call", {"name": tool, "arguments": args or {}})
        if "error" in r:
            raise AssertionError(f"{tool} returned JSON-RPC error: {r['error']}")
        return r["result"]["content"][0]["text"], r["result"]["isError"]

    def close(self) -> None:
        self.sock.close()


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = cli.main(list(argv))
        except SystemExit as e:
            print(e)
            code = 1
    return code, out.getvalue()


def build_sandbox() -> Sandbox:
    """SBX-2..4: build (downloads are cached in the real ~/.cache) and open."""
    code, out = run_cli("sandbox", "build")
    assert code == 0, f"sandbox build failed:\n{out}"
    return Sandbox.open(Docker())


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class DemoStackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.saved_env = isolate_env(root, keep_docker_env=True)
        cls.name = f"it-{secrets.token_hex(3)}"
        cls.proj = root / "projects" / "demo"
        shutil.copytree(DEMO, cls.proj)
        try:
            sb = build_sandbox()
            code, out = run_cli("register", cls.name, str(cls.proj), "--no-approve")
            assert code == 0, out
            code, out = run_cli("approve", cls.name, "--yes")
            assert code == 0, f"approve failed:\n{out}"
            cls.approve_output = out
            code, out = run_cli("token", cls.name)
            assert code == 0, out
            token = (paths.tokens_dir() / cls.name).read_text().strip()

            cls.broker = Broker(sb)
            cls.server = TCPServer(("127.0.0.1", 0), cls.broker)
            threading.Thread(target=cls.server.serve_forever, daemon=True).start()
            cls.client = McpClient(cls.server.server_address[1], token)
            cls.client.request("initialize", {"protocolVersion": "2025-06-18"})
            text, is_error = cls.client.call("stack_up", {"wait": True})
            assert not is_error, f"stack_up failed:\n{text}"
            cls.up_output = text
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls):
        with contextlib.suppress(Exception):
            cls.client.call("stack_down")
            cls.client.close()
        with contextlib.suppress(Exception):
            cls.server.shutdown()
            cls.server.server_close()
        with contextlib.suppress(Exception):  # also drop the built image
            p = Registry.load()[cls.name]
            cls.broker.run_compose(p, cls.broker.ops_file(p, load_approved(p)),
                                   ["down", "--volumes", "--remove-orphans", "--rmi", "local"],
                                   "down")
        restore_env(cls.saved_env)
        cls.tmp.cleanup()

    def logs_until(self, predicate, args=None, timeout=30.0) -> str:
        deadline = time.monotonic() + timeout
        while True:
            text, is_error = self.client.call("stack_logs", args)
            self.assertFalse(is_error, text)
            if predicate(text) or time.monotonic() > deadline:
                return text
            time.sleep(1)

    # ------------------------------------------------------------------ tests

    def test_approval_roundtrip_is_exact(self):
        # APR-4 / SPEC V1: compose re-loads the stored config without changes.
        self.assertNotIn("WARNING", self.approve_output)

    def test_up_reports_running_service(self):
        self.assertIn("ticker: running", self.up_output)
        self.assertRegex(self.up_output, r"web: running .*healthy")

    def test_up_output_is_condensed(self):
        self.assertIn("Container cb-", self.up_output)
        self.assertNotRegex(self.up_output, r"(?m)^#\d+ ")  # no BuildKit step lines

    def test_status(self):
        text, is_error = self.client.call("stack_status")
        self.assertFalse(is_error, text)
        self.assertRegex(text, r"ticker: running")
        self.assertRegex(text, r"web: running")

    def test_cli_down_all_stops_stack(self):
        # CLI-1: the registry is isolated, so --all sees only this test's project.
        code, out = run_cli("down", "--all")
        self.assertEqual(code, 0, out)
        self.assertIn(f"{self.name}: stack cb-{self.name} is down", out)
        text, _ = self.client.call("stack_status")
        self.assertIn("No containers exist", text)
        code, out = run_cli("down", "--all")
        self.assertEqual(code, 0, out)
        self.assertIn("nothing to do", out)
        text, is_error = self.client.call("stack_up", {"wait": True})  # for the other tests
        self.assertFalse(is_error, text)

    # ---- web: nested build context (POL-S11, EXE-5a)

    def web_url(self, path: str) -> str:
        text, _ = self.client.call("stack_status")
        m = re.search(r"web: .*127\.0\.0\.1:(\d+)->8080/tcp", text)
        self.assertIsNotNone(m, text)
        return f"http://127.0.0.1:{m.group(1)}{path}"

    def http_get(self, path: str) -> tuple[int, str]:
        try:
            with urllib.request.urlopen(self.web_url(path), timeout=10) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            e.close()
            return e.code, ""

    def test_web_built_from_nested_context(self):
        # COPY . /www/ copied web/, not the project root.
        code, body = self.http_get("/index.html")
        self.assertEqual(code, 200)
        self.assertIn("hello from the web service", body)
        self.assertEqual(self.http_get("/web/index.html")[0], 404)
        self.assertEqual(self.http_get("/ticker/ticker.sh")[0], 404)

    def test_web_dockerignore_honoured(self):
        self.assertEqual(self.http_get("/drafts/notes.txt")[0], 404)
        self.assertEqual(self.http_get("/Dockerfile")[0], 404)

    def test_up_refuses_context_swapped_for_symlink(self):
        web = self.proj / "web"
        real = self.proj / "web.real"
        os.rename(web, real)
        try:
            os.symlink(real, web)
            text, is_error = self.client.call("stack_up")
            self.assertTrue(is_error, text)
            # If compose resolves the symlink itself, the live config changes and
            # stack_up refuses as drift instead. Either way nothing is built.
            self.assertRegex(text, r"symlink|differs from the version the human approved")
            self.assertNotIn(str(self.proj), text)
        finally:
            if web.is_symlink():
                web.unlink()
            os.rename(real, web)
        status, _ = self.client.call("stack_status")
        self.assertIn("web: running", status)

    def test_logs_stream_stdout_with_timestamps(self):
        text = self.logs_until(lambda t: len(re.findall(r"tick \d+", t)) >= 3)
        self.assertGreaterEqual(len(re.findall(r"tick \d+", text)), 3, text)
        self.assertRegex(text, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

    def test_logs_include_stderr(self):
        text = self.logs_until(lambda t: "warn: tick" in t)
        self.assertIn("warn: tick 5 is a multiple of 5", text)

    def test_logs_tail_limits_lines(self):
        self.logs_until(lambda t: len(re.findall(r"tick \d+", t)) >= 3)
        text, is_error = self.client.call("stack_logs", {"service": "ticker", "tail": 2})
        self.assertFalse(is_error, text)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertTrue(1 <= len(lines) <= 2, text)

    def test_logs_since(self):
        text = self.logs_until(lambda t: "tick" in t, {"since": "10m"})
        self.assertIn("tick", text)

    def test_logs_rejects_unknown_service(self):
        r = self.client.request("tools/call", {"name": "stack_logs",
                                               "arguments": {"service": "nope"}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_restart_restarts_process(self):
        text, is_error = self.client.call("stack_restart", {"service": "ticker"})
        self.assertFalse(is_error, text)
        text = self.logs_until(lambda t: t.count("ticker starting") >= 2)
        self.assertGreaterEqual(text.count("ticker starting"), 2, text)

    def test_up_refuses_unapproved_compose_change(self):
        compose = self.proj / "compose.yaml"
        original = compose.read_text()
        try:
            # Lands in the last service (web); must be a key web doesn't have yet.
            compose.write_text(original + '    stop_grace_period: 3s\n')
            text, is_error = self.client.call("stack_up")
            self.assertTrue(is_error, text)
            self.assertIn("differs from the version the human approved", text)
            self.assertIn(f"compose-broker approve {self.name}", text)
        finally:
            compose.write_text(original)
        status, _ = self.client.call("stack_status")
        self.assertIn("ticker: running", status)


ESCAPE_COMPOSE = """\
services:
  probe:
    build:
      context: ./ctx
      dockerfile: ../probe.Dockerfile
    env_file: probe.env
    command: ["sh", "-c", "cat /ctx/secret.txt 2>/dev/null || echo NO-SECRET; env; sleep 3600"]
"""


@unittest.skipIf(SKIP_REASON, SKIP_REASON or "")
class SandboxEscapeTests(unittest.TestCase):
    """5.9: symlinks the agent plants after approval resolve inside the sandbox,
    so host files never reach a build (V7, EXE-5a) or the config (V8)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.addCleanup(restore_env, isolate_env(root, keep_docker_env=True))
        self.secret = f"HOST-SECRET-{secrets.token_hex(8)}"
        self.host_dir = root / "host-only"
        self.host_dir.mkdir()
        (self.host_dir / "secret.txt").write_text(self.secret + "\n")
        (self.host_dir / "secret.env").write_text(f"LEAK={self.secret}\n")
        self.proj = root / "projects" / "esc"
        (self.proj / "ctx").mkdir(parents=True)
        (self.proj / "ctx" / "placeholder.txt").write_text("ok\n")
        (self.proj / "compose.yaml").write_text(ESCAPE_COMPOSE)
        (self.proj / "probe.Dockerfile").write_text("FROM busybox:1.36\nCOPY . /ctx/\n")
        (self.proj / "probe.env").write_text("LEAK=none\n")
        self.sb = build_sandbox()
        self.name = f"esc-{secrets.token_hex(3)}"
        code, out = run_cli("register", self.name, str(self.proj), "--no-approve")
        self.assertEqual(code, 0, out)
        code, out = run_cli("approve", self.name, "--yes")
        self.assertEqual(code, 0, out)
        self.broker = Broker(self.sb)
        self.addCleanup(self.teardown_stack)

    def teardown_stack(self):
        with contextlib.suppress(Exception):
            p = Registry.load()[self.name]
            self.broker.run_compose(p, self.broker.ops_file(p, load_approved(p)),
                                    ["down", "--volumes", "--remove-orphans", "--rmi", "local"],
                                    "down")

    def swap_for_symlink(self, rel: str, target: Path) -> None:
        path = self.proj / rel
        os.rename(path, self.proj / f"{rel}.real")
        os.symlink(target, path)

    def test_build_context_symlink_reads_nothing_from_host(self):
        # Positive control: the stack builds and runs before the swap, so a later
        # failure is caused by the swap, not by an unrelated problem (network etc.).
        r = self.broker.op_up(self.name, wait=False)
        self.assertTrue(r.ok, r.text)
        logs = ""
        for _ in range(30):
            logs = self.broker.op_logs(self.name, None, 200, None).text
            if "NO-SECRET" in logs:
                break
            time.sleep(1)
        self.assertIn("NO-SECRET", logs)
        self.swap_for_symlink("ctx", self.host_dir)
        try:
            r = self.broker.op_up(self.name, wait=False)
        except Exception as e:  # a refusal is fine too
            r = None
            self.assertNotIn(self.secret, str(e))
        if r is not None:
            self.assertNotIn(self.secret, r.text)
            if r.ok:
                logs = self.broker.op_logs(self.name, None, 200, None).text
                self.assertNotIn(self.secret, logs)

    def test_env_file_symlink_not_resolved_on_host(self):
        self.swap_for_symlink("probe.env", self.host_dir / "secret.env")
        p = Registry.load()[self.name]
        try:
            cfg = resolve_live(self.sb, p)
        except Exception as e:  # ResolveError: the target does not exist in the sandbox
            self.assertNotIn(self.secret, getattr(e, "detail", str(e)))
        else:
            self.assertNotIn(self.secret, json.dumps(cfg))
