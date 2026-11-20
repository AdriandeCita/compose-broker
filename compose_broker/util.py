"""Errors and small helpers shared by every module."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

MAX_TOOL_OUTPUT = 48 * 1024  # EXE-10


class BrokerError(Exception):
    """A refusal whose message is safe to show to the agent."""


class ResolveError(Exception):
    """compose could not resolve the config. `detail` is for the human only."""

    def __init__(self, detail: str):
        super().__init__("compose config could not be resolved")
        self.detail = detail


class InvalidParams(Exception):
    """JSON-RPC -32602."""


def log(msg: str) -> None:
    print(f"{now_iso()} {msg}", file=sys.stderr, flush=True)


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical(cfg: Any) -> bytes:
    """APR-2 canonical encoding."""
    return json.dumps(cfg, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode()


def digest(cfg: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(cfg)).hexdigest()


def pretty(cfg: Any) -> str:
    return json.dumps(cfg, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    os.chmod(path, mode)


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600) -> None:
    ensure_dir(path.parent)
    if isinstance(data, str):
        data = data.encode()
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def truncate_front(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= limit:
        return text
    kept = raw[-limit:].decode("utf-8", "ignore")
    nl = kept.find("\n")
    if 0 <= nl < 500:
        kept = kept[nl + 1:]
    dropped = len(raw) - len(kept.encode())
    return f"[... {dropped} bytes of earlier output truncated ...]\n{kept}"


def dirs_overlap(a: str, b: str) -> bool:
    return a == b or a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")
