#!/usr/bin/env bash
# NOIR one-command installer for a dedicated Ubuntu/Debian VPS with systemd.
# Run as root, or let this script re-exec with sudo. A real TTY is required for
# the hidden Discord token prompt and the single-server guild ID prompt;
# do not pipe this script into bash.
set -Eeuo pipefail
umask 077

APP_DIR=/opt/noir
STATE_DIR=/var/lib/noir
ENV_FILE=/etc/noir/noir.env
SERVICE=noir.service
REF="${NOIR_REF:-${FIER_REF:-main}}"
REPO="${NOIR_REPO:-${FIER_REPO:-siddhantpashte123-hub/vps-deploy-ment}}"
REPAIR="${NOIR_REPAIR:-${FIER_REPAIR:-0}}"
TMP_DIR="$(mktemp -d /tmp/noir-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

say() { printf '\n\033[1;35mNOIR » %s\033[0m\n' "$*"; }
die() { printf '\nNOIR installer error: %s\n' "$*" >&2; exit 1; }

if [[ ${EUID} -ne 0 ]]; then
  command -v sudo >/dev/null || die "sudo is required (or run this script as root)."
  [[ -f ${BASH_SOURCE[0]} ]] || die "Save this script to a file before running it with sudo."
  exec sudo bash "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
fi
[[ $(uname -s) == Linux ]] || die "Linux is required."
[[ -d /run/systemd/system ]] || die "systemd is required. Use a normal VPS, not a container/WSL."
command -v apt-get >/dev/null || die "This installer supports Ubuntu 22.04+ / Debian 12+."
case "$(uname -m)" in x86_64|aarch64|arm64) ;; *) die "Supported CPUs: x86_64 and aarch64." ;; esac

say "Installing Python, Docker and storage utilities"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip ca-certificates curl \
  docker.io e2fsprogs util-linux sudo coreutils
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10+ is required"' \
  || die "Use Ubuntu 22.04+ or Debian 12+ (Python 3.10+)."
systemctl enable --now docker
systemctl is-active --quiet docker || die "Docker did not start."

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$SOURCE_DIR/noir/cli.py" || ! -f "$SOURCE_DIR/runtime/noir-pinggy" \
  || ! -f "$SOURCE_DIR/runtime/ubuntu/Dockerfile" \
  || ! -f "$SOURCE_DIR/runtime/ubuntu22/Dockerfile" || ! -f "$SOURCE_DIR/runtime/debian/Dockerfile" \
  || ! -f "$SOURCE_DIR/runtime/alpine/Dockerfile" ]]; then
  say "Downloading NOIR from GitHub ($REF)"
  ARCHIVE="$TMP_DIR/source.tar.gz"
  curl --fail --location --retry 3 --connect-timeout 20 \
    "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$ARCHIVE"
  mkdir "$TMP_DIR/source"
  tar -xzf "$ARCHIVE" -C "$TMP_DIR/source" --strip-components=1
  SOURCE_DIR="$TMP_DIR/source"
fi
[[ -f "$SOURCE_DIR/noir/cli.py" ]] || die "Downloaded source is incomplete."

say "Installing the bot under $APP_DIR"
if systemctl list-unit-files "$SERVICE" --no-legend | grep -q .; then
  systemctl stop "$SERVICE" || true
fi
id -u noir >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin noir
usermod -aG docker noir
install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o root -g root -m 0755 "$STATE_DIR"
install -d -o noir -g noir -m 0700 "$STATE_DIR/data"
install -d -o root -g root -m 0700 "$STATE_DIR/backups"
install -d -o root -g root -m 0700 /etc/noir
install -d -o root -g root -m 0755 /usr/local/libexec

# Migrate a pre-NOIR (fier) installation so saved config, VPS disks, and
# SQLite inventory carry over automatically before `noir upgrade` runs.
if systemctl list-unit-files fier.service --no-legend 2>/dev/null | grep -q .; then
  say "Migrating legacy service to NOIR"
  systemctl stop fier.service || true
  systemctl disable fier.service || true
  rm -f /etc/systemd/system/fier.service
  systemctl daemon-reload || true
fi
if [[ -f /etc/fier/fier.env && ! -f "$ENV_FILE" ]]; then
  python3 - "$ENV_FILE" <<'PY'
import pathlib, re, sys
dst = pathlib.Path(sys.argv[1])
lines = []
for raw in pathlib.Path("/etc/fier/fier.env").read_text(encoding="utf-8").splitlines():
    line = re.sub(r"^(\s*)FIER_", r"\1NOIR_", raw)
    line = line.replace("/var/lib/fier", "/var/lib/noir")
    line = line.replace("/usr/local/libexec/fier-volume", "/usr/local/libexec/noir-volume")
    line = line.replace("fier-vps", "noir-vps")
    lines.append(line)
dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  chmod 0600 "$ENV_FILE"
  chown root:root "$ENV_FILE"
  rm -rf /etc/fier
fi
if [[ -d /var/lib/fier ]]; then
  if [[ -d /var/lib/fier/mounts ]]; then
    for m in /var/lib/fier/mounts/*; do
      [[ -e "$m" ]] || continue
      docker rm -f "$(basename "$m")" >/dev/null 2>&1 || true
      umount "$m" >/dev/null 2>&1 || umount -l "$m" >/dev/null 2>&1 || true
    done
  fi
  for sub in disks mounts data; do
    if [[ -d "/var/lib/fier/$sub" ]]; then
      install -d -m 0700 "$STATE_DIR/$sub"
      cp -a "/var/lib/fier/$sub/." "$STATE_DIR/$sub/" 2>/dev/null || true
    fi
  done
  if [[ -f "$STATE_DIR/data/fier.sqlite3" && ! -f "$STATE_DIR/data/noir.sqlite3" ]]; then
    mv "$STATE_DIR/data/fier.sqlite3" "$STATE_DIR/data/noir.sqlite3"
    for suffix in -wal -shm -journal; do
      [[ -f "$STATE_DIR/data/fier.sqlite3$suffix" ]] && \
        mv "$STATE_DIR/data/fier.sqlite3$suffix" "$STATE_DIR/data/noir.sqlite3$suffix" || true
    done
  fi
  chown -R noir:noir "$STATE_DIR/data"
  rm -rf /var/lib/fier
fi
rm -f /etc/sudoers.d/fier /usr/local/libexec/fier-volume
rm -rf /opt/fier
if id -u fier >/dev/null 2>&1; then
  userdel fier >/dev/null 2>&1 || true
fi
docker image rm fier-vps:local fier-vps:debian-12 >/dev/null 2>&1 || true
docker network rm fier-vps >/dev/null 2>&1 || true
# Copy only application files; never copy a developer's .env, .git or venv.
tar -C "$SOURCE_DIR" --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  -cf - noir scripts runtime systemd assets requirements.txt requirements-dev.txt pyproject.toml \
  .env.example README.md install.sh | tar -C "$APP_DIR" -xf -
chown -R root:root "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$APP_DIR/requirements.txt"
# The installer runs with umask 077; the systemd service runs as `noir` and
# must be able to traverse/read the root-owned application and virtualenv.
chmod -R a+rX "$APP_DIR"

say "Building Ubuntu 24.04, Ubuntu 22.04, Debian 12 and Alpine 3.20 VPS images"
cd "$APP_DIR"
"$APP_DIR/.venv/bin/python" scripts/fetch_sshx.py --output runtime/sshx
docker build --pull -t noir-vps:local -f runtime/ubuntu/Dockerfile runtime
docker build --pull -t noir-vps:ubuntu-22.04 -f runtime/ubuntu22/Dockerfile runtime
docker build --pull -t noir-vps:debian-12 -f runtime/debian/Dockerfile runtime
docker build --pull -t noir-vps:alpine-3.20 -f runtime/alpine/Dockerfile runtime
install -o root -g root -m 0755 scripts/noir-volume /usr/local/libexec/noir-volume
SUDOERS="$TMP_DIR/noir.sudoers"
printf 'noir ALL=(root) NOPASSWD: /usr/local/libexec/noir-volume\n' > "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null || die "sudoers validation failed."
install -o root -g root -m 0440 "$SUDOERS" /etc/sudoers.d/noir
sudo -u noir sudo -n /usr/local/libexec/noir-volume check noir-000000000000 \
  || die "The unprivileged bot cannot use the disk helper."

say "Connect your Discord bot"
SETUP_ARGS=(setup --env-file "$ENV_FILE")
if [[ $REPAIR == 1 ]]; then
  SETUP_ARGS+=(--keep-existing-token)
fi
"$APP_DIR/.venv/bin/python" -m noir "${SETUP_ARGS[@]}"
chmod 0600 "$ENV_FILE"
chown root:root "$ENV_FILE"
if ! grep -q '^NOIR_PUBLIC_IP=' "$ENV_FILE" 2>/dev/null; then
  PUB_IP="$(curl -4 -fsSL --max-time 5 https://api.ipify.org 2>/dev/null || true)"
  if [[ "$PUB_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf '\nNOIR_PUBLIC_IP=%s\n' "$PUB_IP" >> "$ENV_FILE"
  fi
fi
"$APP_DIR/.venv/bin/python" -m noir doctor --env-file "$ENV_FILE"

say "Upgrading existing VPS containers to the root runtime (home files are preserved)"
if "$APP_DIR/.venv/bin/python" -m noir upgrade --env-file "$ENV_FILE"; then
  say "Existing VPS containers are on the root runtime"
else
  say "Warning: some existing VPS containers could not be upgraded; starting the bot anyway"
fi
# The upgrade CLI runs as root; keep the inventory writable by the service user.
chown -R noir:noir "$STATE_DIR/data"

say "Starting NOIR automatically"
install -o root -g root -m 0644 systemd/noir.service "/etc/systemd/system/$SERVICE"
chmod -R a+rX "$APP_DIR"
systemctl daemon-reload
systemctl enable "$SERVICE"
START_TIME="$(date -u '+%Y-%m-%d %H:%M:%S')"
systemctl restart "$SERVICE"
READY=0
for _ in $(seq 1 60); do
  if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 40 --no-pager
    die "Bot failed to start. Check Message Content Intent and the log above, then re-run install.sh."
  fi
  if journalctl -u "$SERVICE" --since "$START_TIME" --no-pager 2>/dev/null | grep -q 'NOIR_READY'; then
    READY=1
    break
  fi
  sleep 2
done
if [[ $READY -ne 1 ]]; then
  journalctl -u "$SERVICE" -n 40 --no-pager
  die "Discord readiness was not confirmed within 120 seconds. The service is running; check the log."
fi

CLIENT_ID="$("$APP_DIR/.venv/bin/python" -c 'from dotenv import dotenv_values; print(dotenv_values("/etc/noir/noir.env").get("NOIR_CLIENT_ID", ""))')"
GUILD_ID="$("$APP_DIR/.venv/bin/python" -c 'from dotenv import dotenv_values; print(dotenv_values("/etc/noir/noir.env").get("NOIR_ALLOWED_GUILD_ID", ""))')"
[[ -n "$GUILD_ID" ]] || die "NOIR_ALLOWED_GUILD_ID is missing. Re-run install.sh and enter your server ID."
say "NOIR is online"
printf 'This bot is locked to server ID %s and will leave any other server.\n' "$GUILD_ID"
printf 'Invite it to that server (View Channels, Send Messages, Embed Links, Read Message History):\n'
printf 'https://discord.com/oauth2/authorize?client_id=%s&scope=bot&permissions=84992&guild_id=%s&disable_guild_select=true\n\n' "$CLIENT_ID" "$GUILD_ID"
printf 'In your server:  !deploy 1 1 1 @user [30d]   →   Ubuntu 24.04 / 22.04 / Debian 12 / Alpine 3.20\n'
printf 'Promo codes:     !gencode 1 1 2 30d 5        →   members claim via !redeem <CODE> or /redeem\n'
printf 'Host dashboard:  !status   →   admin-only live Host Node CPU/RAM/Disk/Fleet dashboard\n'
printf 'Fleet browser:   !list [@user]   →   paginated admin VPS browser & quick manage\n'
printf 'Admin controls:  !suspend <id> [reason] • !unsuspend <id> • !resize <id> RAM CPU DISK • !renew <id> 30d\n'
printf 'Server hub:      !panel   →   post an interactive NOIR Cloud Panel (My VPS + Control in DM)\n'
printf 'Server or DM:    !manage  →   Console (SSH via Pinggy.io + SSHX) / Tunnel / Ports / Templates / Backup\n'
printf 'Team & Git:      !share <id> @user • !tunnel <id> <port> • !gitdeploy <id> <github_url> • !autobackup <id> on\n'
printf 'Admin ops:       !maintenance on [reason] • !announce <msg> • !netlock <id> on • !delete @user\n'
printf 'Deploy is admin-only (server administrators or NOIR_ADMIN_IDS in %s).\n' "$ENV_FILE"
printf 'Recommended: Developer Portal → Bot → turn OFF Public Bot.\n'
printf 'Logs:     journalctl -u noir -f\nRestart:  systemctl restart noir\nStatus:   systemctl status noir\n'
