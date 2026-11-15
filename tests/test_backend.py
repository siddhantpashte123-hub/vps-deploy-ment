from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from fier.backend import DockerBackend, parse_sshx_url
from fier.errors import BackendError
from fier.models import Specs


@pytest.mark.parametrize(
    "url",
    [
        "https://sshx.io/s/abc-123#key_456",
        "https://sshx.io/s/session#secret",
    ],
)
def test_valid_sshx_url(url):
    assert parse_sshx_url(url + "\n") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://sshx.io/s/a#b",
        "https://evil.example/s/a#b",
        "https://sshx.io.evil.example/s/a#b",
        "https://user@sshx.io/s/a#b",
        "https://sshx.io:8443/s/a#b",
        "https://sshx.io/s/a#b?x=1",
        "https://sshx.io/s/a",
        "javascript:alert(1)",
        "https://sshx.io/s/a#b\nc",
    ],
)
def test_reject_unsafe_sshx_url(url):
    assert parse_sshx_url(url) is None


def test_container_hardening_and_real_limits(settings):
    client = Mock()
    client.containers.run.return_value = SimpleNamespace(id="container-id")
    backend = DockerBackend(settings, client)
    with patch.object(backend, "_helper") as helper:
        result = backend.create("fier-a1b2c3d4e5f6", Specs(2, 3, 4), 123, 456)
    assert result == "container-id"
    helper.assert_called_once_with("create", "fier-a1b2c3d4e5f6", 4)
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["mem_limit"] == 2 * 1024**3
    assert kwargs["memswap_limit"] == 2 * 1024**3
    assert kwargs["nano_cpus"] == 3_000_000_000
    assert kwargs["pids_limit"] == 256
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["read_only"] is True and kwargs["privileged"] is False
    assert kwargs["user"] == "1000:1000"
    assert "ports" not in kwargs and kwargs["mounts"][0]["Target"] == "/home/fier"
    assert "DISCORD_TOKEN" not in kwargs["environment"]
    assert kwargs["log_config"].type == "json-file"


def test_helper_rejects_path_injection(settings):
    backend = DockerBackend(settings, Mock())
    with pytest.raises(BackendError):
        backend._helper("delete", "../../etc")
    with pytest.raises(BackendError):
        backend._helper("create", "fier-a1b2c3d4e5f6", -1)


def test_helper_fixed_argv_no_shell(settings):
    backend = DockerBackend(settings, Mock())
    completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
    with patch("fier.backend.subprocess.run", return_value=completed) as run:
        backend._helper("create", "fier-a1b2c3d4e5f6", 1)
    assert run.call_args.args[0] == [
        "sudo",
        "-n",
        str(settings.volume_helper),
        "create",
        "fier-a1b2c3d4e5f6",
        "1",
    ]
    assert run.call_args.kwargs.get("shell", False) is False


def test_refuse_unmanaged_container(settings):
    container = Mock()
    container.labels = {"io.github.fier.managed": "false"}
    client = Mock()
    client.containers.get.return_value = container
    backend = DockerBackend(settings, client)
    with pytest.raises(BackendError, match="not managed"):
        backend.get("fier-a1b2c3d4e5f6")
