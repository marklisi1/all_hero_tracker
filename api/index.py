import asyncio
import json
import os
from pathlib import Path

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

app = FastAPI()

_ROOT = Path(__file__).parent.parent
HERO_ORDER = [
    line.strip()
    for line in (_ROOT / "all_hero_challenge_order.txt").read_text().splitlines()
    if line.strip()
]
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
CYCLE_LEN = len(HERO_ORDER)

DISCORD_API = "https://discord.com/api/v10"
STATE_KEY = "ahc_state"

LEADERBOARD_STATS = [
    ("kills",   "🗡️ Most Kills",   lambda v: f"{int(v)} kills"),
    ("deaths",  "💀 Most Deaths",  lambda v: f"{int(v)} deaths"),
    ("assists", "🤝 Most Assists", lambda v: f"{int(v)} assists"),
    ("kda",     "⭐ Best KDA",     lambda v: f"{v:.1f} KDA"),
]

def _env(key: str) -> str:
    val = os.environ.get(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val


# --- Upstash helpers ---

async def _redis(client: httpx.AsyncClient, *cmd) -> object:
    resp = await client.post(
        _env("UPSTASH_REDIS_REST_URL"),
        json=list(cmd),
        headers={"Authorization": f"Bearer {_env('UPSTASH_REDIS_REST_TOKEN')}"},
    )
    resp.raise_for_status()
    return resp.json()["result"]


async def load_state(client: httpx.AsyncClient) -> dict:
    raw = await _redis(client, "GET", STATE_KEY)
    if raw is None:
        with open(_ROOT / "state.json") as f:
            return json.load(f)
    return json.loads(raw)


async def save_state(client: httpx.AsyncClient, state: dict) -> None:
    await _redis(client, "SET", STATE_KEY, json.dumps(state))


# --- OpenDota logic ---

async def get_recent_games(client: httpx.AsyncClient, player_id: str) -> list[tuple[str, bool]]:
    resp = await client.get(
        f"https://api.opendota.com/api/players/{player_id}/matches",
        params={"limit": 50},
    )
    resp.raise_for_status()
    games = []
    for m in resp.json():
        hero_name = HERO_MAP.get(m["hero_id"])
        if not hero_name:
            continue
        is_radiant = m["player_slot"] < 128
        won = is_radiant == m["radiant_win"]
        games.append((hero_name, won))
    games.reverse()
    return games


def advance_player(current_idx: int, games: list[tuple[str, bool]]) -> tuple[int, int]:
    for hero_name, won in games:
        if won and hero_name == HERO_ORDER[current_idx % CYCLE_LEN]:
            current_idx += 1
    current_hero = HERO_ORDER[current_idx % CYCLE_LEN]
    attempts = sum(1 for hero_name, _ in games if hero_name == current_hero)
    return current_idx, attempts


def _kda(m: dict) -> float:
    return (m["kills"] + m["assists"]) / max(m["deaths"], 1)


def _stat_val(m: dict, stat: str) -> float | None:
    if stat == "kda":
        return _kda(m) if all(k in m for k in ("kills", "deaths", "assists")) else None
    return m.get(stat)


async def _fetch_matches(client: httpx.AsyncClient, player_id: str, limit: int = 50) -> list[dict]:
    resp = await client.get(
        f"https://api.opendota.com/api/players/{player_id}/matches",
        params={"limit": limit},
    )
    resp.raise_for_status()
    return resp.json()


def _find_best(player_matches: dict, stat: str, n: int) -> tuple | None:
    best_val, best_name, best_hero = None, None, None
    for name, matches in player_matches.items():
        for m in matches[:n]:
            val = _stat_val(m, stat)
            if val is not None and (best_val is None or val > best_val):
                best_val, best_name, best_hero = val, name, HERO_MAP.get(m["hero_id"], "Unknown")
    return (best_name, best_val, best_hero) if best_name else None


def _col_text(player_matches: dict, n: int | None, records: dict) -> str:
    lines = []
    for stat, label, fmt in LEADERBOARD_STATS:
        if n is None:
            rec = records.get(stat)
            entry = f"**{rec['player']}** — {fmt(rec['value'])} as {rec['hero']}" if rec else "—"
        else:
            best = _find_best(player_matches, stat, n)
            entry = f"**{best[0]}** — {fmt(best[1])} as {best[2]}" if best else "—"
        lines.append(f"{label}: {entry}")
    return "\n".join(lines)


# --- Leaderboard command handler ---

async def process_leaderboard(application_id: str, token: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        state = await load_state(client)
        names = [name for name in state if name in PLAYER_IDS]

        results = await asyncio.gather(
            *[_fetch_matches(client, PLAYER_IDS[name]) for name in names],
            return_exceptions=True,
        )
        player_matches = {
            name: (r if not isinstance(r, Exception) else [])
            for name, r in zip(names, results)
        }

        records: dict = state.get("records", {})
        changed = False
        for name, matches in player_matches.items():
            for m in matches:
                for stat, _, _ in LEADERBOARD_STATS:
                    val = _stat_val(m, stat)
                    if val is None:
                        continue
                    cur = records.get(stat)
                    if cur is None or val > cur["value"]:
                        records[stat] = {
                            "player": name,
                            "value": val,
                            "hero": HERO_MAP.get(m["hero_id"], "Unknown"),
                            "match_id": m["match_id"],
                        }
                        changed = True
        if changed:
            state["records"] = records
            await save_state(client, state)

        fields = [
            {"name": "Last 10 Games", "value": _col_text(player_matches, 10, records), "inline": False},
            {"name": "Last 50 Games", "value": _col_text(player_matches, 50, records), "inline": False},
            {"name": "All Time",      "value": _col_text(player_matches, None, records), "inline": False},
        ]
        embed = {"title": "Leaderboard", "color": 0xF1C40F, "fields": fields}

        await client.patch(
            f"{DISCORD_API}/webhooks/{application_id}/{token}/messages/@original",
            json={"embeds": [embed]},
        )


# --- AHC command handler ---

async def process_ahc(application_id: str, token: str) -> None:
    async with httpx.AsyncClient(timeout=10) as client:
        state = await load_state(client)

        names = [name for name in state if name in PLAYER_IDS]
        game_lists = await asyncio.gather(
            *[get_recent_games(client, PLAYER_IDS[name]) for name in names],
            return_exceptions=True,
        )

    updated = False
    lines = []
    for name, result in zip(names, game_lists):
        old_idx = state[name]["hero_index"]
        if isinstance(result, Exception):
            new_idx, attempts = old_idx, 0
        else:
            new_idx, attempts = advance_player(old_idx, result)
        if new_idx != old_idx:
            state[name]["hero_index"] = new_idx
            updated = True
        hero = HERO_ORDER[new_idx % CYCLE_LEN]
        lines.append((name, hero, attempts, new_idx))

    lines.sort(key=lambda x: x[3], reverse=True)
    fields = [
        {
            "name": name,
            "value": f"**{hero}** ({attempts} attempt{'s' if attempts != 1 else ''})",
            "inline": False,
        }
        for name, hero, attempts, _ in lines
    ]
    embed = {"title": "All Hero Challenge Progress", "color": 0x9B59B6, "fields": fields}

    async with httpx.AsyncClient(timeout=10) as client:
        if updated:
            await save_state(client, state)
        await client.patch(
            f"{DISCORD_API}/webhooks/{application_id}/{token}/messages/@original",
            json={"embeds": [embed]},
        )


# --- Discord interaction endpoint ---

def _verify(signature: str, timestamp: str, body: bytes) -> None:
    verify_key = VerifyKey(bytes.fromhex(_env("DISCORD_PUBLIC_KEY")))
    try:
        verify_key.verify(f"{timestamp}{body.decode()}".encode(), bytes.fromhex(signature))
    except BadSignatureError:
        raise HTTPException(status_code=401)


@app.post("/")
async def interactions(request: Request, background_tasks: BackgroundTasks) -> dict:
    sig = request.headers.get("x-signature-ed25519", "")
    ts = request.headers.get("x-signature-timestamp", "")
    body = await request.body()
    _verify(sig, ts, body)

    data = json.loads(body)

    if data["type"] == 1:  # PING
        return {"type": 1}

    if data["type"] == 2:
        name = data["data"]["name"]
        if name == "ahc":
            background_tasks.add_task(process_ahc, data["application_id"], data["token"])
            return {"type": 5}
        if name == "leaderboard":
            background_tasks.add_task(process_leaderboard, data["application_id"], data["token"])
            return {"type": 5}

    raise HTTPException(status_code=400)
