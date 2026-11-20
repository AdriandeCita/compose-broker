"""Unix and TCP listeners; one MCP session per connection (SPEC 5.5, MCP-1)."""
from __future__ import annotations

import json
import socket
import socketserver
import stat
import threading
from pathlib import Path

from . import identity
from .broker import Broker
from .mcp import Session, rpc_error
from .util import ensure_dir, log

MAX_MESSAGE_BYTES = 1 << 20


class Handler(socketserver.StreamRequestHandler):
    """One connection. Lines are read in order; tool calls answer from worker
    threads (MCP-7), so writes go through one lock."""

    def setup(self) -> None:
        super().setup()
        self._write_lock = threading.Lock()

    def handle(self) -> None:
        server = self.server
        broker: Broker = server.broker  # type: ignore[attr-defined]
        transport: str = server.transport  # type: ignore[attr-defined]
        if transport == "unix":
            project, why = identity.identify_unix(broker.docker, self.request)
        else:
            project, why = identity.identify_tcp(self.request, self.rfile)
        log(f"connection ({transport}): {'project ' + project if project else 'UNIDENTIFIED'} — {why}")
        if transport == "tcp" and project is None:
            return  # ID-5: unauthenticated network peers get nothing
        session = Session(broker, project, transport, notify=self._send)
        try:
            self._serve_session(session)
        finally:
            session.wait_idle()
            log(f"[{project or 'UNIDENTIFIED'}] connection closed ({transport}) after "
                f"{session.calls} tool call(s)")

    def _serve_session(self, session: Session) -> None:
        while True:
            try:
                line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
            except OSError:
                return
            if not line:
                return
            if len(line) > MAX_MESSAGE_BYTES:
                self._send(rpc_error(None, -32600, "message too large"))
                return
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self._send(rpc_error(None, -32700, "parse error"))
                continue
            session.submit(msg, self._send)

    def _send(self, obj: dict) -> bool:
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode()
        try:
            with self._write_lock:
                self.wfile.write(data)
                self.wfile.flush()
            return True
        except (OSError, ValueError):  # ValueError: the stream was closed
            return False


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, broker: Broker):
        self.broker, self.transport = broker, "unix"
        super().__init__(str(path), Handler)


class TCPServer(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], broker: Broker):
        self.broker, self.transport = broker, "tcp"
        if ":" in addr[0]:
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)


def prepare_socket_path(path: Path) -> None:
    ensure_dir(path.parent, 0o755)
    if path.exists() or path.is_symlink():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise SystemExit(f"{path} exists and is not a socket; refusing to replace it")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(path))
            raise SystemExit(f"another broker is already listening on {path}")
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink()
        finally:
            probe.close()
