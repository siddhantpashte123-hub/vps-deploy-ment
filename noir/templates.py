from __future__ import annotations

from dataclasses import dataclass

from .errors import UserError


@dataclass(frozen=True)
class StarterTemplate:
    id: str
    label: str
    summary: str
    directory: str
    install_script: str


TEMPLATES: tuple[StarterTemplate, ...] = (
    StarterTemplate(
        id="python-bot",
        label="Discord.py Bot Starter",
        summary="Python venv + discord.py skeleton in /home/noir/python-bot",
        directory="/home/noir/python-bot",
        install_script=r"""
set -e
mkdir -p /home/noir/python-bot
cat > /home/noir/python-bot/bot.py <<'EOF'
import os
import discord
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"Online as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN", "PASTE_TOKEN_HERE")
    bot.run(token)
EOF
cat > /home/noir/python-bot/README.txt <<'EOF'
Run inside tmux:
  cd /home/noir/python-bot
  python3 -m venv .venv && .venv/bin/pip install discord.py
  DISCORD_TOKEN=your_token .venv/bin/python bot.py
EOF
echo "Scaffolded /home/noir/python-bot (bot.py + README.txt)"
""",
    ),
    StarterTemplate(
        id="node-bot",
        label="Node.js Discord Bot Starter",
        summary="Node.js project + index.js in /home/noir/node-bot",
        directory="/home/noir/node-bot",
        install_script=r"""
set -e
mkdir -p /home/noir/node-bot
cat > /home/noir/node-bot/package.json <<'EOF'
{
  "name": "noir-node-bot",
  "version": "1.0.0",
  "main": "index.js",
  "scripts": { "start": "node index.js" },
  "dependencies": { "discord.js": "^14.16.0" }
}
EOF
cat > /home/noir/node-bot/index.js <<'EOF'
const { Client, GatewayIntentBits } = require("discord.js");
const client = new Client({ intents: [GatewayIntentBits.Guilds] });
client.once("ready", () => console.log("Logged in as", client.user.tag));
client.login(process.env.DISCORD_TOKEN || "PASTE_TOKEN_HERE");
EOF
echo "Scaffolded /home/noir/node-bot (package.json + index.js — run: npm install && npm start)"
""",
    ),
    StarterTemplate(
        id="fastapi-web",
        label="FastAPI Web API Starter",
        summary="FastAPI HTTP server on port 8000 in /home/noir/fastapi-app",
        directory="/home/noir/fastapi-app",
        install_script=r"""
set -e
mkdir -p /home/noir/fastapi-app
cat > /home/noir/fastapi-app/main.py <<'EOF'
from fastapi import FastAPI

app = FastAPI(title="NOIR FastAPI Service")

@app.get("/")
def read_root():
    return {"status": "ok", "service": "NOIR VPS"}
EOF
cat > /home/noir/fastapi-app/README.txt <<'EOF'
Start on port 8000 (then forward port 8000 with !port add <vps_id> 8000):
  cd /home/noir/fastapi-app
  python3 -m venv .venv && .venv/bin/pip install fastapi uvicorn
  .venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
EOF
echo "Scaffolded /home/noir/fastapi-app (main.py + README.txt)"
""",
    ),
    StarterTemplate(
        id="express-web",
        label="Express.js Web Server",
        summary="Node Express HTTP server on port 3000 in /home/noir/express-app",
        directory="/home/noir/express-app",
        install_script=r"""
set -e
mkdir -p /home/noir/express-app
cat > /home/noir/express-app/package.json <<'EOF'
{
  "name": "noir-express-app",
  "version": "1.0.0",
  "main": "server.js",
  "scripts": { "start": "node server.js" },
  "dependencies": { "express": "^4.21.0" }
}
EOF
cat > /home/noir/express-app/server.js <<'EOF'
const express = require("express");
const app = express();
const port = process.env.PORT || 3000;
app.get("/", (_req, res) => res.json({ status: "online", poweredBy: "NOIR Cloud" }));
app.listen(port, "0.0.0.0", () => console.log(`Listening on 0.0.0.0:${port}`));
EOF
echo "Scaffolded /home/noir/express-app (forward port 3000 with !port add <vps_id> 3000)"
""",
    ),
)

_BY_ID = {t.id: t for t in TEMPLATES}


def get_template(template_id: str) -> StarterTemplate:
    cleaned = template_id.strip().lower()
    if cleaned not in _BY_ID:
        opts = ", ".join(t.id for t in TEMPLATES)
        raise UserError(f"Unknown template `{template_id}`. Choose one of: {opts}.")
    return _BY_ID[cleaned]
