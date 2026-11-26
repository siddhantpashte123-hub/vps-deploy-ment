from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from discord.ext import commands

from noir.bot import build_bot, mention_id, resolve_member
from noir.errors import AccessDenied, UserError
from noir.models import Actor, Instance, Specs, utc_now
from noir.ui import (
    ConfirmView,
    DeployOSView,
    ManageView,
    PanelHubView,
    ReinstallOSView,
    UserDeleteView,
)

TARGET_ID = 222333444555666777


def make_member(user_id, guild, *, admin=False, bot=False):
    member = Mock(spec=discord.Member)
    member.id = user_id
    member.guild = guild
    member.bot = bot
    member.guild_permissions = SimpleNamespace(administrator=admin)
    member.roles = []
    return member


@pytest.fixture
def bot(settings, manager, backend):
    return build_bot(settings, manager, backend)


@pytest.fixture
def guild(actor):
    return SimpleNamespace(
        id=actor.guild_id,
        get_member=Mock(return_value=None),
        fetch_member=AsyncMock(),
    )


@pytest.fixture
def admin_member(actor, guild):
    return make_member(actor.user_id, guild, admin=True)


@pytest.fixture
def target_member(guild):
    member = make_member(TARGET_ID, guild)
    guild.get_member.return_value = member
    guild.fetch_member.return_value = member
    return member


def make_ctx(bot, guild, author, name, arguments=""):
    message = SimpleNamespace(
        _state=None,
        id=111,
        author=author,
        guild=guild,
        channel=SimpleNamespace(id=222),
        mentions=[],
        attachments=[],
        created_at=datetime.now(timezone.utc),
        edited_at=None,
    )
    command = bot.get_command(name)
    ctx = commands.Context(
        message=message,
        bot=bot,
        view=commands.view.StringView(arguments),
        prefix="!",
        command=command,
        invoked_with=name,
    )
    sent_message = SimpleNamespace(id=333, edit=AsyncMock())
    ctx.send = AsyncMock(return_value=sent_message)
    return ctx


async def invoke(bot, guild, author, name, arguments=""):
    ctx = make_ctx(bot, guild, author, name, arguments)
    # These tests exercise parsing/authorization, not the production cooldown.
    ctx.command.reset_cooldown(ctx)
    try:
        await ctx.command.invoke(ctx)
    except commands.CommandInvokeError as exc:
        raise exc.original from exc
    return ctx


def make_interaction(member, guild_id, *, values=None):
    message = SimpleNamespace(id=444, edit=AsyncMock())
    return SimpleNamespace(
        user=member,
        guild_id=guild_id,
        data={"values": values or []},
        message=message,
        response=SimpleNamespace(
            send_message=AsyncMock(),
            edit_message=AsyncMock(),
            defer=AsyncMock(),
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
        original_response=AsyncMock(return_value=message),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"<@{TARGET_ID}>", TARGET_ID),
        (f"<@!{TARGET_ID}>", TARGET_ID),
        (str(TARGET_ID), None),
        (f"<@{TARGET_ID}> extra", None),
        (f"<@&{TARGET_ID}>", None),
        ("@everyone", None),
    ],
)
def test_mention_id(raw, expected):
    assert mention_id(raw) == expected


async def test_deploy_without_target_offers_os_for_self(bot, guild, admin_member, actor, backend):
    ctx = await invoke(bot, guild, admin_member, "deploy", "1 2 3")
    view = ctx.send.call_args.kwargs["view"]
    assert isinstance(view, DeployOSView)
    assert view.owner_id == actor.user_id
    assert view.specs == Specs(1, 2, 3)
    assert {option.value for option in view.select.options} == {"ubuntu-24.04", "debian-12"}
    assert view.select.max_values == 1
    assert f"<@{actor.user_id}>" in str(ctx.send.call_args.kwargs["embed"].to_dict())
    assert backend.events == []


async def test_deploy_mention_targets_member_without_members_intent(
    bot, guild, admin_member, target_member, backend
):
    guild.get_member.return_value = None
    ctx = await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@!{TARGET_ID}>")
    guild.fetch_member.assert_awaited_once_with(TARGET_ID)
    view = ctx.send.call_args.kwargs["view"]
    assert view.owner_id == target_member.id
    assert bot.intents.members is False
    assert backend.events == []


async def test_deploy_uses_member_from_message_mentions(bot, guild, admin_member, target_member):
    guild.get_member.return_value = None
    ctx = make_ctx(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}>")
    ctx.message.mentions = [target_member]
    await ctx.command.invoke(ctx)
    guild.fetch_member.assert_not_awaited()
    assert ctx.send.call_args.kwargs["view"].owner_id == TARGET_ID


async def test_deploy_rejects_non_admin_even_with_legacy_deploy_role(
    bot, guild, actor, monkeypatch, backend
):
    monkeypatch.setenv("NOIR_DEPLOY_ROLE_ID", "42")
    member = make_member(actor.user_id, guild)
    member.roles = [SimpleNamespace(id=42)]
    with pytest.raises(UserError, match="Only server administrators"):
        await invoke(bot, guild, member, "deploy", f"1 1 1 <@{TARGET_ID}>")
    guild.fetch_member.assert_not_awaited()
    assert backend.events == []


async def test_deploy_rejects_raw_id_and_bot_target(bot, guild, admin_member, target_member):
    with pytest.raises(UserError, match="Mention a member"):
        await invoke(bot, guild, admin_member, "deploy", f"1 1 1 {TARGET_ID}")
    target_member.bot = True
    with pytest.raises(UserError, match="bot account"):
        await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}>")


@pytest.mark.parametrize("failure", ["not-found", "http"])
async def test_resolve_member_reports_safe_lookup_errors(bot, guild, admin_member, failure):
    response = SimpleNamespace(status=404 if failure == "not-found" else 500, reason="test")
    error_type = discord.NotFound if failure == "not-found" else discord.HTTPException
    guild.fetch_member.side_effect = error_type(response, "lookup failed")
    with pytest.raises(UserError, match="member of this server|look up that member"):
        await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}>")


async def test_resolve_member_ignores_user_object_without_guild_membership(bot, guild):
    guild.get_member.return_value = None
    fetched = make_member(TARGET_ID, guild)
    guild.fetch_member.return_value = fetched
    ctx = make_ctx(bot, guild, make_member(1, guild, admin=True), "deploy")
    ctx.message.mentions = [SimpleNamespace(id=TARGET_ID, guild=SimpleNamespace(id=999))]
    assert await resolve_member(ctx, TARGET_ID) is fetched


async def test_deploy_argument_errors(bot, guild, admin_member):
    with pytest.raises(commands.MissingRequiredArgument):
        await invoke(bot, guild, admin_member, "deploy", "1 1")
    with pytest.raises(commands.BadArgument):
        await invoke(bot, guild, admin_member, "deploy", "1.5 1 1")
    with pytest.raises(commands.TooManyArguments):
        await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}> extra")
    with pytest.raises(UserError, match="RAM must be"):
        await invoke(bot, guild, admin_member, "deploy", "9 1 1")


async def test_delete_mention_lists_only_target_and_deletes_nothing(
    bot, guild, admin_member, target_member, manager, actor, backend
):
    first = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    second = await manager.deploy(actor, Specs(2, 2, 2), owner_id=TARGET_ID, os_key="debian-12")
    await manager.deploy(actor, Specs(1, 1, 1))
    ctx = await invoke(bot, guild, admin_member, "delete", f"<@{TARGET_ID}>")
    view = ctx.send.call_args.kwargs["view"]
    assert isinstance(view, UserDeleteView)
    assert view.owner_id == TARGET_ID
    assert {option.value for option in view.select.options} == {first.id, second.id}
    assert view.select.max_values == 1
    assert not any(event.startswith("remove:") for event in backend.events)
    assert len(await manager.list_for(actor, TARGET_ID)) == 2


async def test_delete_mention_requires_admin_and_existing_vps(
    bot, guild, actor, target_member, manager, backend
):
    member = make_member(actor.user_id, guild)
    with pytest.raises(UserError, match="Only admins"):
        await invoke(bot, guild, member, "delete", f"<@{TARGET_ID}>")
    with pytest.raises(UserError, match="has no VPSes"):
        await invoke(
            bot, guild, make_member(actor.user_id, guild, admin=True), "delete", f"<@{TARGET_ID}>"
        )
    assert backend.events == []


async def test_delete_mention_rejects_resource_arguments(bot, guild, admin_member, target_member):
    with pytest.raises(commands.BadArgument, match="without resource numbers"):
        await invoke(bot, guild, admin_member, "delete", f"<@{TARGET_ID}> 1 1")


async def test_delete_id_and_specs_only_open_confirmation(
    bot, guild, admin_member, manager, actor, backend
):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    ctx = await invoke(bot, guild, admin_member, "delete", instance.id)
    assert isinstance(ctx.send.call_args.kwargs["view"], ConfirmView)
    ctx = await invoke(bot, guild, admin_member, "delete", "1 1 1")
    view = ctx.send.call_args.kwargs["view"]
    assert isinstance(view, ConfirmView)
    assert view.instance.id == instance.id
    assert not any(event.startswith("remove:") for event in backend.events)


async def test_guild_check_rejects_other_guilds_and_strangers_in_dm(
    settings, manager, backend, actor, guild, monkeypatch
):
    restricted = build_bot(replace(settings, allowed_guild_id=actor.guild_id), manager, backend)
    member = make_member(actor.user_id, guild, admin=True)
    other_guild = SimpleNamespace(id=999, get_member=Mock(), fetch_member=AsyncMock())
    with pytest.raises(commands.CheckFailure, match="restricted"):
        await invoke(restricted, other_guild, member, "deploy", "1 1 1")
    # In DMs, a user who is not in the locked server, not in NOIR_ADMIN_IDS, and owns
    # no VPS in the store is rejected.
    response = SimpleNamespace(status=404, reason="not found")
    guild.get_member.return_value = None
    guild.fetch_member = AsyncMock(side_effect=discord.NotFound(response, "not a member"))
    monkeypatch.setattr(restricted, "locked_guild", lambda: guild)
    stranger = Mock(spec=discord.User)
    stranger.id = 777888999000111222
    with pytest.raises(UserError, match="locked server"):
        await invoke(restricted, None, stranger, "manage")


async def test_dm_control_for_owner_and_admin(
    settings, manager, backend, actor, guild, monkeypatch
):
    instance = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    owner_member = make_member(TARGET_ID, guild, admin=False)
    admin_member = make_member(actor.user_id, guild, admin=True)
    guild.get_member = Mock(
        side_effect=lambda uid: (
            owner_member if uid == TARGET_ID else admin_member if uid == actor.user_id else None
        )
    )
    bot = build_bot(settings, manager, backend)
    monkeypatch.setattr(bot, "locked_guild", lambda: guild)

    owner_user = Mock(spec=discord.User)
    owner_user.id = TARGET_ID
    ctx = await invoke(bot, None, owner_user, "manage")
    view = ctx.send.call_args.kwargs["view"]
    assert isinstance(view, ManageView)
    assert view.in_dm is True
    assert view.selected == instance.id
    assert "file" not in ctx.send.call_args.kwargs
    assert not any(getattr(c, "label", "") == "Open in DM" for c in view.children)
    # Power & Stats controls update the card silently in-place without spam followups.
    dm_interaction = make_interaction(owner_user, None)
    assert await view.interaction_check(dm_interaction)
    await view.stop.callback(dm_interaction)
    assert backend.items[instance.id]["status"] == "stopped"
    dm_interaction.followup.send.assert_not_awaited()
    stopped_embed = view.message.edit.call_args.kwargs["embed"]
    assert "Stopped · Offline" in stopped_embed.description
    assert "Uptime** Offline" in stopped_embed.description

    start_interaction = make_interaction(owner_user, None)
    await view.start.callback(start_interaction)
    assert backend.items[instance.id]["status"] == "running"
    start_interaction.followup.send.assert_not_awaited()
    running_embed = view.message.edit.call_args.kwargs["embed"]
    assert "Running · Online" in running_embed.description
    assert "RAM** 128MB / 1024MB" in running_embed.description

    stats_interaction = make_interaction(owner_user, None)
    await view.stats.callback(stats_interaction)
    stats_interaction.followup.send.assert_not_awaited()

    # Admin can deploy for a server member from DMs.
    admin_user = Mock(spec=discord.User)
    admin_user.id = actor.user_id
    deploy_ctx = await invoke(bot, None, admin_user, "deploy", f"1 1 1 <@{TARGET_ID}>")
    deploy_view = deploy_ctx.send.call_args.kwargs["view"]
    assert isinstance(deploy_view, DeployOSView)
    assert deploy_view.owner_id == TARGET_ID
    assert await deploy_view.interaction_check(make_interaction(admin_user, None))


async def test_panel_hub_posts_card_and_sends_dm_console(
    bot, guild, admin_member, manager, actor, monkeypatch
):
    instance = await manager.deploy(actor, Specs(2, 2, 5))
    guild.name = "NOIR HQ"
    guild.get_member.return_value = admin_member
    monkeypatch.setattr(bot, "locked_guild", lambda: guild)
    ctx = await invoke(bot, guild, admin_member, "panel")
    embed = ctx.send.call_args.kwargs["embed"]
    hub = ctx.send.call_args.kwargs["view"]
    assert isinstance(hub, PanelHubView)
    assert "Server Cloud Panel" in embed.title
    assert "NOIR Utility" in embed.footer.text

    dm_channel = SimpleNamespace(send=AsyncMock(return_value=SimpleNamespace(id=555)))
    admin_member.create_dm = AsyncMock(return_value=dm_channel)
    press = make_interaction(admin_member, actor.guild_id)
    await hub.open_dm.callback(press)
    dm_channel.send.assert_awaited_once()
    sent_view = dm_channel.send.call_args.kwargs["view"]
    assert isinstance(sent_view, ManageView)
    assert sent_view.in_dm is True
    assert sent_view.selected == instance.id


def test_build_bot_requires_single_guild_lock(settings, manager, backend):
    with pytest.raises(UserError, match="NOIR_ALLOWED_GUILD_ID is required"):
        build_bot(replace(settings, allowed_guild_id=None), manager, backend)


async def test_actor_for_rejects_other_guild(bot, guild, actor):
    member = make_member(actor.user_id, guild, admin=True)
    with pytest.raises(UserError, match="locked to one Discord server"):
        bot.actor_for(member, 999)


async def test_on_guild_join_leaves_unauthorized_server(bot, actor):
    allowed = SimpleNamespace(id=actor.guild_id, name="allowed", leave=AsyncMock())
    other = SimpleNamespace(id=999, name="other", leave=AsyncMock())
    await bot.on_guild_join(allowed)
    await bot.on_guild_join(other)
    allowed.leave.assert_not_awaited()
    other.leave.assert_awaited_once()


async def test_on_ready_leaves_unauthorized_servers(bot, actor, monkeypatch):
    allowed = SimpleNamespace(id=actor.guild_id, name="allowed", leave=AsyncMock())
    other = SimpleNamespace(id=999, name="other", leave=AsyncMock())
    monkeypatch.setattr(bot._connection, "_guilds", {g.id: g for g in (allowed, other)})
    monkeypatch.setattr(bot, "change_presence", AsyncMock())
    await bot.on_ready()
    allowed.leave.assert_not_awaited()
    other.leave.assert_awaited_once()
    bot.change_presence.assert_awaited_once()


async def test_os_selection_deploys_for_target_once(
    bot, guild, admin_member, target_member, manager, actor, store, backend
):
    ctx = await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}>")
    view = ctx.send.call_args.kwargs["view"]
    interaction = make_interaction(admin_member, actor.guild_id, values=["debian-12"])
    assert await view.interaction_check(interaction)
    await view.select.callback(interaction)
    records = await manager.list_for(actor, TARGET_ID)
    assert len(records) == 1
    assert records[0].owner_id == TARGET_ID
    assert records[0].os == "debian-12"
    assert store.get(records[0].id).os == "debian-12"
    assert backend.items[records[0].id]["os"] == "debian-12"
    again = make_interaction(admin_member, actor.guild_id, values=["ubuntu-24.04"])
    await view.select.callback(again)
    assert len(await manager.list_for(actor, TARGET_ID)) == 1
    assert "already started" in again.response.send_message.call_args.args[0]


async def test_os_selection_rejects_other_admin_and_non_admin(bot, guild, admin_member, actor):
    ctx = await invoke(bot, guild, admin_member, "deploy", "1 1 1")
    view = ctx.send.call_args.kwargs["view"]
    for member in (make_member(999, guild, admin=True), make_member(999, guild)):
        interaction = make_interaction(member, actor.guild_id, values=["debian-12"])
        assert not await view.interaction_check(interaction)
        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.call_args.kwargs["ephemeral"] is True


async def test_user_delete_picker_confirms_exactly_one(
    bot, guild, admin_member, target_member, manager, actor, backend, store
):
    first = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    second = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    ctx = await invoke(bot, guild, admin_member, "delete", f"<@{TARGET_ID}>")
    picker = ctx.send.call_args.kwargs["view"]
    choice = make_interaction(admin_member, actor.guild_id, values=[first.id])
    assert await picker.interaction_check(choice)
    await picker.select.callback(choice)
    confirm = choice.response.send_message.call_args.kwargs["view"]
    assert isinstance(confirm, ConfirmView)
    assert confirm.instance.id == first.id
    assert choice.response.send_message.call_args.kwargs["ephemeral"] is True
    assert len(await manager.list_for(actor, TARGET_ID)) == 2
    pressed = make_interaction(admin_member, actor.guild_id)
    assert await confirm.interaction_check(pressed)
    await confirm.confirm.callback(pressed)
    assert first.id not in backend.items
    assert second.id in backend.items
    assert store.get(second.id).owner_id == TARGET_ID
    with pytest.raises(UserError):
        store.get(first.id)


async def test_user_delete_picker_cancel_keeps_all(
    bot, guild, admin_member, target_member, manager, actor, backend
):
    first = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    ctx = await invoke(bot, guild, admin_member, "delete", f"<@{TARGET_ID}>")
    picker = ctx.send.call_args.kwargs["view"]
    choice = make_interaction(admin_member, actor.guild_id, values=[first.id])
    await picker.select.callback(choice)
    confirm = choice.response.send_message.call_args.kwargs["view"]
    pressed = make_interaction(admin_member, actor.guild_id)
    await confirm.cancel.callback(pressed)
    assert len(await manager.list_for(actor, TARGET_ID)) == 2
    assert not any(event.startswith("remove:") for event in backend.events)


async def test_user_delete_picker_is_admin_and_originator_only(bot, guild, admin_member, actor):
    view = UserDeleteView(bot, Actor(actor.user_id, actor.guild_id, True), TARGET_ID, [])
    for member in (make_member(999, guild, admin=True), make_member(actor.user_id, guild)):
        interaction = make_interaction(member, actor.guild_id)
        assert not await view.interaction_check(interaction)


async def test_user_delete_picker_rejects_foreign_owner_selection(
    bot, guild, admin_member, manager, actor, backend
):
    foreign = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    view = UserDeleteView(bot, Actor(actor.user_id, actor.guild_id, True), 999, [foreign])
    view.select.options = [discord.SelectOption(label=foreign.id, value=foreign.id)]
    interaction = make_interaction(admin_member, actor.guild_id, values=[foreign.id])
    await view.select.callback(interaction)
    assert "no longer belongs" in interaction.response.send_message.call_args.args[0]
    assert foreign.id in backend.items


async def test_reinstall_choice_then_confirmation_switches_os(
    bot, guild, admin_member, manager, actor, store, backend
):
    instance = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    owner = make_member(TARGET_ID, guild)
    view = ReinstallOSView(bot, Actor(TARGET_ID, actor.guild_id), instance)
    choice = make_interaction(owner, actor.guild_id, values=["debian-12"])
    assert await view.interaction_check(choice)
    await view.select.callback(choice)
    confirm = choice.response.edit_message.call_args.kwargs["view"]
    assert confirm.os_key == "debian-12"
    assert store.get(instance.id).os == "ubuntu-24.04"
    pressed = make_interaction(owner, actor.guild_id)
    assert await confirm.interaction_check(pressed)
    await confirm.confirm.callback(pressed)
    assert store.get(instance.id).os == "debian-12"
    assert backend.items[instance.id]["os"] == "debian-12"
    assert "wipe:" + instance.id in backend.events


async def test_confirmation_rechecks_revoked_admin(
    bot, guild, admin_member, manager, actor, backend
):
    instance = await manager.deploy(actor, Specs(1, 1, 1), owner_id=TARGET_ID)
    view = ConfirmView(
        manager, Actor(actor.user_id, actor.guild_id, True), instance, "delete", bot=bot
    )
    admin_member.guild_permissions.administrator = False
    interaction = make_interaction(admin_member, actor.guild_id)
    assert await view.interaction_check(interaction)
    with pytest.raises(AccessDenied):
        await manager.delete(view.actor, instance.id)
    assert instance.id in backend.items


async def test_configured_admin_ids_allow_non_guild_admin(settings, manager, backend, guild, actor):
    configured = build_bot(
        replace(settings, admin_ids=frozenset({actor.user_id})), manager, backend
    )
    member = make_member(actor.user_id, guild)
    ctx = await invoke(configured, guild, member, "deploy", "1 1 1")
    assert ctx.send.call_args.kwargs["view"].owner_id == actor.user_id


async def test_user_delete_picker_paginates_over_25(bot, actor):
    now = utc_now()
    instances = [
        Instance(
            f"noir-{index:012x}",
            actor.guild_id,
            TARGET_ID,
            1,
            1,
            1,
            None,
            "running",
            now,
            now,
            os="debian-12" if index % 2 else "ubuntu-24.04",
        )
        for index in range(26)
    ]
    view = UserDeleteView(bot, actor, TARGET_ID, instances)
    assert len(view.select.options) == 25
    assert view.previous.disabled is True
    assert view.next.disabled is False
    interaction = make_interaction(Mock(spec=discord.Member), actor.guild_id)
    await view.next.callback(interaction)
    assert view.page == 1
    assert len(view.select.options) == 1
    assert view.select.options[0].value == instances[25].id
    assert view.next.disabled is True
    assert "Page 2/2" in interaction.response.edit_message.call_args.kwargs["embed"].footer.text
    await view.previous.callback(interaction)
    assert view.page == 0
    assert len(view.select.options) == 25


async def test_manage_reinstall_opens_os_choice_with_banner(
    bot, guild, admin_member, manager, actor
):
    instance = await manager.deploy(actor, Specs(1, 1, 1))
    view = ManageView(bot, actor, [instance], instance.id)
    interaction = make_interaction(admin_member, actor.guild_id)
    await view.reinstall.callback(interaction)
    kwargs = interaction.response.send_message.call_args.kwargs
    assert isinstance(kwargs["view"], ReinstallOSView)
    assert kwargs["ephemeral"] is True
    assert kwargs["file"] is not None
    assert "Debian 12" in str(kwargs["embed"].to_dict())


async def test_deploy_failure_keeps_os_and_owner_context(
    bot, guild, admin_member, target_member, actor, backend
):
    backend.fail_create = True
    ctx = await invoke(bot, guild, admin_member, "deploy", f"1 1 1 <@{TARGET_ID}>")
    view = ctx.send.call_args.kwargs["view"]
    interaction = make_interaction(admin_member, actor.guild_id, values=["debian-12"])
    await view.select.callback(interaction)
    embed = view.message.edit.call_args.kwargs["embed"]
    text = str(embed.to_dict())
    assert "Deployment failed" in text
    assert "Debian 12" in text
    assert f"<@{TARGET_ID}>" in text
    assert view.message.edit.call_args.kwargs["view"] is None
