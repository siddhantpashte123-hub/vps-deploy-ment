from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import UserError
from .os_catalog import DEFAULT_OS

# New VPS IDs use the noir- prefix. Legacy fier- IDs from pre-NOIR installs
# remain valid so existing VPSes keep working after the rename.
ID_PATTERN = re.compile(r"(?:noir|fier)-[a-f0-9]{12}\Z")
DURATION_RE = re.compile(r"([1-9][0-9]{0,3})([smhdw])\Z", re.IGNORECASE)
STATUSES = {
    "provisioning",
    "running",
    "stopped",
    "suspended",
    "reinstalling",
    "deleting",
    "error",
}


def valid_id(value: str) -> bool:
    return isinstance(value, str) and bool(ID_PATTERN.fullmatch(value))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_duration_seconds(raw: str) -> int:
    match = DURATION_RE.fullmatch(raw.strip())
    if not match:
        raise UserError(
            "Invalid duration. Use a number + unit such as `30d`, `7d`, `12h`, or `2w`."
        )
    amount = int(match.group(1))
    unit = match.group(2).lower()
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = amount * factors[unit]
    if seconds > 3650 * 86400:
        raise UserError("Duration cannot exceed 10 years.")
    return seconds


def future_iso(seconds: int, *, base_iso: str | None = None) -> str:
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    base = now
    if base_iso:
        try:
            parsed = datetime.fromisoformat(base_iso)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            if parsed > now:
                base = parsed
        except ValueError:
            pass
    return (base + timedelta(seconds=seconds)).isoformat()


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
class LiveStats:
    online: bool
    uptime_seconds: int = 0
    cpu_percent: float = 0.0
    ram_used_bytes: int = 0
    disk_used_bytes: int = 0


@dataclass(frozen=True)
class HostStatus:
    hostname: str
    uptime_seconds: int
    cpu_cores: int
    cpu_percent: float
    load_1m: float
    ram_used_bytes: int
    ram_total_bytes: int
    disk_used_bytes: int
    disk_total_bytes: int
    storage_mode: str
    running_vps: int
    stopped_vps: int
    total_vps: int
    max_total_vps: int
    reserved_ram_gb: int
    reserved_cpu: int
    reserved_disk_gb: int
    suspended_vps: int = 0


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
    expires_at: str | None = None
    suspended_reason: str | None = None
    backup_at: str | None = None

    @property
    def is_suspended(self) -> bool:
        return self.status == "suspended"

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
