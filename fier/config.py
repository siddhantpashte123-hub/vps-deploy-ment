from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .errors import UserError


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
    deploy_role_id: int | None
    data_dir: Path
    image: str
    network: str
    volume_helper: Path
    max_ram_gb: int
    max_cpu: int
    max_disk_gb: int
    max_per_user: int
    max_total: int
    host_ram_mb: int
    host_cpu: int

    @classmethod
    def load(cls, require_token: bool = True, env_file: Path | None = None) -> Settings:
        if env_file and env_file.exists():
            load_dotenv(env_file, override=False)
        load_dotenv(Path.cwd() / ".env", override=False)
        load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if require_token and not token:
            raise UserError(
                "DISCORD_TOKEN is missing. Run the installer or `python -m fier setup`."
            )
        if any(c.isspace() or ord(c) < 32 for c in token):
            raise UserError("DISCORD_TOKEN contains invalid whitespace/control characters.")
        admins = os.getenv("FIER_ADMIN_IDS", "").strip()
        admin_ids: set[int] = set()
        if admins:
            for part in admins.split(","):
                part = part.strip()
                if not part.isdigit() or not 1 <= len(part) <= 25:
                    raise UserError("FIER_ADMIN_IDS must contain comma-separated numeric IDs.")
                admin_ids.add(int(part))
        settings = cls(
            token=token,
            client_id=_optional_id("FIER_CLIENT_ID"),
            allowed_guild_id=_optional_id("FIER_ALLOWED_GUILD_ID"),
            admin_ids=frozenset(admin_ids),
            deploy_role_id=_optional_id("FIER_DEPLOY_ROLE_ID"),
            data_dir=Path(os.getenv("FIER_DATA_DIR", "/var/lib/fier/data")).resolve(),
            image=os.getenv("FIER_IMAGE", "fier-vps:local").strip(),
            network=os.getenv("FIER_NETWORK", "fier-vps").strip(),
            volume_helper=Path(os.getenv("FIER_VOLUME_HELPER", "/usr/local/libexec/fier-volume")),
            max_ram_gb=_int("FIER_MAX_RAM_GB", 4, 1, 128),
            max_cpu=_int("FIER_MAX_CPU", 4, 1, 128),
            max_disk_gb=_int("FIER_MAX_DISK_GB", 20, 1, 256),
            max_per_user=_int("FIER_MAX_VPS_PER_USER", 2, 1, 100),
            max_total=_int("FIER_MAX_VPS_TOTAL", 20, 1, 500),
            host_ram_mb=_int("FIER_HOST_RAM_MB", 0, 0, 1024 * 1024)
            or max(512, int(_physical_ram_mb() * 0.75)),
            host_cpu=_int("FIER_HOST_CPU", 0, 0, 1024) or (os.cpu_count() or 1),
        )
        if not settings.image or not settings.network:
            raise UserError("FIER_IMAGE and FIER_NETWORK cannot be empty.")
        return settings
