#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch upcoming fixtures (Sportmonks only) for a fixed set of leagues and write:
- JSON per-league: data/fixtures/<league_id>.json
- A verification TXT: data/fixtures/fixtures.txt

Notes:
- Filters strictly by the target league IDs (no cross-league leakage).
- Handles pagination. Retries transient errors.
- Sorts leagues and fixtures deterministically.
- Date window: today .. today + DAYS_AHEAD (env, default 21).
- Requires env var SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN).

Exit codes:
  0 on success
  1 if token missing
  2 on HTTP error (after retries)
"""

import os
import sys
import json
import time
import datetime as dt
from typing import Dict, List, Optional

import requests

# ------------------------- Config -------------------------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

# Read token from either name (both supported)
API_TOKEN = os.getenv("SPORTMONKS_API_TOKEN") or os.getenv("SPORTMONKS_TOKEN")

# Target leagues (Sportmonks IDs) — keep this list as your single source of truth
TARGET_LEAGUES = [8, 9, 384, 387, 82, 301, 564, 567, 600]
TARGET_LEAGUES = sorted(TARGET_LEAGUES)  # ensure deterministic order

# Date window
def _int(x, default):
    try:
        return int(x)
    except Exception:
        return default

DAYS_AHEAD = _int(os.getenv("DAYS_AHEAD"), 21)  # today .. today+N inclusive

# Networking
TIMEOUT = 25
RETRIES = 3
BACKOFF = 1.7

# Output paths
OUT_DIR = os.path.join("data", "fixtures")
TXT_PATH = os.path.join(OUT_DIR, "fixtures.txt")

# ------------------------- Utils -------------------------
def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    d = start
    out = []
    while d <= end_inclusive:
        out.append(d.strftime("%Y-%m-%d"))
        d += dt.timedelta(days=1)
    return out

def ensure_outdir():
    os.makedirs(OUT_DIR, exist_ok=True)

def http_get_json(url: str, params: dict) -> dict:
    """GET with basic retries/backoff, returns parsed JSON or raises."""
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code >= 400:
                # try to show useful context
                try:
                    jerr = r.json()
                except Exception:
                    jerr = {"message": r.text[:300]}
                raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url} :: {jerr}")
            return r.json()
        except Exception as e:
            last_exc = e
            if attempt < RETRIES:
                sleep = BACKOFF ** attempt
                time.sleep(sleep)
            else:
                raise
    raise last_exc  # just in case

def sm_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return http_get_json(url, params)

def get_fixtures_for_date(date_str: str, league_filter: set) -> List[dict]:
    """Fetch fixtures for one date; filter strictly by league_id ∈ league_filter."""
    params = {
        "include": "participants;state;league",
        "order": "asc",
        "page": 1,
    }
    payload = sm_get(f"fixtures/date/{date_str}", params)
    data = (payload.get("data") or [])
    meta = payload.get("meta") or {}
    last_page = int(meta.get("last_page") or 1)

    # pagination
    for p in range(2, last_page + 1):
        params["page"] = p
        payload = sm_get(f"fixtures/date/{date_str}", params)
        data.extend(payload.get("data") or [])

    # strict filter & basic integrity
    out = []
    for fx in data:
        if fx.get("league_id") in league_filter and fx.get("participants"):
            out.append(fx)
    # deterministic sort
    out.sort(key=lambda x: (x.get("league_id"), x.get("starting_at") or "", x.get("id") or 0))
    return out

# ------------------------- Main -------------------------
def main() -> int:
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_API_TOKEN/SPORTMONKS_TOKEN not set.", file=sys.stderr)
        return 1

    ensure_outdir()

    start = today_utc()
    end = start + dt.timedelta(days=DAYS_AHEAD)
    days = daterange_str(start, end)

    league_set = set(TARGET_LEAGUES)

    # Collect fixtures per league
    fixtures_by_league: Dict[int, List[dict]] = {lid: [] for lid in TARGET_LEAGUES}

    total = 0
    for ds in days:
        try:
            fxs = get_fixtures_for_date(ds, league_filter=league_set)
        except Exception as e:
            print(f"[WARN] {ds}: {e}", file=sys.stderr)
            continue
        # bucket by league
        for fx in fxs:
            lid = fx.get("league_id")
            if lid in fixtures_by_league:
                fixtures_by_league[lid].append(fx)
                total += 1

    # Deterministic sorting inside each league
    for lid in TARGET_LEAGUES:
        fixtures_by_league[lid].sort(key=lambda x: (x.get("starting_at") or "", x.get("id") or 0))

    # Write per-league JSONs
    written_files = 0
    for lid in TARGET_LEAGUES:
        path = os.path.join(OUT_DIR, f"{lid}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(fixtures_by_league[lid], f, ensure_ascii=False, indent=2)
        written_files += 1

    # Write verification TXT (simple, grep-friendly)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    txt_lines = [
        f"Time (UTC): {stamp}",
        f"Window    : {start.isoformat()} -> {end.isoformat()}",
        f"Leagues   : {','.join(map(str, TARGET_LEAGUES))}",
        f"Fixtures  : {total} (written {total})",
        "",
        "Per league counts:",
    ]
    for lid in TARGET_LEAGUES:
        txt_lines.append(f"  - {lid}: {len(fixtures_by_league[lid])}")
    with open(TXT_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(txt_lines) + "\n")

    # Console echo (useful in Actions logs)
    print("\n".join(txt_lines))
    print(f"Wrote {written_files} JSON files to {OUT_DIR} and a verification TXT.")

    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except requests.HTTPError as e:
        print(f"\nHTTPError: {e}\n", file=sys.stderr)
        sys.exit(2)
