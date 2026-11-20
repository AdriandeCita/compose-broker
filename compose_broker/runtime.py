"""Safe reads inside the project and the generated runtime file (EXE-2/4/5)."""
from __future__ import annotations

import copy
import os
import posixpath
import stat
from pathlib import Path

from . import paths
from .registry import Project
from .util import BrokerError, atomic_write, pretty

MAX_DOCKERFILE_BYTES = 1 << 20
PLACEHOLDER_DOCKERFILE = "FROM scratch\n"


def project_rel(p: Project, path: str, what: str, base: str = "") -> str:
    """Normalise `path` (absolute, or relative to project-relative `base`) to a
    project-relative path. Raises if it leaves the project. "" means the root."""
    if os.path.isabs(path):
        norm = os.path.normpath(path)
        if norm == p.dir:
            return ""
        if not norm.startswith(p.dir.rstrip("/") + "/"):
            raise BrokerError(f"{what} must be inside the project directory")
        return os.path.relpath(norm, p.dir)
    rel = posixpath.normpath(posixpath.join(base, path)) if (base or path) else "."
    if rel == ".":
        return ""
    if rel == ".." or rel.startswith("../") or rel.startswith("/"):
        raise BrokerError(f"{what} path must stay inside the project directory")
    return rel


def context_rel(p: Project, build: dict) -> str:
    """Project-relative build context ("" for the project root)."""
    ctx = build.get("context")
    if not isinstance(ctx, str) or not os.path.isabs(ctx):
        raise BrokerError("build context must be a directory inside the project")
    return project_rel(p, ctx, "build context")


def dockerfile_rel(p: Project, dockerfile: str, ctx_rel: str = "") -> str:
    """Project-relative Dockerfile path. Relative paths are relative to the
    build context, as in compose, and may use `..` as long as they stay inside
    the project. The broker reads the normalised path itself (EXE-5)."""
    rel = project_rel(p, dockerfile, "Dockerfile", base=ctx_rel)
    if not rel:
        raise BrokerError("Dockerfile path is empty")
    return rel


def read_project_file(root: str, rel: str, max_bytes: int = MAX_DOCKERFILE_BYTES) -> str:
    """Read root/rel without following any symlink below root."""
    parts = rel.split("/")
    err = BrokerError(f"Dockerfile {rel!r} could not be read safely (it must be a regular "
                      f"file under {max_bytes} bytes, not reached through a symlink)")
    fds: list[int] = []
    try:
        fds.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY))
        for comp in parts[:-1]:
            fds.append(os.open(comp, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=fds[-1]))
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=fds[-1])
        fds.append(fd)
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > max_bytes:
            raise err
        data = os.read(fd, max_bytes + 1)
        if len(data) > max_bytes:
            raise err
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise err
    finally:
        for fd in reversed(fds):
            os.close(fd)


def build_runtime(p: Project, cfg: dict, *, for_build: bool) -> dict:
    """EXE-2/4/5: derive the file compose actually runs from the approved config."""
    rt = copy.deepcopy(cfg)
    for svc in (rt.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        svc.pop("env_file", None)
        build = svc.get("build")
        if not isinstance(build, dict):
            continue
        if for_build:
            if "dockerfile_inline" not in build:
                rel = dockerfile_rel(p, build.get("dockerfile") or "Dockerfile",
                                     context_rel(p, build))
                build["dockerfile_inline"] = read_project_file(p.dir, rel)
        else:
            build["dockerfile_inline"] = PLACEHOLDER_DOCKERFILE
        build.pop("dockerfile", None)
    return rt


def write_runtime(p: Project, cfg: dict, kind: str) -> Path:
    path = paths.runtime_dir() / f"{p.name}.{kind}.json"
    atomic_write(path, pretty(cfg))
    return path
