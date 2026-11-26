from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from docker.errors import ImageNotFound

from noir.backend import DockerBackend
from noir.config import Settings
from noir.errors import BackendError, UserError
from noir.models import Specs
from noir.os_catalog import DEFAULT_OS, OS_CHOICES, os_choice, os_label

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
    monkeypatch.setenv("NOIR_IMAGE", "legacy:ubuntu")
    monkeypatch.delenv("NOIR_IMAGE_UBUNTU", raising=False)
    monkeypatch.delenv("NOIR_IMAGE_DEBIAN", raising=False)
    settings = Settings.load()
    assert settings.image_for("ubuntu-24.04") == "legacy:ubuntu"
    assert settings.image_for("debian-12") == "noir-vps:debian-12"
    monkeypatch.setenv("NOIR_IMAGE_UBUNTU", "custom:ubuntu")
    monkeypatch.setenv("NOIR_IMAGE_DEBIAN", "custom:debian")
    settings = Settings.load()
    assert settings.images == {"ubuntu-24.04": "custom:ubuntu", "debian-12": "custom:debian"}
    with pytest.raises(UserError):
        settings.image_for("fedora-99")
    with pytest.raises(TypeError):
        settings.images["debian-12"] = "mutated"


@pytest.mark.parametrize("name", ["NOIR_IMAGE_UBUNTU", "NOIR_IMAGE_DEBIAN"])
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
        backend.create("noir-a1b2c3d4e5f6", Specs(1, 1, 1), 1, 2, os_key=os_key)
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == settings.image_for(os_key)
    assert kwargs["labels"]["noir.os"] == os_key


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
        assert "COPY noir-sshx /usr/local/bin/noir-sshx" in text
        assert "USER 0:0" in text
        tag = "noir-vps:local" if directory == "ubuntu" else "noir-vps:debian-12"
        assert (
            f"docker build --pull -t {tag} -f runtime/{directory}/Dockerfile runtime" in installer
        )
        assert f"$SOURCE_DIR/runtime/{directory}/Dockerfile" in installer
    assert (ROOT / "runtime" / "noir-sshx").is_file()
    assert (ROOT / "assets" / "noir-banner.png").is_file()
    assert not (ROOT / "assets" / "fier-banner.png").exists()
    assert "NOIR_ALLOWED_GUILD_ID" in installer
    assert 'REPAIR="${NOIR_REPAIR:-${FIER_REPAIR:-0}}"' in installer
    assert "/etc/fier/fier.env" in installer
    assert "guild_id=%s&disable_guild_select=true" in installer
    assert "locked to server ID %s" in installer


def test_legacy_fier_ids_and_labels_remain_compatible(settings):
    from unittest.mock import Mock

    from noir.models import valid_id

    legacy_id = "fier-7d972345ed1a"
    new_id = "noir-7d972345ed1a"
    assert valid_id(legacy_id)
    assert valid_id(new_id)
    container = Mock()
    container.labels = {"io.github.fier.managed": "true", "fier.id": legacy_id}
    client = Mock()
    client.containers.get.return_value = container
    backend = DockerBackend(settings, client)
    assert backend.get(legacy_id) is container
