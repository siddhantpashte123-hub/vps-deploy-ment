from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import Settings
from .errors import UserError
from .manager import Manager
from .models import DURATION_RE, Actor, Instance, Specs, future_iso, parse_duration_seconds
from .ui import (
    ACCENT,
    COLOR,
    DANGER,
    SUCCESS,
    ConfirmView,
    DeployOSView,
    FleetView,
    HostStatusView,
    ManageView,
    PanelHubView,
    UserDeleteView,
    action_card_embed,
    audit_embed,
    confirm_embed,
    empty_embed,
    error_text,
    fleet_embed,
    help_embed,
    host_status_embed,
    instance_embed,
    os_choice_embed,
    panel_hub_embed,
    user_delete_embed,
)

log = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@!?(\d{15,25})>\Z")


def mention_id(raw: str) -> int | None:
    match = MENTION_RE.fullmatch(raw.strip())
    return int(match.group(1)) if match else None


class NoirBot(commands.Bot):
    def __init__(self, settings: Settings, manager: Manager, backend):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
            case_insensitive=True,
        )
        self.settings = settings
        self.manager = manager
        self.backend = backend
        self._ready_logged = False

    def locked_guild(self) -> discord.Guild | None:
        allowed = self.settings.allowed_guild_id
        return self.get_guild(allowed) if allowed is not None else None

    def actor_for(self, user: discord.Member | discord.User, guild_id: int | None) -> Actor:
        allowed = self.settings.allowed_guild_id
        if allowed is None:
            raise UserError("NOIR_ALLOWED_GUILD_ID is not configured. Run setup again.")
        if guild_id is not None and guild_id != allowed:
            raise UserError("This bot is locked to one Discord server.")
        is_admin = user.id in self.settings.admin_ids
        if isinstance(user, discord.Member) and user.guild.id == allowed:
            is_admin = is_admin or user.guild_permissions.administrator
        elif guild_id is None:
            guild = self.locked_guild()
            cached = guild.get_member(user.id) if guild is not None else None
            if isinstance(cached, discord.Member):
                is_admin = is_admin or cached.guild_permissions.administrator
        return Actor(user.id, allowed, is_admin)

    async def resolve_actor(
        self, user: discord.Member | discord.User, guild_id: int | None
    ) -> Actor:
        allowed = self.settings.allowed_guild_id
        if allowed is None:
            raise UserError("NOIR_ALLOWED_GUILD_ID is not configured. Run setup again.")
        if guild_id is not None and guild_id != allowed:
            raise UserError("This bot is locked to one Discord server.")
        if isinstance(user, discord.Member) and user.guild.id == allowed:
            return self.actor_for(user, allowed)
        # Direct Message or uncached interaction: verify membership in the locked server
        # unless the user is an explicit host admin or already owns a VPS in the store.
        guild = self.locked_guild()
        member: discord.Member | None = None
        if guild is not None:
            member = guild.get_member(user.id)
            if not isinstance(member, discord.Member):
                try:
                    member = await guild.fetch_member(user.id)
                except discord.NotFound:
                    member = None
                except discord.HTTPException as exc:
                    raise UserError(
                        "Could not verify your membership in the server. Try again in a moment."
                    ) from exc
        if isinstance(member, discord.Member):
            return self.actor_for(member, allowed)
        if user.id in self.settings.admin_ids:
            return Actor(user.id, allowed, True)
        owned = await self.manager.list(Actor(user.id, allowed, False))
        if owned:
            return Actor(user.id, allowed, False)
        raise UserError(
            "Direct-message control is only available to members of NOIR's locked server."
        )

    async def audit_log(
        self,
        action: str,
        *,
        actor_id: int | None = None,
        instance: Instance | None = None,
        detail: str | None = None,
        color: int = COLOR,
    ) -> None:
        channel_id = self.settings.log_channel_id
        if not channel_id:
            return
        try:
            channel = self.get_channel(channel_id)
            if channel is None:
                channel = await self.fetch_channel(channel_id)
            if hasattr(channel, "send"):
                await channel.send(
                    embed=audit_embed(
                        action,
                        actor_id=actor_id,
                        instance=instance,
                        detail=detail,
                        color=color,
                    )
                )
        except Exception:
            log.warning("Could not deliver audit log to channel %s", channel_id, exc_info=True)

    @tasks.loop(seconds=60)
    async def _expiry_loop(self) -> None:
        try:
            expired = await self.manager.sweep_expired()
        except Exception:
            log.exception("Expiry sweep failed")
            return
        for inst in expired:
            await self.audit_log(
                "VPS Auto-Suspended (Expired)",
                actor_id=None,
                instance=inst,
                detail="Plan timer reached expiry; container stopped with `/home/noir` preserved",
                color=DANGER,
            )
            try:
                user = self.get_user(inst.owner_id) or await self.fetch_user(inst.owner_id)
                if user is not None:
                    dm = await user.create_dm()
                    await dm.send(
                        embed=action_card_embed(
                            "VPS Suspended (Expired)",
                            inst,
                            status_line="Suspended · Plan Expired",
                            detail_line=(
                                "**Files preserved** — your `/home/noir` files are safe. "
                                "Contact a server administrator to renew and unsuspend this VPS"
                            ),
                            color=DANGER,
                        )
                    )
            except Exception:
                log.debug("Could not DM owner %s about expired VPS %s", inst.owner_id, inst.id)

    async def setup_hook(self) -> None:
        await asyncio.to_thread(self.backend.preflight)
        await self.manager.reconcile()
        if not self._expiry_loop.is_running():
            self._expiry_loop.start()
        if self.settings.allowed_guild_id is not None:
            guild_obj = discord.Object(id=self.settings.allowed_guild_id)
            self.tree.copy_global_to(guild=guild_obj)
            try:
                await self.tree.sync(guild=guild_obj)
            except Exception:
                log.warning(
                    "Could not sync slash commands to guild %s", guild_obj.id, exc_info=True
                )

    async def close(self) -> None:
        if self._expiry_loop.is_running():
            self._expiry_loop.cancel()
        await super().close()

    async def _leave_guild(self, guild: discord.Guild) -> None:
        try:
            await guild.leave()
            log.warning("Left unauthorized guild id=%s name=%s", guild.id, guild.name)
        except discord.HTTPException:
            log.exception("Could not leave unauthorized guild id=%s", guild.id)

    async def on_guild_join(self, guild: discord.Guild) -> None:
        allowed = self.settings.allowed_guild_id
        if allowed is not None and guild.id != allowed:
            await self._leave_guild(guild)

    async def on_ready(self) -> None:
        allowed = self.settings.allowed_guild_id
        if allowed is not None:
            for guild in list(self.guilds):
                if guild.id != allowed:
                    await self._leave_guild(guild)
        if not self._ready_logged:
            log.info("NOIR_READY bot=%s id=%s", self.user, self.user.id if self.user else "unknown")
            self._ready_logged = True
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching, name="!manage • Server & DM"
            )
        )

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        original = getattr(error, "original", error)
        if isinstance(error, commands.MissingRequiredArgument):
            text = (
                "Usage: `!deploy RAM CPU DISK [@user]` — for example `!deploy 1 1 1 @user`. "
                "Admins only; omit the mention to deploy for yourself."
            )
        elif isinstance(error, commands.MemberNotFound):
            text = "Mention a member of this server, for example `!deploy 1 1 1 @user`."
        elif isinstance(error, (commands.BadArgument, commands.TooManyArguments)):
            text = (
                "Use whole numbers: `!deploy 1 1 1 [@user]`. "
                "Delete with `!delete @user`, `!delete <id>`, or your exact `!delete 1 1 1`."
            )
        elif isinstance(error, commands.CommandOnCooldown):
            text = f"Please wait {error.retry_after:.0f}s before trying again."
        elif isinstance(error, (commands.NoPrivateMessage, commands.CheckFailure)):
            text = str(error) or "You do not have permission to use that command here."
        else:
            log.exception("Command failed", exc_info=original)
            text = error_text(original)
        await ctx.send(text, reference=ctx.message, mention_author=False)


async def resolve_member(ctx: commands.Context, user_id: int) -> discord.Member:
    guild = ctx.guild or ctx.bot.locked_guild()
    if guild is None:
        raise UserError(
            "NOIR cannot reach its locked server right now. Run this command inside the server."
        )
    member = guild.get_member(user_id)
    if member is None:
        member = discord.utils.get(ctx.message.mentions, id=user_id)
    if isinstance(member, discord.Member) and member.guild.id == guild.id:
        return member
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound as exc:
        raise UserError("That user is not a member of this server.") from exc
    except discord.HTTPException as exc:
        raise UserError("Could not look up that member. Try again in a moment.") from exc


def build_bot(settings: Settings, manager: Manager, backend) -> NoirBot:
    if settings.allowed_guild_id is None:
        raise UserError(
            "NOIR_ALLOWED_GUILD_ID is required. Run the installer or `python -m noir setup` "
            "and enter the one server ID this bot may stay in."
        )
    bot = NoirBot(settings, manager, backend)

    async def guild_check(ctx: commands.Context) -> bool:
        if ctx.guild is not None and ctx.guild.id != settings.allowed_guild_id:
            raise commands.CheckFailure("This bot is restricted to another Discord server.")
        return True

    bot.add_check(guild_check)

    @bot.command(name="deploy", ignore_extra=False)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def deploy(
        ctx: commands.Context,
        ram: int,
        cpu: int,
        disk: int,
        arg1: str | None = None,
        arg2: str | None = None,
    ):
        """Admin-only: deploy for yourself or a member, with optional duration (e.g. 30d)."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can create VPSes.")
        specs = Specs(ram, cpu, disk)
        specs.validate(settings.max_ram_gb, settings.max_cpu, settings.max_disk_gb)
        expires_at: str | None = None
        if arg1 is None and arg2 is None:
            owner_id = actor.user_id
        elif arg2 is None:
            assert arg1 is not None
            if DURATION_RE.fullmatch(arg1.strip()):
                owner_id = actor.user_id
                expires_at = future_iso(parse_duration_seconds(arg1))
            else:
                target_id = mention_id(arg1)
                if target_id is None:
                    raise UserError(
                        "Mention a member of this server, for example `!deploy 1 1 1 @user [30d]`."
                    )
                owner = await resolve_member(ctx, target_id)
                if owner.bot:
                    raise UserError(
                        "Cannot deploy a VPS for a bot account. Mention a human member."
                    )
                owner_id = owner.id
        else:
            assert arg1 is not None
            target_id = mention_id(arg1)
            if target_id is None:
                raise UserError(
                    "Mention a member of this server, for example `!deploy 1 1 1 @user 30d`."
                )
            if not DURATION_RE.fullmatch(arg2.strip()):
                raise commands.TooManyArguments(
                    "Usage: `!deploy RAM CPU DISK [@user] [duration]` (e.g. `30d`, `12h`)."
                )
            expires_at = future_iso(parse_duration_seconds(arg2))
            owner = await resolve_member(ctx, target_id)
            if owner.bot:
                raise UserError("Cannot deploy a VPS for a bot account. Mention a human member.")
            owner_id = owner.id
        view = DeployOSView(bot, actor, specs, owner_id, expires_at=expires_at)
        message = await ctx.send(
            embed=os_choice_embed(specs, owner_id, expires_at=expires_at),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="manage")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def manage(ctx: commands.Context, instance_id: str | None = None):
        """Open your VPS panel in the server or in bot DMs."""
        in_dm = ctx.guild is None
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        instances = await manager.list(actor)
        selected = None
        if instance_id:
            instance = await manager.get(actor, instance_id)
            selected = instance.id
            if instance not in instances:
                instances.append(instance)
        elif instances:
            selected = instances[0].id
        instance = next((i for i in instances if i.id == selected), None)
        stats = await manager.stats(actor, instance.id) if instance is not None else None
        view = ManageView(bot, actor, instances, selected, in_dm=in_dm)
        message = await ctx.send(
            embed=(
                instance_embed(instance, stats, index=view.current_index())
                if instance
                else empty_embed()
            ),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message
        if len(instances) > 25:
            await ctx.send(
                "The selector shows the first 25 VPSes. Use `!manage <id>` for any other VPS.",
                reference=ctx.message,
                mention_author=False,
            )

    @bot.command(name="panel")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def panel(ctx: commands.Context):
        """Post an interactive NOIR Cloud Panel card (or open your DM console in DMs)."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if ctx.guild is None:
            instances = await manager.list(actor)
            selected = instances[0].id if instances else None
            stats = await manager.stats(actor, instances[0].id) if instances else None
            view = ManageView(bot, actor, instances, selected, in_dm=True)
            message = await ctx.send(
                embed=(
                    instance_embed(instances[0], stats, index=1) if instances else empty_embed()
                ),
                view=view,
                reference=ctx.message,
                mention_author=False,
            )
            view.message = message
            return
        if not actor.is_admin:
            raise UserError("Only server administrators can post the server cloud panel.")
        fleet = [i for i in bot.manager.store.list() if i.guild_id == actor.guild_id]
        guild_name = getattr(ctx.guild, "name", None) or "Private Cloud"
        await ctx.send(
            embed=panel_hub_embed(guild_name, len(fleet)),
            view=PanelHubView(bot),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="status")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def status_command(ctx: commands.Context):
        """Admin-only: open the live Host Node resource dashboard."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can view the host status dashboard.")
        host = await manager.host_status(actor)
        view = HostStatusView(bot, actor)
        message = await ctx.send(
            embed=host_status_embed(host),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="delete", ignore_extra=False)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def delete(
        ctx: commands.Context,
        first: str | None = None,
        cpu: int | None = None,
        disk: int | None = None,
    ):
        """Delete by mention (admin picks one), by ID, or by your exact RAM CPU DISK."""
        in_dm = ctx.guild is None
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if first is None:
            instances = await manager.list(actor)
            stats = await manager.stats(actor, instances[0].id) if instances else None
            view = ManageView(bot, actor, instances, in_dm=in_dm)
            message = await ctx.send(
                embed=(
                    instance_embed(instances[0], stats, index=1) if instances else empty_embed()
                ),
                view=view,
                reference=ctx.message,
                mention_author=False,
            )
            view.message = message
            return
        target_id = mention_id(first)
        if target_id is not None:
            if cpu is not None or disk is not None:
                raise commands.BadArgument("Use `!delete @user` without resource numbers.")
            if not actor.is_admin:
                raise UserError("Only admins can delete another member's VPS by mention.")
            member = await resolve_member(ctx, target_id)
            instances = await manager.list_for(actor, member.id)
            if not instances:
                raise UserError(f"<@{member.id}> has no VPSes in this server.")
            view = UserDeleteView(bot, actor, member.id, instances)
            message = await ctx.send(
                embed=user_delete_embed(member.id, instances, view.page),
                view=view,
                reference=ctx.message,
                mention_author=False,
            )
            view.message = message
            return
        if cpu is None and disk is None:
            instance = await manager.get(actor, first)
        elif cpu is not None and disk is not None:
            try:
                specs = Specs(int(first), cpu, disk)
            except ValueError as exc:
                raise commands.BadArgument(
                    "Use `!delete @user`, `!delete <id>`, or `!delete 1 1 1`."
                ) from exc
            matches = await manager.matching_own(actor, specs)
            if not matches:
                raise UserError(
                    "You have no VPS with those exact resources. Use `!manage` to see your IDs."
                )
            if len(matches) > 1:
                ids = "\n".join(f"• `{i.id}`" for i in matches)
                raise UserError(
                    "Several of your VPSes match. Nothing was deleted. Choose one:\n"
                    f"{ids}\nUse `!delete <id>`."
                )
            instance = matches[0]
        else:
            raise commands.BadArgument(
                "Use `!delete @user`, `!delete <id>`, or all three numbers: `!delete 1 1 1`."
            )
        view = ConfirmView(manager, actor, instance, "delete", bot=bot)
        message = await ctx.send(
            embed=confirm_embed(instance, "delete"),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        await ctx.send(
            embed=help_embed(settings),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="list", aliases=["vpslist"], ignore_extra=False)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def list_command(ctx: commands.Context, target: str | None = None):
        """Admin-only: paginated fleet browser across all server VPSes or a specific member."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can browse the server VPS fleet.")
        owner_id: int | None = None
        if target is not None:
            tid = mention_id(target)
            if tid is None:
                raise UserError("Mention a member to filter, for example `!list @user`.")
            member = await resolve_member(ctx, tid)
            owner_id = member.id
        instances = await manager.list(actor, owner_id=owner_id)
        view = FleetView(bot, actor, instances, owner_id=owner_id)
        message = await ctx.send(
            embed=fleet_embed(instances, view.page, owner_id=owner_id),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="suspend")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def suspend_command(
        ctx: commands.Context, instance_id: str, *, reason: str | None = None
    ):
        """Admin-only: suspend a VPS while preserving /home/noir."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can suspend a VPS.")
        instance = await manager.suspend(actor, instance_id, reason)
        reason_text = f" (`{instance.suspended_reason}`)" if instance.suspended_reason else ""
        await bot.audit_log(
            "VPS Suspended",
            actor_id=actor.user_id,
            instance=instance,
            detail=instance.suspended_reason or "Manual admin suspension",
            color=DANGER,
        )
        await ctx.send(
            embed=action_card_embed(
                "VPS Suspended",
                instance,
                status_line=f"Suspended · Locked{reason_text}",
                detail_line=(
                    "**Container stopped** — `/home/noir` files remain intact; "
                    "run `!unsuspend` to restore access"
                ),
                color=DANGER,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="unsuspend")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def unsuspend_command(ctx: commands.Context, instance_id: str):
        """Admin-only: unsuspend and start a suspended VPS."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can unsuspend a VPS.")
        instance = await manager.unsuspend(actor, instance_id)
        await bot.audit_log(
            "VPS Unsuspended",
            actor_id=actor.user_id,
            instance=instance,
            color=SUCCESS,
        )
        await ctx.send(
            embed=action_card_embed(
                "VPS Unsuspended",
                instance,
                status_line="Running · Unlocked",
                detail_line="**Access restored** — the VPS container is back online",
                color=SUCCESS,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="resize", ignore_extra=False)
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def resize_command(
        ctx: commands.Context, instance_id: str, ram: int, cpu: int, disk: int
    ):
        """Admin-only: live plan change without wiping /home/noir."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can resize a VPS.")
        specs = Specs(ram, cpu, disk)
        instance = await manager.resize(actor, instance_id, specs)
        await bot.audit_log(
            "VPS Resized",
            actor_id=actor.user_id,
            instance=instance,
            detail=f"Updated plan to {specs.label()}",
            color=ACCENT,
        )
        await ctx.send(
            embed=action_card_embed(
                "VPS Plan Updated",
                instance,
                status_line=f"{instance.status.capitalize()} · Plan Applied",
                detail_line=(
                    f"**Live resize complete** — `{specs.label()}` applied "
                    "with `/home/noir` preserved"
                ),
                color=ACCENT,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="renew", ignore_extra=False)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def renew_command(ctx: commands.Context, instance_id: str, duration: str):
        """Admin-only: extend VPS expiry (e.g. 30d, 12h) or clear it with 'never'."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can renew a VPS.")
        cleaned = duration.strip().lower()
        seconds = (
            None
            if cleaned in {"never", "none", "off", "permanent"}
            else parse_duration_seconds(cleaned)
        )
        instance = await manager.renew(actor, instance_id, seconds)
        detail = "Expiry removed (permanent)" if seconds is None else f"Extended by `{cleaned}`"
        await bot.audit_log(
            "VPS Renewed",
            actor_id=actor.user_id,
            instance=instance,
            detail=detail,
            color=SUCCESS,
        )
        await ctx.send(
            embed=action_card_embed(
                "VPS Expiry Updated",
                instance,
                status_line=f"{instance.status.capitalize()} · {detail}",
                detail_line="**Timer updated** — automatic expiry schedule saved",
                color=SUCCESS,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="transfer", ignore_extra=False)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def transfer_command(ctx: commands.Context, instance_id: str, target: str):
        """Transfer VPS ownership to another server member (owner or admin)."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        instance = await manager.get(actor, instance_id)
        if not actor.is_admin and instance.owner_id != actor.user_id:
            raise UserError("You can only transfer a VPS that you own.")
        tid = mention_id(target)
        if tid is None:
            raise UserError("Mention a member of this server, for example `!transfer <id> @user`.")
        member = await resolve_member(ctx, tid)
        if member.bot:
            raise UserError("Cannot transfer a VPS to a bot account. Mention a human member.")
        # Allow owner-initiated transfer via elevated call after ownership & membership checks
        transfer_actor = Actor(actor.user_id, actor.guild_id, True)
        updated = await manager.transfer(transfer_actor, instance.id, member.id)
        await bot.audit_log(
            "VPS Transferred",
            actor_id=actor.user_id,
            instance=updated,
            detail=f"Transferred from <@{instance.owner_id}> to <@{member.id}>",
            color=ACCENT,
        )
        await ctx.send(
            embed=action_card_embed(
                "VPS Transferred",
                updated,
                status_line="Ownership Updated",
                detail_line=f"**New owner** — <@{member.id}> can now manage this VPS via `!manage`",
                color=ACCENT,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="backup", ignore_extra=False)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def backup_command(ctx: commands.Context, instance_id: str):
        """Create a compressed /home/noir snapshot for your VPS."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        instance = await manager.backup(actor, instance_id)
        await bot.audit_log(
            "Backup Created",
            actor_id=actor.user_id,
            instance=instance,
            color=SUCCESS,
        )
        await ctx.send(
            embed=action_card_embed(
                "Backup Snapshot Saved",
                instance,
                status_line="Snapshot Ready · `/home/noir` Archived",
                detail_line=(
                    "**Saved** — run `!restore <id>` or click **Restore** in `!manage` anytime"
                ),
                color=SUCCESS,
            ),
            reference=ctx.message,
            mention_author=False,
        )

    @bot.command(name="restore", ignore_extra=False)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def restore_command(ctx: commands.Context, instance_id: str):
        """Restore the latest /home/noir snapshot (opens confirmation)."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        instance = await manager.get(actor, instance_id)
        if not instance.backup_at:
            raise UserError("No backup snapshot exists yet. Run `!backup <id>` first.")
        view = ConfirmView(manager, actor, instance, "restore", bot=bot)
        message = await ctx.send(
            embed=confirm_embed(instance, "restore"),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="ping")
    async def ping(ctx: commands.Context):
        await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        await ctx.send(
            f"Pong! Gateway latency: {bot.latency * 1000:.0f} ms",
            reference=ctx.message,
            mention_author=False,
        )

    _register_slash_commands(bot, settings, manager)
    return bot


def _register_slash_commands(bot: NoirBot, settings: Settings, manager: Manager) -> None:
    @bot.tree.command(name="manage", description="Open your NOIR VPS control panel")
    @app_commands.describe(instance_id="Optional VPS ID (e.g. noir-a1b2c3d4e5f6)")
    async def slash_manage(interaction: discord.Interaction, instance_id: str | None = None):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            instances = await manager.list(actor)
            selected = None
            if instance_id:
                inst = await manager.get(actor, instance_id)
                selected = inst.id
                if inst not in instances:
                    instances.append(inst)
            elif instances:
                selected = instances[0].id
            inst = next((i for i in instances if i.id == selected), None)
            stats = await manager.stats(actor, inst.id) if inst is not None else None
            in_dm = interaction.guild_id is None
            view = ManageView(bot, actor, instances, selected, in_dm=in_dm)
            await interaction.response.send_message(
                embed=(
                    instance_embed(inst, stats, index=view.current_index())
                    if inst
                    else empty_embed()
                ),
                view=view,
                ephemeral=not in_dm,
            )
            view.message = await interaction.original_response()
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(
        name="status", description="Admin: live Host Node CPU/RAM/Disk/Fleet dashboard"
    )
    async def slash_status(interaction: discord.Interaction):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            if not actor.is_admin:
                raise UserError("Only server administrators can view the host status dashboard.")
            host = await manager.host_status(actor)
            view = HostStatusView(bot, actor)
            await interaction.response.send_message(
                embed=host_status_embed(host), view=view, ephemeral=True
            )
            view.message = await interaction.original_response()
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(name="list", description="Admin: browse all provisioned VPSes")
    @app_commands.describe(member="Optional member filter")
    async def slash_list(interaction: discord.Interaction, member: discord.Member | None = None):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            if not actor.is_admin:
                raise UserError("Only server administrators can browse the server VPS fleet.")
            owner_id = member.id if member is not None else None
            instances = await manager.list(actor, owner_id=owner_id)
            view = FleetView(bot, actor, instances, owner_id=owner_id)
            await interaction.response.send_message(
                embed=fleet_embed(instances, view.page, owner_id=owner_id),
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(name="panel", description="Admin: post the NOIR Cloud Panel hub card")
    async def slash_panel(interaction: discord.Interaction):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            if interaction.guild is None:
                instances = await manager.list(actor)
                selected = instances[0].id if instances else None
                stats = await manager.stats(actor, instances[0].id) if instances else None
                view = ManageView(bot, actor, instances, selected, in_dm=True)
                await interaction.response.send_message(
                    embed=(
                        instance_embed(instances[0], stats, index=1) if instances else empty_embed()
                    ),
                    view=view,
                )
                view.message = await interaction.original_response()
                return
            if not actor.is_admin:
                raise UserError("Only server administrators can post the server cloud panel.")
            fleet = [i for i in bot.manager.store.list() if i.guild_id == actor.guild_id]
            guild_name = getattr(interaction.guild, "name", None) or "Private Cloud"
            await interaction.response.send_message(
                embed=panel_hub_embed(guild_name, len(fleet)),
                view=PanelHubView(bot),
            )
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(name="deploy", description="Admin: deploy a new root VPS")
    @app_commands.describe(
        ram="RAM in GiB",
        cpu="vCPU cores",
        disk="Disk in GiB",
        member="Optional owner (defaults to you)",
        duration="Optional expiry timer (e.g. 30d, 12h)",
    )
    async def slash_deploy(
        interaction: discord.Interaction,
        ram: int,
        cpu: int,
        disk: int,
        member: discord.Member | None = None,
        duration: str | None = None,
    ):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            if not actor.is_admin:
                raise UserError("Only server administrators can create VPSes.")
            specs = Specs(ram, cpu, disk)
            specs.validate(settings.max_ram_gb, settings.max_cpu, settings.max_disk_gb)
            if member is not None and member.bot:
                raise UserError("Cannot deploy a VPS for a bot account. Mention a human member.")
            owner_id = member.id if member is not None else actor.user_id
            expires_at = future_iso(parse_duration_seconds(duration)) if duration else None
            view = DeployOSView(bot, actor, specs, owner_id, expires_at=expires_at)
            await interaction.response.send_message(
                embed=os_choice_embed(specs, owner_id, expires_at=expires_at),
                view=view,
            )
            view.message = await interaction.original_response()
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(name="suspend", description="Admin: suspend a VPS")
    async def slash_suspend(
        interaction: discord.Interaction, instance_id: str, reason: str | None = None
    ):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            instance = await manager.suspend(actor, instance_id, reason)
            await bot.audit_log(
                "VPS Suspended",
                actor_id=actor.user_id,
                instance=instance,
                detail=instance.suspended_reason or "Manual admin suspension",
                color=DANGER,
            )
            await interaction.response.send_message(
                embed=action_card_embed(
                    "VPS Suspended",
                    instance,
                    status_line="Suspended · Locked",
                    detail_line="**Container stopped** — `/home/noir` files remain intact",
                    color=DANGER,
                )
            )
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)

    @bot.tree.command(name="unsuspend", description="Admin: unsuspend a VPS")
    async def slash_unsuspend(interaction: discord.Interaction, instance_id: str):
        try:
            actor = await bot.resolve_actor(interaction.user, interaction.guild_id)
            instance = await manager.unsuspend(actor, instance_id)
            await bot.audit_log(
                "VPS Unsuspended",
                actor_id=actor.user_id,
                instance=instance,
                color=SUCCESS,
            )
            await interaction.response.send_message(
                embed=action_card_embed(
                    "VPS Unsuspended",
                    instance,
                    status_line="Running · Unlocked",
                    detail_line="**Access restored** — the VPS container is back online",
                    color=SUCCESS,
                )
            )
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
