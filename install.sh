#!/usr/bin/env bash
# FIER one-command installer for a dedicated Ubuntu/Debian VPS with systemd.
# Run as root, or let this script re-exec with sudo. A real TTY is required for
# the hidden Discord token prompt; do not pipe this script into bash.
set -Eeuo pipefail
umask 077

APP_DIR=/opt/fier
STATE_DIR=/var/lib/fier
ENV_FILE=/etc/fier/fier.env
SERVICE=fier.service
REF="${FIER_REF:-main}"
REPO="${FIER_REPO:-siddhantpashte123-hub/vps-deploy-ment}"
TMP_DIR="$(mktemp -d /tmp/fier-install.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

say() { printf '\n\033[1;35mFIER » %s\033[0m\n' "$*"; }
die() { printf '\nFIER installer error: %s\n' "$*" >&2; exit 1; }

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
if [[ ! -f "$SOURCE_DIR/fier/cli.py" || ! -f "$SOURCE_DIR/runtime/Dockerfile" ]]; then
  say "Downloading FIER from GitHub ($REF)"
  ARCHIVE="$TMP_DIR/source.tar.gz"
  curl --fail --location --retry 3 --connect-timeout 20 \
    "https://codeload.github.com/$REPO/tar.gz/refs/heads/$REF" -o "$ARCHIVE"
  mkdir "$TMP_DIR/source"
  tar -xzf "$ARCHIVE" -C "$TMP_DIR/source" --strip-components=1
  SOURCE_DIR="$TMP_DIR/source"
fi
[[ -f "$SOURCE_DIR/fier/cli.py" ]] || die "Downloaded source is incomplete."

say "Installing the bot under $APP_DIR"
if systemctl list-unit-files "$SERVICE" --no-legend | grep -q .; then
  systemctl stop "$SERVICE" || true
fi
id -u fier >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin fier
usermod -aG docker fier
install -d -o root -g root -m 0755 "$APP_DIR"
install -d -o root -g root -m 0755 "$STATE_DIR"
install -d -o fier -g fier -m 0700 "$STATE_DIR/data"
install -d -o root -g root -m 0700 /etc/fier
install -d -o root -g root -m 0755 /usr/local/libexec
# Copy only application files; never copy a developer's .env, .git or venv.
tar -C "$SOURCE_DIR" --exclude='__pycache__' --exclude='.pytest_cache' --exclude='.ruff_cache' \
  -cf - fier scripts runtime systemd requirements.txt requirements-dev.txt pyproject.toml \
  .env.example README.md install.sh | tar -C "$APP_DIR" -xf -
chown -R root:root "$APP_DIR"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --disable-pip-version-check -q -r "$APP_DIR/requirements.txt"
# The installer runs with umask 077; the systemd service runs as `fier` and
# must be able to traverse/read the root-owned application and virtualenv.
chmod -R a+rX "$APP_DIR"

say "Building the Ubuntu VPS image (first run may take a few minutes)"
cd "$APP_DIR"
"$APP_DIR/.venv/bin/python" scripts/fetch_sshx.py --output runtime/sshx
docker build --pull -t fier-vps:local runtime
install -o root -g root -m 0755 scripts/fier-volume /usr/local/libexec/fier-volume
SUDOERS="$TMP_DIR/fier.sudoers"
printf 'fier ALL=(root) NOPASSWD: /usr/local/libexec/fier-volume\n' > "$SUDOERS"
visudo -cf "$SUDOERS" >/dev/null || die "sudoers validation failed."
install -o root -g root -m 0440 "$SUDOERS" /etc/sudoers.d/fier
sudo -u fier sudo -n /usr/local/libexec/fier-volume check fier-000000000000 \
  || die "The unprivileged bot cannot use the disk helper."

say "Connect your Discord bot"
SETUP_ARGS=(setup --env-file "$ENV_FILE")
if [[ ${FIER_REPAIR:-0} == 1 ]]; then
  SETUP_ARGS+=(--keep-existing-token)
fi
"$APP_DIR/.venv/bin/python" -m fier "${SETUP_ARGS[@]}"
chmod 0600 "$ENV_FILE"
chown root:root "$ENV_FILE"
"$APP_DIR/.venv/bin/python" -m fier doctor --env-file "$ENV_FILE"

say "Starting FIER automatically"
install -o root -g root -m 0644 systemd/fier.service "/etc/systemd/system/$SERVICE"
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
  if journalctl -u "$SERVICE" --since "$START_TIME" --no-pager 2>/dev/null | grep -q 'FIER_READY'; then
    READY=1
    break
  fi
  sleep 2
done
if [[ $READY -ne 1 ]]; then
  journalctl -u "$SERVICE" -n 40 --no-pager
  die "Discord readiness was not confirmed within 120 seconds. The service is running; check the log."
fi

CLIENT_ID="$("$APP_DIR/.venv/bin/python" -c 'from dotenv import dotenv_values; print(dotenv_values("/etc/fier/fier.env").get("FIER_CLIENT_ID", ""))')"
say "FIER is online"
printf 'Invite the bot (View Channels, Send Messages, Embed Links, Read Message History):\n'
printf 'https://discord.com/oauth2/authorize?client_id=%s&scope=bot&permissions=84992\n\n' "$CLIENT_ID"
printf 'In your server:  !deploy 1 1 1   →   !manage   →   SSHX\n'
printf 'Deploy is admin-only by default. Configure a deploy role in %s if needed.\n' "$ENV_FILE"
printf 'Logs:     journalctl -u fier -f\nRestart:  systemctl restart fier\nStatus:   systemctl status fier\n'
