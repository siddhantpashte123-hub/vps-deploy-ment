from __future__ import annotations

import logging
import time
from pathlib import Path

import discord

from .errors import NoirError, UserError
from .models import Actor, HostStatus, Instance, LiveStats, Specs
from .os_catalog import DEFAULT_OS, OS_CHOICES, os_choice, os_label

log = logging.getLogger(__name__)

# NOIR dark-slate palette (clean left accent bar, gold/amber when stopped, green when online)
COLOR = 0x5865F2
ACCENT = 0x38BDF8
SUCCESS = 0x22C55E
WARNING = 0xFACC15
DANGER = 0xEF4444
MUTED = 0x64748B
STATUS_TEXT = {
    "running": "Running · Online",
    "stopped": "Stopped · Offline",
    "error": "Error · Needs Attention",
    "provisioning": "Provisioning · Booting",
    "reinstalling": "Reinstalling · Wiping Disk",
    "deleting": "Deleting · Removing",
}
STATUS_COLOR = {
    "running": SUCCESS,
    "stopped": WARNING,
    "error": DANGER,
    "provisioning": ACCENT,
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


def _brand(embed: discord.Embed, subtitle: str = "NOIR Cloud") -> discord.Embed:
    embed.set_footer(text=subtitle)
    return embed


def _timestamp(value: str) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0


def _format_uptime(seconds: int, online: bool) -> str:
    if not online:
        return "Offline"
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{max(1, secs)}s"


def _vmid_short(instance_id: str) -> str:
    tail = instance_id.split("-", 1)[-1]
    return tail[:6].upper()


def instance_embed(
    instance: Instance,
    stats: LiveStats | None = None,
    *,
    index: int = 1,
) -> discord.Embed:
    color = STATUS_COLOR.get(instance.status, COLOR)
    status_line = STATUS_TEXT.get(instance.status, instance.status.capitalize())
    online = instance.status == "running" and (stats.online if stats is not None else True)

    ram_total_mb = instance.ram_gb * 1024
    disk_total_gb = float(instance.disk_gb)
    if online and stats is not None:
        uptime_text = _format_uptime(stats.uptime_seconds, True)
        cpu_pct = min(999.9, max(0.0, stats.cpu_percent))
        ram_used_mb = min(ram_total_mb, max(0, round(stats.ram_used_bytes / (1024**2))))
        ram_pct = (ram_used_mb / ram_total_mb * 100.0) if ram_total_mb else 0.0
        disk_used_gb = min(disk_total_gb, max(0.0, stats.disk_used_bytes / (1024**3)))
        disk_pct = (disk_used_gb / disk_total_gb * 100.0) if disk_total_gb else 0.0
    elif online:
        uptime_text = "Online"
        cpu_pct = 0.0
        ram_used_mb = 0
        ram_pct = 0.0
        disk_used_gb = 0.0
        disk_pct = 0.0
    else:
        uptime_text = "Offline"
        cpu_pct = 0.0
        ram_used_mb = 0
        ram_pct = 0.0
        disk_used_gb = 0.0
        disk_pct = 0.0

    cores_word = "Core" if instance.cpu == 1 else "Cores"
    access_line = (
        "**Root terminal** — click **Console** to open a private SSHX root shell"
        if instance.status == "running"
        else (
            "**Stopped** — click **Start** to boot this VPS (files are preserved)"
            if instance.status == "stopped"
            else "**Attention** — click **Refresh** or **Reinstall** to recover this VPS"
        )
    )
    description = (
        f"`{instance.id}-{instance.owner_id}`\n"
        f"> **Status** {status_line}\n\n"
        f"> **RAM** {instance.ram_gb}GB • **CPU** {instance.cpu} {cores_word} • "
        f"**Storage** {instance.disk_gb}GB\n"
        f"> **OS** {instance.os} • **Config** {instance.ram_gb}GB RAM / "
        f"{instance.cpu} CPU / {instance.disk_gb}GB Disk\n"
        f"> **Uptime** {uptime_text}\n\n"
        f"> **CPU** {cpu_pct:.1f}% • **RAM** {ram_used_mb}MB / {ram_total_mb}MB "
        f"({ram_pct:.1f}%) • **Disk** {disk_used_gb:.1f}GB / {disk_total_gb:.1f}GB "
        f"({disk_pct:.1f}%)\n"
        f"> {access_line}"
    )
    embed = discord.Embed(
        title=f"VPS #{max(1, index)} — VMID `{_vmid_short(instance.id)}`",
        description=description,
        color=color,
    )
    embed.set_footer(text="NOIR Cloud")
    return embed


def empty_embed() -> discord.Embed:
    embed = discord.Embed(
        title="VPS Console — No Active Instance",
        description=(
            "`noir-empty`\n"
            "> **Status** Idle · No VPS Assigned\n\n"
            "> **Deploy** `!deploy 1 1 1 @user` *(Admin Only)*\n"
            "> **Images** `ubuntu-24.04` • `debian-12`\n"
            "> **Control** Server Channel or Bot DMs (`!manage`)\n\n"
            "> Ask a server administrator to provision a root VPS for you."
        ),
        color=WARNING,
    )
    return _brand(embed, "NOIR Cloud")


def panel_hub_embed(guild_name: str, count: int) -> discord.Embed:
    plural = "" if count == 1 else "s"
    embed = discord.Embed(
        title=f"Server Cloud Panel — `{guild_name}`",
        description=(
            f"`noir-fleet-{count}`\n"
            f"> **Status** Online · Ready\n\n"
            f"> **Active Fleet** {count} Provisioned VPS Instance{plural}\n"
            f"> **OS Images** Ubuntu 24.04 LTS • Debian 12\n"
            f"> **Direct Messages** `!manage` • `!help` anytime in Bot DMs\n\n"
            f"> Click **My VPS** to open your console here or "
            f"**Control in DM** to manage privately."
        ),
        color=COLOR,
    )
    return _brand(embed, "NOIR Utility • Interactive Cloud Control")


def help_embed(settings) -> discord.Embed:
    embed = discord.Embed(
        title="NOIR — Command & DM Reference",
        description=(
            "`noir-cli-guide`\n"
            "> **Status** Online · Server & DM Control Enabled\n\n"
            "> **Owner / DM** `!manage [id]` • `!panel` • `!delete <id>` • `!delete 1 1 1`\n"
            "> **Admin Deploy** `!deploy RAM CPU DISK [@user]` *(Ubuntu 24.04 / Debian 12)*\n"
            "> **Admin Tools** `!status` *(Host Node Dashboard)* • `!delete @user`\n\n"
            f"> **Per-User Limit** {settings.max_per_user} VPS • **Max Plan** "
            f"{settings.max_ram_gb}GB RAM / {settings.max_cpu} CPU / "
            f"{settings.max_disk_gb}GB Disk\n"
            "> **Tip** Send `!manage` directly to NOIR in **DMs** anytime."
        ),
        color=COLOR,
    )
    return _brand(embed, "NOIR Cloud")


def host_status_embed(host: HostStatus) -> discord.Embed:
    ram_used_gb = host.ram_used_bytes / (1024**3)
    ram_total_gb = max(0.1, host.ram_total_bytes / (1024**3))
    ram_pct = (
        min(100.0, (host.ram_used_bytes / host.ram_total_bytes * 100.0))
        if host.ram_total_bytes
        else 0.0
    )
    disk_used_gb = host.disk_used_bytes / (1024**3)
    disk_total_gb = max(0.1, host.disk_total_bytes / (1024**3))
    disk_free_gb = max(0.0, disk_total_gb - disk_used_gb)
    disk_pct = (
        min(100.0, (host.disk_used_bytes / host.disk_total_bytes * 100.0))
        if host.disk_total_bytes
        else 0.0
    )
    cores_word = "Core" if host.cpu_cores == 1 else "Cores"
    uptime_text = _format_uptime(host.uptime_seconds, True)
    color = DANGER if (ram_pct >= 90.0 or disk_pct >= 90.0) else SUCCESS

    embed = discord.Embed(
        title=f"Host Node — `{host.hostname}`",
        description=(
            f"`noir-host-telemetry`\n"
            f"> **Status** Online · Healthy\n\n"
            f"> **CPU** {host.cpu_cores} {cores_word} • **Load** {host.load_1m:.2f} • "
            f"**Storage Mode** `{host.storage_mode}`\n"
            f"> **Fleet** {host.running_vps} Running • {host.stopped_vps} Stopped • "
            f"{host.total_vps} / {host.max_total_vps} Total VPS\n"
            f"> **Uptime** {uptime_text}\n\n"
            f"> **CPU** {host.cpu_percent:.1f}% • **RAM** {ram_used_gb:.1f}GB / "
            f"{ram_total_gb:.1f}GB ({ram_pct:.1f}%) • **Disk** {disk_used_gb:.1f}GB / "
            f"{disk_total_gb:.1f}GB ({disk_pct:.1f}% · {disk_free_gb:.1f}GB Free)\n"
            f"> **Allocated to VPSes** {host.reserved_ram_gb}GB RAM • "
            f"{host.reserved_cpu} vCPU • {host.reserved_disk_gb}GB Disk"
        ),
        color=color,
    )
    return _brand(embed, "NOIR Cloud • Host Node Dashboard")


def sshx_embed(instance: Instance, *, closed: bool = False) -> discord.Embed:
    vmid = _vmid_short(instance.id)
    cores_word = "Core" if instance.cpu == 1 else "Cores"
    if closed:
        status_line = "Closed · Link Revoked"
        note = "**Session terminated** — click **Console** on the VPS panel to open a new shell"
        color = MUTED
    else:
        status_line = "Active · Private Root Shell"
        note = (
            "**Root access** — do not share this link. Closing this message does not revoke it; "
            "click **Close Session** when finished"
        )
        color = SUCCESS
    embed = discord.Embed(
        title=f"Root Console — VMID `{vmid}`",
        description=(
            f"`{instance.id}-{instance.owner_id}`\n"
            f"> **Status** {status_line}\n\n"
            f"> **User** `root` • **OS** {instance.os} • **Config** "
            f"{instance.ram_gb}GB RAM / {instance.cpu} {cores_word} / {instance.disk_gb}GB Disk\n"
            f"> **Home** `/home/noir` *(Persistent)* • **Network** Isolated Bridge\n\n"
            f"> {note}"
        ),
        color=color,
    )
    return _brand(embed, "NOIR Cloud • Ephemeral Root Console")


def confirm_embed(
    instance: Instance, action: str, *, os_key: str | None = None, state: str = "pending"
) -> discord.Embed:
    vmid = _vmid_short(instance.id)
    cores_word = "Core" if instance.cpu == 1 else "Cores"
    if action == "delete":
        heading = f"Delete VPS — VMID `{vmid}`"
        target_os = instance.os
        warning_line = (
            "**Permanent action** — container, `/home/noir` disk, and all files will be destroyed"
        )
    else:
        heading = f"Reinstall VPS — VMID `{vmid}`"
        target_os = os_key or instance.os
        warning_line = (
            f"**Destructive reinstall** — wipes `/home/noir` and installs "
            f"fresh **{os_label(target_os)}**"
        )
    if state == "done":
        status_line = "Completed"
        color = SUCCESS if action != "delete" else MUTED
    elif state == "cancelled":
        status_line = "Cancelled · No Changes Made"
        color = MUTED
    else:
        status_line = "Awaiting Confirmation"
        color = DANGER
    embed = discord.Embed(
        title=heading,
        description=(
            f"`{instance.id}-{instance.owner_id}`\n"
            f"> **Status** {status_line}\n\n"
            f"> **RAM** {instance.ram_gb}GB • **CPU** {instance.cpu} {cores_word} • "
            f"**Storage** {instance.disk_gb}GB\n"
            f"> **OS** {target_os} • **Owner** <@{instance.owner_id}>\n\n"
            f"> {warning_line}"
        ),
        color=color,
    )
    return _brand(embed, "NOIR Cloud")


def user_delete_embed(owner_id: int, instances: list[Instance], page: int = 0) -> discord.Embed:
    page_size = 25
    pages = max(1, (len(instances) + page_size - 1) // page_size)
    page = max(0, min(page, pages - 1))
    chunk = instances[page * page_size : (page + 1) * page_size]
    lines = [
        f"> `{i.id}` • **{i.os}** • {i.ram_gb}GB/{i.cpu}C/{i.disk_gb}GB • `{i.status.upper()}`"
        for i in chunk
    ]
    embed = discord.Embed(
        title=f"Delete Member VPS — Page {page + 1}/{pages}",
        description=(
            f"`owner-{owner_id}`\n"
            f"> **Status** Select One Instance · Confirmation Required\n"
            f"> **Target** <@{owner_id}> ({len(instances)} VPS)\n\n" + "\n".join(lines)
        ),
        color=DANGER,
    )
    return _brand(embed, f"NOIR Cloud • Page {page + 1}/{pages} • Confirmation required")


def _bar(percent: int, width: int = 14) -> str:
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
    status_line = "Online · Ready" if percent >= 100 else f"Provisioning · {percent}%"
    cores_word = "Core" if specs.cpu == 1 else "Cores"
    owner_part = f" • **Owner** <@{owner_id}>" if owner_id is not None else ""
    pipeline = []
    for key, label, threshold in DEPLOY_STAGES:
        if percent >= 100 or threshold < percent:
            pipeline.append(f"> `[DONE]` {label}")
        elif key == stage:
            pipeline.append(f"> `[WORK]` **{label}**")
        else:
            pipeline.append(f"> `[WAIT]` {label}")
    embed = discord.Embed(
        title=f"Provisioning VPS — `{percent}%`",
        description=(
            f"`{_bar(percent)}` **{current}**\n"
            f"> **Status** {status_line}\n\n"
            f"> **RAM** {specs.ram_gb}GB • **CPU** {specs.cpu} {cores_word} • "
            f"**Storage** {specs.disk_gb}GB\n"
            f"> **OS** {os_label(os_key)} (`{os_key}`){owner_part}\n\n" + "\n".join(pipeline)
        ),
        color=ACCENT if percent < 100 else SUCCESS,
    )
    return _brand(embed, "NOIR Cloud • Live Provisioning")


def deploy_failure_embed(
    specs: Specs, message: str, os_key: str = DEFAULT_OS, owner_id: int | None = None
) -> discord.Embed:
    cores_word = "Core" if specs.cpu == 1 else "Cores"
    owner_part = f" • **Owner** <@{owner_id}>" if owner_id is not None else ""
    embed = discord.Embed(
        title="NOIR · Deployment failed",
        description=(
            f"`deploy-aborted`\n"
            f"> **Status** Failed · Rolled Back\n\n"
            f"> **RAM** {specs.ram_gb}GB • **CPU** {specs.cpu} {cores_word} • "
            f"**Storage** {specs.disk_gb}GB\n"
            f"> **OS** {os_label(os_key)} (`{os_key}`){owner_part}\n\n"
            f"> **Reason** {message}"
        ),
        color=DANGER,
    )
    return _brand(embed, "NOIR Cloud")


def os_choice_embed(
    specs: Specs, owner_id: int | None = None, *, reinstall: bool = False
) -> discord.Embed:
    action = "Reinstall" if reinstall else "Deploy"
    cores_word = "Core" if specs.cpu == 1 else "Cores"
    owner_line = f"> **Owner** <@{owner_id}>\n" if owner_id is not None else ""
    images_line = " • ".join(f"**{c.label}** (`{c.id}`)" for c in OS_CHOICES)
    note_line = (
        "> **Warning** Reinstalling **permanently erases all files** in `/home/noir`."
        if reinstall
        else "> **Access** Root SSHX Console • Python, Node.js, Git & build tools preinstalled"
    )
    embed = discord.Embed(
        title=f"{action} VPS — Select Operating System",
        description=(
            f"`plan-{specs.ram_gb}g-{specs.cpu}c-{specs.disk_gb}g`\n"
            f"> **Status** Awaiting OS Selection\n\n"
            f"> **RAM** {specs.ram_gb}GB • **CPU** {specs.cpu} {cores_word} • "
            f"**Storage** {specs.disk_gb}GB\n"
            f"> **Config** {specs.ram_gb}GB RAM / {specs.cpu} CPU / {specs.disk_gb}GB Disk\n"
            f"{owner_line}\n"
            f"> **Images** {images_line}\n"
            f"{note_line}"
        ),
        color=WARNING if reinstall else ACCENT,
    )
    return _brand(embed, "NOIR Cloud")


def _os_select(placeholder: str, selected: str | None = None) -> discord.ui.Select:
    options = [
        discord.SelectOption(
            label=choice.label,
            value=choice.id,
            description=f"Root VPS · {choice.id}",
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

    @discord.ui.button(label="My VPS", style=discord.ButtonStyle.primary)
    async def my_vps(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
            instances = await self.bot.manager.list(actor)
            stats = await self.bot.manager.stats(actor, instances[0].id) if instances else None
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        selected = instances[0].id if instances else None
        view = ManageView(self.bot, actor, instances, selected, in_dm=False)
        await interaction.response.send_message(
            embed=instance_embed(instances[0], stats, index=1) if instances else empty_embed(),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Control in DM", style=discord.ButtonStyle.secondary)
    async def open_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
            instances = await self.bot.manager.list(actor)
            selected = instances[0].id if instances else None
            stats = await self.bot.manager.stats(actor, instances[0].id) if instances else None
            view = ManageView(self.bot, actor, instances, selected, in_dm=True)
            dm = await interaction.user.create_dm()
            view.message = await dm.send(
                embed=(
                    instance_embed(instances[0], stats, index=1) if instances else empty_embed()
                ),
                view=view,
            )
            await interaction.followup.send(
                "Sent your **NOIR** control console to your DMs. "
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
        stats = await self.bot.manager.stats(self.actor, instance.id)
        view = ManageView(self.bot, self.actor, [instance], instance.id, in_dm=in_dm)
        await message.edit(
            content=None,
            embed=instance_embed(instance, stats, index=1),
            attachments=[],
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
            content=None,
            embed=confirm_embed(instance, "reinstall", os_key=choice.id),
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

    async def _finish(
        self, interaction: discord.Interaction, text: str, *, ok: bool = True
    ) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        embed = confirm_embed(
            self.instance,
            self.action,
            os_key=self.os_key,
            state="done" if ok else "cancelled",
        )
        try:
            if self.message:
                await self.message.edit(embed=embed, view=self)
            elif interaction.message:
                await interaction.message.edit(embed=embed, view=self)
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
        ok = True
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
            ok = False
        await self._finish(interaction, text, ok=ok)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.handled = True
        await interaction.response.send_message("Cancelled. Nothing was changed.", ephemeral=True)
        await self._finish_no_response(interaction)

    async def _finish_no_response(self, interaction: discord.Interaction) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        embed = confirm_embed(self.instance, self.action, os_key=self.os_key, state="cancelled")
        try:
            if self.message:
                await self.message.edit(embed=embed, view=self)
            elif interaction.message:
                await interaction.message.edit(embed=embed, view=self)
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
            embed=confirm_embed(instance, "delete"),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._rebuild()
        await interaction.response.edit_message(
            embed=user_delete_embed(self.owner_id, self.instances, self.page), view=self
        )

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=1)
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
                    label=f"VPS #{idx} — {i.id}",
                    description=f"{os_label(i.os)} · {i.specs.label()} · {i.status}"[:100],
                    value=i.id,
                    default=i.id == self.selected,
                )
                for idx, i in enumerate(instances[:25], start=1)
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

    def current_index(self) -> int:
        for idx, item in enumerate(self.instances, start=1):
            if item.id == self.selected:
                return idx
        return 1

    def _replace_instance(self, updated: Instance) -> None:
        self.instances = [updated if i.id == updated.id else i for i in self.instances]

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
        self._replace_instance(instance)
        stats = await self.manager.stats(self.actor, instance.id)
        await interaction.response.edit_message(
            embed=instance_embed(instance, stats, index=self.current_index()),
            attachments=[],
            view=self,
        )

    @property
    def manager(self):
        return self.bot.manager

    async def _edit_panel(
        self, interaction: discord.Interaction, embed: discord.Embed, view: discord.ui.View
    ) -> None:
        target = self.message or interaction.message
        if target is not None:
            try:
                await target.edit(embed=embed, attachments=[], view=view)
                return
            except discord.HTTPException:
                pass
        try:
            await interaction.edit_original_response(embed=embed, attachments=[], view=view)
        except discord.HTTPException:
            pass

    async def _power(self, interaction: discord.Interaction, action: str) -> None:
        await interaction.response.defer()
        try:
            instance = await getattr(self.manager, action)(self.actor, self.selected)
            self._replace_instance(instance)
            stats = await self.manager.stats(self.actor, instance.id)
            await self._edit_panel(
                interaction,
                instance_embed(instance, stats, index=self.current_index()),
                self,
            )
        except Exception as exc:
            log.exception("Manage action %s failed", action)
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, row=1)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "start")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.secondary, row=1)
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._power(interaction, "stop")

    @discord.ui.button(label="Console", style=discord.ButtonStyle.primary, row=1)
    async def sshx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instance, url = await self.manager.sshx(self.actor, self.selected)
            view = SSHXView(self.manager, self.actor, instance.id, url, instance=instance)
            view.message = await interaction.followup.send(
                embed=sshx_embed(instance),
                view=view,
                ephemeral=True,
                wait=True,
            )
        except Exception as exc:
            log.exception("SSHX failed")
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Stats", style=discord.ButtonStyle.secondary, row=1)
    async def stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            instance = await self.manager.refresh(self.actor, self.selected)
            self._replace_instance(instance)
            live = await self.manager.stats(self.actor, instance.id)
            await self._edit_panel(
                interaction,
                instance_embed(instance, live, index=self.current_index()),
                self,
            )
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Reinstall", style=discord.ButtonStyle.danger, row=2)
    async def reinstall(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ReinstallOSView(self.bot, self.actor, instance)
        await interaction.response.send_message(
            embed=os_choice_embed(instance.specs, instance.owner_id, reinstall=True),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            self.instances = await self.manager.list(self.actor)
            if self.selected not in {i.id for i in self.instances}:
                self.selected = self.instances[0].id if self.instances else None
            instance = (
                await self.manager.refresh(self.actor, self.selected) if self.selected else None
            )
            if instance is not None:
                self._replace_instance(instance)
            live = (
                await self.manager.stats(self.actor, instance.id) if instance is not None else None
            )
            replacement = ManageView(
                self.bot, self.actor, self.instances, self.selected, in_dm=self.in_dm
            )
            embed = (
                instance_embed(instance, live, index=replacement.current_index())
                if instance
                else empty_embed()
            )
            replacement.message = self.message or interaction.message
            await self._edit_panel(interaction, embed, replacement)
            self.stop()
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ConfirmView(self.manager, self.actor, instance, "delete", bot=self.bot)
        await interaction.response.send_message(
            embed=confirm_embed(instance, "delete"),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Open in DM", style=discord.ButtonStyle.secondary, row=2)
    async def send_dm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instances = await self.manager.list(self.actor)
            selected = self.selected if any(i.id == self.selected for i in instances) else None
            if selected is None and instances:
                selected = instances[0].id
            current = next((i for i in instances if i.id == selected), None)
            dm_view = ManageView(self.bot, self.actor, instances, selected, in_dm=True)
            live = await self.manager.stats(self.actor, current.id) if current else None
            dm = await interaction.user.create_dm()
            dm_view.message = await dm.send(
                embed=(
                    instance_embed(current, live, index=dm_view.current_index())
                    if current
                    else empty_embed()
                ),
                view=dm_view,
            )
            await interaction.followup.send(
                "Sent your **NOIR** VPS console to your DMs.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "Could not send you a DM. Enable DMs from server members in your privacy settings.",
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class HostStatusView(discord.ui.View):
    """Admin-only host node dashboard with silent in-place Refresh."""

    def __init__(self, bot, actor: Actor):
        super().__init__(timeout=300)
        self.bot = bot
        self.actor = actor
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            actor = await self.bot.resolve_actor(interaction.user, interaction.guild_id)
        except UserError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return False
        if not actor.is_admin or actor.guild_id != self.actor.guild_id:
            await interaction.response.send_message(
                "Only server administrators can use the host dashboard.", ephemeral=True
            )
            return False
        self.actor = actor
        return True

    @discord.ui.button(label="Refresh", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            host = await self.bot.manager.host_status(self.actor)
            embed = host_status_embed(host)
            target = self.message or interaction.message
            if target is not None:
                await target.edit(embed=embed, view=self)
            else:
                await interaction.edit_original_response(embed=embed, view=self)
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class SSHXView(discord.ui.View):
    def __init__(
        self,
        manager,
        actor: Actor,
        instance_id: str,
        url: str,
        *,
        instance: Instance | None = None,
    ):
        super().__init__(timeout=300)
        self.manager = manager
        self.actor = actor
        self.instance_id = instance_id
        self.instance = instance
        self.message: discord.WebhookMessage | None = None
        self.add_item(discord.ui.Button(label="Open root terminal", url=url))

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

    @discord.ui.button(label="Close Session", style=discord.ButtonStyle.danger)
    async def close_session(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await self.manager.close_sshx(self.actor, self.instance_id)
            self.stop()
            for child in self.children:
                child.disabled = True  # type: ignore[attr-defined]
            edit_kwargs: dict = {"view": self}
            if self.instance is not None:
                edit_kwargs["embed"] = sshx_embed(self.instance, closed=True)
            await interaction.edit_original_response(**edit_kwargs)
        except Exception as exc:
            await interaction.followup.send(error_text(exc), ephemeral=True)
