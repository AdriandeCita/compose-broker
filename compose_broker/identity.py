"""Caller identification (SPEC 5.5): which project is this connection?"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import struct
from pathlib import Path

from .docker import TIMEOUTS, Docker
from .registry import Registry

AUTH_TIMEOUT_S = 10
CONTAINER_ID_RE = re.compile(r"[0-9a-f]{64}")
TOKEN_LINE_RE = re.compile(rb"^AUTH ([A-Za-z0-9_-]{20,200})\r?\n$")


def identify_unix(docker: Docker, sock: socket.socket) -> tuple[str | None, str]:
    """ID-2: peer pid -> container id -> bind mounts -> the one project containing one."""
    try:
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                                struct.calcsize("3i"))
        pid, _uid, _gid = struct.unpack("3i", creds)
    except (OSError, AttributeError):
        return None, "peer credentials unavailable (unix mode requires Linux)"
    try:
        cgroup = Path(f"/proc/{pid}/cgroup").read_text()
    except OSError:
        return None, f"cannot read cgroup of pid {pid}"
    ids = CONTAINER_ID_RE.findall(cgroup)
    if not ids:
        return None, f"pid {pid} is not inside a docker container"
    cid = ids[-1]
    r = docker.run(["container", "inspect", "--format", "{{json .Mounts}}", cid],
                   timeout=TIMEOUTS["inspect"], merge_stderr=False)
    if r.code != 0:
        return None, f"docker inspect {cid[:12]} failed: {r.stderr.strip()}"
    try:
        mounts = json.loads(r.output) or []
    except ValueError:
        return None, f"docker inspect {cid[:12]} returned invalid JSON"
    sources = [os.path.normpath(m["Source"]) for m in mounts
               if m.get("Type") == "bind" and m.get("Source")]
    matches = {p.name for p in Registry.load().values()
               if any(p.dir == s or p.dir.startswith(s.rstrip("/") + "/") for s in sources)}
    if len(matches) != 1:
        return None, (f"container {cid[:12]} matches {len(matches)} registered projects "
                      f"({', '.join(sorted(matches)) or 'none'}); exactly one required")
    name = matches.pop()
    return name, f"container {cid[:12]} (pid {pid})"


def identify_tcp(sock: socket.socket, rfile) -> tuple[str | None, str]:
    """ID-5: first line must be `AUTH <token>`; compared by hash in constant time."""
    sock.settimeout(AUTH_TIMEOUT_S)
    try:
        line = rfile.readline(512)
    except OSError:
        return None, "no AUTH line within timeout"
    finally:
        sock.settimeout(None)
    m = TOKEN_LINE_RE.match(line or b"")
    if not m:
        return None, "malformed AUTH line"
    h = hashlib.sha256(m.group(1)).hexdigest()
    for p in Registry.load().values():
        if p.token_sha256 and hmac.compare_digest(p.token_sha256, h):
            return p.name, "token"
    return None, "unknown token"
