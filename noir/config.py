from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from dotenv import load_dotenv

from .errors import UserError
from .os_catalog import DEFAULT_OS, os_choice


def _int(name: str, default: int, minimum: int = 0, maximum: int = 1024) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise UserError(f"{name} must be a whole number.") from exc
    if not minimum <= value <= maximum:
        raise UserError(f"{name} must be between {minimum} and {maximum}.")
    return value


def _optional_id(name: str) -> int | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    if not raw.isdigit() or not 1 <= len(raw) <= 25:
        raise UserError(f"{name} must be a Discord numeric ID.")
    return int(raw)


def _physical_ram_mb() -> int:
    try:
        return (os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")) // 1024**2
    except (ValueError, OSError, AttributeError):
        return 2048


@dataclass(frozen=True)
class Settings:
    token: str = field(repr=False)
    client_id: int | None
    allowed_guild_id: int | None
    admin_ids: frozenset[int]
    data_dir: Path
    images: Mapping[str, str]
    network: str
    volume_helper: Path
    max_ram_gb: int
    max_cpu: int
    max_disk_gb: int
    max_per_user: int
    max_total: int
    host_ram_mb: int
    host_cpu: int
    log_channel_id: int | None = None
    public_ip: str = ""
    port_start: int = 20000
    port_end: int = 20199
    max_ports_per_vps: int = 5

    def image_for(self, os_key: str) -> str:
        os_choice(os_key)
        return self.images[os_key]

    @classmethod
    def load(cls, require_token: bool = True, env_file: Path | None = None) -> Settings:
        if env_file and env_file.exists():
            load_dotenv(env_file, override=False)
        load_dotenv(Path.cwd() / ".env", override=False)
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_token and not token:
            raise UserError(
                "DISCORD_TOKEN is missing. Run the installer or `python -m noir setup`."
            )
        if any(c.isspace() or ord(c) < 32 for c in token):
            raise UserError("DISCORD_TOKEN contains invalid whitespace/control characters.")
        admins = os.getenv("NOIR_ADMIN_IDS", "").strip()
        admin_ids: set[int] = set()
        if admins:
            for part in admins.split(","):
                part = part.strip()
                if not part.isdigit() or not 1 <= len(part) <= 25:
                    raise UserError("NOIR_ADMIN_IDS must contain comma-separated numeric IDs.")
                admin_ids.add(int(part))
        legacy_image = os.getenv("NOIR_IMAGE", "").strip()
        ubuntu_image = os.getenv("NOIR_IMAGE_UBUNTU", legacy_image or "noir-vps:local").strip()
        ubuntu22_image = os.getenv("NOIR_IMAGE_UBUNTU_22", "noir-vps:ubuntu-22.04").strip()
        debian_image = os.getenv("NOIR_IMAGE_DEBIAN", "noir-vps:debian-12").strip()
        alpine_image = os.getenv("NOIR_IMAGE_ALPINE", "noir-vps:alpine-3.20").strip()
        port_start = _int("NOIR_PORT_START", 20000, 1024, 65000)
        port_end = _int("NOIR_PORT_END", 20199, 1024, 65535)
        if port_end < port_start:
            raise UserError("NOIR_PORT_END must be greater than or equal to NOIR_PORT_START.")
        settings = cls(
            token=token,
            client_id=_optional_id("NOIR_CLIENT_ID"),
            allowed_guild_id=_optional_id("NOIR_ALLOWED_GUILD_ID"),
            admin_ids=frozenset(admin_ids),
            data_dir=Path(os.getenv("NOIR_DATA_DIR", "/var/lib/noir/data")).resolve(),
            images=MappingProxyType(
                {
                    DEFAULT_OS: ubuntu_image,
                    "ubuntu-22.04": ubuntu22_image,
                    "debian-12": debian_image,
                    "alpine-3.20": alpine_image,
                }
            ),
            network=os.getenv("NOIR_NETWORK", "noir-vps").strip(),
            volume_helper=Path(os.getenv("NOIR_VOLUME_HELPER", "/usr/local/libexec/noir-volume")),
            max_ram_gb=_int("NOIR_MAX_RAM_GB", 4, 1, 128),
            max_cpu=_int("NOIR_MAX_CPU", 4, 1, 128),
            max_disk_gb=_int("NOIR_MAX_DISK_GB", 20, 1, 256),
            max_per_user=_int("NOIR_MAX_VPS_PER_USER", 2, 1, 100),
            max_total=_int("NOIR_MAX_VPS_TOTAL", 20, 1, 500),
            host_ram_mb=_int("NOIR_HOST_RAM_MB", 0, 0, 1024 * 1024)
            or max(512, int(_physical_ram_mb() * 0.75)),
            host_cpu=_int("NOIR_HOST_CPU", 0, 0, 1024) or (os.cpu_count() or 1),
            log_channel_id=_optional_id("NOIR_LOG_CHANNEL_ID"),
            public_ip=os.getenv("NOIR_PUBLIC_IP", "").strip(),
            port_start=port_start,
            port_end=port_end,
            max_ports_per_vps=_int("NOIR_MAX_PORTS_PER_VPS", 5, 1, 50),
        )
        if not settings.network:
            raise UserError("NOIR_NETWORK cannot be empty.")
        for os_key, image in settings.images.items():
            if not image or any(c.isspace() or ord(c) < 32 for c in image):
                raise UserError(f"The configured image for {os_key} is invalid.")
        return settings
