#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch and cache last-2 head-to-head results for upcoming fixtures.

Writes per pair (order-independent):
  data/h2h/{minId}_{maxId}.json

Schema:
{
  "pair": [home_id, away_id],
  "sorted_key": "min_max",
  "fetched_at": "2025-11-16T20:00:00Z",
  "last2": [
    {
      "fixture_id": 123,
      "starting_at": "YYYY-MM-DD HH:MM:SS",
      "home_id": 111, "away_id": 222,
      "home_goals": 2, "away_goals": 1,
      "o25": true, "btts": true
    }
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

def _parse_score_str(s: str) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(s, str): return None, None
    m = re.search(r"^\s*(\d+)\s*[-:x]\s*(\d+)\s*$", s)
    if not m: return None, None
    return int(m.group(1)), int(m.group(2))

FT_KEYWORDS = {"ft", "fulltime", "full time", "full-time", "regular time", "90"}

def extract_ft_scores(item: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    Be liberal in what we accept:
      • scores as dict with *_score keys
      • scores as list of objects containing 'description' ~ FT and 'score' like '2-1'
      • top-level home_score/away_score fallback
    """
    scores = item.get("scores")

    # Case 1: dict-like scores
    if isinstance(scores, dict):
        for hk, ak in [
            ("localteam_score","visitorteam_score"),
            ("home_score","away_score"),
            ("ft_home_goals","ft_away_goals"),
        ]:
            if (hk in scores) or (ak in scores):
                try:
                    return int(scores.get(hk)), int(scores.get(ak))
                except Exception:
                    pass
        # String FT like "2-1"
        for key in ("ft_score",):
            h, a = _parse_score_str(scores.get(key) or "")
            if h is not None: return h, a

    # Case 2: list-like scores (SportMonks often returns this)
    if isinstance(scores, list):
        # Prefer an element whose description/type looks like FT
        for obj in scores:
            desc = (obj.get("description") or obj.get("type") or "").strip().lower()
            if any(k in desc for k in FT_KEYWORDS):
                h, a = _parse_score_str(obj.get("score") or obj.get("ft_score") or "")
                if h is not None: return h, a
        # Fallback: last element with a parsable "score"
        for obj in reversed(scores):
            h, a = _parse_score_str(obj.get("score") or "")
            if h is not None: return h, a

    # Case 3: top-level fallbacks
    for hk, ak in [("home_score","away_score")]:
        if hk in item or ak in item:
            try:
                return int(item.get(hk)), int(item.get(ak))
            except Exception:
                pass

    # As a last resort, look for a generic ft_score string on item
    h, a = _parse_score_str(item.get("ft_score") or "")
    return (h, a)

def h2h_api(team_a: int, team_b: int) -> List[dict]:
    """
    Pull head-to-head list (most-recent first). We ask for 'scores'
    to increase the odds they're present, but the parser works either way.
    """
    ENDPOINTS = [
        f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_a}/{team_b}",
        f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_b}/{team_a}",
    ]
    params = {
        "api_token": TOKEN,
        "per_page": 10,
        "sort": "-starting_at",
        # Ask for scores if plan allows; safe if ignored
        "include": "scores",
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
        # Defensive: some responses nest H2H under 'data.h2h'
        if isinstance(data, dict) and "h2h" in data:
            data = data.get("h2h") or []
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

    # Build unique team pairs from upcoming fixtures
    pairs: List[Tuple[int,int]] = []
    for lid in lids:
        blob = load_json(FIX_DIR / f"{lid}.json")
        for fx in (blob.get("fixtures") or []):
            hid, aid = extract_team_ids(fx)
            if not (isinstance(hid,int) and isinstance(aid,int)):
                continue
            pairs.append((hid, aid))

    seen = set()
    unique_pairs = []
    for a,b in pairs:
        key = (a,b) if a<b else (b,a)
        if key not in seen:
            seen.add(key)
            unique_pairs.append((a,b))

    updated = 0
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
        updated += 1
        time.sleep(SLEEP)

    print(f"Fetched/updated {updated} H2H caches into {OUT_DIR}")

if __name__ == "__main__":
    main()
