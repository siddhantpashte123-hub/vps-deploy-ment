from __future__ import annotations

import logging

import discord

from .errors import FierError, UserError
from .models import Actor, Instance

log = logging.getLogger(__name__)
COLOR = 0xF0523B
STATUS_ICON = {"running": "🟢", "stopped": "⚪", "error": "🔴"}


def error_text(exc: Exception) -> str:
    if isinstance(exc, UserError):
        return str(exc)
    if isinstance(exc, FierError):
        return (
            "The host could not complete that action. Try again or contact the host administrator."
        )
    return "Something went wrong. The host administrator can check `journalctl -u fier`."


def instance_embed(instance: Instance) -> discord.Embed:
    icon = STATUS_ICON.get(instance.status, "🟡")
    embed = discord.Embed(
        title=f"FIER · {instance.id}",
        description=f"{icon} **{instance.status.upper()}**\nUbuntu 24.04 development container",
        color=COLOR if instance.status != "error" else 0xD83C3E,
    )
    embed.add_field(name="RAM", value=f"{instance.ram_gb} GiB")
    embed.add_field(name="CPU", value=f"{instance.cpu} core(s)")
    embed.add_field(name="Disk", value=f"{instance.disk_gb} GiB")
    embed.add_field(name="Owner", value=f"<@{instance.owner_id}>", inline=False)
    embed.add_field(
        name="Terminal",
        value="Click **SSHX** for a private browser-terminal link. No public SSH port is exposed.",
        inline=False,
    )
    embed.set_footer(text="Start/Stop preserve files • Reinstall/Delete destroy files")
    return embed


def empty_embed() -> discord.Embed:
    return discord.Embed(
        title="FIER · Your VPSes",
        description=(
            "No VPSes yet. Deploy one with:\n```text\n!deploy 1 1 1\n```\n"
            "RAM (GiB) · CPU cores · disk (GiB)"
        ),
        color=COLOR,
    )


class ConfirmView(discord.ui.View):
    def __init__(self, manager, actor: Actor, instance: Instance, action: str):
        super().__init__(timeout=120)
        self.manager = manager
        self.actor = actor
        self.instance = instance
        self.action = action
        self.handled = False
        self.message: discord.Message | None = None
        self.confirm.label = "Delete permanently" if action == "delete" else "Wipe & reinstall"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.actor.user_id:
            await interaction.response.send_message(
                "This confirmation is not yours.", ephemeral=True
            )
            return False
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
            operation = self.manager.delete if self.action == "delete" else self.manager.reinstall
            await operation(self.actor, self.instance.id)
            text = (
                "VPS deleted."
                if self.action == "delete"
                else "VPS reinstalled. Previous files were erased."
            )
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
                    description=f"{i.specs.label()} · {i.status}"[:100],
                    value=i.id,
                    default=i.id == self.selected,
                )
                for i in instances[:25]
            ]
            select = discord.ui.Select(placeholder="Choose a VPS…", options=options, row=0)
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
                f"`{instance.id}` is now **{instance.status}**.", ephemeral=True
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

    @discord.ui.button(label="Reinstall", emoji="🔄", style=discord.ButtonStyle.danger, row=1)
    async def reinstall(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ConfirmView(self.manager, self.actor, instance, "reinstall")
        await interaction.response.send_message(
            f"**Wipe `{instance.id}`?** This permanently erases its files, installed packages, "
            "and running processes, then creates a fresh Ubuntu container with the same resources.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="SSHX", emoji="💻", style=discord.ButtonStyle.primary, row=1)
    async def sshx(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            instance, url = await self.manager.sshx(self.actor, self.selected)
            view = SSHXView(self.manager, self.actor, instance.id, url)
            view.message = await interaction.followup.send(
                f"**Private terminal for `{instance.id}`**\n"
                "Anyone with this link can access the terminal. Do not share it. "
                "The session stays active until you close it or stop/reinstall/delete the VPS. "
                "Hiding this message or waiting for this panel to expire does not revoke the link.",
                view=view,
                ephemeral=True,
                wait=True,
            )
        except Exception as exc:
            log.exception("SSHX failed")
            await interaction.followup.send(error_text(exc), ephemeral=True)

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger, row=2)
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            instance = await self.manager.get(self.actor, self.selected)
        except Exception as exc:
            await interaction.response.send_message(error_text(exc), ephemeral=True)
            return
        view = ConfirmView(self.manager, self.actor, instance, "delete")
        await interaction.response.send_message(
            f"**Permanently delete `{instance.id}`?** Its container and disk will be destroyed. "
            "This cannot be undone.",
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="Refresh", emoji="🔃", style=discord.ButtonStyle.secondary, row=2)
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
        self.add_item(discord.ui.Button(label="Open SSHX terminal", emoji="↗️", url=url))

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
