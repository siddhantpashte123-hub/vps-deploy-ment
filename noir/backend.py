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
from .models import LiveStats, PinggySSHSession, Specs, valid_id
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
                client = docker.from_env(timeout=35)
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
        ports: dict[int, int] | None = None,
    ) -> str:
        _report(progress, "container", 60)
        sleep_cmd = (
            ["/bin/sleep", "infinity"]
            if os_key.startswith("alpine")
            else ["/usr/bin/sleep", "infinity"]
        )
        docker_ports = (
            {f"{cport}/tcp": ("0.0.0.0", hport) for cport, hport in ports.items()}
            if ports
            else None
        )
        run_kwargs: dict[str, Any] = {
            "image": self.settings.image_for(os_key),
            "name": instance_id,
            "hostname": instance_id,
            "command": sleep_cmd,
            "detach": True,
            "init": True,
            "user": ROOT_USER,
            "working_dir": VPS_HOME,
            "environment": {
                "HOME": VPS_HOME,
                "USER": "root",
                "LOGNAME": "root",
                "NOIR_VPS_ID": instance_id,
                "NOIR_ROOT_ACCESS": "1",
                "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            },
            "labels": {
                LABEL: "true",
                "noir.id": instance_id,
                "noir.owner": str(owner_id),
                "noir.guild": str(guild_id),
                "noir.runtime": "root-v2",
                "noir.os": os_key,
            },
            "network": self.settings.network,
            "mem_limit": specs.memory_bytes,
            "memswap_limit": specs.memory_bytes,
            "nano_cpus": specs.cpu * 1_000_000_000,
            "pids_limit": 256,
            "cap_drop": ["ALL"],
            "cap_add": ROOT_CAPABILITIES,
            "security_opt": ["no-new-privileges:true"],
            "read_only": False,
            "mounts": [
                Mount(
                    target=VPS_HOME,
                    source=self.mount_path(instance_id),
                    type="bind",
                    read_only=False,
                    propagation="rprivate",
                )
            ],
            "tmpfs": {
                "/tmp": "rw,nosuid,nodev,size=64m,mode=1777",
                "/run": "rw,nosuid,nodev,size=16m,mode=755",
            },
            "ulimits": [Ulimit(name="nofile", soft=1024, hard=1024)],
            "log_config": LogConfig(type="json-file", config={"max-size": "5m", "max-file": "2"}),
            "restart_policy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "auto_remove": False,
            "privileged": False,
        }
        if docker_ports:
            run_kwargs["ports"] = docker_ports
        try:
            container = self.client.containers.run(**run_kwargs)
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
        ports: dict[int, int] | None = None,
    ) -> str:
        _report(progress, "storage", 35)
        self._helper("create", instance_id, specs.disk_gb)
        _report(progress, "storage", 50)
        return self._run_container(
            instance_id, specs, owner_id, guild_id, progress, os_key, ports=ports
        )

    def recreate_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        running: bool = True,
        os_key: str = DEFAULT_OS,
        ports: dict[int, int] | None = None,
    ) -> str:
        """Replace only the container, preserving the persistent home disk/files."""
        container = self.get(instance_id)
        if container is not None:
            try:
                container.remove(force=True, v=True)
            except (DockerException, OSError) as exc:
                raise BackendError("Docker could not replace the old VPS container.") from exc
        self._helper("mount", instance_id)
        container_id = self._run_container(
            instance_id, specs, owner_id, guild_id, os_key=os_key, ports=ports
        )
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

    def restart(self, instance_id: str) -> None:
        container = self.get(instance_id)
        if container is None:
            raise UserError("The container is missing. Reinstall this VPS to recreate it.")
        try:
            container.restart(timeout=15)
        except (DockerException, OSError) as exc:
            raise BackendError("Docker could not restart the VPS.") from exc

    def resize_disk(self, instance_id: str, disk_gb: int) -> None:
        self._helper("resize", instance_id, disk_gb)

    def backup_home(self, instance_id: str) -> int:
        out = self._helper("backup", instance_id)
        return int(out) if out.isdigit() else 0

    def restore_home(self, instance_id: str) -> None:
        self._helper("restore", instance_id)

    def delete_backup(self, instance_id: str) -> None:
        self._helper("delete-backup", instance_id)

    def remove_all(self, instance_id: str) -> None:
        container = self.get(instance_id)
        if container is not None:
            try:
                container.kill()
            except (DockerException, OSError):
                pass
            try:
                container.remove(force=True, v=True)
            except DockerNotFound:
                pass
            except (DockerException, OSError) as exc:
                raise BackendError(f"Docker could not remove container: {str(exc)[:140]}") from exc
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

    def _sync_runtime_script(
        self, instance_id: str, script_name: str, container_dest: str
    ) -> None:
        """Keep an in-container helper script synced with the host runtime copy."""
        import base64
        import hashlib
        from pathlib import Path

        helper_src = Path(__file__).resolve().parent.parent / "runtime" / script_name
        if not helper_src.is_file():
            return
        raw = helper_src.read_bytes()
        expected_sha = hashlib.sha256(raw).hexdigest()
        code, cur_out = self._exec(instance_id, ["sha256sum", container_dest])
        if code == 0 and cur_out.strip().startswith(expected_sha):
            return
        b64 = base64.b64encode(raw).decode("ascii")
        install_cmd = (
            f"printf '%s' '{b64}' | base64 -d > {container_dest} && chmod 0755 {container_dest}"
        )
        shell = "/bin/bash"
        c2, _ = self._exec(instance_id, ["test", "-x", "/bin/bash"])
        if c2 != 0:
            shell = "/bin/sh"
        self._exec(instance_id, [shell, "-c", install_cmd])

    def _ensure_sshx_binary(self, instance_id: str) -> None:
        """Auto-provision /usr/local/bin/sshx if missing inside an older container."""
        code, _ = self._exec(instance_id, ["test", "-x", "/usr/local/bin/sshx"])
        if code == 0:
            return
        from pathlib import Path

        host_bin = Path(__file__).resolve().parent.parent / "runtime" / "sshx"
        if host_bin.is_file():
            container = self.get(instance_id)
            if container is not None:
                import io
                import tarfile

                buf = io.BytesIO()
                data = host_bin.read_bytes()
                with tarfile.open(fileobj=buf, mode="w") as tf:
                    info = tarfile.TarInfo(name="sshx")
                    info.size = len(data)
                    info.mode = 0o755
                    tf.addfile(info, io.BytesIO(data))
                buf.seek(0)
                try:
                    container.put_archive("/usr/local/bin", buf.getvalue())
                    return
                except (DockerException, OSError):
                    pass
        # Fallback: download static musl sshx directly inside the container
        dl_cmd = (
            "ARCH=$(uname -m); "
            "case \"$ARCH\" in aarch64|arm64) A=aarch64;; *) A=x86_64;; esac; "
            "curl -fsSL --max-time 25 "
            "\"https://s3.amazonaws.com/sshx/sshx-${A}-unknown-linux-musl.tar.gz\" "
            "| tar -xz -C /usr/local/bin sshx && chmod 0755 /usr/local/bin/sshx"
        )
        shell = "/bin/bash"
        c2, _ = self._exec(instance_id, ["test", "-x", "/bin/bash"])
        if c2 != 0:
            shell = "/bin/sh"
        self._exec(instance_id, [shell, "-c", dl_cmd])

    def sshx(self, instance_id: str) -> str:
        code, out = self._exec(instance_id, ["/usr/local/bin/noir-sshx", "start", instance_id])
        if code != 0:
            self._sync_runtime_script(instance_id, "noir-sshx", "/usr/local/bin/noir-sshx")
            self._ensure_sshx_binary(instance_id)
            code, out = self._exec(
                instance_id, ["/usr/local/bin/noir-sshx", "start", instance_id]
            )
        if code != 0:
            detail = out.strip()[:180] or "helper returned non-zero status"
            raise UserError(f"SSHX could not start inside the VPS (`{detail}`).")
        # Return only the URL to the caller; never log exec output (it is a capability).
        synced = False
        for idx in range(30):
            code, output = self._exec(instance_id, ["/usr/local/bin/noir-sshx", "url"])
            url = parse_sshx_url(output) if code == 0 else None
            if url:
                return url
            if code == 2 and "EXIT:" in output:
                reason = output.split("EXIT:", 1)[-1].strip()[:180]
                raise UserError(
                    f"SSHX exited before creating a session: `{reason or 'check outbound HTTPS'}`"
                )
            if idx == 2 and not synced:
                synced = True
                self._sync_runtime_script(instance_id, "noir-sshx", "/usr/local/bin/noir-sshx")
                self._ensure_sshx_binary(instance_id)
                self._exec(instance_id, ["/usr/local/bin/noir-sshx", "start", instance_id])
            time.sleep(1)
        raise UserError(
            "SSHX did not return a link within 30 seconds. Check outbound HTTPS and sshx.io status."
        )

    def close_sshx(self, instance_id: str) -> None:
        self._exec(instance_id, ["/usr/local/bin/noir-sshx", "stop"])

    def _ensure_pinggy_helper(self, instance_id: str) -> None:
        """Keep /usr/local/bin/noir-pinggy synced with the host runtime script."""
        from pathlib import Path

        helper_src = Path(__file__).resolve().parent.parent / "runtime" / "noir-pinggy"
        if not helper_src.is_file():
            raise UserError(
                "Pinggy helper missing on host. Re-run `install.sh` with `NOIR_REPAIR=1`."
            )
        self._sync_runtime_script(instance_id, "noir-pinggy", "/usr/local/bin/noir-pinggy")

    def pinggy_ssh(self, instance_id: str) -> PinggySSHSession:
        self._ensure_pinggy_helper(instance_id)
        code, out = self._exec(
            instance_id, ["/usr/local/bin/noir-pinggy", "start", instance_id]
        )
        if code != 0:
            detail = out.strip()[:180] or "sshd or outbound tunnel could not start"
            raise UserError(f"Pinggy SSH could not start inside the VPS ({detail}).")
        last_wait = ""
        for _ in range(30):
            code, output = self._exec(instance_id, ["/usr/local/bin/noir-pinggy", "info"])
            if code == 0:
                lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
                if len(lines) >= 3 and lines[1].isdigit():
                    return PinggySSHSession(
                        host=lines[0], port=int(lines[1]), password=lines[2]
                    )
            elif code == 2 and "EXIT:" in output:
                reason = output.split("EXIT:", 1)[-1].strip()[:180]
                raise UserError(
                    f"Pinggy SSH connection closed early: `{reason or 'check outbound port 443'}`"
                )
            elif "WAIT:" in output:
                last_wait = output.split("WAIT:", 1)[-1].strip()[:160]
            time.sleep(1)
        hint = f" (last output: `{last_wait}`)" if last_wait else ""
        raise UserError(
            "Pinggy did not return an SSH address within 30s. "
            f"Ensure outbound TCP port 443 is open.{hint}"
        )

    def close_pinggy_ssh(self, instance_id: str) -> None:
        self._exec(instance_id, ["/usr/local/bin/noir-pinggy", "stop"])

    def pinggy_http(self, instance_id: str, port: int) -> str:
        self._ensure_pinggy_helper(instance_id)
        code, out = self._exec(
            instance_id, ["/usr/local/bin/noir-pinggy", "http-start", instance_id, str(port)]
        )
        if code != 0:
            detail = out.strip()[:180] or "tunnel process failed to spawn"
            raise UserError(f"Pinggy HTTPS tunnel could not start ({detail}).")
        last_wait = ""
        for _ in range(30):
            code, output = self._exec(
                instance_id, ["/usr/local/bin/noir-pinggy", "http-url", str(port)]
            )
            url = output.strip()
            if code == 0 and url.startswith("https://"):
                return url
            if code == 2 and "EXIT:" in output:
                reason = output.split("EXIT:", 1)[-1].strip()[:180]
                raise UserError(
                    f"Pinggy HTTPS tunnel closed early: `{reason or 'check outbound port 443'}`"
                )
            if "WAIT:" in output:
                last_wait = output.split("WAIT:", 1)[-1].strip()[:160]
            time.sleep(1)
        hint = f" (last output: `{last_wait}`)" if last_wait else ""
        raise UserError(
            "Pinggy did not return an HTTPS URL within 30s. "
            f"Ensure outbound TCP port 443 is open.{hint}"
        )

    def close_pinggy_http(self, instance_id: str, port: int) -> None:
        self._exec(instance_id, ["/usr/local/bin/noir-pinggy", "http-stop", str(port)])

    def set_network_lock(self, instance_id: str, locked: bool) -> None:
        container = self.get(instance_id)
        if container is None:
            raise UserError("The container is missing. Reinstall this VPS to recreate it.")
        try:
            networks = self.client.networks.list(names=[self.settings.network])
            net = next((n for n in networks if n.name == self.settings.network), None)
            if net is None:
                raise BackendError("Managed Docker network was not found.")
            net.reload()
            attached = {c.id for c in net.containers}
            if locked and container.id in attached:
                net.disconnect(container, force=True)
            elif not locked and container.id not in attached:
                net.connect(container)
        except (DockerException, OSError) as exc:
            raise BackendError("Could not update container network state.") from exc

    def exec_command(self, instance_id: str, shell_cmd: str) -> tuple[int, str]:
        container = self.get(instance_id)
        if container is None or container.status != "running":
            raise UserError("Start the VPS before running commands.")
        return self._exec(instance_id, ["timeout", "20", "/bin/bash", "-lc", shell_cmd])

    def inspect_processes(self, instance_id: str) -> str:
        container = self.get(instance_id)
        if container is None or container.status != "running":
            raise UserError("Start the VPS first to inspect active processes.")
        script = (
            "echo '=== PROCESSES ==='; "
            "ps -eo pid,pcpu,pmem,etime,args --sort=-pcpu 2>/dev/null | head -n 12 "
            "|| ps aux | head -n 12; "
            "echo ''; echo '=== LISTENING PORTS ==='; "
            "ss -tulpn 2>/dev/null | head -n 10 || true"
        )
        _, out = self._exec(instance_id, ["/bin/bash", "-lc", script])
        return out.strip()[:1800]

    def write_ssh_key(self, instance_id: str, pubkey: str) -> None:
        container = self.get(instance_id)
        if container is None or container.status != "running":
            return
        import shlex

        quoted = shlex.quote(pubkey)
        script = (
            "mkdir -p /home/noir/.ssh /root/.ssh && "
            "chmod 700 /home/noir/.ssh /root/.ssh && "
            f"printf '%s\\n' {quoted} > /home/noir/.ssh/authorized_keys && "
            f"printf '%s\\n' {quoted} > /root/.ssh/authorized_keys && "
            "chmod 600 /home/noir/.ssh/authorized_keys /root/.ssh/authorized_keys"
        )
        self._exec(instance_id, ["/bin/bash", "-lc", script])

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
