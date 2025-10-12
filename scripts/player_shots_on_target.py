#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build per-player shots-on-target histories (last N league matches of CURRENT season, ≥45').
Targets ONLY players found in data/predicted_xi/by_league/*.json.
Writes:
  - data/player_shots_on_target/by_league/{league_id}.json
  - data/player_shots_on_target/combined.json
  - data/player_shots_on_target/summary.txt

Notes:
- Uses Sportmonks v3:
  * fixtures between (team, date range) + include=lineups.details;lineups.player
  * filters=lineupDetailTypes:86,119;fixtureLeagues:{league_id}
  * Honors 100-day window limit by walking backward windows.
- Stat type IDs (Sportmonks):
  * SHOTS_ON_TARGET=86, MINUTES_PLAYED=119  (see your API notes)
"""

import os
import json
import time
import datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests

# ---- API config ----
API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
if not API_TOKEN:
    raise SystemExit("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.")

TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
GLOBAL_MIN_DELAY = 0.18  # gentle pacing

# ---- Stat type ids ----
SHOTS_ON_TGT = 86
MINUTES_PLAYED = 119

# ---- Collection rules ----
LAST_N = int(os.getenv("PLAYER_SOT_LAST_N", "10"))
MIN_MINUTES = int(os.getenv("PLAYER_MIN_MINUTES", "45"))

# ---- IO ----
PRED_XI_DIR = Path("data/predicted_xi/by_league")
OUT_ROOT = Path("data/player_shots_on_target")
BY_LEAGUE_DIR = OUT_ROOT / "by_league"
BY_LEAGUE_DIR.mkdir(parents=True, exist_ok=True)

# ---- tiny http helpers ----
_last_call = 0.0
def _pace():
    global _last_call
    now = time.time()
    if now - _last_call < GLOBAL_MIN_DELAY:
        time.sleep(GLOBAL_MIN_DELAY - (now - _last_call))
    _last_call = time.time()

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{path.lstrip('/')}"
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
            return r.json()
        except Exception as e:
            last_exc = e
            if i < RETRIES:
                sleep = BACKOFF ** i
                print(f"[RETRY] {path} (attempt {i}) sleeping {sleep:.1f}s")
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # pragma: no cover

# ---- helpers ----
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def load_predicted_targets() -> Dict[Tuple[int, int, int], Dict[int, dict]]:
    """
    Returns: targets_by_team[(league_id, season_id, team_id)] = {player_id: {name, position_tag}}
    Position tag prefers predicted 'role' (LB/RB/CB/DM/AM/LW/RW/ST/GK) else 'position_label'.
    """
    out: Dict[Tuple[int, int, int], Dict[int, dict]] = {}
    if not PRED_XI_DIR.exists():
        return out

    for f in PRED_XI_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = int(blob.get("league_id") or 0)
        fixtures = blob.get("fixtures") or []
        for row in fixtures:
            fid = row.get("fixture_id")
            season_id = row.get("season_id") or None
            if season_id is None and isinstance(fid, int):
                # Light attempt to fetch season if it wasn't persisted in predicted_xi
                try:
                    fx = api_get(f"fixtures/{fid}", params={"include": ""}).get("data") or {}
                    season_id = int(fx.get("season_id") or 0) or None
                except Exception:
                    pass
            for side in ("home", "away"):
                team = row.get(side) or {}
                team_id = team.get("team_id")
                if not (lid and season_id and team_id):
                    continue
                key = (int(lid), int(season_id), int(team_id))
                target_map = out.setdefault(key, {})
                for lp in team.get("predicted_xi") or []:
                    pid = lp.get("player_id")
                    if not pid:
                        continue
                    pos = (lp.get("role") or lp.get("position_label") or "").strip() or None
                    target_map[int(pid)] = {
                        "name": lp.get("name") or f"Player {pid}",
                        "position_tag": pos,
                    }
    return out

def get_season_bounds(season_id: int) -> Tuple[dt.date, dt.date]:
    """Fetch season start/end (clamp end to today)."""
    j = api_get(f"seasons/{season_id}")
    data = j.get("data") or {}
    start_s = (data.get("starting_at") or "").split("T")[0] or (data.get("starting_at") or "").split(" ")[0]
    end_s = (data.get("ending_at") or "").split("T")[0] or (data.get("ending_at") or "").split(" ")[0]
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else today_utc_date().replace(month=8, day=1)
    end = min(today_utc_date(), dt.datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else today_utc_date())
    return start, end

def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, league_id: int, page: int = 1) -> dict:
    """
    GET fixtures for team in [start,end] with league filter and needed includes, ordered desc.
    Only lineups.details for SHOTS_ON_TGT + MINUTES.
    """
    path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
    params = {
        "include": "lineups.details;lineups.player;state",
        "filters": f"lineupDetailTypes:{SHOTS_ON_TGT},{MINUTES_PLAYED};fixtureLeagues:{league_id}",
        "order": "desc",
        "per_page": 50,
        "page": page,
    }
    return api_get(path, params)

def minutes_from_details(details: List[dict]) -> Optional[int]:
    for d in details or []:
        if int(d.get("type_id") or 0) == MINUTES_PLAYED:
            v = d.get("data") or d.get("value") or {}
            val = v.get("value") if isinstance(v, dict) else None
            try:
                return int(val)
            except Exception:
                try:
                    return int(float(val))
                except Exception:
                    return None
    return None

def shots_on_target_from_details(details: List[dict]) -> int:
    for d in details or []:
        if int(d.get("type_id") or 0) == SHOTS_ON_TGT:
            vobj = d.get("data") or d.get("value") or {}
            val = vobj.get("value") if isinstance(vobj, dict) else None
            try:
                return int(val)
            except Exception:
                try:
                    return int(float(val))
                except Exception:
                    return 0
    return 0

def collect_player_series_for_team(league_id: int, season_id: int, team_id: int, targets: Dict[int, dict]) -> Dict[int, dict]:
    """
    Returns: player_id -> { 'on_target': [latest->older], 'minutes': [...], 'fixtures': [ids] }
    (Only fixtures in this league & season; include only if minutes >= MIN_MINUTES)
    """
    want = set(targets.keys())
    series: Dict[int, dict] = {pid: {"on_target": [], "minutes": [], "fixtures": []} for pid in want}

    start_season, end_today = get_season_bounds(season_id)
    end = end_today
    have_all = lambda: all(len(series[pid]["on_target"]) >= LAST_N for pid in want)

    while end >= start_season and not have_all():
        win_start = max(start_season, end - dt.timedelta(days=99))  # 100-day window
        page = 1
        has_more = True
        while has_more and not have_all():
            j = fetch_team_fixtures_window(team_id, win_start, end, league_id, page=page)
            data = j.get("data") or []
            meta = j.get("meta") or {}
            has_more = bool(meta.get("has_more"))
            page += 1

            for fx in data:
                # Hard filters: league + season + finished (state_id 5 commonly == finished)
                if int(fx.get("league_id") or 0) != league_id:
                    continue
                if int(fx.get("season_id") or 0) != season_id:
                    continue
                st = int(fx.get("state_id") or 0)
                if st not in (5,):  # finished only
                    continue

                fid = int(fx.get("id") or 0)
                for lp in fx.get("lineups") or []:
                    pid = int(lp.get("player_id") or 0)
                    if pid not in want:
                        continue
                    if int(lp.get("team_id") or 0) != team_id:
                        continue
                    details = lp.get("details") or []
                    mins = minutes_from_details(details)
                    if mins is None or mins < MIN_MINUTES:
                        continue
                    sot = shots_on_target_from_details(details)
                    s = series[pid]
                    s["on_target"].append(int(sot))
                    s["minutes"].append(int(mins))
                    s["fixtures"].append(fid)

            if have_all():
                break

        # walk window backward
        end = win_start - dt.timedelta(days=1)

    # Clamp to LAST_N, keep latest->older order
    for pid in series:
        series[pid]["on_target"] = series[pid]["on_target"][:LAST_N]
        series[pid]["minutes"] = series[pid]["minutes"][:LAST_N]
        series[pid]["fixtures"] = series[pid]["fixtures"][:LAST_N]
    return series

def main():
    targets_by_team = load_predicted_targets()
    if not targets_by_team:
        print("No predicted XI targets found. Did predict_lineups run?")
        return

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    combined_players: List[dict] = []
    per_league_players: Dict[int, List[dict]] = {}

    items = list(targets_by_team.items())
    for idx, ((league_id, season_id, team_id), targets) in enumerate(items, 1):
        print(f"[{idx}/{len(items)}] League {league_id} season {season_id} team {team_id} — {len(targets)} players")
        series = collect_player_series_for_team(league_id, season_id, team_id, targets)
        for pid, s in series.items():
            entry = {
                "league_id": league_id,
                "season_id": season_id,
                "team_id": team_id,
                "player_id": pid,
                "name": targets[pid]["name"],
                "position_tag": targets[pid].get("position_tag"),
                # Order: latest first (most recent match to older)
                "on_target_last_n": s["on_target"],
                # Extras we got in the same calls:
                "minutes_last_n": s["minutes"],
                "fixture_ids": s["fixtures"],
                "n": len(s["on_target"]),
                "n_requested": LAST_N,
                "min_minutes": MIN_MINUTES,
                "order": "latest_first",
            }
            per_league_players.setdefault(league_id, []).append(entry)
            combined_players.append(entry)

    # write per-league
    for lid, rows in per_league_players.items():
        rows.sort(key=lambda r: (r["team_id"], r["player_id"]))
        payload = {
            "generated_at": generated_at,
            "league_id": lid,
            "count": len(rows),
            "players": rows,
        }
        out = BY_LEAGUE_DIR / f"{lid}.json"
        ensure_dir(out)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
        print(f"Wrote {out}")

    # combined
    combined = {
        "generated_at": generated_at,
        "count": len(combined_players),
        "players": combined_players,
    }
    outc = OUT_ROOT / "combined.json"
    ensure_dir(outc)
    tmpc = outc.with_suffix(".json.tmp")
    tmpc.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    tmpc.replace(outc)

    # summary
    lines = []
    lines.append(f"Time (UTC): {generated_at}")
    lines.append(f"Players : {len(combined_players)}")
    lines.append("")
    for lid in sorted(per_league_players):
        lines.append(f"League {lid}: {len(per_league_players[lid])} players")
    (OUT_ROOT / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
