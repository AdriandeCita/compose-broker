"""Registered projects and their approved configs (SPEC 5.1, 5.2)."""
from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import paths
from .util import BrokerError, atomic_write, digest, pretty

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")  # REG-2


@dataclass
class Project:
    name: str
    dir: str
    compose_files: list[str]
    compose_project: str
    allow_reset: bool = False
    token_sha256: str | None = None
    approved_sha256: str | None = None
    approved_at: str | None = None

    def compose_paths(self) -> list[str]:
        return [f if os.path.isabs(f) else os.path.join(self.dir, f)
                for f in self.compose_files]


class Registry:
    _write_lock = threading.Lock()

    @staticmethod
    def load() -> dict[str, Project]:
        try:
            raw = json.loads(paths.registry_path().read_text())
        except FileNotFoundError:
            return {}
        return {name: Project(name=name, **fields)
                for name, fields in raw.get("projects", {}).items()}

    @staticmethod
    def save(projects: dict[str, Project]) -> None:
        data = {"version": 1, "projects": {
            name: {k: v for k, v in asdict(p).items() if k != "name"}
            for name, p in sorted(projects.items())}}
        with Registry._write_lock:
            atomic_write(paths.registry_path(), pretty(data))

    @staticmethod
    def update(name: str, **changes: Any) -> Project:
        projects = Registry.load()
        if name not in projects:
            raise SystemExit(f"project {name!r} is not registered")
        for k, v in changes.items():
            setattr(projects[name], k, v)
        Registry.save(projects)
        return projects[name]


def approved_path(name: str) -> Path:
    return paths.approved_dir() / f"{name}.json"


def load_approved(p: Project) -> dict:
    """APR-5: return the approved config after verifying its integrity."""
    hint = f"ask the human to run `compose-broker approve {p.name}` on the host"
    if not p.approved_sha256:
        raise BrokerError(f"This project's compose configuration has not been approved yet; {hint}.")
    try:
        cfg = json.loads(approved_path(p.name).read_text())
    except (OSError, ValueError):
        raise BrokerError(f"The approved configuration is missing on the host; {hint}.")
    if digest(cfg) != p.approved_sha256:
        raise BrokerError(f"The approved configuration failed its integrity check; {hint}.")
    return cfg
