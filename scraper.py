import json
from pathlib import Path

import httpx

OPENDOTA_BASE = "https://api.opendota.com/api"

_HERO_MAP: dict[int, str] = {
    int(k): v
    for k, v in json.loads((Path(__file__).parent / "hero_ids.json").read_text()).items()
}


async def get_recent_games(player_id: str) -> list[tuple[str, bool]]:
    """
    Returns a list of (hero_name, won) tuples for the last 50 matches,
    ordered chronologically oldest-first.
    """
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{OPENDOTA_BASE}/players/{player_id}/matches",
            params={"limit": 50},
        )
        resp.raise_for_status()
        matches = resp.json()

    games = []
    for m in matches:
        hero_name = _HERO_MAP.get(m["hero_id"])
        if not hero_name:
            continue
        is_radiant = m["player_slot"] < 128
        won = is_radiant == m["radiant_win"]
        games.append((hero_name, won))

    games.reverse()
    return games
