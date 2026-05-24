"""
One-off script to seed match_cache in Upstash for the l10/l50 leaderboard.

Fetches full match data for each player's last 50 non-turbo games.
Rate-limited to ~30 req/min (batches of 10, 20s sleep between batches).
Estimated runtime: 5-8 minutes.

Usage:
    uv run python seed_match_cache.py
    uv run python seed_match_cache.py --dry-run
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
STATE_KEY = "ahc_state"
CACHE_SIZE = 50
BATCH_SIZE = 10
BATCH_SLEEP = 20  # seconds — 10 req per 20s = 30 req/min, safely under 60/min free tier


async def _redis(client: httpx.AsyncClient, *cmd) -> object:
    resp = await client.post(
        os.environ["UPSTASH_REDIS_REST_URL"],
        json=list(cmd),
        headers={"Authorization": f"Bearer {os.environ['UPSTASH_REDIS_REST_TOKEN']}"},
    )
    resp.raise_for_status()
    return resp.json()["result"]


def _is_turbo(m: dict) -> bool:
    return m.get("game_mode") == 23


def _extract_player(match: dict, account_id: str) -> dict | None:
    for p in match.get("players", []):
        if str(p.get("account_id")) == account_id:
            return p
    return None


def _extract_cache_entry(pdata: dict) -> dict:
    entry: dict = {"match_id": pdata["match_id"], "hero_id": pdata.get("hero_id")}
    for stat in ("kills", "deaths", "assists", "gold_per_min", "xp_per_min",
                 "hero_damage", "tower_damage", "hero_healing", "obs_placed", "sen_placed"):
        entry[stat] = pdata.get(stat)
    entry["smoke_of_deceit"] = (pdata.get("purchase") or {}).get("smoke_of_deceit")
    return entry


async def main(dry_run: bool) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        raw = await _redis(client, "GET", STATE_KEY)
        state = json.loads(raw) if raw else json.loads((_ROOT / "state.json").read_text())

        # Fetch last 50 matches per player
        print("Fetching match lists for all players...")
        list_results = await asyncio.gather(
            *[
                client.get(
                    f"https://api.opendota.com/api/players/{pid}/matches",
                    params={"limit": 50},
                )
                for pid in PLAYER_IDS.values()
            ],
            return_exceptions=True,
        )
        player_matches: dict[str, list] = {}
        for name, result in zip(PLAYER_IDS.keys(), list_results):
            if isinstance(result, Exception):
                print(f"  {name}: ERROR — {result}")
                player_matches[name] = []
                continue
            result.raise_for_status()
            non_turbo = [m for m in result.json() if not _is_turbo(m)][:CACHE_SIZE]
            player_matches[name] = non_turbo
            print(f"  {name}: {len(non_turbo)} non-turbo matches")

        # Collect unique match IDs across all players
        all_ids: set[int] = set()
        for matches in player_matches.values():
            all_ids.update(m["match_id"] for m in matches)
        unique_ids = sorted(all_ids, reverse=True)

        n_batches = (len(unique_ids) + BATCH_SIZE - 1) // BATCH_SIZE
        est_min = n_batches * BATCH_SLEEP // 60
        print(f"\n{len(unique_ids)} unique match IDs across all players.")
        print(f"Fetching in batches of {BATCH_SIZE} with {BATCH_SLEEP}s sleep (~{30} req/min).")
        print(f"Estimated time: ~{est_min}-{est_min + 2} minutes\n")

        # Fetch full match data in rate-limited batches
        match_by_id: dict[int, dict] = {}
        batches = [unique_ids[i:i + BATCH_SIZE] for i in range(0, len(unique_ids), BATCH_SIZE)]

        for i, batch in enumerate(batches, 1):
            print(f"  Batch {i}/{len(batches)} ({len(batch)} matches)...", end=" ", flush=True)
            batch_results = await asyncio.gather(
                *[client.get(f"https://api.opendota.com/api/matches/{mid}") for mid in batch],
                return_exceptions=True,
            )
            ok = 0
            for mid, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    print(f"\n    WARN: {mid} failed — {result}")
                    continue
                if result.status_code != 200:
                    print(f"\n    WARN: {mid} returned HTTP {result.status_code}")
                    continue
                match_by_id[mid] = result.json()
                ok += 1
            print(f"{ok}/{len(batch)} OK")

            if i < len(batches):
                print(f"  Sleeping {BATCH_SLEEP}s...", flush=True)
                await asyncio.sleep(BATCH_SLEEP)

        print(f"\nFetched {len(match_by_id)}/{len(unique_ids)} matches successfully.")

        # Build cache per player (newest first, capped at CACHE_SIZE)
        print("\nBuilding cache entries...")
        match_cache: dict[str, list] = {}
        for name, matches in player_matches.items():
            entries = []
            for m in matches:
                mid = m["match_id"]
                full = match_by_id.get(mid)
                if full is None:
                    continue
                pdata = _extract_player(full, PLAYER_IDS[name])
                if pdata is None:
                    continue
                entries.append(_extract_cache_entry(pdata))
            entries.sort(key=lambda e: e["match_id"], reverse=True)
            match_cache[name] = entries
            print(f"  {name}: {len(entries)} cache entries")

        if dry_run:
            print("\nDry run — nothing written.")
            return

        state["match_cache"] = match_cache
        await _redis(client, "SET", STATE_KEY, json.dumps(state))
        print("\nMatch cache written to Upstash.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
