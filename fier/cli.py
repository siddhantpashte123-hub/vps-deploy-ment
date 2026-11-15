from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
import shlex
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .errors import FierError


def validate_token(token: str) -> dict:
    if not 40 <= len(token) <= 200 or not re.fullmatch(r"[A-Za-z0-9_.-]+", token):
        raise FierError(
            "That does not look like a Discord bot token. Copy it from Bot → Reset Token."
        )
    request = urllib.request.Request(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {token}", "User-Agent": f"FIER/{__version__}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise FierError(
                "Discord rejected the token. Check that you copied a BOT token."
            ) from exc
        raise FierError(
            f"Discord token validation returned HTTP {exc.code}. Try again later."
        ) from exc
    except (OSError, ValueError) as exc:
        raise FierError(
            "Could not reach Discord to validate the token. Check outbound HTTPS."
        ) from exc
    if not data.get("bot") or not str(data.get("id", "")).isdigit():
        raise FierError("The response was not a Discord bot account. Use the Bot page token.")
    return data


def write_env(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text().splitlines() if path.exists() else []
    seen = set()
    lines = []
    for line in existing:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else None
        )
        if key in updates:
            lines.append(f"{key}={shlex.quote(updates[key])}")
            seen.add(key)
        else:
            lines.append(line)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={shlex.quote(value)}")
    fd, temporary = tempfile.mkstemp(prefix=".env-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def setup(env_path: Path, template: Path | None = None, keep_existing: bool = False) -> None:
    if not env_path.exists() and template and template.exists():
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(template.read_text())
        env_path.chmod(0o600)
    from dotenv import dotenv_values

    current = dotenv_values(env_path).get("DISCORD_TOKEN") or ""
    if keep_existing:
        if not current:
            raise FierError("No existing token found. Run setup without --keep-existing-token.")
        token = current
        print("Reusing existing Discord token from the environment file.")
    else:
        print("\nFIER — Discord bot token")
        print("Developer Portal → your application → Bot → Reset Token → Copy.")
        print("Also enable MESSAGE CONTENT INTENT on that Bot page.")
        print(
            "Input is hidden. The token is stored in a root-readable mode-0600 file, "
            "never in Git.\n"
        )
        token = getpass.getpass(
            "Paste bot token (Enter keeps existing token): " if current else "Paste bot token: "
        ).strip()
        if not token and current:
            token = current
    data = validate_token(token)
    write_env(env_path, {"DISCORD_TOKEN": token, "FIER_CLIENT_ID": str(data["id"])})
    print(f"Token validated for {data.get('username', 'bot')}. Saved securely to {env_path}.")
    print(
        f"Invite: https://discord.com/oauth2/authorize?client_id={data['id']}&scope=bot&permissions=84992"
    )
    print("Enable Message Content Intent in the Developer Portal before starting the bot.")


def run() -> None:
    from .backend import DockerBackend
    from .bot import build_bot
    from .config import Settings
    from .manager import Manager
    from .store import Store

    settings = Settings.load()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(settings.data_dir / "fier.sqlite3")
    backend = DockerBackend(settings)
    manager = Manager(settings, store, backend)
    bot = build_bot(settings, manager, backend)
    try:
        # discord.py installs its own signal handling and closes the gateway cleanly.
        bot.run(settings.token, log_handler=None)
    finally:
        backend.close()
        store.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    parser = argparse.ArgumentParser(prog="fier", description="FIER Discord VPS manager")
    parser.add_argument("command", choices=["run", "setup", "doctor", "version"])
    parser.add_argument("--env-file", type=Path, default=Path("/etc/fier/fier.env"))
    parser.add_argument(
        "--keep-existing-token",
        action="store_true",
        help="with setup: validate and reuse the saved token without prompting",
    )
    args = parser.parse_args()
    try:
        if args.command == "version":
            print(__version__)
        elif args.command == "setup":
            setup(
                args.env_file,
                Path(__file__).resolve().parent.parent / ".env.example",
                keep_existing=args.keep_existing_token,
            )
        elif args.command == "run":
            run()
        else:
            from .backend import DockerBackend
            from .config import Settings

            settings = Settings.load(require_token=False, env_file=args.env_file)
            backend = DockerBackend(settings)
            try:
                info = backend.preflight()
            finally:
                backend.close()
            print(
                f"Docker OK: {info.get('ServerVersion')} · image {settings.image} · "
                f"disk helper OK · storage={info.get('fier_storage_mode', 'unknown')}"
            )
            if info.get("fier_storage_mode") == "directory":
                print(
                    "Note: loop mounts are unavailable on this host; VPS homes use directory "
                    "storage. Disk sizes are reserved budgets, not hard filesystem quotas."
                )
    except FierError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        raise SystemExit(130) from None
