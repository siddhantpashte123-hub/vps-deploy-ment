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
from .models import (
    Actor,
    HostStatus,
    Instance,
    LiveStats,
    PortMapping,
    RedeemCode,
    Specs,
    clean_vps_name,
    future_iso,
    utc_now,
    valid_id,
    validate_ssh_pubkey,
)
from .os_catalog import DEFAULT_OS, os_choice
from .store import Store
from .templates import get_template

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

    async def list(self, actor: Actor, *, owner_id: int | None = None) -> list[Instance]:
        if owner_id is not None and not actor.is_admin and owner_id != actor.user_id:
            raise AccessDenied("Only admins can list another member's VPSes.")
        filter_owner = owner_id if actor.is_admin else actor.user_id
        return await asyncio.to_thread(self.store.list, actor.guild_id, filter_owner)

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

    async def _ports_dict(self, instance_id: str) -> dict[int, int]:
        mappings = await asyncio.to_thread(self.store.list_ports, instance_id)
        return {m.container_port: m.host_port for m in mappings}

    async def _apply_owner_ssh_key(self, instance_id: str, owner_id: int) -> None:
        pubkey = await asyncio.to_thread(self.store.get_ssh_key, owner_id)
        if pubkey:
            fn = getattr(self.backend, "write_ssh_key", None)
            if fn is not None:
                try:
                    await asyncio.to_thread(fn, instance_id, pubkey)
                except Exception:
                    log.debug("Could not write SSH key into %s", instance_id, exc_info=True)

    async def deploy(
        self,
        actor: Actor,
        specs: Specs,
        on_progress: ProgressCallback | None = None,
        *,
        owner_id: int | None = None,
        os_key: str = DEFAULT_OS,
        expires_at: str | None = None,
        allow_redeem: bool = False,
    ) -> Instance:
        if not actor.is_admin and not allow_redeem:
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
                expires_at=expires_at,
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
                await self._apply_owner_ssh_key(instance.id, owner_id)
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
            if instance.is_suspended and not actor.is_admin:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            if action == "reinstall":
                if instance.is_suspended:
                    raise UserError("Unsuspend this VPS before reinstalling it.")
                target_os = instance.os if os_key is None else os_key
                os_choice(target_os)
                await asyncio.to_thread(self.store.update, instance_id, status="reinstalling")
                try:
                    ports = await self._ports_dict(instance_id)
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
                        ports,
                    )
                    await self._apply_owner_ssh_key(instance_id, instance.owner_id)
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
            if instance.is_suspended and action in {"start", "restart"}:
                raise UserError("This VPS is suspended — run `!unsuspend` first.")
            if action == "start":
                await asyncio.to_thread(self.backend.start, instance_id)
            elif action == "stop":
                await asyncio.to_thread(self.backend.stop, instance_id)
            elif action == "restart":
                restart_fn = getattr(self.backend, "restart", None)
                if restart_fn is not None:
                    await asyncio.to_thread(restart_fn, instance_id)
                else:
                    await asyncio.to_thread(self.backend.stop, instance_id)
                    await asyncio.to_thread(self.backend.start, instance_id)
            else:
                raise ValueError("Unknown operation")
            status = await asyncio.to_thread(self.backend.status, instance_id)
            return await asyncio.to_thread(self.store.update, instance_id, status=status)

    async def start(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "start")

    async def stop(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "stop")

    async def restart(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "restart")

    async def reinstall(
        self, actor: Actor, instance_id: str, os_key: str | None = None
    ) -> Instance:
        return await self._operation(actor, instance_id, "reinstall", os_key)

    async def delete(self, actor: Actor, instance_id: str) -> Instance:
        return await self._operation(actor, instance_id, "delete")

    async def suspend(self, actor: Actor, instance_id: str, reason: str | None = None) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can suspend a VPS.")
        async with self._locks[instance_id]:
            await self.get(actor, instance_id)
            await asyncio.to_thread(self.backend.stop, instance_id)
            clean_reason = (reason or "").strip()[:160] or None
            return await asyncio.to_thread(
                self.store.update,
                instance_id,
                status="suspended",
                suspended_reason=clean_reason,
            )

    async def unsuspend(self, actor: Actor, instance_id: str) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can unsuspend a VPS.")
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            await asyncio.to_thread(self.backend.start, instance.id)
            status = await asyncio.to_thread(self.backend.status, instance.id)
            return await asyncio.to_thread(
                self.store.update,
                instance.id,
                status=status,
                suspended_reason=None,
            )

    async def resize(self, actor: Actor, instance_id: str, specs: Specs) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can resize a VPS.")
        specs.validate(self.settings.max_ram_gb, self.settings.max_cpu, self.settings.max_disk_gb)
        async with self._capacity_lock, self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if specs.disk_gb < instance.disk_gb:
                raise UserError(
                    f"Disk cannot shrink below {instance.disk_gb}GB without reinstalling."
                )
            _, total_ram, total_cpu = await asyncio.to_thread(self.store.totals)
            new_ram_total = total_ram - instance.ram_gb + specs.ram_gb
            new_cpu_total = total_cpu - instance.cpu + specs.cpu
            if new_ram_total > self.settings.host_ram_mb // 1024:
                raise UserError("Not enough unreserved host RAM for that plan.")
            if new_cpu_total > self.settings.host_cpu:
                raise UserError("Not enough unreserved host CPU capacity for that plan.")
            disk_delta_gb = specs.disk_gb - instance.disk_gb
            if disk_delta_gb > 0:
                free = await asyncio.to_thread(
                    lambda: shutil.disk_usage(self.settings.data_dir).free
                )
                if free < disk_delta_gb * 1024**3 + 1024**3:
                    raise UserError("Not enough free host disk space to expand this VPS.")
                resize_fn = getattr(self.backend, "resize_disk", None)
                if resize_fn is not None:
                    await asyncio.to_thread(resize_fn, instance_id, specs.disk_gb)
            was_running = instance.status == "running"
            was_suspended = instance.is_suspended
            ports = await self._ports_dict(instance_id)
            container_id = await asyncio.to_thread(
                self.backend.recreate_container,
                instance_id,
                specs,
                instance.owner_id,
                instance.guild_id,
                was_running and not was_suspended,
                instance.os,
                ports,
            )
            new_status = "suspended" if was_suspended else ("running" if was_running else "stopped")
            return await asyncio.to_thread(
                self.store.update,
                instance_id,
                ram_gb=specs.ram_gb,
                cpu=specs.cpu,
                disk_gb=specs.disk_gb,
                container_id=container_id,
                status=new_status,
            )

    async def renew(self, actor: Actor, instance_id: str, duration_seconds: int | None) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can change VPS expiry.")
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            new_expiry = (
                None
                if duration_seconds is None
                else future_iso(duration_seconds, base_iso=instance.expires_at)
            )
            fields: dict[str, str | int | None] = {
                "expires_at": new_expiry,
                "warned_24h": 0,
                "warned_1h": 0,
            }
            if (
                instance.is_suspended
                and instance.suspended_reason == "Expired"
                and (new_expiry is None or duration_seconds is not None)
            ):
                await asyncio.to_thread(self.backend.start, instance_id)
                fields["status"] = await asyncio.to_thread(self.backend.status, instance_id)
                fields["suspended_reason"] = None
            return await asyncio.to_thread(self.store.update, instance_id, **fields)

    async def transfer(self, actor: Actor, instance_id: str, new_owner_id: int) -> Instance:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can transfer a VPS.")
        async with self._capacity_lock, self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.owner_id == new_owner_id:
                return instance
            existing = await asyncio.to_thread(self.store.list, actor.guild_id, new_owner_id)
            if len(existing) >= self.settings.max_per_user:
                raise UserError(
                    f"<@{new_owner_id}> already has the maximum of "
                    f"{self.settings.max_per_user} VPSes."
                )
            return await asyncio.to_thread(self.store.update, instance_id, owner_id=new_owner_id)

    async def backup(self, actor: Actor, instance_id: str) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended and not actor.is_admin:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            fn = getattr(self.backend, "backup_home", None)
            if fn is None:
                raise BackendError("Backup is not supported by this backend.")
            await asyncio.to_thread(fn, instance_id)
            return await asyncio.to_thread(self.store.update, instance_id, backup_at=utc_now())

    async def restore(self, actor: Actor, instance_id: str) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended and not actor.is_admin:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            if not instance.backup_at:
                raise UserError("No backup snapshot exists yet. Click **Backup** first.")
            fn = getattr(self.backend, "restore_home", None)
            if fn is None:
                raise BackendError("Restore is not supported by this backend.")
            was_running = instance.status == "running"
            if was_running:
                await asyncio.to_thread(self.backend.stop, instance_id)
            await asyncio.to_thread(fn, instance_id)
            if was_running:
                await asyncio.to_thread(self.backend.start, instance_id)
            status = (
                "suspended"
                if instance.is_suspended
                else await asyncio.to_thread(self.backend.status, instance_id)
            )
            return await asyncio.to_thread(self.store.update, instance_id, status=status)

    async def sweep_expired(self) -> list[Instance]:
        """Auto-suspend any non-suspended VPS whose expires_at timestamp has passed."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        instances = await asyncio.to_thread(self.store.list)
        expired: list[Instance] = []
        for instance in instances:
            if not instance.expires_at or instance.status in {"suspended", "deleting"}:
                continue
            try:
                exp = datetime.fromisoformat(instance.expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if exp <= now:
                async with self._locks[instance.id]:
                    try:
                        await asyncio.to_thread(self.backend.stop, instance.id)
                    except Exception:
                        log.warning("Could not stop expired VPS %s", instance.id, exc_info=True)
                    updated = await asyncio.to_thread(
                        self.store.update,
                        instance.id,
                        status="suspended",
                        suspended_reason="Expired",
                    )
                    expired.append(updated)
        return expired

    async def sweep_expiry_warnings(self) -> list[tuple[Instance, str]]:
        """Return [(instance, '24h'|'1h')] once per threshold before expiry."""
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        instances = await asyncio.to_thread(self.store.list)
        alerts: list[tuple[Instance, str]] = []
        for instance in instances:
            if not instance.expires_at or instance.status in {"suspended", "deleting"}:
                continue
            try:
                exp = datetime.fromisoformat(instance.expires_at)
                if exp.tzinfo is None:
                    exp = exp.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            remaining = (exp - now).total_seconds()
            if remaining <= 0:
                continue
            if remaining <= 3600 and not instance.warned_1h:
                updated = await asyncio.to_thread(
                    self.store.update, instance.id, warned_1h=1, warned_24h=1
                )
                alerts.append((updated, "1h"))
            elif remaining <= 86400 and not instance.warned_24h:
                updated = await asyncio.to_thread(self.store.update, instance.id, warned_24h=1)
                alerts.append((updated, "24h"))
        return alerts

    async def rename(self, actor: Actor, instance_id: str, raw_name: str | None) -> Instance:
        cleaned = clean_vps_name(raw_name)
        async with self._locks[instance_id]:
            await self.get(actor, instance_id)
            return await asyncio.to_thread(self.store.update, instance_id, name=cleaned)

    async def list_ports(self, actor: Actor, instance_id: str) -> list[PortMapping]:
        await self.get(actor, instance_id)
        return await asyncio.to_thread(self.store.list_ports, instance_id)

    async def add_port(self, actor: Actor, instance_id: str, container_port: int) -> PortMapping:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended and not actor.is_admin:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            mapping = await asyncio.to_thread(
                self.store.allocate_port,
                instance.id,
                container_port,
                port_start=self.settings.port_start,
                port_end=self.settings.port_end,
                max_per_vps=self.settings.max_ports_per_vps,
            )
            ports = await self._ports_dict(instance.id)
            was_running = instance.status == "running"
            container_id = await asyncio.to_thread(
                self.backend.recreate_container,
                instance.id,
                instance.specs,
                instance.owner_id,
                instance.guild_id,
                was_running and not instance.is_suspended,
                instance.os,
                ports,
            )
            if was_running and not instance.is_suspended:
                await self._apply_owner_ssh_key(instance.id, instance.owner_id)
            await asyncio.to_thread(self.store.update, instance.id, container_id=container_id)
            return mapping

    async def remove_port(self, actor: Actor, instance_id: str, port_number: int) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended and not actor.is_admin:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            removed = await asyncio.to_thread(self.store.remove_port, instance.id, port_number)
            if not removed:
                raise UserError(f"Port `{port_number}` is not forwarded on `{instance.id}`.")
            ports = await self._ports_dict(instance.id)
            was_running = instance.status == "running"
            container_id = await asyncio.to_thread(
                self.backend.recreate_container,
                instance.id,
                instance.specs,
                instance.owner_id,
                instance.guild_id,
                was_running and not instance.is_suspended,
                instance.os,
                ports,
            )
            if was_running and not instance.is_suspended:
                await self._apply_owner_ssh_key(instance.id, instance.owner_id)
            return await asyncio.to_thread(
                self.store.update, instance.id, container_id=container_id
            )

    async def create_code(
        self,
        actor: Actor,
        specs: Specs,
        *,
        duration_seconds: int | None = None,
        max_uses: int = 1,
        custom_code: str | None = None,
    ) -> RedeemCode:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can generate redeem codes.")
        specs.validate(self.settings.max_ram_gb, self.settings.max_cpu, self.settings.max_disk_gb)
        return await asyncio.to_thread(
            self.store.create_code,
            actor.guild_id,
            specs,
            duration_seconds=duration_seconds,
            max_uses=max_uses,
            created_by=actor.user_id,
            custom_code=custom_code,
        )

    async def list_codes(self, actor: Actor) -> list[RedeemCode]:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can view redeem codes.")
        return await asyncio.to_thread(self.store.list_codes, actor.guild_id)

    async def delete_code(self, actor: Actor, raw_code: str) -> None:
        if not actor.is_admin:
            raise AccessDenied("Only server administrators can delete redeem codes.")
        ok = await asyncio.to_thread(self.store.delete_code, actor.guild_id, raw_code)
        if not ok:
            raise UserError(f"Code `{raw_code}` was not found.")

    async def validate_redeem(self, actor: Actor, raw_code: str) -> RedeemCode:
        mine = await asyncio.to_thread(self.store.list, actor.guild_id, actor.user_id)
        if len(mine) >= self.settings.max_per_user:
            raise UserError(f"You already have the maximum of {self.settings.max_per_user} VPSes.")
        return await asyncio.to_thread(
            self.store.check_code_redeemable, actor.guild_id, raw_code, actor.user_id
        )

    async def redeem_deploy(
        self,
        actor: Actor,
        raw_code: str,
        os_key: str,
        on_progress: ProgressCallback | None = None,
    ) -> Instance:
        promo = await self.validate_redeem(actor, raw_code)
        expires_at = (
            future_iso(promo.duration_seconds) if promo.duration_seconds is not None else None
        )
        instance = await self.deploy(
            actor,
            promo.specs,
            on_progress,
            owner_id=actor.user_id,
            os_key=os_key,
            expires_at=expires_at,
            allow_redeem=True,
        )
        try:
            await asyncio.to_thread(
                self.store.consume_code, actor.guild_id, promo.code, actor.user_id, instance.id
            )
        except Exception:
            await asyncio.to_thread(self.backend.remove_all, instance.id)
            await asyncio.to_thread(self.store.delete, instance.id)
            raise
        return instance

    async def exec_command(
        self, actor: Actor, instance_id: str, shell_cmd: str
    ) -> tuple[Instance, int, str]:
        cleaned = shell_cmd.strip()
        if not cleaned or len(cleaned) > 500:
            raise UserError("Command must be between 1 and 500 characters.")
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            fn = getattr(self.backend, "exec_command", None)
            if fn is None:
                raise BackendError("Quick exec is not supported by this backend.")
            code, output = await asyncio.to_thread(fn, instance.id, cleaned)
            return instance, code, output

    async def inspect_processes(self, actor: Actor, instance_id: str) -> tuple[Instance, str]:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            fn = getattr(self.backend, "inspect_processes", None)
            if fn is None:
                raise BackendError("Process inspection is not supported by this backend.")
            output = await asyncio.to_thread(fn, instance.id)
            return instance, output

    async def install_template(
        self, actor: Actor, instance_id: str, template_id: str
    ) -> tuple[Instance, str]:
        tmpl = get_template(template_id)
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            fn = getattr(self.backend, "exec_command", None)
            if fn is None:
                raise BackendError("Template installer is not supported by this backend.")
            code, output = await asyncio.to_thread(fn, instance.id, tmpl.install_script)
            if code != 0:
                raise BackendError(f"Template setup failed: {output[:200]}")
            return instance, tmpl.directory

    async def set_ssh_key(self, actor: Actor, raw_pubkey: str) -> tuple[str, int]:
        pubkey = validate_ssh_pubkey(raw_pubkey)
        await asyncio.to_thread(self.store.set_ssh_key, actor.user_id, pubkey)
        owned = await asyncio.to_thread(self.store.list, actor.guild_id, actor.user_id)
        applied = 0
        fn = getattr(self.backend, "write_ssh_key", None)
        if fn is not None:
            for inst in owned:
                if inst.status == "running":
                    try:
                        await asyncio.to_thread(fn, inst.id, pubkey)
                        applied += 1
                    except Exception:
                        log.debug("SSH key sync skipped for %s", inst.id, exc_info=True)
        return pubkey, applied

    async def clear_ssh_key(self, actor: Actor) -> bool:
        return await asyncio.to_thread(self.store.clear_ssh_key, actor.user_id)

    async def get_ssh_key(self, actor: Actor) -> str | None:
        return await asyncio.to_thread(self.store.get_ssh_key, actor.user_id)

    async def sshx(self, actor: Actor, instance_id: str) -> tuple[Instance, str]:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.is_suspended:
                raise UserError("This VPS is suspended — contact an admin to unsuspend it.")
            url = await asyncio.to_thread(self.backend.sshx, instance_id)
            return instance, url

    async def close_sshx(self, actor: Actor, instance_id: str) -> None:
        async with self._locks[instance_id]:
            await self.get(actor, instance_id)
            await asyncio.to_thread(self.backend.close_sshx, instance_id)

    async def refresh(self, actor: Actor, instance_id: str) -> Instance:
        async with self._locks[instance_id]:
            instance = await self.get(actor, instance_id)
            if instance.status in {"provisioning", "reinstalling", "deleting", "suspended"}:
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
                was_suspended = instance.is_suspended
                running = instance.status not in {"stopped", "suspended"}
                try:
                    ports = await self._ports_dict(instance.id)
                    container_id = await asyncio.to_thread(
                        self.backend.recreate_container,
                        instance.id,
                        instance.specs,
                        instance.owner_id,
                        instance.guild_id,
                        running,
                        instance.os,
                        ports,
                    )
                    if running:
                        await self._apply_owner_ssh_key(instance.id, instance.owner_id)
                    new_state = (
                        "suspended" if was_suspended else ("running" if running else "stopped")
                    )
                    await asyncio.to_thread(
                        self.store.update,
                        instance.id,
                        container_id=container_id,
                        status=new_state,
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
            elif instance.is_suspended:
                if states.get(instance.id) == "running":
                    try:
                        await asyncio.to_thread(self.backend.stop, instance.id)
                    except Exception:
                        log.warning(
                            "Could not stop suspended container %s", instance.id, exc_info=True
                        )
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
    suspended_vps = sum(1 for i in instances if i.status == "suspended")
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
        suspended_vps=suspended_vps,
    )
