from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Protocol

from .config import Settings
from .errors import AccessDenied, BackendError, UserError
from .models import Actor, Instance, Specs, utc_now, valid_id
from .os_catalog import DEFAULT_OS, os_choice
from .store import Store

log = logging.getLogger(__name__)


ProgressCallback = Callable[[str, int], Awaitable[None]]
BackendProgress = Callable[[str, int], None]


class Backend(Protocol):
    def create(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        progress: BackendProgress | None = None,
        os_key: str = DEFAULT_OS,
    ) -> str: ...
    def recreate_container(
        self,
        instance_id: str,
        specs: Specs,
        owner_id: int,
        guild_id: int,
        running: bool = True,
        os_key: str = DEFAULT_OS,
    ) -> str: ...
    def status(self, instance_id: str) -> str: ...
    def start(self, instance_id: str) -> None: ...
    def stop(self, instance_id: str) -> None: ...
    def remove_all(self, instance_id: str) -> None: ...
    def recreate_disk(self, instance_id: str, disk_gb: int) -> None: ...
    def sshx(self, instance_id: str) -> str: ...
    def close_sshx(self, instance_id: str) -> None: ...
    def reconcile(self, known_ids: set[str]) -> dict[str, str]: ...


class Manager:
    def __init__(self, settings: Settings, store: Store, backend: Backend):
        self.settings = settings
        self.store = store
        self.backend = backend
        self._capacity_lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def authorize(self, actor: Actor, instance: Instance) -> Instance:
        if actor.guild_id != instance.guild_id:
            raise AccessDenied("This VPS belongs to another server.")
        if actor.user_id != instance.owner_id and not actor.is_admin:
            raise AccessDenied("You can manage only your own VPS.")
        return instance

    async def get(self, actor: Actor, instance_id: str) -> Instance:
        if not valid_id(instance_id):
            raise UserError("Use a valid VPS ID, for example `noir-a1b2c3d4e5f6`.")
        return self.authorize(actor, await asyncio.to_thread(self.store.get, instance_id))

    async def list(self, actor: Actor) -> list[Instance]:
        return await asyncio.to_thread(
            self.store.list, actor.guild_id, None if actor.is_admin else actor.user_id
        )

    async def matching_own(self, actor: Actor, specs: Specs) -> list[Instance]:
        instances = await asyncio.to_thread(self.store.list, actor.guild_id, actor.user_id)
        return [i for i in instances if i.specs == specs]

    async def list_for(self, actor: Actor, owner_id: int) -> list[Instance]:
        """Admin-only inventory for one member of the actor's guild."""
        if not actor.is_admin:
            raise AccessDenied("Only admins can list another member's VPSes.")
        return await asyncio.to_thread(self.store.list, actor.guild_id, owner_id)

    async def _emit(self, on_progress: ProgressCallback | None, stage: str, percent: int) -> None:
        if on_progress is None:
            return
        try:
            await on_progress(stage, percent)
        except Exception:
            # A failed Discord edit must never fail an otherwise healthy deploy.
            log.exception("Deployment progress update failed")

    async def deploy(
        self,
        actor: Actor,
        specs: Specs,
        on_progress: ProgressCallback | None = None,
        *,
        owner_id: int | None = None,
        os_key: str = DEFAULT_OS,
    ) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only admins can create VPSes.")
        os_choice(os_key)
        owner_id = actor.user_id if owner_id is None else owner_id
        await self._emit(on_progress, "validate", 5)
        specs.validate(self.settings.max_ram_gb, self.settings.max_cpu, self.settings.max_disk_gb)
        async with self._capacity_lock:
            mine = await asyncio.to_thread(self.store.list, actor.guild_id, owner_id)
            if len(mine) >= self.settings.max_per_user:
                raise UserError(
                    f"<@{owner_id}> already has the maximum of {self.settings.max_per_user} VPSes."
                )
            count, ram, cpu = await asyncio.to_thread(self.store.totals)
            if count >= self.settings.max_total:
                raise UserError("This host has reached its VPS limit.")
            if ram + specs.ram_gb > self.settings.host_ram_mb // 1024:
                raise UserError(
                    "Not enough unreserved host RAM. Delete a VPS or ask the host owner."
                )
            if cpu + specs.cpu > self.settings.host_cpu:
                raise UserError("Not enough unreserved host CPU capacity.")
            free = await asyncio.to_thread(lambda: shutil.disk_usage(self.settings.data_dir).free)
            if free < specs.disk_bytes + 1024**3:
                raise UserError("Not enough host disk space (including a 1 GiB safety reserve).")
            now = utc_now()
            instance = Instance(
                id="noir-" + secrets.token_hex(6),
                guild_id=actor.guild_id,
                owner_id=owner_id,
                ram_gb=specs.ram_gb,
                cpu=specs.cpu,
                disk_gb=specs.disk_gb,
                container_id=None,
                status="provisioning",
                created_at=now,
                updated_at=now,
                os=os_key,
            )
            await self._emit(on_progress, "reserve", 15)
            await asyncio.to_thread(self.store.add, instance)
        loop = asyncio.get_running_loop()

        def thread_progress(stage: str, percent: int) -> None:
            if on_progress is None:
                return
            try:
                asyncio.run_coroutine_threadsafe(
                    self._emit(on_progress, stage, percent), loop
                ).result(timeout=30)
            except Exception:
                log.exception("Deployment progress update failed")

        async with self._locks[instance.id]:
            try:
                container_id = await asyncio.to_thread(
                    self.backend.create,
                    instance.id,
                    specs,
                    owner_id,
                    actor.guild_id,
                    thread_progress,
                    os_key,
                )
                await self._emit(on_progress, "ready", 98)
                result = await asyncio.to_thread(
                    self.store.update, instance.id, container_id=container_id, status="running"
                )
                await self._emit(on_progress, "online", 100)
                return result
            except Exception as exc:
                log.exception("Deployment failed for %s", instance.id)
                try:
                    await asyncio.to_thread(self.backend.remove_all, instance.id)
                    await asyncio.to_thread(self.store.delete, instance.id)
                except Exception:
                    log.exception("Cleanup failed; retaining error record %s", instance.id)
                    await asyncio.to_thread(self.store.update, instance.id, status="error")
                    raise UserError(
                        f"Deployment failed. Cleanup also failed; retain `{instance.id}` and retry "
                        f"`!delete {instance.id}` or contact the host administrator."
                    ) from exc
                if isinstance(exc, UserError):
                    raise
                raise BackendError(
                    "Deployment failed. Ask the host administrator to check the logs."
                ) from exc

    async def _operation(
        self, actor: Actor, instance_id: str, action: str, os_key: str | None = None
    ) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if action == "delete":
                await asyncio.to_thread(self.store.update, instance_id, status="deleting")
                await asyncio.to_thread(self.backend.remove_all, instance_id)
                await asyncio.to_thread(self.store.delete, instance_id)
                return instance
            if action == "reinstall":
                target_os = instance.os if os_key is None else os_key
                os_choice(target_os)
                await asyncio.to_thread(self.store.update, instance_id, status="reinstalling")
                try:
                    await asyncio.to_thread(self.backend.remove_all, instance_id)
                    await asyncio.to_thread(
                        self.backend.recreate_disk, instance_id, instance.disk_gb
                    )
                    container_id = await asyncio.to_thread(
                        self.backend.create,
                        instance_id,
                        instance.specs,
                        instance.owner_id,
                        instance.guild_id,
                        None,
                        target_os,
                    )
                    return await asyncio.to_thread(
                        self.store.update,
                        instance_id,
                        container_id=container_id,
                        status="running",
                        os=target_os,
                    )
                except Exception:
                    await asyncio.to_thread(self.store.update, instance_id, status="error")
                    raise
            if action == "start":
                await asyncio.to_thread(self.backend.start, instance_id)
            elif action == "stop":
                await asyncio.to_thread(self.backend.stop, instance_id)
            else:
                raise ValueError("Unknown operation")
            status = await asyncio.to_thread(self.backend.status, instance_id)
            return await asyncio.to_thread(self.store.update, instance_id, status=status)

    async def start(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "start")

    async def stop(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "stop")

    async def reinstall(
        self, actor: Actor, instance_id: str, os_key: str | None = None
    ) -> Instance:
        return await self._operation(actor, instance_id, "reinstall", os_key)

    async def delete(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "delete")

    async def sshx(self, actor: Actor, instance_id: str) -> tuple[Instance, str]:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            url = await asyncio.to_thread(self.backend.sshx, instance_id)
            return instance, url

    async def close_sshx(self, actor: Actor, instance_id: str) -> None:
        async with self._locks[instance_id]:
            await self.get(actor, instance_id)
            await asyncio.to_thread(self.backend.close_sshx, instance_id)

    async def refresh(self, actor: Actor, instance_id: str) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.status in {"provisioning", "reinstalling", "deleting"}:
                return instance
            status = await asyncio.to_thread(self.backend.status, instance_id)
            return await asyncio.to_thread(self.store.update, instance_id, status=status)

    async def upgrade_runtime(self) -> list[tuple[str, bool]]:
        """Replace containers with the current root runtime; preserve home disks/files."""
        instances = await asyncio.to_thread(self.store.list)
        results: list[tuple[str, bool]] = []
        for instance in instances:
            if instance.status == "deleting":
                continue
            async with self._locks[instance.id]:
                running = instance.status != "stopped"
                try:
                    container_id = await asyncio.to_thread(
                        self.backend.recreate_container,
                        instance.id,
                        instance.specs,
                        instance.owner_id,
                        instance.guild_id,
                        running,
                        instance.os,
                    )
                    await asyncio.to_thread(
                        self.store.update,
                        instance.id,
                        container_id=container_id,
                        status="running" if running else "stopped",
                    )
                    results.append((instance.id, True))
                except Exception:
                    log.exception("Runtime upgrade failed for %s", instance.id)
                    await asyncio.to_thread(self.store.update, instance.id, status="error")
                    results.append((instance.id, False))
        return results

    async def reconcile(self) -> None:
        instances = await asyncio.to_thread(self.store.list)
        known = {i.id for i in instances}
        states = await asyncio.to_thread(self.backend.reconcile, known)
        for instance in instances:
            if instance.status in {"provisioning", "reinstalling", "deleting"}:
                await asyncio.to_thread(self.store.update, instance.id, status="error")
            else:
                await asyncio.to_thread(
                    self.store.update, instance.id, status=states.get(instance.id, "error")
                )
