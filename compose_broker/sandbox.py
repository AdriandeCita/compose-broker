"""The compose sandbox (SPEC 5.9): every `docker compose` runs in a short-lived
container that sees only the project (read-only), the broker's own files for
the call, and the daemon socket. A symlink the agent plants in the project then
resolves inside that container, where there is nothing worth reading.

Contents: preconditions (SBX-9), image build and verification (SBX-2..4), the
`docker run` argv (SBX-5, SBX-6), names/labels/busy/cleanup (SBX-7, SBX-8), and
the compose entry points used by the rest of the broker.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .docker import TIMEOUTS, Docker, RunResult
from .registry import Project
from .util import ResolveError, atomic_write, ensure_dir, log, now_iso, pretty

IMAGE_DIR = Path(__file__).resolve().parent / "sandbox_image"
MANIFEST_PATH = IMAGE_DIR / "manifest.json"
IMAGE_TAG = "compose-broker-sandbox"

LABEL_PROJECT = "com.compose-broker.sandbox"
LABEL_OP = "com.compose-broker.op"
MUTATING_OPS = ("up", "down", "restart")

DOCKER_IN = "/usr/local/bin/docker"
SOCKET_IN = "/var/run/docker.sock"
FILES_IN = "/cb"
TMP_IN = "/tmp"
TMPFS_BYTES = 64 << 20
MEMORY_LIMIT = "1g"
PIDS_LIMIT = "512"
SANDBOX_ENV = {  # SBX-6: the complete environment. Readable by the agent via a swap.
    "PATH": "/usr/local/bin",
    "HOME": TMP_IN,
    "DOCKER_CONFIG": f"{TMP_IN}/docker",
    "LANG": "C.UTF-8",
    "DOCKER_HOST": f"unix://{SOCKET_IN}",
    "BUILDKIT_PROGRESS": "plain",
    "COMPOSE_ANSI": "never",
    "DOCKER_CLI_HINTS": "false",
}
VERSION_COMMANDS = {  # tool -> argv after the docker entrypoint (SBX-4)
    "docker": ["--version"],
    "compose": ["compose", "version", "--short"],
    "buildx": ["buildx", "version"],
}
# The client verifies registry TLS itself, so the image needs CA certificates.
# Taken from the (trusted) host at build time, which keeps corporate CAs working.
CA_BUNDLE_CANDIDATES = ("/etc/ssl/certs/ca-certificates.crt",   # Debian, Ubuntu, Arch, Alpine
                        "/etc/pki/tls/certs/ca-bundle.crt",       # Fedora, RHEL
                        "/etc/ssl/ca-bundle.pem",                 # openSUSE
                        "/etc/ssl/cert.pem")
CA_BUNDLE_FILE = "ca-certificates.crt"
ARCH_ALIASES = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}
DOWNLOAD_TIMEOUT_S = 120


class SandboxError(Exception):
    """The sandbox cannot be used. The message is for the human on the host."""


# ----------------------------------------------------------------- manifest / config

def load_manifest() -> tuple[dict, str]:
    """The manifest, and the digest of the whole image definition (manifest and
    Dockerfile), so a change to either one requires a rebuild (SBX-4)."""
    raw = MANIFEST_PATH.read_bytes()
    h = hashlib.sha256()
    for part in (raw, (IMAGE_DIR / "Dockerfile").read_bytes()):
        h.update(hashlib.sha256(part).digest())
    return json.loads(raw), h.hexdigest()


def load_config() -> dict:
    try:
        return json.loads(paths.sandbox_config().read_text())
    except FileNotFoundError:
        raise SandboxError("the sandbox image has not been built; run `compose-broker sandbox build`")
    except (OSError, ValueError) as e:
        raise SandboxError(f"{paths.sandbox_config()} is unreadable ({e}); "
                           "run `compose-broker sandbox build`")


def host_arch() -> str:
    arch = ARCH_ALIASES.get(platform.machine().lower())
    if arch is None:
        raise SandboxError(f"unsupported architecture {platform.machine()!r}")
    return arch


def version_matches(expected: str, output: str) -> bool:
    return re.search(rf"(?<![\w.])v?{re.escape(expected)}(?![\w.])", output) is not None


# ----------------------------------------------------------------- preconditions (SBX-9)

@dataclass
class Endpoint:
    socket: str           # host path of the daemon's unix socket
    rootless: bool


def probe_endpoint(docker: Docker) -> Endpoint:
    for var in ("DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        if os.environ.get(var):
            raise SandboxError(f"{var} is set; the sandbox supports only a local daemon on a "
                               "unix socket (SPEC SBX-9)")
    r = docker.run(["context", "inspect", "--format", "{{json .Endpoints.docker}}"],
                   timeout=TIMEOUTS["sandbox"], merge_stderr=False)
    if r.code != 0:
        raise SandboxError(f"`docker context inspect` failed: {r.stderr.strip()}")
    try:
        host = json.loads(r.output).get("Host", "")
    except (ValueError, AttributeError):
        raise SandboxError("`docker context inspect` returned unexpected output")
    if not host.startswith("unix://"):
        raise SandboxError(f"the docker endpoint is {host!r}; the sandbox supports only a local "
                           "daemon on a unix socket (SPEC SBX-9)")
    sock = host[len("unix://"):]
    try:
        if not stat.S_ISSOCK(os.stat(sock).st_mode):
            raise SandboxError(f"{sock} is not a socket")
    except OSError as e:
        raise SandboxError(f"docker socket {sock} is not accessible: {e}")
    r = docker.run(["info", "--format", "{{json .SecurityOptions}}"],
                   timeout=TIMEOUTS["sandbox"], merge_stderr=False)
    if r.code != 0:
        raise SandboxError(f"`docker info` failed: {r.stderr.strip()}")
    try:
        opts = json.loads(r.output) or []
    except ValueError:
        raise SandboxError("`docker info` returned unexpected output")
    if any("selinux" in str(o) for o in opts):
        raise SandboxError("the daemon runs with SELinux; the sandbox does not support it yet "
                           "(SPEC SBX-9)")
    return Endpoint(socket=sock, rootless=any("rootless" in str(o) for o in opts))


# ----------------------------------------------------------------- the sandbox

def _mount(**fields: str) -> str:
    """--mount value, CSV-quoted so paths with commas or quotes stay intact."""
    buf = io.StringIO()
    csv.writer(buf, lineterminator="").writerow(f"{k}={v}" for k, v in fields.items())
    return buf.getvalue()


class Sandbox:
    def __init__(self, docker: Docker, image_id: str, endpoint: Endpoint):
        self.docker = docker
        self.image_id = image_id
        self.endpoint = endpoint

    @classmethod
    def open(cls, docker: Docker) -> "Sandbox":
        """SBX-4, SBX-9: check preconditions and the recorded image, or raise."""
        endpoint = probe_endpoint(docker)
        cfg = load_config()
        _, definition_sha = load_manifest()
        if cfg.get("definition_sha256") != definition_sha:
            raise SandboxError("the sandbox image definition (manifest or Dockerfile) changed "
                               "since the image was built; run `compose-broker sandbox build`")
        image_id = str(cfg.get("image_id", ""))
        r = docker.run(["image", "inspect", "--format", "{{.Id}}", image_id],
                       timeout=TIMEOUTS["sandbox"], merge_stderr=False)
        if r.code != 0 or r.output.strip() != image_id:
            raise SandboxError(f"sandbox image {image_id[:19]} is missing; "
                               "run `compose-broker sandbox build`")
        sb = cls(docker, image_id, endpoint)
        sb.verify_versions(cfg.get("versions") or {})
        return sb

    # ---- argv (SBX-5, SBX-6)

    def user_args(self) -> list[str]:
        if self.endpoint.rootless:
            return ["--user", "0:0"]   # container root is the host user
        gid = os.stat(self.endpoint.socket).st_gid
        return ["--user", f"{os.getuid()}:{os.getgid()}", "--group-add", str(gid)]

    def run_args(self, name: str, project: str, op: str, mounts: list[str],
                 workdir: str | None) -> list[str]:
        args = ["run", "--rm", "--name", name,
                "--label", f"{LABEL_PROJECT}={project}", "--label", f"{LABEL_OP}={op}",
                # The client fetches registry auth tokens itself, even for anonymous
                # pulls, so it needs a network. bridge, not host: no host loopback.
                "--network", "bridge", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--init", "--log-driver", "none",
                "--pids-limit", PIDS_LIMIT, "--memory", MEMORY_LIMIT,
                *self.user_args(),
                "--mount", _mount(type="tmpfs", dst=TMP_IN, **{"tmpfs-size": str(TMPFS_BYTES),
                                                                "tmpfs-mode": "1777"}),
                "--mount", _mount(type="bind", src=self.endpoint.socket, dst=SOCKET_IN)]
        for m in mounts:
            args += ["--mount", m]
        if workdir:
            args += ["--workdir", workdir]
        for k, v in SANDBOX_ENV.items():
            args += ["--env", f"{k}={v}"]
        return args + ["--entrypoint", DOCKER_IN, self.image_id]

    # ---- running

    def run(self, project: str, op: str, command: list[str], *, timeout: int,
            mounts: list[str] = (), workdir: str | None = None,
            merge_stderr: bool = True) -> RunResult:
        name = f"cb-broker-{project}-{op}-{secrets.token_hex(4)}"
        argv = self.run_args(name, project, op, list(mounts), workdir) + command
        ensure_dir(paths.state_dir())
        r = self.docker.run(argv, timeout=timeout, cwd=str(paths.state_dir()),
                            merge_stderr=merge_stderr, on_timeout=lambda: self.remove(name))
        if r.timed_out or op in MUTATING_OPS:
            # SBX-8: the lock is released only once the container is gone.
            self.remove(name)
        return r

    def remove(self, name: str) -> None:
        r = self.docker.run(["rm", "-f", name], timeout=TIMEOUTS["sandbox"], merge_stderr=False)
        if r.code != 0 and "no such container" not in r.stderr.lower():
            log(f"WARNING: could not remove sandbox container {name}: {r.stderr.strip()}")

    def compose(self, p: Project, files: list[str | Path], args: list[str], *, kind: str,
                env_file: Path | None = None, merge_stderr: bool = True) -> RunResult:
        """`docker compose` for project p. Files inside the project are used at
        their own path (the project is mounted there); any other file (broker
        runtime files, the empty env file, compose files registered from outside
        the project) is mounted read-only under /cb."""
        mounts = [_mount(type="bind", src=p.dir, dst=p.dir, readonly="true")]
        inner: list[str] = []
        for i, f in enumerate([*files, *([env_file] if env_file else [])]):
            f = str(f)
            if f.startswith(p.dir.rstrip("/") + "/"):
                inner.append(f)
            else:
                dst = f"{FILES_IN}/{i}/{os.path.basename(f)}"
                mounts.append(_mount(type="bind", src=f, dst=dst, readonly="true"))
                inner.append(dst)
        command = ["compose", "--project-name", p.compose_project, "--project-directory", p.dir]
        if env_file is not None:
            command += ["--env-file", inner.pop()]
        for f in inner:
            command += ["-f", f]
        return self.run(p.name, kind, command + args, timeout=TIMEOUTS[kind],
                        mounts=mounts, workdir=p.dir, merge_stderr=merge_stderr)

    # ---- SBX-4

    def verify_versions(self, expected: dict) -> dict:
        manifest, _ = load_manifest()
        found = {}
        for tool, spec in manifest["tools"].items():
            want = spec["version"]
            if expected and expected.get(tool) != want:
                raise SandboxError(f"recorded {tool} version {expected.get(tool)!r} does not match "
                                   f"the manifest ({want}); run `compose-broker sandbox build`")
            r = self.run("_", "version", VERSION_COMMANDS[tool], timeout=TIMEOUTS["sandbox"],
                         merge_stderr=True)
            if r.code != 0 or not version_matches(want, r.output):
                raise SandboxError(f"sandbox {tool} reports {r.output.strip()[:200]!r}, expected "
                                   f"{want}; run `compose-broker sandbox build`")
            found[tool] = want
        return found

    # ---- SBX-7, SBX-8

    def containers(self, project: str | None = None) -> list[tuple[str, str]]:
        """(name, op) of running or stopped sandbox containers, optionally for one
        project. Only containers from the recorded image count (SBX-7)."""
        label = f"{LABEL_PROJECT}={project}" if project else LABEL_PROJECT
        r = self.docker.run(["ps", "-aq", "--no-trunc", "--filter", f"label={label}"],
                            timeout=TIMEOUTS["sandbox"], merge_stderr=False)
        ids = r.output.split() if r.code == 0 else []
        if not ids:
            return []
        r = self.docker.run(["container", "inspect", "--format",
                             f'{{{{.Name}}}} {{{{.Image}}}} {{{{index .Config.Labels "{LABEL_OP}"}}}}',
                             *ids], timeout=TIMEOUTS["sandbox"], merge_stderr=False)
        out = []
        for line in r.output.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1] == self.image_id:
                out.append((parts[0].lstrip("/"), parts[2]))
        return out

    def busy(self, p: Project) -> bool:
        return any(op in MUTATING_OPS for _, op in self.containers(p.name))

    def cleanup(self) -> int:
        """SBX-8: remove sandbox containers left over from an earlier run."""
        found = self.containers()
        for name, _ in found:
            self.remove(name)
        return len(found)


# ----------------------------------------------------------------- compose entry points

def resolve_live(sb: Sandbox, p: Project) -> dict:
    """APR-1: resolve the project's own (agent-writable) compose files, in the sandbox."""
    r = sb.compose(p, p.compose_paths(), ["config", "--format", "json"], kind="config",
                   merge_stderr=False)
    if r.timed_out or r.code != 0:
        raise ResolveError(r.stderr.strip() or r.output.strip() or f"exit code {r.code}")
    try:
        cfg = json.loads(r.output)
    except ValueError as e:
        raise ResolveError(f"compose config did not return JSON: {e}")
    if not isinstance(cfg, dict):
        raise ResolveError("compose config did not return a mapping")
    return cfg


def ensure_empty_env() -> Path:
    """EXE-3: the empty env file passed to every run-time invocation."""
    path = paths.empty_env()
    if not path.exists():
        atomic_write(path, "")
    return path


# ----------------------------------------------------------------- image build (SBX-2, SBX-3)

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _fetch(url: str, sha256: str, dest: Path) -> None:
    """Download url to dest unless a cached copy with the right hash exists."""
    if dest.exists() and _sha256_file(dest) == sha256:
        return
    ensure_dir(dest.parent)
    req = urllib.request.Request(url, headers={"User-Agent": "compose-broker"})
    h = hashlib.sha256()
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.")
    try:
        with os.fdopen(fd, "wb") as out, \
                urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_S) as resp:
            while chunk := resp.read(1 << 20):
                h.update(chunk)
                out.write(chunk)
        if h.hexdigest() != sha256:
            raise SandboxError(f"checksum mismatch for {url}: got {h.hexdigest()}, "
                               f"manifest pins {sha256}")
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _extract(src: Path, member: str | None, dest: Path) -> None:
    if member is None:
        shutil.copyfile(src, dest)
    else:
        with tarfile.open(src, "r:gz") as tar:
            info = tar.getmember(member)
            if not info.isreg():
                raise SandboxError(f"{member} in {src.name} is not a regular file")
            with tar.extractfile(info) as r, open(dest, "wb") as w:
                shutil.copyfileobj(r, w)
    os.chmod(dest, 0o755)


def find_ca_bundle() -> Path:
    for c in CA_BUNDLE_CANDIDATES:
        if os.path.isfile(c):
            return Path(c)
    raise SandboxError("no CA certificate bundle found on the host (looked in "
                       + ", ".join(CA_BUNDLE_CANDIDATES) + ")")


def build_image(docker: Docker, say=print) -> dict:
    """`compose-broker sandbox build`: fetch and verify the pinned binaries,
    build the image from a broker-owned directory, verify it, record it."""
    endpoint = probe_endpoint(docker)
    manifest, definition_sha = load_manifest()
    arch = host_arch()
    cache = paths.cache_dir() / "sandbox"
    ensure_dir(paths.state_dir())
    ctx = Path(tempfile.mkdtemp(dir=paths.state_dir(), prefix="sandbox-build."))
    try:
        shutil.copyfile(IMAGE_DIR / "Dockerfile", ctx / "Dockerfile")
        ca = find_ca_bundle()
        shutil.copyfile(ca, ctx / CA_BUNDLE_FILE)  # follows the host's own symlinks: trusted
        os.chmod(ctx / CA_BUNDLE_FILE, 0o644)
        ca_sha = _sha256_file(ctx / CA_BUNDLE_FILE)
        for tool, spec in manifest["tools"].items():
            a = spec["arch"][arch]
            cached = cache / f"{tool}-{spec['version']}-{arch}-{a['sha256'][:16]}"
            say(f"fetching {tool} {spec['version']} ({arch})")
            _fetch(a["url"], a["sha256"], cached)
            _extract(cached, a.get("member"), ctx / spec["file"])
        iid = ctx / "iid"
        tag = f"{IMAGE_TAG}:{definition_sha[:12]}"
        say(f"building {tag}")
        r = docker.run(["build", "--network", "none", "--iidfile", str(iid), "--tag", tag,
                        str(ctx)], timeout=TIMEOUTS["build"], cwd=str(ctx))
        if r.code != 0 or not iid.exists():
            raise SandboxError(f"image build failed:\n{r.output.strip()[-4000:]}")
        image_id = iid.read_text().strip()
    finally:
        shutil.rmtree(ctx, ignore_errors=True)
    sb = Sandbox(docker, image_id, endpoint)
    versions = sb.verify_versions({})
    record = {"image_id": image_id, "tag": tag, "definition_sha256": definition_sha,
              "arch": arch, "versions": versions, "ca_bundle": str(ca),
              "ca_bundle_sha256": ca_sha, "built_at": now_iso()}
    atomic_write(paths.sandbox_config(), pretty(record))
    return record
