from __future__ import annotations

import logging
import time
from pathlib import Path

import discord

from .errors import FierError, UserError
from .models import Actor, Instance, Specs
from .os_catalog import DEFAULT_OS, OS_CHOICES, os_choice, os_label

log = logging.getLogger(__name__)

COLOR = 0x8B5CF6
ACCENT = 0x22D3EE
SUCCESS = 0x10B981
DANGER = 0xEF4444
MUTED = 0x64748B
STATUS_ICON = {
    "running": "🟢",
    "stopped": "⚪",
    "error": "🔴",
    "provisioning": "🚀",
    "reinstalling": "♻️",
    "deleting": "🗑️",
}
BANNER_PATH = Path(__file__).resolve().parent.parent / "assets" / "fier-banner.png"
BANNER_FILENAME = "fier-banner.png"

DEPLOY_STAGES = [
    ("validate", "Validate plan", 5),
    ("reserve", "Reserve resources", 15),
    ("storage", "Prepare persistent disk", 50),
    ("container", "Create root container", 60),
    ("boot", "Boot & health-check", 85),
    ("ready", "Finalize inventory", 98),
    ("online", "Bring online", 100),
]


def error_text(exc: Exception) -> str:
    if isinstance(exc, UserError):
        return str(exc)
    if isinstance(exc, FierError):
        return (
            "The host could not complete that action. Try again or contact the host administrator."
        )
    return "Something went wrong. The host administrator can check `journalctl -u fier`."


def banner_file() -> discord.File | None:
    if BANNER_PATH.is_file():
        return discord.File(BANNER_PATH, filename=BANNER_FILENAME)
    return None


def _brand(embed: discord.Embed) -> discord.Embed:
    embed.set_footer(text="FIER • Private Cloud Console")
    if BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{BANNER_FILENAME}")
    return embed


def instance_embed(instance: Instance) -> discord.Embed:
    icon = STATUS_ICON.get(instance.status, "🟡")
    color = (
        SUCCESS if instance.status == "running" else DANGER if instance.status == "error" else COLOR
    )
    embed = discord.Embed(
        title=f"⚡ FIER · {instance.id}",
        description=(
            f"{icon} **{instance.status.upper()}**  ·  `{os_label(instance.os)}`\n"
            "🔐 **Root terminal** · persistent home · isolated container"
        ),
        color=color,
    )
    embed.add_field(name="🧠 Memory", value=f"**{instance.ram_gb}** GiB", inline=True)
    embed.add_field(name="⚙️ Compute", value=f"**{instance.cpu}** vCPU", inline=True)
    embed.add_field(name="💾 Storage", value=f"**{instance.disk_gb}** GiB", inline=True)
    embed.add_field(name="👤 Owner", value=f"<@{instance.owner_id}>", inline=True)
    embed.add_field(name="🌐 Network", value="Private bridge", inline=True)
    embed.add_field(
        name="🕒 Updated", value=f"<t:{int(_timestamp(instance.updated_at))}:R>", inline=True
    )
    embed.add_field(
        name="💻 Terminal",
        value=(
            "Press **SSHX** for a private **root** browser terminal. No public SSH port is exposed."
        ),
        inline=False,
    )
    embed.set_footer(text="Start/Stop preserve files • Reinstall/Delete destroy files")
    if BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{BANNER_FILENAME}")
    return embed


def _timestamp(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def empty_embed() -> discord.Embed:
    return _brand(
        discord.Embed(
            title="⚡ FIER · Your private cloud",
            description=(
                "No VPSes yet. An admin can launch your first root instance:\n"
                "```text\n!deploy 1 1 1 @user\n```\n"
                "`RAM GiB` · `CPU cores` · `Disk GiB` · then choose Ubuntu 24.04 or Debian 12"
            ),
            color=COLOR,
        )
    )


def user_delete_embed(owner_id: int, instances: list[Instance], page: int = 0) -> discord.Embed:
    page_size = 25
    pages = max(1, (len(instances) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    chunk = instances[page * page_size : (page + 1) * page_size]
    lines = [f"• `{i.id}` · {os_label(i.os)} · {i.specs.label()} · {i.status}" for i in chunk]
    embed = discord.Embed(
        title="🗑️ FIER · Delete a member's VPS",
        description=(
            f"Admin selection for <@{owner_id}>. Choose **one** VPS below, then confirm. "
            "Nothing is deleted until you confirm.\n\n" + "\n".join(lines)
        ),
        color=DANGER,
    )
    embed.set_footer(text=f"FIER • Page {page + 1}/{pages} • Confirmation required")
    if BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{BANNER_FILENAME}")
    return embed


def _bar(percent: int, width: int = 12) -> str:
    percent = max(0, min(100, percent))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def deploy_progress_embed(
    specs: Specs,
    stage: str,
    percent: int,
    os_key: str = DEFAULT_OS,
    owner_id: int | None = None,
) -> discord.Embed:
    percent = max(0, min(100, percent))
    current = next((label for key, label, _ in DEPLOY_STAGES if key == stage), "Working")
    pipeline = []
    for key, label, threshold in DEPLOY_STAGES:
        if percent >= 100 or threshold < percent:
            pipeline.append(f"✅ {label}")
        elif key == stage:
            pipeline.append(f"🔄 **{label}**")
        else:
            pipeline.append(f"⬜ {label}")
    embed = discord.Embed(
        title="🚀 FIER · Deploying your VPS",
        description=(
            f"`{_bar(percent)}` **{percent}%**\n"
            f"Current phase: **{current}**\n\n" + "\n".join(pipeline)
        ),
        color=ACCENT if percent < 100 else SUCCESS,
    )
    embed.add_field(name="📦 Plan", value=specs.label(), inline=False)
    embed.add_field(name="🔐 Access", value="Root SSHX terminal", inline=True)
    embed.add_field(name="🧩 OS", value=os_label(os_key), inline=True)
    if owner_id is not None:
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}>", inline=True)
    return _brand(embed)


def deploy_failure_embed(
    specs: Specs, message: str, os_key: str = DEFAULT_OS, owner_id: int | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title="🛑 FIER · Deployment failed",
        description=message,
        color=DANGER,
    )
    embed.add_field(name="📦 Plan", value=specs.label(), inline=False)
    embed.add_field(name="🧩 OS", value=os_label(os_key), inline=True)
    if owner_id is not None:
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}>", inline=True)
    return _brand(embed)


def os_choice_embed(
    specs: Specs, owner_id: int | None = None, *, reinstall: bool = False
) -> discord.Embed:
    action = "Reinstall" if reinstall else "Deploy"
    embed = discord.Embed(
        title=f"🧩 FIER · Choose an OS to {action.lower()}",
        description=(
            "Select the operating system for this root VPS. "
            "Both images include Python, Node.js, Git, build tools, and SSHX."
            + ("\n\n⚠️ Reinstalling **permanently erases all files**." if reinstall else "")
        ),
        color=ACCENT,
    )
    embed.add_field(name="📦 Plan", value=specs.label(), inline=False)
    if owner_id is not None:
        embed.add_field(name="👤 Owner", value=f"<@{owner_id}>", inline=True)
    embed.add_field(
        name="🧩 Available OS",
        value="\n".join(f"{choice.emoji} **{choice.label}**" for choice in OS_CHOICES),
        inline=False,
    )
    return _brand(embed)


def _os_select(placeholder: str, selected: str | None = None) -> discord.ui.Select:
    options = [
        discord.SelectOption(
            label=choice.label,
            value=choice.id,
            description=f"Root VPS · {choice.id}",
            emoji=choice.emoji,
            default=choice.id == selected,
        )
        for choice in OS_CHOICES
    ]
    return discord.ui.Select(placeholder=placeholder, options=options, min_values=1, max_values=1)


class DeployOSView(discord.ui.View):
    """Admin picks the OS, then the live deploy meter replaces this message."""

    def __init__(self, bot, actor: Actor, specs: Specs, owner_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.actor = actor
        self.specs = specs
        self.owner_id = owner_id
        self.handled = False
        self.message: discord.Message | None = None
        self.select = _os_select("Choose an OS to deploy…")
        self.select.callback = self._choose  # type: ignore[method-assign]
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        actor = self.bot.actor_for(interaction.user, interaction.guild_id)
        if (
            actor.user_id != self.actor.user_id
            or actor.guild_id != self.actor.guild_id
            or not actor.is_admin
        ):
            await interaction.response.send_message(
                "Only the admin who started this deploy can choose its OS.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    async def _choose(self, interaction: discord.Interaction) -> None:
        if self.handled:
            await interaction.response.send_message("This deploy already started.", ephemeral=True)
            return
        values = interaction.data.get("values", [])  # type: ignore[union-attr]
        try:
            choice = os_choice(values[0] if values else "")
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.handled = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        await interaction.response.edit_message(
            embed=deploy_progress_embed(self.specs, "validate", 5, choice.id, self.owner_id),
            view=self,
        )
        self.stop()
        message = self.message or interaction.message
        if message is None:
            return
        last_edit = 0.0

        async def on_progress(stage: str, percent: int) -> None:
            nonlocal last_edit
            now = time.monotonic()
            if percent < 100 and now - last_edit < 1.5:
                return
            last_edit = now
            await message.edit(
                embed=deploy_progress_embed(self.specs, stage, percent, choice.id, self.owner_id)
            )

        try:
            instance = await self.bot.manager.deploy(
                self.actor, self.specs, on_progress, owner_id=self.owner_id, os_key=choice.id
            )
        except Exception as exc:
            log.exception("Deploy command failed")
            await message.edit(
                embed=deploy_failure_embed(self.specs, error_text(exc), choice.id, self.owner_id),
                view=None,
            )
            return
        view = ManageView(self.bot, self.actor, [instance], instance.id)
        await message.edit(
            content=f"✅ VPS deployed for <@{self.owner_id}>: `{instance.id}`",
            embed=instance_embed(instance),
            view=view,
        )
        view.message = message

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ReinstallOSView(discord.ui.View):
    """Pick the replacement OS, then confirm the destructive reinstall."""

    def __init__(self, bot, actor: Actor, instance: Instance):
        super().__init__(timeout=180)
        self.bot = bot
        self.actor = actor
        self.instance = instance
        self.handled = False
        self.message: discord.Message | None = None
        self.select = _os_select("Choose the new OS…", instance.os)
        self.select.callback = self._choose  # type: ignore[method-assign]
        self.add_item(self.select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        actor = self.bot.actor_for(interaction.user, interaction.guild_id)
        if actor.user_id != self.actor.user_id or actor.guild_id != self.actor.guild_id:
            await interaction.response.send_message(
                "This reinstall choice is not yours.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    async def _choose(self, interaction: discord.Interaction) -> None:
        if self.handled:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        values = interaction.data.get("values", [])  # type: ignore[union-attr]
        try:
            choice = os_choice(values[0] if values else "")
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        try:
            instance = await self.bot.manager.get(self.actor, self.instance.id)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        self.handled = True
        self.stop()
        view = ConfirmView(
            self.bot.manager, self.actor, instance, "reinstall", os_key=choice.id, bot=self.bot
        )
        await interaction.response.edit_message(
            content=(
                f"♻️ **Wipe `{instance.id}` and reinstall {choice.label}?** "
                "This permanently erases its files, installed packages, and running processes, "
                "then creates a fresh root container with the same resources."
            ),
            embed=None,
            attachments=[],
            view=view,
        )
        view.message = await interaction.original_response()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmView(discord.ui.View):
    def __init__(
        self,
        manager,
        actor: Actor,
        instance: Instance,
        action: str,
        os_key: str | None = None,
        bot=None,
    ):
        super().__init__(timeout=120)
        self.manager = manager
        self.actor = actor
        self.instance = instance
        self.action = action
        self.os_key = os_key
        self.bot = bot
        self.handled = False
        self.message: discord.Message | None = None
        self.confirm.label = "Delete permanently" if action == "delete" else "Wipe & reinstall"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        actor = self.actor
        if self.bot is not None:
            actor = self.bot.actor_for(interaction.user, interaction.guild_id)
        if actor.user_id != self.actor.user_id or actor.guild_id != self.actor.guild_id:
            await interaction.response.send_message(
                "This confirmation is not yours.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    async def _finish(self, interaction: discord.Interaction, text: str) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            if self.message:
                await self.message.edit(view=self)
            elif interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        await interaction.followup.send(text, ephemeral=True)
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.handled:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        self.handled = True
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if self.action == "delete":
                await self.manager.delete(self.actor, self.instance.id)
                text = "VPS deleted."
            else:
                instance = await self.manager.reinstall(
                    self.actor, self.instance.id, os_key=self.os_key
                )
                text = f"VPS reinstalled with {os_label(instance.os)}. Previous files were erased."
        except Exception as exc:
            log.exception("Confirmation action failed for %s", self.instance.id)
            text = error_text(exc)
        await self._finish(interaction, text)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.handled = True
        await interaction.response.send_message("Cancelled. Nothing was changed.", ephemeral=True)
        await self._finish_no_response(interaction)

    async def _finish_no_response(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        try:
            if self.message:
                await self.message.edit(view=self)
            elif interaction.message:
                await interaction.message.edit(view=self)
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class UserDeleteView(discord.ui.View):
    """Admin-only picker for one member's VPSes; selection opens a delete confirmation."""

    def __init__(self, bot, actor: Actor, owner_id: int, instances: list[Instance]):
        super().__init__(timeout=300)
        self.bot = bot
        self.actor = actor
        self.owner_id = owner_id
        self.instances = instances
        self.page = 0
        self.handled = False
        self.message: discord.Message | None = None
        self.select = discord.ui.Select(placeholder="Choose the VPS to delete…", row=0)
        self.select.callback = self._choose  # type: ignore[method-assign]
        self.add_item(self.select)
        self._rebuild()

    @property
    def manager(self):
        return self.bot.manager

    def _rebuild(self) -> None:
        page_size = 25
        pages = max(1, (len(self.instances) + page_size - 1) // page_size)
        self.page = max(0, min(self.page, pages - 1))
        chunk = self.instances[self.page * page_size : (self.page + 1) * page_size]
        self.select.options = [
            discord.SelectOption(
                label=i.id,
                value=i.id,
                description=f"{os_label(i.os)} · {i.specs.label()} · {i.status}"[:100],
                emoji="🗑️",
            )
            for i in chunk
        ]
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        actor = self.bot.actor_for(interaction.user, interaction.guild_id)
        if (
            actor.user_id != self.actor.user_id
            or actor.guild_id != self.actor.guild_id
            or not actor.is_admin
        ):
            await interaction.response.send_message(
                "Only the admin who opened this picker can use it.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    async def _choose(self, interaction: discord.Interaction) -> None:
        if self.handled:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        values = interaction.data.get("values", [])  # type: ignore[union-attr]
        if not values:
            await interaction.response.send_message("Choose a VPS first.", ephemeral=True)
            return
        try:
            instance = await self.manager.get(self.actor, values[0])
            if instance.owner_id != self.owner_id:
                raise UserError("That VPS no longer belongs to the selected member.")
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        self.handled = True
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()
        view = ConfirmView(self.manager, self.actor, instance, "delete", bot=self.bot)
        await interaction.response.send_message(
            f"⚠️ **Delete `{instance.id}` owned by <@{self.owner_id}>?** "
            f"{os_label(instance.os)} · {instance.specs.label()}. "
            "All files and processes will be permanently destroyed.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(
            embed=user_delete_embed(self.owner_id, self.instances, self.page), view=self
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._rebuild()
        await interaction.response.edit_message(
            embed=user_delete_embed(self.owner_id, self.instances, self.page), view=self
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ManageView(discord.ui.View):
    def __init__(self, bot, actor: Actor, instances: list[Instance], selected: str | None = None):
        super().__init__(timeout=600)
        self.bot = bot
        self.actor = actor
        self.instances = instances
        self.selected = selected or (instances[0].id if instances else None)
        self.message: discord.Message | None = None
        if len(instances) > 1:
            options = [
                discord.SelectOption(
                    label=i.id,
                    description=f"{os_label(i.os)} · {i.specs.label()} · {i.status}"[:100],
                    value=i.id,
                    default=i.id == self.selected,
                    emoji="⚡",
                )
                for i in instances[:25]
            ]
            select = discord.ui.Select(placeholder="Choose your VPS…", options=options, row=0)
            select.callback = self._select  # type: ignore[method-assign]
            self.add_item(select)
        if not instances:
            for child in list(self.children):
                self.remove_item(child)
        elif len(instances) > 25:
            # Discord selects max out at 25. Specific IDs remain accessible by command.
            self.select_note = True

    def current(self) -> Instance | None:
        return next((i for i in self.instances if i.id == self.selected), None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        actor = self.bot.actor_for(interaction.user, interaction.guild_id)
        if actor.user_id != self.actor.user_id or actor.guild_id != self.actor.guild_id:
            await interaction.response.send_message(
                "Open your own panel with `!manage`.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    async def _select(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", [])  # type: ignore[union-attr]
        if values:
            self.selected = values[0]
        instance = await self.manager.get(self.actor, self.selected)
        await interaction.response.edit_message(embed=instance_embed(instance), view=self)

    @property
    def manager(self):
        return self.bot.manager

    async def _power(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instance = await getattr(self.manager, action)(self.actor, self.selected)
            await interaction.followup.send(
                f"⚡ `{instance.id}` is now **{instance.status}**.", ephemeral=True
            )
            if self.message:
                await self.message.edit(embed=instance_embed(instance), view=self)
        except Exception as exc:
            log.exception("Manage action %s failed", action)
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Start", emoji="▶️", style=discord.ButtonStyle.success, row=1)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "start")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.secondary, row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "stop")

    @discord.ui.button(label="Refresh", emoji="🔃", style=discord.ButtonStyle.secondary, row=1)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            self.instances = await self.manager.list(self.actor)
            if self.selected not in {i.id for i in self.instances}:
                self.selected = self.instances[0].id if self.instances else None
            instance = (
                await self.manager.refresh(self.actor, self.selected) if self.selected else None
            )
            replacement = ManageView(self.bot, self.actor, self.instances, self.selected)
            embed = instance_embed(instance) if instance else empty_embed()
            if self.message:
                replacement.message = self.message
                await self.message.edit(embed=embed, view=replacement)
            self.stop()
            await interaction.followup.send("Panel refreshed.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="SSHX", emoji="💻", style=discord.ButtonStyle.primary, row=2)
    async def sshx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instance, url = await self.manager.sshx(self.actor, self.selected)
            view = SSHXView(self.manager, self.actor, instance.id, url)
            view.message = await interaction.followup.send(
                f"🔐 **Private root terminal for `{instance.id}`**\n"
                "Anyone with this link gets **root access inside this VPS**. Do not share it. "
                "The session stays active until you close it or stop/reinstall/delete the VPS. "
                "Hiding this message or waiting for this panel to expire does not revoke the link.",
                view=view,
                ephemeral=True,
                wait=True,
            )
        except Exception as exc:
            log.exception("SSHX failed")
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Reinstall", emoji="♻️", style=discord.ButtonStyle.danger, row=2)
    async def reinstall(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ReinstallOSView(self.bot, self.actor, instance)
        await interaction.response.send_message(
            embed=os_choice_embed(instance.specs, instance.owner_id, reinstall=True),
            file=banner_file(),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ConfirmView(self.manager, self.actor, instance, "delete", bot=self.bot)
        await interaction.response.send_message(
            f"🗑️ **Permanently delete `{instance.id}`?** Its container and disk will be destroyed. "
            "This cannot be undone.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SSHXView(discord.ui.View):
    def __init__(self, manager, actor: Actor, instance_id: str, url: str):
        super().__init__(timeout=300)
        self.manager = manager
        self.actor = actor
        self.instance_id = instance_id
        self.message: discord.WebhookMessage | None = None
        self.add_item(discord.ui.Button(label="Open root terminal", emoji="↗️", url=url))

    async def on_timeout(self) -> None:
        # Expiring the panel does not revoke the session; the warning says so.
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor.user_id:
            await interaction.response.send_message(
                "This private panel is not yours.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Close terminal session", style=discord.ButtonStyle.danger)
    async def close_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.manager.close_sshx(self.actor, self.instance_id)
            self.stop()
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            await interaction.edit_original_response(view=self)
            await interaction.followup.send(
                "SSHX session closed; its link is no longer usable.", ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)
