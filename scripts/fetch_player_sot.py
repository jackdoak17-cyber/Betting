#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Build shots-on-target (SOT) time series for players appearing in our predicted XIs.

Rules:
- League matches only (restricted to the leagues we track)
- Current season only
- For each player: take the LAST 10 league appearances (latest first)
- Only include an appearance if the player played >= 45 minutes
- Output JSON per league + combined JSON
- Also write plain-text summaries, including a team-by-team summary
- Drive the player list from data/predicted_xi/by_league/*.json

Output directory:
  data/player_shots_on_target/
    - by_league/{league_id}.json
    - combined.json
    - summary.txt
    - summary_by_team.txt

ENV:
  SPORTMONKS_TOKEN  (required)
"""

import os
import sys
import time
import json
import glob
import datetime as dt
from typing import Dict, List, Tuple, Optional

import requests

# ---------------- Config ----------------

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not API_TOKEN:
    print("ERROR: SPORTMONKS_TOKEN is not set.", file=sys.stderr)
    sys.exit(1)

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18   # gentle pacing across calls

# Leagues we care about (must match your repo)
LEAGUE_NAMES: Dict[int, str] = {
    8: "Premier League",
    9: "Championship",
    82: "Bundesliga",
    301: "Ligue 1",
    384: "Serie A",
    387: "Serie B",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}
LEAGUE_IDS = sorted(LEAGUE_NAMES.keys())

# Limit
N_LAST = 10
MIN_MINUTES = 45

# Batch pacing
BATCH_SIZE = 12              # players per small batch
SLEEP_BETWEEN_BATCHES = 1.0  # seconds

# ---------------- HTTP helpers ----------------

_MEMO: Dict[str, dict] = {}
_last_call_ts = 0.0

def _pace():
    global _last_call_ts
    now = time.time()
    if now - _last_call_ts < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call_ts))
    _last_call_ts = time.time()

def _key(url: str, params: dict) -> str:
    return url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """GET with retries, backoff, light memo, and 429 handling."""
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
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

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()

def position_bucket_from_id(position_id: Optional[int]) -> Optional[str]:
    # Broad fallback if we don't have LB/RB/etc.
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0)

def pick_role_tag(player_obj: dict) -> str:
    # Prefer specific "role" from predicted_xi if present, else "position_label", else bucket
    role = (player_obj.get("role") or "").strip()
    if role:
        return role
    pos_label = (player_obj.get("position_label") or "").strip()
    if pos_label:
        return pos_label
    bucket = position_bucket_from_id(player_obj.get("position_id"))
    return bucket or "?"

# ---------------- Load predicted XI players ----------------

def load_predicted_players() -> Dict[int, Dict[int, Dict[int, dict]]]:
    """
    Returns: nested dict
      by_league[league_id][team_id][player_id] = {
          "name": str,
          "position_tag": "LB/RB/CB/DM/AM/LW/RW/ST/GK/DEF/MID/FWD/?"
      }
    """
    base = "data/predicted_xi/by_league"
    by_league: Dict[int, Dict[int, Dict[int, dict]]] = {}

    paths = sorted(glob.glob(os.path.join(base, "*.json")))
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8") as f:
                j = json.load(f)
        except Exception:
            continue

        lid = int(j.get("league_id") or 0)
        if lid not in LEAGUE_IDS:
            continue

        fixtures = j.get("fixtures") or []
        for fx in fixtures:
            home = fx.get("home") or {}
            away = fx.get("away") or {}
            for side in (home, away):
                tid = int(side.get("team_id") or 0)
                if not tid:
                    continue
                by_league.setdefault(lid, {}).setdefault(tid, {})
                players = side.get("predicted_xi") or []
                for p in players:
                    pid = int(p.get("player_id") or 0)
                    if not pid:
                        continue
                    name = (p.get("name") or "").strip() or f"Player {pid}"
                    tag = pick_role_tag(p)
                    by_league[lid][tid][pid] = {
                        "name": name,
                        "position_tag": tag,
                    }
    return by_league

# ---------------- Season helpers ----------------

def current_season_meta(league_id: int) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Try to fetch current season id and date bounds for a league.
    Returns (season_id, start_date, end_date) as strings YYYY-MM-DD if known.
    """
    season_id = None
    start_date = None
    end_date = None

    # Primary attempt
    try:
        j = api_get(f"leagues/{league_id}", {"include": "currentSeason"})
        data = j.get("data") or {}
        cur = data.get("currentSeason") or data.get("current_season") or data.get("season") or {}
        season_id = cur.get("id") or cur.get("season_id") or data.get("current_season_id")
        start_date = (cur.get("starting_at") or cur.get("start_date") or "")[:10] or None
        end_date = (cur.get("ending_at") or cur.get("end_date") or "")[:10] or None
    except Exception:
        pass

    # Fallback: try seasons list
    if not season_id:
        try:
            j = api_get(f"seasons", {"filters": f"league_id:{league_id}", "order": "desc"})
            arr = j.get("data") or []
            if arr:
                cur = arr[0]
                season_id = cur.get("id")
                start_date = (cur.get("starting_at") or "")[:10] or start_date
                end_date = (cur.get("ending_at") or "")[:10] or end_date
        except Exception:
            pass

    try:
        season_id = int(season_id) if season_id else None
    except Exception:
        season_id = None

    return season_id, start_date, end_date

# ---------------- Fixture retrieval ----------------

def team_recent_league_fixtures(team_id: int, league_id: int, limit: int = 24) -> List[dict]:
    """
    Best-effort to pull recent fixtures for a team, filtered to a given league.
    We request team 'results' and filter locally. Returns fixtures sorted by starting_at desc.
    """
    out: List[dict] = []

    # Ask for results; include league so we can filter cheaply.
    # (If modifiers like :order(...) or :limit(...) are ignored by the API, we still get a default chunk.)
    include = "results;results.league"
    try:
        j = api_get(f"teams/{team_id}", {"include": include})
        data = j.get("data") or {}
        results = data.get("results") or []
        # Filter to league
        for fx in results:
            if int(fx.get("league_id") or 0) == league_id:
                out.append(fx)
    except Exception:
        pass

    # Sort latest-first and keep up to limit
    out.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
    return out[:limit]

def extract_player_minutes_and_sot_from_fixture(fx_blob: dict, player_id: int) -> Tuple[Optional[int], Optional[int]]:
    """
    Given a fixture blob that includes statistics;statistics.player, find minutes and SOT for the player.
    Returns (minutes_played, shots_on_target) or (None, None) if not found.
    """
    # Sportmonks structure: fixture["statistics"] is list (per team), each has "players" list with per-player stats.
    stats = fx_blob.get("statistics") or []
    for team_stat in stats:
        players = team_stat.get("players") or team_stat.get("player") or []
        for p in players:
            pid = p.get("player_id") or (p.get("player") or {}).get("id")
            try:
                pid = int(pid)
            except Exception:
                pid = None
            if not pid or pid != player_id:
                continue

            minutes = None
            # minutes can be under "minutes", "minutes_played", or within "other" dicts depending on feed
            for key in ("minutes_played", "minutes", "mins_played"):
                if p.get(key) is not None:
                    minutes = int(p.get(key))
                    break
            if minutes is None:
                # sometimes in object nested as "maps" or "other"; best-effort
                other = p.get("other") or {}
                if isinstance(other, dict):
                    for key in ("minutes_played", "minutes"):
                        if other.get(key) is not None:
                            minutes = int(other.get(key))
                            break

            # shots on target can be "shots_on_target" or "shots_on"
            sot = None
            for key in ("shots_on_target", "shots_on"):
                if p.get(key) is not None:
                    sot = int(p.get(key))
                    break
            if sot is None:
                shots = p.get("shots") or {}
                if isinstance(shots, dict):
                    val = shots.get("on") or shots.get("on_target")
                    if val is not None:
                        sot = int(val)

            return minutes, sot
    return None, None

def fetch_fixture_with_stats(fixture_id: int) -> Optional[dict]:
    """
    GET a fixture and include statistics;statistics.player.
    Returns fixture blob or None if 404/Not Found.
    """
    try:
        j = api_get(f"fixtures/{fixture_id}", {"include": "statistics;statistics.player"})
        return j.get("data") or {}
    except requests.HTTPError as e:
        # Gracefully ignore missing fixtures
        if e.response is not None and e.response.status_code == 404:
            print(f"[WARN] fixture {fixture_id}: 404 Not Found")
            return None
        raise
    except Exception:
        raise

# ---------------- Core builder ----------------

def build_player_series_for_team(
    league_id: int,
    season_id: Optional[int],
    team_id: int,
    players: Dict[int, dict],
) -> List[dict]:
    """
    For one team, build SOT series for all 'players' dict (player_id -> {name, position_tag}).
    Returns list of row dicts for output JSON.
    """
    out_rows: List[dict] = []

    # Get a chunk of recent league fixtures (latest first)
    cand_fixtures = team_recent_league_fixtures(team_id, league_id, limit=40)
    cand_ids = [int(f.get("id")) for f in cand_fixtures if f.get("id")]

    # For speed, we won't pre-fetch all fixtures. We walk each player's history,
    # visiting fixture IDs in order, and stop once we collect N_LAST valid entries.

    for idx, (player_id, meta) in enumerate(players.items(), 1):
        if (idx - 1) % BATCH_SIZE == 0:
            time.sleep(SLEEP_BETWEEN_BATCHES)

        series_sot: List[int] = []
        series_min: List[int] = []
        series_fids: List[int] = []
        taken = 0

        for fid in cand_ids:
            fx = fetch_fixture_with_stats(fid)
            if not fx:
                continue

            # Quick guard: ensure league matches filter is respected
            if int(fx.get("league_id") or 0) != league_id:
                continue
            if season_id and int(fx.get("season_id") or 0) != int(season_id):
                continue

            minutes, sot = extract_player_minutes_and_sot_from_fixture(fx, player_id)
            if minutes is None or minutes < MIN_MINUTES:
                continue

            series_sot.append(int(sot or 0))
            series_min.append(int(minutes))
            series_fids.append(int(fid))
            taken += 1
            if taken >= N_LAST:
                break

        row = {
            "league_id": league_id,
            "season_id": season_id,
            "team_id": team_id,
            "player_id": player_id,
            "name": meta["name"],
            "position_tag": meta["position_tag"],
            "sot_last_n": series_sot,          # latest first
            "minutes_last_n": series_min,      # aligns with sot_last_n
            "fixture_ids": series_fids,        # aligns with sot_last_n
            "n": len(series_sot),
            "n_requested": N_LAST,
            "min_minutes": MIN_MINUTES,
            "order": "latest_first",
        }
        out_rows.append(row)

    return out_rows

# ---------------- Writers ----------------

def write_json(path: str, obj: dict) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def write_text(path: str, text: str) -> None:
    ensure_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)

# ---------------- Summaries ----------------

def write_summary_counts(out_root: str, per_league_rows: Dict[int, List[dict]]) -> None:
    total_players = sum(len(per_league_rows.get(lid, [])) for lid in LEAGUE_IDS)
    lines = []
    lines.append(f"Time (UTC): {now_iso()}")
    lines.append(f"Players : {total_players}")
    lines.append("")
    for lid in LEAGUE_IDS:
        cnt = len(per_league_rows.get(lid, []))
        lines.append(f"League {lid}: {cnt} players")
    write_text(os.path.join(out_root, "summary.txt"), "\n".join(lines) + "\n")

def write_summary_by_team(out_root: str, per_league_rows: Dict[int, List[dict]]) -> None:
    """
    Pretty list: per league -> per team -> player lines with SOT series.
    """
    # Build nested: league -> team_id -> team_name? (we don't have team name in rows)
    # We'll fetch a cheap team name map (cached).
    team_name_cache: Dict[int, str] = {}

    def team_name(team_id: int) -> str:
        if team_id in team_name_cache:
            return team_name_cache[team_id]
        # best-effort: try teams/{id}
        try:
            j = api_get(f"teams/{team_id}", {})
            nm = (j.get("data") or {}).get("name") or f"Team {team_id}"
        except Exception:
            nm = f"Team {team_id}"
        team_name_cache[team_id] = nm
        return nm

    lines: List[str] = []
    for lid in LEAGUE_IDS:
        rows = per_league_rows.get(lid, [])
        if not rows:
            continue
        lines.append(f"===== {LEAGUE_NAMES.get(lid, str(lid))} (LID {lid}) =====")

        # group by team
        by_team: Dict[int, List[dict]] = {}
        for r in rows:
            by_team.setdefault(int(r["team_id"]), []).append(r)

        for tid in sorted(by_team.keys(), key=lambda t: team_name(t).lower()):
            lines.append(team_name(tid))
            # sort players by name
            team_players = sorted(by_team[tid], key=lambda r: (r["position_tag"], r["name"]))
            for r in team_players:
                arr = r.get("sot_last_n") or []
                arr_str = ",".join(str(x) for x in arr) if arr else ""
                lines.append(f"  {r['name']} ({r['position_tag']}): [{arr_str}]")
            lines.append("")  # spacer

        lines.append("")  # league spacer

    write_text(os.path.join(out_root, "summary_by_team.txt"), "\n".join(lines).rstrip() + "\n")

# ---------------- Main ----------------

def main():
    predicted = load_predicted_players()
    if not predicted:
        print("No predicted XI data found. Did the Predict lineups workflow run?", file=sys.stderr)
        sys.exit(0)

    out_root = "data/player_shots_on_target"
    by_league_root = os.path.join(out_root, "by_league")
    ensure_dir(by_league_root)

    per_league_rows: Dict[int, List[dict]] = {}
    combined_rows: List[dict] = []

    for lid in LEAGUE_IDS:
        if lid not in predicted:
            continue

        season_id, _, _ = current_season_meta(lid)

        league_rows: List[dict] = []
        teams = predicted[lid]  # team_id -> {player_id -> meta}

        print(f"\n-- League {lid} ({LEAGUE_NAMES.get(lid)}) --")
        for tid, players in teams.items():
            # Build series for all predicted XI players in this team
            rows = build_player_series_for_team(lid, season_id, tid, players)
            league_rows.extend(rows)

        # sort league rows deterministically
        league_rows.sort(key=lambda r: (r["team_id"], r["position_tag"], r["name"], r["player_id"]))

        payload = {
            "generated_at": now_iso(),
            "league_id": lid,
            "league_name": LEAGUE_NAMES.get(lid, str(lid)),
            "count": len(league_rows),
            "players": league_rows,
        }
        write_json(os.path.join(by_league_root, f"{lid}.json"), payload)

        per_league_rows[lid] = league_rows
        combined_rows.extend(league_rows)

    # Combined JSON
    combined_payload = {
        "generated_at": now_iso(),
        "count": len(combined_rows),
        "players": combined_rows,
    }
    write_json(os.path.join(out_root, "combined.json"), combined_payload)

    # Summaries
    write_summary_counts(out_root, per_league_rows)
    write_summary_by_team(out_root, per_league_rows)

    # Final console note
    total_players = len(combined_rows)
    print("\nDone.")
    print(f"Players with SOT series: {total_players}")
    for lid in LEAGUE_IDS:
        print(f" - {LEAGUE_NAMES.get(lid, lid)}: {len(per_league_rows.get(lid, []))}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
