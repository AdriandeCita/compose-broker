"""Shared fixtures: isolated XDG dirs and a fake `docker` executable."""
import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from compose_broker.registry import Project

FAKE_IMAGE_ID = "sha256:" + "f" * 64

FAKE_DOCKER = textwrap.dedent("""\
    #!/usr/bin/env python3
    import csv, json, os, sys, time
    IMAGE_ID = "@IMAGE_ID@"
    args = sys.argv[1:]
    home = os.environ.get("HOME")
    with open(home + "/docker-calls.jsonl", "a") as f:
        f.write(json.dumps(args) + "\\n")
    # Optional per-test behaviour: $HOME/fake-docker.json
    try:
        fake = json.load(open(home + "/fake-docker.json"))
    except FileNotFoundError:
        fake = {}
    versions = fake.get("versions", {"docker": "29.9.2", "compose": "5.6.1", "buildx": "0.38.1"})

    if args[:1] == ["context"]:
        print(json.dumps({"Host": fake.get("host", "unix:///var/run/docker.sock")})); sys.exit(0)
    if args[:1] == ["info"]:
        print(json.dumps(fake.get("security_options", ["name=seccomp,profile=builtin"]))); sys.exit(0)
    if args[:2] == ["image", "inspect"]:
        if args[-1] in fake.get("images", [IMAGE_ID]):
            print(args[-1]); sys.exit(0)
        print("No such image", file=sys.stderr); sys.exit(1)
    if args[:1] == ["ps"] and "--format" in args:
        # Host-side `ps` for compose project labels: one line per container.
        for name in fake.get("compose_projects", []):
            print(name)
        sys.exit(0)
    if args[:1] == ["ps"]:
        for c in fake.get("containers", []):
            print(c["id"])
        sys.exit(0)
    if args[:2] == ["container", "inspect"] and "{{json .Mounts}}" not in args:
        for c in fake.get("containers", []):
            if c["id"] in args:
                print(f"/{c['name']} {c['image']} {c['op']}")
        sys.exit(0)
    if args[:1] == ["rm"]:
        sys.exit(0)
    if args[:1] == ["build"]:
        iid = args[args.index("--iidfile") + 1]
        open(iid, "w").write(IMAGE_ID); print("built"); sys.exit(0)

    if args[:1] == ["run"]:
        # The sandbox: skip run options up to the image, map /cb mounts back to
        # host paths, and continue as the docker CLI inside the container.
        mounts, workdir, i = {}, None, 1
        while args[i] != IMAGE_ID:
            if args[i] in ("--rm", "--read-only", "--init"):
                i += 1; continue
            if args[i] == "--mount":
                fields = dict(x.split("=", 1) for x in next(csv.reader([args[i + 1]])))
                if fields.get("type") == "bind":
                    mounts[fields["dst"]] = fields["src"]
            if args[i] == "--workdir":
                workdir = args[i + 1]
            i += 2
        args = [mounts.get(a, a) for a in args[i + 1:]]
        if workdir:
            os.chdir(workdir)
        if args == ["--version"]:
            print(f"Docker version {versions['docker']}, build fake"); sys.exit(0)
        if args == ["compose", "version", "--short"]:
            print(versions["compose"]); sys.exit(0)
        if args == ["buildx", "version"]:
            print(f"github.com/docker/buildx v{versions['buildx']} fake"); sys.exit(0)

    if args[:1] == ["compose"]:
        files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        for sub, secs in fake.get("sleep", {}).items():  # e.g. {"up": 2}: a slow compose up
            if sub in args:
                time.sleep(secs)
        if "config" in args:
            # Live config: the fixture the test dropped into the project dir.
            src = files[0]
            if src.endswith(".json") and "runtime" in src:
                print(open(src).read())
            else:
                print(open(os.path.join(os.path.dirname(src), "resolved.json")).read())
            sys.exit(0)
        if "ps" in args:
            print(json.dumps({"Service": "web", "State": "running", "Status": "Up 2 seconds",
                              "Publishers": [{"URL": "127.0.0.1", "PublishedPort": 8080,
                                              "TargetPort": 80, "Protocol": "tcp"}]}))
            sys.exit(0)
        if "up" in args and os.path.exists("up-result.json"):
            # Scripted `up` result: the test writes {"code": N, "output": "..."} to the project dir.
            res = json.load(open("up-result.json"))
            print(res["output"]); sys.exit(res["code"])
        if "logs" in args:
            print("web-1  | 2026-11-05T10:00:00Z listening on :80")
            sys.exit(0)
        print("ok"); sys.exit(0)
    sys.exit(1)
""").replace("@IMAGE_ID@", FAKE_IMAGE_ID)

ISOLATED_ENV = ("HOME", "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR")


def isolate_env(root: Path, *, keep_docker_env: bool = False) -> dict:
    """Point the broker's config/state dirs under root. Returns what to restore.

    By default HOME, XDG_CACHE_HOME and XDG_RUNTIME_DIR move too. Pass
    keep_docker_env=True when a real docker CLI must still find its contexts
    (~/.docker) and rootless socket ($XDG_RUNTIME_DIR/docker.sock); the real
    cache then keeps the sandbox downloads between runs.
    """
    saved = {k: os.environ.get(k) for k in ISOLATED_ENV}
    os.environ.update(XDG_CONFIG_HOME=str(root / "cfg"), XDG_STATE_HOME=str(root / "state"))
    if not keep_docker_env:
        (root / "home").mkdir(exist_ok=True)
        os.environ.update(HOME=str(root / "home"), XDG_RUNTIME_DIR=str(root / "run"),
                          XDG_CACHE_HOME=str(root / "cache"))
    return saved


def restore_env(saved: dict) -> None:
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class IsolatedDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.addCleanup(restore_env, isolate_env(self.root))
        self.home = self.root / "home"


def fake_sandbox(docker_bin) -> "Sandbox":
    """A Sandbox on the fake docker, skipping Sandbox.open's checks. Rootless, so
    no real daemon socket is needed for the --user arguments."""
    from compose_broker.docker import Docker
    from compose_broker.sandbox import Endpoint, Sandbox
    return Sandbox(Docker(str(docker_bin)), FAKE_IMAGE_ID,
                   Endpoint(socket="/var/run/docker.sock", rootless=True))


def docker_calls(home) -> list[list[str]]:
    return [json.loads(line) for line in (Path(home) / "docker-calls.jsonl").read_text().splitlines()]


class Env(IsolatedDirs):
    """Isolated dirs, a fake docker, and a registered-looking project "shop"."""

    def setUp(self):
        super().setUp()
        self.docker_bin = self.root / "docker"
        self.docker_bin.write_text(FAKE_DOCKER)
        self.docker_bin.chmod(0o755)
        self.proj_dir = self.root / "projects" / "shop"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "compose.yaml").write_text("# fixture\n")
        (self.proj_dir / "Dockerfile").write_text("FROM alpine\n")
        self.p = Project(name="shop", dir=os.path.realpath(self.proj_dir),
                         compose_files=["compose.yaml"], compose_project="cb-shop")

    def good_config(self):
        d = self.p.dir
        return {
            "name": "cb-shop",
            "services": {
                "web": {
                    "build": {"context": d, "dockerfile": "Dockerfile"},
                    "volumes": [{"type": "bind", "source": d, "target": "/workspace"},
                                {"type": "volume", "source": "data", "target": "/data"}],
                    "ports": [{"target": 80, "published": "8080", "host_ip": "127.0.0.1",
                               "protocol": "tcp", "mode": "ingress"}],
                    "networks": {"default": None},
                    "security_opt": ["no-new-privileges:true"],
                    "user": "1000:1000",
                },
                "db": {"image": "postgres:16", "environment": {"POSTGRES_PASSWORD": "x"},
                       "env_file": [{"path": f"{d}/.env.db", "required": True}]},
            },
            "volumes": {"data": {"name": "cb-shop_data"}},
            "networks": {"default": {"name": "cb-shop_default"}},
        }
