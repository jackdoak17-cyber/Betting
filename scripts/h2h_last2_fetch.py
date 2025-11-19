#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
scripts/h2h_last2_fetch.py
Builds per-pair H2H caches and per-league bundles from Sportmonks v3.

Input:
  - Upcoming fixtures from: data/fixtures/by_league/<league_id>.json
    (expects .json with {"fixtures": [...]} and each fixture has participants with meta.location home/away)

Output:
  - Pair cache JSONs: data/h2h/<minId>_<maxId>.json
    {
      "pair_key": "18_27",
      "a_id": 18, "b_id": 27,         # a=min, b=max
      "fetched_at": "ISOZ",
      "lastN_meta": [ { "starting_at": "...", "a_goals": int|null, "b_goals": int|null }, ... ],
      "vectors": {
        "a": { "goals":[...], "shots":[...], "sot":[...], "corners":[...],
               "fouls":[...], "offsides":[...], "yellow":[...], "red":[...], "possession":[...] },
        "b": { ... same keys ... }
      }
    }

  - League bundles: data/h2h/by_league/<league_id>.json
    {
      "count": <fixtures in that league file>,
      "fixtures": [
        {
          "fixture_id": ...,
          "starting_at": "...",
          "home_id": ..., "home_name": "...",
          "away_id": ..., "away_name": "...",
          "pair_key": "18_27",
          "cache_present": true/false,
          "fetched_at": "ISOZ|None",
          "lastN_meta": [ { "starting_at": "...", "home_goals": int|null, "away_goals": int|null }, ... ],
          "vectors": {
            "home": { ... sequences ... },
            "away": { ... sequences ... }
          }
        },
        ...
      ]
    }

Environment:
  SPORTMONKS_TOKEN             (required)
  H2H_MATCHES                  (default 5)
  H2H_CACHE_HOURS              (default 24)  # TTL for pair cache
  H2H_FORCE_REFETCH_EMPTY      (0/1, default 0)  # if cached vectors are empty, force refetch
  H2H_OVERWRITE_EMPTY          (0/1, default 0)  # if refetch fails/empty, overwrite old cache or keep it
  H2H_PER_PAGE                 (default 15)  # smaller pages reduce chunked-read issues
  SM_TIMEOUT                   (default 30)
  SM_SLEEP                     (default 0.05)
  SM_HTTP_RETRIES              (default 5)
  SM_REQ_ATTEMPTS              (default 3)
  SM_BACKOFF                   (default 0.7)

Notes:
- Total Shots is computed robustly:
    explicit_total
    > SOT + OffTarget + Blocked
    > Attempts + Blocked
    > Attempts
    > SOT + OffTarget
- Cards prefer events.* types (YellowCard, RedCard, second yellow counted as red).
"""

from __future__ import annotations

import os, sys, re, json, time, math
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import urllib3

# ------------------- Config / Paths -------------------

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)

H2H_MATCHES = int(os.getenv("H2H_MATCHES", "5"))
H2H_CACHE_HOURS = int(os.getenv("H2H_CACHE_HOURS", "24"))
H2H_FORCE_REFETCH_EMPTY = os.getenv("H2H_FORCE_REFETCH_EMPTY", "0") not in {"0", "", "false", "False"}
H2H_OVERWRITE_EMPTY = os.getenv("H2H_OVERWRITE_EMPTY", "0") not in {"0", "", "false", "False"}

H2H_PER_PAGE = int(os.getenv("H2H_PER_PAGE", "15"))
SM_TIMEOUT = int(os.getenv("SM_TIMEOUT", "30"))
SM_SLEEP = float(os.getenv("SM_SLEEP", "0.05"))
SM_HTTP_RETRIES = int(os.getenv("SM_HTTP_RETRIES", "5"))
SM_REQ_ATTEMPTS = int(os.getenv("SM_REQ_ATTEMPTS", "3"))
SM_BACKOFF = float(os.getenv("SM_BACKOFF", "0.7"))

ROOT = Path(".")
FIX_BY_LEAGUE_DIR = ROOT / "data" / "fixtures" / "by_league"
H2H_DIR = ROOT / "data" / "h2h"
H2H_BY_LEAGUE_DIR = H2H_DIR / "by_league"
H2H_DIR.mkdir(parents=True, exist_ok=True)
H2H_BY_LEAGUE_DIR.mkdir(parents=True, exist_ok=True)

# ------------------- HTTP client (resilient) -------------------

def _build_session() -> requests.Session:
    retry = Retry(
        total=SM_HTTP_RETRIES,
        connect=SM_HTTP_RETRIES,
        read=SM_HTTP_RETRIES,
        backoff_factor=SM_BACKOFF,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    s = requests.Session()
    s.headers.update({
        "User-Agent": "h2h-cache/1.0 (+github-actions)",
        "Accept": "application/json",
    })
    adapter = HTTPAdapter(max_retries=retry, pool_maxsize=32)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

_SESSION = _build_session()

class _TransientHTTP(Exception):
    pass

def _get_json_with_retries(url: str, params: dict, attempts: int = SM_REQ_ATTEMPTS) -> dict:
    last = None
    for i in range(1, attempts + 1):
        try:
            r = _SESSION.get(url, params=params, timeout=SM_TIMEOUT)
            if 500 <= r.status_code < 600:
                raise _TransientHTTP(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ChunkedEncodingError,
                requests.exceptions.ConnectionError,
                requests.exceptions.ReadTimeout,
                urllib3.exceptions.ProtocolError,
                urllib3.exceptions.IncompleteRead,
                _TransientHTTP) as e:
            last = e
            time.sleep(min(8, 1.5 * i))
    raise last if last else RuntimeError("HTTP retries exhausted")

# ------------------- IO helpers -------------------

def _read_json(path: Path) -> Any:
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _write_json_atomic(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    tmp.replace(path)

def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _is_fresh(iso_ts: str, ttl_hours: int) -> bool:
    try:
        dt = datetime.strptime(iso_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt < timedelta(hours=ttl_hours)
    except Exception:
        return False

# ------------------- Parsing helpers -------------------

def get_list_or_data(obj: dict, key: str):
    v = obj.get(key)
    if isinstance(v, list): return v
    if isinstance(v, dict) and isinstance(v.get("data"), list): return v["data"]
    return []

def nrm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").strip().lower()).strip()

def to_int_if_numeric(val: Any) -> Optional[int]:
    try:
        s = str(val).strip().rstrip("%")
        if s == "": return None
        f = float(s)
        return int(round(f))
    except Exception:
        return None

def is_final_score_desc(desc: str) -> bool:
    d = (desc or "").upper()
    return d in {"CURRENT", "FT", "FULLTIME", "SECOND_HALF", "2ND_HALF"}

def extract_ft_for_team(scores: List[dict], tid: int) -> Optional[int]:
    val = None
    for s in scores:
        if not is_final_score_desc(s.get("description")):
            continue
        if int(s.get("participant_id") or -1) != tid:
            continue
        g = to_int_if_numeric((s.get("score") or {}).get("goals"))
        if g is not None:
            val = g
    return val

# Stat aliases and robust total shots computation
ALIASES = {
    # total shots (explicit)
    "total shots": "shots_total", "shots total": "shots_total", "shots": "shots_total",
    "total shots (incl blocks)": "shots_total", "shots (total)": "shots_total",

    # on/off target
    "shots on target": "sot", "on target": "sot", "shots on": "sot", "shots on goal": "sot",
    "shots off target": "soff", "off target": "soff", "shots (off target)": "soff",

    # blocked & attempts
    "blocked shots": "sblk", "shots blocked": "sblk", "blocked": "sblk",
    "goal attempts": "attempts", "attempts": "attempts", "attempts on goal": "attempts",
    "attempts at goal": "attempts", "shots attempts": "attempts", "shots attempted": "attempts",

    # other team stats
    "corners": "corners", "corner": "corners", "corner kicks": "corners",
    "fouls": "fouls",
    "offsides": "offsides", "offside": "offsides",
    "possession": "poss", "ball possession": "poss", "ball possession %": "poss", "possession %": "poss",

    # cards (fallback if events missing)
    "yellow cards": "yellow", "yellow": "yellow",
    "red cards": "red", "red": "red",
}

def extract_numeric_from_statdata(d: dict) -> Optional[int]:
    if not isinstance(d, dict):
        return to_int_if_numeric(d)
    for k in ("value", "total", "count", "number"):
        if k in d:
            v = to_int_if_numeric(d.get(k))
            if v is not None: return v
    for v in d.values():
        got = to_int_if_numeric(v)
        if got is not None:
            return got
    return None

def extract_stats_for_team(stats: List[dict], tid: int) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for row in stats:
        try:
            if int(row.get("participant_id") or -1) != tid:
                continue
        except Exception:
            continue
        keyname = nrm((row.get("type") or {}).get("name"))
        normkey = ALIASES.get(keyname)
        if not normkey:
            continue
        val = extract_numeric_from_statdata(row.get("data") or {})
        if val is not None:
            out[normkey] = int(val)

    # Robust total shots rule
    shots_total = out.get("shots_total")
    sot  = out.get("sot")
    soff = out.get("soff")
    sblk = out.get("sblk")
    att  = out.get("attempts")

    if shots_total is not None:
        out["shots"] = shots_total
    elif (sot is not None) or (soff is not None) or (sblk is not None):
        out["shots"] = (sot or 0) + (soff or 0) + (sblk or 0)
    elif (att is not None) and (sblk is not None):
        out["shots"] = att + sblk
    elif att is not None:
        out["shots"] = att
    elif (sot is not None) or (soff is not None):
        out["shots"] = (sot or 0) + (soff or 0)

    return out

def count_cards_from_events(events: List[dict], tid: int) -> Tuple[int, int]:
    y_count = 0
    r_count = 0
    for ev in events:
        try:
            if int(ev.get("participant_id") or -1) != tid:
                continue
        except Exception:
            continue
        tname = nrm((ev.get("type") or {}).get("name"))
        if tname in {"yellowcard", "yellow card"}:
            y_count += 1
        elif tname in {"redcard", "red card"}:
            r_count += 1
        elif tname in {"yellowredcard", "yellow red card", "second yellow"}:
            r_count += 1
    return y_count, r_count

# ------------------- H2H fetch -------------------

def _fetch_h2h_page(a: int, b: int, page: int, per_page: int) -> dict:
    url = f"{API_BASE}/{SPORT}/fixtures/head-to-head/{a}/{b}"
    params = {
        "api_token": TOKEN,
        "include": "participants;scores;statistics.type;events.type",
        "per_page": per_page,
        "sort": "-starting_at",
        "page": page,
    }
    return _get_json_with_retries(url, params, attempts=SM_REQ_ATTEMPTS)

def fetch_h2h(a: int, b: int, want: int) -> List[dict]:
    out: List[dict] = []
    page = 1
    while True:
        try:
            j = _fetch_h2h_page(a, b, page, H2H_PER_PAGE)
        except Exception as e:
            print(f"[WARN] fetch_h2h {a}-{b} page={page} failed: {e}", file=sys.stderr)
            break
        rows = j.get("data") or []
        out.extend(rows)
        if len(out) >= want:
            break
        meta = j.get("meta") or {}
        last_page = meta.get("last_page")
        has_more = (meta.get("pagination") or {}).get("has_more", None)
        if last_page:
            try:
                lp = int(last_page)
            except Exception:
                lp = page
            if page >= lp:
                break
        elif has_more is not None:
            if not has_more:
                break
        else:
            if len(rows) < H2H_PER_PAGE:
                break
        page += 1
        time.sleep(SM_SLEEP)
    out.sort(key=lambda x: (x.get("starting_at") or ""), reverse=True)
    return out[:want]

# ------------------- Pair cache build -------------------

def _pair_key(a: int, b: int) -> Tuple[str, int, int]:
    if a <= b:
        return f"{a}_{b}", a, b
    else:
        return f"{b}_{a}", b, a

def _empty_vectors() -> Dict[str, List[Optional[int]]]:
    keys = ["goals","shots","sot","corners","fouls","offsides","yellow","red","possession"]
    return {k: [] for k in keys}

def build_pair_cache(a: int, b: int, lastN: int, force: bool = False) -> dict:
    """
    Returns the (possibly updated) pair cache dict.
    Respects TTL; refetch if 'force' or TTL expired; if vectors empty and H2H_FORCE_REFETCH_EMPTY, refetch.
    """
    key, A, B = _pair_key(a, b)
    pair_path = H2H_DIR / f"{key}.json"
    now_iso = _iso_utc_now()

    cached = _read_json(pair_path) or {}
    cached_fresh = False
    if cached and isinstance(cached.get("fetched_at"), str):
        cached_fresh = _is_fresh(cached["fetched_at"], H2H_CACHE_HOURS)

    # Check emptiness
    def _is_empty_cache(d: dict) -> bool:
        try:
            va = d.get("vectors", {}).get("a", {})
            vb = d.get("vectors", {}).get("b", {})
            lm = d.get("lastN_meta") or []
            # empty if no lastN_meta or both sides entirely empty
            all_a_empty = all((not va.get(k)) for k in ["goals","shots","sot","corners","fouls","offsides","yellow","red","possession"])
            all_b_empty = all((not vb.get(k)) for k in ["goals","shots","sot","corners","fouls","offsides","yellow","red","possession"])
            return (len(lm) == 0) or (all_a_empty and all_b_empty)
        except Exception:
            return True

    must_refetch = force or (not cached_fresh) or (H2H_FORCE_REFETCH_EMPTY and _is_empty_cache(cached))

    if not must_refetch and cached:
        return cached

    # (Re)fetch
    try:
        items = fetch_h2h(A, B, lastN)
    except Exception as e:
        print(f"[PAIR-ERROR] {A}-{B}: {e}", file=sys.stderr)
        if cached and not H2H_OVERWRITE_EMPTY:
            return cached
        # write an empty-but-stamped cache so TTL logic can move on next time
        payload = {
            "pair_key": key, "a_id": A, "b_id": B,
            "fetched_at": now_iso, "lastN_meta": [],
            "vectors": {"a": _empty_vectors(), "b": _empty_vectors()},
            "error": str(e),
        }
        _write_json_atomic(pair_path, payload)
        return payload

    # Build sequences
    A_goals=[]; B_goals=[]
    A_shots=[]; B_shots=[]
    A_sot=[];   B_sot=[]
    A_corn=[];  B_corn=[]
    A_fouls=[]; B_fouls=[]
    A_offs=[];  B_offs=[]
    A_yel=[];   B_yel=[]
    A_red=[];   B_red=[]
    A_poss=[];  B_poss=[]
    last_meta=[]

    def _push(lst: List[Optional[int]], val: Optional[int]):
        lst.append(None if val is None else int(val))

    for it in items:
        scores = get_list_or_data(it, "scores")
        stats  = get_list_or_data(it, "statistics")
        events = get_list_or_data(it, "events")

        Ag = extract_ft_for_team(scores, A)
        Bg = extract_ft_for_team(scores, B)

        Astat = extract_stats_for_team(stats, A)
        Bstat = extract_stats_for_team(stats, B)

        Ay, Ar = count_cards_from_events(events, A)
        By, Br = count_cards_from_events(events, B)
        if Ay == 0 and Ar == 0 and (Astat.get("yellow") or Astat.get("red")):
            Ay = int(Astat.get("yellow") or 0); Ar = int(Astat.get("red") or 0)
        if By == 0 and Br == 0 and (Bstat.get("yellow") or Bstat.get("red")):
            By = int(Bstat.get("yellow") or 0); Br = int(Bstat.get("red") or 0)

        _push(A_goals, Ag); _push(B_goals, Bg)
        _push(A_shots, Astat.get("shots"));   _push(B_shots, Bstat.get("shots"))
        _push(A_sot,   Astat.get("sot"));     _push(B_sot,   Bstat.get("sot"))
        _push(A_corn,  Astat.get("corners")); _push(B_corn,  Bstat.get("corners"))
        _push(A_fouls, Astat.get("fouls"));   _push(B_fouls, Bstat.get("fouls"))
        _push(A_offs,  Astat.get("offsides"));_push(B_offs,  Bstat.get("offsides"))
        _push(A_yel,   Ay);                   _push(B_yel,   By)
        _push(A_red,   Ar);                   _push(B_red,   Br)
        _push(A_poss,  Astat.get("poss"));    _push(B_poss,  Bstat.get("poss"))

        last_meta.append({
            "starting_at": (it.get("starting_at") or "")[:19],
            "a_goals": Ag,
            "b_goals": Bg,
        })

    payload = {
        "pair_key": key,
        "a_id": A,
        "b_id": B,
        "fetched_at": _iso_utc_now(),
        "lastN_meta": last_meta,
        "vectors": {
            "a": {
                "goals": A_goals, "shots": A_shots, "sot": A_sot, "corners": A_corn,
                "fouls": A_fouls, "offsides": A_offs, "yellow": A_yel, "red": A_red, "possession": A_poss,
            },
            "b": {
                "goals": B_goals, "shots": B_shots, "sot": B_sot, "corners": B_corn,
                "fouls": B_fouls, "offsides": B_offs, "yellow": B_yel, "red": B_red, "possession": B_poss,
            },
        }
    }

    # If refetch returned empty and we prefer not to overwrite, keep old
    if (len(last_meta) == 0) and cached and not H2H_OVERWRITE_EMPTY:
        return cached

    _write_json_atomic(pair_path, payload)
    return payload

# ------------------- Fixtures & League Bundles -------------------

def _participants_to_ids_names(participants: List[dict]) -> Tuple[Optional[int], Optional[str], Optional[int], Optional[str]]:
    hid = hname = aid = aname = None
    for p in participants or []:
        meta = p.get("meta") or {}
        loc = (meta.get("location") or "").lower()
        if loc == "home":
            hid = int(p.get("id") or 0) or None
            hname = p.get("name")
        elif loc == "away":
            aid = int(p.get("id") or 0) or None
            aname = p.get("name")
    return hid, hname, aid, aname

def _load_fixtures_from_league(path: Path) -> List[dict]:
    j = _read_json(path) or {}
    rows = j.get("fixtures") or []
    out = []
    for fx in rows:
        hid, hname, aid, aname = _participants_to_ids_names(fx.get("participants") or [])
        if not (hid and aid):
            continue
        out.append({
            "fixture_id": int(fx.get("id")),
            "league_id": int(fx.get("league_id") or 0),
            "starting_at": fx.get("starting_at"),
            "home_id": hid, "home_name": hname,
            "away_id": aid, "away_name": aname,
        })
    return out

def _map_pair_to_home_away(pair_cache: dict, home_id: int, away_id: int):
    """
    Re-orient a/b vectors to home/away vectors based on fixture teams.
    """
    A = int(pair_cache.get("a_id"))
    B = int(pair_cache.get("b_id"))
    vec = pair_cache.get("vectors") or {}
    va = vec.get("a") or _empty_vectors()
    vb = vec.get("b") or _empty_vectors()

    if home_id == A and away_id == B:
        home_vec, away_vec = va, vb
        hm_key_goals, aw_key_goals = "a_goals", "b_goals"
    elif home_id == B and away_id == A:
        home_vec, away_vec = vb, va
        hm_key_goals, aw_key_goals = "b_goals", "a_goals"
    else:
        # Shouldn't happen, but fallback to empty
        home_vec, away_vec = _empty_vectors(), _empty_vectors()
        hm_key_goals, aw_key_goals = "a_goals", "b_goals"

    # translate lastN_meta goals to home/away orientation
    last_meta_src = pair_cache.get("lastN_meta") or []
    last_meta_ha = []
    for row in last_meta_src:
        last_meta_ha.append({
            "starting_at": row.get("starting_at"),
            "home_goals": row.get(hm_key_goals),
            "away_goals": row.get(aw_key_goals),
        })

    return home_vec, away_vec, last_meta_ha

# ------------------- Main -------------------

def main():
    if not TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    # 1) Discover leagues & fixtures
    league_files = sorted(FIX_BY_LEAGUE_DIR.glob("*.json"))
    all_fixtures: Dict[int, List[dict]] = {}
    pair_set: set[Tuple[int,int]] = set()

    for lf in league_files:
        fixtures = _load_fixtures_from_league(lf)
        if not fixtures:
            continue
        lid = int(lf.stem)
        all_fixtures[lid] = fixtures
        for fx in fixtures:
            a, b = fx["home_id"], fx["away_id"]
            key = _pair_key(a, b)[0]
            # store normalized pair (min,max)
            _, A, B = _pair_key(a, b)
            pair_set.add((A, B))

    print(f"Pairs discovered: {len(pair_set)}")

    # 2) Ensure pair caches
    updated = 0
    for (A, B) in sorted(pair_set):
        before = _read_json(H2H_DIR / f"{A}_{B}.json")
        res = build_pair_cache(A, B, H2H_MATCHES, force=False)
        after = _read_json(H2H_DIR / f"{A}_{B}.json")
        if json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True):
            updated += 1
        time.sleep(SM_SLEEP)

    print(f"Updated caches: {updated}/{len(pair_set)} (TTL={H2H_CACHE_HOURS}h, N={H2H_MATCHES})")

    # 3) Write league bundles
    bundles = 0
    for lid, fixtures in all_fixtures.items():
        out_fixtures = []
        for fx in fixtures:
            home_id = fx["home_id"]; away_id = fx["away_id"]
            pair_key, A, B = _pair_key(home_id, away_id)
            pair_path = H2H_DIR / f"{pair_key}.json"
            cached = _read_json(pair_path) or {}
            cache_present = bool(cached)
            fetched_at = cached.get("fetched_at")

            if not cached or (H2H_FORCE_REFETCH_EMPTY and (
                not cached.get("lastN_meta") or
                all(not (cached.get("vectors") or {}).get(side, {}).get(k)
                    for side in ("a","b")
                    for k in ["goals","shots","sot","corners","fouls","offsides","yellow","red","possession"])
            )):
                # try once more to fetch just for this fixture's bundle
                cached = build_pair_cache(home_id, away_id, H2H_MATCHES, force=True)
                cache_present = bool(cached)
                fetched_at = cached.get("fetched_at")

            home_vec, away_vec, last_meta_ha = _map_pair_to_home_away(cached or {}, home_id, away_id)

            out_fixtures.append({
                "fixture_id": fx["fixture_id"],
                "starting_at": fx["starting_at"],
                "home_id": home_id,
                "home_name": fx["home_name"],
                "away_id": away_id,
                "away_name": fx["away_name"],
                "pair_key": pair_key,
                "cache_present": cache_present,
                "fetched_at": fetched_at,
                "lastN_meta": last_meta_ha,
                "vectors": {
                    "home": home_vec,
                    "away": away_vec
                }
            })

        payload = {
            "count": len(out_fixtures),
            "fixtures": out_fixtures
        }
        _write_json_atomic(H2H_BY_LEAGUE_DIR / f"{lid}.json", payload)
        bundles += 1

    print(f"Wrote league bundles: {bundles} -> data/h2h/by_league/<league_id>.json")

if __name__ == "__main__":
    main()