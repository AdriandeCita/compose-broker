"""Tool operations behind the MCP surface (SPEC 5.4, 5.7)."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import paths
from .docker import RunResult
from .policy import check_policy
from .registry import Project, Registry, load_approved
from .runtime import build_runtime, write_runtime
from .output import build_failed, condense
from .sandbox import Sandbox, ensure_empty_env, resolve_live
from .util import (BrokerError, InvalidParams, ResolveError, digest, ensure_dir, log, now_iso,
                   truncate_front)

LOG_TAIL_DEFAULT = 200
LOG_TAIL_MAX = 2000
WAIT_TIMEOUT_S = 180
UP_FAILED_BUILD = ("The build failed, so no containers were replaced. Any service shown as "
                   "running below is still the PREVIOUS version, without your changes. Fix the "
                   "error above and call stack_up again.")
UP_FAILED_START = ("The stack did not start correctly. Check the services below, then call "
                   "stack_logs to see why.")
UP_TIMED_OUT = ("stack_up timed out before it finished. If it was still building, no containers "
                "were replaced and any service shown as running below is still the PREVIOUS "
                "version. Call stack_up again; if it times out again, tell the human.")
STALE_ERROR_MAX = 2048  # bytes of the remembered build error repeated by later calls
SINCE_RE = re.compile(
    r"^(\d{1,6}[smh]|\d{4}-\d{2}-\d{2}"
    r"(T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})?)?)$")


class ToolResult:
    def __init__(self, text: str, ok: bool = True):
        self.text, self.ok = text, ok


class Broker:
    def __init__(self, sandbox: Sandbox):
        self.sandbox = sandbox
        self.docker = sandbox.docker  # host CLI, for identity (ID-2)
        self._locks: dict[str, threading.Lock] = {}
        self._running: dict[str, tuple[str, float]] = {}  # project -> (tool, start), under its lock
        self._locks_guard = threading.Lock()
        self._audit_lock = threading.Lock()
        # Last stack_up that left old containers running (build failure or timeout), per project:
        # (wall-clock time, condensed error). Cleared by the next successful stack_up. In memory
        # only: a broker restart forgets it.
        self._stale: dict[str, tuple[float, str]] = {}

    # ---------------------------------------------------------------- helpers
    def project(self, name: str) -> Project:
        p = Registry.load().get(name)
        if p is None:
            raise BrokerError("This project is no longer registered with the broker.")
        return p

    @contextmanager
    def exclusive(self, p: Project, tool: str):
        """EXE-9: one mutating operation per project, failing fast when busy."""
        with self._locks_guard:
            lock = self._locks.setdefault(p.name, threading.Lock())
        busy = BrokerError("Another operation on this stack is still running; wait and retry.")
        if not lock.acquire(blocking=False):
            running = self._running.get(p.name)
            if running is None:
                raise busy
            what, t0 = running
            raise BrokerError(
                f"{what} is already running on this stack (started {int(time.monotonic() - t0)}s "
                f"ago). Do not call {what} or any other stack-changing tool again until it "
                "returns; its result will tell you how it went. stack_status and stack_logs "
                "still work in the meantime.")
        try:
            self._running[p.name] = (tool, time.monotonic())
            if self.sandbox.busy(p):  # SBX-8: e.g. the CLI, or left over from a crashed broker
                raise busy
            yield
        finally:
            self._running.pop(p.name, None)
            lock.release()

    def audit(self, **record: Any) -> None:
        record = {"ts": now_iso(), **record}
        try:
            with self._audit_lock:
                path = paths.audit_log()
                ensure_dir(path.parent)
                fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log(f"audit write failed: {e}")

    def ops_file(self, p: Project, approved: dict) -> Path:
        return write_runtime(p, build_runtime(p, approved, for_build=False), "ops")

    def run_compose(self, p: Project, runtime_file: Path, args: list[str], kind: str,
                    merge_stderr: bool = True) -> RunResult:
        """EXE-3, SBX-1: compose on a broker-generated file, inside the sandbox."""
        return self.sandbox.compose(p, [runtime_file], args, kind=kind,
                                    env_file=ensure_empty_env(), merge_stderr=merge_stderr)

    @staticmethod
    def describe(r: RunResult, what: str) -> str:
        status = "timed out" if r.timed_out else f"exited with code {r.code}"
        return f"{what} {status}."

    # ---------------------------------------------------------------- tools (MCP-4)
    def op_up(self, name: str, wait: bool) -> ToolResult:
        p = self.project(name)
        with self.exclusive(p, "stack_up"):
            approved = load_approved(p)
            try:
                live = resolve_live(self.sandbox, p)
            except ResolveError as e:
                log(f"[{p.name}] stack_up: live config failed to resolve:\n{e.detail}")
                raise BrokerError(
                    "The project's compose files could not be resolved (syntax error, missing "
                    "file, or similar). Details are not shown here; check your recent edits to "
                    f"compose files or .env, or ask the human to run `compose-broker check {p.name}`.")
            if digest(live) != p.approved_sha256:
                log(f"[{p.name}] stack_up refused: live config differs from approved")
                raise BrokerError(
                    "The compose configuration (compose files, overrides or .env) differs from the "
                    "version the human approved, so the stack was not started. You cannot approve "
                    "it yourself. Either revert your change, or tell the human what you changed and "
                    f"why, and ask them to run `compose-broker approve {p.name}` on the host.")
            errors = [f for f in check_policy(approved, p) if f.level == "error"]
            if errors:
                log(f"[{p.name}] approved config fails current policy: " + "; ".join(map(str, errors)))
                raise BrokerError("The approved configuration no longer passes the broker's policy; "
                                  f"ask the human to run `compose-broker check {p.name}`.")
            runtime = write_runtime(p, build_runtime(p, approved, for_build=True), "up")
            args = ["up", "-d", "--build", "--remove-orphans"]
            if wait:
                args += ["--wait", "--wait-timeout", str(WAIT_TIMEOUT_S)]
            r = self.run_compose(p, runtime, args, "up")
            ok = r.code == 0 and not r.timed_out
            output = condense(r.output)
            if ok:
                headline, note = "SUCCESS: the stack was rebuilt and started.", ""
                self._stale.pop(p.name, None)
            elif r.timed_out:
                headline, note = "FAILED: stack_up timed out.", UP_TIMED_OUT
                self._stale[p.name] = (time.time(), "stack_up timed out.\n" + output)
            elif build_failed(r.output):
                headline = "FAILED: the image build failed. Your changes are NOT running."
                note = UP_FAILED_BUILD
                self._stale[p.name] = (time.time(), output)
            else:
                headline, note = "FAILED: the stack did not start correctly.", UP_FAILED_START
                self._stale.pop(p.name, None)  # containers were replaced; status/logs show why
            status = self._status_text(p, approved)
            text = (f"{headline}\n{self.describe(r, 'docker compose up')}\n\n"
                    f"--- output (condensed) ---\n{output}\n")
            if note:
                text += f"\n{note}\n\n"
            text += f"--- services ---\n{status}\n"
            if ok:
                text += "\nNext: exercise the app, then call stack_logs to check for errors."
            return ToolResult(text, ok=ok)

    def op_restart(self, name: str, service: str | None) -> ToolResult:
        p = self.project(name)
        with self.exclusive(p, "stack_restart"):
            approved = load_approved(p)
            self._check_service_arg(approved, service)
            r = self.run_compose(p, self.ops_file(p, approved),
                                 ["restart"] + ([service] if service else []), "restart")
            text = (f"{self.stale_banner(p)}{self.describe(r, 'docker compose restart')}\n"
                    f"{condense(r.output)}\n--- services ---\n"
                    f"{self._status_text(p, approved)}")
            return ToolResult(text, ok=(r.code == 0 and not r.timed_out))

    def op_down(self, name: str, volumes: bool) -> ToolResult:
        p = self.project(name)
        if volumes and not p.allow_reset:
            raise BrokerError("Resetting volumes is not enabled for this project.")
        with self.exclusive(p, "stack_reset" if volumes else "stack_down"):
            approved = load_approved(p)
            args = ["down", "--remove-orphans"] + (["--volumes"] if volumes else [])
            r = self.run_compose(p, self.ops_file(p, approved), args, "down")
            if r.code == 0 and not r.timed_out:
                self._stale.pop(p.name, None)  # the stale containers are gone
            return ToolResult(f"{self.describe(r, 'docker compose down')}\n"
                              f"{condense(r.output)}", ok=(r.code == 0 and not r.timed_out))

    def op_status(self, name: str) -> ToolResult:
        p = self.project(name)
        approved = load_approved(p)
        return ToolResult(self.stale_banner(p) + self._status_text(p, approved))

    def op_logs(self, name: str, service: str | None, tail: int, since: str | None) -> ToolResult:
        p = self.project(name)
        approved = load_approved(p)
        self._check_service_arg(approved, service)
        if not (1 <= tail <= LOG_TAIL_MAX):
            raise InvalidParams(f"tail must be between 1 and {LOG_TAIL_MAX}")
        if since is not None and not SINCE_RE.match(since):
            raise InvalidParams("since must look like '10m', '2h', or an ISO-8601 timestamp")
        args = ["logs", "--no-color", "--timestamps", "--tail", str(tail)]
        if since:
            args += ["--since", since]
        if service:
            args.append(service)
        r = self.run_compose(p, self.ops_file(p, approved), args, "logs")
        body = r.output.strip() or "(no log output)"
        banner = self.stale_banner(p, with_error=False)
        return ToolResult(banner + truncate_front(body), ok=(r.code == 0 and not r.timed_out))

    def stale_banner(self, p: Project, with_error: bool = True) -> str:
        """Warn that running containers predate the agent's latest stack_up (EXE-10b)."""
        stale = self._stale.get(p.name)
        if stale is None:
            return ""
        at, error = stale
        mins = max(0, int(time.time() - at) // 60)
        text = (f"WARNING: the last stack_up ({mins} min ago) FAILED, so the running containers "
                "are the PREVIOUS version and do NOT include your recent changes. Fix the build "
                "error and call stack_up again")
        if not with_error:
            return text + "; stack_status shows the error.\n\n"
        return (f"{text}.\n--- last stack_up error (condensed) ---\n"
                f"{truncate_front(error, STALE_ERROR_MAX)}\n--- current state ---\n")

    def _check_service_arg(self, approved: dict, service: str | None) -> None:
        if service is None:
            return
        names = sorted((approved.get("services") or {}).keys())
        if service not in names:
            raise InvalidParams(f"unknown service {service!r}; services are: {', '.join(names)}")

    def _status_text(self, p: Project, approved: dict) -> str:
        r = self.run_compose(p, self.ops_file(p, approved), ["ps", "-a", "--format", "json"],
                             "ps", merge_stderr=False)
        if r.code != 0:
            return f"(could not get status: {self.describe(r, 'docker compose ps')})"
        text = r.output.strip()
        try:
            rows = json.loads(text) if text.startswith("[") else \
                [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            return "(could not parse status output)"
        if not rows:
            return "No containers exist for this stack yet (use stack_up)."
        lines = []
        for row in sorted(rows, key=lambda x: str(x.get("Service", ""))):
            ports = ", ".join(
                f"{pub.get('URL') or '0.0.0.0'}:{pub['PublishedPort']}->{pub.get('TargetPort')}/{pub.get('Protocol', 'tcp')}"
                for pub in (row.get("Publishers") or []) if pub.get("PublishedPort"))
            state = row.get("State", "?")
            exit_code = f" exit={row.get('ExitCode')}" if state == "exited" else ""
            lines.append(f"{row.get('Service', '?')}: {state}{exit_code} — {row.get('Status', '')}"
                         + (f" — ports {ports}" if ports else ""))
        return "\n".join(lines)

