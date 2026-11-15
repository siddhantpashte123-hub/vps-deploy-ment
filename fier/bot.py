from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import Settings
from .errors import UserError
from .manager import Manager
from .models import Actor, Specs
from .ui import COLOR, ConfirmView, ManageView, empty_embed, error_text, instance_embed

log = logging.getLogger(__name__)


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
            text = "Usage: `!deploy RAM CPU DISK` — for example `!deploy 1 1 1`."
        elif isinstance(error, (commands.BadArgument, commands.TooManyArguments)):
            text = "Use whole numbers: `!deploy 1 1 1`. Delete by ID or by your exact RAM CPU DISK."
        elif isinstance(error, commands.CommandOnCooldown):
            text = f"Please wait {error.retry_after:.0f}s before trying again."
        elif isinstance(error, (commands.NoPrivateMessage, commands.CheckFailure)):
            text = str(error) or "You do not have permission to use that command here."
        else:
            log.exception("Command failed", exc_info=original)
            text = error_text(original)
        await ctx.send(text, reference=ctx.message, mention_author=False)


def build_bot(settings: Settings, manager: Manager, backend) -> FierBot:
    bot = FierBot(settings, manager, backend)

    async def guild_check(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.NoPrivateMessage("Use FIER commands in a server.")
        if settings.allowed_guild_id and ctx.guild.id != settings.allowed_guild_id:
            raise commands.CheckFailure("This bot is restricted to another Discord server.")
        return True

    bot.add_check(guild_check)

    @bot.command(name="deploy")
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def deploy(ctx: commands.Context, ram: int, cpu: int, disk: int):
        """Deploy Ubuntu with RAM GiB, CPU cores, and disk GiB."""
        actor = bot.actor_for(ctx.author, ctx.guild.id)
        member = ctx.author
        allowed = actor.is_admin or (
            settings.deploy_role_id is not None
            and isinstance(member, discord.Member)
            and any(role.id == settings.deploy_role_id for role in member.roles)
        )
        if not allowed:
            raise UserError(
                "Deploying is restricted to server administrators"
                + (" and the configured deploy role." if settings.deploy_role_id else ".")
            )
        specs = Specs(ram, cpu, disk)
        specs.validate(settings.max_ram_gb, settings.max_cpu, settings.max_disk_gb)
        async with ctx.typing():
            instance = await manager.deploy(actor, specs)
        view = ManageView(bot, actor, [instance], instance.id)
        message = await ctx.send(
            f"✅ VPS deployed: `{instance.id}`",
            embed=instance_embed(instance),
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

    @bot.command(name="delete")
    @commands.cooldown(2, 10, commands.BucketType.user)
    async def delete(
        ctx: commands.Context,
        first: str | None = None,
        cpu: int | None = None,
        disk: int | None = None,
    ):
        """Delete by ID, or match YOUR VPS with RAM CPU DISK. Always confirms."""
        actor = bot.actor_for(ctx.author, ctx.guild.id)
        if first is None:
            instances = await manager.list(actor)
            view = ManageView(bot, actor, instances)
            message = await ctx.send(
                embed=instance_embed(instances[0]) if instances else empty_embed(),
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
                raise commands.BadArgument("Use `!delete 1 1 1` or `!delete <id>`.") from exc
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
            raise commands.BadArgument("Use `!delete <id>` or all three numbers: `!delete 1 1 1`.")
        view = ConfirmView(manager, actor, instance, "delete")
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
            description="Your own Ubuntu development containers, controlled from Discord.",
        )
        embed.add_field(
            name="Deploy", value="`!deploy 1 1 1`\nRAM GiB · CPU cores · disk GiB", inline=False
        )
        embed.add_field(
            name="Manage",
            value="`!manage` or `!manage <id>`\nStart · Stop · Reinstall · SSHX · Delete",
            inline=False,
        )
        embed.add_field(
            name="Delete",
            value=(
                "`!delete <id>` or `!delete 1 1 1`\n"
                "The numbers match only your VPS; confirmation is required."
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
        await ctx.send(embed=embed, reference=ctx.message, mention_author=False)

    @bot.command(name="ping")
    async def ping(ctx: commands.Context):
        await ctx.send(
            f"Pong! Gateway latency: {bot.latency * 1000:.0f} ms",
            reference=ctx.message,
            mention_author=False,
        )

    return bot
