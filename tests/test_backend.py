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


def test_backend_stats_parses_live_docker_telemetry(settings):
    from datetime import datetime, timedelta, timezone

    started = (datetime.now(timezone.utc) - timedelta(seconds=90)).strftime(
        "%Y-%m-%dT%H:%M:%S.123456789Z"
    )
    container = Mock()
    container.labels = {"io.github.noir.managed": "true", "noir.id": "noir-a1b2c3d4e5f6"}
    container.status = "running"
    container.attrs = {"State": {"Running": True, "StartedAt": started}}
    container.stats.return_value = {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 200_000_000},
            "system_cpu_usage": 2_000_000_000,
            "online_cpus": 2,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 100_000_000},
            "system_cpu_usage": 1_000_000_000,
        },
        "memory_stats": {"usage": 300 * 1024**2, "stats": {"inactive_file": 44 * 1024**2}},
    }
    container.exec_run.return_value = Mock(exit_code=0, output=b"Used\n536870912\n")
    client = Mock()
    client.containers.get.return_value = container
    backend = DockerBackend(settings, client)
    live = backend.stats("noir-a1b2c3d4e5f6")
    assert live.online is True
    assert 85 <= live.uptime_seconds <= 105
    assert abs(live.cpu_percent - 20.0) < 0.01
    assert live.ram_used_bytes == 256 * 1024**2
    assert live.disk_used_bytes == 512 * 1024**2


def test_pinggy_and_sshx_helper_domain_and_url_extraction():
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent

    def load_script(name: str):
        path = str(root / "runtime" / name)
        loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    pinggy = load_script("noir-pinggy")
    # Current Pinggy free TCP & HTTPS domains (*.run.pinggy-free.link, *.free.pinggy.net, etc.)
    for sample, expected_host, expected_port in [
        (
            "\x1b[2J\x1b[1;1Htcp://rndnj-103-170-183.run.pinggy-free.link:37779\r\n",
            "rndnj-103-170-183.run.pinggy-free.link",
            "37779",
        ),
        (
            "tcp://abc-123.a.free.pinggy.link:41234",
            "abc-123.a.free.pinggy.link",
            "41234",
        ),
        (
            "tcp://xyz.free.pinggy.net:29999",
            "xyz.free.pinggy.net",
            "29999",
        ),
    ]:
        cleaned = pinggy._strip_ansi(sample)
        m = pinggy.TCP_URL_RE.search(cleaned)
        assert m is not None
        assert (m.group(1), m.group(2)) == (expected_host, expected_port)

    for https_sample, expected_url in [
        (
            "Tunnel ready: https://app-123.run.pinggy-free.link\r\n",
            "https://app-123.run.pinggy-free.link",
        ),
        (
            "https://demo.free.pinggy.net",
            "https://demo.free.pinggy.net",
        ),
    ]:
        m_http = pinggy.HTTPS_URL_RE.search(pinggy._strip_ansi(https_sample))
        assert m_http is not None
        assert m_http.group(0) == expected_url

    sshx_mod = load_script("noir-sshx")
    ansi_sshx = "  ➜  Link: \x1b[36mhttps://sshx.io/s/AbCdEf1234#Key_987654\x1b[0m\r\n"
    m_sshx = sshx_mod.URL_SEARCH.search(sshx_mod._strip_ansi(ansi_sshx))
    assert m_sshx is not None
    assert m_sshx.group(0) == "https://sshx.io/s/AbCdEf1234#Key_987654"


def test_sshx_auto_provisions_when_initial_start_fails(settings):
    backend = DockerBackend(settings, Mock())
    calls = [
        (127, "noir-sshx missing"),
        (0, "https://sshx.io/s/abc12345#secret99\n"),
        (0, "https://sshx.io/s/abc12345#secret99\n"),
    ]
    with (
        patch.object(backend, "_exec", side_effect=calls),
        patch.object(backend, "_sync_runtime_script") as sync_mock,
        patch.object(backend, "_ensure_sshx_binary") as bin_mock,
    ):
        url = backend.sshx("noir-a1b2c3d4e5f6")
    assert url == "https://sshx.io/s/abc12345#secret99"
    sync_mock.assert_called_once()
    bin_mock.assert_called_once()

