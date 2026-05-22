import asyncio
import json
import os
from pathlib import Path

import discord
from dotenv import load_dotenv

from scraper import get_recent_games

load_dotenv()

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "state.json"
HERO_ORDER_FILE = BASE_DIR / "all_hero_challenge_order.txt"

HERO_ORDER = [line.strip() for line in HERO_ORDER_FILE.read_text().splitlines() if line.strip()]
CYCLE_LEN = len(HERO_ORDER)

PLAYER_IDS = {
    "Mark": "108078679",
    "John": "107323490",
    "Eric": "105570520",
    "Hadi": "58821919",
    "Matt": "109323944",
    "Mike": "76342551",
}

intents = discord.Intents.default()
bot = discord.Bot(intents=intents)


def load_state() -> dict:
    with open(STATE_FILE) as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def advance_player(current_idx: int, games: list[tuple[str, bool]]) -> int:
    for hero_name, won in games:
        if won and hero_name == HERO_ORDER[current_idx % CYCLE_LEN]:
            current_idx += 1
    return current_idx


@bot.slash_command(name="ahc", description="Check All Hero Challenge progress for all players")
async def ahc(ctx: discord.ApplicationContext) -> None:
    await ctx.defer()

    state = load_state()
    lines = []

    tasks = {
        name: asyncio.create_task(get_recent_games(PLAYER_IDS[name]))
        for name in state
        if name in PLAYER_IDS
    }
    results = {name: await task for name, task in tasks.items()}

    updated = False
    for name, games in results.items():
        old_idx = state[name]["hero_index"]
        new_idx = advance_player(old_idx, games)
        if new_idx != old_idx:
            state[name]["hero_index"] = new_idx
            updated = True

        hero = HERO_ORDER[new_idx % CYCLE_LEN]
        position = (new_idx % CYCLE_LEN) + 1
        completed = new_idx
        lines.append((name, hero, position, completed))

    if updated:
        save_state(state)

    lines.sort(key=lambda x: x[3], reverse=True)

    embed = discord.Embed(title="All Hero Challenge Progress", color=0x9B59B6)
    for name, hero, position, completed in lines:
        embed.add_field(
            name=name,
            value=f"**{hero}** (#{position}/{CYCLE_LEN} • {completed} completed)",
            inline=False,
        )

    await ctx.respond(embed=embed)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN not set in environment")
    bot.run(token)
