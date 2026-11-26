from __future__ import annotations

import logging
import time
from pathlib import Path

import discord

from .errors import NoirError, UserError
from .models import Actor, Instance, Specs
from .os_catalog import DEFAULT_OS, OS_CHOICES, os_choice, os_label

log = logging.getLogger(__name__)

# NOIR dark-slate & blurple palette (native dark cards with crisp left accent bars)
COLOR = 0x5865F2
ACCENT = 0x38BDF8
SUCCESS = 0x22C55E
WARNING = 0xF59E0B
DANGER = 0xEF4444
MUTED = 0x64748B
STATUS_PILL = {
    "running": ("🟢", "ONLINE"),
    "stopped": ("⚪", "STOPPED"),
    "error": ("🔴", "ERROR"),
    "provisioning": ("🟡", "PROVISIONING"),
    "reinstalling": ("🟡", "REINSTALLING"),
    "deleting": ("🔴", "DELETING"),
}
STATUS_COLOR = {
    "running": SUCCESS,
    "stopped": MUTED,
    "error": DANGER,
    "provisioning": WARNING,
    "reinstalling": WARNING,
    "deleting": DANGER,
}
BANNER_PATH = Path(__file__).resolve().parent.parent / "assets" / "noir-banner.png"
BANNER_FILENAME = "noir-banner.png"

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
    if isinstance(exc, NoirError):
        return (
            "The host could not complete that action. Try again or contact the host administrator."
        )
    return "Something went wrong. The host administrator can check `journalctl -u noir`."


def banner_file() -> discord.File | None:
    if BANNER_PATH.is_file():
        return discord.File(BANNER_PATH, filename=BANNER_FILENAME)
    return None


def _brand(embed: discord.Embed, subtitle: str = "Private Cloud & Root VPS") -> discord.Embed:
    embed.set_footer(text=f"NOIR Utility • {subtitle}")
    if BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{BANNER_FILENAME}")
    return embed


def _timestamp(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def instance_embed(instance: Instance) -> discord.Embed:
    dot, label = STATUS_PILL.get(instance.status, ("🟡", instance.status.upper()))
    color = STATUS_COLOR.get(instance.status, COLOR)
    ts = int(_timestamp(instance.updated_at))
    embed = discord.Embed(
        title="Server VPS Console",
        description=(
            f"**Status**\n`{dot} {label}`\n\n"
            "Isolated **Root terminal** container with persistent home storage. "
            "Control this VPS here or directly in **bot DMs** with `!manage`."
        ),
        color=color,
    )
    embed.set_author(name="NOIR Cloud")
    embed.add_field(
        name="Resources",
        value=(
            f"RAM: **{instance.ram_gb} GiB**\n"
            f"CPU: **{instance.cpu} vCPU**\n"
            f"Disk: **{instance.disk_gb} GiB**"
        ),
        inline=True,
    )
    embed.add_field(
        name="Environment",
        value=(
            f"OS: **{os_label(instance.os)}**\nOwner: <@{instance.owner_id}>\nUpdated: <t:{ts}:R>"
        ),
        inline=True,
    )
    embed.add_field(
        name="Root Terminal",
        value=(
            "Click **SSHX** below to open a private **root** browser terminal. "
            "No public SSH port is exposed."
        ),
        inline=False,
    )
    embed.set_footer(
        text=(
            f"vID: {instance.id} • Managed Via NOIR • "
            "Start/Stop preserve files • Reinstall/Delete erase files"
        )
    )
    if BANNER_PATH.is_file():
        embed.set_image(url=f"attachment://{BANNER_FILENAME}")
    return embed


def empty_embed() -> discord.Embed:
    embed = discord.Embed(
        title="NOIR Cloud Panel",
        description=(
            "You do not have an active VPS yet. Ask a server administrator to provision "
            "a dedicated root container for you, or deploy one if you are an admin:\n"
            "```text\n!deploy 1 1 1 @user\n```\n"
            "Once created, you can control your VPS in the server or anytime in **bot DMs** "
            "using `!manage`."
        ),
        color=COLOR,
    )
    embed.set_author(name="NOIR Utility")
    return _brand(embed, "Interactive Cloud Control")


def panel_hub_embed(guild_name: str, count: int) -> discord.Embed:
    embed = discord.Embed(
        title="Server Cloud Panel",
        description=(
            "Manage your isolated Ubuntu 24.04 or Debian 12 root VPS directly from Discord! "
            "Click a button below to open your personal control panel here or have NOIR send "
            "a private control console to your **DMs**."
        ),
        color=COLOR,
    )
    embed.set_author(name=f"NOIR • {guild_name}")
    embed.add_field(
        name="Active Fleet",
        value=f"**{count}** provisioned VPS instance{'s' if count != 1 else ''}",
        inline=True,
    )
    embed.add_field(
        name="Direct Messages",
        value="Send `!manage` or `!help` to NOIR in DMs anytime",
        inline=True,
    )
    return _brand(embed, "Constructive & Interactive Cloud Control")


def help_embed(settings) -> discord.Embed:
    embed = discord.Embed(
        title="NOIR Command & DM Guide",
        description=(
            "Admin-provisioned Ubuntu 24.04 & Debian 12 root containers. "
            "Every control command works both in your locked Discord server **and in bot DMs**."
        ),
        color=COLOR,
    )
    embed.set_author(name="NOIR Utility")
    embed.add_field(
        name="My VPS & DM Control",
        value=(
            "`!manage` or `!manage <id>` — Start · Stop · Refresh · SSHX · Reinstall · Delete\n"
            "`!panel` — Post an interactive **My VPS / Control in DM** hub card\n"
            "💬 **In bot DMs:** send `!manage`, `!delete <id>`, "
            "`!deploy 1 1 1 <@USER_ID>`, or `!help`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Deploy (Admins Only)",
        value=(
            "`!deploy 1 1 1 @user` (or `<@USER_ID>` in DMs) — create a VPS for a server member\n"
            "`!deploy 1 1 1` — create a VPS for yourself\n"
            "Format: `RAM GiB` · `CPU cores` · `Disk GiB`, then pick Ubuntu 24.04 or Debian 12."
        ),
        inline=False,
    )
    embed.add_field(
        name="Delete",
        value=(
            "`!delete @user` — admin selects **one** of that member's VPSes, then confirms\n"
            "`!delete <id>` or `!delete 1 1 1` — delete your own matching VPS "
            "(confirmation required)"
        ),
        inline=False,
    )
    return _brand(
        embed,
        f"Limit: {settings.max_per_user}/user • Max plan: "
        f"{settings.max_ram_gb} GiB RAM / {settings.max_cpu} CPU / {settings.max_disk_gb} GiB disk",
    )


def user_delete_embed(owner_id: int, instances: list[Instance], page: int = 0) -> discord.Embed:
    page_size = 25
    pages = max(1, (len(instances) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    chunk = instances[page * page_size : (page + 1) * page_size]
    lines = [
        f"• `{i.id}` · **{os_label(i.os)}** · {i.specs.label()} · `{i.status.upper()}`"
        for i in chunk
    ]
    embed = discord.Embed(
        title="Delete Member VPS",
        description=(
            f"**Target:** <@{owner_id}>\n"
            "Select **one** VPS from the menu below, then confirm. "
            "Nothing is deleted until you confirm.\n\n" + "\n".join(lines)
        ),
        color=DANGER,
    )
    embed.set_author(name="NOIR Admin")
    embed.set_footer(text=f"NOIR Utility • Page {page + 1}/{pages} • Confirmation required")
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
    status_pill = "🟢 ONLINE" if percent >= 100 else "🟡 PROVISIONING"
    pipeline = []
    for key, label, threshold in DEPLOY_STAGES:
        if percent >= 100 or threshold < percent:
            pipeline.append(f"✅ {label}")
        elif key == stage:
            pipeline.append(f"🔄 **{label}**")
        else:
            pipeline.append(f"⬜ {label}")
    embed = discord.Embed(
        title="Provisioning Root VPS",
        description=(
            f"**Status**\n`{status_pill}` · **{percent}%**\n"
            f"`{_bar(percent)}` — **{current}**\n\n" + "\n".join(pipeline)
        ),
        color=ACCENT if percent < 100 else SUCCESS,
    )
    embed.set_author(name="NOIR Cloud")
    embed.add_field(name="Plan", value=specs.label(), inline=True)
    embed.add_field(name="OS", value=os_label(os_key), inline=True)
    embed.add_field(name="Access", value="Root SSHX terminal", inline=True)
    if owner_id is not None:
        embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    return _brand(embed, "Live Provisioning Pipeline")


def deploy_failure_embed(
    specs: Specs, message: str, os_key: str = DEFAULT_OS, owner_id: int | None = None
) -> discord.Embed:
    embed = discord.Embed(
        title="NOIR · Deployment failed",
        description=f"**Status**\n`🔴 FAILED`\n\n{message}",
        color=DANGER,
    )
    embed.set_author(name="NOIR Cloud")
    embed.add_field(name="Plan", value=specs.label(), inline=True)
    embed.add_field(name="OS", value=os_label(os_key), inline=True)
    if owner_id is not None:
        embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    return _brand(embed, "Provisioning Aborted")


def os_choice_embed(
    specs: Specs, owner_id: int | None = None, *, reinstall: bool = False
) -> discord.Embed:
    action = "Reinstall" if reinstall else "Deploy"
    embed = discord.Embed(
        title=f"Select Operating System · {action}",
        description=(
            "**Status**\n`🟡 AWAITING OS SELECTION`\n\n"
            "Choose the Linux distribution for this isolated root VPS. "
            "Both images include Python, Node.js, Git, build tools, and SSHX."
            + ("\n\n⚠️ Reinstalling **permanently erases all files**." if reinstall else "")
        ),
        color=ACCENT,
    )
    embed.set_author(name="NOIR Cloud")
    embed.add_field(name="Plan", value=specs.label(), inline=True)
    if owner_id is not None:
        embed.add_field(name="Owner", value=f"<@{owner_id}>", inline=True)
    embed.add_field(
        name="Available Images",
        value="\n".join(f"{choice.emoji} **{choice.label}**" for choice in OS_CHOICES),
        inline=False,
    )
    return _brand(embed, "Ubuntu 24.04 LTS & Debian 12")


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


class PanelHubView(discord.ui.View):
    """Interactive NOIR hub card: open an ephemeral panel here or receive a DM console."""

    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="My VPS", emoji="🖥️", style=discord.ButtonStyle.primary)
    async def my_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
            instances = await self.bot.manager.list(actor)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        selected = instances[0].id if instances else None
        view = ManageView(self.bot, actor, instances, selected, in_dm=False)
        await interaction.response.send_message(
            embed=instance_embed(instances[0]) if instances else empty_embed(),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Control in DM", emoji="✉️", style=discord.ButtonStyle.secondary)
    async def open_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
            instances = await self.bot.manager.list(actor)
            selected = instances[0].id if instances else None
            view = ManageView(self.bot, actor, instances, selected, in_dm=True)
            dm = await interaction.user.create_dm()
            kwargs = {
                "embed": instance_embed(instances[0]) if instances else empty_embed(),
                "view": view,
            }
            banner = banner_file()
            if banner is not None:
                kwargs["file"] = banner
            view.message = await dm.send(**kwargs)
            await interaction.followup.send(
                "✉️ Sent your **NOIR** control console to your DMs. "
                "You can also type `!manage` there anytime.",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Could not send you a DM. Enable **Allow direct messages from server members** "
                "in your Discord privacy settings, then try again.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)


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
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
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
        in_dm = interaction.guild_id is None
        view = ManageView(self.bot, self.actor, [instance], instance.id, in_dm=in_dm)
        await message.edit(
            content=f"✅ VPS deployed for <@{self.owner_id}> · `{instance.id}`",
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
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
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
            try:
                actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
            except UserError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return False
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
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
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
    def __init__(
        self,
        bot,
        actor: Actor,
        instances: list[Instance],
        selected: str | None = None,
        *,
        in_dm: bool = False,
    ):
        super().__init__(timeout=600)
        self.bot = bot
        self.actor = actor
        self.instances = instances
        self.selected = selected or (instances[0].id if instances else None)
        self.in_dm = in_dm
        self.message: discord.Message | None = None
        if in_dm:
            self.remove_item(self.send_dm)
        if len(instances) > 1:
            options = [
                discord.SelectOption(
                    label=i.id,
                    description=f"{os_label(i.os)} · {i.specs.label()} · {i.status}"[:100],
                    value=i.id,
                    default=i.id == self.selected,
                    emoji="🖥️",
                )
                for i in instances[:25]
            ]
            select = discord.ui.Select(placeholder="Switch VPS instance…", options=options, row=0)
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
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
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
                f"`{instance.id}` is now **{instance.status.upper()}**.", ephemeral=True
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
            replacement = ManageView(
                self.bot, self.actor, self.instances, self.selected, in_dm=self.in_dm
            )
            embed = instance_embed(instance) if instance else empty_embed()
            if self.message:
                replacement.message = self.message
                await self.message.edit(embed=embed, view=replacement)
            self.stop()
            await interaction.followup.send("Panel refreshed.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Open in DM", emoji="✉️", style=discord.ButtonStyle.secondary, row=1)
    async def send_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instances = await self.manager.list(self.actor)
            selected = self.selected if any(i.id == self.selected for i in instances) else None
            if selected is None and instances:
                selected = instances[0].id
            current = next((i for i in instances if i.id == selected), None)
            dm_view = ManageView(self.bot, self.actor, instances, selected, in_dm=True)
            dm = await interaction.user.create_dm()
            kwargs = {
                "embed": instance_embed(current) if current else empty_embed(),
                "view": dm_view,
            }
            banner = banner_file()
            if banner is not None:
                kwargs["file"] = banner
            dm_view.message = await dm.send(**kwargs)
            await interaction.followup.send(
                "✉️ Sent your **NOIR** VPS console to your DMs.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Could not send you a DM. Enable DMs from server members in your privacy settings.",
                ephemeral=True,
            )
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
