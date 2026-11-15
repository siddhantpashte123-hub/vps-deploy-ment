import io
import json
import stat
import urllib.error
from unittest.mock import Mock, patch

import pytest
from dotenv import dotenv_values

from fier.cli import validate_token, write_env
from fier.config import Settings
from fier.errors import FierError
from fier.models import Actor, Instance, utc_now
from fier.ui import (
    ConfirmView,
    ManageView,
    SSHXView,
    deploy_progress_embed,
    instance_embed,
)


def test_env_atomic_private_and_preserves_other_settings(tmp_path):
    path = tmp_path / "bot.env"
    path.write_text("FIER_MAX_RAM_GB=8\n# comment\n")
    write_env(path, {"DISCORD_TOKEN": "secret-token", "FIER_CLIENT_ID": "123"})
    write_env(path, {"DISCORD_TOKEN": "rotated-token"})
    values = dotenv_values(path)
    assert values["DISCORD_TOKEN"] == "rotated-token"
    assert values["FIER_MAX_RAM_GB"] == "8"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "# comment" in path.read_text()


def test_token_validation_rejects_bad_token():
    with pytest.raises(FierError):
        validate_token("not-a-token")


def test_token_validation_api():
    response = Mock()
    response.__enter__ = Mock(
        return_value=io.BytesIO(json.dumps({"id": "123", "bot": True, "username": "FIER"}).encode())
    )
    response.__exit__ = Mock(return_value=False)
    with patch("fier.cli.urllib.request.urlopen", return_value=response):
        assert validate_token("a" * 60)["id"] == "123"
    with patch(
        "fier.cli.urllib.request.urlopen",
        side_effect=urllib.error.HTTPError("url", 401, "unauthorized", {}, None),
    ):
        with pytest.raises(FierError, match="rejected"):
            validate_token("a" * 60)


def test_setup_keep_existing_token_does_not_prompt(tmp_path, monkeypatch):
    env = tmp_path / "fier.env"
    env.write_text("DISCORD_TOKEN=" + "a" * 60 + "\n")
    monkeypatch.setattr("fier.cli.validate_token", lambda token: {"id": "123", "username": "FIER"})
    monkeypatch.setattr("fier.cli.getpass.getpass", lambda prompt: pytest.fail("prompted"))
    from fier.cli import setup

    setup(env, keep_existing=True)
    assert dotenv_values(env)["DISCORD_TOKEN"] == "a" * 60
    assert dotenv_values(env)["FIER_CLIENT_ID"] == "123"


def test_settings_repr_hides_token(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "sensitive-token-value")
    monkeypatch.setenv("FIER_ADMIN_IDS", "123,456")
    settings = Settings.load()
    assert "sensitive-token-value" not in repr(settings)
    assert settings.admin_ids == frozenset({123, 456})


def sample_instance():
    now = utc_now()
    return Instance("fier-a1b2c3d4e5f6", 1, 2, 1, 1, 1, "c", "running", now, now)


def test_embed_contains_no_terminal_credentials():
    embed = instance_embed(sample_instance())
    text = str(embed.to_dict())
    assert "fier-a1b2c3d4e5f6" in text
    assert "sshx.io/s/" not in text


def test_management_buttons_present():
    bot = Mock()
    view = ManageView(bot, Actor(2, 1), [sample_instance()])
    labels = {child.label for child in view.children}
    assert {"Start", "Stop", "Reinstall", "SSHX", "Delete", "Refresh"} <= labels
    confirm = ConfirmView(Mock(), Actor(2, 1), sample_instance(), "delete")
    assert confirm.confirm.label == "Delete permanently"


def test_sshx_link_is_a_button_not_embed_description():
    url = "https://sshx.io/s/private#capability"
    view = SSHXView(Mock(), Actor(2, 1), "fier-a1b2c3d4e5f6", url)
    assert any(child.url == url for child in view.children)
    assert view.timeout == 300
    assert any(child.label == "Open root terminal" for child in view.children)


def test_deploy_progress_meter_shows_percent_and_phases():
    from fier.models import Specs

    embed = deploy_progress_embed(Specs(1, 1, 1), "storage", 50)
    text = str(embed.to_dict())
    assert "50%" in text
    assert "Prepare persistent disk" in text
    assert "Create root container" in text
    assert "█" in text and "░" in text
    complete = deploy_progress_embed(Specs(1, 1, 1), "online", 100)
    assert "100%" in str(complete.to_dict())


def test_premium_instance_embed_advertises_root_without_credentials():
    embed = instance_embed(sample_instance())
    text = str(embed.to_dict())
    assert "Root terminal" in text
    assert "sshx.io/s/" not in text
