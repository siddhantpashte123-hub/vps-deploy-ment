from pathlib import Path
from types import SimpleNamespace

import pytest

from noir.config import Settings
from noir.manager import Manager
from noir.models import Actor
from noir.store import Store
from tests.fakes import FakeBackend


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        token="x" * 60,
        client_id=None,
        allowed_guild_id=987654321,
        admin_ids=frozenset(),
        data_dir=tmp_path,
        images={"ubuntu-24.04": "noir-vps:local", "debian-12": "noir-vps:debian-12"},
        network="noir-vps",
        volume_helper=Path("/usr/local/libexec/noir-volume"),
        max_ram_gb=4,
        max_cpu=4,
        max_disk_gb=20,
        max_per_user=2,
        max_total=20,
        host_ram_mb=8192,
        host_cpu=8,
    )


@pytest.fixture
def store(tmp_path: Path):
    store = Store(tmp_path / "test.sqlite3")
    yield store
    store.close()


@pytest.fixture
def backend():
    return FakeBackend()


@pytest.fixture
def manager(settings, store, backend, monkeypatch):
    monkeypatch.setattr(
        "noir.manager.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100 * 1024**3),
    )
    return Manager(settings, store, backend)


@pytest.fixture
def actor():
    return Actor(123456789, 987654321, is_admin=True)


@pytest.fixture
def member_actor():
    return Actor(123456789, 987654321, is_admin=False)
