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
from .models import Specs, valid_id

log = logging.getLogger(__name__)
LABEL = "io.github.fier.managed"
URL_RE = re.compile(r"https://sshx\.io/s/[A-Za-z0-9_-]+#[A-Za-z0-9_-]+")

# Root is confined to the VPS container. These are Docker's default unprivileged
# container capabilities; no SYS_ADMIN, privileged mode, host PID/network, Docker
# socket, or host filesystem mount is granted.
ROOT_USER = "0:0"
VPS_HOME = "/home/fier"
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
            self.client.images.get(self.settings.image)
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
                if network.attrs.get("Labels", {}).get(LABEL) != "true":
                    raise BackendError("FIER_NETWORK exists but is not managed by FIER.")
            self._helper("check", "fier-000000000000")
            mode = self._helper("probe", "fier-000000000000")
            if mode not in {"loop", "directory"}:
                raise BackendError("Disk helper returned an unknown storage mode.")
            self.storage_mode = mode
            info["fier_storage_mode"] = mode
            return info
        except ImageNotFound as exc:
            raise BackendError("VPS image is missing. Re-run install.sh to build it.") from exc
        except (DockerException, OSError) as exc:
            raise BackendError(
                "Docker is unavailable. Check the Docker service and the fier user's docker group."
            ) from exc

    def mount_path(self, instance_id: str) -> str:
        if not valid_id(instance_id):
            raise BackendError("Invalid VPS identifier.")
        # Must match the fixed STATE_ROOT in the root-owned volume helper.
        return f"/var/lib/fier/mounts/{instance_id}"

    def _run_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        progress: Progress | None = None,
    ) -> str:
        _report(progress, "container", 60)
        try:
            container = self.client.containers.run(
                image=self.settings.image,
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
                    "FIER_VPS_ID": instance_id,
                    "FIER_ROOT_ACCESS": "1",
                    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
                },
                labels={
                    LABEL: "true",
                    "fier.id": instance_id,
                    "fier.owner": str(owner_id),
                    "fier.guild": str(guild_id),
                    "fier.runtime": "root-v2",
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
    ) -> str:
        _report(progress, "storage", 35)
        self._helper("create", instance_id, specs.disk_gb)
        _report(progress, "storage", 50)
        return self._run_container(instance_id, specs, owner_id, guild_id, progress)

    def recreate_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        running: bool = True,
    ) -> str:
        """Replace only the container, preserving the persistent home disk/files."""
        container = self.get(instance_id)
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except (DockerException, OSError) as exc:
                raise BackendError("Docker could not replace the old VPS container.") from exc
        self._helper("mount", instance_id)
        container_id = self._run_container(instance_id, specs, owner_id, guild_id)
        if not running:
            self.stop(instance_id)
        return container_id

    def get(self, instance_id: str) -> Any:
        if not valid_id(instance_id):
            raise BackendError("Invalid VPS identifier.")
        try:
            container = self.client.containers.get(instance_id)
            container.reload()
            if (
                container.labels.get(LABEL) != "true"
                or container.labels.get("fier.id") != instance_id
            ):
                raise BackendError("Refusing to control a container not managed by FIER.")
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
        code, _ = self._exec(instance_id, ["/usr/local/bin/fier-sshx", "start", instance_id])
        if code != 0:
            raise BackendError("SSHX could not start inside the VPS.")
        # Return only the URL to the caller; never log exec output (it is a capability).
        for _ in range(30):
            code, output = self._exec(instance_id, ["/usr/local/bin/fier-sshx", "url"])
            url = parse_sshx_url(output) if code == 0 else None
            if url:
                return url
            time.sleep(1)
        raise BackendError(
            "SSHX did not return a link within 30 seconds. Check outbound HTTPS and sshx.io status."
        )

    def close_sshx(self, instance_id: str) -> None:
        self._exec(instance_id, ["/usr/local/bin/fier-sshx", "stop"])

    def reconcile(self, known_ids: set[str]) -> dict[str, str]:
        try:
            containers = self.client.containers.list(all=True, filters={"label": LABEL})
            result = {}
            for container in containers:
                instance_id = container.labels.get("fier.id", "")
                if valid_id(instance_id) and instance_id in known_ids:
                    container.reload()
                    result[instance_id] = (
                        "running" if container.attrs.get("State", {}).get("Running") else "stopped"
                    )
            return result
        except (DockerException, OSError) as exc:
            raise BackendError("Could not reconcile Docker state.") from exc
