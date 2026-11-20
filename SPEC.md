# compose-broker — Specification

Version 0.1 (draft)

## 1. Purpose

Coding agents run inside per-project devcontainers. To close the feedback loop on multi-service applications (UI + backends + databases) they need to rebuild and restart the project's docker compose stack and read its logs, without the human relaying commands.

compose-broker is a single host-side process that exposes a small MCP tool surface (`stack_up`, `stack_down`, `stack_restart`, `stack_status`, `stack_logs`, optionally `stack_reset`). Each devcontainer can operate only its own project's stack, and only in configurations the human has explicitly approved.

The broker is the security boundary, so it must be small, dependency-free (Python stdlib only), and auditable.

## 2. Threat model

**Trusted:** the human operator, the host OS and kernel, the Docker daemon and CLI, the broker's code, the broker's vendored sandbox image definition and the binaries it pins by checksum (5.9), and the broker's host-side state (`~/.config/compose-broker`, `~/.local/state/compose-broker`).

**Untrusted (assume adversarial):**
- Everything the agent can write: the entire project directory, including compose files, `.env`, override and `include` targets, Dockerfiles, `.dockerignore`, and any file or symlink.
- Every byte arriving over the MCP connection.
- Processes in the devcontainer and in the application containers.
- Application log output (it may contain text injected by third parties).

The agent may be a weak or misbehaving model that actively tries to escalate. Safety must not depend on the agent's cooperation or on tool descriptions.

**Security goals:**

| ID | Goal |
|----|------|
| G1 | The agent cannot obtain code execution on the host, or read or write host files outside its project directory, through the broker. |
| G2 | The agent cannot affect other projects' containers, volumes, networks or images, or any other container on the host. |
| G3 | The agent cannot change a stack's topology or privileges without explicit human approval on the host. |
| G4 | The broker does not disclose host information to the agent beyond its own stack's status and logs. In particular, it never relays compose error output or resolved configuration. |

**Accepted risks (out of scope by owner decision):**
- Changes to `.devcontainer/*` (mounts, `runArgs`, `initializeCommand`) take effect only on a manual rebuild, which the human gates.
- Application containers run agent-written code, with whatever network access their networks give them: the internet, the LAN, and services on the host. A firewall in the devcontainer does not restrict them. Only a host firewall covering all Docker bridges does.
- A container running as root with the project root mounted writable can leave root-owned files in the project, including setuid-root binaries. Executing such a file on the host would give root. The agent cannot execute anything on the host through the broker, so this needs a human to run the file. The policy warns about it (POL-S13) but does not block it, because many images run as root and the same exposure exists whenever the agent has root in its devcontainer.
- Resource exhaustion (CPU, disk, image builds). The broker limits wall-clock time only.
- Vulnerabilities in the kernel, Docker daemon, BuildKit, or compose itself.

## 3. Architecture

```
 devcontainer (project "shop")                host
 ┌──────────────────────────┐                 ┌──────────────────────────────────────┐
 │ agent ── MCP client      │                 │ compose-broker serve                 │
 │            │ stdio       │  unix socket    │   identify caller → project          │
 │          socat ──────────┼─────────────────┼─▶ check live config == approved      │
 │                          │ (or TCP+token)  │   policy check                       │
 └──────────────────────────┘                 │   docker compose -f <approved> ...   │
                                              └──────────────────────────────────────┘
```

The broker speaks MCP's stdio framing (newline-delimited JSON-RPC 2.0) directly over each stream connection. Inside the container, `socat STDIO UNIX-CONNECT:...` bridges the MCP client's stdio server definition to the socket.

The broker never runs `docker compose` on the host directly. Every compose call runs in a short-lived sandbox container that sees only the project directory (read-only), the broker's files for that call, and the daemon socket (5.9).

## 4. Files and state

| Path | Contents | Mode |
|------|----------|------|
| `~/.config/compose-broker/projects.json` | Registry of projects | 0600 |
| `~/.config/compose-broker/approved/<name>.json` | Approved, fully resolved compose config | 0600 |
| `~/.config/compose-broker/tokens/<name>` | TCP-mode token (plaintext, for mounting) | 0600 |
| `~/.local/state/compose-broker/runtime/` | Generated files actually passed to compose | 0600 |
| `~/.config/compose-broker/sandbox.json` | Sandbox image ID, manifest digest and tool versions (SBX-3) | 0600 |
| `~/.cache/compose-broker/sandbox/` | Downloaded sandbox binaries, verified against the manifest on every use (SBX-2) | 0700 dir |
| `~/.local/state/compose-broker/audit.log` | JSON-lines audit log | 0600 |
| `~/.local/state/compose-broker/empty.env` | Empty env file passed to compose at run time | 0600 |
| `$XDG_RUNTIME_DIR/compose-broker/broker.sock` | Default unix socket | 0666 (configurable) |

`XDG_CONFIG_HOME`, `XDG_STATE_HOME` and `XDG_CACHE_HOME` are honoured. Directories are created with mode 0700, except the socket directory (0755), which must be traversable by container users. All writes are atomic (write to a temp file, then rename).

## 5. Requirements

### 5.1 Registration

- **REG-1** Projects are registered only by the human, via `compose-broker register NAME DIR [-f FILE]...`. The broker never scans the filesystem and never registers a project in response to anything a container does.
- **REG-2** `NAME` matches `^[a-z0-9][a-z0-9_-]{0,39}$`. The compose project name is fixed to `cb-NAME` and is never taken from compose files or the environment.
- **REG-3** `DIR` is stored as its realpath and must be an existing directory. Registration of `/` or `$HOME` is refused.
- **REG-4** Project directories must not overlap. A new directory must not equal, contain, or be contained in an existing project's directory, which keeps caller identity unambiguous.
- **REG-5** Compose files are listed explicitly and in order (`-f`). If none are given, exactly one of `compose.yaml`, `compose.yml`, `docker-compose.yaml`, `docker-compose.yml` must exist in the root of `DIR`; no deeper search is performed.
- **REG-6** Registration ends with the approval flow (APR-*) unless `--no-approve` is passed.
- **REG-7** `stack_reset` (volume deletion) is offered only for projects registered with `--allow-reset`.

### 5.2 Approval

- **APR-1** The resolved config is the JSON output of `docker compose -p cb-NAME --project-directory DIR -f F1 ... config --format json`. It resolves interpolation, `.env`, `include`, `extends`, overrides, and profiles into one document.
- **APR-2** The digest is `sha256` over the canonical JSON encoding (sorted keys, no whitespace, UTF-8).
- **APR-3** `compose-broker approve NAME` resolves the live config, runs the policy check (POL-*), and refuses if any policy error exists. It then verifies that every referenced Dockerfile can be read safely (EXE-5), shows the human a unified diff against the previously approved config (or the full config on first approval), and asks for confirmation (`--yes` skips the prompt). On confirmation, it stores the config and records its digest and timestamp in the registry.
- **APR-4** On approval, the broker performs a round-trip check: it re-resolves the stored file with compose and compares digests. A mismatch is shown as a warning with a diff. A mismatch means the file compose will actually run differs from what the human reviewed.
- **APR-5** On load, the approved file's digest is verified against the registry. A mismatch is treated as "not approved".
- **APR-6** `compose-broker check NAME` reports the policy findings for the live config and whether it matches the approved one (with a diff), without changing anything. Full compose error output is shown here, to the human only.

### 5.3 Policy

The policy is an **allowlist**: any key not explicitly permitted is an error, so new compose features are denied until reviewed. Errors block approval and `stack_up`; warnings are shown to the human at approval time.

**Top level.** Allowed keys are `name`, `services`, `networks`, `volumes`, `secrets`, `configs`, and `x-*`. `name` must equal `cb-NAME`. Anything else (for example `models`) is an error.

**Services.**
- **POL-S1** Allowed keys: `annotations`, `attach`, `build`, `cap_drop`, `command`, `configs`, `cpu_count`, `cpu_percent`, `cpu_period`, `cpu_quota`, `cpu_shares`, `cpus`, `cpuset`, `depends_on`, `deploy`, `develop`, `dns`, `dns_opt`, `dns_search`, `domainname`, `entrypoint`, `env_file`, `environment`, `expose`, `extra_hosts`, `group_add`, `healthcheck`, `hostname`, `image`, `init`, `ipc`, `labels`, `links`, `logging`, `mac_address`, `mem_limit`, `mem_reservation`, `mem_swappiness`, `memswap_limit`, `network_mode`, `networks`, `oom_score_adj`, `pid`, `pids_limit`, `platform`, `ports`, `post_start`, `pre_stop`, `profiles`, `pull_policy`, `read_only`, `restart`, `scale`, `secrets`, `security_opt`, `shm_size`, `stdin_open`, `stop_grace_period`, `stop_signal`, `sysctls`, `tmpfs`, `tty`, `ulimits`, `user`, `volumes`, `working_dir`, `x-*`.
- **POL-S2** Explicitly forbidden (with a specific message): `privileged`, `cap_add`, `devices`, `device_cgroup_rules`, `volumes_from`, `userns_mode`, `uts`, `cgroup`, `cgroup_parent`, `runtime`, `isolation`, `storage_opt`, `credential_spec`, `gpus`, `use_api_socket`, `provider`, `container_name`, `external_links`, `oom_kill_disable`, `label_file`.
- **POL-S3** `ipc` may only be `private`, `shareable`, `none`, or `service:<name>`. `pid` may only be `service:<name>`. `network_mode` may only be `none`, `bridge`, `default`, or `service:<name>`. `host` and `container:<id>` are always rejected.
- **POL-S4** `security_opt` may only contain `no-new-privileges` (optionally `:true` / `=true`).
- **POL-S5** Labels (service and build) must not use the `com.docker.compose.` prefix, so compose bookkeeping cannot be spoofed, or the `com.compose-broker.` prefix, so the agent cannot make its containers look like sandbox containers (SBX-7).
- **POL-S6** `logging.driver` may only be unset, `json-file`, or `local`.
- **POL-S7** `deploy` may only contain `resources`, `restart_policy`, `replicas`, `labels`, and `mode` (value `replicated`). Device and generic-resource reservations are forbidden.
- **POL-S8** `post_start` / `pre_stop` hooks may only contain `command`, `user`, `working_dir`, and `environment`. In particular, `privileged` is not allowed.
- **POL-S9** Mounts (`volumes`) must be in normalized long form:
  - `bind`: the source must be **exactly** the project directory. This is the only bind source permitted. Projects mount the root once (e.g. `.:/workspace`) and address sub-paths inside the container. Rationale: any deeper bind source can be swapped for a symlink by the agent between check and use, and the daemon would follow it on the host. The project root itself cannot be replaced from inside the devcontainer. Propagation may only be unset, `private`, or `rprivate`.
  - `volume`: must be anonymous or name a volume declared at top level.
  - `tmpfs`: allowed.
  - Anything else (`npipe`, `image`, `cluster`, ...): error.
- **POL-S10** `ports` published without `host_ip`, or on `0.0.0.0` / `::`, produce a **warning** (the port is reachable from the LAN).
- **POL-S11** `build`:
  - Allowed keys: `context`, `dockerfile`, `dockerfile_inline`, `args`, `target`, `labels`, `shm_size`, `extra_hosts`, `platforms`, `pull`, `no_cache`, `network`, `ulimits`.
  - Forbidden: `additional_contexts`, `secrets`, `ssh`, `cache_from`, `cache_to`, `entitlements`, `privileged`, `isolation`, `tags`.
  - `context` must be a local directory: the project directory itself or a directory inside it. URLs (git, http) and paths outside the project are errors. A context swapped for a symlink later resolves inside the sandbox (5.9), not on the host.
  - `dockerfile`, when relative, is relative to the context, as in compose. It may contain `..` as long as the normalised path stays inside the project, so a Dockerfile may live outside its context (e.g. `context: ./web`, `dockerfile: ../docker/web.Dockerfile`). Absolute paths must lie inside the project.
  - `network` may only be unset, `default`, or `none`.
- **POL-S12** A service with `build` must either omit `image` or use an image name beginning with `cb-NAME-`. This prevents a built image from shadowing an image other projects rely on (e.g. tagging a build as `postgres:16`).
- **POL-S13** A service that bind-mounts the project root writable (not `read_only`), and whose `user` is unset (image default, usually root) or root (`0`, `root`, with any group), produces a **warning**. So does a `post_start` / `pre_stop` hook of such a service whose effective user is root. Rationale: on a rootful daemon, files it creates in the project are owned by root on the host, and it could plant a setuid-root binary there for the human to run later (see accepted risks). User names other than `root` are treated as non-root, because the broker cannot resolve them. On rootless Docker, container root is the host user, so the warning does not apply.

**Top-level resources.**
- **POL-T1** `volumes`: allowed keys are `name`, `driver`, `labels`, and `external` (only `false`). `driver` must be unset or `local`. `driver_opts` is forbidden (the local driver can bind arbitrary host paths through it). `name`, if present, must start with `cb-NAME_`.
- **POL-T2** `networks`: allowed keys are `name`, `driver`, `labels`, `internal`, `ipam`, `enable_ipv6`, `attachable`, and `external` (only `false`). `driver` must be unset or `bridge`. `name`, if present, must start with `cb-NAME_`.
- **POL-T3** `secrets` may only use `environment` sources. `configs` may only use `content` or `environment` sources. `file` sources (bind-mounted from the host) and `external` are forbidden.

### 5.4 Execution

- **EXE-1** `stack_up` re-resolves the live config, compares its digest to the approved digest, and refuses on any mismatch or resolution error. The refusal message tells the agent to ask the human to run `compose-broker approve NAME` and contains no compose output (G4).
- **EXE-2** Compose is always run on a broker-generated runtime file derived from the **approved** config, never on the project's own files. This closes the window between checking and running.
- **EXE-3** Runtime invocations (run inside the sandbox, SBX-1) are `docker compose --project-name cb-NAME --project-directory DIR --env-file <empty> -f <runtime-file> ...`. The empty env file prevents `.env` from re-interpolating anything at run time.
- **EXE-4** The runtime file removes `env_file` from every service. The approved config already contains its values merged into `environment`; removing the file references prevents compose from re-reading files through agent-swappable paths.
- **EXE-5** For `stack_up`, every service's Dockerfile is read by the broker itself, component by component with `O_NOFOLLOW` relative to a directory fd for the project root. It must be a regular file of at most 1 MiB, and its contents are inlined as `dockerfile_inline`. A Dockerfile reached through a symlink, or not a regular file, fails the operation. For all other operations, Dockerfiles are replaced with a `FROM scratch` placeholder because no build happens.
- **EXE-6** Dockerfile *contents* are not part of the approved digest. Agents may freely edit Dockerfiles and application code. BuildKit sandboxes builds. The context is the project root or a directory inside it (POL-S11), read by the docker CLI inside the sandbox (5.9).
- **EXE-7** Host subprocesses (the docker CLI starting and removing sandbox containers, `docker inspect`, `docker build` of the sandbox image) run with argument lists (never a shell), stdin from `/dev/null`, a fresh session, and a minimal environment: `PATH`, `HOME`, `LANG`, plus `DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_CONFIG`, `DOCKER_CERT_PATH`, `DOCKER_TLS_VERIFY`, and `XDG_RUNTIME_DIR` if set. `COMPOSE_*` variables are not inherited. On timeout, the whole process group receives SIGTERM, then SIGKILL after 10 s. Processes inside the sandbox get the fixed environment of SBX-6 instead.
- **EXE-8** Timeouts: `up` 900 s, `down`/`restart` 300 s, `ps` 30 s, `logs` 60 s, `config` 60 s, `inspect` 15 s, sandbox housekeeping (version checks, `ps`, `rm`) 30 s, sandbox image build 600 s.
- **EXE-9** Mutating operations (`up`, `down`, `restart`, `reset`) take a per-project lock. A concurrent mutating call fails immediately with a "busy" message, naming the running tool when it runs in this server (MCP-7). `status` and `logs` do not lock, and run alongside a mutating operation.
- **EXE-10** Tool output is truncated from the front to at most 48 KiB, with a marker.
- **EXE-10a** Output of `up`, `restart` and `down` is condensed before it reaches the agent (MCP-5). Compose progress lines are collapsed to the final state of each object, and BuildKit step lines are dropped except for every line of a failed step (`#N ERROR`). All other lines (warnings, compose and daemon errors) are kept. The result is capped to the last 120 lines and 8 KiB. When `up` fails, a note follows the output. After a build failure, it says that no containers were replaced and that running services are the previous version. Otherwise it says the stack did not start and points to `stack_logs`.
- **EXE-10b** The result of `stack_up` starts with a one-line verdict (`SUCCESS: …` or `FAILED: …`). A timed-out `up` gets its own note: the previous version may still be running. When `up` fails at the build or times out, the broker remembers the time and the condensed output for that project, in memory only. Until the next successful `stack_up` or `stack_down`, `stack_status` and `stack_restart` start with a warning that the running containers are the previous version, followed by that output (capped to 2 KiB). `stack_logs` starts with the warning only, and points to `stack_status` for the error. A start failure (containers replaced) clears the record, because status and logs then show the real state. The repeated text is the agent's own build output, which `stack_up` has already returned to it (G4 unchanged).
- **EXE-11** The `logs` arguments are validated. `service` must be a service in the approved config. `tail` is an integer from 1 to 2000 (default 200). `since` matches `^\d{1,6}[smh]$` or an ISO-8601 date/time.

### 5.5 Identity and transport

- **ID-1** The project is determined by the broker, never by the caller. No tool takes a project, path, or file argument.
- **ID-2** Unix-socket mode (Linux):
  1. The broker reads `SO_PEERCRED` to get the peer PID.
  2. It reads `/proc/<pid>/cgroup` and extracts the last 64-hex container ID.
  3. It runs `docker container inspect` to get the container's mounts. The caller's project is the unique registered project whose directory equals, or lies under, the source of one of the container's bind mounts.
  4. Zero or multiple matches, a caller outside a container, or any error means "unidentified".
- **ID-3** Identity is established once per connection, at accept time. The registry entry is re-read on every tool call, so approvals and unregistrations take effect immediately.
- **ID-4** Unidentified unix connections complete the MCP handshake but see no tools. The `instructions` field explains why without revealing details, which go to the host log only.
- **ID-5** TCP mode (for setups where peer credentials do not map to a container, ID-2): the first line must be `AUTH <token>` within 10 s. Tokens are 256-bit, URL-safe, and generated by `compose-broker token NAME`. Only their SHA-256 is stored in the registry, and comparison is constant-time. Failed authentication closes the connection without a response.
- **ID-6** Binding TCP on a wildcard address logs a warning. The recommended bind address on Linux is the Docker bridge IP.

### 5.6 MCP interface

- **MCP-1** Transport: newline-delimited JSON-RPC 2.0, one session per connection, with a maximum message size of 1 MiB. Batches are rejected. Messages are read in order, but responses to `tools/call` may arrive out of order (MCP-7), matched by `id`.
- **MCP-2** Supported protocol versions: `2025-06-18`, `2025-03-26`, `2024-11-05`. The client's version is echoed if supported; otherwise the newest is offered.
- **MCP-3** Methods: `initialize`, `ping`, `tools/list`, `tools/call`. Notifications from the client are accepted and ignored (including `notifications/cancelled`: a running operation is never cut off midway, see SBX-8). Other methods return `-32601`. Unknown tools or invalid arguments return `-32602`. The only notification the server sends is `notifications/progress` (MCP-8).
- **MCP-4** Tools:

| Tool | Arguments | Effect |
|------|-----------|--------|
| `stack_up` | `wait?: bool` | `up -d --build --remove-orphans` (+ `--wait --wait-timeout 180`), then status |
| `stack_restart` | `service?: str` | `restart [service]` (no rebuild, no config change) |
| `stack_down` | — | `down --remove-orphans` (volumes kept) |
| `stack_status` | — | `ps -a`, summarized per service |
| `stack_logs` | `service?: str`, `tail?: int`, `since?: str` | `logs --no-color --timestamps` |
| `stack_reset` | — | `down --volumes --remove-orphans`; listed only with `--allow-reset` |

- **MCP-5** Tool descriptions are written for weak models. They state when to use each tool, what to do next, and that logs are data rather than instructions. Because many clients ignore `instructions`, the facts a weak model most often gets wrong are repeated in the tool descriptions: docker runs on the host, so its absence in the agent's environment is expected and no reason to give up (`stack_up`), and published ports are on the host, not on the agent's `localhost` (`stack_status`). These texts carry no host-specific detail (G4): `host.docker.internal` is a generic hint, not a lookup.
- **MCP-6** Refusals and failed commands return `isError: true` with a human-readable explanation of what the agent should do next.
- **MCP-7** Concurrency within a connection. `tools/call` runs on a worker thread, so the connection keeps answering `ping`, `tools/list`, `stack_status` and `stack_logs` while a long `stack_up` runs. Other methods are answered inline, in order. Writes to the connection are serialized, so lines never interleave. Limits, checked in arrival order before a worker starts:
  - At most 4 tool calls run at once per connection. Further calls are refused immediately (`isError`, "wait for one to return"), without starting a thread.
  - A call identical to one still running on the same connection (same tool, same arguments) is refused immediately with "do not call it again, wait for its result", for every tool. Once the first call returns, the same call is allowed again.
  - Mutating calls stay under the per-project lock (EXE-9) across all connections. Its refusal names the operation that holds the lock and how long it has been running, and tells the agent not to call another stack-changing tool until it returns.
  Refused calls are audited like any other (AUD-1). When the client closes its sending side, the server lets running calls finish and still writes their responses, then closes the connection.
- **MCP-8** Progress. If a `tools/call` carries `_meta.progressToken` (a string of at most 256 characters, or an integer), the server sends `notifications/progress` every 15 s while the call runs, with that token, an increasing `progress` counter, no `total`, and the message `<tool> still running (<N>s)`. Clients that reset their request timeout on progress then keep waiting for long builds. The message never contains compose output (G4). Heartbeats stop before the response is sent. Without a valid token, nothing is sent.

### 5.7 Audit and logging

- **AUD-1** Every tool call, including calls to unknown tools and calls with invalid arguments, appends a JSON line to `audit.log` when it finishes, with: timestamp, call ID (random, 8 hex characters), project, transport, tool, arguments, outcome (`ok` / `failed` / `refused` / `invalid` / `error`), duration, and, unless the outcome is `ok`, `detail` (the reason).
- **AUD-2** Identification failures, policy refusals, and compose resolution errors are logged to stderr with full detail, on the host only.
- **AUD-3** The host log (stderr) also records each session's start (client name and version, protocol), each tool call (tool, arguments, outcome, duration, and the reason, shortened, if not `ok`), and each connection's close with its call count. Every value the agent controls is written JSON-escaped (ASCII only), so it cannot inject terminal control sequences.
- **AUD-4** Long calls are also recorded when they start. For the stack-changing tools (`stack_up`, `stack_restart`, `stack_down`, `stack_reset`), once the tool name has been validated and the call has passed the MCP-7 limits, the broker writes an `audit.log` record with outcome `started` (no duration), carrying the same call ID as the final record. It also writes a host-log line `[NAME] "tool" {args} -> started`. A call refused by the project lock (EXE-9) or by argument validation therefore shows `started` followed by `refused` or `invalid`. Short calls (`stack_status`, `stack_logs`) get only the final record.

### 5.8 CLI

| Command | Purpose |
|---------|---------|
| `register NAME DIR [-f FILE]... [--allow-reset] [--no-approve]` | Add a project (REG-*) |
| `unregister NAME` | Remove the registry entry, approved config, and token. Does not stop the stack. |
| `down NAME... \| --all` | Take stacks down (CLI-1) |
| `list` | Show projects, approval status, and flags |
| `check NAME` | Policy findings + drift vs. approved (APR-6) |
| `approve NAME [--yes]` | Approval flow (APR-3) |
| `token NAME` | Create or rotate the TCP token and print its path |
| `snippet NAME [--tcp HOST:PORT]` | Print devcontainer.json / .mcp.json snippets |
| `serve [--unix PATH] [--no-unix] [--tcp HOST:PORT] [--socket-mode MODE]` | Run the server |
| `sandbox build` | Download and verify the pinned binaries, build the sandbox image, record its ID (SBX-2, SBX-3) |

- **CLI-1** `down` runs the same command as `stack_down` (`down --remove-orphans`, volumes kept): in the sandbox, on a runtime file derived from the approved config (EXE-2, SBX-1). The human's call honours the same rules as the agent's. A project whose approved config is missing or fails its integrity check (APR-5) is skipped, and the CLI prints the manual `docker compose -p cb-NAME down` command (to be run outside the project directory). A project with a mutating sandbox still running (SBX-8), for example an agent's `stack_up` in the server, is skipped as busy. The server's in-process lock (EXE-9) does not cover the CLI, but the sandbox check works across processes. `--all` selects every registered project that has at least one container (running or stopped) labelled `com.docker.compose.project=cb-NAME`, from one host `docker ps -a`. It is meant for freeing host ports that stacks left running by agents still hold. Remaining projects are still processed after one fails, and the exit code is 1 if any failed.

### 5.9 Compose sandbox

**Why.** Path-based attacks on the broker had a common cause: the docker CLI ran on the host, so any symlink the agent planted in the project resolved against the host filesystem. The sandbox runs the CLI in a container whose filesystem holds nothing but the project, the broker's runtime files and the daemon socket. A planted symlink then resolves inside that container, where there is nothing worth reading. This covers nested and root build contexts, `.dockerignore`, and everything `compose config` reads (`.env`, `env_file`, `include`, `extends`). It replaced an earlier design that copied nested build contexts into broker-owned snapshots.

- **SBX-1 Scope.** Every `docker compose` invocation runs in a sandbox container: `config` (APR-1, APR-4, APR-6, EXE-1) and all runtime operations (EXE-3). The host's compose and buildx plugins are never used. The host docker CLI only starts and removes sandbox containers and runs `docker container inspect` for identity (ID-2), which reads no project files.
- **SBX-2 Image.** The image definition is vendored in the package (`compose_broker/sandbox_image/`): a `Dockerfile` (`FROM scratch`, the three binaries and a CA bundle, nothing else) and `manifest.json`, which pins the static docker CLI, the compose plugin and the buildx plugin by version, URL and sha256, for `amd64` and `arm64`. The human builds it with `compose-broker sandbox build`:
  - The broker downloads each binary (or reuses a cached copy) and verifies its sha256 against the manifest. Any mismatch aborts the build, and the bad download is not cached.
  - The docker CLI is extracted from its release tarball by member name, and the plugins are used as-is.
  - The host's CA bundle (the first of `/etc/ssl/certs/ca-certificates.crt`, `/etc/pki/tls/certs/ca-bundle.crt`, `/etc/ssl/ca-bundle.pem`, `/etc/ssl/cert.pem`) is copied to `/etc/ssl/certs/ca-certificates.crt`. The client verifies registry TLS itself (found in the second host run). The host is trusted and its bundle keeps locally installed CAs working. Its sha256 is recorded (SBX-3) but not checked at startup: a CA update on the host just means the next `sandbox build` picks it up. The bundle holds public certificates only, so it being readable through a swap discloses at most which extra CAs the host trusts.
  - The image is built with `--network none` from a fresh broker-owned directory under the state dir, never a project directory, which is removed afterwards. The tag is `compose-broker-sandbox:<first 12 hex of the definition digest>`, for humans only (SBX-3).
  - Updating a pin is a reviewed change to `manifest.json`. Pins should name releases at least two weeks old.
- **SBX-3 Image identity.** `sandbox build` records the image ID (`sha256:...`), the definition digest (sha256 over the sha256s of `manifest.json` and `Dockerfile`), the tool versions and the CA bundle's path and sha256 in `~/.config/compose-broker/sandbox.json` (0600). Sandbox containers are always started by image ID, never by tag, so retagging on the daemon cannot substitute another image.
- **SBX-4 Startup check.** `serve`, `approve` and `check` verify the preconditions (SBX-9), that the vendored image definition still has the recorded digest, that an image with the recorded ID exists, and that `docker --version`, `docker compose version --short` and `docker buildx version` inside it report the manifest's versions. On any mismatch they refuse to run and tell the human to run `compose-broker sandbox build`. Because only the sandbox's compose is used (SBX-1), this check replaces any comparison with the host's compose. Switching an existing installation to the sandbox requires re-approving each project, because the approved digest comes from the sandbox's compose output.
- **SBX-5 Mounts.** This list is exhaustive. Anything else mounted into the sandbox becomes readable by the agent through a symlink swap.
  - The project directory: bind, **read-only**, at its exact host path. It must be the same path because compose passes bind-mount sources (POL-S9) to the daemon as host paths.
  - Every other file the call needs (the runtime file of the current operation, the round-trip file for APR-4, the empty env file, and any compose file registered from outside the project): bind, read-only, at `/cb/<n>/<basename>`. Mount values are CSV-quoted, so paths with commas or quotes stay intact.
  - The daemon's unix socket, at `/var/run/docker.sock`. A socket's contents cannot be read through a symlink swap, so exposing it in the context is harmless.
  - A `tmpfs` at `/tmp` (64 MiB) for `HOME` and `DOCKER_CONFIG`, which buildx and compose write to.
  - Never: the host home directory, `~/.docker` (registry credentials, contexts), TLS client certificates, or the broker's config and state directories.
- **SBX-6 Container options.**
  - `--rm`, `--network bridge`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--init`, `--log-driver none` (output is taken from the attached streams, so the daemon stores nothing), `--pids-limit 512` and `--memory 1g`. The entrypoint is the image's docker CLI, and the image is named by ID.
  - Network: the client fetches registry auth tokens itself, even for anonymous pulls of public images (found in the first host run, which used `--network none`), so it needs network access. It uses the default bridge, not the host network, so it cannot reach services on the host's loopback. As a result, registries addressed as `localhost:PORT` do not work. Before the sandbox, the CLI ran on the host with full network access, so this is not a regression.
  - The host `docker run` client runs with a broker-owned working directory (the state dir), not the project.
  - `--workdir` is the project directory. No stdin, and no TTY, so stdout and stderr stay separate.
  - Environment, set explicitly and with nothing inherited: `PATH`, `HOME=/tmp`, `DOCKER_CONFIG=/tmp/docker`, `LANG`, `DOCKER_HOST=unix:///var/run/docker.sock`, `BUILDKIT_PROGRESS=plain`, `COMPOSE_ANSI=never`, `DOCKER_CLI_HINTS=false`. This is visible to the agent via `/proc/1/environ` through a swap, so it must never contain secrets.
  - User: on a rootful daemon, the broker's own UID:GID, plus `--group-add` with the socket's group, so project files are read with the same permissions as today. On a rootless daemon, container root, which maps to the host user.
- **SBX-7 Names and labels.** Each sandbox container is named `cb-broker-NAME-<op>-<random>` and labelled `com.compose-broker.sandbox=NAME` and `com.compose-broker.op=<op>` (version checks use the project name `_`). Labels alone are not trusted: the agent's own stack could carry the same labels. A container counts as a sandbox only if its labels match **and** its image ID equals the recorded one. POL-S5 also forbids the `com.compose-broker.` prefix, so the agent cannot label its own containers that way at all.
- **SBX-8 Lifecycle.**
  - On timeout (EXE-8), the broker sends SIGTERM to the `docker run` client (it is forwarded into the container), waits 10 s, then runs `docker rm -f` on the sandbox container by name. Killing the client alone would leave the container running.
  - After every mutating operation, and after any timeout, the broker runs `docker rm -f` on the sandbox container (a no-op if `--rm` already removed it). The per-project lock (EXE-9) is released only after that.
  - Before a mutating operation, a still-running mutating sandbox container for the same project (SBX-7) makes the operation fail as "busy, retry".
  - At `serve` startup, all sandbox containers (SBX-7) left over from a previous run are removed.
- **SBX-9 Preconditions.** Supported only with a local daemon over a unix socket. The broker asks `docker context inspect` for the current endpoint, which reflects `DOCKER_HOST`, `DOCKER_CONTEXT` and the configured current context, and requires `unix://...` pointing at an existing socket. `DOCKER_TLS_VERIFY` and `DOCKER_CERT_PATH` must be unset. Anything else would require mounting credentials (SBX-5), so the broker refuses to start. Rootless and SELinux daemons are detected from `docker info`'s security options. SELinux-enforcing hosts are unsupported until relabeling is designed, because `:z` would modify the project directory's labels. Docker Desktop is unsupported.
- **SBX-10 Unchanged.** EXE-2 to EXE-5 (runtime file, empty env file, `env_file` stripping, inlined Dockerfiles) stay as they are, as defence in depth. EXE-7 applies to the host-side `docker run` and `docker rm` clients. Output handling (EXE-10, EXE-10a) and error confidentiality (G4) are unchanged: sandbox output is treated exactly like compose output today.
- **SBX-11 Limitations.**
  - No registry credentials are available, so builds whose `FROM` names a private image fail, and `up` cannot pull private images. A human can pre-pull service images on the host. Builds from private base images need the credential proxy (SBX-13).
  - Starting the sandbox adds latency to every tool call. On the first host, `stack_status` and `stack_logs` took 0.3–0.45 s end to end.
- **SBX-12 To verify on a real host.** The integration tests exercise 2, 3, 4 and 7 (they fail if any of them is wrong). These passed on the first Linux host (2026-11-10, rootful, docker 29.9.2 / compose 5.6.1 / buildx 0.38.1 in the sandbox). Items 1, 5 and 6 are still open and need checking by hand, and all items need re-checking after any pin update.
  1. compose and buildx (including bake) read nothing from the working directory beyond the files they are given: no bake files, `.docker` directories or plugin paths from the project.
     *Open.* If wrong, the impact is limited: whatever such a file references resolves inside the sandbox, outputs can only land on the tmpfs (the project is read-only), and nothing it names can be executed on the host. The remaining risk is the agent changing build options without approval, for example bake `entitlements` (buildx should refuse those without `--allow`, which the broker never passes) or extra build args. To check: add a `docker-bake.hcl` with a distinctive label on the demo's targets, a `compose.override.yaml` and a `.docker/` directory to a copy of the demo, then run `stack_up` and inspect the built image. None of them may have any effect.
  2. Attached output still streams with `--log-driver none`.
  3. buildx and compose work with a read-only root filesystem and `DOCKER_CONFIG` on tmpfs.
  4. The image needs nothing else at run time (`/etc/passwd`, timezone data). CA certificates turned out to be needed and are included (SBX-2).
  5. SIGTERM forwarded by `docker run` cancels a running build and an in-progress `up` cleanly.
     *Open.* If SIGTERM is not enough, SBX-8 still applies: after 10 s the broker runs `docker rm -f` on the sandbox, and BuildKit cancels a build when its client session disappears. So the worst case is an `up` cut off midway, which leaves some services recreated and some not, the same as killing compose on the host. To check: give a copy of the demo a Dockerfile step `RUN sleep 600`, lower `TIMEOUTS["up"]` to about 20 s, and call `stack_up`. It must report a timeout. Within about 10 s, `docker ps -a --filter label=com.compose-broker.sandbox` must be empty, and the daemon must show no build still running.
  6. The user mapping in SBX-6 works on rootless Docker.
     *Open, untested.* Container root should map to the host user, who owns both the socket and the project files. If the mapping is wrong, the sandbox cannot read the project or reach the socket, so every operation fails with an error. That fails closed, not open. To check: run the integration tests with `DOCKER_HOST=unix://$XDG_RUNTIME_DIR/docker.sock` against a rootless daemon.
  7. Inside the sandbox, `docker compose config` resolves the demo identically, and APR-4's round-trip stays exact.
- **SBX-13 Credential proxy (deferred).** Some projects use private images, but not all, so this is deferred until the first such project moves to the broker. Sketch, to be designed properly before implementation:
  - The sandbox still gets no credentials file. Instead a broker-written `config.json`, mounted read-only (SBX-5), names a credential helper, `docker-credential-cb`, shipped in the image. The helper forwards `get` requests over a per-call unix socket that the broker mounts into the sandbox, and the broker answers from the host's credential store.
  - The broker answers only for registries the human allowed for that project, recorded at approval, for example `compose-broker approve NAME --registry ghcr.io/acme`. Everything else gets "not found". `store`, `erase` and `list` are always refused.
  - The token never sits in a file in the sandbox. It flows from the broker through the helper to the CLI and on to BuildKit's session, and none of those is readable from a build. The agent can still make builds pull any image the token can read. The allowlist and read-only, project-scoped tokens bound that.
  - Open: whether BuildKit asks the client for credentials through this helper for `FROM` in every build mode (classic builder vs bake), and whether short-lived tokens can be minted per call instead of passing long-lived ones.

## 6. Devcontainer integration

`devcontainer.json` (unix mode):

```json
"mounts": ["source=${localEnv:XDG_RUNTIME_DIR}/compose-broker,target=/run/compose-broker,type=bind"]
```

Mount the **directory**, not the socket: a file mount keeps pointing at a dead inode after the broker restarts. The image needs `socat`.

MCP client config (Claude Code `.mcp.json` shown; other clients take the same command and args):

```json
{ "mcpServers": { "compose": {
    "command": "socat",
    "args": ["STDIO", "UNIX-CONNECT:/run/compose-broker/broker.sock"] } } }
```

TCP mode: mount `~/.config/compose-broker/tokens/NAME` read-only at `/run/compose-broker-token`, and use:

```json
"command": "sh",
"args": ["-c", "{ printf 'AUTH %s\\n' \"$(cat /run/compose-broker-token)\"; cat; } | socat - TCP:host.docker.internal:7777"]
```

Before the first `serve`, the human runs `compose-broker sandbox build` once (SBX-2), and again whenever the image definition (`manifest.json` or `Dockerfile`) changes. `serve` refuses to start until then (SBX-4).

Compose files must follow the policy shape: at most one bind mount, of the project root, and build contexts inside the project (`context: .` with `dockerfile: path/to/Dockerfile`, or `context: ./frontend`).

## 7. Known limitations and items to verify

- **V1** EXE-4 relies on `docker compose config` merging `env_file` contents into `environment`. Verify on the installed compose version: the round-trip check and a test service with an `env_file` will show it.
- **V2** Per-Dockerfile ignore files (`Dockerfile.dockerignore`) are not honoured, because Dockerfiles are inlined. The `.dockerignore` at the root of each build context is honoured.
- **V3** The PID-to-container mapping (ID-2) has a theoretical PID-reuse race between accept and `/proc` read. Exploiting it would require causing a PID in another project's container to be reused at the right moment. This is accepted for v1; pidfd-based verification is future work.
- **V4** Requests on one connection are processed sequentially, so a long `stack_up` delays a concurrent `ping` on the same connection. Cancellation is not supported.
- **V5** The policy allowlist may reject keys that newer compose versions emit by default. Extend it deliberately, never by switching to a denylist.
- **V6** Unix mode requires Linux with the broker running in the same PID namespace as the Docker daemon's containers' host. Docker Desktop is unsupported, because the sandbox requires a local unix-socket daemon (SBX-9).
- **V7** *(Closed by 5.9.)* Root build contexts used to be read by the docker CLI on the host, by path, so swapping a directory for a symlink mid-transfer could pull host files into a build.
- **V8** *(Closed by 5.9.)* `compose config` used to run on the host, so a symlinked `.env`, `env_file`, `include` or `extends` target could pull a host file's contents into the resolved config.
- **V9** Sandbox containers add latency to every tool call (0.3–0.45 s for status and logs on the first host). Status and logs could later run through one long-lived read-only sandbox per project if this matters.

## 8. Future work

### 8.1 Options under consideration

Designs that have been discussed but not decided. Nothing here is a requirement.

**OPT-1 Attach the calling devcontainer to its stack's network.**

*Problem.* The agent needs to reach its running app (for example `curl` the backend) from inside the devcontainer. Today that means publishing ports on an address the devcontainer can reach and using `host.docker.internal`. That route reaches every host service listening on that address, not just the app, and Docker cannot limit it to particular ports. The README describes a manual firewall script for the devcontainer. Limiting the agent to particular ports of its own stack adds little, because those containers run the agent's code anyway. The useful boundary is "the agent reaches its stack, and nothing else on the host".

*Sketch.*
- Opt-in per project at registration (for example `register --attach-caller`), recorded in the registry and shown by `list`.
- After a successful `stack_up`, the broker runs `docker network connect` to attach the **calling** container (known from ID-2) to the stack's networks. That is `cb-NAME_default` plus any other network the approved config declares, unless the config marks some as not attachable. The agent then uses service names (`http://backend:8000`), and ports only need publishing on `127.0.0.1`, for the human.
- Before `stack_down` and `stack_reset`, the broker disconnects every container it attached. Otherwise compose cannot remove the network ("network has active endpoints") and the operation fails.
- The broker attaches only the container a request came from, only to that project's networks, and records what it attached, so it can also clean up after a restart.

*Constraints and open questions.*
- **Unix-socket mode only:** TCP mode (ID-5) has no container identity. Should TCP mode refuse the option, or accept a container ID recorded at `register` time?
- **Lifecycle:** after a host or devcontainer restart, the devcontainer is not attached until the next `stack_up`. Should `stack_status` re-attach? It is not a mutating op today (EXE-9).
- **Other agents:** when several devcontainers share a project directory (REG-4 allows only one project per directory, but not one container per project), each caller gets attached. Is that wanted?
- **Policy:** POL-T2 forbids `external` networks. The attach must stay a broker action, never something the compose file can request.
- **Done right, this replaces the need for published ports to be reachable from containers.** Combined with a firewall that drops all traffic from the devcontainer to the host and private ranges, the devcontainer then reaches exactly its stack. The app containers are a separate question (see accepted risks).
- **Cost:** two new host-side docker calls in the mutating path, plus state for the attached containers. Weigh this against the manual firewall script in the README.

### 8.2 Other items

The registry credential proxy (SBX-13), per-service restart in `stack_up`, per-project policy overrides (reviewed at approval), pidfd-based identity, an approval diff view that highlights security-relevant changes, and resource limits on builds.

**Persisting the stale-build record (EXE-10b).** The record of a failed or timed-out `stack_up` lives in broker memory, so a broker restart forgets it. After a restart, `stack_status` and `stack_logs` look healthy again while the previous version is still running. The record could go in the state directory (for example `state/stale/<project>.json`, written with `atomic_write`, mode 0600), and be removed on the next successful `stack_up` or `stack_down`, and by `unregister`. Before doing that, decide two things. A record from before the restart may be wrong if the human rebuilt the stack by hand meanwhile, so keep its timestamp in the warning. And the record holds build output that the agent produced, so it must stay under the same size cap and never be shown to the human as trusted text.
