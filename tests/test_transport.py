import json
import socket
import threading
from unittest import mock

from compose_broker import mcp, paths
from compose_broker.broker import Broker
from compose_broker.cli import cmd_token
from compose_broker.registry import Registry, approved_path
from compose_broker.server import TCPServer, UnixServer, prepare_socket_path
from compose_broker.util import atomic_write, digest, now_iso, pretty
from tests.helpers import Env, fake_sandbox


class TransportTests(Env):
    def test_unix_socket_roundtrip_unidentified(self):
        broker = Broker(fake_sandbox(self.docker_bin))
        path = self.root / "s" / "broker.sock"
        prepare_socket_path(path)
        srv = UnixServer(path, broker)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            c = socket.socket(socket.AF_UNIX)
            c.connect(str(path))
            f = c.makefile("rwb")
            f.write(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}\n')
            f.write(b'not json\n')
            f.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
            f.flush()
            r1, r2, r3 = (json.loads(f.readline()) for _ in range(3))
            self.assertIn("not associated", r1["result"]["instructions"])
            self.assertEqual(r2["error"]["code"], -32700)
            self.assertEqual(r3["result"]["tools"], [])
            c.close()
        finally:
            srv.shutdown()
            srv.server_close()

    def test_tcp_requires_token(self):
        Registry.save({"shop": self.p})
        cmd_token(type("A", (), {"name": "shop"})())
        token = (paths.tokens_dir() / "shop").read_text().strip()
        broker = Broker(fake_sandbox(self.docker_bin))
        srv = TCPServer(("127.0.0.1", 0), broker)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            bad = socket.create_connection(("127.0.0.1", port))
            bad.sendall(b"AUTH " + b"x" * 43 + b"\n" + b'{"jsonrpc":"2.0","id":1,"method":"ping"}\n')
            self.assertEqual(bad.recv(100), b"")  # closed without a response
            bad.close()
            good = socket.create_connection(("127.0.0.1", port))
            f = good.makefile("rwb")
            f.write(f"AUTH {token}\n".encode() + b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n')
            f.flush()
            self.assertEqual(len(json.loads(f.readline())["result"]["tools"]), 5)
            good.close()
        finally:
            srv.shutdown()
            srv.server_close()


class ConcurrencyTests(Env):
    """MCP-7, MCP-8: one connection keeps answering while a tool call runs."""

    def setUp(self):
        super().setUp()
        cfg = self.good_config()
        (self.proj_dir / "resolved.json").write_text(json.dumps(cfg))
        self.p.approved_sha256, self.p.approved_at = digest(cfg), now_iso()
        Registry.save({"shop": self.p})
        atomic_write(approved_path("shop"), pretty(cfg))
        cmd_token(type("A", (), {"name": "shop"})())
        token = (paths.tokens_dir() / "shop").read_text().strip()
        self.srv = TCPServer(("127.0.0.1", 0), Broker(fake_sandbox(self.docker_bin)))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.sock = socket.create_connection(("127.0.0.1", self.srv.server_address[1]))
        self.sock.settimeout(10)
        self.addCleanup(self.sock.close)
        self.f = self.sock.makefile("rwb")
        self.f.write(f"AUTH {token}\n".encode())
        self.f.flush()
        self.fake(sleep={"up": 1.5})

    def fake(self, **kw):
        (self.home / "fake-docker.json").write_text(json.dumps(kw))

    def send(self, mid, method, params=None):
        self.f.write(json.dumps({"jsonrpc": "2.0", "id": mid, "method": method,
                                 "params": params or {}}).encode() + b"\n")
        self.f.flush()

    def call(self, mid, tool, args=None, **extra):
        self.send(mid, "tools/call", {"name": tool, "arguments": args or {}, **extra})

    def read(self):
        line = self.f.readline()
        self.assertTrue(line, "connection closed")
        return json.loads(line)

    def read_until(self, mid):
        """Messages up to and including the response with this id."""
        out = []
        while True:
            out.append(self.read())
            if out[-1].get("id") == mid:
                return out

    @staticmethod
    def text(msg):
        return msg["result"]["content"][0]["text"]

    def test_connection_answers_while_up_runs(self):
        self.call(1, "stack_up")
        self.send(2, "ping")
        self.call(3, "stack_status")
        self.call(4, "stack_up")        # identical call: refused, not queued
        msgs = [self.read(), self.read(), self.read()]
        # By now stack_up's worker holds the project lock (it took it before its
        # first compose call, and stack_status needed one of its own).
        self.call(5, "stack_down")      # other mutating call: project lock
        msgs += self.read_until(1)
        ids = [m["id"] for m in msgs]
        self.assertEqual(ids[-1], 1)
        self.assertEqual(sorted(ids[:-1]), [2, 3, 4, 5])  # all answered before the slow up
        by_id = {m["id"]: m for m in msgs}
        self.assertEqual(by_id[2]["result"], {})
        self.assertIn("web: running", self.text(by_id[3]))
        self.assertTrue(by_id[4]["result"]["isError"])
        self.assertIn("identical stack_up call is already running", self.text(by_id[4]))
        self.assertTrue(by_id[5]["result"]["isError"])
        self.assertIn("stack_up is already running on this stack", self.text(by_id[5]))
        self.assertFalse(by_id[1]["result"]["isError"], self.text(by_id[1]))
        # Once it has returned, the same call is allowed again.
        self.call(6, "stack_up")
        self.assertFalse(self.read_until(6)[-1]["result"]["isError"])

    def test_inflight_cap(self):
        self.fake(sleep={"up": 1.5, "logs": 1.5})
        with mock.patch.object(mcp, "MAX_INFLIGHT_CALLS", 2):
            self.call(1, "stack_up")
            self.call(2, "stack_logs")
            self.call(3, "stack_logs", {"tail": 5})
            first = self.read()
            self.assertEqual(first["id"], 3)
            self.assertTrue(first["result"]["isError"])
            self.assertIn("already running on this connection", self.text(first))
            seen = {}
            while not {1, 2} <= seen.keys():
                m = self.read()
                seen[m["id"]] = m
            self.assertFalse(seen[1]["result"]["isError"])
            self.assertFalse(seen[2]["result"]["isError"])

    def test_progress_heartbeats_with_token(self):
        with mock.patch.object(mcp, "PROGRESS_INTERVAL_S", 0.2):
            self.call(1, "stack_up", _meta={"progressToken": "tok-1"})
            msgs = self.read_until(1)
            self.call(2, "stack_up")  # no token: no heartbeats
            later = self.read_until(2)
        beats = [m for m in msgs if m.get("method") == "notifications/progress"]
        self.assertGreaterEqual(len(beats), 2)
        self.assertTrue(all(b["params"]["progressToken"] == "tok-1" for b in beats))
        self.assertEqual([b["params"]["progress"] for b in beats],
                         list(range(1, len(beats) + 1)))
        self.assertIn("stack_up still running", beats[0]["params"]["message"])
        self.assertEqual([m.get("id") for m in later], [2])  # nothing after the response

    def test_bad_progress_token_ignored(self):
        with mock.patch.object(mcp, "PROGRESS_INTERVAL_S", 0.2):
            self.call(1, "stack_up", _meta={"progressToken": {"x": 1}})
            msgs = self.read_until(1)
        self.assertEqual([m.get("id") for m in msgs], [1])

    def test_response_delivered_after_client_half_close(self):
        self.call(1, "stack_up")
        self.sock.shutdown(socket.SHUT_WR)
        msgs = self.read_until(1)
        self.assertFalse(msgs[-1]["result"]["isError"])
