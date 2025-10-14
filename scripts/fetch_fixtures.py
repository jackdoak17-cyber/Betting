#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch upcoming fixtures from Sportmonks for specific leagues and write:
- data/fixtures/latest.json         (with generated_at + metadata + all fixtures)
- data/fixtures/{league_id}.json    (per-league fixtures)
- data/fixtures/by_league/{id}.json (same payload; kept for compatibility)
- data/fixtures/fixtures.txt        (human summary)

Env:
  SPORTMONKS_TOKEN or SPORTMONKS_API_TOKEN or SM_TOKEN
  DAYS_AHEAD  (default: 7)

Leagues:
  8,9,82,301,384,387,564,567,600
"""

import os
import sys
import json
import datetime as dt
from typing import List, Dict, Optional
from pathlib import Path

import requests

# ---------- Config ----------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)

LEAGUES: Dict[int, str] = {
    8:   "Premier League",
    9:   "Championship",
    82:  "Bundesliga",
    301: "Ligue 1",
    384: "Serie A",
    387: "Serie B",
    564: "La Liga",
    567: "La Liga 2",
    600: "Super Lig",
}
LEAGUE_IDS = sorted(LEAGUES.keys())

# ↓ Changed from 21 -> 7 (and still overrideable via env)
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))

DATE_FMT = "%Y-%m-%d"
TIMEOUT = 25

ROOT = Path(".")
OUT_DIR = ROOT / "data" / "fixtures"
BY_LEAGUE_DIR = OUT_DIR / "by_league"


# ---------- Utils ----------
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def api_get(path: str, params: Optional[dict] = None) -> dict:
    if params is None:
        params = {}
    params = {**params, "api_token": API_TOKEN}
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()

def _to_int(v):
    try:
        return int(v)
    except Exception:
        return None

def get_fixtures_for_date(date_str: str, league_filter: Optional[set] = None) -> List[dict]:
    """
    Fetch fixtures for a single date, include basic relations, paginate, then
    return a normalized list (league_id coerced to int).
    """
    params = {"include": "participants;state;league", "order": "asc", "page": 1}
    j = api_get(f"fixtures/date/{date_str}", params)
    data = (j.get("data") or [])
    meta = (j.get("meta") or {})
    last_page = int(meta.get("last_page", 1))

    for p in range(2, last_page + 1):
        params["page"] = p
        jp = api_get(f"fixtures/date/{date_str}", params)
        data.extend(jp.get("data") or [])

    out = []
    dropped_wrong_league = 0
    dropped_no_parts = 0

    for fx in data:
        lid_raw = fx.get("league_id")
        lid = _to_int(lid_raw)

        if league_filter and (lid is None or lid not in league_filter):
            dropped_wrong_league += 1
            continue

        parts = fx.get("participants") or []
        if not parts:
            dropped_no_parts += 1
            continue

        # Keep a tidy subset; coerce ids to int where sensible
        out.append({
            "id": _to_int(fx.get("id")),
            "league_id": lid,
            "season_id": _to_int(fx.get("season_id")),
            "stage_id": _to_int(fx.get("stage_id")),
            "round_id": _to_int(fx.get("round_id")),
            "name": fx.get("name"),
            "starting_at": fx.get("starting_at"),
            "starting_at_timestamp": _to_int(fx.get("starting_at_timestamp")),
            "state_id": _to_int(fx.get("state_id")),
            "venue_id": _to_int(fx.get("venue_id")),
            "participants": [
                {
                    "id": _to_int(p.get("id")),
                    "name": p.get("name"),
                    "short_code": p.get("short_code"),
                    "meta": p.get("meta"),  # home/away info lives here
                }
                for p in parts
            ],
        })

    if dropped_wrong_league or dropped_no_parts:
        print(f"[{date_str}] dropped: wrong_league={dropped_wrong_league}, no_participants={dropped_no_parts}")

    return out

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


# ---------- Main ----------
def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN / SPORTMONKS_API_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    start = today_utc_date()
    end = start + dt.timedelta(days=DAYS_AHEAD)
    dates = daterange_str(start, end)

    all_fixtures: List[dict] = []
    by_league: Dict[int, List[dict]] = {lid: [] for lid in LEAGUE_IDS}

    for ds in dates:
        try:
            day = get_fixtures_for_date(ds, league_filter=set(LEAGUE_IDS))
        except requests.HTTPError as e:
            print(f"[WARN] {ds}: {e}", file=sys.stderr)
            continue
        all_fixtures.extend(day)
        for fx in day:
            by_league[int(fx["league_id"])].append(fx)

    # Sort deterministically (by time then id)
    def key_fx(fx): return (fx.get("starting_at_timestamp") or 0, fx.get("id") or 0)
    all_fixtures.sort(key=key_fx)
    for lid in by_league:
        by_league[lid].sort(key=key_fx)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    meta = {
        "generated_at": generated_at,
        "window_start": start.strftime(DATE_FMT),
        "window_end": end.strftime(DATE_FMT),
        "leagues": LEAGUE_IDS,
        "count": len(all_fixtures),
    }

    # Write latest.json
    write_json(OUT_DIR / "latest.json", {"meta": meta, "fixtures": all_fixtures})

    # Write per-league JSONs (top-level and by_league/)
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

    # Human summary
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
