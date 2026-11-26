from __future__ import annotations

from noir.models import Specs
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
    ) -> str:
        self.events.append("recreate-container:" + instance_id)
        self.items[instance_id] = {
            "status": "running" if running else "stopped",
            "specs": specs,
            "owner": owner_id,
            "guild": guild_id,
            "os": os_key,
        }
        return "container-" + instance_id

    def status(self, instance_id: str) -> str:
        return self.items.get(instance_id, {}).get("status", "error")

    def start(self, instance_id: str) -> None:
        self.items[instance_id]["status"] = "running"
        self.events.append("start:" + instance_id)

    def stop(self, instance_id: str) -> None:
        self.items[instance_id]["status"] = "stopped"
        self.events.append("stop:" + instance_id)

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

    def reconcile(self, known_ids: set[str]) -> dict[str, str]:
        return {key: value["status"] for key, value in self.items.items() if key in known_ids}
