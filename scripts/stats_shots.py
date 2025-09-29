#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Compute last up to 10 LEAGUE appearances (>=45') shot counts per player for the next 6 days
of fixtures for a single league, then save JSONL with both hit rates AND the raw series.
USAGE:  python scripts/stats_shots.py <LEAGUE_ID>

Writes: data/shots_stats_<LEAGUE_ID>.jsonl   (one JSON object per player)
Fields (per line):
  {
    "league_id": 8,
    "team_id": 123,
    "player_id": 456,
    "display": "#7 Bukayo Saka (pos=FWD)",
    "pos": "FWD",
    "apps10": 10, "hit10_1p": 100.0, "hit10_2p": 80.0, "hit10_3p": 50.0,
    "apps5": 5,  "hit5_1p": 100.0,  "hit5_2p": 60.0,  "hit5_3p": 40.0,
    "series10": [2,1,3,0,1, ...],     # newest→older, only matches with >=45'
    "series5":  [2,1,3,0,1]           # newest→older, subset of series10
  }
"""
import os, sys, time, json, datetime as dt
from typing import Dict, List, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN")

LINEUP_TYPE_STARTER = 11
APPEARANCE_MINUTES_THRESHOLD = 45
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.6
DATE_FMT = "%Y-%m-%d"

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

class Memo:
    def __init__(self):
        self.store: Dict[str, dict] = {}
    def get(self, k): return self.store.get(k)
    def set(self, k, v): self.store[k] = v

memo = Memo()

def cached_get(url: str, params: Optional[dict] = None) -> dict:
    if params is None: params = {}
    params = {**params, "api_token": API_TOKEN}
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    hit = memo.get(key)
    if hit is not None: return hit
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code >= 400:
                try: jerr = r.json()
                except Exception: jerr = {"message": r.text[:300]}
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {jerr}")
            j = r.json(); memo.set(key, j); return j
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                time.sleep(BACKOFF ** attempt)
            else:
                raise
    raise last_exc

def api_get(path: str, params: Optional[dict] = None) -> dict:
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return cached_get(url, params or {})

def get_fixtures_for_date(date_str: str, league_id: int) -> List[dict]:
    params = {"include":"participants;state;league","order":"asc","page":1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = j.get("data", []) or []
    meta = j.get("meta") or {}
    last_page = meta.get("last_page", 1)
    for p in range(2, last_page + 1):
        params["page"] = p
        jp = api_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data", []) or [])
    out = []
    for fx in data:
        if fx.get("league_id") != league_id: continue
        if not fx.get("participants"): continue
        out.append(fx)
    return out

def pick_home_away(participants: List[dict]):
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away

def get_team_last_fixture_with_xi(team_id: int, league_id: int) -> Optional[dict]:
    try:
        j = api_get(f"teams/{team_id}", {"include": "latest.league;latest.lineups;latest.lineups.player"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        candidates = [fx for fx in lst if (fx or {}).get("league_id") == league_id]
        candidates.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
        for fx in candidates:
            fid = fx.get("id"); if not fid: continue
            full = api_get(f"fixtures/{fid}", {"include":"lineups;lineups.player"}).get("data", {})
            if any(l.get("type_id")==LINEUP_TYPE_STARTER and l.get("team_id")==team_id for l in (full.get("lineups") or [])):
                full["participants"] = fx.get("participants") or []
                return full
    except Exception:
        pass
    start = today_utc()
    for back in range(1, 181):
        d = (start - dt.timedelta(days=back)).strftime(DATE_FMT)
        try: fxs = get_fixtures_for_date(d, league_id)
        except Exception: continue
        for fx in fxs:
            if any(p.get("id")==team_id for p in (fx.get("participants") or [])):
                full = api_get(f"fixtures/{fx['id']}", {"include":"lineups;lineups.player"}).get("data", {})
                if any(l.get("type_id")==LINEUP_TYPE_STARTER and l.get("team_id")==team_id for l in (full.get("lineups") or [])):
                    full["participants"] = fx.get("participants") or []
                    return full
    return None

SHOT_DEVS_TOTAL = {"SHOTS","SHOTS_TOTAL"}
SHOT_DEVS_SOT   = {"SHOTS_ON_TARGET"}
SHOT_DEVS_SOFF  = {"SHOTS_OFF_TARGET"}
MINUTES_DEVS    = {"MINUTES_PLAYED","MINUTES"}

def _num_from_detail(det: dict) -> int:
    v = (det.get("data") or {}).get("value")
    if isinstance(v, dict):
        if "total" in v:
            try: return int(v["total"] or 0)
            except Exception: return 0
        s = 0
        for x in v.values():
            if isinstance(x, (int,float)): s += int(x)
        return s
    try: return int(v or 0)
    except Exception: return 0

def get_fixture_lineups_minutes_and_shots(fixture_id: int):
    j = api_get(f"fixtures/{fixture_id}", {"include":"lineups.details.type"}).get("data", {})
    lineups = j.get("lineups") or []
    lineups_map, shots_map, minutes_map = {}, {}, {}
    for lp in lineups:
        pid = lp.get("player_id"); if not pid: continue
        pid = int(pid); lineups_map[pid] = lp
        total_from_api = None; sot = soff = 0; mins = None
        for det in (lp.get("details") or []):
            dev = (det.get("type") or {}).get("developer_name","").upper()
            if   dev in SHOT_DEVS_TOTAL: total_from_api = _num_from_detail(det)
            elif dev in SHOT_DEVS_SOT:   sot += _num_from_detail(det)
            elif dev in SHOT_DEVS_SOFF:  soff += _num_from_detail(det)
            elif dev in MINUTES_DEVS:
                mv = _num_from_detail(det); mins = mv if mins is None else max(mins, mv)
        if mins is not None: minutes_map[pid] = mins
        if (total_from_api is not None) or (sot+soff) > 0:
            shots_map[pid] = total_from_api if total_from_api is not None else (sot+soff)
    return lineups_map, shots_map, minutes_map

def get_team_recent_league_fixtures(team_id: int, league_id: int, want: int) -> List[dict]:
    collected, seen = [], set()
    try:
        j = api_get(f"teams/{team_id}", {"include":"latest.league"})
        latest = j.get("data", {}).get("latest")
        lst = latest if isinstance(latest, list) else ([latest] if latest else [])
        for fx in lst:
            if fx and fx.get("league_id")==league_id and fx.get("id") not in seen:
                collected.append(fx); seen.add(fx.get("id"))
    except Exception:
        pass
    today = today_utc()
    for back in range(1, 731):
        d = (today - dt.timedelta(days=back)).strftime(DATE_FMT)
        try: fixtures = get_fixtures_for_date(d, league_id)
        except Exception: continue
        for fx in fixtures:
            fid = fx.get("id"); if not fid or fid in seen: continue
            if any(p.get("id")==team_id for p in (fx.get("participants") or [])):
                collected.append(fx); seen.add(fid)
        if len(collected) >= want*14: break
    collected.sort(key=lambda x: x.get("starting_at") or "", reverse=True)
    return collected

def get_player_last_n_series(team_id: int, player_id: int, n: int, league_id: int) -> List[int]:
    fixtures = get_team_recent_league_fixtures(team_id, league_id, n)
    series: List[Tuple[str,int]] = []
    for fx in fixtures:
        if len(series) >= n: break
        fid = fx.get("id"); if not fid: continue
        try:
            _, shots_map, minutes_map = get_fixture_lineups_minutes_and_shots(fid)
        except Exception:
            continue
        mins = minutes_map.get(int(player_id))
        if mins is None or mins < APPEARANCE_MINUTES_THRESHOLD:
            continue
        shots = shots_map.get(int(player_id), 0)
        series.append((fx.get("starting_at") or "", shots))
    series.sort(key=lambda x: x[0], reverse=True)
    return [s for _, s in series][:n]

def compute_rate(series: List[int], threshold: int) -> float:
    if not series: return 0.0
    hits = sum(1 for x in series if x >= threshold)
    return 100.0 * hits / len(series)

def main():
    if not API_TOKEN:
        print("ERROR: Set SPORTMONKS_TOKEN secret.", file=sys.stderr)
        sys.exit(1)
    if len(sys.argv) < 2:
        print("usage: stats_shots.py LEAGUE_ID", file=sys.stderr)
        sys.exit(2)
    league_id = int(sys.argv[1])

    os.makedirs("data", exist_ok=True)

    start = today_utc()
    end = days_ahead(start, 5)  # next 6 days
    date_list = daterange_str(start, end)

    # 1) fixtures in window
    fixtures = []
    for ds in date_list:
        try:
            fixtures.extend(get_fixtures_for_date(ds, league_id))
        except Exception as e:
            print(f"[WARN] fixtures for {ds}: {e}", file=sys.stderr)

    # 2) collect predicted XIs
    players = {}  # pid -> (lp_row, team_id)
    for fx in fixtures:
        parts = fx.get("participants") or []
        home, away = pick_home_away(parts)
        if not (home and away): continue
        for team in (home, away):
            team_id = team.get("id")
            # prefer official XI
            starters = []
            try:
                full = api_get(f"fixtures/{fx['id']}", {"include":"lineups;lineups.player"}).get("data", {})
                starters = [l for l in (full.get("lineups") or [])
                            if l.get("type_id")==LINEUP_TYPE_STARTER and l.get("team_id")==team_id]
                starters.sort(key=lambda x: x.get("formation_position") or 9999)
                starters = starters[:11]
            except Exception:
                starters = []
            if not starters:
                last = get_team_last_fixture_with_xi(team_id, league_id) or {}
                lps = [l for l in (last.get("lineups") or [])
                       if l.get("team_id")==team_id and l.get("type_id")==LINEUP_TYPE_STARTER]
                lps.sort(key=lambda x: x.get("formation_position") or 9999)
                starters = lps[:11]
            for lp in starters:
                pid = lp.get("player_id")
                if not isinstance(pid, (int, str)): continue
                try: pid = int(pid)
                except: continue
                if pid not in players:
                    players[pid] = (lp, int(team_id))

    # 3) compute series and rates, write JSONL
    out_path = f"data/shots_stats_{league_id}.jsonl"
    cnt = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for pid, (lp, team_id) in players.items():
            pname = (lp.get("player_name") or "").strip()
            jno = lp.get("jersey_number")
            pos_label = pos_id_to_label(lp.get("position_id"))
            s10 = get_player_last_n_series(team_id, pid, 10, league_id)
            s5  = s10[:5] if len(s10) >= 5 else s10
            rec = {
                "league_id": league_id,
                "team_id": team_id,
                "player_id": pid,
                "display": f"#{jno} {pname} (pos={pos_label})",
                "pos": pos_label,
                "apps10": len(s10),
                "hit10_1p": compute_rate(s10, 1),
                "hit10_2p": compute_rate(s10, 2),
                "hit10_3p": compute_rate(s10, 3),
                "apps5": len(s5),
                "hit5_1p": compute_rate(s5, 1),
                "hit5_2p": compute_rate(s5, 2),
                "hit5_3p": compute_rate(s5, 3),
                "series10": s10,
                "series5": s5,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            cnt += 1
    print(f"[OK] wrote {cnt} player rows -> {out_path}")

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
