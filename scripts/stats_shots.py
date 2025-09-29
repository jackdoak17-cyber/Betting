#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json, sys
from typing import Dict, List, Tuple, Optional
from common import sm_get, DATA_DIR, pos_label

APPEAR_MIN = 45
LINEUP_TYPE_STARTER = 11

SHOT_DEVS_TOTAL = {"SHOTS","SHOTS_TOTAL"}
SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS    = {"MINUTES_PLAYED","MINUTES"}

def load_lineups_for_league(league_id: int) -> List[dict]:
    path = os.path.join(DATA_DIR, "lineups.jsonl")
    if not os.path.isfile(path): return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
                if row.get("league_id") == league_id:
                    out.append(row)
            except:
                pass
    return out

def fixtures_for_date(date_str: str, league_id: int) -> List[dict]:
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = sm_get(f"fixtures/date/{date_str}", params, ttl_sec=1800)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page + 1):
        params["page"] = p
        jp = sm_get(f"fixtures/date/{date_str}", params, ttl_sec=1800)
        data.extend(jp.get("data", []) or [])
    return [fx for fx in data if fx.get("league_id") == league_id]

def _num(detail: dict) -> int:
    v = (detail.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except: return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int, float)): s += int(x)
        return s
    try: return int(v or 0)
    except: return 0

def lineup_minutes_shots(fid: int) -> Tuple[Dict[int,int], Dict[int,int]]:
    j = sm_get(f"fixtures/{fid}", {"include":"lineups.details.type"}, ttl_sec=1800).get("data", {})
    shots, mins = {}, {}
    for lp in (j.get("lineups") or []):
        pid = lp.get("player_id")
        if pid is None: continue
        pid = int(pid)
        total_from_api = None; sot = soff = 0; mval = None
        for det in (lp.get("details") or []):
            dev = ((det.get("type") or {}).get("developer_name") or "").upper()
            if dev in SHOT_DEVS_TOTAL: total_from_api = _num(det)
            elif dev in SHOT_DEVS_SOT: sot += _num(det)
            elif dev in SHOT_DEVS_SOFF: soff += _num(det)
            elif dev in MINUTES_DEVS:
                mv = _num(det)
                mval = mv if mval is None else max(mval, mv)
        if mval is not None: mins[pid] = mval
        if total_from_api is not None or (sot + soff) > 0:
            shots[pid] = total_from_api if total_from_api is not None else (sot + soff)
    return mins, shots

def get_team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    # seed from 'latest'
    collected, seen = [], set()
    try:
        j = sm_get(f"teams/{team_id}", {"include":"latest.league"}, ttl_sec=3600)
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id") == league_id and fx.get("id") not in seen:
                collected.append(fx); seen.add(fx.get("id"))
    except Exception:
        pass
    # scan back by date for 730 days (cheap via fixtures/date + league filter)
    import datetime as dt
    from common import DATE_FMT, today_utc
    today = today_utc()
    for back in range(1, 731):
        ds = (today - dt.timedelta(days=back)).strftime(DATE_FMT)
        try:
            fxs = fixtures_for_date(ds, league_id)
        except Exception:
            continue
        for fx in fxs:
            fid = fx.get("id")
            if not fid or fid in seen: continue
            if any(p.get("id") == team_id for p in (fx.get("participants") or [])):
                collected.append(fx); seen.add(fid)
        if len(collected) >= want * 14:
            break
    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected

def last_n_shots_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    series = []
    fixtures = get_team_recent_league_fixtures(team_id, league_id, n)
    for fx in fixtures:
        if len(series) >= n: break
        mins, shots = lineup_minutes_shots(fx.get("id"))
        m = mins.get(player_id)
        if m is None or m < APPEAR_MIN: continue
        series.append(shots.get(player_id, 0))
    return series[:n]

def main():
    if len(sys.argv) < 2:
        print("usage: stats_shots.py LEAGUE_ID")
        sys.exit(2)
    league_id = int(sys.argv[1])
    # load players from XIs
    xi_rows = load_lineups_for_league(league_id)
    # map players to team (from the lineup row itself)
    by_player = {}
    for r in xi_rows:
        pid = r.get("player_id")
        if pid is None:  # guard
            continue
        by_player[pid] = r

    out_path = os.path.join(DATA_DIR, f"shots_rollups_{league_id}.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for pid, row in by_player.items():
            team_id = row["team_id"]
            s10 = last_n_shots_series(team_id, pid, 10, league_id)
            s5 = s10[:5] if len(s10) >= 5 else s10
            def hit(series): 
                return 0 if not series else round(100.0 * sum(1 for x in series if x>=1) / len(series), 1)
            rec = {
                "league_id": league_id,
                "player_id": pid,
                "player_name": row["player_name"],
                "team_id": team_id,
                "team_name": row["team_name"],
                "position": row.get("position") or pos_label(row.get("position_id")),
                "apps10": len(s10), "hit10": hit(s10), "apps5": len(s5), "hit5": hit(s5),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[OK] shots rollups written: {out_path}")

if __name__ == "__main__":
    main()
