#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch and cache last-2 head-to-head (with scores) for upcoming fixtures.

Writes: data/h2h/{minId}_{maxId}.json
"""

import os, sys, json, time, datetime as dt, re
from pathlib import Path
from typing import List, Tuple, Optional
import requests

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
OUT_DIR = ROOT / "data" / "h2h"; OUT_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))
SLEEP = float(os.getenv("SM_SLEEP", "0.05"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))

if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
    sys.exit(1)

def now_iso(): return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def discover_league_ids() -> List[int]:
    out = []
    for p in FIX_DIR.glob("*.json"):
        try: out.append(int(p.stem))
        except: pass
    return sorted(set(out))

def load_json(p: Path) -> dict:
    try: return json.loads(p.read_text(encoding="utf-8"))
    except Exception: return {}

def extract_team_ids(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    home_id = away_id = None
    for p in (fx.get("participants") or []):
        try: tid = int(p.get("id"))
        except Exception: continue
        loc = ((p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("venue") or "").lower()
        if loc == "home": home_id = tid
        elif loc == "away": away_id = tid
    if home_id is None:
        for k in ("home_team_id","localteam_id","home_id","localteamid"):
            v = fx.get(k); 
            if isinstance(v,(int,str)) and str(v).isdigit(): home_id = int(v); break
    if away_id is None:
        for k in ("away_team_id","visitorteam_id","away_id","visitorteamid"):
            v = fx.get(k); 
            if isinstance(v,(int,str)) and str(v).isdigit(): away_id = int(v); break
    return home_id, away_id

def path_for(a: int, b: int) -> Path:
    lo, hi = (a,b) if a<=b else (b,a)
    return OUT_DIR / f"{lo}_{hi}.json"

def cache_fresh(p: Path) -> bool:
    if not p.exists(): return False
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        ts = j.get("fetched_at"); 
        if not ts: return False
        t = dt.datetime.fromisoformat(ts.replace("Z","+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t) < dt.timedelta(hours=CACHE_HOURS)
    except Exception:
        return False

def write_json(p: Path, obj: dict):
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":"), sort_keys=True), encoding="utf-8")
    tmp.replace(p)

def pick_int(x) -> Optional[int]:
    try: return int(x)
    except Exception: return None

def extract_ft_scores(item: dict) -> Tuple[Optional[int], Optional[int]]:
    s = item.get("scores") or {}
    for hk, ak in [("localteam_score","visitorteam_score"), ("home_score","away_score")]:
        if hk in s or ak in s:
            return pick_int(s.get(hk)), pick_int(s.get(ak))
    ft_str = s.get("ft_score") or item.get("ft_score") or ""
    if isinstance(ft_str, str):
        m = re.match(r"\s*(\d+)\s*[-:x]\s*(\d+)\s*$", ft_str)
        if m: return pick_int(m.group(1)), pick_int(m.group(2))
    if "home_score" in item or "away_score" in item:
        return pick_int(item.get("home_score")), pick_int(item.get("away_score"))
    return None, None

def h2h_api(team_a: int, team_b: int) -> List[dict]:
    url = f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_a}/{team_b}"
    params = {
        "api_token": TOKEN,
        "per_page": 50,
        "sort": "-starting_at",
        "include": "scores",  # <— ensure FT scores are present
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
        if r.status_code != 200: 
            return []
        j = r.json()
    except Exception:
        return []
    data = j.get("data") or []
    data.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
    return data

def main():
    lids = [int(x) for x in os.getenv("LEAGUE_IDS","").split(",") if x.strip()] or discover_league_ids()

    pairs = []
    for lid in lids:
        blob = load_json(FIX_DIR / f"{lid}.json")
        for fx in (blob.get("fixtures") or []):
            hid, aid = extract_team_ids(fx)
            if isinstance(hid,int) and isinstance(aid,int):
                key = tuple(sorted((hid,aid)))
                if key not in pairs: pairs.append(key)

    for a,b in pairs:
        p = path_for(a,b)
        if cache_fresh(p): 
            continue
        lst = h2h_api(a,b)[:2]
        last2 = []
        o25_hits = btts_hits = n = 0
        for it in lst:
            h, aw = extract_ft_scores(it)
            if h is None or aw is None: 
                continue
            n += 1
            o25 = (h + aw) >= 3
            btts = (h > 0 and aw > 0)
            last2.append({
                "fixture_id": it.get("id"),
                "starting_at": it.get("starting_at"),
                "home_id": it.get("localteam_id") or it.get("home_team_id"),
                "away_id": it.get("visitorteam_id") or it.get("away_team_id"),
                "home_goals": h, "away_goals": aw,
                "o25": o25, "btts": btts,
            })
            o25_hits += 1 if o25 else 0
            btts_hits += 1 if btts else 0

        write_json(p, {
            "pair": [a,b],
            "sorted_key": f"{min(a,b)}_{max(a,b)}",
            "fetched_at": now_iso(),
            "last2": last2,
            "summary": {"o25_hits": o25_hits, "o25_n": n, "btts_hits": btts_hits, "btts_n": n}
        })
        time.sleep(SLEEP)

    print(f"Fetched/updated {len(pairs)} H2H caches into {OUT_DIR}")

if __name__ == "__main__":
    main()
