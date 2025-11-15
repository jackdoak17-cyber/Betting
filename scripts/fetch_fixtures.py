#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Robust Sportmonks fixtures fetcher (v3)

- Tries the BETWEEN endpoint first with server-side filters.
- Falls back to the per-day endpoint if the BETWEEN result looks suspiciously small.
- Handles both pagination styles seen in v3 (`last_page` OR `pagination.has_more`).
- Writes:
  data/fixtures/latest.json
  data/fixtures/{league_id}.json
  data/fixtures/by_league/{league_id}.json
  data/fixtures/fixtures.txt

Env:
  SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN or SM_TOKEN)
  DAYS_AHEAD (default 7)
"""

import os
import sys
import json
import datetime as dt
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
DATE_FMT = "%Y-%m-%d"
TIMEOUT = 25

LEAGUES = {
    8:   "Premier League",
    9:   "Championship",
    72:  "Eredivisie",
    82:  "Bundesliga",
    181: "Admiral Bundesliga",
    208: "Pro League",
    244: "1. HNL",
    271: "Superliga",
    301: "Ligue 1",
    384: "Serie A",
    387: "Serie B",
    444: "Eliteserien",
    453: "Ekstraklasa",
    462: "Liga Portugal",
    501: "Premiership",
    564: "La Liga",
    567: "La Liga 2",
    573: "Allsvenskan",
    591: "Super League",
    600: "Super Lig",
}
LEAGUE_IDS = sorted(LEAGUES.keys())

ROOT = Path(".")
OUT_DIR = ROOT / "data" / "fixtures"
BY_LEAGUE_DIR = OUT_DIR / "by_league"

def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime(DATE_FMT)

def daterange(start: dt.date, end_inclusive: dt.date):
    d = start
    while d <= end_inclusive:
        yield d
        d += dt.timedelta(days=1)

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def to_int(v):
    try:
        return int(v)
    except Exception:
        return None

def normalize_fixture(fx: dict) -> dict:
    parts = fx.get("participants") or []
    return {
        "id": to_int(fx.get("id")),
        "league_id": to_int(fx.get("league_id")),
        "season_id": to_int(fx.get("season_id")),
        "stage_id": to_int(fx.get("stage_id")),
        "round_id": to_int(fx.get("round_id")),
        "name": fx.get("name"),
        "starting_at": fx.get("starting_at"),
        "starting_at_timestamp": to_int(fx.get("starting_at_timestamp")),
        "state_id": to_int(fx.get("state_id")),
        "venue_id": to_int(fx.get("venue_id")),
        "participants": [
            {
                "id": to_int(p.get("id")),
                "name": p.get("name"),
                "short_code": p.get("short_code"),
                "meta": p.get("meta"),
            } for p in parts
        ],
    }

def read_pagination_meta(meta: dict) -> Tuple[Optional[int], Optional[bool]]:
    """
    Returns (pages, has_more). Handles both shapes:
      meta = {"last_page": N, ...}
      meta = {"pagination": {"total_pages": N, "has_more": bool}, ...}
      meta may also contain top-level "has_more".
    """
    if not isinstance(meta, dict):
        return (None, None)
    # Style 1: last_page
    if "last_page" in meta and meta.get("last_page"):
        try:
            return (int(meta["last_page"]), None)
        except Exception:
            pass
    # Style 2: pagination.total_pages / pagination.has_more
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

def fetch_between(start: dt.date, end: dt.date, league_ids: List[int]) -> List[dict]:
    """Server-side filter + robust pagination via fixtures/between."""
    leagues_csv = ",".join(str(x) for x in league_ids)
    base = f"fixtures/between/{dstr(start)}/{dstr(end)}"
    page = 1
    per_page = 50  # v3 cap often = 50
    rows_all: List[dict] = []
    while True:
        params = {
            "include": "participants;state;league",
            "order": "asc",
            "per_page": per_page,
            "page": page,
            # static filter style used by v3
            "filters": f"fixtureLeagues:{leagues_csv}",
        }
        j = api_get(base, params)
        rows = j.get("data") or []
        meta = j.get("meta") or {}
        pages, has_more = read_pagination_meta(meta)
        rows_all.extend(normalize_fixture(fx) for fx in rows)
        print(f"[between] page={page} got={len(rows)} total_so_far={len(rows_all)} pages={pages} has_more={has_more}")
        if pages:
            if page >= pages:
                break
            page += 1
        elif has_more is not None:
            if not has_more:
                break
            page += 1
        else:
            if len(rows) < per_page:
                break
            page += 1
    return rows_all

def fetch_by_date_loop(start: dt.date, end: dt.date, league_ids: List[int]) -> List[dict]:
    """Per-day fallback with server-side filter + robust pagination."""
    leagues_csv = ",".join(str(x) for x in league_ids)
    per_page = 50
    out: List[dict] = []
    for d in daterange(start, end):
        path = f"fixtures/date/{dstr(d)}"
        page = 1
        while True:
            params = {
                "include": "participants;state;league",
                "order": "asc",
                "per_page": per_page,
                "page": page,
                "filters": f"fixtureLeagues:{leagues_csv}",
            }
            j = api_get(path, params)
            rows = j.get("data") or []
            meta = j.get("meta") or {}
            pages, has_more = read_pagination_meta(meta)
            out.extend(normalize_fixture(fx) for fx in rows)
            print(f"[date {dstr(d)}] page={page} got={len(rows)} total_so_far={len(out)} pages={pages} has_more={has_more}")
            if pages:
                if page >= pages:
                    break
                page += 1
            elif has_more is not None:
                if not has_more:
                    break
                page += 1
            else:
                if len(rows) < per_page:
                    break
                page += 1
    return out

def unique_by_id(rows: List[dict]) -> List[dict]:
    seen = set()
    deduped = []
    for fx in rows:
        fid = fx.get("id")
        if fid is None or fid in seen:
            continue
        seen.add(fid)
        deduped.append(fx)
    return deduped

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

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    start = today_utc_date()
    end = start + dt.timedelta(days=DAYS_AHEAD)

    # 1) BETWEEN (fast path)
    between_rows = fetch_between(start, end, LEAGUE_IDS)

    # 2) Augment with per-day if result looks light
    # (Heuristic: across 9 leagues, 7 days <<100 is suspicious)
    if len(between_rows) < 100:
        day_rows = fetch_by_date_loop(start, end, LEAGUE_IDS)
        fixtures = unique_by_id(between_rows + day_rows)
    else:
        fixtures = unique_by_id(between_rows)

    # Bucket per league
    by_league: Dict[int, List[dict]] = {lid: [] for lid in LEAGUE_IDS}
    for fx in fixtures:
        lid = int(fx.get("league_id") or 0)
        if lid in by_league:
            by_league[lid].append(fx)

    # Sort deterministic
    def key_fx(fx): return (fx.get("starting_at_timestamp") or 0, fx.get("id") or 0)
    fixtures.sort(key=key_fx)
    for lid in by_league:
        by_league[lid].sort(key=key_fx)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    meta = {
        "generated_at": generated_at,
        "window_start": dstr(start),
        "window_end": dstr(end),
        "leagues": LEAGUE_IDS,
        "count": len(fixtures),
    }

    # Write
    write_json(OUT_DIR / "latest.json", {"meta": meta, "fixtures": fixtures})
    for lid in LEAGUE_IDS:
        payload = {
            "league_id": lid,
            "league_name": LEAGUES[lid],
            "window_start": meta["window_start"],
            "window_end": meta["window_end"],
            "count": len(by_league[lid]),
            "fixtures": by_league[lid],
        }
        write_json(OUT_DIR / f"{lid}.json", payload)
        write_json(BY_LEAGUE_DIR / f"{lid}.json", payload)

    # Human summary (keeps your current format)
    counts = "\n".join([f"  - {lid}: {len(by_league[lid])}" for lid in LEAGUE_IDS])
    text = (
        f"Time (UTC): {generated_at}\n"
        f"Window    : {meta['window_start']} -> {meta['window_end']}\n"
        f"Leagues   : {','.join(map(str, LEAGUE_IDS))}\n"
        f"Fixtures  : {meta['count']} (written {meta['count']})\n\n"
        f"Per league counts:\n{counts}\n"
    )
    write_text(OUT_DIR / "fixtures.txt", text)
    print(text)

if __name__ == "__main__":
    main()
