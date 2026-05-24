"""
Loads manually curated alltime records from records_seed.json into Upstash.

Entries in records_seed.json are normalized to list format. Existing records
are only replaced if the seeded value is strictly higher.

Usage:
    uv run python load_seed.py
    uv run python load_seed.py --dry-run
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
STATE_KEY = "ahc_state"


async def _redis(client: httpx.AsyncClient, *cmd) -> object:
    resp = await client.post(
        os.environ["UPSTASH_REDIS_REST_URL"],
        json=list(cmd),
        headers={"Authorization": f"Bearer {os.environ['UPSTASH_REDIS_REST_TOKEN']}"},
    )
    resp.raise_for_status()
    return resp.json()["result"]


async def main(dry_run: bool) -> None:
    seed = json.loads((_ROOT / "records_seed.json").read_text())
    alltime_seed = seed.get("alltime", {})

    async with httpx.AsyncClient(timeout=10) as client:
        raw = await _redis(client, "GET", STATE_KEY)
        state = json.loads(raw) if raw else json.loads((_ROOT / "state.json").read_text())

        records: dict = state.setdefault("records", {})
        alltime: dict = records.setdefault("alltime", {})
        changed = False

        for stat, raw_entry in alltime_seed.items():
            # Normalize to list
            entries = raw_entry if isinstance(raw_entry, list) else [raw_entry]
            # Skip placeholder entries
            entries = [e for e in entries if e.get("player") and e.get("value")]
            if not entries:
                print(f"  {stat}: skipped (no data)")
                continue

            seed_val = entries[0]["value"]
            existing = alltime.get(stat, [])
            if isinstance(existing, dict):
                existing = [existing]
            existing_val = existing[0]["value"] if existing else None

            if existing_val is not None and existing_val > seed_val:
                print(f"  {stat}: keeping existing — {existing[0]['player']} {existing_val} (seed: {seed_val})")
            elif existing_val == seed_val:
                print(f"  {stat}: no change — already {seed_val}")
            else:
                tag = f"(was {existing[0]['player']} {existing_val})" if existing else "(new)"
                names = " & ".join(f"{e['player']} ({e['hero']})" for e in entries)
                print(f"  {stat}: {names} — {seed_val}  {tag}")
                alltime[stat] = entries
                changed = True

        if not changed:
            print("\nNo changes to write.")
            return

        if dry_run:
            print("\nDry run — nothing written.")
            return

        state["records"] = records
        await _redis(client, "SET", STATE_KEY, json.dumps(state))
        print("\nAlltime records written to Upstash.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))
