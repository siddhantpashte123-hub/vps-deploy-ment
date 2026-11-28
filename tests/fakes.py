from __future__ import annotations

from noir.models import LiveStats, Specs
from noir.os_catalog import DEFAULT_OS


class FakeBackend:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.fail_create = False
        self.fail_cleanup = False
        self.events: list[str] = []

    def create(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        progress=None,
        os_key: str = DEFAULT_OS,
        ports: dict[int, int] | None = None,
    ) -> str:
        self.events.append("create:" + instance_id)
        if progress:
            progress("storage", 35)
        if self.fail_create:
            raise RuntimeError("simulated docker failure")
        self.items[instance_id] = {
            "status": "running",
            "specs": specs,
            "owner": owner_id,
            "guild": guild_id,
            "os": os_key,
            "ports": dict(ports or {}),
            "ssh_key": None,
            "templates": [],
        }
        if progress:
            progress("container", 60)
            progress("boot", 85)
            progress("ready", 95)
        return "container-" + instance_id

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
        self.events.append("recreate-container:" + instance_id)
        prev = self.items.get(instance_id, {})
        self.items[instance_id] = {
            "status": "running" if running else "stopped",
            "specs": specs,
            "owner": owner_id,
            "guild": guild_id,
            "os": os_key,
            "ports": dict(ports) if ports is not None else dict(prev.get("ports") or {}),
            "ssh_key": prev.get("ssh_key"),
            "templates": list(prev.get("templates") or []),
            "backed_up": prev.get("backed_up", False),
        }
        return "container-" + instance_id

    def status(self, instance_id: str) -> str:
        return self.items.get(instance_id, {}).get("status", "error")

    def stats(self, instance_id: str) -> LiveStats:
        item = self.items.get(instance_id)
        if not item or item.get("status") != "running":
            return LiveStats(online=False)
        return LiveStats(
            online=True,
            uptime_seconds=item.get("uptime", 125),
            cpu_percent=item.get("cpu_percent", 2.4),
            ram_used_bytes=item.get("ram_used_bytes", 128 * 1024**2),
            disk_used_bytes=item.get("disk_used_bytes", 320 * 1024**2),
        )

    def start(self, instance_id: str) -> None:
        self.items[instance_id]["status"] = "running"
        self.events.append("start:" + instance_id)

    def stop(self, instance_id: str) -> None:
        self.items[instance_id]["status"] = "stopped"
        self.events.append("stop:" + instance_id)

    def restart(self, instance_id: str) -> None:
        self.items[instance_id]["status"] = "running"
        self.events.append("restart:" + instance_id)

    def resize_disk(self, instance_id: str, disk_gb: int) -> None:
        self.events.append(f"resize-disk:{instance_id}:{disk_gb}")

    def backup_home(self, instance_id: str) -> int:
        self.items[instance_id]["backed_up"] = True
        self.events.append("backup:" + instance_id)
        return 4096

    def restore_home(self, instance_id: str) -> None:
        if not self.items.get(instance_id, {}).get("backed_up"):
            from noir.errors import BackendError

            raise BackendError("Disk helper: No saved backup exists for this VPS yet.")
        self.events.append("restore:" + instance_id)

    def remove_all(self, instance_id: str) -> None:
        self.events.append("remove:" + instance_id)
        if self.fail_cleanup:
            raise RuntimeError("simulated cleanup failure")
        self.items.pop(instance_id, None)

    def recreate_disk(self, instance_id: str, disk_gb: int) -> None:
        self.events.append("wipe:" + instance_id)

    def sshx(self, instance_id: str) -> str:
        return "https://sshx.io/s/test-session#test-secret"

    def close_sshx(self, instance_id: str) -> None:
        self.events.append("close-sshx:" + instance_id)

    def exec_command(self, instance_id: str, shell_cmd: str) -> tuple[int, str]:
        if self.items.get(instance_id, {}).get("status") != "running":
            from noir.errors import UserError

            raise UserError("Start the VPS before running commands.")
        self.events.append(f"exec:{instance_id}:{shell_cmd[:40]}")
        return 0, f"executed: {shell_cmd[:80]}"

    def inspect_processes(self, instance_id: str) -> str:
        if self.items.get(instance_id, {}).get("status") != "running":
            from noir.errors import UserError

            raise UserError("Start the VPS first to inspect active processes.")
        self.events.append("logs:" + instance_id)
        return "=== PROCESSES ===\nPID %CPU %MEM ARGS\n  1  0.0  0.1 sleep infinity"

    def write_ssh_key(self, instance_id: str, pubkey: str) -> None:
        if instance_id in self.items:
            self.items[instance_id]["ssh_key"] = pubkey
        self.events.append("sshkey:" + instance_id)

    def reconcile(self, known_ids: set[str]) -> dict[str, str]:
        return {key: value["status"] for key, value in self.items.items() if key in known_ids}
