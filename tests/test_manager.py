import asyncio

import pytest

from fier.errors import AccessDenied, BackendError, UserError
from fier.models import Actor, Specs
from fier.store import Store


async def test_deploy_start_stop_persist(manager, backend, actor, tmp_path):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    assert instance.status == "running"
    assert instance.container_id == "container-" + instance.id
    assert (await manager.stop(actor, instance.id)).status == "stopped"
    assert (await manager.start(actor, instance.id)).status == "running"
    reopened = Store(tmp_path / "test.sqlite3")
    try:
        assert reopened.get(instance.id).owner_id == actor.user_id
    finally:
        reopened.close()


async def test_ownership_and_guild_isolation(manager, actor):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    with pytest.raises(AccessDenied):
        await manager.stop(Actor(999, actor.guild_id), instance.id)
    with pytest.raises(AccessDenied):
        await manager.get(Actor(actor.user_id, 555), instance.id)
    admin = Actor(999, actor.guild_id, True)
    assert (await manager.stop(admin, instance.id)).status == "stopped"


async def test_reinstall_wipes_and_keeps_identity(manager, backend, actor):
    first = await manager.deploy(actor, Specs(2, 2, 3))
    second = await manager.reinstall(actor, first.id)
    assert second.id == first.id and second.specs == first.specs
    assert second.status == "running"
    assert "wipe:" + first.id in backend.events


async def test_delete_removes_disk_and_record(manager, backend, actor):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    await manager.delete(actor, instance.id)
    assert instance.id not in backend.items
    assert await manager.list(actor) == []
    with pytest.raises(UserError):
        await manager.get(actor, instance.id)


@pytest.mark.parametrize(
    "specs", [Specs(0, 1, 1), Specs(1, -1, 1), Specs(5, 1, 1), Specs(1, 1, 21), Specs(1.5, 1, 1)]
)
async def test_invalid_plans_rejected(manager, actor, specs):
    with pytest.raises(UserError):
        await manager.deploy(actor, specs)


async def test_per_user_limit(manager, actor):
    await manager.deploy(actor, Specs(1, 1, 1))
    await manager.deploy(actor, Specs(1, 1, 1))
    with pytest.raises(UserError, match="maximum"):
        await manager.deploy(actor, Specs(1, 1, 1))


async def test_concurrent_deploys_cannot_oversubscribe(manager, actor):
    results = await asyncio.gather(
        *[manager.deploy(actor, Specs(1, 1, 1)) for _ in range(5)], return_exceptions=True
    )
    assert sum(not isinstance(r, Exception) for r in results) == 2
    assert sum(isinstance(r, UserError) for r in results) == 3


async def test_failed_deploy_cleans_up(manager, backend, actor):
    backend.fail_create = True
    with pytest.raises(BackendError):
        await manager.deploy(actor, Specs(1, 1, 1))
    assert await manager.list(actor) == []
    assert backend.items == {}


async def test_failed_cleanup_retains_error_record(manager, backend, actor):
    backend.fail_create = True
    backend.fail_cleanup = True
    with pytest.raises(UserError, match="Cleanup also failed"):
        await manager.deploy(actor, Specs(1, 1, 1))
    records = await manager.list(actor)
    assert len(records) == 1 and records[0].status == "error"
    backend.fail_cleanup = False
    await manager.delete(actor, records[0].id)
    assert await manager.list(actor) == []


async def test_numeric_delete_matches_only_own(manager, actor):
    mine = await manager.deploy(actor, Specs(1, 1, 1))
    other = await manager.deploy(Actor(222, actor.guild_id), Specs(1, 1, 1))
    matches = await manager.matching_own(actor, Specs(1, 1, 1))
    assert [i.id for i in matches] == [mine.id]
    assert other.id not in {i.id for i in matches}


async def test_sshx_close_requires_owner(manager, backend, actor):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    _, url = await manager.sshx(actor, instance.id)
    assert url.startswith("https://sshx.io/")
    with pytest.raises(AccessDenied):
        await manager.close_sshx(Actor(999, actor.guild_id), instance.id)
    await manager.close_sshx(actor, instance.id)
    assert "close-sshx:" + instance.id in backend.events


async def test_reconcile_interrupted_deploy(manager, store, backend, actor):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    store.update(instance.id, status="provisioning")
    await manager.reconcile()
    assert store.get(instance.id).status == "error"
    backend.items[instance.id]["status"] = "stopped"
    await manager.reconcile()
    assert store.get(instance.id).status == "stopped"
