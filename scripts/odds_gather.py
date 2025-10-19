#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sportmonks odds gatherer (v3)

Reads upcoming fixtures from: data/fixtures/{league_id}.json
Fetches ALL available pre-match odds for each fixture via:
  GET /v3/football/odds/pre-match/fixtures/{fixture_id}

Writes:
  data/odds/latest.json
  data/odds/{league_id}.json
  data/odds/by_league/{league_id}.json
  data/odds/fixtures/{fixture_id}.json
  data/odds/odds.txt

Env:
  SPORTMONKS_TOKEN      (required)
  LEAGUE_IDS            (optional, CSV; defaults to the 9 leagues you listed)
  SM_ODDS_MARKETS       (optional, CSV market IDs; passes filters=markets:...)
  SM_ODDS_BOOKMAKERS    (optional, CSV bookmaker IDs; passes filters=bookmakers:...)
  SM_ODDS_PER_PAGE      (optional, default 50)
  SM_ODDS_SLEEP_MS      (optional, default 300)  # polite delay between requests
  SM_ODDS_RETRIES       (optional, default 2)
"""

import os
import sys
import json
import time
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
TIMEOUT = 25

# --- ENV / config -------------------------------------------------------------

API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)

DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]

LEAGUE_IDS = [
    int(x)
    for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",")
    if x.strip()
]

MARKETS_FILTER = os.getenv("SM_ODDS_MARKETS")  # e.g. "1,2,12"
BOOKMAKERS_FILTER = os.getenv("SM_ODDS_BOOKMAKERS")  # e.g. "2,14"
PER_PAGE = int(os.getenv("SM_ODDS_PER_PAGE", "50"))
SLEEP_MS = int(os.getenv("SM_ODDS_SLEEP_MS", "300"))
RETRIES = int(os.getenv("SM_ODDS_RETRIES", "2"))

# --- IO paths -----------------------------------------------------------------

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
OUT_DIR = ROOT / "data" / "odds"
BY_LEAGUE_DIR = OUT_DIR / "by_league"
PER_FIXTURE_DIR = OUT_DIR / "fixtures"

# --- helpers ------------------------------------------------------------------

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tmp.replace(path)

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)

def api_get(path: str, params: Optional[dict] = None, timeout=TIMEOUT) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                # rate limited — backoff a bit
                time.sleep(1.0 + attempt * 0.5)
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            if attempt < RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except requests.HTTPError:
            # propagate 4xx/5xx with message
            raise
    # unreachable
    return {}

def read_pagination_meta(meta: dict) -> Tuple[Optional[int], Optional[bool]]:
    """
    Returns (pages, has_more). Handles both shapes seen in v3:
      meta = {"last_page": N, ...}
      meta = {"pagination": {"total_pages": N, "has_more": bool}, ...}
      (sometimes has_more also appears top-level)
    """
    if not isinstance(meta, dict):
        return (None, None)
    if "last_page" in meta and meta.get("last_page"):
        try:
            return (int(meta["last_page"]), None)
        except Exception:
            pass
    pag = meta.get("pagination") or {}
    pages = None
    if "total_pages" in pag and pag.get("total_pages"):
        try:
            pages = int(pag["total_pages"])
        except Exception:
            pages = None
    has_more = None
    if "has_more" in meta:
        has_more = bool(meta.get("has_more"))
    elif "has_more" in pag:
        has_more = bool(pag.get("has_more"))
    return (pages, has_more)

# --- odds fetchers ------------------------------------------------------------

def odds_params_extra() -> dict:
    filters = []
    if MARKETS_FILTER:
        filters.append(f"markets:{MARKETS_FILTER}")
    if BOOKMAKERS_FILTER:
        filters.append(f"bookmakers:{BOOKMAKERS_FILTER}")
    params = {
        "per_page": PER_PAGE,
        "order": "asc",
    }
    if filters:
        params["filters"] = ";".join(filters)
    return params

def fetch_prematch_odds_for_fixture(fixture_id: int) -> List[dict]:
    """
    GET /v3/football/odds/pre-match/fixtures/{fixture_id}
    Robust pagination, returns the concatenated 'data' rows.
    """
    base = f"odds/pre-match/fixtures/{fixture_id}"
    page = 1
    out: List[dict] = []
    while True:
        params = {**odds_params_extra(), "page": page}
        j = api_get(base, params)
        data = j.get("data") or []
        meta = j.get("meta") or {}
        out.extend(data)
        pages, has_more = read_pagination_meta(meta)
        # print(f"[odds {fixture_id}] page={page} got={len(data)} total={len(out)} pages={pages} has_more={has_more}")
        if pages:
            if page >= pages:
                break
            page += 1
        elif has_more is not None:
            if not has_more:
                break
            page += 1
        else:
            if len(data) < PER_PAGE:
                break
            page += 1
        # polite delay
        if SLEEP_MS > 0:
            time.sleep(SLEEP_MS / 1000.0)
    return out

# --- fixtures IO --------------------------------------------------------------

def load_fixtures_for_league(league_id: int) -> Dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.exists():
        return {"league_id": league_id, "fixtures": [], "league_name": str(league_id)}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

# --- main ---------------------------------------------------------------------

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    leagues_summary = []
    total_fixtures = 0
    total_odds_rows = 0

    for lid in LEAGUE_IDS:
        fx_payload = load_fixtures_for_league(lid)
        fixtures = fx_payload.get("fixtures") or []
        league_name = fx_payload.get("league_name") or str(lid)
        window_start = fx_payload.get("window_start")
        window_end = fx_payload.get("window_end")

        league_rows = []
        for fx in fixtures:
            fid = int(fx.get("id"))
            name = fx.get("name")
            starting_at = fx.get("starting_at")
            # fetch odds
            try:
                odds_rows = fetch_prematch_odds_for_fixture(fid)
            except requests.HTTPError as e:
                # store minimal error record to aid debugging
                odds_rows = []
                print(f"[WARN] league {lid} fixture {fid} odds fetch failed: {e}", file=sys.stderr)
            # persist per-fixture copy (useful for caching / diffs)
            write_json(PER_FIXTURE_DIR / f"{fid}.json", {
                "fixture_id": fid,
                "fetched_at": generated_at,
                "odds": odds_rows,
            })
            league_rows.append({
                "fixture_id": fid,
                "name": name,
                "starting_at": starting_at,
                "league_id": lid,
                "odds": odds_rows,
            })
            total_fixtures += 1
            total_odds_rows += len(odds_rows)

            if SLEEP_MS > 0:
                time.sleep(SLEEP_MS / 1000.0)

        league_payload = {
            "league_id": lid,
            "league_name": league_name,
            "generated_at": generated_at,
            "window_start": window_start,
            "window_end": window_end,
            "fixture_count": len(fixtures),
            "fixtures_with_odds": len([x for x in league_rows if x.get("odds")]),
            "odds_row_count": sum(len(x.get("odds") or []) for x in league_rows),
            "fixtures": league_rows,
        }
        write_json(OUT_DIR / f"{lid}.json", league_payload)
        write_json(BY_LEAGUE_DIR / f"{lid}.json", league_payload)

        leagues_summary.append({
            "league_id": lid,
            "league_name": league_name,
            "fixtures": len(fixtures),
            "fixtures_with_odds": league_payload["fixtures_with_odds"],
            "odds_rows": league_payload["odds_row_count"],
        })

    latest = {
        "generated_at": generated_at,
        "league_ids": LEAGUE_IDS,
        "total_fixtures_seen": total_fixtures,
        "total_odds_rows": total_odds_rows,
        "leagues": leagues_summary,
    }
    write_json(OUT_DIR / "latest.json", latest)

    # human-readable summary
    lines = [
        f"Time (UTC): {generated_at}",
        f"Leagues   : {','.join(map(str, LEAGUE_IDS))}",
        f"Fixtures  : {total_fixtures}",
        f"Odds rows : {total_odds_rows}",
        "",
        "Per-league counts:",
    ]
    for s in leagues_summary:
        lines.append(
            f"  - {s['league_id']:>3} ({s['league_name']}): "
            f"fixtures={s['fixtures']} fixtures_with_odds={s['fixtures_with_odds']} odds_rows={s['odds_rows']}"
        )
    write_text(OUT_DIR / "odds.txt", "\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
