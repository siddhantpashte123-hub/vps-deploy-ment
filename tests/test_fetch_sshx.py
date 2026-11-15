import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_sshx.py"
spec = importlib.util.spec_from_file_location("fetch_sshx", SCRIPT)
fetch_sshx = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch_sshx)

ELF = b"\x7fELF" + b"fake-binary"


def make_tar(entries: list[tuple[str, bytes, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data, kind in entries:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.type = kind
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


def test_accepts_plain_root_binary():
    data = make_tar([("sshx", ELF, tarfile.REGTYPE)])
    assert fetch_sshx.extract_sshx_binary(data) == ELF


def test_accepts_dot_slash_and_appledouble_metadata():
    data = make_tar(
        [
            ("./", b"", tarfile.DIRTYPE),
            ("._sshx", b"mac-metadata", tarfile.REGTYPE),
            ("./sshx", ELF, tarfile.REGTYPE),
        ]
    )
    assert fetch_sshx.extract_sshx_binary(data) == ELF


def test_rejects_missing_binary():
    data = make_tar([("sshx-server", ELF, tarfile.REGTYPE)])
    with pytest.raises(fetch_sshx.ArchiveError):
        fetch_sshx.extract_sshx_binary(data)


def test_rejects_duplicate_root_binary():
    data = make_tar([("sshx", ELF, tarfile.REGTYPE), ("./sshx", ELF, tarfile.REGTYPE)])
    with pytest.raises(fetch_sshx.ArchiveError):
        fetch_sshx.extract_sshx_binary(data)


def test_rejects_symlink_binary():
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo("sshx")
        member.type = tarfile.SYMTYPE
        member.linkname = "/bin/sh"
        archive.addfile(member)
    with pytest.raises(fetch_sshx.ArchiveError):
        fetch_sshx.extract_sshx_binary(buffer.getvalue())


def test_rejects_traversal():
    data = make_tar([("../sshx", ELF, tarfile.REGTYPE)])
    with pytest.raises(fetch_sshx.ArchiveError):
        fetch_sshx.extract_sshx_binary(data)


def test_rejects_non_elf():
    data = make_tar([("sshx", b"<html>error</html>", tarfile.REGTYPE)])
    with pytest.raises(fetch_sshx.ArchiveError):
        fetch_sshx.extract_sshx_binary(data)
