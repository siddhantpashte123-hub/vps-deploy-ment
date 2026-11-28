# NOIR — Discord VPS Deploy Bot

Admin-created Ubuntu 24.04 / Ubuntu 22.04 / Debian 12 / Alpine 3.20 development containers, controlled from your locked Discord server **and directly in bot DMs** (both `!` prefix and `/` slash commands):

```text
!deploy 1 1 1 @user [30d]       admin only: RAM · CPU · disk · optional @user & expiry timer
!gencode 1 1 2 [30d] [uses]     admin only: generate redeemable promo/trial codes (!codes to list)
!redeem <CODE>                  any member: redeem a promo code & choose OS to deploy instantly
!status                         admin only: live Host Node dashboard (CPU, RAM, Disk, Fleet)
!list [@user]                   admin only: paginated Fleet Browser + quick-manage selector
!suspend <id> [reason]          admin only: stop & lock a VPS while keeping /home/noir safe
!unsuspend <id>                 admin only: unlock & start a suspended VPS
!resize <id> RAM CPU DISK       admin only: live plan upgrade without wiping /home/noir
!renew <id> <30d|never>         admin only: extend or clear automatic expiry timer
!netlock <id> <on|off>          admin only: isolate/restore outbound container networking
!maintenance <on|off> [reason]  admin only: pause non-admin operations during host maintenance
!announce <message>             admin only: broadcast an official DM notice to all VPS owners
!panel                          admin only: post an interactive NOIR Cloud Panel hub card
!delete @user                   admin only: pick one of that member's VPSes, then confirm
!manage                         server or DMs: Console (SSH + SSHX) · Tunnel · Ports · Rename · Logs · Templates · Backup · Restore
!ssh [id]                       owner/sub-user: direct Pinggy.io TCP root SSH tunnel + credentials
!tunnel <id> <port>             owner/sub-user: expose any container HTTP port as a public HTTPS URL (Pinggy.io)
!share <id> @user / !unshare    owner or admin: grant/revoke teammate sub-user access
!autobackup <id> <on|off>       owner or admin: automatic 24-hour /home/noir snapshots
!gitdeploy <id> <github_url>    owner or admin: clone/pull a GitHub repo & install dependencies
!port add <id> <port>           owner or admin: forward a public host TCP port to your container
!rename <id> <name>             owner or admin: set a custom nickname for your VPS
!exec <id> <cmd> / !logs <id>   owner or sub-user: quick command exec or active process/port snapshot
!template <id> [template]       owner or sub-user: 1-click Discord.py / Node.js / FastAPI / Express starter
!sshkey <pubkey|clear>          owner: sync your SSH public key to /root/.ssh & /home/noir/.ssh
!transfer <id> @user            owner or admin: transfer VPS ownership to another member
```

The installer installs dependencies, builds both VPS images, **asks for your bot token with hidden input**, validates it with Discord, then asks for the **one Discord server (guild) ID** the bot may stay in, starts a systemd service, and waits for the bot to report ready. The bot is locked to that server and automatically leaves any other server.

> **What “VPS” means here:** each instance is a Docker container on *your* host, not a new cloud server from a provider. Choose **Ubuntu 24.04 LTS** or **Debian 12 (bookworm)** at deploy and reinstall time. Both include Python, Node.js/npm, Git, build tools, tmux, and common utilities. SSHX opens directly as **root inside that container**, so `apt update` / `apt install` work. It is still container isolation: no separate kernel, no host systemd, no public IP, and no host root access is intended.

## 1. Prepare the Discord application

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and create an application named **NOIR**.
2. Open **Bot**, reset/copy the token, and enable **Message Content Intent**. This privileged intent is required for `!` commands. Server Members Intent is not required; mentioned members are resolved from the message or the REST API.
3. Recommended: turn **Public Bot** off so only you can invite it. NOIR also locks itself to the one server ID you enter during setup and leaves any other server automatically.
4. Copy your Discord **Server ID**: User Settings → Advanced → Developer Mode ON, then right-click the server icon → Copy Server ID. The installer asks for this right after the bot token.
5. Keep the token private. **Do not paste it into Discord or commit it to Git.** The installer asks for it in your server terminal.

## 2. One-command install on your VPS

Use a dedicated **Ubuntu 22.04+ / Debian 12+** VPS with systemd, root/sudo access, x86_64 or ARM64, at least 2 GB RAM, and several GB of free disk. Outbound HTTPS must reach Discord, Docker Hub, Ubuntu/Debian/PyPI package mirrors, GitHub, the official SSHX download, and `sshx.io`.

Run this on the VPS (one shell command):

```bash
curl -fsSL https://raw.githubusercontent.com/siddhantpashte123-hub/vps-deploy-ment/main/install.sh -o /tmp/noir-install.sh && sudo bash /tmp/noir-install.sh
```

The installer will:

- install Python, Docker, and ext4/loop-mount utilities;
- install the app under `/opt/noir` and create an unprivileged `noir` service user;
- download the official SSHX client and record its SHA-256;
- build `noir-vps:local` (Ubuntu 24.04) and `noir-vps:debian-12` (Debian 12);
- prompt: `Paste bot token:` (hidden input);
- prompt: `Paste server (guild) ID:` — the **only** Discord server this bot may stay in;
- save the token and `NOIR_ALLOWED_GUILD_ID` in `/etc/noir/noir.env` with mode `0600`;
- enable/start `noir.service` and check Discord readiness;
- print a server-locked bot invite URL.

Invite the bot with the printed URL. It needs **View Channels, Send Messages, Embed Links, and Read Message History**. It does **not** need Administrator in Discord. Enable Message Content Intent before starting; otherwise Discord rejects the gateway connection. If someone adds the bot to another server, NOIR leaves that server automatically and refuses commands there.

If you already uploaded/cloned this repository, the local equivalent is:

```bash
sudo bash install.sh
```

Re-running the installer preserves the environment file and existing VPS inventory/home disks. It also recreates existing VPS containers on the current root runtime **using each VPS's recorded OS** without wiping their `/home/noir` files. Existing inventory rows migrate to Ubuntu 24.04 automatically. Press Enter at the token prompt to keep the existing token. For a repair/upgrade that reuses the saved token, run `sudo NOIR_REPAIR=1 bash install.sh`; it also reuses the saved server lock, or asks for the server ID once if none is saved yet. If no token is saved at all (fresh host or empty env file), repair mode falls back to the normal interactive token + server ID prompts instead of failing. Review the script before running it, as with any root installer. To install a different branch deliberately: `sudo NOIR_REF=main bash install.sh` (only after that branch contains the code).

## 3. Use it

In the **one configured server** where the bot is present, an **admin** runs:

```text
!deploy 1 1 1 @user
```

The bot asks which OS to install (**Ubuntu 24.04 LTS** or **Debian 12**), then shows a live premium progress meter with the deployment path: validate plan → reserve resources → prepare persistent disk → create root container → boot/health-check → finalize inventory → online. The VPS is owned by the mentioned member; per-user limits count against that member, not the admin. Omit the mention (`!deploy 1 1 1`) to deploy for yourself.

The owner then runs:

```text
!manage
```

The management panel has:

| Button | Behavior |
|---|---|
| **Start** | Start the existing container and refresh live metrics in-place; files are preserved. |
| **Stop** | Stop processes and update the card in-place; the home disk is preserved. |
| **Restart** | Cleanly restart the container in-place without losing `/home/noir` files. |
| **Console** | Opens a 2-option selector: **SSH** (direct TCP root SSH tunnel via `free.pinggy.io` with `ssh -p <port> root@<host>`, password & SFTP/VS Code support) or **SSHX** (instant private web terminal via `sshx.io`). |
| **Tunnel** | Expose any HTTP web server port inside your container as an instant `https://*.pinggy.link` URL via Pinggy.io. |
| **Stats** | Refresh live container uptime, CPU %, RAM MB, and `/home/noir` disk usage in-place. |
| **Backup** | Save a compressed `.tar.gz` snapshot of `/home/noir` on the host. |
| **Restore** | Confirm first, then restore `/home/noir` from your latest saved snapshot. |
| **Reinstall** | Choose the replacement OS (Ubuntu 24.04 / Ubuntu 22.04 / Debian 12), confirm, then **erase all files** and recreate a fresh root container with the same ID/resources/owner. |
| **Refresh** | Reconcile the panel with Docker and live telemetry. |
| **Delete** | Confirm first, then remove the container, its disk, and saved snapshot permanently. |
| **Open in DM** | Send a live personal control console straight to your bot DMs. |

Other commands (work in the locked server **and in bot DMs** for members/admins of that server):

```text
!status                     admin only: open the live Host Node dashboard (CPU, RAM, Disk, Fleet)
!panel                      post an interactive Server Cloud Panel card (or open your DM console)
!manage noir-a1b2c3d4e5f6   open a specific VPS
!delete @user               admin only: list that member's VPSes, pick one, then confirm
!delete noir-a1b2c3d4e5f6   delete by ID (confirmation required)
!delete 1 1 1               delete the unique VPS YOU own with exactly 1 GiB RAM / 1 CPU / 1 GiB disk
!delete                     open the management panel
!help
!ping
```

`!delete @user` never bulk-deletes: the admin selects exactly one VPS from that member's list (paged if more than 25) and must confirm. If several of your VPSes match `!delete 1 1 1`, nothing is deleted; the bot lists IDs so you can choose. In bot DMs, commands are scoped to your locked server and only available to verified members/admins/owners of that server. Use `!delete`, not `! delete`.

### Permissions and limits

- **Single-server lock:** `NOIR_ALLOWED_GUILD_ID` is required. The installer asks for it after the token; the bot refuses commands elsewhere and leaves any other server it is added to.
- **Deploy:** admins only — server administrators plus `NOIR_ADMIN_IDS`. The old `NOIR_DEPLOY_ROLE_ID` knob no longer grants creation rights.
- **Manage/delete:** only the owner, server administrators, and configured bot admins; always scoped to the same guild. `!delete @user` is admin-only.
- Defaults: max **2 VPSes/user**, **20 total**, plans up to **4 GiB RAM / 4 CPU / 20 GiB disk**.
- RAM/CPU reservations are checked across the host before deployment. Where loop mounts work, disk images are fully allocated with `fallocate` and act as hard filesystem quotas for the persistent `/home/noir` disk; a 1 GiB safety reserve is required. On hosts without usable loop devices (common in LXC), NOIR falls back to per-VPS owned directories: the disk argument is still a reserved host budget, but not a hard filesystem quota. The container's writable system layer is ephemeral and is not included in that home-disk quota. `python -m noir doctor` prints the active storage mode.
- Edit `/etc/noir/noir.env` to change limits, admin IDs, or per-OS images, then `sudo systemctl restart noir`. See `.env.example`.

### Inside the terminal

SSHX starts directly at a root shell (`root@noir-…`). You can use system package management inside the container:

```bash
apt update
apt install -y <package>
python3 -m venv ~/mybot/.venv
source ~/mybot/.venv/bin/activate
pip install -U pip
# put your application in ~/mybot, then run it inside tmux:
tmux new -s app
```

`tmux` keeps a process running after you close the browser terminal, until the VPS is stopped/reinstalled/deleted. Start does not automatically relaunch your application. Persistent files live under `/home/noir`; Reinstall/Delete erase that home disk. Root here means root in the isolated container, not root on the bot host.

**SSHX links grant terminal access.** Do not share them. Closing/hiding the Discord message or letting its buttons expire does not revoke a session; use **Close terminal session**, or stop/reinstall/delete the VPS. SSHX is a third-party service and requires outbound network access.

## Operations

```bash
sudo systemctl status noir
sudo journalctl -u noir -f
sudo systemctl restart noir
sudo systemctl stop noir
sudo /opt/noir/.venv/bin/python -m noir doctor --env-file /etc/noir/noir.env
# Recreate existing containers on their recorded OS root image; preserves /home/noir files:
sudo /opt/noir/.venv/bin/python -m noir upgrade --env-file /etc/noir/noir.env
```

- Inventory: `/var/lib/noir/data/noir.sqlite3` (persists across bot restarts).
- Fixed-size home filesystem images: `/var/lib/noir/disks/`.
- Host mountpoints: `/var/lib/noir/mounts/`.
- Token/config: `/etc/noir/noir.env` (root-only).
- Containers use restart policy `unless-stopped`: running VPSes return after a host/Docker restart; VPSes explicitly stopped through the bot stay stopped.
- Bot restarts reconcile inventory with Docker. Interrupted deployments are marked `error`; use Delete or Reinstall to clean/recreate them.

### Troubleshooting

| Symptom | Check |
|---|---|
| Bot process runs but is not online | Message Content Intent enabled? `journalctl -u noir -n 100`; token valid? Outbound HTTPS to Discord? |
| Commands ignored | Bot invited to this server? Can it see/read/send in the channel? Using `!deploy`, not `! deploy`? Correct allowed guild? |
| Bot leaves a server / “restricted to another Discord server” | That server is not the saved `NOIR_ALLOWED_GUILD_ID`. Re-run setup/installer and enter the intended Server ID, or edit `/etc/noir/noir.env` and restart. |
| “Only server administrators can create VPSes” | You must be a server administrator or listed in `NOIR_ADMIN_IDS`. Deploy roles no longer grant creation. |
| “image … is missing” | Re-run the installer so both `noir-vps:local` and `noir-vps:debian-12` are built; check `NOIR_IMAGE_UBUNTU` / `NOIR_IMAGE_DEBIAN`. |
| Docker unavailable | `systemctl status docker`; service user has the `docker` supplementary group; run doctor. |
| Disk helper denied | `/etc/sudoers.d/noir` and root-owned `/usr/local/libexec/noir-volume` installed? Re-run installer. |
| `mount failed: No such file or directory` / loop errors | Usually missing `/dev/loop*` inside LXC. Re-run the installer; it creates loop nodes when allowed, mounts in PID 1's namespace, and otherwise falls back to directory storage. |
| `Permission denied` executing `/opt/noir/.venv/bin/python` | Re-run the installer; it makes the root-owned app/venv traversable by the `noir` service user. |
| Not enough RAM/CPU/disk | Delete unused VPSes or adjust host budgets only if the host actually has capacity. |
| SSHX still shows `noir@…` / `apt` read-only | The container is on the old runtime. Re-run the installer, or run `sudo /opt/noir/.venv/bin/python -m noir upgrade --env-file /etc/noir/noir.env`; home files are preserved. |
| SSHX times out | VPS running? Outbound HTTPS/gRPC to `sshx.io` allowed? Third-party service available? Stop/start and retry. |
| Container missing after manual Docker cleanup | Use Reinstall (wipes data) or Delete. NOIR refuses to control unlabeled containers. |

## Security boundaries

This is a **host-administration tool**, not a sandbox for hostile users. Docker socket/group access is effectively host-root access. Run it on a dedicated host, keep deploy permission restricted to trusted people, and never give untrusted users the Docker socket. VPS containers run as root *inside the container* with Docker's default unprivileged capability set, `no-new-privileges`, no privileged mode, no published ports, no Docker socket/host filesystem mount, and a dedicated bridge with inter-container traffic disabled. They can still make outbound connections and may reach host/LAN endpoints unless you add appropriate host firewall rules. Do not expose this to untrusted tenants without stronger isolation (e.g. separate VMs) and network policy.

The root-owned disk helper accepts only fixed actions, generated IDs, and bounded integer sizes; it does not accept arbitrary paths or shell commands. The bot never receives your Discord token inside a VPS. SSHX URLs are not logged or put in public embeds.

The official SSHX download is a rolling upstream artifact. The installer records its hash in `/opt/noir/runtime/sshx.sha256`. To pin a hash you have reviewed, set `NOIR_SSHX_SHA256` before installing; mismatches are rejected.

## Development / verification

Python 3.10+:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/ruff check noir scripts/fetch_sshx.py scripts/noir-volume runtime/noir-sshx tests
bash -n install.sh
```

Tests use a fake Docker backend and cover admin-only creation, mentioned-owner limits, OS selection on deploy/reinstall/upgrade, SQLite OS migration, ownership, guild isolation, resource reservation, concurrent deploy limits, cleanup failures, persistence, destructive operations, SSHX URL validation/private links, container hardening, token-file permissions, and installer-independent CLI behavior. They do **not** contact Discord or require Docker. CI runs these checks; a real deployment still needs a live Docker host and your bot token.

Project layout:

```text
noir/                 Discord commands, premium UI, inventory, Docker backend, CLI
runtime/              Ubuntu/Debian root images + shared SSHX session helper
scripts/              root disk helper + SSHX downloader
assets/               Discord embed banner
systemd/noir.service  automatic service
tests/                offline unit tests
install.sh            one-command host installer/upgrade
```
