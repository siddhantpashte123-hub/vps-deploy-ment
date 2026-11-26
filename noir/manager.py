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
from .models import Actor, HostStatus, Instance, LiveStats, Specs, utc_now, valid_id
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

    async def stats(self, actor: Actor, instance_id: str) -> LiveStats:
        instance = await self.get(actor, instance_id)
        if instance.status != "running":
            return LiveStats(online=False)
        fn = getattr(self.backend, "stats", None)
        if fn is None:
            return LiveStats(online=True)
        try:
            return await asyncio.to_thread(fn, instance_id)
        except Exception:
            log.debug("Stats collection failed for %s", instance_id, exc_info=True)
            return LiveStats(online=instance.status == "running")

    async def host_status(self, actor: Actor) -> HostStatus:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can view host status.")
        instances = [
            i
            for i in await asyncio.to_thread(self.store.list)
            if i.guild_id == actor.guild_id and i.status != "deleting"
        ]
        storage_mode = getattr(self.backend, "storage_mode", "unknown") or "unknown"
        return await asyncio.to_thread(
            _collect_host_status, self.settings, instances, str(storage_mode)
        )

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


def _read_cpu_times() -> tuple[int, int] | None:
    try:
        with open("/proc/stat", encoding="utf-8") as handle:
            first = handle.readline().split()
        if len(first) < 5 or first[0] != "cpu":
            return None
        nums = [int(v) for v in first[1:9]]
        idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
        return sum(nums), idle
    except (OSError, ValueError):
        return None


def _sample_host_cpu_percent() -> float:
    import time

    t1 = _read_cpu_times()
    if t1 is None:
        return 0.0
    time.sleep(0.12)
    t2 = _read_cpu_times()
    if t2 is None:
        return 0.0
    total_delta = t2[0] - t1[0]
    idle_delta = t2[1] - t1[1]
    if total_delta <= 0:
        return 0.0
    return max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0))


def _read_meminfo_bytes() -> tuple[int, int]:
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    info[parts[0].rstrip(":")] = int(parts[1]) * 1024
    except OSError:
        return 0, 0
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = max(0, total - avail) if total else 0
    return used, total


def _read_host_uptime() -> int:
    try:
        with open("/proc/uptime", encoding="utf-8") as handle:
            return max(0, int(float(handle.read().split()[0])))
    except (OSError, ValueError, IndexError):
        return 0


def _collect_host_status(
    settings: Settings, instances: list[Instance], storage_mode: str
) -> HostStatus:
    import os
    import socket

    hostname = socket.gethostname() or "noir-host"
    uptime_seconds = _read_host_uptime()
    cpu_cores = os.cpu_count() or 1
    cpu_percent = _sample_host_cpu_percent()
    try:
        load_1m = float(os.getloadavg()[0])
    except OSError:
        load_1m = 0.0
    ram_used_bytes, ram_total_bytes = _read_meminfo_bytes()
    disk_path = settings.data_dir if settings.data_dir.exists() else settings.data_dir.parent
    if not disk_path.exists():
        disk_path = settings.data_dir.anchor or "/"  # type: ignore[assignment]
    usage = shutil.disk_usage(disk_path)
    free_bytes = int(getattr(usage, "free", 0) or 0)
    total_bytes = int(getattr(usage, "total", free_bytes) or free_bytes)
    used_bytes = int(getattr(usage, "used", max(0, total_bytes - free_bytes)) or 0)
    running_vps = sum(1 for i in instances if i.status == "running")
    stopped_vps = sum(1 for i in instances if i.status == "stopped")
    return HostStatus(
        hostname=hostname,
        uptime_seconds=uptime_seconds,
        cpu_cores=cpu_cores,
        cpu_percent=cpu_percent,
        load_1m=load_1m,
        ram_used_bytes=ram_used_bytes,
        ram_total_bytes=ram_total_bytes,
        disk_used_bytes=max(0, used_bytes),
        disk_total_bytes=max(total_bytes, free_bytes),
        storage_mode=storage_mode,
        running_vps=running_vps,
        stopped_vps=stopped_vps,
        total_vps=len(instances),
        max_total_vps=settings.max_total,
        reserved_ram_gb=sum(i.ram_gb for i in instances),
        reserved_cpu=sum(i.cpu for i in instances),
        reserved_disk_gb=sum(i.disk_gb for i in instances),
    )
