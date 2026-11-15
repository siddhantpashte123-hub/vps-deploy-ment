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
    other = await manager.deploy(actor, Specs(1, 1, 1), owner_id=222)
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


async def test_deploy_emits_ordered_progress(manager, actor):
    events = []

    async def on_progress(stage, percent):
        events.append((stage, percent))

    instance = await manager.deploy(actor, Specs(1, 1, 1), on_progress)
    assert instance.status == "running"
    assert [p for _, p in events] == sorted(p for _, p in events)
    assert events[0] == ("validate", 5)
    assert events[-1] == ("online", 100)
    assert {"reserve", "storage", "container", "boot", "ready"} <= {s for s, _ in events}


async def test_progress_failure_does_not_fail_deploy(manager, actor):
    async def broken(stage, percent):
        raise RuntimeError("discord edit failed")

    instance = await manager.deploy(actor, Specs(1, 1, 1), broken)
    assert instance.status == "running"


async def test_upgrade_runtime_preserves_home_os_and_stopped_state(manager, backend, actor, store):
    running = await manager.deploy(actor, Specs(1, 1, 1))
    stopped = await manager.deploy(actor, Specs(1, 1, 1), owner_id=222, os_key="debian-12")
    await manager.stop(Actor(222, actor.guild_id), stopped.id)
    results = await manager.upgrade_runtime()
    assert dict(results) == {running.id: True, stopped.id: True}
    assert store.get(running.id).status == "running"
    assert store.get(stopped.id).status == "stopped"
    assert store.get(running.id).os == "ubuntu-24.04"
    assert store.get(stopped.id).os == "debian-12"
    assert backend.items[running.id]["os"] == "ubuntu-24.04"
    assert backend.items[stopped.id]["os"] == "debian-12"
    assert "recreate-container:" + running.id in backend.events
    assert "wipe:" + running.id not in backend.events
    assert "remove:" + running.id not in backend.events


async def test_reconcile_interrupted_deploy(manager, store, backend, actor):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    store.update(instance.id, status="provisioning")
    await manager.reconcile()
    assert store.get(instance.id).status == "error"
    backend.items[instance.id]["status"] = "stopped"
    await manager.reconcile()
    assert store.get(instance.id).status == "stopped"


async def test_deploy_requires_admin(manager, member_actor, backend, store):
    with pytest.raises(AccessDenied, match="Only admins"):
        await manager.deploy(member_actor, Specs(1, 1, 1))
    with pytest.raises(AccessDenied, match="Only admins"):
        await manager.deploy(member_actor, Specs(1, 1, 1), owner_id=222)
    assert backend.events == []
    assert store.list() == []


async def test_deploy_for_member_records_owner_guild_and_os(manager, actor, backend, store):
    instance = await manager.deploy(actor, Specs(1, 2, 3), owner_id=222, os_key="debian-12")
    assert instance.owner_id == 222
    assert instance.guild_id == actor.guild_id
    assert instance.os == "debian-12"
    assert backend.items[instance.id]["owner"] == 222
    assert backend.items[instance.id]["guild"] == actor.guild_id
    assert backend.items[instance.id]["os"] == "debian-12"
    assert store.get(instance.id) == instance
    assert store.list(actor.guild_id, actor.user_id) == []
    owner = Actor(222, actor.guild_id)
    assert await manager.list(owner) == [instance]
    assert (await manager.stop(owner, instance.id)).status == "stopped"


async def test_per_user_limit_counts_target_not_admin(manager, actor):
    await manager.deploy(actor, Specs(1, 1, 1), owner_id=222)
    await manager.deploy(actor, Specs(1, 1, 1), owner_id=222)
    with pytest.raises(UserError, match="<@222> already has the maximum"):
        await manager.deploy(actor, Specs(1, 1, 1), owner_id=222)
    own = await manager.deploy(actor, Specs(1, 1, 1))
    assert own.owner_id == actor.user_id


async def test_list_for_is_admin_only_and_guild_scoped(manager, actor, member_actor):
    mine = await manager.deploy(actor, Specs(1, 1, 1), owner_id=222)
    other_guild_admin = Actor(999, 555, True)
    elsewhere = await manager.deploy(other_guild_admin, Specs(1, 1, 1), owner_id=222)
    assert await manager.list_for(actor, 222) == [mine]
    assert await manager.list_for(other_guild_admin, 222) == [elsewhere]
    with pytest.raises(AccessDenied, match="Only admins"):
        await manager.list_for(member_actor, member_actor.user_id)


async def test_reinstall_can_switch_os_and_preserves_identity(manager, actor, backend, store):
    first = await manager.deploy(actor, Specs(2, 2, 3), owner_id=222, os_key="debian-12")
    second = await manager.reinstall(actor, first.id, os_key="ubuntu-24.04")
    assert second.id == first.id
    assert second.owner_id == 222
    assert second.guild_id == first.guild_id
    assert second.specs == first.specs
    assert second.os == "ubuntu-24.04"
    assert second.status == "running"
    assert backend.items[first.id]["os"] == "ubuntu-24.04"
    assert store.get(first.id).os == "ubuntu-24.04"
    assert "wipe:" + first.id in backend.events
    third = await manager.reinstall(actor, first.id)
    assert third.os == "ubuntu-24.04"


async def test_invalid_os_rejected_before_any_mutation(manager, actor, backend, store):
    with pytest.raises(UserError, match="Unknown OS"):
        await manager.deploy(actor, Specs(1, 1, 1), os_key="fedora-99")
    assert store.list() == []
    assert backend.events == []
    instance = await manager.deploy(actor, Specs(1, 1, 1), os_key="debian-12")
    before = list(backend.events)
    with pytest.raises(UserError, match="Unknown OS"):
        await manager.reinstall(actor, instance.id, os_key="fedora-99")
    assert backend.events == before
    assert store.get(instance.id) == instance


async def test_failed_os_switch_keeps_recorded_os_and_marks_error(manager, actor, backend, store):
    instance = await manager.deploy(actor, Specs(1, 1, 1), os_key="debian-12")
    backend.fail_create = True
    with pytest.raises(RuntimeError):
        await manager.reinstall(actor, instance.id, os_key="ubuntu-24.04")
    retained = store.get(instance.id)
    assert retained.status == "error"
    assert retained.os == "debian-12"
