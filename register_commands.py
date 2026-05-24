"""
One-time script to register the /ahc slash command with Discord.
Guild commands register instantly; global commands take up to 1 hour.

Usage:
    uv run python register_commands.py --guild <GUILD_ID>   # instant, for testing
    uv run python register_commands.py                       # global
"""
import argparse
import os

import httpx
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
APPLICATION_ID = os.environ["DISCORD_APPLICATION_ID"]

COMMANDS = [
    {
        "name": "ahc",
        "description": "Check All Hero Challenge progress for all players",
        "type": 1,
    },
    {
        "name": "l10",
        "description": "Leaderboard — best stats across each player's last 10 non-turbo games",
        "type": 1,
    },
    {
        "name": "l50",
        "description": "Leaderboard — best stats across each player's last 50 non-turbo games",
        "type": 1,
    },
    {
        "name": "alltime",
        "description": "Leaderboard — all-time best stats (non-turbo)",
        "type": 1,
    },
]

parser = argparse.ArgumentParser()
parser.add_argument("--guild", help="Guild ID for instant registration (omit for global)")
args = parser.parse_args()

if args.guild:
    url = f"https://discord.com/api/v10/applications/{APPLICATION_ID}/guilds/{args.guild}/commands"
else:
    url = f"https://discord.com/api/v10/applications/{APPLICATION_ID}/commands"

with httpx.Client() as client:
    resp = client.put(url, json=COMMANDS, headers={"Authorization": f"Bot {TOKEN}"})
    print(resp.status_code, resp.json())
