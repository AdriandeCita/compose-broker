"""On-disk locations (SPEC 4). Computed on every call from XDG variables."""
from __future__ import annotations

import os
from pathlib import Path


def _xdg(var: str, default: str) -> Path:
    v = os.environ.get(var)
    return Path(v) if v and os.path.isabs(v) else Path.home() / default


def config_dir() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "compose-broker"


def state_dir() -> Path:
    return _xdg("XDG_STATE_HOME", ".local/state") / "compose-broker"


def registry_path() -> Path:
    return config_dir() / "projects.json"


def approved_dir() -> Path:
    return config_dir() / "approved"


def tokens_dir() -> Path:
    return config_dir() / "tokens"


def runtime_dir() -> Path:
    return state_dir() / "runtime"


def sandbox_config() -> Path:
    return config_dir() / "sandbox.json"


def cache_dir() -> Path:
    return _xdg("XDG_CACHE_HOME", ".cache") / "compose-broker"


def audit_log() -> Path:
    return state_dir() / "audit.log"


def empty_env() -> Path:
    return state_dir() / "empty.env"


def default_socket_path() -> Path:
    rt = os.environ.get("XDG_RUNTIME_DIR")
    base = Path(rt) if rt and os.path.isabs(rt) else state_dir()
    return base / "compose-broker" / "broker.sock"
