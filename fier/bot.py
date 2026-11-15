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
    COLOR,
    ConfirmView,
    DeployOSView,
    ManageView,
    UserDeleteView,
    banner_file,
    empty_embed,
    error_text,
    instance_embed,
    os_choice_embed,
    user_delete_embed,
)

log = logging.getLogger(__name__)
MENTION_RE = re.compile(r"<@!?(\d{15,25})>\Z")


def mention_id(raw: str) -> int | None:
    match = MENTION_RE.fullmatch(raw.strip())
    return int(match.group(1)) if match else None


class FierBot(commands.Bot):
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

    def actor_for(self, user: discord.Member | discord.User, guild_id: int | None) -> Actor:
        if guild_id is None:
            raise UserError("Use FIER inside a Discord server, not in DMs.")
        is_admin = user.id in self.settings.admin_ids
        if isinstance(user, discord.Member):
            is_admin = is_admin or user.guild_permissions.administrator
        return Actor(user.id, guild_id, is_admin)

    async def setup_hook(self) -> None:
        import asyncio

        await asyncio.to_thread(self.backend.preflight)
        await self.manager.reconcile()

    async def on_ready(self) -> None:
        if not self._ready_logged:
            log.info("FIER_READY bot=%s id=%s", self.user, self.user.id if self.user else "unknown")
            self._ready_logged = True
        await self.change_presence(
            activity=discord.Activity(type=discord.ActivityType.watching, name="!deploy • !manage")
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
    member = ctx.guild.get_member(user_id)
    if member is None:
        member = discord.utils.get(ctx.message.mentions, id=user_id)
    if isinstance(member, discord.Member) and member.guild.id == ctx.guild.id:
        return member
    try:
        return await ctx.guild.fetch_member(user_id)
    except discord.NotFound as exc:
        raise UserError("That user is not a member of this server.") from exc
    except discord.HTTPException as exc:
        raise UserError("Could not look up that member. Try again in a moment.") from exc


def build_bot(settings: Settings, manager: Manager, backend) -> FierBot:
    bot = FierBot(settings, manager, backend)

    async def guild_check(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Use FIER commands in a server.")
        if settings.allowed_guild_id and ctx.guild.id != settings.allowed_guild_id:
            raise commands.CheckFailure("This bot is restricted to another Discord server.")
        return True

    bot.add_check(guild_check)

    @bot.command(name="deploy", ignore_extra=False)
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def deploy(
        ctx: commands.Context, ram: int, cpu: int, disk: int, target: str | None = None
    ):
        """Admin-only: deploy for yourself or a mentioned member, then choose an OS."""
        actor = bot.actor_for(ctx.author, ctx.guild.id)
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
            file=banner_file(),
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="manage")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def manage(ctx: commands.Context, instance_id: str | None = None):
        """Open your VPS panel: Start, Stop, Reinstall, SSHX, Delete."""
        actor = bot.actor_for(ctx.author, ctx.guild.id)
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
        view = ManageView(bot, actor, instances, selected)
        message = await ctx.send(
            embed=instance_embed(instance) if instance else empty_embed(),
            file=banner_file(),
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

    @bot.command(name="delete", ignore_extra=False)
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def delete(
        ctx: commands.Context,
        first: str | None = None,
        cpu: int | None = None,
        disk: int | None = None,
    ):
        """Delete by mention (admin picks one), by ID, or by your exact RAM CPU DISK."""
        actor = bot.actor_for(ctx.author, ctx.guild.id)
        if first is None:
            instances = await manager.list(actor)
            view = ManageView(bot, actor, instances)
            message = await ctx.send(
                embed=instance_embed(instances[0]) if instances else empty_embed(),
                file=banner_file(),
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
                file=banner_file(),
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
            f"⚠️ **Delete `{instance.id}`?** {instance.specs.label()}. "
            "All files and processes will be permanently destroyed.",
            view=view,
            reference=ctx.message,
            mention_author=False,
        )
        view.message = message

    @bot.command(name="help")
    async def help_command(ctx: commands.Context):
        embed = discord.Embed(
            title="FIER · VPS control",
            color=COLOR,
            description=(
                "Admin-created Ubuntu 24.04 / Debian 12 development containers, "
                "controlled from Discord."
            ),
        )
        embed.add_field(
            name="Deploy (admins only)",
            value=(
                "`!deploy 1 1 1 @user` — create for that member\n"
                "`!deploy 1 1 1` — create for yourself\n"
                "RAM GiB · CPU cores · disk GiB, then choose the OS."
            ),
            inline=False,
        )
        embed.add_field(
            name="Manage",
            value=(
                "`!manage` or `!manage <id>`\nStart · Stop · Reinstall (choose OS) · SSHX · Delete"
            ),
            inline=False,
        )
        embed.add_field(
            name="Delete",
            value=(
                "`!delete @user` — admin picks one of that member's VPSes, then confirms\n"
                "`!delete <id>` or `!delete 1 1 1` — delete your own matching VPS; "
                "confirmation required."
            ),
            inline=False,
        )
        embed.set_footer(
            text=(
                f"Per-user limit: {settings.max_per_user} • Max plan: "
                f"{settings.max_ram_gb} GiB RAM / {settings.max_cpu} CPU / "
                f"{settings.max_disk_gb} GiB disk"
            )
        )
        await ctx.send(embed=embed, file=banner_file(), reference=ctx.message, mention_author=False)

    @bot.command(name="ping")
    async def ping(ctx: commands.Context):
        await ctx.send(
            f"Pong! Gateway latency: {bot.latency * 1000:.0f} ms",
            reference=ctx.message,
            mention_author=False,
        )

    return bot
