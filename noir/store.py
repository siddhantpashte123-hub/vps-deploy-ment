from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from .errors import NotFound
from .models import STATUSES, Instance, utc_now, valid_id
from .os_catalog import valid_os

COLUMNS = (
    "id, guild_id, owner_id, ram_gb, cpu, disk_gb, container_id, status, "
    "created_at, updated_at, os, expires_at, suspended_reason, backup_at"
)


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
                    backup_at TEXT
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
            self._db.execute(
                "CREATE INDEX IF NOT EXISTS instances_owner ON instances(guild_id, owner_id)"
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
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            self._db.execute("DELETE FROM instances WHERE id = ?", (instance_id,))

    def totals(self) -> tuple[int, int, int]:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*), COALESCE(SUM(ram_gb),0), COALESCE(SUM(cpu),0) FROM instances"
            ).fetchone()
        return int(row[0]), int(row[1]), int(row[2])
