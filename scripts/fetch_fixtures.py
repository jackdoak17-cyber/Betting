#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Fetch upcoming fixtures from Sportmonks for specific leagues and write:
- data/fixtures/latest.json
- data/fixtures/{league_id}.json
- data/fixtures/by_league/{league_id}.json
- data/fixtures/fixtures.txt

Env:
  SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN or SM_TOKEN)
  DAYS_AHEAD  (default: 7)

Leagues:
  8,9,82,301,384,387,564,567,600
"""

import os
import sys
import json
import datetime as dt
from typing import List, Dict, Optional, Tuple
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

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
DATE_FMT = "%Y-%m-%d"
TIMEOUT = 25

ROOT = Path(".")
OUT_DIR = ROOT / "data" / "fixtures"
BY_LEAGUE_DIR = OUT_DIR / "by_league"

# ---------- Utils ----------
def today_utc_date() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def dstr(d: dt.date) -> str:
    return d.strftime(DATE_FMT)

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

def _get_total_pages(meta: dict) -> int:
    # v3 sometimes exposes either meta.last_page or meta.pagination.total_pages
    if not meta:
        return 1
    if "last_page" in meta and meta.get("last_page"):
        try:
            return int(meta["last_page"])
        except Exception:
            pass
    pag = meta.get("pagination") or {}
    if "total_pages" in pag and pag.get("total_pages"):
        try:
            return int(pag["total_pages"])
        except Exception:
            pass
    return 1

# ---------- Core ----------
def get_fixtures_between(start: dt.date, end: dt.date, league_ids: List[int]) -> List[dict]:
    """
    Use the between endpoint with server-side league filtering + robust pagination.
    """
    leagues_csv = ",".join(str(x) for x in league_ids)

    params = {
        "include": "participants;state;league",
        "order": "asc",
        "per_page": 200,              # <- be explicit: avoid silent 15/25 caps
        "page": 1,
        "leagues": leagues_csv,       # server-side filter
    }

    path = f"fixtures/between/{dstr(start)}/{dstr(end)}"
    first = api_get(path, params)
    data = (first.get("data") or [])
    meta = (first.get("meta") or {})
    total_pages = _get_total_pages(meta)

    # paginate
    for p in range(2, total_pages + 1):
        params["page"] = p
        jp = api_get(path, params)
        data.extend(jp.get("data") or [])

    # Normalize / coerce
    out = []
    dropped_no_parts = 0
    for fx in data:
        lid = _to_int(fx.get("league_id"))
        parts = fx.get("participants") or []
        if not parts:
            dropped_no_parts += 1
            continue

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
                    "meta": p.get("meta"),
                } for p in parts
            ],
        })

    print(f"[fetch] between {dstr(start)} -> {dstr(end)} pages={total_pages} total={len(out)} dropped_no_participants={dropped_no_parts}")
    return out

# ---------- Writers ----------
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

    fixtures = get_fixtures_between(start, end, LEAGUE_IDS)

    # bucket per league
    by_league: Dict[int, List[dict]] = {lid: [] for lid in LEAGUE_IDS}
    for fx in fixtures:
        lid = int(fx["league_id"] or 0)
        if lid in by_league:
            by_league[lid].append(fx)

    # Sort deterministically
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

    # Write latest.json
    write_json(OUT_DIR / "latest.json", {"meta": meta, "fixtures": fixtures})

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

    # Human summary (matches your current formatting)
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
