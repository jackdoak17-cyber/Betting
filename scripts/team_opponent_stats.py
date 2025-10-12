#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Collect per-team LAST_N opponent series from Sportmonks v3 fixture team statistics.

Opponent stats captured (latest -> older):
- opp_shots_total       (type_id 42)
- opp_shots_on_target   (86)
- opp_fouls             (56)
- opp_tackles           (78)
- opp_cards_total       (yellow 84 + red 83)
- opp_saves             (57)
- opp_goal_kicks        (53)
- opp_corners           (34)

Writes:
  - data/team_opponent_stats/by_league/{league_id}.json
  - data/team_opponent_stats/combined.json
  - data/team_opponent_stats/summary.txt

Notes:
- Only finished fixtures (state_id == 5) in the target league/season.
- We use include=participants;statistics;state (NOT 'teams').
- We filter by fixtureStatisticTypes to keep payloads lean.
"""

import os, json, time, datetime as dt
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
GLOBAL_MIN_DELAY = 0.18  # gentle pacing between calls

# ---- Type IDs (override via env if needed) ----
SHOTS_TOTAL      = int(os.getenv("TEAM_STAT_SHOTS_TOTAL_ID", "42"))
SHOTS_ON_TARGET  = int(os.getenv("TEAM_STAT_SHOTS_ON_TARGET_ID", "86"))
FOULS            = int(os.getenv("TEAM_STAT_FOULS_ID", "56"))
TACKLES          = int(os.getenv("TEAM_STAT_TACKLES_ID", "78"))
YELLOW           = int(os.getenv("TEAM_STAT_YELLOW_CARDS_ID", "84"))
RED              = int(os.getenv("TEAM_STAT_RED_CARDS_ID", "83"))
SAVES            = int(os.getenv("TEAM_STAT_SAVES_ID", "57"))
GOAL_KICKS       = int(os.getenv("TEAM_STAT_GOAL_KICKS_ID", "53"))
CORNERS          = int(os.getenv("TEAM_STAT_CORNERS_ID", "34"))

# ---- Collection rules ----
LAST_N = int(os.getenv("TEAM_OPP_STATS_LAST_N", "10"))

# ---- IO ----
PX_DIR = Path("data/predicted_xi/by_league")
OUT_ROOT = Path("data/team_opponent_stats")
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

# ---- helpers ----
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def ensure_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

def load_target_teams() -> Dict[Tuple[int, int], Dict[int, str]]:
    """
    Returns: teams_by_league_season[(league_id, season_id)] = {team_id: team_name}
    Pulls from predicted_xi files (home/away). If season_id missing, resolve via a fixture lookup.
    """
    teams: Dict[Tuple[int, int], Dict[int, str]] = {}
    if not PX_DIR.exists():
        return teams

    for f in PX_DIR.glob("*.json"):
        try:
            blob = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        lid = int(blob.get("league_id") or 0)
        for row in (blob.get("fixtures") or []):
            fid = row.get("fixture_id")
            season_id = row.get("season_id") or None
            if season_id is None and isinstance(fid, int):
                try:
                    fx = api_get(f"fixtures/{fid}").get("data") or {}
                    season_id = int(fx.get("season_id") or 0) or None
                except Exception:
                    pass
            if not (lid and season_id):
                continue
            key = (int(lid), int(season_id))
            mm = teams.setdefault(key, {})
            for side in ("home", "away"):
                s = row.get(side) or {}
                tid, nm = s.get("team_id"), (s.get("name") or "").strip()
                if isinstance(tid, int) and nm:
                    mm.setdefault(tid, nm)
    return teams

def get_season_bounds(season_id: int) -> Tuple[dt.date, dt.date]:
    """Fetch season start/end (clamp end to today)."""
    j = api_get(f"seasons/{season_id}")
    data = j.get("data") or {}
    def _take(s: Optional[str]) -> Optional[str]:
        if not s: return None
        return s.split("T")[0].split(" ")[0]
    start_s = _take(data.get("starting_at"))
    end_s   = _take(data.get("ending_at"))
    start = dt.datetime.strptime(start_s, "%Y-%m-%d").date() if start_s else today_utc_date().replace(month=8, day=1)
    end   = min(today_utc_date(), dt.datetime.strptime(end_s, "%Y-%m-%d").date() if end_s else today_utc_date())
    return start, end

def fetch_team_fixtures_window(team_id: int, start: dt.date, end: dt.date, league_id: int, type_ids: List[int], page: int = 1) -> dict:
    """
    GET fixtures for team in [start,end] with league & statistic type filters, latest first.
    """
    path = f"fixtures/between/{dstr(start)}/{dstr(end)}/{team_id}"
    # Valid includes are participants;statistics;state
    includes = "participants;statistics;state"
    filt = f"fixtureLeagues:{league_id};fixtureStatisticTypes:{','.join(str(x) for x in type_ids)}"
    params = {
        "include": includes,
        "filters": filt,
        "per_page": 50,
        "page": page,
    }
    try:
        return api_get(path, params)
    except requests.HTTPError as e:
        # Belt-and-braces fallback: probe without statistics, then bulk fetch by IDs
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            probe = api_get(path, {"include": "participants;state", "filters": f"fixtureLeagues:{league_id}", "per_page": 50, "page": page})
            ids = ",".join(str(fx.get("id")) for fx in (probe.get("data") or []) if fx.get("id"))
            if ids:
                return api_get(f"fixtures/{ids}", {"include": "participants;statistics;state"})
        raise

def _opponent_id_from_fixture(fx: dict, team_id: int) -> Optional[int]:
    """
    Prefer participants include; fallback to any statistics participant != team_id.
    """
    parts = fx.get("participants")
    if isinstance(parts, list) and len(parts) >= 2:
        ids = [int(p.get("id")) for p in parts if p.get("id") is not None]
        others = [i for i in ids if i != team_id]
        if others:
            return others[0]
    # Fallback
    stats = fx.get("statistics") or []
    participants = {int(s.get("participant_id")) for s in stats if s.get("participant_id") is not None}
    others = [i for i in participants if i != team_id]
    if others:
        return others[0]
    return None

def collect_opponent_series(league_id: int, season_id: int, team_id: int) -> dict:
    """
    Build opponent stat series for the given team.
    Returns:
      {
        'stats': {
            'opp_shots_total':[...],'opp_shots_on_target':[...],'opp_fouls':[...],'opp_tackles':[...],
            'opp_cards_total':[...],'opp_saves':[...],'opp_goal_kicks':[...],'opp_corners':[...]
        },
        'fixtures': [ids],  # aligned to the series; latest->older
      }
    """
    type_ids = list({SHOTS_TOTAL, SHOTS_ON_TARGET, FOULS, TACKLES, YELLOW, RED, SAVES, GOAL_KICKS, CORNERS})
    start_season, end_today = get_season_bounds(season_id)
    end = end_today

    series = {
        "opp_shots_total": [], "opp_shots_on_target": [], "opp_fouls": [], "opp_tackles": [],
        "opp_cards_total": [], "opp_saves": [], "opp_goal_kicks": [], "opp_corners": []
    }
    fixture_ids: List[int] = []

    def have_enough() -> bool:
        return all(len(series[k]) >= LAST_N for k in series.keys())

    while end >= start_season and not have_enough():
        win_start = max(start_season, end - dt.timedelta(days=99))  # 100-day window
        page = 1
        has_more = True
        while has_more and not have_enough():
            j = fetch_team_fixtures_window(team_id, win_start, end, league_id, type_ids, page=page)
            data = j.get("data") or []
            meta = j.get("meta") or {}
            per_page = 50
            # meta.has_more may not always be present; infer from page size as fallback
            has_more = bool(meta.get("has_more")) or (len(data) == per_page)
            page += 1

            for fx in data:
                if int(fx.get("league_id") or 0) != league_id:
                    continue
                if int(fx.get("season_id") or 0) != season_id:
                    continue
                if int(fx.get("state_id") or 0) not in (5,):  # finished only
                    continue

                fid = int(fx.get("id") or 0)
                opp_id = _opponent_id_from_fixture(fx, team_id)
                if opp_id is None:
                    continue

                # Gather opponent stats: type_id -> value
                by_type_opp: Dict[int, int] = {}
                for s in (fx.get("statistics") or []):
                    try:
                        if int(s.get("participant_id") or 0) != opp_id:
                            continue
                        t = int(s.get("type_id") or 0)
                        vobj = s.get("data") or s.get("value") or {}
                        val = vobj.get("value") if isinstance(vobj, dict) else None
                        if val is None:
                            continue
                        by_type_opp[t] = int(float(val))
                    except Exception:
                        continue

                cards_total = int(by_type_opp.get(YELLOW, 0)) + int(by_type_opp.get(RED, 0))

                series["opp_shots_total"].append(int(by_type_opp.get(SHOTS_TOTAL, 0)))
                series["opp_shots_on_target"].append(int(by_type_opp.get(SHOTS_ON_TARGET, 0)))
                series["opp_fouls"].append(int(by_type_opp.get(FOULS, 0)))
                series["opp_tackles"].append(int(by_type_opp.get(TACKLES, 0)))
                series["opp_cards_total"].append(cards_total)
                series["opp_saves"].append(int(by_type_opp.get(SAVES, 0)))
                series["opp_goal_kicks"].append(int(by_type_opp.get(GOAL_KICKS, 0)))
                series["opp_corners"].append(int(by_type_opp.get(CORNERS, 0)))
                fixture_ids.append(fid)

                if have_enough():
                    break

        end = win_start - dt.timedelta(days=1)

    # clamp latest->older to LAST_N
    for k in series:
        series[k] = series[k][:LAST_N]
    fixture_ids = fixture_ids[:LAST_N]

    return {"stats": series, "fixtures": fixture_ids}

def main():
    teams_by_ls = load_target_teams()
    if not teams_by_ls:
        print("No targets found. Did predicted_xi run?")
        return

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    per_league: Dict[int, List[dict]] = {}
    combined_rows: List[dict] = []

    items = []
    for (lid, sid), tm in teams_by_ls.items():
        for tid, tname in tm.items():
            items.append((lid, sid, tid, tname))
    items.sort()

    for idx, (lid, sid, tid, tname) in enumerate(items, 1):
        print(f"[{idx}/{len(items)}] L{lid} S{sid} Team {tid} — {tname}")
        coll = collect_opponent_series(lid, sid, tid)
        entry = {
            "league_id": lid,
            "season_id": sid,
            "team_id": tid,
            "team_name": tname,
            "order": "latest_first",
            "n_requested": LAST_N,
            "fixture_ids": coll["fixtures"],
            **{k + "_last_n": coll["stats"][k] for k in coll["stats"].keys()},
        }
        per_league.setdefault(lid, []).append(entry)
        combined_rows.append(entry)

    # write per-league
    for lid, rows in per_league.items():
        rows.sort(key=lambda r: (r["team_name"].lower(), r["team_id"]))
        payload = {
            "generated_at": generated_at,
            "league_id": lid,
            "count": len(rows),
            "teams": rows,
        }
        out = BY_LEAGUE_DIR / f"{lid}.json"
        ensure_dir(out)
        tmp = out.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"))
        tmp.replace(out)
        print(f"Wrote {out}")

    # combined
    combined = {
        "generated_at": generated_at,
        "count": len(combined_rows),
        "teams": combined_rows,
    }
    outc = OUT_ROOT / "combined.json"
    ensure_dir(outc)
    tmpc = outc.with_suffix(".json.tmp")
    tmpc.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"))
    tmpc.replace(outc)

    # summary (counts per league)
    lines = []
    lines.append(f"Time (UTC): {generated_at}")
    lines.append(f"Teams  : {len(combined_rows)}")
    lines.append("")
    for lid in sorted(per_league):
        lines.append(f"League {lid}: {len(per_league[lid])} teams")
    (OUT_ROOT / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
