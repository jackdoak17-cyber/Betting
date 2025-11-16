#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch and cache last-2 head-to-head results for upcoming fixtures.

Writes one file per pair (order-independent):
  data/h2h/{minId}_{maxId}.json

Schema:
{
  "pair": [home_id, away_id],             # unsorted, as in the fixture
  "sorted_key": "min_max",
  "fetched_at": "2025-11-16T20:00:00Z",
  "last2": [
    {
      "fixture_id": 123,
      "starting_at": "YYYY-MM-DD HH:MM:SS",
      "home_id": 111, "away_id": 222,
      "home_goals": 2, "away_goals": 1,
      "o25": true, "btts": true
    },
    ...
  ],
  "summary": { "o25_hits": 1, "o25_n": 2, "btts_hits": 2, "btts_n": 2 }
}

Env:
  SPORTMONKS_TOKEN (required)
  H2H_CACHE_HOURS (default 24)
  LEAGUE_IDS       (optional CSV; default = discover from data/fixtures/*.json)
"""

import os, sys, json, time, datetime as dt, re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

ROOT = Path(".")
FIX_DIR  = ROOT / "data" / "fixtures"
OUT_DIR  = ROOT / "data" / "h2h"; OUT_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))

SLEEP = float(os.getenv("SM_SLEEP", "0.05"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))

if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
    sys.exit(1)

def now_utc_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def parse_dt(s: str) -> Optional[dt.datetime]:
    try:
        return dt.datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return None

def discover_league_ids() -> List[int]:
    out = []
    for p in FIX_DIR.glob("*.json"):
        try: out.append(int(p.stem))
        except: pass
    return sorted(set(out))

def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def extract_team_ids(fx: dict) -> Tuple[Optional[int], Optional[int]]:
    # Prefer participants meta
    for p in fx.get("participants") or []:
        pass
    home_id = away_id = None
    parts = fx.get("participants") or []
    for p in parts:
        try:
            tid = int(p.get("id"))
        except Exception:
            continue
        loc = ((p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("venue") or "").lower()
        if loc == "home":
            home_id = tid
        elif loc == "away":
            away_id = tid
    # Fallback common keys
    if home_id is None:
        for k in ("home_team_id","localteam_id","home_id","localteamid"):
            v = fx.get(k)
            if isinstance(v,(int,str)) and str(v).isdigit():
                home_id = int(v); break
    if away_id is None:
        for k in ("away_team_id","visitorteam_id","away_id","visitorteamid"):
            v = fx.get(k)
            if isinstance(v,(int,str)) and str(v).isdigit():
                away_id = int(v); break
    return home_id, away_id

def h2h_cache_path(a: int, b: int) -> Path:
    lo, hi = (a,b) if a <= b else (b,a)
    return OUT_DIR / f"{lo}_{hi}.json"

def cache_is_fresh(p: Path) -> bool:
    if not p.exists(): return False
    try:
        j = json.loads(p.read_text(encoding="utf-8"))
        ts = j.get("fetched_at")
        if not ts: return False
        t = dt.datetime.fromisoformat(ts.replace("Z","+00:00"))
        return (dt.datetime.now(dt.timezone.utc) - t) < dt.timedelta(hours=CACHE_HOURS)
    except Exception:
        return False

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)

def pick_int(x) -> Optional[int]:
    try:
        return int(x)
    except Exception:
        return None

def extract_ft_scores(item: dict) -> Tuple[Optional[int], Optional[int]]:
    # Try common shapes
    s = item.get("scores") or {}
    for hk, ak in [
        ("localteam_score","visitorteam_score"),
        ("home_score","away_score"),
        ("ft_home_goals","ft_away_goals"),
    ]:
        if hk in s or ak in s:
            return pick_int(s.get(hk)), pick_int(s.get(ak))
    # Some payloads store FT as "2-1"
    ft_str = s.get("ft_score") or item.get("ft_score") or ""
    if isinstance(ft_str, str):
        m = re.match(r"\s*(\d+)\s*[-:x]\s*(\d+)\s*$", ft_str)
        if m: 
            return pick_int(m.group(1)), pick_int(m.group(2))
    # Last resorts
    if "home_score" in item or "away_score" in item:
        return pick_int(item.get("home_score")), pick_int(item.get("away_score"))
    return None, None

def h2h_api(team_a: int, team_b: int) -> List[dict]:
    """
    Try a couple of likely endpoints; return list of fixture dicts (most-recent first).
    Adjust ENDPOINTS if your plan differs; this keeps the value flags script API-free.
    """
    ENDPOINTS = [
        f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_a}/{team_b}",
        f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_b}/{team_a}",
    ]
    params = {
        "api_token": TOKEN,
        "per_page": 10,
        "sort": "-starting_at",
        # include scores/participants if your plan supports it; safe to leave lean
        # "include": "scores,participants"
    }
    for url in ENDPOINTS:
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        try:
            j = r.json()
        except Exception:
            continue
        data = j.get("data") or []
        # Normalize sort descending by starting_at
        data.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
        if data:
            return data
        time.sleep(SLEEP)
    return []

def main():
    if os.getenv("LEAGUE_IDS"):
        lids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        lids = discover_league_ids()

    pairs: List[Tuple[int,int]] = []

    for lid in lids:
        blob = load_json(FIX_DIR / f"{lid}.json")
        for fx in (blob.get("fixtures") or []):
            hid, aid = extract_team_ids(fx)
            if not (isinstance(hid,int) and isinstance(aid,int)): 
                continue
            pairs.append((hid, aid))

    # unique by sorted pair
    seen = set()
    unique_pairs = []
    for a,b in pairs:
        key = (a,b) if a<b else (b,a)
        if key not in seen:
            seen.add(key)
            unique_pairs.append((a,b))

    for a,b in unique_pairs:
        outp = h2h_cache_path(a,b)
        if cache_is_fresh(outp):
            continue

        lst = h2h_api(a,b)[:2]  # last 2
        last2 = []
        o25_hits = 0
        btts_hits = 0
        n = 0

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
                "home_id": it.get("localteam_id") or it.get("home_team_id") or None,
                "away_id": it.get("visitorteam_id") or it.get("away_team_id") or None,
                "home_goals": h,
                "away_goals": aw,
                "o25": o25,
                "btts": btts,
            })
            o25_hits += 1 if o25 else 0
            btts_hits += 1 if btts else 0

        payload = {
            "pair": [a,b],
            "sorted_key": f"{min(a,b)}_{max(a,b)}",
            "fetched_at": now_utc_iso(),
            "last2": last2,
            "summary": {
                "o25_hits": o25_hits, "o25_n": n,
                "btts_hits": btts_hits, "btts_n": n
            }
        }
        write_json(outp, payload)
        time.sleep(SLEEP)

    print(f"Fetched/updated {len(unique_pairs)} H2H caches into {OUT_DIR}")

if __name__ == "__main__":
    main()
