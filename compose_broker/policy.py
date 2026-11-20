"""Policy check of a resolved compose config (SPEC 5.3). An allowlist."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable

from .registry import Project
from .runtime import context_rel, dockerfile_rel
from .util import BrokerError

SERVICE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")


@dataclass
class Finding:
    level: str  # "error" | "warning"
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():7} {self.where}: {self.message}"


TOP_LEVEL_KEYS = {"name", "services", "networks", "volumes", "secrets", "configs"}

SERVICE_KEYS = {
    "annotations", "attach", "build", "cap_drop", "command", "configs",
    "cpu_count", "cpu_percent", "cpu_period", "cpu_quota", "cpu_shares", "cpus",
    "cpuset", "depends_on", "deploy", "develop", "dns", "dns_opt", "dns_search",
    "domainname", "entrypoint", "env_file", "environment", "expose", "extra_hosts",
    "group_add", "healthcheck", "hostname", "image", "init", "ipc", "labels",
    "links", "logging", "mac_address", "mem_limit", "mem_reservation",
    "mem_swappiness", "memswap_limit", "network_mode", "networks", "oom_score_adj",
    "pid", "pids_limit", "platform", "ports", "post_start", "pre_stop", "profiles",
    "pull_policy", "read_only", "restart", "scale", "secrets", "security_opt",
    "shm_size", "stdin_open", "stop_grace_period", "stop_signal", "sysctls",
    "tmpfs", "tty", "ulimits", "user", "volumes", "working_dir",
}

FORBIDDEN_SERVICE_KEYS = {
    "privileged": "grants full access to host devices and the kernel",
    "cap_add": "adds kernel capabilities",
    "devices": "exposes host devices",
    "device_cgroup_rules": "exposes host devices",
    "volumes_from": "can mount volumes of containers outside the project",
    "userns_mode": "can disable user-namespace isolation",
    "uts": "can share the host UTS namespace",
    "cgroup": "can share the host cgroup namespace",
    "cgroup_parent": "places the container in an arbitrary host cgroup",
    "runtime": "selects an alternative container runtime",
    "isolation": "changes the isolation technology",
    "storage_opt": "changes storage driver options",
    "credential_spec": "accesses host credentials",
    "gpus": "exposes host GPUs",
    "use_api_socket": "mounts the Docker API socket and credentials into the container",
    "provider": "runs an external binary on the host",
    "container_name": "global names can collide with containers outside the project",
    "external_links": "links to containers outside the project",
    "oom_kill_disable": "can destabilise the host",
    "label_file": "reads host files at load time",
}

BUILD_KEYS = {"context", "dockerfile", "dockerfile_inline", "args", "target", "labels",
              "shm_size", "extra_hosts", "platforms", "pull", "no_cache", "network",
              "ulimits"}
FORBIDDEN_BUILD_KEYS = {
    "additional_contexts": "can read host paths outside the project",
    "secrets": "reads host files or environment into the build",
    "ssh": "forwards host SSH agent or keys into the build",
    "cache_from": "can read host paths (type=local)",
    "cache_to": "can write host paths (type=local)",
    "entitlements": "grants extra build privileges",
    "privileged": "runs the build privileged",
    "isolation": "changes the isolation technology",
    "tags": "can overwrite images used by other projects",
}
DEPLOY_KEYS = {"resources", "restart_policy", "replicas", "labels", "mode"}
HOOK_KEYS = {"command", "user", "working_dir", "environment"}
VOLUME_KEYS = {"name", "driver", "labels", "external"}
NETWORK_KEYS = {"name", "driver", "labels", "internal", "ipam", "enable_ipv6",
                "attachable", "external"}
SECRET_KEYS = {"name", "environment", "external"}
CONFIG_KEYS = {"name", "content", "environment", "external"}


def check_policy(cfg: Any, p: Project) -> list[Finding]:
    findings: list[Finding] = []
    err = lambda where, msg: findings.append(Finding("error", where, msg))
    warn = lambda where, msg: findings.append(Finding("warning", where, msg))

    if not isinstance(cfg, dict):
        err("<root>", "resolved config is not a mapping")
        return findings
    for key in cfg:
        if not key.startswith("x-") and key not in TOP_LEVEL_KEYS:
            err(key, "top-level key is not in the policy allowlist")
    if cfg.get("name", p.compose_project) != p.compose_project:
        err("name", f"project name must be {p.compose_project!r}")

    services = cfg.get("services")
    if not isinstance(services, dict) or not services:
        err("services", "no services defined")
        services = {}
    volumes = cfg.get("volumes") if isinstance(cfg.get("volumes"), dict) else {}
    for sname, svc in services.items():
        _check_service(str(sname), svc, p, set(volumes), err, warn)

    _check_named(cfg.get("volumes"), "volumes", VOLUME_KEYS, {None, "local"}, p, err)
    _check_named(cfg.get("networks"), "networks", NETWORK_KEYS, {None, "bridge"}, p, err)
    for kind, allowed in (("secrets", SECRET_KEYS), ("configs", CONFIG_KEYS)):
        section = cfg.get(kind) or {}
        if not isinstance(section, dict):
            err(kind, "not a mapping")
            continue
        for rname, spec in section.items():
            where = f"{kind}.{rname}"
            if not isinstance(spec, dict):
                err(where, "not a mapping")
                continue
            for k, v in spec.items():
                if k == "file":
                    err(f"{where}.file", "file sources are bind-mounted from the host; "
                        "use 'environment' (or 'content' for configs) instead")
                elif k == "external":
                    if v:
                        err(f"{where}.external", f"external {kind} are not allowed")
                elif k not in allowed and not k.startswith("x-"):
                    err(f"{where}.{k}", "key is not in the policy allowlist")
    return findings


def _check_labels(labels: Any, where: str, err: Callable) -> None:
    if not labels:
        return
    keys = labels.keys() if isinstance(labels, dict) else \
        [str(item).split("=", 1)[0] for item in labels]
    for k in keys:
        if str(k).startswith("com.docker.compose."):
            err(where, f"label {k!r} is reserved for compose bookkeeping")
        elif str(k).startswith("com.compose-broker."):
            err(where, f"label {k!r} is reserved for the broker's sandbox containers")


def _check_service(sname: str, svc: Any, p: Project, volume_names: set,
                   err: Callable, warn: Callable) -> None:
    base = f"services.{sname}"
    at = lambda key: f"{base}.{key}"
    if not SERVICE_RE.match(sname):
        err(base, "service name contains unexpected characters")
    if not isinstance(svc, dict):
        err(base, "not a mapping")
        return

    for key in svc:
        if key.startswith("x-"):
            continue
        if key in FORBIDDEN_SERVICE_KEYS:
            err(at(key), f"forbidden: {FORBIDDEN_SERVICE_KEYS[key]}")
        elif key not in SERVICE_KEYS:
            err(at(key), "key is not in the policy allowlist")

    # POL-S3 namespaces
    ipc = svc.get("ipc")
    if ipc is not None and ipc not in ("private", "shareable", "none") \
            and not str(ipc).startswith("service:"):
        err(at("ipc"), f"{ipc!r} not allowed (private, shareable, none, service:<name>)")
    pid = svc.get("pid")
    if pid is not None and not str(pid).startswith("service:"):
        err(at("pid"), f"{pid!r} not allowed (only service:<name>)")
    nm = svc.get("network_mode")
    if nm is not None and nm not in ("none", "bridge", "default") \
            and not str(nm).startswith("service:"):
        err(at("network_mode"), f"{nm!r} not allowed (none, bridge, default, service:<name>)")

    # POL-S4
    for i, opt in enumerate(svc.get("security_opt") or []):
        if str(opt).replace("=", ":") not in ("no-new-privileges", "no-new-privileges:true"):
            err(f"{base}.security_opt[{i}]", f"{opt!r} not allowed (only no-new-privileges)")

    # POL-S5, S6
    _check_labels(svc.get("labels"), at("labels"), err)
    logging_cfg = svc.get("logging")
    if isinstance(logging_cfg, dict) and logging_cfg.get("driver") not in (None, "json-file", "local"):
        err(at("logging.driver"), "only json-file and local are allowed "
            "(others ship data off the host and break stack_logs)")

    # POL-S7
    deploy = svc.get("deploy")
    if deploy is not None:
        if not isinstance(deploy, dict):
            err(at("deploy"), "not a mapping")
        else:
            for k in deploy:
                if k not in DEPLOY_KEYS:
                    err(at(f"deploy.{k}"), "key is not in the policy allowlist")
            if deploy.get("mode") not in (None, "replicated"):
                err(at("deploy.mode"), "only 'replicated' is allowed")
            res = deploy.get("resources")
            resv = res.get("reservations") if isinstance(res, dict) else None
            if isinstance(resv, dict) and (resv.get("devices") or resv.get("generic_resources")):
                err(at("deploy.resources.reservations"), "device reservations (GPUs etc.) are not allowed")

    # POL-S8
    for hook in ("post_start", "pre_stop"):
        for i, h in enumerate(svc.get(hook) or []):
            where = f"{base}.{hook}[{i}]"
            if not isinstance(h, dict):
                err(where, "not a mapping")
                continue
            for k in h:
                if k not in HOOK_KEYS:
                    err(f"{where}.{k}", "not allowed in hooks")

    # POL-S9 mounts
    writable_root = False
    for i, mount in enumerate(svc.get("volumes") or []):
        where = f"{base}.volumes[{i}]"
        if not isinstance(mount, dict):
            err(where, "expected normalized long syntax")
            continue
        mtype = mount.get("type")
        if mtype == "bind":
            src = mount.get("source")
            if not isinstance(src, str) or os.path.normpath(src) != p.dir:
                err(where, f"bind source {src!r} not allowed; the only permitted bind source "
                    f"is the project root {p.dir!r}. Mount it once (e.g. '.:/workspace') "
                    "and point services at sub-paths inside the container")
            prop = (mount.get("bind") or {}).get("propagation")
            if prop not in (None, "private", "rprivate"):
                err(where, f"bind propagation {prop!r} not allowed")
            writable_root = writable_root or not mount.get("read_only")
        elif mtype == "volume":
            src = mount.get("source")
            if src is not None and src not in volume_names:
                err(where, f"volume {src!r} is not declared in top-level volumes")
        elif mtype == "tmpfs":
            pass
        else:
            err(where, f"mount type {mtype!r} not allowed")

    # POL-S13: root writing into the project leaves root-owned (possibly setuid) files on the host
    if writable_root:
        users = [("user", svc.get("user"))] + [
            (f"{hook}[{i}].user", h.get("user") if "user" in h else svc.get("user"))
            for hook in ("post_start", "pre_stop")
            for i, h in enumerate(svc.get(hook) or []) if isinstance(h, dict)]
        for key, user in users:
            if _is_root_user(user):
                warn(at(key), "runs as root (or the image default) with the project root mounted "
                     "writable: files it creates on the host are owned by root, and it could "
                     "plant a setuid-root binary there. Set user: to your host UID:GID, mount "
                     "the project read-only, or ignore this on rootless Docker")
                break

    # POL-S10
    for i, port in enumerate(svc.get("ports") or []):
        if isinstance(port, dict) and port.get("published") not in (None, "", "0", 0):
            if port.get("host_ip") in (None, "", "0.0.0.0", "::"):
                warn(f"{base}.ports[{i}]", f"port {port.get('published')} is published on all "
                     "host interfaces (reachable from your network); consider host_ip")

    # POL-S11, S12
    build = svc.get("build")
    if build is not None:
        if not isinstance(build, dict):
            err(at("build"), "not a mapping")
        else:
            for k in build:
                if k in FORBIDDEN_BUILD_KEYS:
                    err(at(f"build.{k}"), f"forbidden: {FORBIDDEN_BUILD_KEYS[k]}")
                elif k not in BUILD_KEYS and not k.startswith("x-"):
                    err(at(f"build.{k}"), "key is not in the policy allowlist")
            ctx = None
            try:
                ctx = context_rel(p, build)
            except BrokerError:
                err(at("build.context"), f"build context {build.get('context')!r} not allowed; "
                    f"it must be a local directory inside the project {p.dir!r}")
            if build.get("dockerfile") is not None and ctx is not None:
                try:
                    dockerfile_rel(p, str(build["dockerfile"]), ctx)
                except BrokerError as e:
                    err(at("build.dockerfile"), str(e))
            if build.get("network") not in (None, "default", "none"):
                err(at("build.network"), "only default or none are allowed")
            _check_labels(build.get("labels"), at("build.labels"), err)
        image = svc.get("image")
        if image is not None and not str(image).startswith(f"{p.compose_project}-"):
            err(at("image"), f"built images must be named '{p.compose_project}-...' (or omit "
                "image) so they cannot replace images used elsewhere")


def _is_root_user(user: Any) -> bool:
    """Unset (image default, usually root) or explicitly root. Names other than
    root are taken as non-root; the broker cannot resolve them."""
    if user is None or str(user).strip() == "":
        return True
    return str(user).split(":", 1)[0].strip() in ("0", "root")


def _check_named(section: Any, kind: str, allowed: set, drivers: set,
                 p: Project, err: Callable) -> None:
    if section is None:
        return
    if not isinstance(section, dict):
        err(kind, "not a mapping")
        return
    prefix = f"{p.compose_project}_"
    for rname, spec in section.items():
        where = f"{kind}.{rname}"
        spec = spec or {}
        if not isinstance(spec, dict):
            err(where, "not a mapping")
            continue
        for k, v in spec.items():
            if k.startswith("x-"):
                continue
            if k == "external":
                if v:
                    err(f"{where}.external", f"external {kind} are not allowed "
                        "(they may belong to other projects)")
            elif k == "driver_opts":
                err(f"{where}.driver_opts", "forbidden: can bind arbitrary host paths")
            elif k not in allowed:
                err(f"{where}.{k}", "key is not in the policy allowlist")
        if spec.get("driver") not in drivers:
            err(f"{where}.driver", f"driver {spec.get('driver')!r} not allowed")
        name = spec.get("name")
        if name is not None and not str(name).startswith(prefix):
            err(f"{where}.name", f"must start with {prefix!r} so it cannot refer to "
                "another project's resources")
