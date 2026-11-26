from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import UserError
from .os_catalog import DEFAULT_OS

# New VPS IDs use the noir- prefix. Legacy fier- IDs from pre-NOIR installs
# remain valid so existing VPSes keep working after the rename.
ID_PATTERN = re.compile(r"(?:noir|fier)-[a-f0-9]{12}\Z")
STATUSES = {"provisioning", "running", "stopped", "reinstalling", "deleting", "error"}


def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Specs:
    ram_gb: int
    cpu: int
    disk_gb: int

    def validate(self, max_ram: int = 1024, max_cpu: int = 128, max_disk: int = 1024) -> None:
        for label, value, maximum in (
            ("RAM", self.ram_gb, max_ram),
            ("CPU", self.cpu, max_cpu),
            ("disk", self.disk_gb, max_disk),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise UserError(f"{label} must be a whole number from 1 to {maximum}.")

    @property
    def memory_bytes(self) -> int:
        return self.ram_gb * 1024**3

    @property
    def disk_bytes(self) -> int:
        return self.disk_gb * 1024**3

    def label(self) -> str:
        return f"{self.ram_gb} GiB RAM · {self.cpu} CPU · {self.disk_gb} GiB disk"


@dataclass(frozen=True)
class Actor:
    user_id: int
    guild_id: int
    is_admin: bool = False


@dataclass(frozen=True)
class Instance:
    id: str
    guild_id: int
    owner_id: int
    ram_gb: int
    cpu: int
    disk_gb: int
    container_id: str | None
    status: str
    created_at: str
    updated_at: str
    os: str = DEFAULT_OS

    @property
    def specs(self) -> Specs:
        return Specs(self.ram_gb, self.cpu, self.disk_gb)

    @classmethod
    def from_row(cls, row: object) -> Instance:
        # sqlite3.Row supports mapping access.
        data = dict(row)  # type: ignore[arg-type]
        data["guild_id"] = int(data["guild_id"])
        data["owner_id"] = int(data["owner_id"])
        return cls(**data)
