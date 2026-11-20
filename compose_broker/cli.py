"""Human-facing CLI (SPEC 5.8). Only this module prints compose errors in full."""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import secrets
import signal
import socketserver
import sys
import threading
from pathlib import Path

from . import VERSION, paths
from .broker import Broker
from .docker import TIMEOUTS, Docker
from .mcp import SERVER_NAME
from .policy import Finding, check_policy
from .registry import NAME_RE, Project, Registry, approved_path, load_approved
from .runtime import build_runtime, write_runtime
from .sandbox import Sandbox, SandboxError, build_image, ensure_empty_env, resolve_live
from .server import TCPServer, UnixServer, prepare_socket_path
from .util import (BrokerError, ResolveError, atomic_write, digest, dirs_overlap,
                   ensure_dir, log, now_iso, pretty)

COMPOSE_DEFAULT_FILES = ("compose.yaml", "compose.yml",
                         "docker-compose.yaml", "docker-compose.yml")
COMPOSE_PROJECT_LABEL = "com.docker.compose.project"


def _get_project(name: str) -> Project:
    p = Registry.load().get(name)
    if p is None:
        raise SystemExit(f"project {name!r} is not registered (see `compose-broker list`)")
    return p


def _print_findings(findings: list[Finding]) -> None:
    for f in findings:
        print(f"  {f}")


def _diff(old: str, new: str, old_label: str, new_label: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                        old_label, new_label))


def _sandbox() -> Sandbox:
    """SBX-4/SBX-9: the verified sandbox, or exit with the reason."""
    try:
        return Sandbox.open(Docker())
    except SandboxError as e:
        raise SystemExit(f"sandbox not usable: {e}")


def _roundtrip_diff(sb: Sandbox, p: Project, cfg: dict) -> str | None:
    """APR-4: does compose reproduce the stored config when re-loading it?"""
    path = write_runtime(p, cfg, "roundtrip")
    r = sb.compose(p, [path], ["config", "--format", "json"], kind="config",
                   env_file=ensure_empty_env(), merge_stderr=False)
    if r.code != 0:
        return f"stored config could not be re-loaded by compose:\n{r.stderr}"
    try:
        again = json.loads(r.output)
    except ValueError:
        return "stored config re-load did not return JSON"
    if digest(again) == digest(cfg):
        return None
    return _diff(pretty(cfg), pretty(again), "approved", "as-compose-will-run-it")


def cmd_register(a: argparse.Namespace) -> int:
    if not NAME_RE.match(a.name):
        raise SystemExit("NAME must match ^[a-z0-9][a-z0-9_-]{0,39}$")
    d = os.path.realpath(a.dir)
    if not os.path.isdir(d):
        raise SystemExit(f"{d} is not a directory")
    if d in ("/", os.path.realpath(Path.home())):
        raise SystemExit("refusing to register / or your home directory")
    projects = Registry.load()
    if a.name in projects:
        raise SystemExit(f"{a.name!r} is already registered (unregister it first)")
    for other in projects.values():
        if dirs_overlap(d, other.dir):
            raise SystemExit(f"{d} overlaps with project {other.name!r} ({other.dir})")
    files = a.compose_file
    if not files:
        present = [f for f in COMPOSE_DEFAULT_FILES if os.path.isfile(os.path.join(d, f))]
        if len(present) != 1:
            raise SystemExit("could not pick a compose file; pass -f explicitly "
                             f"(found: {', '.join(present) or 'none'})")
        files = present
    stored: list[str] = []
    for f in files:
        full = os.path.realpath(f if os.path.isabs(f) else os.path.join(d, f))
        if not os.path.isfile(full):
            raise SystemExit(f"compose file {full} does not exist")
        stored.append(os.path.relpath(full, d) if full.startswith(d + "/") else full)
    p = Project(name=a.name, dir=d, compose_files=stored,
                compose_project=f"cb-{a.name}", allow_reset=a.allow_reset)
    projects[a.name] = p
    Registry.save(projects)
    print(f"registered {a.name}: {d} ({', '.join(stored)}), compose project {p.compose_project}")
    if a.no_approve:
        print(f"run `compose-broker approve {a.name}` before the agent can start the stack")
        return 0
    return cmd_approve(argparse.Namespace(name=a.name, yes=False))


def cmd_unregister(a: argparse.Namespace) -> int:
    projects = Registry.load()
    p = projects.pop(a.name, None)
    if p is None:
        raise SystemExit(f"{a.name!r} is not registered")
    Registry.save(projects)
    for path in (approved_path(a.name), paths.tokens_dir() / a.name):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    print(f"unregistered {a.name}. Its stack (if running) was left alone; stop it, from "
          f"outside the project directory, with:\n  docker compose -p {p.compose_project} down")
    return 0


def cmd_list(_a: argparse.Namespace) -> int:
    projects = Registry.load()
    if not projects:
        print("no projects registered")
        return 0
    for p in projects.values():
        flags = [f for f, on in (("reset", p.allow_reset), ("tcp-token", bool(p.token_sha256))) if on]
        approved = f"approved {p.approved_at}" if p.approved_sha256 else "NOT APPROVED"
        print(f"{p.name:20} {p.dir}\n{'':20} {', '.join(p.compose_files)} | {approved}"
              + (f" | {', '.join(flags)}" if flags else ""))
    return 0


def cmd_check(a: argparse.Namespace) -> int:
    p = _get_project(a.name)
    sb = _sandbox()
    try:
        live = resolve_live(sb, p)
    except ResolveError as e:
        print(f"compose config failed:\n{e.detail}")
        return 1
    findings = check_policy(live, p)
    print("policy: " + ("clean" if not findings else f"{len(findings)} finding(s)"))
    _print_findings(findings)
    try:
        build_runtime(p, live, for_build=True)
    except BrokerError as e:
        print(f"  ERROR   {e}")
        findings.append(Finding("error", "build", str(e)))
    ok = not any(f.level == "error" for f in findings)
    if not p.approved_sha256:
        print("approval: never approved")
        return 1
    if digest(live) == p.approved_sha256:
        print(f"approval: live config matches the version approved {p.approved_at}")
        return 0 if ok else 1
    print("approval: live config DIFFERS from the approved version:")
    try:
        old = approved_path(p.name).read_text()
    except OSError:
        old = ""
    print(_diff(old, pretty(live), "approved", "live"))
    return 1


def cmd_approve(a: argparse.Namespace) -> int:
    p = _get_project(a.name)
    sb = _sandbox()
    try:
        live = resolve_live(sb, p)
    except ResolveError as e:
        print(f"compose config failed:\n{e.detail}", file=sys.stderr)
        return 1
    findings = check_policy(live, p)
    try:
        build_runtime(p, live, for_build=True)
    except BrokerError as e:
        findings.append(Finding("error", "build", str(e)))
    if findings:
        print("policy findings:")
        _print_findings(findings)
    if any(f.level == "error" for f in findings):
        print("\nnot approved: fix the errors above and re-run approve", file=sys.stderr)
        return 1
    new = digest(live)
    if p.approved_sha256 == new and approved_path(p.name).exists():
        print(f"{p.name}: already approved, nothing changed")
        return 0
    try:
        old = approved_path(p.name).read_text() if p.approved_sha256 else ""
    except OSError:
        old = ""
    print(_diff(old, pretty(live), "approved", "candidate") if old else
          f"first approval of {p.name}; full resolved config:\n\n{pretty(live)}")
    rt = _roundtrip_diff(sb, p, live)
    if rt:
        print("WARNING: compose does not reproduce the stored config exactly when re-loading it. "
              "What will run differs from the above as follows:\n" + rt)
    if not a.yes:
        try:
            answer = input(f"Approve this configuration for {p.name}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("not approved")
            return 1
    atomic_write(approved_path(p.name), pretty(live))
    Registry.update(p.name, approved_sha256=new, approved_at=now_iso())
    print(f"approved {p.name} ({new[:19]}…)")
    return 0


def _projects_with_containers(docker: Docker) -> set[str] | None:
    """Compose project names that have containers (running or stopped), or None on error."""
    r = docker.run(["ps", "-a", "--filter", f"label={COMPOSE_PROJECT_LABEL}",
                    "--format", f'{{{{.Label "{COMPOSE_PROJECT_LABEL}"}}}}'],
                   timeout=TIMEOUTS["ps"], merge_stderr=False)
    if r.code != 0 or r.timed_out:
        print(f"`docker ps` failed: {r.stderr.strip() or f'exit code {r.code}'}", file=sys.stderr)
        return None
    return {line.strip() for line in r.output.splitlines() if line.strip()}


def _down(sb: Sandbox, p: Project) -> bool:
    """CLI-1: `down --remove-orphans` on the approved config, in the sandbox, like stack_down."""
    try:
        approved = load_approved(p)
    except BrokerError as e:
        print(f"{p.name}: cannot run down without a valid approved config ({e})\n"
              f"  stop it by hand, from outside the project: docker compose -p {p.compose_project} down",
              file=sys.stderr)
        return False
    if sb.busy(p):  # SBX-8: the server (or another CLI) is running up/down/restart on it
        print(f"{p.name}: another operation on this stack is still running; retry when it finishes",
              file=sys.stderr)
        return False
    runtime = write_runtime(p, build_runtime(p, approved, for_build=False), "ops")
    r = sb.compose(p, [runtime], ["down", "--remove-orphans"], kind="down",
                   env_file=ensure_empty_env())
    if r.output.strip():
        print(r.output.rstrip())
    if r.timed_out or r.code != 0:
        status = "timed out" if r.timed_out else f"failed (exit code {r.code})"
        print(f"{p.name}: down {status}", file=sys.stderr)
        return False
    print(f"{p.name}: stack {p.compose_project} is down (volumes kept)")
    return True


def cmd_down(a: argparse.Namespace) -> int:
    if bool(a.names) == a.all:
        raise SystemExit("pass one or more project names, or --all")
    projects = Registry.load()
    if a.all:
        sb = _sandbox()
        active = _projects_with_containers(sb.docker)
        if active is None:
            return 1
        targets = [p for p in projects.values() if p.compose_project in active]
        if not targets:
            print("no registered project has containers; nothing to do")
            return 0
        print("taking down: " + ", ".join(p.name for p in targets))
    else:
        missing = [n for n in a.names if n not in projects]
        if missing:
            raise SystemExit(f"not registered: {', '.join(missing)} (see `compose-broker list`)")
        targets = [projects[n] for n in dict.fromkeys(a.names)]
        sb = _sandbox()
    failed = [p.name for p in targets if not _down(sb, p)]
    if failed:
        print(f"failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


def cmd_token(a: argparse.Namespace) -> int:
    _get_project(a.name)
    token = secrets.token_urlsafe(32)
    path = paths.tokens_dir() / a.name
    atomic_write(path, token + "\n")
    Registry.update(a.name, token_sha256=hashlib.sha256(token.encode()).hexdigest())
    print(f"token written to {path} (previous token, if any, is now invalid)")
    print("note: the container user must be able to read it; adjust ownership/mode if UIDs differ")
    return 0


def cmd_snippet(a: argparse.Namespace) -> int:
    p = _get_project(a.name)
    sock_dir = paths.default_socket_path().parent
    if a.tcp:
        token_path = paths.tokens_dir() / p.name
        mounts = [f"source={token_path},target=/run/compose-broker-token,type=bind,readonly"]
        host, port = a.tcp.rsplit(":", 1)
        cmd = {"command": "sh", "args": ["-c",
               "{ printf 'AUTH %s\\n' \"$(cat /run/compose-broker-token)\"; cat; } | "
               f"socat - TCP:host.docker.internal:{port}"]}
        extra = {"runArgs": ["--add-host=host.docker.internal:host-gateway"]}
    else:
        mounts = [f"source={sock_dir},target=/run/compose-broker,type=bind"]
        cmd = {"command": "socat", "args": ["STDIO", "UNIX-CONNECT:/run/compose-broker/broker.sock"]}
        extra = {}
    print("// .devcontainer/devcontainer.json (merge in; the image needs socat)")
    print(json.dumps({"mounts": mounts, **extra}, indent=2))
    print("\n// .mcp.json (Claude Code; other clients use the same command/args)")
    print(json.dumps({"mcpServers": {"compose": cmd}}, indent=2))
    return 0


def cmd_sandbox_build(_a: argparse.Namespace) -> int:
    try:
        rec = build_image(Docker())
    except SandboxError as e:
        print(f"sandbox build failed: {e}", file=sys.stderr)
        return 1
    versions = ", ".join(f"{k} {v}" for k, v in rec["versions"].items())
    print(f"sandbox image {rec['image_id'][:19]}… ({rec['tag']}) ready: {versions}")
    print("projects approved before this build must be re-approved if their config changed: "
          "run `compose-broker check NAME`")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    sb = _sandbox()
    removed = sb.cleanup()
    if removed:
        log(f"removed {removed} sandbox container(s) left over from an earlier run")
    broker = Broker(sb)
    ensure_dir(paths.state_dir())
    ensure_empty_env()
    servers: list[socketserver.BaseServer] = []
    sock_path: Path | None = None
    if not a.no_unix:
        sock_path = Path(a.unix) if a.unix else paths.default_socket_path()
        prepare_socket_path(sock_path)
        servers.append(UnixServer(sock_path, broker))
        os.chmod(sock_path, int(a.socket_mode, 8))
        log(f"listening on unix:{sock_path} (mode {a.socket_mode})")
    if a.tcp:
        host, port = a.tcp.rsplit(":", 1)
        host = host.strip("[]")
        if host in ("", "0.0.0.0", "::"):
            log("WARNING: TCP listener on a wildcard address is reachable from your network")
        servers.append(TCPServer((host, int(port)), broker))
        log(f"listening on tcp:{host}:{port}")
    if not servers:
        raise SystemExit("nothing to listen on")

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    for srv in servers:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    log(f"{SERVER_NAME} {VERSION} ready; {len(Registry.load())} project(s) registered")
    while not stop.wait(1):
        pass
    log("shutting down")
    for srv in servers:
        srv.shutdown()
        srv.server_close()
    if sock_path:
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="compose-broker", description="Host-side MCP server that lets sandboxed coding "
                                 "agents drive their own project's docker compose stack.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("register", help="register a project directory")
    s.add_argument("name")
    s.add_argument("dir")
    s.add_argument("-f", "--compose-file", action="append", default=[],
                   help="compose file (repeatable, order matters)")
    s.add_argument("--allow-reset", action="store_true", help="offer stack_reset (deletes volumes)")
    s.add_argument("--no-approve", action="store_true")
    s.set_defaults(fn=cmd_register)

    for name, fn, hlp in (("unregister", cmd_unregister, "remove a project"),
                          ("check", cmd_check, "policy + drift report"),
                          ("token", cmd_token, "create/rotate the TCP token")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("name")
        s.set_defaults(fn=fn)

    s = sub.add_parser("approve", help="review and approve the current compose config")
    s.add_argument("name")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("down", help="stop and remove a project's stack (volumes kept)")
    s.add_argument("names", nargs="*", metavar="NAME")
    s.add_argument("--all", action="store_true",
                   help="every registered project that has containers")
    s.set_defaults(fn=cmd_down)

    s = sub.add_parser("list", help="list projects")
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("snippet", help="print devcontainer/MCP config snippets")
    s.add_argument("name")
    s.add_argument("--tcp", metavar="HOST:PORT")
    s.set_defaults(fn=cmd_snippet)

    s = sub.add_parser("sandbox", help="manage the compose sandbox image")
    sandbox_sub = s.add_subparsers(dest="sandbox_cmd", required=True)
    s = sandbox_sub.add_parser("build", help="download, verify and build the sandbox image")
    s.set_defaults(fn=cmd_sandbox_build)

    s = sub.add_parser("serve", help="run the MCP server")
    s.add_argument("--unix", metavar="PATH", help=f"socket path (default {paths.default_socket_path()})")
    s.add_argument("--no-unix", action="store_true")
    s.add_argument("--tcp", metavar="HOST:PORT", help="also listen on TCP (token auth)")
    s.add_argument("--socket-mode", default="0666")
    s.set_defaults(fn=cmd_serve)

    a = ap.parse_args(argv)
    return a.fn(a)
