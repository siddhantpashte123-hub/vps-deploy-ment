from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import docker
from docker.errors import DockerException, ImageNotFound
from docker.errors import NotFound as DockerNotFound
from docker.types import LogConfig, Mount, Ulimit

from .config import Settings
from .errors import BackendError, UserError
from .models import LiveStats, Specs, valid_id
from .os_catalog import DEFAULT_OS, os_label

log = logging.getLogger(__name__)
LABEL = "io.github.noir.managed"
# Pre-NOIR installs created containers/networks with the legacy label; keep
# recognizing them so existing VPSes survive the rename and upgrade cleanly.
LEGACY_LABEL = "io.github.fier.managed"
URL_RE = re.compile(r"https://sshx\.io/s/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+")

# Root is confined to the VPS container. These are Docker's default unprivileged
# container capabilities; no SYS_ADMIN, privileged mode, host PID/network, Docker
# socket, or host filesystem mount is granted.
ROOT_USER = "0:0"
VPS_HOME = "/home/noir"
ROOT_CAPABILITIES = [
    "AUDIT_WRITE",
    "CHOWN",
    "DAC_OVERRIDE",
    "FOWNER",
    "FSETID",
    "KILL",
    "MKNOD",
    "NET_BIND_SERVICE",
    "NET_RAW",
    "SETFCAP",
    "SETGID",
    "SETUID",
    "SETPCAP",
    "SYS_CHROOT",
]
Progress = Callable[[str, int], None]


def parse_sshx_url(raw: str) -> str | None:
    raw = raw.strip()
    if len(raw) > 1024 or any(c.isspace() for c in raw):
        return None
    parsed = urlsplit(raw)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "sshx.io"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.query
        or not re.fullmatch(r"/s/[A-Za-z0-9_-]+", parsed.path)
        or not re.fullmatch(r"[A-Za-z0-9_-]+", parsed.fragment)
    ):
        return None
    return raw if URL_RE.fullmatch(raw) else None


def _report(progress: Progress | None, stage: str, percent: int) -> None:
    if progress is not None:
        progress(stage, percent)


def _parse_uptime(started_at: Any) -> int:
    from datetime import datetime, timezone

    if not isinstance(started_at, str) or started_at.startswith("0001-"):
        return 0
    cleaned = started_at.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    # Docker emits up to 9 fractional digits; Python 3.10 handles 6 reliably.
    cleaned = re.sub(r"\.(\d{6})\d+", r".\1", cleaned)
    try:
        started = datetime.fromisoformat(cleaned)
    except ValueError:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _cpu_percent(raw: dict[str, Any]) -> float:
    cpu = raw.get("cpu_stats") or {}
    pre = raw.get("precpu_stats") or {}
    cpu_delta = int((cpu.get("cpu_usage") or {}).get("total_usage") or 0) - int(
        (pre.get("cpu_usage") or {}).get("total_usage") or 0
    )
    sys_delta = int(cpu.get("system_cpu_usage") or 0) - int(pre.get("system_cpu_usage") or 0)
    online = int(
        cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1
    )
    if cpu_delta <= 0 or sys_delta <= 0:
        return 0.0
    return max(0.0, (cpu_delta / sys_delta) * online * 100.0)


class DockerBackend:
    """Blocking Docker SDK calls are made through asyncio.to_thread by the manager."""

    def __init__(self, settings: Settings, client: Any = None):
        self.settings = settings
        self.storage_mode = "unknown"
        if client is None:
            try:
                client = docker.from_env(timeout=120)
            except DockerException as exc:
                raise BackendError(
                    "Cannot connect to Docker. Start Docker and add the bot user to docker."
                ) from exc
        self.client = client

    def close(self) -> None:
        self.client.close()

    def _helper(self, action: str, instance_id: str, size: int | None = None) -> str:
        if not valid_id(instance_id):
            raise BackendError("Refusing an invalid VPS identifier.")
        cmd = ["sudo", "-n", str(self.settings.volume_helper), action, instance_id]
        if size is not None:
            if type(size) is not int or not 1 <= size <= 256:
                raise BackendError("Refusing an invalid disk size.")
            cmd.append(str(size))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError("The disk helper failed or timed out.") from exc
        if result.returncode:
            # The helper emits only fixed, non-secret diagnostics.
            detail = result.stderr.strip()[:400] or result.stdout.strip()[:400]
            raise BackendError(f"Disk helper: {detail}")
        return result.stdout.strip()

    def preflight(self) -> dict[str, Any]:
        try:
            self.client.ping()
            info = self.client.info()
            for os_key, image in self.settings.images.items():
                try:
                    self.client.images.get(image)
                except ImageNotFound as exc:
                    raise BackendError(
                        f"The {os_label(os_key)} image `{image}` is missing. "
                        "Re-run install.sh to build it."
                    ) from exc
            networks = self.client.networks.list(names=[self.settings.network])
            network = next((n for n in networks if n.name == self.settings.network), None)
            if network is None:
                network = self.client.networks.create(
                    self.settings.network,
                    driver="bridge",
                    labels={LABEL: "true"},
                    options={
                        "com.docker.network.bridge.enable_icc": "false",
                        "com.docker.network.bridge.enable_ip_masquerade": "true",
                    },
                )
            else:
                network.reload()
                net_labels = network.attrs.get("Labels", {})
                if net_labels.get(LABEL) != "true" and net_labels.get(LEGACY_LABEL) != "true":
                    raise BackendError("NOIR_NETWORK exists but is not managed by NOIR.")
            self._helper("check", "noir-000000000000")
            mode = self._helper("probe", "noir-000000000000")
            if mode not in {"loop", "directory"}:
                raise BackendError("Disk helper returned an unknown storage mode.")
            self.storage_mode = mode
            info["noir_storage_mode"] = mode
            return info
        except ImageNotFound as exc:
            raise BackendError("VPS image is missing. Re-run install.sh to build it.") from exc
        except (DockerException, OSError) as exc:
            raise BackendError(
                "Docker is unavailable. Check the Docker service and the noir user's docker group."
            ) from exc

    def mount_path(self, instance_id: str) -> str:
        if not valid_id(instance_id):
            raise BackendError("Invalid VPS identifier.")
        # Must match the fixed STATE_ROOT in the root-owned volume helper.
        return f"/var/lib/noir/mounts/{instance_id}"

    def _run_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        progress: Progress | None = None,
        os_key: str = DEFAULT_OS,
    ) -> str:
        _report(progress, "container", 60)
        try:
            container = self.client.containers.run(
                image=self.settings.image_for(os_key),
                name=instance_id,
                hostname=instance_id,
                command=["/usr/bin/sleep", "infinity"],
                detach=True,
                init=True,
                user=ROOT_USER,
                working_dir=VPS_HOME,
                environment={
                    "HOME": VPS_HOME,
                    "USER": "root",
                    "LOGNAME": "root",
                    "NOIR_VPS_ID": instance_id,
                    "NOIR_ROOT_ACCESS": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                },
                labels={
                    LABEL: "true",
                    "noir.id": instance_id,
                    "noir.owner": str(owner_id),
                    "noir.guild": str(guild_id),
                    "noir.runtime": "root-v2",
                    "noir.os": os_key,
                },
                network=self.settings.network,
                mem_limit=specs.memory_bytes,
                memswap_limit=specs.memory_bytes,
                nano_cpus=specs.cpu * 1_000_000_000,
                pids_limit=256,
                cap_drop=["ALL"],
                cap_add=ROOT_CAPABILITIES,
                security_opt=["no-new-privileges:true"],
                read_only=False,
                mounts=[
                    Mount(
                        target=VPS_HOME,
                        source=self.mount_path(instance_id),
                        type="bind",
                        read_only=False,
                        propagation="rprivate",
                    )
                ],
                tmpfs={
                    "/tmp": "rw,nosuid,nodev,size=64m,mode=1777",
                    "/run": "rw,nosuid,nodev,size=16m,mode=755",
                },
                ulimits=[Ulimit(name="nofile", soft=1024, hard=1024)],
                log_config=LogConfig(type="json-file", config={"max-size": "5m", "max-file": "2"}),
                restart_policy={"Name": "unless-stopped", "MaximumRetryCount": 0},
                auto_remove=False,
                privileged=False,
            )
            _report(progress, "boot", 85)
            container.reload()
            if not container.attrs.get("State", {}).get("Running"):
                raise BackendError("The VPS container did not stay running.")
            _report(progress, "ready", 95)
            return container.id
        except (DockerException, OSError) as exc:
            raise BackendError("Docker could not create the VPS. Check host resources.") from exc

    def create(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        progress: Progress | None = None,
        os_key: str = DEFAULT_OS,
    ) -> str:
        _report(progress, "storage", 35)
        self._helper("create", instance_id, specs.disk_gb)
        _report(progress, "storage", 50)
        return self._run_container(instance_id, specs, owner_id, guild_id, progress, os_key)

    def recreate_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        running: bool = True,
        os_key: str = DEFAULT_OS,
    ) -> str:
        """Replace only the container, preserving the persistent home disk/files."""
        container = self.get(instance_id)
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except (DockerException, OSError) as exc:
                raise BackendError("Docker could not replace the old VPS container.") from exc
        self._helper("mount", instance_id)
        container_id = self._run_container(instance_id, specs, owner_id, guild_id, os_key=os_key)
        if not running:
            self.stop(instance_id)
        return container_id

    def get(self, instance_id: str) -> Any:
        if not valid_id(instance_id):
            raise BackendError("Invalid VPS identifier.")
        try:
            container = self.client.containers.get(instance_id)
            container.reload()
            labels = container.labels
            managed = labels.get(LABEL) == "true" or labels.get(LEGACY_LABEL) == "true"
            label_id = labels.get("noir.id") or labels.get("fier.id")
            if not managed or label_id != instance_id:
                raise BackendError("Refusing to control a container not managed by NOIR.")
            return container
        except DockerNotFound:
            return None
        except (DockerException, OSError) as exc:
            raise BackendError("Docker could not inspect the VPS.") from exc

    def status(self, instance_id: str) -> str:
        container = self.get(instance_id)
        if container is None:
            return "error"
        state = container.attrs.get("State", {})
        if state.get("Running"):
            return "running"
        if state.get("Status") in {"exited", "created"}:
            return "stopped"
        return "error"

    def stats(self, instance_id: str) -> LiveStats:
        container = self.get(instance_id)
        if container is None:
            return LiveStats(online=False)
        state = container.attrs.get("State", {})
        if not state.get("Running"):
            return LiveStats(online=False)
        uptime_seconds = _parse_uptime(state.get("StartedAt"))
        cpu_percent = 0.0
        ram_used_bytes = 0
        try:
            raw = container.stats(stream=False)
            if isinstance(raw, dict):
                cpu_percent = _cpu_percent(raw)
                mem = raw.get("memory_stats") or {}
                cache = int((mem.get("stats") or {}).get("inactive_file") or 0)
                ram_used_bytes = max(0, int(mem.get("usage") or 0) - cache)
        except Exception:
            log.debug("Could not read Docker stats for %s", instance_id, exc_info=True)
        disk_used_bytes = 0
        try:
            code, out = self._exec(instance_id, ["df", "-B1", "--output=used", VPS_HOME])
            if code == 0:
                lines = [ln.strip() for ln in out.splitlines() if ln.strip().isdigit()]
                if lines:
                    disk_used_bytes = max(0, int(lines[-1]))
        except Exception:
            log.debug("Could not read disk usage for %s", instance_id, exc_info=True)
        return LiveStats(
            online=True,
            uptime_seconds=uptime_seconds,
            cpu_percent=cpu_percent,
            ram_used_bytes=ram_used_bytes,
            disk_used_bytes=disk_used_bytes,
        )

    def start(self, instance_id: str) -> None:
        container = self.get(instance_id)
        if container is None:
            raise UserError("The container is missing. Reinstall this VPS to recreate it.")
        try:
            if container.status != "running":
                container.start()
        except (DockerException, OSError) as exc:
            raise BackendError("Docker could not start the VPS.") from exc

    def stop(self, instance_id: str) -> None:
        container = self.get(instance_id)
        if container is None:
            return
        try:
            container.stop(timeout=15)
        except (DockerException, OSError) as exc:
            raise BackendError("Docker could not stop the VPS.") from exc

    def remove_all(self, instance_id: str) -> None:
        container = self.get(instance_id)
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except (DockerException, OSError) as exc:
                raise BackendError("Docker could not remove the container.") from exc
        self._helper("delete", instance_id)

    def recreate_disk(self, instance_id: str, disk_gb: int) -> None:
        self._helper("delete", instance_id)
        self._helper("create", instance_id, disk_gb)

    def _exec(self, instance_id: str, command: list[str]) -> tuple[int, str]:
        container = self.get(instance_id)
        if container is None or container.status != "running":
            raise UserError("Start the VPS before opening SSHX.")
        try:
            result = container.exec_run(command, user=ROOT_USER, workdir=VPS_HOME, demux=False)
            output = result.output or b""
            return int(result.exit_code), output.decode("utf-8", "replace")[:8192]
        except (DockerException, OSError) as exc:
            raise BackendError("Could not run the terminal helper inside the VPS.") from exc

    def sshx(self, instance_id: str) -> str:
        code, _ = self._exec(instance_id, ["/usr/local/bin/noir-sshx", "start", instance_id])
        if code != 0:
            raise BackendError("SSHX could not start inside the VPS.")
        # Return only the URL to the caller; never log exec output (it is a capability).
        for _ in range(30):
            code, output = self._exec(instance_id, ["/usr/local/bin/noir-sshx", "url"])
            url = parse_sshx_url(output) if code == 0 else None
            if url:
                return url
            time.sleep(1)
        raise BackendError(
            "SSHX did not return a link within 30 seconds. Check outbound HTTPS and sshx.io status."
        )

    def close_sshx(self, instance_id: str) -> None:
        self._exec(instance_id, ["/usr/local/bin/noir-sshx", "stop"])

    def reconcile(self, known_ids: set[str]) -> dict[str, str]:
        try:
            seen: dict[str, Any] = {}
            for label in (LABEL, LEGACY_LABEL):
                for container in self.client.containers.list(all=True, filters={"label": label}):
                    seen[container.id] = container
            result = {}
            for container in seen.values():
                instance_id = container.labels.get("noir.id") or container.labels.get("fier.id", "")
                if valid_id(instance_id) and instance_id in known_ids:
                    container.reload()
                    result[instance_id] = (
                        "running" if container.attrs.get("State", {}).get("Running") else "stopped"
                    )
            return result
        except (DockerException, OSError) as exc:
            raise BackendError("Could not reconcile Docker state.") from exc
