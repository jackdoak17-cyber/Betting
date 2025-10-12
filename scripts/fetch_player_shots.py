#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch player shot stats for predicted XI players in all leagues, then write:
  - data/player_shots/by_league/{league_id}.json
  - data/player_shots/combined.json
  - data/player_shots/summary.txt
  - data/player_shots/summary_verbose.txt

Assumptions:
- Uses predicted XI from data/predicted_xi/by_league/*.json
- Fetches last up to 10 league fixtures per player via player/latest
- Extracts total shots (type_id for SHOTS_TOTAL) from lineup details
- Only same league and current season (fetched via league/currentSeason)

Env:
  SPORTMONKS_TOKEN
"""

import os
import json
import time
import datetime as dt
from typing import Dict, List, Optional

import requests

# ---------------- Config ----------------
API_BASE = "https://api.sportmonks.com/v3/football"
CORE_BASE = "https://api.sportmonks.com/v3/core"  # Separate for core endpoints
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN is not set.")

# Light throttling / retries
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18      # min spacing between GETs
BATCH_SIZE = 3               # players per tiny batch (smooth usage)
SLEEP_BETWEEN_BATCHES = 1.2  # pause between batches

# Limits
MAX_GAMES = 10

# Leagues from common.py
LEAGUE_IDS = [8, 9, 384, 387, 82, 301, 564, 567, 600, 72, 271]
LEAGUE_NAMES = {
    8: "Premier League",
    9: "Championship",
    384: "Serie A",
    387: "Serie B",
    82: "Bundesliga",
    301: "Ligue 1",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
    72: "Eredivisie",
    271: "Superliga (Denmark)",
}

# ---------------- HTTP helper with memo ----------------
_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def api_get(path: str, params: Optional[dict] = None, base: str = API_BASE) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{base}/{path.lstrip('/')}"
    k = _key(url, params)
    if k in _MEMO:
        return _MEMO[k]

    last_exc = None
    for i in range(1, RETRIES + 1):
        _pace()
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                sleep = min(60, (BACKOFF ** i) * 2.0)
                print(f"[429] {path} — sleeping {sleep:.1f}s")
                time.sleep(sleep)
                continue
            r.raise_for_status()
            j = r.json()
            _MEMO[k] = j
            return j
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ---------------- Utilities ----------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def _load_json(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return None

# ---------------- Load predicted XI from repo ----------------
def load_all_predicted_xi() -> Dict[int, dict]:
    base = "data/predicted_xi/by_league"
    leagues: Dict[int, dict] = {}
    if os.path.isdir(base):
        for name in os.listdir(base):
            if name.endswith(".json"):
                try:
                    lid = int(name[:-5])
                    blob = _load_json(os.path.join(base, name))
                    if blob:
                        leagues[lid] = blob
                except ValueError:
                    continue
    return leagues

# ---------------- Get shots type ID ----------------
def get_shots_type_id() -> int:
    j = api_get("types", {"filters": "developer_name:SHOTS_TOTAL"}, base=CORE_BASE)
    data = j.get("data", [])
    for t in data:
        if t.get("developer_name") == "SHOTS_TOTAL":
            return int(t["id"])
    raise ValueError("SHOTS_TOTAL type not found")

# ---------------- Main ----------------
def main():
    predicted = load_all_predicted_xi()
    if not predicted:
        print("No predicted XI found. Did the predict workflow run?")
        return

    out_root = "data/player_shots"
    by_league_root = os.path.join(out_root, "by_league")
    ensure_dir(by_league_root)

    processed = 0
    by_league_counts: Dict[int, int] = {}
    league_payloads: Dict[int, List[dict]] = {}

    # Get shots type ID
    shots_type_id = get_shots_type_id()

    # team-level caches
    season_cache: Dict[int, int] = {}  # lid -> season_id

    # player-level
    for lid, blob in predicted.items():
        if lid not in LEAGUE_IDS:
            continue

        # Get current season_id
        if lid not in season_cache:
            j = api_get(f"leagues/{lid}", {"include": "currentSeason"})
            cs = j.get("data", {}).get("currentseason") or j.get("data", {}).get("currentSeason")
            season_cache[lid] = int(cs["id"]) if cs else None
        season_id = season_cache[lid]
        if not season_id:
            print(f"[WARN] No current season for league {lid}")
            continue

        fixtures = blob.get("fixtures", [])
        league_items: List[dict] = []

        for idx, fx in enumerate(fixtures, 1):
            if (idx - 1) % BATCH_SIZE == 0:
                print(f"\n-- Batch {((idx - 1)//BATCH_SIZE) + 1} starting (fixture {idx}/{len(fixtures)}) --")

            fid = int(fx["fixture_id"])
            start_at = fx.get("starting_at") or ""
            home = fx["home"]
            away = fx["away"]
            hid, aid = int(home["team_id"]), int(away["team_id"])
            hname, aname = home.get("name", "").strip(), away.get("name", "").strip()

            def fetch_shots(player: dict) -> List[int]:
                pid = int(player["player_id"])
                j = api_get(f"players/{pid}", {
                    "include": "last.lineups.details;last.league;last.season;last.state"
                })
                last = j.get("data", {}).get("last") or []
                shots_list: List[int] = []
                for lf in sorted(last, key=lambda x: x.get("starting_at_timestamp") or 0, reverse=True):
                    if len(shots_list) >= MAX_GAMES:
                        break
                    if (lf.get("league_id") != lid or
                        lf.get("season_id") != season_id or
                        lf.get("state_id") != 5):  # finished
                        continue
                    lineups = lf.get("lineups") or []
                    pl = next((l for l in lineups if l.get("player_id") == pid), None)
                    if not pl:
                        continue
                    details = pl.get("details") or []
                    sd = next((d for d in details if d.get("type_id") == shots_type_id), None)
                    shots = int(sd["data"]["value"]) if sd and "value" in sd["data"] else 0
                    shots_list.append(shots)
                return shots_list[::-1]  # oldest first

            def pack_team(t: dict) -> dict:
                players = []
                for p in t["predicted_xi"]:
                    shots = fetch_shots(p)
                    players.append({
                        "player_id": p["player_id"],
                        "name": p["name"],
                        "role": p.get("role"),
                        "shots": shots
                    })
                return {
                    "team_id": t["team_id"],
                    "name": t["name"],
                    "players": players
                }

            item = {
                "fixture_id": fid,
                "starting_at": start_at,
                "home": pack_team(home),
                "away": pack_team(away),
            }
            league_items.append(item)

            processed += 1
            by_league_counts[lid] = by_league_counts.get(lid, 0) + 1

            # small pause each batch
            if idx % BATCH_SIZE == 0:
                time.sleep(SLEEP_BETWEEN_BATCHES)

        # Write PER-LEAGUE JSON
        now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
        payload = {
            "utc_time": now_iso,
            "league_id": lid,
            "league_name": LEAGUE_NAMES.get(lid, str(lid)),
            "season_id": season_id,
            "fixtures": sorted(league_items, key=lambda r: (r.get("starting_at") or "", r["fixture_id"])),
        }
        with open(os.path.join(by_league_root, f"{lid}.json"), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        league_payloads[lid] = payload["fixtures"]  # Store fixtures for combined

    # Optional combined JSON
    combined_fixtures: List[dict] = []
    for lid in sorted(league_payloads):
        combined_fixtures.extend(league_payloads[lid])
    ensure_dir(out_root)
    with open(os.path.join(out_root, "combined.json"), "w", encoding="utf-8") as f:
        json.dump({
            "utc_time": now_iso,
            "processed": processed,
            "by_league": by_league_counts,
            "fixtures": combined_fixtures,
        }, f, ensure_ascii=False)

    # summaries
    with open(os.path.join(out_root, "summary.txt"), "w", encoding="utf-8") as f:
        f.write(f"Time (UTC): {now_iso}\n")
        f.write(f"Fixtures   : {processed}\n\n")
        f.write("Per league counts:\n")
        for lid in sorted(by_league_counts):
            f.write(f"  - {lid} ({LEAGUE_NAMES.get(lid, lid)}): {by_league_counts[lid]}\n")

    # verbose
    lines: List[str] = []
    lines.append(f"Time (UTC): {now_iso}")
    lines.append(f"Fixtures   : {processed}")
    lines.append("")
    for lid in sorted(league_payloads):
        lname = LEAGUE_NAMES.get(lid, str(lid))
        lines.append(f"===== {lname} (LID {lid}) =====")
        for r in league_payloads[lid]:
            dt_str = (r.get("starting_at") or "").replace("T", " ").replace("Z", "")
            lines.append(f"{dt_str}  —  {r['home']['name']} vs {r['away']['name']}  (FID {r['fixture_id']})")

            def shots_line(team: dict) -> str:
                parts = []
                for p in team["players"]:
                    nm = p["name"]
                    role = p.get("role") or "?"
                    shots_str = ",".join(map(str, p["shots"])) if p["shots"] else "(no data)"
                    parts.append(f"{nm} [{role}] = {shots_str}")
                return "\n    ".join(parts) if parts else "(no players)"

            lines.append(f"  {r['home']['name']} players shots:\n    {shots_line(r['home'])}")
            lines.append(f"  {r['away']['name']} players shots:\n    {shots_line(r['away'])}")
            lines.append("")
    with open(os.path.join(out_root, "summary_verbose.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")

    print("\nDone.")
    print(f"Processed fixtures: {processed}")
    for lid in sorted(by_league_counts):
        print(f"  - {LEAGUE_NAMES.get(lid, lid)}: {by_league_counts[lid]}")
    print("Wrote:")
    print("  • data/player_shots/by_league/<league_id>.json")
    print("  • data/player_shots/combined.json")
    print("  • data/player_shots/summary.txt")
    print("  • data/player_shots/summary_verbose.txt")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
