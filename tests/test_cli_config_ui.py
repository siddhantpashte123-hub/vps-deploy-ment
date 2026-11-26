import io
import json
import stat
import urllib.error
from unittest.mock import Mock, patch

import pytest
from dotenv import dotenv_values

from noir.cli import validate_token, write_env
from noir.config import Settings
from noir.errors import NoirError
from noir.models import Actor, Instance, utc_now
from noir.ui import (
    ConfirmView,
    ManageView,
    SSHXView,
    deploy_progress_embed,
    instance_embed,
)


def test_env_atomic_private_and_preserves_other_settings(tmp_path):
    path = tmp_path / "bot.env"
    path.write_text("NOIR_MAX_RAM_GB=8\n# comment\n")
    write_env(path, {"DISCORD_TOKEN": "secret-token", "NOIR_CLIENT_ID": "123"})
    write_env(path, {"DISCORD_TOKEN": "rotated-token"})
    values = dotenv_values(path)
    assert values["DISCORD_TOKEN"] == "rotated-token"
    assert values["NOIR_MAX_RAM_GB"] == "8"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "# comment" in path.read_text()


def test_token_validation_rejects_bad_token():
    with pytest.raises(NoirError):
        validate_token("not-a-token")


def test_token_validation_api():
    response = Mock()
    response.__enter__ = Mock(
        return_value=io.BytesIO(json.dumps({"id": "123", "bot": True, "username": "NOIR"}).encode())
    )
    response.__exit__ = Mock(return_value=False)
    with patch("noir.cli.urllib.request.urlopen", return_value=response):
        assert validate_token("a" * 60)["id"] == "123"
    with patch(
        "noir.cli.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError("url", 401, "unauthorized", {}, None),
    ):
        with pytest.raises(NoirError, match="rejected"):
            validate_token("a" * 60)


def test_setup_keep_existing_token_and_guild_does_not_prompt(tmp_path, monkeypatch):
    env = tmp_path / "noir.env"
    env.write_text("DISCORD_TOKEN=" + "a" * 60 + "\nNOIR_ALLOWED_GUILD_ID=987654321\n")
    monkeypatch.setattr("noir.cli.validate_token", lambda token: {"id": "123", "username": "NOIR"})
    monkeypatch.setattr("noir.cli.getpass.getpass", lambda prompt: pytest.fail("prompted token"))
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("prompted guild"))
    from noir.cli import setup

    setup(env, keep_existing=True)
    values = dotenv_values(env)
    assert values["DISCORD_TOKEN"] == "a" * 60
    assert values["NOIR_CLIENT_ID"] == "123"
    assert values["NOIR_ALLOWED_GUILD_ID"] == "987654321"


def test_setup_prompts_guild_after_token_and_locks_invite(tmp_path, monkeypatch, capsys):
    env = tmp_path / "noir.env"
    prompts = []

    def fake_input(prompt):
        prompts.append(prompt)
        return "987654321"

    monkeypatch.setattr("noir.cli.validate_token", lambda token: {"id": "123", "username": "NOIR"})
    monkeypatch.setattr("noir.cli.getpass.getpass", lambda prompt: "a" * 60)
    monkeypatch.setattr("builtins.input", fake_input)
    from noir.cli import setup

    setup(env)
    values = dotenv_values(env)
    assert values["DISCORD_TOKEN"] == "a" * 60
    assert values["NOIR_ALLOWED_GUILD_ID"] == "987654321"
    assert len(prompts) == 1
    assert "Paste server (guild) ID" in prompts[0]
    out = capsys.readouterr().out
    assert out.index("Token validated") < out.index("NOIR — single-server lock")
    assert "guild_id=987654321" in out
    assert "disable_guild_select=true" in out
    assert "Public Bot" in out


def test_setup_repair_without_saved_guild_prompts_once(tmp_path, monkeypatch):
    env = tmp_path / "noir.env"
    env.write_text("DISCORD_TOKEN=" + "a" * 60 + "\n")
    monkeypatch.setattr("noir.cli.validate_token", lambda token: {"id": "123", "username": "NOIR"})
    monkeypatch.setattr("noir.cli.getpass.getpass", lambda prompt: pytest.fail("prompted token"))
    monkeypatch.setattr("builtins.input", lambda prompt: "987654321")
    from noir.cli import setup

    setup(env, keep_existing=True)
    assert dotenv_values(env)["NOIR_ALLOWED_GUILD_ID"] == "987654321"


@pytest.mark.parametrize("existing_env", [None, "DISCORD_TOKEN=\n"])
def test_setup_repair_without_saved_token_falls_back_to_prompts(
    tmp_path, monkeypatch, capsys, existing_env
):
    env = tmp_path / "noir.env"
    if existing_env is not None:
        env.write_text(existing_env)
    token_prompts = []

    def fake_getpass(prompt):
        token_prompts.append(prompt)
        return "a" * 60

    monkeypatch.setattr("noir.cli.validate_token", lambda token: {"id": "123", "username": "NOIR"})
    monkeypatch.setattr("noir.cli.getpass.getpass", fake_getpass)
    monkeypatch.setattr("builtins.input", lambda prompt: "987654321")
    from noir.cli import setup

    setup(env, keep_existing=True)
    values = dotenv_values(env)
    assert values["DISCORD_TOKEN"] == "a" * 60
    assert values["NOIR_ALLOWED_GUILD_ID"] == "987654321"
    assert token_prompts == ["Paste bot token: "]
    out = capsys.readouterr().out
    assert "No saved Discord token found" in out
    assert "Falling back to interactive setup" in out


def test_prompt_guild_id_retries_invalid_and_keeps_existing(monkeypatch):
    from noir.cli import prompt_guild_id

    answers = iter(["abc", "0", "123456789012345678"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    assert prompt_guild_id() == "123456789012345678"
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    assert prompt_guild_id("987654321") == "987654321"


def test_parse_guild_id_and_invite_url():
    from noir.cli import invite_url, parse_guild_id

    assert parse_guild_id(" 987654321 ") == 987654321
    for bad in ["", "abc", "0", "-1", "12x3", "1" * 26]:
        with pytest.raises(NoirError):
            parse_guild_id(bad)
    url = invite_url("123", "987654321")
    assert "client_id=123" in url
    assert "guild_id=987654321" in url
    assert "disable_guild_select=true" in url


def test_settings_repr_hides_token(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "sensitive-token-value")
    monkeypatch.setenv("NOIR_ADMIN_IDS", "123,456")
    settings = Settings.load()
    assert "sensitive-token-value" not in repr(settings)
    assert settings.admin_ids == frozenset({123, 456})


def sample_instance():
    now = utc_now()
    return Instance("noir-a1b2c3d4e5f6", 1, 2, 1, 1, 1, "c", "running", now, now)


def test_embed_contains_no_terminal_credentials():
    embed = instance_embed(sample_instance())
    text = str(embed.to_dict())
    assert "noir-a1b2c3d4e5f6" in text
    assert "sshx.io/s/" not in text


def test_management_buttons_present():
    bot = Mock()
    view = ManageView(bot, Actor(2, 1), [sample_instance()])
    labels = {child.label for child in view.children}
    assert {"Start", "Stop", "Console", "Stats", "Reinstall", "Refresh", "Delete"} <= labels
    assert all(child.emoji is None for child in view.children if hasattr(child, "emoji"))
    confirm = ConfirmView(Mock(), Actor(2, 1), sample_instance(), "delete")
    assert confirm.confirm.label == "Delete permanently"


def test_sshx_link_is_a_button_not_embed_description():
    url = "https://sshx.io/s/private#capability"
    view = SSHXView(Mock(), Actor(2, 1), "noir-a1b2c3d4e5f6", url)
    assert any(child.url == url for child in view.children)
    assert view.timeout == 300
    assert any(child.label == "Open root terminal" for child in view.children)


def test_deploy_progress_meter_shows_percent_and_phases():
    from noir.models import Specs

    embed = deploy_progress_embed(Specs(1, 1, 1), "storage", 50)
    text = str(embed.to_dict())
    assert "50%" in text
    assert "Prepare persistent disk" in text
    assert "Create root container" in text
    assert "█" in text and "░" in text
    complete = deploy_progress_embed(Specs(1, 1, 1), "online", 100)
    assert "100%" in str(complete.to_dict())


def test_premium_instance_embed_advertises_root_without_credentials():
    from noir.models import LiveStats

    stats = LiveStats(
        online=True,
        uptime_seconds=3725,
        cpu_percent=12.5,
        ram_used_bytes=256 * 1024**2,
        disk_used_bytes=512 * 1024**2,
    )
    embed = instance_embed(sample_instance(), stats, index=1)
    data = embed.to_dict()
    text = str(data)
    assert "VPS #1 — VMID `A1B2C3`" == data["title"]
    assert "`noir-a1b2c3d4e5f6-2`" in data["description"]
    assert "> **Status** Running · Online" in data["description"]
    assert "> **Uptime** 1h 2m" in data["description"]
    assert "> **CPU** 12.5% • **RAM** 256MB / 1024MB (25.0%)" in data["description"]
    assert "Root terminal" in text
    assert "image" not in data
    assert "sshx.io/s/" not in text
