"""Host docker CLI subprocesses (EXE-7, EXE-8). Compose itself runs in the sandbox."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

TIMEOUTS = {"config": 60, "inspect": 15, "up": 900, "down": 300,
            "restart": 300, "ps": 30, "logs": 60, "sandbox": 30, "build": 600}
KILL_GRACE_S = 10


@dataclass
class RunResult:
    code: int
    output: str
    stderr: str
    timed_out: bool


class Docker:
    PASSTHROUGH_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                       "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY", "XDG_RUNTIME_DIR")

    def __init__(self, binary: str | None = None):
        found = binary or shutil.which("docker")
        if not found:
            raise SystemExit("docker CLI not found in PATH")
        self.bin = os.path.realpath(found)

    def env(self) -> dict[str, str]:
        env = {
            "PATH": os.pathsep.join([os.path.dirname(self.bin),
                                     "/usr/local/bin", "/usr/bin", "/bin"]),
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "BUILDKIT_PROGRESS": "plain",
            "COMPOSE_ANSI": "never",
            "DOCKER_CLI_HINTS": "false",
        }
        for k in self.PASSTHROUGH_ENV:
            if k in os.environ:
                env[k] = os.environ[k]
        return env

    def run(self, args: list[str], *, timeout: int, cwd: str | None = None,
            merge_stderr: bool = True, on_timeout: Callable[[], None] | None = None) -> RunResult:
        """Run the host docker CLI. On timeout: SIGTERM to the process group, then
        after KILL_GRACE_S `on_timeout` (e.g. removing a sandbox container that
        SIGKILL on the client would orphan, SBX-8), then SIGKILL."""
        proc = subprocess.Popen(
            [self.bin, *args], cwd=cwd, env=self.env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT if merge_stderr else subprocess.PIPE,
            start_new_session=True)
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill(proc, signal.SIGTERM)
            try:
                out, err = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                if on_timeout is not None:
                    on_timeout()
                self._kill(proc, signal.SIGKILL)
                out, err = proc.communicate()
        dec = lambda b: (b or b"").decode("utf-8", "replace")
        return RunResult(proc.returncode, dec(out), dec(err), timed_out)

    @staticmethod
    def _kill(proc: subprocess.Popen, sig: int) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass
