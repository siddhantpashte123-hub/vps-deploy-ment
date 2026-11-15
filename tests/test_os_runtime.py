from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from docker.errors import ImageNotFound

from fier.backend import DockerBackend
from fier.config import Settings
from fier.errors import BackendError, UserError
from fier.models import Specs
from fier.os_catalog import DEFAULT_OS, OS_CHOICES, os_choice, os_label

ROOT = Path(__file__).resolve().parents[1]


def test_os_catalog_and_labels():
    assert DEFAULT_OS == "ubuntu-24.04"
    assert {choice.id for choice in OS_CHOICES} == {"ubuntu-24.04", "debian-12"}
    assert os_label("ubuntu-24.04") == "Ubuntu 24.04 LTS"
    assert os_label("debian-12") == "Debian 12 (bookworm)"
    assert os_choice("debian-12").base_image == "debian:12"
    with pytest.raises(UserError, match="ubuntu-24.04, debian-12"):
        os_choice("windows-11")


def test_per_os_images_and_legacy_fallback(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "x" * 60)
    monkeypatch.setenv("FIER_IMAGE", "legacy:ubuntu")
    monkeypatch.delenv("FIER_IMAGE_UBUNTU", raising=False)
    monkeypatch.delenv("FIER_IMAGE_DEBIAN", raising=False)
    settings = Settings.load()
    assert settings.image_for("ubuntu-24.04") == "legacy:ubuntu"
    assert settings.image_for("debian-12") == "fier-vps:debian-12"
    monkeypatch.setenv("FIER_IMAGE_UBUNTU", "custom:ubuntu")
    monkeypatch.setenv("FIER_IMAGE_DEBIAN", "custom:debian")
    settings = Settings.load()
    assert settings.images == {"ubuntu-24.04": "custom:ubuntu", "debian-12": "custom:debian"}
    with pytest.raises(UserError):
        settings.image_for("fedora-99")
    with pytest.raises(TypeError):
        settings.images["debian-12"] = "mutated"


@pytest.mark.parametrize("name", ["FIER_IMAGE_UBUNTU", "FIER_IMAGE_DEBIAN"])
def test_invalid_per_os_image_rejected(monkeypatch, name):
    monkeypatch.setenv("DISCORD_TOKEN", "x" * 60)
    monkeypatch.setenv(name, "bad image")
    with pytest.raises(UserError, match="configured image"):
        Settings.load()


@pytest.mark.parametrize("os_key", [choice.id for choice in OS_CHOICES])
def test_backend_selects_and_labels_requested_os(settings, os_key):
    client = Mock()
    container = Mock()
    container.id = "container-id"
    container.attrs = {"State": {"Running": True}}
    client.containers.run.return_value = container
    backend = DockerBackend(settings, client)
    with patch.object(backend, "_helper"):
        backend.create("fier-a1b2c3d4e5f6", Specs(1, 1, 1), 1, 2, os_key=os_key)
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == settings.image_for(os_key)
    assert kwargs["labels"]["fier.os"] == os_key


def test_preflight_requires_every_os_image(settings):
    client = Mock()
    client.info.return_value = {}
    client.images.get.side_effect = lambda image: (
        Mock()
        if image == settings.image_for("ubuntu-24.04")
        else (_ for _ in ()).throw(ImageNotFound(image))
    )
    backend = DockerBackend(settings, client)
    with pytest.raises(BackendError, match="Debian 12.*missing"):
        backend.preflight()
    assert client.images.get.call_count == 2


def test_runtime_dockerfiles_and_installer_build_both_contexts():
    installer = (ROOT / "install.sh").read_text()
    for choice in OS_CHOICES:
        directory = "ubuntu" if choice.id == "ubuntu-24.04" else "debian"
        dockerfile = ROOT / "runtime" / directory / "Dockerfile"
        text = dockerfile.read_text()
        assert text.splitlines()[0] == f"FROM {choice.base_image}"
        assert "COPY sshx /usr/local/bin/sshx" in text
        assert "COPY fier-sshx /usr/local/bin/fier-sshx" in text
        assert "USER 0:0" in text
        tag = "fier-vps:local" if directory == "ubuntu" else "fier-vps:debian-12"
        assert (
            f"docker build --pull -t {tag} -f runtime/{directory}/Dockerfile runtime" in installer
        )
        assert f"$SOURCE_DIR/runtime/{directory}/Dockerfile" in installer
    assert (ROOT / "runtime" / "fier-sshx").is_file()
    assert "FIER_ALLOWED_GUILD_ID" in installer
    assert "guild_id=%s&disable_guild_select=true" in installer
    assert "locked to server ID %s" in installer
