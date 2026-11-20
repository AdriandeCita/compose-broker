"""Condense docker compose output for the agent (EXE-10, MCP-5).

Small models do badly with kilobytes of BuildKit progress. Tool results keep
only what the agent can act on:

- compose's own progress lines ("Container x-1 Starting", "... Started") are
  collapsed to one final state per object;
- BuildKit step lines ("#7 ...") are dropped, except every line of a step that
  failed ("#7 ERROR ..."), which carries the build error and the failing
  command's output;
- any other line (warnings, "failed to solve: ...", daemon errors) is kept.

The result is capped to the last MAX_LINES lines and MAX_BYTES bytes.
"""
from __future__ import annotations

import re

from .util import truncate_front

MAX_LINES = 120
MAX_BYTES = 8 * 1024

_STEP_RE = re.compile(r"^#(\d+) ")
_FAILED_STEP_RE = re.compile(r"^#(\d+) ERROR\b")
_PROGRESS_RE = re.compile(r"^\s*(Image|Container|Network|Volume)\s+(\S+)\s+(\S.*?)\s*$")
_SUMMARY_RE = re.compile(r"^\s*\[\+\] ")  # "[+] Running 2/2" headers
_SOLVE_FAILED_RE = re.compile(r"^\s*(failed to solve|target \S+: failed to solve)\b")


def build_failed(output: str) -> bool:
    """True if compose stopped at the image build, before touching containers."""
    return any(_FAILED_STEP_RE.match(line) or _SOLVE_FAILED_RE.match(line)
               for line in output.splitlines())


def condense(output: str) -> str:
    lines = output.splitlines()
    failed_steps = {m.group(1) for line in lines if (m := _FAILED_STEP_RE.match(line))}
    states: dict[tuple[str, str], str] = {}
    kept: list[tuple[str, str] | str] = []  # an object key, or a verbatim line
    for raw in lines:
        line = raw.rstrip()
        if not line.strip() or _SUMMARY_RE.match(line):
            continue
        step = _STEP_RE.match(line)
        if step:
            if step.group(1) in failed_steps:
                kept.append(line)
            continue
        progress = _PROGRESS_RE.match(line)
        if progress:
            key = (progress.group(1), progress.group(2))
            if key not in states:
                kept.append(key)
            states[key] = progress.group(3)
            continue
        kept.append(line)
    text = "\n".join(f"{k[0]} {k[1]}: {states[k]}" if isinstance(k, tuple) else k
                     for k in kept[-MAX_LINES:])
    if len(kept) > MAX_LINES:
        text = f"[... {len(kept) - MAX_LINES} earlier lines omitted ...]\n{text}"
    return truncate_front(text, MAX_BYTES) if text else "(no output)"
