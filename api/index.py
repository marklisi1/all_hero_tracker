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

BASIC_STATS = [
    ("kills",   "🗡️ Most Kills",   lambda v: f"{int(v)} kills"),
    ("deaths",  "💀 Most Deaths",  lambda v: f"{int(v)} deaths"),
    ("assists", "🤝 Most Assists", lambda v: f"{int(v)} assists"),
    ("kda",     "⭐ Best KDA",     lambda v: f"{v:.1f} KDA"),
]

SORT_STATS = [
    ("gold_per_min",    "💰 Best GPM",       lambda v: f"{int(v):,} GPM"),
    ("xp_per_min",      "✨ Best XPM",       lambda v: f"{int(v):,} XPM"),
    ("hero_damage",     "⚔️ Hero Damage",    lambda v: f"{int(v):,} dmg"),
    ("tower_damage",    "🏰 Tower Damage",   lambda v: f"{int(v):,} dmg"),
    ("hero_healing",    "💚 Hero Healing",   lambda v: f"{int(v):,} healed"),
    ("obs_placed",      "👁️ Obs Placed",     lambda v: f"{int(v)} obs"),
    ("sen_placed",      "🔵 Sentries",       lambda v: f"{int(v)} sentries"),
    ("smoke_of_deceit", "💨 Smokes",         lambda v: f"{int(v)} smokes"),
]

ALL_STATS = BASIC_STATS + SORT_STATS

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
    if stat == "smoke_of_deceit":
        val = (m.get("purchase") or {}).get("smoke_of_deceit")
        return float(val) if val else None
    return m.get(stat)


async def _fetch_matches(client: httpx.AsyncClient, player_id: str, limit: int = 50) -> list[dict]:
    resp = await client.get(
        f"https://api.opendota.com/api/players/{player_id}/matches",
        params={"limit": limit},
    )
    resp.raise_for_status()
    return resp.json()


async def _fetch_full_match(client: httpx.AsyncClient, match_id: int) -> dict:
    resp = await client.get(f"https://api.opendota.com/api/matches/{match_id}")
    resp.raise_for_status()
    return resp.json()


def _extract_player(match: dict, account_id: str) -> dict | None:
    for p in match.get("players", []):
        if str(p.get("account_id")) == account_id:
            return p
    return None


def _apply_candidate(records: dict, stat: str, candidate: dict) -> bool:
    """Insert candidate into records[stat] if it ties or beats the current best. Returns True if changed."""
    cur = records.get(stat, [])
    if isinstance(cur, dict):
        cur = [cur]
    best_val = cur[0]["value"] if cur else None
    if best_val is None or candidate["value"] > best_val:
        records[stat] = [candidate]
        return True
    if candidate["value"] == best_val and not any(r["match_id"] == candidate["match_id"] for r in cur):
        records[stat] = cur + [candidate]
        return True
    return False


def _is_turbo(m: dict) -> bool:
    return m.get("game_mode") == 23


def _find_best(ranked_matches: dict, stat: str, n: int) -> list[dict]:
    """Returns all record dicts sharing the best value across the first n matches per player."""
    best_val: float | None = None
    bests: list[dict] = []
    for name, matches in ranked_matches.items():
        for m in matches[:n]:
            val = _stat_val(m, stat)
            if val is None:
                continue
            entry = {"player": name, "value": val,
                     "hero": HERO_MAP.get(m["hero_id"], "Unknown"), "match_id": m["match_id"]}
            if best_val is None or val > best_val:
                best_val = val
                bests = [entry]
            elif val == best_val:
                if not any(r["match_id"] == m["match_id"] for r in bests):
                    bests.append(entry)
    return bests


def _format_entries(entries: list[dict], fmt) -> str:
    val_str = fmt(entries[0]["value"])
    if len(entries) == 1:
        return f"**{entries[0]['player']}** — {val_str} as {entries[0]['hero']}"
    parts = [f"**{e['player']}** ({e['hero']})" for e in entries]
    return " & ".join(parts) + f" — {val_str}"


def _col_text(ranked_matches: dict, timeframe: str, records: dict) -> str:
    n = {"last10": 10, "last50": 50, "alltime": None}[timeframe]
    tf_records = records.get(timeframe, {})
    lines = []
    for stat, label, fmt in ALL_STATS:
        is_basic = any(stat == s for s, _, _ in BASIC_STATS)
        if n is not None and is_basic:
            # Always computed fresh from the ranked match list
            bests = _find_best(ranked_matches, stat, n)
            entry = _format_entries(bests, fmt) if bests else "—"
        else:
            recs = tf_records.get(stat, [])
            if isinstance(recs, dict):
                recs = [recs]
            entry = _format_entries(recs, fmt) if recs else "—"
        lines.append(f"{label}: {entry}")
    return "\n".join(lines)


# --- Leaderboard command handler ---

async def process_leaderboard(application_id: str, token: str, timeframe: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        state = await load_state(client)
        names = [name for name in state if name in PLAYER_IDS]
        last_seen: dict = state.get("last_seen", {})

        # Fetch recent match list for all players (all game modes — needed for last_seen tracking)
        list_results = await asyncio.gather(
            *[_fetch_matches(client, PLAYER_IDS[name]) for name in names],
            return_exceptions=True,
        )
        player_matches = {
            name: (r if not isinstance(r, Exception) else [])
            for name, r in zip(names, list_results)
        }

        # Non-turbo view used for display and basic-stat record updates
        ranked_matches = {
            name: [m for m in matches if not _is_turbo(m)]
            for name, matches in player_matches.items()
        }

        # New non-turbo match IDs per player since last_seen (turbo advances last_seen but isn't fetched)
        new_per_player: dict[str, list[int]] = {
            name: [
                m["match_id"] for m in matches
                if m["match_id"] > last_seen[name] and not _is_turbo(m)
            ]
            for name, matches in player_matches.items()
            if last_seen.get(name) is not None
        }

        # Deduplicate across players — friends often share the same matches
        unique_new_ids = list({mid for ids in new_per_player.values() for mid in ids})

        full_results = await asyncio.gather(
            *[_fetch_full_match(client, mid) for mid in unique_new_ids],
            return_exceptions=True,
        )
        match_by_id: dict[int, dict] = {
            mid: result
            for mid, result in zip(unique_new_ids, full_results)
            if not isinstance(result, Exception)
        }

        records: dict = state.get("records", {})
        changed = False

        # Update alltime BASIC_STATS from ranked match list (free — already fetched)
        alltime = records.setdefault("alltime", {})
        for name, matches in ranked_matches.items():
            for m in matches:
                for stat, _, _ in BASIC_STATS:
                    val = _stat_val(m, stat)
                    if val is None:
                        continue
                    if _apply_candidate(alltime, stat, {"player": name, "value": val,
                                                         "hero": HERO_MAP.get(m["hero_id"], "Unknown"),
                                                         "match_id": m["match_id"]}):
                        changed = True

        # Update ALL_STATS across all timeframes from full match data (new non-turbo games only)
        for name, new_ids in new_per_player.items():
            for mid in new_ids:
                full = match_by_id.get(mid)
                if full is None:
                    continue
                pdata = _extract_player(full, PLAYER_IDS[name])
                if pdata is None:
                    continue
                hero = HERO_MAP.get(pdata.get("hero_id"), "Unknown")
                for tf in ("last10", "last50", "alltime"):
                    tf_records = records.setdefault(tf, {})
                    for stat, _, _ in ALL_STATS:
                        val = _stat_val(pdata, stat)
                        if val is None:
                            continue
                        if _apply_candidate(tf_records, stat, {
                            "player": name, "value": val, "hero": hero, "match_id": mid,
                        }):
                            changed = True

        # Advance last_seen using all matches including turbo
        new_last_seen = {**last_seen}
        for name, matches in player_matches.items():
            if matches:
                new_last_seen[name] = matches[0]["match_id"]
        if new_last_seen != last_seen:
            changed = True

        if changed:
            state["records"] = records
            state["last_seen"] = new_last_seen
            await save_state(client, state)

        title_map = {"last10": "Last 10 Games", "last50": "Last 50 Games", "alltime": "All Time"}
        embed = {
            "title": f"Leaderboard — {title_map[timeframe]}",
            "description": _col_text(ranked_matches, timeframe, records),
            "color": 0xF1C40F,
        }

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
        if name in ("l10", "l50", "alltime"):
            tf = {"l10": "last10", "l50": "last50", "alltime": "alltime"}[name]
            background_tasks.add_task(process_leaderboard, data["application_id"], data["token"], tf)
            return {"type": 5}

    raise HTTPException(status_code=400)
