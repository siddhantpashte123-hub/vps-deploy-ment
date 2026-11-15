from pathlib import Path
from types import SimpleNamespace

import pytest

from fier.config import Settings
from fier.manager import Manager
from fier.models import Actor
from fier.store import Store
from tests.fakes import FakeBackend


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        token="x" * 60,
        client_id=None,
        allowed_guild_id=None,
        admin_ids=frozenset(),
        data_dir=tmp_path,
        images={"ubuntu-24.04": "fier-vps:local", "debian-12": "fier-vps:debian-12"},
        network="fier-vps",
        volume_helper=Path("/usr/local/libexec/fier-volume"),
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
        "fier.manager.shutil.disk_usage",
        lambda path: SimpleNamespace(free=100 * 1024**3),
    )
    return Manager(settings, store, backend)


@pytest.fixture
def actor():
    return Actor(123456789, 987654321, is_admin=True)


@pytest.fixture
def member_actor():
    return Actor(123456789, 987654321, is_admin=False)
