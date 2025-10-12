#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build per-player "Shots on Target from Outside the Box" (last N league matches of CURRENT season, ≥45').
Uses Sportmonks v3 and mirrors the existing player_shots.py workflow.

Writes:
  - data/player_sot_outbox/by_league/{league_id}.json
  - data/player_sot_outbox/combined.json
  - data/player_sot_outbox/summary.txt

Assumptions & notes:
- SOT type id = 86 (Sportmonks "Shots On Target"). We'll use events+ballCoordinates to locate each on-target shot. :contentReference[oaicite:5]{index=5}
- Fixtures include supports `events` and `ballCoordinates` for coordinates we need. :contentReference[oaicite:6]{index=6}
- Coordinate system is treated as 0..100 on both axes (common in Sportmonks). If your payload differs, tweak `xy_from_event()` and `is_inside_box_norm()`.
- We detect "on target" at event level via several common keys; you may refine `is_on_target_event()` once you inspect a few event payloads.
- We do NOT count penalties by default (toggle EXCLUDE_PENALTIES).
- Penalty-box geometry is normalized from 105x68m: box depth 16.5m (~15.7% of length), width 40.32m (~59.3% of pitch width). We treat "inside box" if:
    distance to NEAREST goal line ≤ 15.7% AND |y-50| ≤ 29.65%. Everything else is "outside box".
- We guard for missing locations by falling back to nearest ballCoordinate around the event time.

Env:
  SPORTMONKS_TOKEN or SPORTMONKS_API_TOKEN or SM_TOKEN
  PLAYER_SOT_OUTBOX_LAST_N (default: 10)
  MIN_MINUTES (default: 45)

Depends on:
  - Predicted XI payloads at data/predicted_xi/by_league/*.json (same as your existing flow)
  - Your existing season bounds and fixture loaders are reproduced here (self-contained).

"""

import os
import json
import time
import math
import datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any
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
SOT_TYPE_ID = 86  # Shots On Target (Sportmonks). :contentReference[oaicite:7]{index=7}

# ---- Collection rules ----
LAST_N = int(os.getenv("PLAYER_SOT_OUTBOX_LAST_N", "10"))
MIN_MINUTES = int(os.getenv("MIN_MINUTES", "45"))
EXCLUDE_PENALTIES = True  # flip to False if you want to keep penalties

# ---- IO ----
PRED_XI_DIR = Path("data/predicted_xi/by_league")
OUT_ROOT = Path("data/player_sot_outbox")
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
    raise last_exc

# ---- fixture + season helpers ----
DATE_FMT = "%Y-%m-%d"
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()
def dstr(d: dt.date) -> str:
    return d.strftime(DATE_FMT)

def get_season_bounds(season_id: int) -> Tuple[dt.date, dt.date]:
    """Fetch season start/end and clamp end to today."""
    j = api_get(f"seasons/{season_id}")
    data = j.get("data") or {}
    start_s = (data.get("starting_at") or "").split(" ")[0] or (data.get("starting_at") or "").split("T")[0]
    end_s   = (data.get("ending_at") or "").split(" ")[0] or (data.get("ending_at") or "").split("T")[0]
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else today_utc_date().replace(month=8, day=1)
    end   = min(today_utc_date(), dt.datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else today_utc_date())
    return start, end

def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, league_id: int, page: int = 1) -> dict:
    """
    GET fixtures for team in [start,end] with league filter and includes we need, ordered desc.
    Note: Sportmonks 'between' window max is 100 days; we walk windows backward. :contentReference[oaicite:8]{index=8}
    """
    path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
    params = {
        # events + ballCoordinates gives us shot locations; lineups.* gives minutes filters.
        "include": "events;ballCoordinates;lineups.details;lineups.player;state",
        "filters": f"fixtureLeagues:{league_id};lineupDetailTypes:{SOT_TYPE_ID},119",  # 119=minutes
        "order": "desc",
        "per_page": 50,
        "page": page,
    }
    return api_get(path, params)

# ---- predicted XI targets ----
def load_predicted_targets() -> Dict[Tuple[int, int, int], Dict[int, dict]]:
    """
    Returns: targets_by_team[(league_id, season_id, team_id)] = {player_id: {name, position_tag}}
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
            # try to record season via fixture (cheap - once each)
            season_id = row.get("season_id")
            if season_id is None:
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
                    target_map[int(pid)] = {"name": lp.get("name") or f"Player {pid}", "position_tag": pos}
    return out

# ---- minutes from lineup details (type 119) ----
def minutes_from_details(details: List[dict]) -> Optional[int]:
    for d in details or []:
        if int(d.get("type_id") or 0) == 119:
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

# -------- Geometry: inside/outside penalty box (normalized 0..100) --------
BOX_DEPTH_PCT   = 16.5 / 105.0 * 100.0     # ~15.71%
BOX_HALF_W_PCT  = (40.32 / 68.0 * 100.0)/2 # ~29.65%

def is_inside_box_norm(x: float, y: float) -> bool:
    """Inside either penalty box for normalized x,y in [0,100]."""
    if x is None or y is None:
        return False
    # distance to the NEAREST goal line (left x=0 or right x=100)
    dist_to_goal_line = min(x, 100.0 - x)
    return (dist_to_goal_line <= BOX_DEPTH_PCT) and (abs(y - 50.0) <= BOX_HALF_W_PCT)

# -------- Event parsing helpers --------
ON_TARGET_TEXTS = {"on target", "ontarget", "shot_on_target", "shots on target", "sot", "goal", "saved"}
PENALTY_HINTS   = {"penalty", "pk", "pen"}

def is_on_target_event(ev: dict) -> bool:
    """
    Heuristics to treat an event as 'on target'.
    - If explicit boolean flag exists (e.g., data.on_target), use it.
    - Otherwise, search common fields/strings: result/outcome/sub_type includes 'on target' or 'goal'/'saved'.
    Adjust once you inspect your payload.
    """
    d = ev.get("data") or {}
    # explicit bools
    for k in ("on_target", "onTarget", "on_target_shot", "shot_on_target"):
        val = d.get(k)
        if isinstance(val, bool):
            return val
        if isinstance(val, (int, float)) and val in (0,1):
            return bool(int(val))
    # texty fields
    fields = [
        str(d.get("result") or ""),
        str(d.get("outcome") or ""),
        str(d.get("sub_type") or ""),
        str(ev.get("result") or ""),
        str(ev.get("outcome") or ""),
        str(ev.get("sub_type") or ""),
        str(ev.get("type") or ""),
        str((ev.get("type_obj") or {}).get("code") or ""),
    ]
    blob = " ".join(s.lower() for s in fields if s)
    return any(tok in blob for tok in ON_TARGET_TEXTS)

def is_penalty_event(ev: dict) -> bool:
    d = ev.get("data") or {}
    # explicit
    for k in ("is_penalty", "penalty", "isPenalty"):
        if isinstance(d.get(k), bool) and d.get(k) is True:
            return True
    # texty hints
    fields = [
        str(d.get("result") or ""), str(d.get("sub_type") or ""),
        str(ev.get("result") or ""), str(ev.get("sub_type") or ""),
    ]
    blob = " ".join(s.lower() for s in fields if s)
    return any(tok in blob for tok in PENALTY_HINTS)

def xy_from_event(ev: dict) -> Tuple[Optional[float], Optional[float], Optional[int], Optional[int]]:
    """
    Return (x,y,minute,second) normalized 0..100 if present on the event.
    Tries common patterns; otherwise returns (None,None,minute,second) so the caller
    can attempt a ballCoordinates lookup near the timestamp.
    """
    d = ev.get("data") or {}
    # minute/second
    minute = None
    second = None
    for k in ("minute", "time", "clock_minute"):
        if isinstance(ev.get(k), (int,float)):
            minute = int(ev[k]); break
        if isinstance(d.get(k), (int,float)):
            minute = int(d[k]); break
    for k in ("second", "clock_second"):
        if isinstance(ev.get(k), (int,float)):
            second = int(ev[k]); break
        if isinstance(d.get(k), (int,float)):
            second = int(d[k]); break

    # coordinates (normalized likely)
    for xk, yk in (("x","y"), ("pos_x","pos_y"), ("location_x","location_y")):
        x = d.get(xk) if isinstance(d, dict) else None
        y = d.get(yk) if isinstance(d, dict) else None
        if isinstance(x, (int,float)) and isinstance(y, (int,float)):
            return float(x), float(y), minute, second

    # sometimes nested
    loc = d.get("location") or {}
    if isinstance(loc, dict):
        x, y = loc.get("x"), loc.get("y")
        if isinstance(x, (int,float)) and isinstance(y, (int,float)):
            return float(x), float(y), minute, second

    return None, None, minute, second

def nearest_xy_from_ballcoords(ballcoords: List[dict], minute: Optional[int], second: Optional[int]) -> Tuple[Optional[float], Optional[float]]:
    """
    Find the (x,y) from ballCoordinates closest to the supplied minute/second.
    Treats x,y as normalized 0..100 if present. If structure differs, adjust here.
    """
    if not ballcoords:
        return None, None
    # Flatten potential nested structures
    rows = []
    for bc in ballcoords:
        # common keys: minute, second, x, y
        m = bc.get("minute"); s = bc.get("second")
        x = bc.get("x");      y = bc.get("y")
        if isinstance(x,(int,float)) and isinstance(y,(int,float)):
            when = (int(m) if isinstance(m,(int,float)) else None, int(s) if isinstance(s,(int,float)) else None)
            rows.append((when, float(x), float(y)))
    if not rows:
        return None, None

    # If we have target time, minimize absolute difference in total seconds
    if minute is not None:
        tgt = minute*60 + (second or 0)
        best = min(rows, key=lambda r: abs((r[0][0] or 0)*60 + (r[0][1] or 0) - tgt))
        return best[1], best[2]

    # Otherwise just take the end snapshot
    return rows[-1][1], rows[-1][2]

def collect_series_for_team(league_id: int, season_id: int, team_id: int, targets: Dict[int, dict]) -> Dict[int, dict]:
    """
    Returns: player_id -> { 'sot_outside': [latest->older], 'minutes': [...], 'fixtures': [ids] }
    Only fixtures in this league & season; include only if minutes >= MIN_MINUTES.
    """
    want = set(targets.keys())
    series: Dict[int, dict] = {pid: {"sot_outside": [], "minutes": [], "fixtures": []} for pid in want}

    start_season, end_today = get_season_bounds(season_id)
    end = end_today

    def have_all() -> bool:
        return all(len(series[pid]["sot_outside"]) >= LAST_N for pid in want)

    while end >= start_season and not have_all():
        win_start = max(start_season, end - dt.timedelta(days=99))  # 100-day window
        page, has_more = 1, True
        while has_more and not have_all():
            j = fetch_team_fixtures_window(team_id, win_start, end, league_id, page=page)
            data = j.get("data") or []
            meta = j.get("meta") or {}
            has_more = bool(meta.get("has_more"))
            page += 1

            for fx in data:
                if int(fx.get("league_id") or 0) != league_id:  # hard league check
                    continue
                if int(fx.get("season_id") or 0) != season_id:
                    continue
                if int(fx.get("state_id") or 0) not in (5,):    # finished
                    continue

                fid = int(fx.get("id") or 0)
                # build quick lookup: minutes per player
                minutes_map: Dict[int, int] = {}
                for lp in (fx.get("lineups") or []):
                    if int(lp.get("team_id") or 0) != team_id:
                        continue
                    pid = int(lp.get("player_id") or 0)
                    if pid in want:
                        mins = minutes_from_details(lp.get("details") or [])
                        if mins is not None:
                            minutes_map[pid] = int(mins)

                # extract ballCoordinates once per fixture
                ballcoords = fx.get("ballCoordinates") or fx.get("ballcoordinates") or []

                # iterate events and aggregate on-target shots outside the box per player
                tmp_counts: Dict[int, int] = {}
                for ev in (fx.get("events") or []):
                    pid = ev.get("player_id") or (ev.get("player") or {}).get("id")
                    try:
                        pid = int(pid)
                    except Exception:
                        continue
                    if pid not in want:
                        continue
                    # minutes filter per player
                    if minutes_map.get(pid, 0) < MIN_MINUTES:
                        continue
                    # must be on target
                    if not is_on_target_event(ev):
                        continue
                    # optional: exclude penalties
                    if EXCLUDE_PENALTIES and is_penalty_event(ev):
                        continue

                    # coordinates
                    x, y, minute, second = xy_from_event(ev)
                    if x is None or y is None:
                        x, y = nearest_xy_from_ballcoords(ballcoords, minute, second)
                    if x is None or y is None:
                        # couldn't resolve location -> skip gracefully
                        continue

                    # normalize safeguard (if 0..1 scale)
                    if 0.0 <= x <= 1.0 and 0.0 <= y <= 1.0:
                        x *= 100.0; y *= 100.0

                    if not is_inside_box_norm(x, y):
                        tmp_counts[pid] = tmp_counts.get(pid, 0) + 1

                # commit per-player (even zeros if they had minutes)
                for pid in want:
                    if minutes_map.get(pid, 0) >= MIN_MINUTES:
                        s = series[pid]
                        s["sot_outside"].append(int(tmp_counts.get(pid, 0)))
                        s["minutes"].append(int(minutes_map[pid]))
                        s["fixtures"].append(fid)

                if have_all():
                    break

        end = win_start - dt.timedelta(days=1)

    # Clamp to LAST_N, keep latest->older order
    for pid in series:
        series[pid]["sot_outside"] = series[pid]["sot_outside"][:LAST_N]
        series[pid]["minutes"] = series[pid]["minutes"][:LAST_N]
        series[pid]["fixtures"] = series[pid]["fixtures"][:LAST_N]
    return series

# ---- main packing/writing ----
def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

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
        series = collect_series_for_team(league_id, season_id, team_id, targets)
        for pid, s in series.items():
            entry = {
                "league_id": league_id,
                "season_id": season_id,
                "team_id": team_id,
                "player_id": pid,
                "name": targets[pid]["name"],
                "position_tag": targets[pid].get("position_tag"),
                # Order: latest first (most recent match to older)
                "sot_outside_last_n": s["sot_outside"],
                "minutes_last_n": s["minutes"],
                "fixture_ids": s["fixtures"],
                "n": len(s["sot_outside"]),
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
    out_c = OUT_ROOT / "combined.json"
    ensure_dir(out_c)
    tmp_c = out_c.with_suffix(".json.tmp")
    tmp_c.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_c.replace(out_c)
    print(f"Wrote {out_c}")

    # summary (quick text view)
    lines: List[str] = []
    lines.append(f"UTC: {generated_at}")
    by_lid: Dict[int, int] = {}
    for lid, rows in per_league_players.items():
        by_lid[lid] = len(rows)
    lines.append("Per-league player rows:")
    for lid in sorted(by_lid.keys()):
        lines.append(f"  - {lid}: {by_lid[lid]}")
    summary_path = OUT_ROOT / "summary.txt"
    ensure_dir(summary_path)
    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {summary_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
