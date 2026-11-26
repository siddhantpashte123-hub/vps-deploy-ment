from __future__ import annotations

import logging
import re

import discord
from discord.ext import commands

from .config import Settings
from .errors import UserError
from .manager import Manager
from .models import Actor, Specs
from .ui import (
    ConfirmView,
    DeployOSView,
    HostStatusView,
    ManageView,
    PanelHubView,
    UserDeleteView,
    confirm_embed,
    empty_embed,
    error_text,
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

    async def setup_hook(self) -> None:
        import asyncio

        await asyncio.to_thread(self.backend.preflight)
        await self.manager.reconcile()

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
        ctx: commands.Context, ram: int, cpu: int, disk: int, target: str | None = None
    ):
        """Admin-only: deploy for yourself or a mentioned member, then choose an OS."""
        actor = await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        if not actor.is_admin:
            raise UserError("Only server administrators can create VPSes.")
        specs = Specs(ram, cpu, disk)
        specs.validate(settings.max_ram_gb, settings.max_cpu, settings.max_disk_gb)
        if target is None:
            owner_id = actor.user_id
        else:
            target_id = mention_id(target)
            if target_id is None:
                raise UserError(
                    "Mention a member of this server, for example `!deploy 1 1 1 @user`."
                )
            owner = await resolve_member(ctx, target_id)
            if owner.bot:
                raise UserError("Cannot deploy a VPS for a bot account. Mention a human member.")
            owner_id = owner.id
        view = DeployOSView(bot, actor, specs, owner_id)
        message = await ctx.send(
            embed=os_choice_embed(specs, owner_id),
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

    @bot.command(name="ping")
    async def ping(ctx: commands.Context):
        await bot.resolve_actor(ctx.author, ctx.guild.id if ctx.guild else None)
        await ctx.send(
            f"Pong! Gateway latency: {bot.latency * 1000:.0f} ms",
            reference=ctx.message,
            mention_author=False,
        )

    return bot
