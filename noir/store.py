from __future__ import annotations

import secrets
import sqlite3
import string
import threading
from pathlib import Path

from .errors import NotFound, UserError
from .models import (
    STATUSES,
    Instance,
    PortMapping,
    RedeemCode,
    Specs,
    utc_now,
    valid_id,
)
from .os_catalog import valid_os

COLUMNS = (
    "id, guild_id, owner_id, ram_gb, cpu, disk_gb, container_id, status, "
    "created_at, updated_at, os, expires_at, suspended_reason, backup_at, "
    "name, warned_24h, warned_1h, autobackup, net_locked"
)
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _random_code() -> str:
    left = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    right = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(4))
    return f"NOIR-{left}-{right}"


class Store:
    """Small, durable inventory. All access is serialized; callers may use threads."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock, self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS instances (
                    id TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    ram_gb INTEGER NOT NULL CHECK(ram_gb > 0),
                    cpu INTEGER NOT NULL CHECK(cpu > 0),
                    disk_gb INTEGER NOT NULL CHECK(disk_gb > 0),
                    container_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    os TEXT NOT NULL DEFAULT 'ubuntu-24.04',
                    expires_at TEXT,
                    suspended_reason TEXT,
                    backup_at TEXT,
                    name TEXT,
                    warned_24h INTEGER NOT NULL DEFAULT 0,
                    warned_1h INTEGER NOT NULL DEFAULT 0,
                    autobackup INTEGER NOT NULL DEFAULT 0,
                    net_locked INTEGER NOT NULL DEFAULT 0
                )"""
            )
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(instances)")}
            if "os" not in columns:
                self._db.execute(
                    "ALTER TABLE instances ADD COLUMN os TEXT NOT NULL DEFAULT 'ubuntu-24.04'"
                )
            if "expires_at" not in columns:
                self._db.execute("ALTER TABLE instances ADD COLUMN expires_at TEXT")
            if "suspended_reason" not in columns:
                self._db.execute("ALTER TABLE instances ADD COLUMN suspended_reason TEXT")
            if "backup_at" not in columns:
                self._db.execute("ALTER TABLE instances ADD COLUMN backup_at TEXT")
            if "name" not in columns:
                self._db.execute("ALTER TABLE instances ADD COLUMN name TEXT")
            if "warned_24h" not in columns:
                self._db.execute(
                    "ALTER TABLE instances ADD COLUMN warned_24h INTEGER NOT NULL DEFAULT 0"
                )
            if "warned_1h" not in columns:
                self._db.execute(
                    "ALTER TABLE instances ADD COLUMN warned_1h INTEGER NOT NULL DEFAULT 0"
                )
            if "autobackup" not in columns:
                self._db.execute(
                    "ALTER TABLE instances ADD COLUMN autobackup INTEGER NOT NULL DEFAULT 0"
                )
            if "net_locked" not in columns:
                self._db.execute(
                    "ALTER TABLE instances ADD COLUMN net_locked INTEGER NOT NULL DEFAULT 0"
                )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS instances_owner ON instances(guild_id, owner_id)"
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS port_mappings (
                    host_port INTEGER PRIMARY KEY,
                    instance_id TEXT NOT NULL,
                    container_port INTEGER NOT NULL CHECK(container_port BETWEEN 1 AND 65535),
                    created_at TEXT NOT NULL,
                    UNIQUE(instance_id, container_port)
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS ports_instance ON port_mappings(instance_id)"
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    guild_id TEXT NOT NULL,
                    ram_gb INTEGER NOT NULL CHECK(ram_gb > 0),
                    cpu INTEGER NOT NULL CHECK(cpu > 0),
                    disk_gb INTEGER NOT NULL CHECK(disk_gb > 0),
                    duration_seconds INTEGER,
                    max_uses INTEGER NOT NULL CHECK(max_uses > 0),
                    uses INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS redeem_claims (
                    code TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    instance_id TEXT NOT NULL,
                    claimed_at TEXT NOT NULL,
                    PRIMARY KEY (code, user_id)
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS ssh_keys (
                    user_id TEXT PRIMARY KEY,
                    pubkey TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS subusers (
                    instance_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    PRIMARY KEY (instance_id, user_id)
                )"""
            )
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS subusers_user ON subusers(user_id)"
            )
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS host_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )"""
            )

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def add(self, instance: Instance) -> Instance:
        if not valid_id(instance.id):
            raise ValueError("Invalid internal instance ID")
        if not valid_os(instance.os):
            raise ValueError("Invalid OS")
        if instance.status not in STATUSES:
            raise ValueError("Invalid status")
        with self._lock, self._db:
            self._db.execute(
                f"INSERT INTO instances ({COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    instance.id,
                    str(instance.guild_id),
                    str(instance.owner_id),
                    instance.ram_gb,
                    instance.cpu,
                    instance.disk_gb,
                    instance.container_id,
                    instance.status,
                    instance.created_at,
                    instance.updated_at,
                    instance.os,
                    instance.expires_at,
                    instance.suspended_reason,
                    instance.backup_at,
                    instance.name,
                    int(instance.warned_24h),
                    int(instance.warned_1h),
                    int(instance.autobackup),
                    int(instance.net_locked),
                ),
            )
        return instance

    def get(self, instance_id: str) -> Instance:
        with self._lock:
            row = self._db.execute(
                f"SELECT {COLUMNS} FROM instances WHERE id = ?", (instance_id,)
            ).fetchone()
        if row is None:
            raise NotFound("That VPS no longer exists. Use `!manage` to refresh.")
        return Instance.from_row(row)

    def list(self, guild_id: int | None = None, owner_id: int | None = None) -> list[Instance]:
        clauses, params = [], []
        if guild_id is not None:
            clauses.append("guild_id = ?")
            params.append(str(guild_id))
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(str(owner_id))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            rows = self._db.execute(
                f"SELECT {COLUMNS} FROM instances{where} ORDER BY created_at, id", params
            ).fetchall()
        return [Instance.from_row(row) for row in rows]

    def update(self, instance_id: str, **fields: str | int | None) -> Instance:
        allowed = {
            "status",
            "container_id",
            "os",
            "owner_id",
            "ram_gb",
            "cpu",
            "disk_gb",
            "expires_at",
            "suspended_reason",
            "backup_at",
            "name",
            "warned_24h",
            "warned_1h",
            "autobackup",
            "net_locked",
        }
        if not fields or not set(fields) <= allowed:
            raise ValueError("Unsupported inventory update")
        if "os" in fields and not valid_os(fields["os"]):  # type: ignore[arg-type]
            raise ValueError("Invalid OS")
        if "status" in fields and fields["status"] not in STATUSES:
            raise ValueError("Invalid status")
        payload = dict(fields)
        if "owner_id" in payload and payload["owner_id"] is not None:
            payload["owner_id"] = str(payload["owner_id"])
        assignments = ", ".join(f"{key} = ?" for key in payload)
        with self._lock, self._db:
            cursor = self._db.execute(
                f"UPDATE instances SET {assignments}, updated_at = ? WHERE id = ?",
                (*payload.values(), utc_now(), instance_id),
            )
            if cursor.rowcount != 1:
                raise NotFound("That VPS no longer exists.")
        return self.get(instance_id)

    def delete(self, instance_id: str) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM port_mappings WHERE instance_id = ?", (instance_id,))
            self._db.execute("DELETE FROM subusers WHERE instance_id = ?", (instance_id,))
            self._db.execute("DELETE FROM instances WHERE id = ?", (instance_id,))

    def totals(self) -> tuple[int, int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(ram_gb),0), COALESCE(SUM(cpu),0) FROM instances"
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])

    # ---- Port mappings ----
    def list_ports(self, instance_id: str) -> list[PortMapping]:
        with self._lock:
            rows = self._db.execute(
                "SELECT host_port, instance_id, container_port, created_at "
                "FROM port_mappings WHERE instance_id = ? ORDER BY host_port",
                (instance_id,),
            ).fetchall()
        return [PortMapping.from_row(r) for r in rows]

    def allocate_port(
        self,
        instance_id: str,
        container_port: int,
        *,
        port_start: int = 20000,
        port_end: int = 20199,
        max_per_vps: int = 5,
    ) -> PortMapping:
        if not 1 <= container_port <= 65535:
            raise UserError("Container port must be between 1 and 65535.")
        with self._lock, self._db:
            existing = self.list_ports(instance_id)
            for item in existing:
                if item.container_port == container_port:
                    return item
            if len(existing) >= max_per_vps:
                raise UserError(f"Maximum of {max_per_vps} forwarded ports reached for this VPS.")
            used = {
                int(r[0])
                for r in self._db.execute("SELECT host_port FROM port_mappings").fetchall()
            }
            chosen = next((p for p in range(port_start, port_end + 1) if p not in used), None)
            if chosen is None:
                raise UserError("No free public ports remain in the host pool.")
            now = utc_now()
            self._db.execute(
                "INSERT INTO port_mappings (host_port, instance_id, container_port, created_at) "
                "VALUES (?, ?, ?, ?)",
                (chosen, instance_id, container_port, now),
            )
        return PortMapping(chosen, instance_id, container_port, now)

    def remove_port(self, instance_id: str, port_number: int) -> bool:
        """Remove by either host_port or container_port belonging to instance_id."""
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM port_mappings WHERE instance_id = ? "
                "AND (host_port = ? OR container_port = ?)",
                (instance_id, port_number, port_number),
            )
        return cur.rowcount > 0

    # ---- Redeem / Promo Codes ----
    def create_code(
        self,
        guild_id: int,
        specs: Specs,
        *,
        duration_seconds: int | None,
        max_uses: int,
        created_by: int,
        custom_code: str | None = None,
    ) -> RedeemCode:
        if max_uses < 1 or max_uses > 1000:
            raise UserError("Code uses must be between 1 and 1000.")
        now = utc_now()
        with self._lock, self._db:
            if custom_code:
                allowed_chars = set(string.ascii_uppercase + string.digits + "-")
                code = custom_code.strip().upper()
                if not (4 <= len(code) <= 32 and set(code) <= allowed_chars):
                    raise UserError("Custom code may only contain A–Z, 0–9, and `-` (4–32 chars).")
                exists = self._db.execute(
                    "SELECT 1 FROM redeem_codes WHERE code = ?", (code,)
                ).fetchone()
                if exists:
                    raise UserError(f"Code `{code}` already exists.")
            else:
                for _ in range(20):
                    candidate = _random_code()
                    exists = self._db.execute(
                        "SELECT 1 FROM redeem_codes WHERE code = ?", (candidate,)
                    ).fetchone()
                    if not exists:
                        code = candidate
                        break
                else:
                    raise RuntimeError("Could not allocate unique redeem code")
            self._db.execute(
                "INSERT INTO redeem_codes "
                "(code, guild_id, ram_gb, cpu, disk_gb, duration_seconds, "
                "max_uses, uses, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    code,
                    str(guild_id),
                    specs.ram_gb,
                    specs.cpu,
                    specs.disk_gb,
                    duration_seconds,
                    max_uses,
                    str(created_by),
                    now,
                ),
            )
            row = self._db.execute("SELECT * FROM redeem_codes WHERE code = ?", (code,)).fetchone()
        return RedeemCode.from_row(row)

    def get_code(self, guild_id: int, raw_code: str) -> RedeemCode:
        code = raw_code.strip().upper()
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM redeem_codes WHERE code = ? AND guild_id = ?",
                (code, str(guild_id)),
            ).fetchone()
        if row is None:
            raise UserError("Invalid or unknown redeem code.")
        return RedeemCode.from_row(row)

    def list_codes(self, guild_id: int) -> list[RedeemCode]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM redeem_codes WHERE guild_id = ? ORDER BY created_at DESC",
                (str(guild_id),),
            ).fetchall()
        return [RedeemCode.from_row(r) for r in rows]

    def delete_code(self, guild_id: int, raw_code: str) -> bool:
        code = raw_code.strip().upper()
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM redeem_codes WHERE code = ? AND guild_id = ?",
                (code, str(guild_id)),
            )
        return cur.rowcount > 0

    def check_code_redeemable(self, guild_id: int, raw_code: str, user_id: int) -> RedeemCode:
        promo = self.get_code(guild_id, raw_code)
        if promo.uses >= promo.max_uses:
            raise UserError("This redeem code has already been fully claimed.")
        with self._lock:
            already = self._db.execute(
                "SELECT 1 FROM redeem_claims WHERE code = ? AND user_id = ?",
                (promo.code, str(user_id)),
            ).fetchone()
        if already is not None:
            raise UserError("You have already redeemed this code.")
        return promo

    def consume_code(
        self, guild_id: int, raw_code: str, user_id: int, instance_id: str
    ) -> RedeemCode:
        code = raw_code.strip().upper()
        with self._lock, self._db:
            promo = self.check_code_redeemable(guild_id, code, user_id)
            cur = self._db.execute(
                "UPDATE redeem_codes SET uses = uses + 1 "
                "WHERE code = ? AND guild_id = ? AND uses < max_uses",
                (promo.code, str(guild_id)),
            )
            if cur.rowcount != 1:
                raise UserError("This redeem code has just run out of uses.")
            self._db.execute(
                "INSERT INTO redeem_claims (code, user_id, instance_id, claimed_at) "
                "VALUES (?, ?, ?, ?)",
                (promo.code, str(user_id), instance_id, utc_now()),
            )
            row = self._db.execute(
                "SELECT * FROM redeem_codes WHERE code = ?", (promo.code,)
            ).fetchone()
        return RedeemCode.from_row(row)

    # ---- SSH Public Keys ----
    def set_ssh_key(self, user_id: int, pubkey: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO ssh_keys (user_id, pubkey, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET pubkey = excluded.pubkey, "
                "updated_at = excluded.updated_at",
                (str(user_id), pubkey, utc_now()),
            )

    def get_ssh_key(self, user_id: int) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT pubkey FROM ssh_keys WHERE user_id = ?", (str(user_id),)
            ).fetchone()
        return str(row[0]) if row else None

    def clear_ssh_key(self, user_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute("DELETE FROM ssh_keys WHERE user_id = ?", (str(user_id),))
        return cur.rowcount > 0

    # ---- Sub-users / Shared Access ----
    def add_subuser(self, instance_id: str, user_id: int, *, max_subusers: int = 5) -> None:
        with self._lock, self._db:
            existing = self.list_subusers(instance_id)
            if user_id in existing:
                return
            if len(existing) >= max_subusers:
                raise UserError(f"Maximum of {max_subusers} shared sub-users reached.")
            self._db.execute(
                "INSERT INTO subusers (instance_id, user_id, added_at) VALUES (?, ?, ?)",
                (instance_id, str(user_id), utc_now()),
            )

    def remove_subuser(self, instance_id: str, user_id: int) -> bool:
        with self._lock, self._db:
            cur = self._db.execute(
                "DELETE FROM subusers WHERE instance_id = ? AND user_id = ?",
                (instance_id, str(user_id)),
            )
        return cur.rowcount > 0

    def list_subusers(self, instance_id: str) -> list[int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT user_id FROM subusers WHERE instance_id = ? ORDER BY added_at",
                (instance_id,),
            ).fetchall()
        return [int(r[0]) for r in rows]

    def is_subuser(self, instance_id: str, user_id: int) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM subusers WHERE instance_id = ? AND user_id = ?",
                (instance_id, str(user_id)),
            ).fetchone()
        return row is not None

    def list_shared_for_user(self, guild_id: int, user_id: int) -> list[Instance]:
        with self._lock:
            rows = self._db.execute(
                f"SELECT {COLUMNS} FROM instances "
                "WHERE guild_id = ? AND id IN "
                "(SELECT instance_id FROM subusers WHERE user_id = ?) "
                "ORDER BY created_at, id",
                (str(guild_id), str(user_id)),
            ).fetchall()
        return [Instance.from_row(r) for r in rows]

    # ---- Host Maintenance Mode ----
    def set_maintenance(self, reason: str | None) -> None:
        with self._lock, self._db:
            if reason is None:
                self._db.execute("DELETE FROM host_settings WHERE key = 'maintenance'")
            else:
                self._db.execute(
                    "INSERT INTO host_settings (key, value) VALUES ('maintenance', ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (reason,),
                )

    def get_maintenance(self) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM host_settings WHERE key = 'maintenance'"
            ).fetchone()
        return str(row[0]) if row is not None else None

