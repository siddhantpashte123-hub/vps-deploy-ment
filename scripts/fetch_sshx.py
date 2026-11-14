#!/usr/bin/env python3
"""Download the official static SSHX client without curl|sh; record its SHA-256.

Upstream publishes a rolling artifact, not versioned release assets. Set
FIER_SSHX_SHA256 to a hash you have reviewed to pin subsequent installations.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

BASE_URL = "https://s3.amazonaws.com/sshx/sshx-{arch}-unknown-linux-musl.tar.gz"
MAX_DOWNLOAD = 64 * 1024 * 1024


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("runtime/sshx"))
    args = parser.parse_args()
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(platform.machine())
    if arch is None:
        raise SystemExit("SSHX auto-install supports Linux x86_64 and aarch64 only.")
    expected = os.getenv("FIER_SSHX_SHA256", "").strip().lower()
    if expected and (len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected)):
        raise SystemExit("FIER_SSHX_SHA256 must be a 64-character hex digest.")
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
    request = urllib.request.Request(url, headers={"User-Agent": "FIER-installer/1.0"})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise SystemExit("SSHX download exceeded the safety limit.")
    digest = hashlib.sha256(data).hexdigest()
    if expected and digest != expected:
        raise SystemExit("SSHX SHA-256 mismatch; refusing the download.")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        members = archive.getmembers()
        if len(members) != 1 or members[0].name != "sshx" or not members[0].isfile():
            raise SystemExit("Unexpected SSHX archive layout; refusing extraction.")
        if members[0].size > MAX_DOWNLOAD:
            raise SystemExit("SSHX binary exceeds the safety limit.")
        source = archive.extractfile(members[0])
        if source is None:
            raise SystemExit("Could not read SSHX binary.")
        binary = source.read(MAX_DOWNLOAD + 1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(".tmp")
    temporary.write_bytes(binary)
    temporary.chmod(0o755)
    temporary.replace(args.output)
    args.output.with_suffix(".sha256").write_text(f"{digest}  sshx\n")
    print(f"SSHX SHA-256: {digest}")


if __name__ == "__main__":
    main()
