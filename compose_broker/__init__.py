"""
compose-broker: a host-side MCP server that lets sandboxed coding agents drive
*their own* project's docker compose stack, and nothing else.

See SPEC.md for requirements (IDs like EXE-5 in this package refer to it).
Stdlib only. Python >= 3.11.

Module map (dependencies point downwards only):

    cli        human-facing commands (register, approve, serve, ...)
    server     unix/TCP listeners, per-connection framing
    mcp        JSON-RPC/MCP session and tool definitions
    identity   caller -> project mapping (SO_PEERCRED / token)
    broker     tool operations, locking, audit
    output     condensing compose output for the agent
    policy     allowlist check of a resolved compose config
    sandbox    compose in a locked-down container; image build and checks
    runtime    safe Dockerfile reads, generated runtime compose files
    docker     host docker CLI subprocess wrapper
    registry   registered projects, approved configs
    util       errors, hashing, atomic writes, logging
    paths      on-disk locations (XDG)
"""

VERSION = "0.1.0"
