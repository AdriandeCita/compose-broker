# compose-broker

A small host-side MCP server that lets a coding agent in a devcontainer rebuild, restart and read the logs of **its own project's** docker compose stack, without giving it the Docker socket.

- Each devcontainer can only operate its own project's stack. The broker works out the project from the connection itself: the caller's container mounts (unix socket) or a per-project token (TCP).
- The agent never runs your compose files directly. It runs a config that you, the human, approved on the host. Any change to services, ports, mounts or volumes needs a new approval. Code and Dockerfile changes don't.
- An allowlist policy rejects privileged containers, host namespaces, extra bind mounts, foreign volumes and networks, and more.
- No dependencies: Python 3.11+ standard library and the `docker` CLI. Compose and buildx come from the broker's own pinned sandbox image, not from your host.

The agent sees these MCP tools:

| Tool | What it does |
|------|--------------|
| `stack_up` | Rebuild images and (re)create the stack (`wait: true` waits until services are running) |
| `stack_restart` | Restart containers (all or one `service`) without rebuilding |
| `stack_down` | Stop and remove containers and networks; volumes are kept |
| `stack_status` | Each service's state, exit code and ports |
| `stack_logs` | Recent logs with timestamps (`service`, `tail` 1–2000, `since` like `10m`) |
| `stack_reset` | Like `stack_down`, but also deletes volumes. Only offered if the project was registered with `--allow-reset` |

For what the broker does and doesn't protect against, see [Security model](#security-model) below. The full requirements and threat model are in [SPEC.md](SPEC.md).

## Install

On a Linux host:

```sh
git clone <this repo> ~/opt/compose-broker
ln -s ~/opt/compose-broker/bin/compose-broker ~/.local/bin/compose-broker
```

Use `bin/compose-broker` rather than `python3 -m compose_broker`. The launcher runs Python in isolated mode, so it can't import stray modules from whatever directory you start it in.

Then build the sandbox image once:

```sh
compose-broker sandbox build
```

The broker never runs `docker compose` on the host. Every compose call runs in a throwaway container that sees only the project directory (read-only) and the Docker socket. A symlink the agent plants in the project, for example a build context pointing at `~/.ssh`, resolves inside that container, where there is nothing to steal. The image holds only the docker CLI, compose and buildx, plus your host's CA certificates. The binaries are pinned by sha256 in [`compose_broker/sandbox_image/manifest.json`](compose_broker/sandbox_image/manifest.json). `sandbox build` downloads them, checks the hashes and builds the image. Run it again after pulling changes to `compose_broker/sandbox_image/`; `serve` refuses to start until you do.

Requirements: Linux, with a local Docker daemon on a unix socket. Rootful Docker is tested. Rootless Docker should work, but hasn't been tested yet. Remote daemons, TLS, SELinux-enforcing hosts and Docker Desktop are not supported. `sandbox build` needs internet access once, to download the binaries; later builds reuse the verified cache.

Upgrading from a version without the sandbox: run `sandbox build`, then `compose-broker check NAME` for each project. Approvals now come from the sandbox's compose, so some projects may need one `approve`.

## Quick start

**1. Shape your compose file to the policy.** Build contexts must be inside the project (`context: .` or `context: ./frontend`). Don't set `image:` on built services, and bind-mount at most the project root. Base images and service images must be public, or pre-pulled on the host: the sandbox has no registry credentials. For a working example, see [examples/demo/compose.yaml](examples/demo/compose.yaml).

```yaml
services:
  api:
    build: { context: ., dockerfile: api/Dockerfile }
    volumes: [".:/workspace"]          # the only bind mount allowed
    user: "1000:1000"                  # your host `id -u`:`id -g`, or approval warns you
    ports: ["127.0.0.1:8080:8080"]     # localhost only; "8080:8080" warns, but the devcontainer needs it (step 5)
```

**2. Register and approve the project.** Registration resolves the config, runs the policy, shows you the full config and asks you to confirm.

```sh
compose-broker register shop ~/code/shop            # add -f FILE (repeatable) for non-default files
                                                     # add --allow-reset to offer stack_reset
```

**3. Run the server** and leave it running:

```sh
compose-broker serve        # listens on $XDG_RUNTIME_DIR/compose-broker/broker.sock
```

**4. Connect the devcontainer.** `compose-broker snippet shop` prints both pieces. Add the mount to `.devcontainer/devcontainer.json`. Mount the directory, not the socket, so a broker restart doesn't leave a dead socket in the container. The image needs `socat`.

```json
"initializeCommand": "mkdir -p ${localEnv:XDG_RUNTIME_DIR}/compose-broker",
"mounts": ["source=${localEnv:XDG_RUNTIME_DIR}/compose-broker,target=/run/compose-broker,type=bind"]
```

The `initializeCommand` runs on the host before the container starts. It creates the directory in case the broker hasn't run since a reboot or logout, which empty `XDG_RUNTIME_DIR`. Without it, the bind mount fails and the devcontainer won't start. With it, the container starts either way, and the socket appears inside it once the broker starts. If the broker isn't running, the MCP server fails to connect. Start the broker, then reconnect (`/mcp` in Claude Code). If the project already has an `initializeCommand`, chain the two with `&&`.

Then add the server to the project's `.mcp.json`:

```json
{ "mcpServers": { "compose": {
    "command": "socat",
    "args": ["STDIO", "UNIX-CONNECT:/run/compose-broker/broker.sock"] } } }
```

Rebuild the devcontainer. The agent now has the `stack_*` tools.

**5. Optional: limit what the devcontainer can reach on the host.** The agent usually wants to try the running app, for example `curl host.docker.internal:8000`. Docker can't limit that to particular ports: every host service listening on that address is reachable too (sshd, other projects' databases, the broker's TCP port). The devcontainer's firewall script can limit it. Like the rest of `.devcontainer/`, the script only changes when you rebuild the container.

Publish the app's ports the plain way, so both your browser and the devcontainer reach them:

```yaml
    ports:
      - "8000:8000"
```

Approval warns about this (POL-S10): the port is also reachable from your LAN. Accept the warning, or block those ports from the LAN in the host firewall.

Most devcontainers for coding agents already ship a default-deny firewall script, such as Claude Code's reference `init-firewall.sh`. That script computes the gateway as `HOST_IP` and then allows the gateway's whole /24, so every port on the host is reachable:

```bash
iptables -A OUTPUT -d "$HOST_NETWORK" -j ACCEPT
```

Replace that line with the ports you published:

```bash
for port in 8000 8080 4200; do   # the app's published ports (+ 9223 for a host browser, see "Letting the agent use a browser")
  iptables -A OUTPUT -d "$HOST_IP" -p tcp --dport "$port" -j ACCEPT
done
```

Also check the script for rules that allow a port to *any* destination. The reference script allows outbound SSH (`--dport 22`) and DNS that way, and both then reach the host as well. If you need SSH only to the internet, add `! -d "$HOST_NETWORK"` to that rule.

The firewall only holds if the agent can't change it. In the reference setup it can't, but check your own:

- The agent runs as a non-root user (`remoteUser`), and `sudo` lets it run only the firewall script. With root and `NET_ADMIN`, it could simply flush the rules.
- The Dockerfile copies the script into the image (for example `/usr/local/bin/`), and `postStartCommand` runs that copy, not the one in the workspace.

Check from inside the devcontainer:
- `curl host.docker.internal:8000` must work.
- `nc -zv host.docker.internal 22` must fail, and so must `nc -zv <your host's LAN IP> 22`.
- `sudo iptables -F` must be refused.

Things to know:

- **It covers the devcontainer only, not the app containers.** The agent writes their code, so it can still make, say, the backend connect to a host service and report back in its logs. Containers reaching the host is an accepted risk in [SPEC.md](SPEC.md). Closing it too needs a host firewall rule that drops traffic from *all* Docker bridges (`docker0`, `br-*`) to the host, except the ports you allow.
- **The broker's unix socket is unaffected.** In TCP mode, add the broker's port to the loop.
- **To let the agent drive a browser on your host**, see [Letting the agent use a browser](#letting-the-agent-use-a-browser).
- **Attaching the devcontainer directly to the stack's network is under consideration** (SPEC.md, OPT-1).

## Security model

The broker assumes the agent is hostile. That includes everything it can write in the project (compose files, `.env`, Dockerfiles, symlinks) and every message it sends over MCP. Tool descriptions and the agent's cooperation are never relied on.

### What it protects against

- **Escaping to the host.** The agent can't run commands on the host or read host files outside its project through the broker. Compose runs only in the sandbox, so paths and symlinks in the project resolve there, not on your host. Dockerfiles are read without following symlinks.
- **Touching other projects.** A devcontainer can drive only its own stack. The project comes from the connection, never from a tool argument. Names, volumes, networks and built images are confined to `cb-NAME`, and labels that could impersonate other projects or the broker are refused.
- **Changing the stack without you.** Any change to services, images, ports, mounts, volumes or environment needs your `approve`. Until then, `stack_up` refuses to start anything.
- **Dangerous configurations, even if you approve them.** Policy errors can't be approved at all: privileged containers, added capabilities, devices, host namespaces, bind mounts other than the project root (including the Docker socket), host-path volumes, foreign networks or volumes, and build secrets or SSH forwarding.
- **Leaking host details.** Compose errors and resolved configs are never shown to the agent. It gets a short reason and is told to ask you. The full detail goes to the `serve` output and the audit log.

### What it accepts

- **Your containers run the agent's code.** Whatever the app containers can reach, the agent can reach: the internet, other services on your network, services on the host. A firewall in the devcontainer (step 5) doesn't change what the app containers can reach. They can also read everything in the project directory.
- **Approval is a real decision.** Within the policy, the broker runs exactly what you approved, including environment values and published ports. Some risky settings only get a warning: a port published on all interfaces, or a container running as root with the project mounted writable.
- **Root containers can write into your project.** On rootful Docker, files they create there are owned by root on the host. That could include a setuid-root program, which would run as root if you executed it. Approval warns about this. Avoid it with `user: "UID:GID"` (your host IDs), a read-only mount (`.:/workspace:ro`), or rootless Docker.
- **Resource use.** Builds and containers can use CPU, memory and disk. The broker only limits how long a call may run.
- **Bugs in the platform:** the kernel, the Docker daemon, BuildKit and compose.
- **`.devcontainer/` changes.** The agent can edit them, and they take effect when you next rebuild the devcontainer. Review them before rebuilding.

### Where you can count on it

Under these conditions:

- The broker runs on Linux against a local Docker daemon, started through `bin/compose-broker`, with a sandbox image built from this repository.
- The devcontainer has no access to the Docker socket or to the broker's directories (`~/.config/compose-broker`, `~/.local/state/compose-broker`). The only broker-related mounts are the socket directory and, in TCP mode, that project's own token.
- The project directory holds nothing the agent shouldn't see. The agent can read the whole directory, so keep host credentials and other projects' secrets out of it.
- You don't run programs from the project directory that you didn't expect to find there, especially root-owned ones, unless every service that mounts the project writable runs as a non-root user.
- You read approval diffs, especially new images, environment values, ports and volumes.

Outside these conditions (Docker Desktop, remote or TLS daemons, a Docker socket mounted into the devcontainer), the guarantees above don't hold. Rootless Docker is expected to work but isn't verified yet. The open items are listed in SPEC.md, SBX-12.

## Which compose files are used

If you don't pass `-f`, `register` looks in the project root for a single file named `compose.yaml`, `compose.yml`, `docker-compose.yaml` or `docker-compose.yml`. If it finds exactly one, it uses it, so older `docker-compose.yml` projects work as they are. If it finds none, or several (an old `docker-compose.yml` next to a new `compose.yaml`), it stops and asks you to choose with `-f`. It never guesses between files and never searches subdirectories.

When a stack is built from several files, list them in the same order you would give to compose. Later files override earlier ones:

```sh
compose-broker register shop ~/code/shop -f compose.yaml -f compose.dev.yaml
```

From then on the broker always uses exactly these files. `COMPOSE_FILE` in the project's `.env` is ignored, so the agent can't switch to a different set. Files pulled in through `include` or `extends` are part of the resolved config, so changes to them need approval like any other.

Compose runs in a sandbox that sees only the project directory and the files you listed with `-f`. So everything else compose reads must be inside the project: `include` and `extends` targets, `env_file`s, `.env`, build contexts. A reference to `../shared/compose.yaml` fails to resolve. Move the file into the project, or register it with `-f`.

Two more limitations:

- To change the file list later, run `unregister` and then `register` again, which also means a fresh approval.
- Compose profiles aren't supported. Services with a `profiles:` key are never started. To switch between variants of a stack, use override files with `-f` instead.

## When the compose config changes

The broker compares the live compose config with the approved one each time the agent calls `stack_up`. If they differ, it doesn't start anything. Instead it tells the agent that it can't approve the change itself, and that it should describe the change and ask you to approve it. Run `compose-broker check shop` to see the diff and any policy findings, then `compose-broker approve shop`. The running server picks up the new approval right away, without a restart.

While the change waits for approval, the other tools (`stack_status`, `stack_logs`, `stack_restart`, `stack_down`) keep working from the approved config. So a broken or half-edited `compose.yaml` never locks the agent out of logs or a clean shutdown.

Only the resolved config counts: compose files, overrides, `include`d files, and `.env` values that feed into it. Application code and Dockerfiles can change freely, and the agent just calls `stack_up` again to rebuild.

## Day-to-day

| Situation | Command |
|-----------|---------|
| Agent says the compose config changed and `stack_up` was refused | `compose-broker check shop` to see the diff and policy findings, then `compose-broker approve shop` |
| See registered projects and approval state | `compose-broker list` |
| Stop a stack an agent left running (for example, its ports collide with something you start by hand) | `compose-broker down shop`, or `compose-broker down --all` for every registered project that has containers. Volumes are kept. |
| Remove a project (leaves its stack running) | `compose-broker unregister shop` |
| See what agents did | The `serve` output shows one line per tool call (arguments, outcome, reason). Stack-changing calls (`stack_up`, `stack_restart`, `stack_down`, `stack_reset`) also get a `-> started` line when they begin, so you can see that a long build is in progress. `~/.local/state/compose-broker/audit.log` keeps the same as JSON lines, with full reasons. A `call` ID links a `started` record to its final one. |
| Find out why a connection got no tools | the `serve` output on stderr (details are never sent to the agent) |

### Watching the stack yourself

The broker always starts the stack detached, but it's an ordinary compose project named `cb-NAME`. To follow it as you would with a foreground `docker compose up`, run this on the host (this needs the compose plugin on your host, though the broker itself doesn't):

```sh
docker compose -p cb-shop logs -f          # add a service name or --tail 50 as needed
```

Run it outside the project folder. With only `-p`, compose finds the containers by their project name, so your host's compose never has to load the agent-editable `compose.yaml` or `.env`. Your terminal isn't tied to the containers' lifetime, though. Recent compose versions keep following when `stack_up` recreates containers, but `stack_down` ends the stream, so just re-run the command.

A convenient setup is two terminals: `compose-broker serve` shows what the agent does, and `logs -f` shows how the app responds.

## Letting the agent use a browser

An agent can test a web app's UI by driving a browser on your host over the Chrome DevTools Protocol. The browser runs outside the devcontainer, so this is independent of the broker. It's just one more port the devcontainer needs to reach.

Chromium-based browsers listen for DevTools only on `127.0.0.1`, which the devcontainer can't reach. So start the browser with a separate profile, and forward port 9223 to it, accepting connections from Docker's address range only. This example uses Brave, but any Chromium-based browser works the same way:

```sh
brave --remote-debugging-port=9222 --user-data-dir=$HOME/.cache/agent-browser
socat -d -d TCP-LISTEN:9223,fork,reuseaddr,range=172.16.0.0/12 TCP:127.0.0.1:9222
```

The separate profile (`--user-data-dir`) matters: whoever reaches the DevTools port has full control of that browser, including its cookies and logged-in sessions. Keep your everyday profile out of it.

Inside the devcontainer, the agent connects to `http://<gateway IP>:9223`, for example `http://172.17.0.1:9223`. Use the IP rather than `host.docker.internal`, because the browser refuses DevTools requests whose `Host` header isn't an IP address or `localhost`.

If you use the firewall from [step 5](#quick-start), add 9223 to the loop of allowed ports.

`socat`'s `range` option already drops connections from outside `172.16.0.0/12`, but `socat` still listens on every interface, and your LAN may itself use addresses in that range. So also block 9223 from your LAN in the host firewall, for example by accepting it only from the devcontainer's bridge:

```bash
sudo iptables -I INPUT -p tcp --dport 9223 -j DROP
sudo iptables -I INPUT -p tcp --dport 9223 -i docker0 -j ACCEPT   # br-* instead if the devcontainer is on a compose network
```

These rules don't persist across reboots on their own. Use your distribution's mechanism (ufw, firewalld, `iptables-save`) to keep them.

## TCP mode

Unix-socket identity needs the caller's PID to map to a container on the host (SPEC ID-2, V6). Where that doesn't work in your setup, use a token over TCP instead:

```sh
compose-broker token shop                  # writes ~/.config/compose-broker/tokens/shop
compose-broker serve --tcp 127.0.0.1:7777  # on Linux, prefer the docker bridge IP
compose-broker snippet shop --tcp 127.0.0.1:7777
```

The snippet mounts the token read-only into the container. It sends `AUTH <token>` as the first line through `socat` to `host.docker.internal`. Running `token` again rotates the token, and the old one stops working at once.

## Files

| Path | Contents |
|------|----------|
| `~/.config/compose-broker/projects.json` | Registry |
| `~/.config/compose-broker/approved/NAME.json` | Approved, fully resolved config. Compose runs a copy generated from this file, never your project files, which the agent could change between the check and the run. |
| `~/.config/compose-broker/tokens/NAME` | TCP token |
| `~/.config/compose-broker/sandbox.json` | Sandbox image ID, definition digest, tool versions and CA bundle hash, checked at startup |
| `~/.local/state/compose-broker/` | Generated runtime files, audit log |
| `~/.cache/compose-broker/sandbox/` | Downloaded sandbox binaries (re-verified on every build) |

`XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_CACHE_HOME` and `XDG_RUNTIME_DIR` are honoured.

## Development

```sh
python3 -m unittest discover                                   # unit tests, fake docker, no daemon
CB_INTEGRATION=1 python3 -m unittest -v tests.test_integration # real Docker, runs examples/demo
```

The integration tests build the sandbox image and start the demo stack. It has a `ticker` service (root build context) that logs a line every second and a warning to stderr every fifth line, and a `web` service (nested build context `web/`) that serves static files. A second test project plants symlinks to a host secret and checks that it never leaks. They drive it over MCP and clean up afterwards. For code layout, see [CLAUDE.md](CLAUDE.md).
