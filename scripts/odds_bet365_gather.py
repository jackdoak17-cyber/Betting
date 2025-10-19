#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sportmonks Bet365 pre-match odds gatherer (fast)

- Reads upcoming fixtures from: data/fixtures/{league_id}.json
- Fetches ONLY Bet365 (bookmaker_id=2 by default) pre-match odds for each fixture:
    GET /v3/football/odds/pre-match/fixtures/{fixture_id}?filter=bookmakers:2
- Uses concurrency + backoff, and the correct 'filter' param (not 'filters').

Writes:
  data/odds/b365/latest.json
  data/odds/b365/{league_id}.json
  data/odds/b365/by_league/{league_id}.json
  data/odds/b365/fixtures/{fixture_id}.json
  data/odds/b365/odds.txt

Env:
  SPORTMONKS_TOKEN        (required)
  SM_BOOKMAKER_ID         (optional, default "2" -> Bet365)
  LEAGUE_IDS              (optional CSV; defaults to: 301,384,387,564,567,600,8,82,9)
  SM_MARKETS              (optional CSV of market IDs; appended to filter as 'markets:...')
  SM_MAX_WORKERS          (optional int, default 6)   # concurrency
  SM_PER_PAGE             (optional int, default 50)  # API cap is typically 50
  SM_TIMEOUT              (optional int, default 20)  # seconds
  SM_RETRIES              (optional int, default 2)
"""

import os
import sys
import json
import time
import math
import datetime as dt
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
BOOKMAKER_ID = int(os.getenv("SM_BOOKMAKER_ID", "2"))  # Bet365 default
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()
]
MARKETS = os.getenv("SM_MARKETS")  # e.g. "1,2,12"
MAX_WORKERS = int(os.getenv("SM_MAX_WORKERS", "6"))
PER_PAGE = int(os.getenv("SM_PER_PAGE", "50"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))
RETRIES = int(os.getenv("SM_RETRIES", "2"))

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
OUT_ROOT = ROOT / "data" / "odds" / "b365"
BY_LEAGUE_DIR = OUT_ROOT / "by_league"
PER_FIXTURE_DIR = OUT_ROOT / "fixtures"

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

def load_fixtures_for_league(league_id: int) -> Dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.exists():
        return {"league_id": league_id, "fixtures": [], "league_name": str(league_id)}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def build_filter() -> str:
    parts = [f"bookmakers:{BOOKMAKER_ID}"]
    if MARKETS:
        parts.append(f"markets:{MARKETS}")
    # Note: Sportmonks v3 expects 'filter' (singular), semicolon-separated
    return ";".join(parts)

def api_get(session: requests.Session, path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    for attempt in range(RETRIES + 1):
        try:
            r = session.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                # Backoff on rate limit
                time.sleep(0.7 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError):
            if attempt < RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise
        except requests.HTTPError:
            # Bubble up other HTTP errors (4xx/5xx) after retries
            if attempt < RETRIES:
                time.sleep(0.5 * (attempt + 1))
                continue
            raise

def read_pagination_meta(meta: dict) -> Tuple[Optional[int], Optional[bool]]:
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

def fetch_bet365_odds_for_fixture(session: requests.Session, fixture_id: int) -> List[dict]:
    base = f"odds/pre-match/fixtures/{fixture_id}"
    page = 1
    out: List[dict] = []
    the_filter = build_filter()
    while True:
        params = {
            "filter": the_filter,  # <-- singular 'filter' works (bookmakers + optional markets)
            "per_page": PER_PAGE,
            "order": "asc",
            "page": page,
        }
        j = api_get(session, base, params)
        rows = j.get("data") or []
        meta = j.get("meta") or {}
        out.extend(rows)

        pages, has_more = read_pagination_meta(meta)
        if pages:
            if page >= pages:
                break
            page += 1
        elif has_more is not None:
            if not has_more:
                break
            page += 1
        else:
            if len(rows) < PER_PAGE:
                break
            page += 1
    return out

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    # Collect fixtures (and keep league mapping)
    league_fixtures: Dict[int, List[dict]] = {}
    league_names: Dict[int, str] = {}
    fixture_to_league: Dict[int, int] = {}

    for lid in LEAGUE_IDS:
        payload = load_fixtures_for_league(lid)
        fixtures = payload.get("fixtures") or []
        league_fixtures[lid] = fixtures
        league_names[lid] = payload.get("league_name") or str(lid)
        for fx in fixtures:
            fid = int(fx.get("id"))
            fixture_to_league[fid] = lid

    all_fixture_ids = list(fixture_to_league.keys())

    # Fetch concurrently
    session = requests.Session()
    results: Dict[int, List[dict]] = {}
    errors: Dict[int, str] = {}

    if not all_fixture_ids:
        print("No fixtures found in data/fixtures — nothing to do.")
        return

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {
            ex.submit(fetch_bet365_odds_for_fixture, session, fid): fid
            for fid in all_fixture_ids
        }
        for fut in as_completed(futures):
            fid = futures[fut]
            try:
                rows = fut.result()
                results[fid] = rows
                # per-fixture save (quick)
                write_json(PER_FIXTURE_DIR / f"{fid}.json", {
                    "fixture_id": fid,
                    "bookmaker_id": BOOKMAKER_ID,
                    "fetched_at": generated_at,
                    "odds": rows,
                })
            except Exception as e:
                errors[fid] = f"{type(e).__name__}: {e}"

    # Aggregate per league
    leagues_summary = []
    total_rows = 0
    total_fixtures = 0

    for lid in LEAGUE_IDS:
        fixtures = league_fixtures.get(lid, [])
        league_name = league_names.get(lid, str(lid))

        league_rows = []
        for fx in fixtures:
            fid = int(fx.get("id"))
            league_rows.append({
                "fixture_id": fid,
                "name": fx.get("name"),
                "starting_at": fx.get("starting_at"),
                "league_id": lid,
                "bookmaker_id": BOOKMAKER_ID,
                "odds": results.get(fid, []),
                "error": errors.get(fid),
            })

        payload = {
            "league_id": lid,
            "league_name": league_name,
            "generated_at": generated_at,
            "bookmaker_id": BOOKMAKER_ID,
            "fixture_count": len(fixtures),
            "fixtures_with_odds": sum(1 for r in league_rows if r.get("odds")),
            "odds_row_count": sum(len(r.get("odds") or []) for r in league_rows),
            "fixtures": league_rows,
        }
        total_fixtures += len(fixtures)
        total_rows += payload["odds_row_count"]

        write_json(OUT_ROOT / f"{lid}.json", payload)
        write_json(BY_LEAGUE_DIR / f"{lid}.json", payload)

        leagues_summary.append({
            "league_id": lid,
            "league_name": league_name,
            "fixtures": len(fixtures),
            "fixtures_with_odds": payload["fixtures_with_odds"],
            "odds_rows": payload["odds_row_count"],
        })

    latest = {
        "generated_at": generated_at,
        "bookmaker_id": BOOKMAKER_ID,
        "league_ids": LEAGUE_IDS,
        "total_fixtures_seen": total_fixtures,
        "total_odds_rows": total_rows,
        "errors": errors,
        "leagues": leagues_summary,
        "filter_used": build_filter(),
    }
    write_json(OUT_ROOT / "latest.json", latest)

    # Human-readable summary
    lines = [
        f"Time (UTC): {generated_at}",
        f"Bookmaker : {BOOKMAKER_ID}",
        f"Leagues   : {','.join(map(str, LEAGUE_IDS))}",
        f"Fixtures  : {total_fixtures}",
        f"Odds rows : {total_rows}",
        "",
        "Per-league counts:",
    ]
    for s in leagues_summary:
        lines.append(
            f"  - {s['league_id']:>3} ({s['league_name']}): "
            f"fixtures={s['fixtures']} fixtures_with_odds={s['fixtures_with_odds']} odds_rows={s['odds_rows']}"
        )
    if errors:
        lines.append("")
        lines.append("Errors:")
        for fid, msg in list(errors.items())[:20]:
            lines.append(f"  fixture {fid}: {msg}")

    write_text(OUT_ROOT / "odds.txt", "\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
