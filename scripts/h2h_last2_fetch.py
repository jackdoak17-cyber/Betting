#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch and cache last-N head-to-head results for *upcoming fixtures only*.

- Discovers pairs from data/fixtures/*.json
- Calls: GET /v3/football/fixtures/head-to-head/{A}/{B}?include=scores
- Stores *order-independent* caches at: data/h2h/{minId}_{maxId}.json
- Default N = 5 (override with env H2H_MATCHES)

Cache TTL:
- Skip re-fetch if file is newer than H2H_CACHE_HOURS (default 24)

Output schema (example):
{
  "pair": [home_id, away_id],             # unsorted, as found in the upcoming fixture
  "sorted_key": "min_max",
  "fetched_at": "2025-11-16T20:00:00Z",
  "match_count": 5,
  "teams": {
    "home_id": 123, "home_name": "Team A",
    "away_id": 456, "away_name": "Team B"
  },
  "lastN": [
    {
      "fixture_id": 18535605,
      "starting_at": "YYYY-MM-DD HH:MM:SS",
      "home_id": 123, "home_name": "Team A",
      "away_id": 456, "away_name": "Team B",
      "home_goals": 2, "away_goals": 2,
      "o25": true, "btts": true
    },
    ...
  ],
  "summary": {
    "o25_hits": 3, "o25_n": 5,
    "btts_hits": 4, "btts_n": 5
  },
  "last2_summary": {
    "o25_hits": 1, "o25_n": 2,
    "btts_hits": 2, "btts_n": 2
  }
}

Env:
  SPORTMONKS_TOKEN  (required)
  H2H_CACHE_HOURS   (default 24)
  H2H_MATCHES       (default 5)
  LEAGUE_IDS        (optional CSV; default discovers from data/fixtures/*.json)
  SM_SLEEP          (default 0.05)
  SM_TIMEOUT        (default 20)
"""

import os, sys, json, time, datetime as dt, re
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

# ---------- Config ----------
ROOT = Path(".")
FIX_DIR  = ROOT / "data" / "fixtures"
OUT_DIR  = ROOT / "data" / "h2h"; OUT_DIR.mkdir(parents=True, exist_ok=True)

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))
H2H_MATCHES = int(os.getenv("H2H_MATCHES", "5"))
SLEEP = float(os.getenv("SM_SLEEP", "0.05"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))

if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
    sys.exit(1)

# ---------- Utils ----------
def now_utc_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00","Z")

def load_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, separators=(",",":"), sort_keys=True), encoding="utf-8")
    tmp.replace(path)

def discover_league_ids() -> List[int]:
    out = []
    for p in FIX_DIR.glob("*.json"):
        try: out.append(int(p.stem))
        except: pass
    return sorted(set(out))

def extract_team_ids_and_names(fx: dict) -> Tuple[Optional[int], Optional[int], Optional[str], Optional[str]]:
    """
    Pull team IDs from fixture. Prefer participants meta (with location home/away).
    Return (home_id, away_id, home_name, away_name)
    """
    home_id = away_id = None
    home_name = away_name = None

    parts = fx.get("participants") or []
    for p in parts:
        try:
            tid = int(p.get("id"))
        except Exception:
            continue
        loc = ((p.get("meta") or {}).get("location") or (p.get("meta") or {}).get("venue") or "").lower()
        nm = (p.get("name") or p.get("short_code") or p.get("display_name") or None)
        if loc == "home":
            home_id, home_name = tid, nm
        elif loc == "away":
            away_id, away_name = tid, nm

    # Fallback IDs
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

    return home_id, away_id, home_name, away_name

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

def _parse_score_str(s: str) -> Tuple[Optional[int], Optional[int]]:
    if not isinstance(s, str): return None, None
    m = re.search(r"^\s*(\d+)\s*[-:x]\s*(\d+)\s*$", s)
    if not m: return None, None
    return int(m.group(1)), int(m.group(2))

# Descriptions to *exclude* from our 90-min tally
EXCLUDE_DESC = {"penalty", "penalties", "penalty_shootout", "shootout", "ps", "psos", "pen"}
# Accept only regular-time-ish segments; if description absent, we accept it
ALLOW_DESC = {"1st_half","first_half","2nd_half","second_half","regular_time","full_time","ft","90","full time","full-time"}

def extract_ft_scores(item: dict) -> Tuple[Optional[int], Optional[int]]:
    """
    Robust FT extractor:
      1) use dict-like scores: *_score or ft_score
      2) use list-like scores with {score:{goals,participant}}; sum only regular-time segments
      3) fallback to top-level *_score lookups
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
        h, a = _parse_score_str(scores.get("ft_score") or "")
        if h is not None: return h, a

    # Case 2: list-like scores (common in v3 when include=scores)
    if isinstance(scores, list):
        home_g = away_g = 0
        for obj in scores:
            desc = str(obj.get("description") or obj.get("type") or "").strip().lower()
            # If description is present and obviously about penalties/shootout, skip
            if any(bad in desc for bad in EXCLUDE_DESC):
                continue
            if desc and not any(ok in desc for ok in ALLOW_DESC):
                # Unknown segment; ignore rather than risk double-counting
                continue

            sc = obj.get("score")
            if isinstance(sc, dict):
                g = sc.get("goals")
                part = str(sc.get("participant") or "").lower()
                try:
                    g = int(g)
                except Exception:
                    continue
                if part in {"home", "localteam"}:
                    home_g += g
                elif part in {"away", "visitorteam"}:
                    away_g += g
        # If we collected anything, return it
        if (home_g + away_g) > 0:
            return home_g, away_g

        # Sometimes list items carry a string "score": "2-1"
        for obj in reversed(scores):
            h, a = _parse_score_str(obj.get("score") or obj.get("ft_score") or "")
            if h is not None:
                return h, a

    # Case 3: top-level fallbacks
    for hk, ak in [("home_score","away_score"),
                   ("localteam_score","visitorteam_score")]:
        if hk in item or ak in item:
            try:
                return int(item.get(hk)), int(item.get(ak))
            except Exception:
                pass

    # Last resort: an ft_score string on item
    h, a = _parse_score_str(item.get("ft_score") or "")
    return (h, a)

def h2h_api(team_a: int, team_b: int, n: int) -> List[dict]:
    """
    Pull head-to-head list (most-recent first).
    We request include=scores for robust FT parsing.
    """
    url = f"{API_BASE}/{SPORT}/fixtures/head-to-head/{team_a}/{team_b}"
    params = {
        "api_token": TOKEN,
        "per_page": max(10, n),   # request at least n (use 10 to be safe)
        "sort": "-starting_at",
        "include": "scores,participants"
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"WARN: request error for {team_a}-{team_b}: {e}")
        return []
    if r.status_code != 200:
        print(f"WARN: HTTP {r.status_code} for {team_a}-{team_b}: {r.text[:160]}")
        return []
    try:
        j = r.json()
    except Exception:
        return []
    data = j.get("data") or []
    # Some payloads nest under 'data.h2h'
    if isinstance(data, dict) and "h2h" in data:
        data = data.get("h2h") or []
    data.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
    return data[:n]

def main():
    # Discover league IDs to read upcoming fixtures from
    if os.getenv("LEAGUE_IDS"):
        lids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        lids = discover_league_ids()

    # Build unique H/A pairs (order-independent) from upcoming fixtures
    pairs = []
    id_to_name: Dict[int, str] = {}

    for lid in lids:
        blob = load_json(FIX_DIR / f"{lid}.json")
        for fx in (blob.get("fixtures") or []):
            hid, aid, hname, aname = extract_team_ids_and_names(fx)
            if not (isinstance(hid,int) and isinstance(aid,int)): 
                continue
            pairs.append((hid, aid))
            if hname: id_to_name.setdefault(hid, hname)
            if aname: id_to_name.setdefault(aid, aname)

    # Unique sorted pairs
    seen = set()
    uniq = []
    for a,b in pairs:
        key = (a,b) if a<b else (b,a)
        if key not in seen:
            seen.add(key)
            uniq.append((a,b))

    updated = 0
    for a,b in uniq:
        outp = h2h_cache_path(a,b)
        if cache_is_fresh(outp):
            continue

        lst = h2h_api(a,b, H2H_MATCHES)
        lastN = []
        o25_hits = btts_hits = n = 0

        for it in lst:
            h, aw = extract_ft_scores(it)
            if h is None or aw is None:
                continue
            n += 1
            o25 = (h + aw) >= 3
            btts = (h > 0 and aw > 0)

            # Try names for readability (participants if present)
            home_id = it.get("localteam_id") or it.get("home_team_id")
            away_id = it.get("visitorteam_id") or it.get("away_team_id")
            home_name = None
            away_name = None
            for p in (it.get("participants") or []):
                loc = ((p.get("meta") or {}).get("location") or "").lower()
                if loc == "home":
                    home_name = p.get("name") or p.get("short_code") or home_name
                elif loc == "away":
                    away_name = p.get("name") or p.get("short_code") or away_name

            # Fallback to global mapping if still missing
            if isinstance(home_id, int):
                home_name = home_name or id_to_name.get(home_id)
            if isinstance(away_id, int):
                away_name = away_name or id_to_name.get(away_id)

            lastN.append({
                "fixture_id": it.get("id"),
                "starting_at": it.get("starting_at"),
                "home_id": home_id, "home_name": home_name,
                "away_id": away_id, "away_name": away_name,
                "home_goals": h, "away_goals": aw,
                "o25": bool(o25), "btts": bool(btts),
            })
            o25_hits += 1 if o25 else 0
            btts_hits += 1 if btts else 0

        # Also compute "last2" summary for quick display elsewhere
        o25_2 = btts_2 = n2 = 0
        for it in lastN[:2]:
            n2 += 1
            o25_2 += 1 if it["o25"] else 0
            btts_2 += 1 if it["btts"] else 0

        payload = {
            "pair": [a,b],
            "sorted_key": f"{min(a,b)}_{max(a,b)}",
            "fetched_at": now_utc_iso(),
            "match_count": len(lastN),
            "teams": {
                "home_id": a, "home_name": id_to_name.get(a),
                "away_id": b, "away_name": id_to_name.get(b),
            },
            "lastN": lastN,
            "summary": {
                "o25_hits": o25_hits, "o25_n": n,
                "btts_hits": btts_hits, "btts_n": n
            },
            "last2_summary": {
                "o25_hits": o25_2, "o25_n": n2,
                "btts_hits": btts_2, "btts_n": n2
            }
        }
        write_json(outp, payload)
        updated += 1
        time.sleep(SLEEP)

    print(f"Pairs: {len(uniq)} | Updated caches: {updated} (TTL={CACHE_HOURS}h, N={H2H_MATCHES})")

if __name__ == "__main__":
    main()
