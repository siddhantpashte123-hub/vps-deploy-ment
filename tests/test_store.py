import sqlite3
from dataclasses import replace

import pytest

from noir.models import Instance, utc_now
from noir.store import Store


def sample_instance(**changes):
    now = utc_now()
    instance = Instance("noir-a1b2c3d4e5f6", 1, 2, 1, 1, 1, "container", "running", now, now)
    return replace(instance, **changes)


def test_os_round_trips_and_updates(store):
    instance = store.add(sample_instance(os="debian-12"))
    assert store.get(instance.id).os == "debian-12"
    assert store.list(1, 2) == [instance]
    updated = store.update(instance.id, os="ubuntu-24.04")
    assert updated.os == "ubuntu-24.04"
    assert store.update(instance.id, status="stopped").os == "ubuntu-24.04"


def test_invalid_os_cannot_enter_inventory(store):
    with pytest.raises(ValueError, match="Invalid OS"):
        store.add(sample_instance(os="fedora-99"))
    assert store.list() == []
    instance = store.add(sample_instance())
    with pytest.raises(ValueError, match="Invalid OS"):
        store.update(instance.id, os="fedora-99")
    assert store.get(instance.id).os == "ubuntu-24.04"


def test_legacy_inventory_migrates_to_ubuntu_without_losing_rows(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    db = sqlite3.connect(path)
    with db:
        db.execute(
            """CREATE TABLE instances (
                id TEXT PRIMARY KEY,
                guild_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                ram_gb INTEGER NOT NULL,
                cpu INTEGER NOT NULL,
                disk_gb INTEGER NOT NULL,
                container_id TEXT,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            "INSERT INTO instances VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "noir-a1b2c3d4e5f6",
                "1",
                "2",
                1,
                1,
                1,
                "old-container",
                "running",
                "2026-01-01T00:00:00+00:00",
                "2026-01-02T00:00:00+00:00",
            ),
        )
    db.close()
    migrated = Store(path)
    try:
        instance = migrated.get("noir-a1b2c3d4e5f6")
        assert instance.os == "ubuntu-24.04"
        assert instance.container_id == "old-container"
        assert instance.owner_id == 2
        assert instance.status == "running"
        assert migrated.update(instance.id, os="debian-12").os == "debian-12"
    finally:
        migrated.close()
    reopened = Store(path)
    try:
        assert reopened.get("noir-a1b2c3d4e5f6").os == "debian-12"
        columns = [row[1] for row in reopened._db.execute("PRAGMA table_info(instances)")]
        assert columns.count("os") == 1
        assert {"expires_at", "suspended_reason", "backup_at"}.issubset(set(columns))
    finally:
        reopened.close()


def test_suspend_expiry_backup_and_spec_fields_persist(store):
    inst = store.add(sample_instance(os="ubuntu-22.04"))
    updated = store.update(
        inst.id,
        status="suspended",
        suspended_reason="Abuse",
        expires_at="2027-01-01T00:00:00+00:00",
        backup_at="2026-11-14T12:00:00+00:00",
        ram_gb=4,
        cpu=2,
        disk_gb=10,
        owner_id=99,
    )
    assert updated.is_suspended is True
    assert updated.suspended_reason == "Abuse"
    assert updated.expires_at == "2027-01-01T00:00:00+00:00"
    assert updated.backup_at == "2026-11-14T12:00:00+00:00"
    assert (updated.ram_gb, updated.cpu, updated.disk_gb, updated.owner_id) == (4, 2, 10, 99)
