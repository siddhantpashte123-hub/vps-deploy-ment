#!/usr/bin/env python3
"""Download the official static SSHX client without curl|sh; record its SHA-256.

Upstream publishes a rolling artifact, not versioned release assets. Set
NOIR_SSHX_SHA256 to the SHA-256 of the *binary* you have reviewed to pin
subsequent installations.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import posixpath
import tarfile
import urllib.request
from pathlib import Path

BASE_URL = "https://s3.amazonaws.com/sshx/sshx-{arch}-unknown-linux-musl.tar.gz"
MAX_DOWNLOAD = 64 * 1024 * 1024
MAX_MEMBERS = 64
ELF_MAGIC = b"\x7fELF"


class ArchiveError(Exception):
    """Safe-to-display archive validation failure."""


def _safe_name(name: str) -> str:
    if not name or "\x00" in name or name.startswith("/"):
        raise ArchiveError("Unsafe path in SSHX archive.")
    parts = name.split("/")
    if ".." in parts:
        raise ArchiveError("Unsafe path in SSHX archive.")
    return posixpath.normpath(name)


def extract_sshx_binary(data: bytes) -> bytes:
    """Return the single regular root-level `sshx` file from the archive.

    Accepts `sshx` and `./sshx`, and tolerates harmless directory/metadata
    entries such as macOS AppleDouble `._sshx` files. Never extracts the archive
    to disk, and rejects links, devices, traversal, duplicates, and non-ELF data.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ArchiveError("SSHX archive contains too many entries.")
            candidates = []
            for member in members:
                normalized = _safe_name(member.name)
                if member.size > MAX_DOWNLOAD:
                    raise ArchiveError("SSHX archive entry exceeds the safety limit.")
                if normalized == "sshx":
                    candidates.append(member)
            if len(candidates) != 1:
                raise ArchiveError("Expected exactly one root-level sshx file in archive.")
            member = candidates[0]
            if not member.isfile():
                raise ArchiveError("SSHX entry is not a regular file.")
            source = archive.extractfile(member)
            if source is None:
                raise ArchiveError("Could not read SSHX binary.")
            binary = source.read(MAX_DOWNLOAD + 1)
    except tarfile.TarError as exc:
        raise ArchiveError("Downloaded file is not a valid tar.gz archive.") from exc
    if len(binary) > MAX_DOWNLOAD:
        raise ArchiveError("SSHX binary exceeds the safety limit.")
    if not binary.startswith(ELF_MAGIC):
        raise ArchiveError("SSHX entry is not a Linux ELF binary.")
    return binary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("runtime/sshx"))
    args = parser.parse_args()
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine())
    if arch is None:
        raise SystemExit("SSHX auto-install supports Linux x86_64 and aarch64 only.")
    expected = os.getenv("NOIR_SSHX_SHA256", "").strip().lower()
    if expected and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
        raise SystemExit("NOIR_SSHX_SHA256 must be a 64-character hex digest.")
    if args.output.is_file() and not expected:
        print("Using existing SSHX binary; see runtime/sshx.sha256.")
        return
    if args.output.is_file() and expected:
        actual = hashlib.sha256(args.output.read_bytes()).hexdigest()
        if actual == expected:
            print("Existing SSHX binary matches the configured SHA-256.")
            return
    url = BASE_URL.format(arch=arch)
    print(f"Downloading official SSHX client for {arch}…")
    request = urllib.request.Request(url, headers={"User-Agent": "NOIR-installer/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise SystemExit("SSHX download exceeded the safety limit.")
    try:
        binary = extract_sshx_binary(data)
    except ArchiveError as exc:
        raise SystemExit(f"Unexpected SSHX archive layout; refusing extraction. {exc}") from exc
    digest = hashlib.sha256(binary).hexdigest()
    if expected and digest != expected:
        raise SystemExit("SSHX SHA-256 mismatch; refusing the download.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    temporary.replace(args.output)
    args.output.with_suffix(".sha256").write_text(f"{digest}  sshx\n")
    print(f"SSHX binary SHA-256: {digest}")


if __name__ == "__main__":
    main()
