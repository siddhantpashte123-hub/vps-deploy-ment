# FIER — Discord VPS Deploy Bot

Control your own Ubuntu development containers from Discord:

```text
!deploy 1 1 1     RAM (GiB) · CPU cores · disk (GiB)
!delete 1 1 1     match one of YOUR VPSes, then confirm
!manage           Start · Stop · Reinstall · SSHX · Delete
```

The installer installs dependencies, builds the VPS image, **asks for your bot token with hidden input**, validates it with Discord, starts a systemd service, and waits for the bot to report ready.

> **What “VPS” means here:** each instance is a Docker container on *your* host, not a new cloud server from a provider. It includes Ubuntu 24.04, Python, Node.js/npm, Git, build tools, tmux, and common utilities. It runs as a non-root user with a read-only system filesystem; use Python virtual environments and npm's home-directory prefix for packages. It does not provide a separate kernel, systemd, root `apt install`, or a public IP.

## 1. Prepare the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application named **FIER**.
2. Open **Bot**, reset/copy the token, and enable **Message Content Intent**. This privileged intent is required for `!` commands. Keep Server Members Intent off.
3. Keep the token private. **Do not paste it into Discord or commit it to Git.** The installer asks for it in your server terminal.

## 2. One-command install on your VPS

Use a dedicated **Ubuntu 22.04+ / Debian 12+** VPS with systemd, root/sudo access, x86_64 or ARM64, at least 2 GB RAM, and several GB of free disk. Outbound HTTPS must reach Discord, Docker Hub, Ubuntu/PyPI package mirrors, GitHub, the official SSHX download, and `sshx.io`.

Run this on the VPS (one shell command):

```bash
curl -fsSL https://raw.githubusercontent.com/siddhantpashte123-hub/vps-deploy-ment/main/install.sh -o /tmp/fier-install.sh && sudo bash /tmp/fier-install.sh
```

The installer will:

- install Python, Docker, and ext4/loop-mount utilities;
- install the app under `/opt/fier` and create an unprivileged `fier` service user;
- download the official SSHX client and record its SHA-256;
- build `fier-vps:local`;
- prompt: `Paste bot token:` (hidden input);
- save the token in `/etc/fier/fier.env` with mode `0600`;
- enable/start `fier.service` and check Discord readiness;
- print the bot invite URL.

Invite the bot with the printed URL. It needs **View Channels, Send Messages, Embed Links, and Read Message History**. It does **not** need Administrator in Discord. Enable Message Content Intent before starting; otherwise Discord rejects the gateway connection.

If you already uploaded/cloned this repository, the local equivalent is:

```bash
sudo bash install.sh
```

Re-running the installer preserves the environment file and existing VPS inventory/disks. Press Enter at the token prompt to keep the existing token. For an unattended repair that reuses the saved token, run `sudo FIER_REPAIR=1 bash install.sh`. Review the script before running it, as with any root installer. To install a different branch deliberately: `sudo FIER_REF=main bash install.sh` (only after that branch contains the code).

## 3. Use it

In a server where the bot is present:

```text
!deploy 1 1 1
!manage
```

The management panel has:

| Button | Behavior |
|---|---|
| **Start** | Start the existing container; files are preserved. |
| **Stop** | Stop processes; the home disk is preserved. |
| **Reinstall** | Confirm first, then **erase all files** and recreate a fresh Ubuntu container with the same ID/resources. |
| **SSHX** | Open a real browser terminal through `sshx.io`; the capability link is sent only to you as an ephemeral Discord response. |
| **Delete** | Confirm first, then remove the container and its disk permanently. |
| **Refresh** | Reconcile the panel with Docker and the inventory. |

Other commands:

```text
!manage fier-a1b2c3d4e5f6   open a specific VPS
!delete fier-a1b2c3d4e5f6   delete by ID (confirmation required)
!delete 1 1 1               delete the unique VPS YOU own with exactly 1 GiB RAM / 1 CPU / 1 GiB disk
!delete                     open the management panel
!help
!ping
```

If several of your VPSes match `!delete 1 1 1`, nothing is deleted; the bot lists IDs so you can choose. Commands work in servers, not DMs. Use `!delete`, not `! delete`.

### Permissions and limits

- **Deploy:** server administrators by default, plus `FIER_ADMIN_IDS` and optionally `FIER_DEPLOY_ROLE_ID`.
- **Manage/delete:** only the owner, server administrators, and configured bot admins; always scoped to the same guild.
- Defaults: max **2 VPSes/user**, **20 total**, plans up to **4 GiB RAM / 4 CPU / 20 GiB disk**.
- RAM/CPU reservations are checked across the host before deployment. Where loop mounts work, disk images are fully allocated with `fallocate` and act as hard filesystem quotas; a 1 GiB safety reserve is required. On hosts without usable loop devices (common in LXC), FIER falls back to per-VPS owned directories: the disk argument is still a reserved host budget, but not a hard filesystem quota. `python -m fier doctor` prints the active storage mode.
- Edit `/etc/fier/fier.env` to change limits or allow a deploy role, then `sudo systemctl restart fier`. See `.env.example`.

### Inside the terminal

```bash
python3 -m venv ~/mybot/.venv
source ~/mybot/.venv/bin/activate
pip install -U pip
# put your application in ~/mybot, then run it inside tmux:
tmux new -s app
```

`tmux` keeps a process running after you close the browser terminal, until the VPS is stopped/reinstalled/deleted. Start does not automatically relaunch your application. For npm, the image sets a writable global prefix under your home. System `apt` is intentionally unavailable to the non-root container user.

**SSHX links grant terminal access.** Do not share them. Closing/hiding the Discord message or letting its buttons expire does not revoke a session; use **Close terminal session**, or stop/reinstall/delete the VPS. SSHX is a third-party service and requires outbound network access.

## Operations

```bash
sudo systemctl status fier
sudo journalctl -u fier -f
sudo systemctl restart fier
sudo systemctl stop fier
sudo /opt/fier/.venv/bin/python -m fier doctor --env-file /etc/fier/fier.env
```

- Inventory: `/var/lib/fier/data/fier.sqlite3` (persists across bot restarts).
- Fixed-size home filesystem images: `/var/lib/fier/disks/`.
- Host mountpoints: `/var/lib/fier/mounts/`.
- Token/config: `/etc/fier/fier.env` (root-only).
- Containers use restart policy `unless-stopped`: running VPSes return after a host/Docker restart; VPSes explicitly stopped through the bot stay stopped.
- Bot restarts reconcile inventory with Docker. Interrupted deployments are marked `error`; use Delete or Reinstall to clean/recreate them.

### Troubleshooting

| Symptom | Check |
|---|---|
| Bot process runs but is not online | Message Content Intent enabled? `journalctl -u fier -n 100`; token valid? Outbound HTTPS to Discord? |
| Commands ignored | Bot invited to this server? Can it see/read/send in the channel? Using `!deploy`, not `! deploy`? Correct allowed guild? |
| “Deploying is restricted” | You must be a server administrator/configured admin, or have the configured deploy role. |
| Docker unavailable | `systemctl status docker`; service user has the `docker` supplementary group; run doctor. |
| Disk helper denied | `/etc/sudoers.d/fier` and root-owned `/usr/local/libexec/fier-volume` installed? Re-run installer. |
| `mount failed: No such file or directory` / loop errors | Usually missing `/dev/loop*` inside LXC. Re-run the installer; it creates loop nodes when allowed, mounts in PID 1's namespace, and otherwise falls back to directory storage. |
| `Permission denied` executing `/opt/fier/.venv/bin/python` | Re-run the installer; it makes the root-owned app/venv traversable by the `fier` service user. |
| Not enough RAM/CPU/disk | Delete unused VPSes or adjust host budgets only if the host actually has capacity. |
| SSHX times out | VPS running? Outbound HTTPS/gRPC to `sshx.io` allowed? Third-party service available? Stop/start and retry. |
| Container missing after manual Docker cleanup | Use Reinstall (wipes data) or Delete. FIER refuses to control unlabeled containers. |

## Security boundaries

This is a **host-administration tool**, not a sandbox for hostile users. Docker socket/group access is effectively host-root access. Run it on a dedicated host, keep deploy permission restricted to trusted people, and never give untrusted users the Docker socket. Containers drop Linux capabilities, disable privilege escalation, run as UID 1000, have no published ports, and use a dedicated bridge with inter-container traffic disabled. They can still make outbound connections and may reach host/LAN endpoints unless you add appropriate host firewall rules. Do not expose this to untrusted tenants without stronger isolation (e.g. separate VMs) and network policy.

The root-owned disk helper accepts only fixed actions, generated IDs, and bounded integer sizes; it does not accept arbitrary paths or shell commands. The bot never receives your Discord token inside a VPS. SSHX URLs are not logged or put in public embeds.

The official SSHX download is a rolling upstream artifact. The installer records its hash in `/opt/fier/runtime/sshx.sha256`. To pin a hash you have reviewed, set `FIER_SSHX_SHA256` before installing; mismatches are rejected.

## Development / verification

Python 3.10+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check fier scripts/fetch_sshx.py scripts/fier-volume runtime/fier-sshx tests
bash -n install.sh
```

Tests use a fake Docker backend and cover ownership, guild isolation, resource reservation, concurrent deploy limits, cleanup failures, persistence, destructive operations, SSHX URL validation/private links, container hardening, token-file permissions, and installer-independent CLI behavior. They do **not** contact Discord or require Docker. CI runs these checks; a real deployment still needs a live Docker host and your bot token.

Project layout:

```text
fier/                 Discord commands, UI, inventory, Docker backend, CLI
runtime/              Ubuntu image + SSHX session helper
scripts/              root disk helper + SSHX downloader
systemd/fier.service  automatic service
tests/                offline unit tests
install.sh            one-command host installer
```
