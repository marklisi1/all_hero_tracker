"""
One-off script to seed all-time leaderboard records in Upstash.

For each player × stat, fetches their all-time best game using OpenDota's
sort param, then writes the overall winners into state["records"].

Usage:
    uv run python seed_records.py           # preview + write
    uv run python seed_records.py --dry-run # preview only
"""
import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv()

_ROOT = Path(__file__).parent
HERO_MAP: dict[int, str] = {
    int(k): v
    for k, v in json.loads((_ROOT / "hero_ids.json").read_text()).items()
}
PLAYER_IDS = {
    "Mark": "108078679",
    "John": "107323490",
    "Eric": "105570520",
    "Hadi": "58821919",
    "Matt": "109323944",
    "Mike": "76342551",
}
STATS = ["kills", "deaths", "assists", "kda", "gold_per_min", "xp_per_min", "hero_damage", "tower_damage", "hero_healing"]
STATE_KEY = "ahc_state"


def _kda(m: dict) -> float:
    return (m["kills"] + m["assists"]) / max(m["deaths"], 1)


def _stat_val(m: dict, stat: str) -> float:
    return _kda(m) if stat == "kda" else m[stat]


async def _redis(client: httpx.AsyncClient, *cmd) -> object:
    resp = await client.post(
        os.environ["UPSTASH_REDIS_REST_URL"],
        json=list(cmd),
        headers={"Authorization": f"Bearer {os.environ['UPSTASH_REDIS_REST_TOKEN']}"},
    )
    resp.raise_for_status()
    return resp.json()["result"]


async def fetch_best(
    client: httpx.AsyncClient, name: str, player_id: str, stat: str
) -> dict | None:
    resp = await client.get(
        f"https://api.opendota.com/api/players/{player_id}/matches",
        params={"limit": 1, "sort": stat},
    )
    resp.raise_for_status()
    matches = resp.json()
    if not matches:
        return None
    m = matches[0]
    return {
        "player": name,
        "value": _stat_val(m, stat),
        "hero": HERO_MAP.get(m["hero_id"], "Unknown"),
        "match_id": m["match_id"],
    }


async def main(dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        raw = await _redis(client, "GET", STATE_KEY)
        state = json.loads(raw) if raw else json.loads((_ROOT / "state.json").read_text())

        tasks = {
            (name, stat): fetch_best(client, name, player_id, stat)
            for name, player_id in PLAYER_IDS.items()
            for stat in STATS
        }
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        by_key = dict(zip(tasks.keys(), results))

        records: dict = state.get("records", {})
        new_records: dict = {}

        for stat in STATS:
            candidates = [
                by_key[(name, stat)]
                for name in PLAYER_IDS
                if isinstance(by_key[(name, stat)], dict)
            ]
            if not candidates:
                print(f"  {stat}: no data")
                continue
            best_val = max(r["value"] for r in candidates)
            winners = [r for r in candidates if r["value"] == best_val]

            existing = records.get(stat, [])
            if isinstance(existing, dict):
                existing = [existing]
            existing_val = existing[0]["value"] if existing else None

            if existing_val is not None and existing_val > best_val:
                print(f"  {stat:8s}: keeping existing — {existing[0]['player']} {existing_val:.2f}")
                new_records[stat] = existing
            else:
                names = " & ".join(f"{r['player']} ({r['hero']})" for r in winners)
                tag = "(new)" if not existing else f"(was {existing[0]['player']} {existing_val:.2f})"
                print(f"  {stat:8s}: {names} — {best_val:.2f}  {tag}")
                new_records[stat] = winners

        if dry_run:
            print("\nDry run — nothing written.")
            return

        state["records"] = new_records
        await _redis(client, "SET", STATE_KEY, json.dumps(state))
        print("\nRecords written to Upstash.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
