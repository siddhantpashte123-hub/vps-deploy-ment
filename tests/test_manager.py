import asyncio

import pytest

from noir.errors import AccessDenied, BackendError, UserError
from noir.models import Actor, Specs
from noir.store import Store


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


async def test_suspend_blocks_member_actions_and_unsuspend_restores(
    manager, actor, member_actor, backend, store
):
    inst = await manager.deploy(actor, Specs(1, 1, 1), owner_id=member_actor.user_id)
    with pytest.raises(AccessDenied):
        await manager.suspend(member_actor, inst.id, "nope")
    suspended = await manager.suspend(actor, inst.id, "TOS check")
    assert suspended.is_suspended is True
    assert suspended.suspended_reason == "TOS check"
    assert backend.items[inst.id]["status"] == "stopped"

    # Member cannot start, restart, open console, or backup while suspended
    for call in (
        lambda: manager.start(member_actor, inst.id),
        lambda: manager.restart(member_actor, inst.id),
        lambda: manager.sshx(member_actor, inst.id),
        lambda: manager.backup(member_actor, inst.id),
    ):
        with pytest.raises(UserError, match="suspended"):
            await call()

    # Refresh and runtime upgrade preserve suspended state
    refreshed = await manager.refresh(member_actor, inst.id)
    assert refreshed.is_suspended is True
    await manager.upgrade_runtime()
    assert store.get(inst.id).is_suspended is True

    restored = await manager.unsuspend(actor, inst.id)
    assert restored.status == "running"
    assert restored.suspended_reason is None


async def test_resize_updates_plan_without_wiping_home_and_blocks_disk_shrink(
    manager, actor, member_actor, backend
):
    inst = await manager.deploy(actor, Specs(1, 1, 4), owner_id=member_actor.user_id)
    with pytest.raises(AccessDenied):
        await manager.resize(member_actor, inst.id, Specs(2, 2, 6))
    with pytest.raises(UserError, match="shrink"):
        await manager.resize(actor, inst.id, Specs(2, 2, 2))
    resized = await manager.resize(actor, inst.id, Specs(2, 2, 8))
    assert (resized.ram_gb, resized.cpu, resized.disk_gb) == (2, 2, 8)
    assert resized.status == "running"
    assert f"resize-disk:{inst.id}:8" in backend.events
    assert f"wipe:{inst.id}" not in backend.events


async def test_restart_backup_restore_and_transfer(manager, actor, member_actor, backend):
    inst = await manager.deploy(actor, Specs(1, 1, 2), owner_id=member_actor.user_id)
    restarted = await manager.restart(member_actor, inst.id)
    assert restarted.status == "running"
    assert f"restart:{inst.id}" in backend.events

    with pytest.raises(UserError, match="No backup snapshot"):
        await manager.restore(member_actor, inst.id)

    backed = await manager.backup(member_actor, inst.id)
    assert backed.backup_at is not None
    restored = await manager.restore(member_actor, inst.id)
    assert restored.status == "running"
    assert f"backup:{inst.id}" in backend.events
    assert f"restore:{inst.id}" in backend.events

    moved = await manager.transfer(actor, inst.id, 777)
    assert moved.owner_id == 777


async def test_expiry_sweep_and_renew_auto_unsuspends(manager, actor, store):
    past = "2020-01-01T00:00:00+00:00"
    inst = await manager.deploy(actor, Specs(1, 1, 1), expires_at=past)
    expired = await manager.sweep_expired()
    assert [i.id for i in expired] == [inst.id]
    locked = store.get(inst.id)
    assert locked.status == "suspended"
    assert locked.suspended_reason == "Expired"

    renewed = await manager.renew(actor, inst.id, 86400 * 30)
    assert renewed.status == "running"
    assert renewed.suspended_reason is None
    assert renewed.expires_at is not None

    cleared = await manager.renew(actor, inst.id, None)
    assert cleared.expires_at is None


async def test_port_forwarding_allocates_and_preserves_across_resize(
    manager, actor, member_actor, backend
):
    inst = await manager.deploy(actor, Specs(1, 1, 2), owner_id=member_actor.user_id)
    mapping = await manager.add_port(member_actor, inst.id, 3000)
    assert mapping.host_port == 20000
    assert backend.items[inst.id]["ports"] == {3000: 20000}

    # Resize keeps port binding
    await manager.resize(actor, inst.id, Specs(2, 2, 4))
    assert backend.items[inst.id]["ports"] == {3000: 20000}

    # Remove port
    await manager.remove_port(member_actor, inst.id, 20000)
    assert backend.items[inst.id]["ports"] == {}


async def test_redeem_codes_single_claim_and_depletion(manager, actor, backend):
    promo = await manager.create_code(actor, Specs(1, 1, 2), duration_seconds=86400 * 7, max_uses=1)
    claimer = Actor(555666777, actor.guild_id, False)
    inst = await manager.redeem_deploy(claimer, promo.code, "alpine-3.20")
    assert inst.owner_id == claimer.user_id
    assert inst.os == "alpine-3.20"
    assert inst.expires_at is not None

    # Depleted / duplicate claim rejected
    with pytest.raises(UserError, match="claimed|already"):
        await manager.validate_redeem(claimer, promo.code)


async def test_rename_sshkey_exec_logs_templates_and_expiry_warnings(
    manager, actor, member_actor, backend, store
):
    from noir.models import future_iso

    soon = future_iso(1800)  # 30 minutes -> triggers 1h warning once
    inst = await manager.deploy(
        actor, Specs(1, 1, 2), owner_id=member_actor.user_id, expires_at=soon
    )
    renamed = await manager.rename(member_actor, inst.id, "Music-Bot")
    assert renamed.name == "Music-Bot"

    pubkey = (
        "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB noir@dev"
    )
    _, synced = await manager.set_ssh_key(member_actor, pubkey)
    assert synced == 1
    assert backend.items[inst.id]["ssh_key"] == pubkey

    _, code, out = await manager.exec_command(member_actor, inst.id, "uname -a")
    assert code == 0
    assert "uname -a" in out

    _, logs = await manager.inspect_processes(member_actor, inst.id)
    assert "PROCESSES" in logs

    _, directory = await manager.install_template(member_actor, inst.id, "python-bot")
    assert directory == "/home/noir/python-bot"

    alerts = await manager.sweep_expiry_warnings()
    assert [(i.id, win) for i, win in alerts] == [(inst.id, "1h")]
    assert await manager.sweep_expiry_warnings() == []


async def test_pinggy_ssh_subusers_autobackup_sweep_and_gitdeploy_validation(
    manager, actor, member_actor, backend, store
):
    inst = await manager.deploy(actor, Specs(1, 1, 2), owner_id=member_actor.user_id)

    # Pinggy SSH session & close
    _, session = await manager.pinggy_ssh(member_actor, inst.id)
    assert session.command == f"ssh -p {session.port} root@{session.host}"
    await manager.close_pinggy_ssh(member_actor, inst.id)
    assert f"close-pinggy-ssh:{inst.id}" in backend.events

    # Sub-user sharing: sub-user sees shared VPS in manager.list(), cannot delete
    sub_actor = Actor(888999000, actor.guild_id, False)
    await manager.share_vps(member_actor, inst.id, sub_actor.user_id)
    visible = await manager.list(sub_actor)
    assert [i.id for i in visible] == [inst.id]
    from noir.errors import AccessDenied

    with pytest.raises(AccessDenied):
        await manager.delete(sub_actor, inst.id)

    # Scheduled auto-backup sweep runs once when due, skips until 24h elapses
    await manager.set_autobackup(member_actor, inst.id, True)
    saved = await manager.sweep_autobackups()
    assert [i.id for i in saved] == [inst.id]
    assert await manager.sweep_autobackups() == []

    # Delete backup clears backup_at and rejects sub-users
    with pytest.raises(AccessDenied):
        await manager.delete_backup(sub_actor, inst.id)
    cleared_bkp = await manager.delete_backup(member_actor, inst.id)
    assert cleared_bkp.backup_at is None
    assert f"delete-backup:{inst.id}" in backend.events

    # Git deploy rejects non-GitHub / shell-injection URLs
    with pytest.raises(UserError, match="HTTPS GitHub repository URL"):
        await manager.git_deploy(member_actor, inst.id, "http://evil.local/repo;rm -rf /")

