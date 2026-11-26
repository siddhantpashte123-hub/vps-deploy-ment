from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from noir.backend import DockerBackend, parse_sshx_url
from noir.errors import BackendError
from noir.models import Specs


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


def test_container_root_runtime_and_real_limits(settings):
    client = Mock()
    container = Mock()
    container.id = "container-id"
    container.attrs = {"State": {"Running": True}}
    client.containers.run.return_value = container
    backend = DockerBackend(settings, client)
    with patch.object(backend, "_helper") as helper:
        result = backend.create("noir-a1b2c3d4e5f6", Specs(2, 3, 4), 123, 456)
    assert result == "container-id"
    helper.assert_called_once_with("create", "noir-a1b2c3d4e5f6", 4)
    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["mem_limit"] == 2 * 1024**3
    assert kwargs["memswap_limit"] == 2 * 1024**3
    assert kwargs["nano_cpus"] == 3_000_000_000
    assert kwargs["pids_limit"] == 256
    assert kwargs["cap_drop"] == ["ALL"]
    assert "CHOWN" in kwargs["cap_add"] and "SETUID" in kwargs["cap_add"]
    assert "SYS_ADMIN" not in kwargs["cap_add"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["read_only"] is False and kwargs["privileged"] is False
    assert kwargs["user"] == "0:0"
    assert kwargs["environment"]["USER"] == "root"
    assert kwargs["labels"]["noir.runtime"] == "root-v2"
    assert "ports" not in kwargs and kwargs["mounts"][0]["Target"] == "/home/noir"
    assert "DISCORD_TOKEN" not in kwargs["environment"]
    assert kwargs["log_config"].type == "json-file"


def test_create_reports_real_progress(settings):
    client = Mock()
    container = Mock()
    container.id = "container-id"
    container.attrs = {"State": {"Running": True}}
    client.containers.run.return_value = container
    backend = DockerBackend(settings, client)
    events = []
    with patch.object(backend, "_helper"):
        backend.create(
            "noir-a1b2c3d4e5f6", Specs(1, 1, 1), 1, 2, lambda s, p: events.append((s, p))
        )
    assert [p for _, p in events] == sorted(p for _, p in events)
    assert events[-1] == ("ready", 95)
    assert {"storage", "container", "boot"} <= {s for s, _ in events}


def test_sshx_exec_runs_as_root(settings):
    backend = DockerBackend(settings, Mock())
    with patch.object(backend, "_exec", return_value=(0, "https://sshx.io/s/a#b\n")) as exec_mock:
        assert backend.sshx("noir-a1b2c3d4e5f6") == "https://sshx.io/s/a#b"
    assert exec_mock.call_args_list[0].args[1] == [
        "/usr/local/bin/noir-sshx",
        "start",
        "noir-a1b2c3d4e5f6",
    ]
    with patch.object(backend, "get") as get:
        container = Mock()
        container.status = "running"
        container.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"ok")
        get.return_value = container
        backend._exec("noir-a1b2c3d4e5f6", ["id"])
    assert container.exec_run.call_args.kwargs["user"] == "0:0"


def test_helper_rejects_path_injection(settings):
    backend = DockerBackend(settings, Mock())
    with pytest.raises(BackendError):
        backend._helper("delete", "../../etc")
    with pytest.raises(BackendError):
        backend._helper("create", "noir-a1b2c3d4e5f6", -1)


def test_helper_fixed_argv_no_shell(settings):
    backend = DockerBackend(settings, Mock())
    completed = SimpleNamespace(returncode=0, stdout="ok\n", stderr="")
    with patch("noir.backend.subprocess.run", return_value=completed) as run:
        backend._helper("create", "noir-a1b2c3d4e5f6", 1)
    assert run.call_args.args[0] == [
        "sudo",
        "-n",
        str(settings.volume_helper),
        "create",
        "noir-a1b2c3d4e5f6",
        "1",
    ]
    assert run.call_args.kwargs.get("shell", False) is False


def test_refuse_unmanaged_container(settings):
    container = Mock()
    container.labels = {"io.github.noir.managed": "false"}
    client = Mock()
    client.containers.get.return_value = container
    backend = DockerBackend(settings, client)
    with pytest.raises(BackendError, match="not managed"):
        backend.get("noir-a1b2c3d4e5f6")


def test_preflight_reports_directory_fallback(settings):
    client = Mock()
    network = Mock()
    network.name = settings.network
    network.attrs = {"Labels": {"io.github.noir.managed": "true"}}
    client.networks.list.return_value = [network]
    client.info.return_value = {"ServerVersion": "29.1.3"}
    backend = DockerBackend(settings, client)
    with patch.object(backend, "_helper", side_effect=["ok", "directory"]):
        info = backend.preflight()
    assert info["noir_storage_mode"] == "directory"
    assert backend.storage_mode == "directory"
