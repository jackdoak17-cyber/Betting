#!/usr/bin/env python3
"""
Fetch upcoming fixtures from Sportmonks and persist results to the repo.

Outputs (all overwritten on each run):
- data/fixtures/latest.json                -> merged fixtures across all leagues
- data/fixtures/by_league/{league_id}.json -> fixtures per league
- data/fixtures/summary.txt                -> short header summary (counts, dates, leagues)
- data/fixtures/fixtures.txt               -> human-readable list of all fixtures

Auth:
- Set SPORTMONKS_TOKEN in GitHub Secrets and export as env for the job.
"""

from __future__ import annotations
import os
import sys
import json
import time
import pathlib
import datetime as dt
from typing import Dict, List, Any
import urllib.request
import urllib.parse

# -------------------------
# Config
# -------------------------

ALLOWED_LEAGUES = [
    8,    # Premier League
    9,    # Championship
    384,  # Serie A
    387,  # Serie B
    82,   # Bundesliga
    301,  # Ligue 1
    564,  # La Liga
    567,  # La Liga 2
    600,  # Süper Lig
]

# Window: today -> today + 21 days (inclusive of start)
WINDOW_DAYS = int(os.getenv("FIXTURE_WINDOW_DAYS", "21"))

# Base output folder
BASE_DIR = pathlib.Path("data/fixtures")

# Sportmonks
SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_TOKEN")
API_BASE = "https://api.sportmonks.com/v3/football/fixtures/between"

# polite rate limiting
PER_REQUEST_SLEEP = float(os.getenv("FIXTURE_REQUEST_DELAY_SEC", "0.4"))

# -------------------------
# Helpers
# -------------------------

def _ensure_env() -> None:
    if not SPORTMONKS_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

def _dates_utc() -> tuple[str, str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.date()
    end = (now + dt.timedelta(days=WINDOW_DAYS)).date()
    return (
        now.isoformat(),
        start.isoformat(),
        end.isoformat(),
    )

def _http_get(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    try:
        return json.loads(data.decode("utf-8"))
    except json.JSONDecodeError:
        # Sometimes Sportmonks returns bytes with BOM/odd chars
        return json.loads(data.decode("utf-8", errors="ignore"))

def _build_url(start_date: str, end_date: str, league_id: int) -> str:
    # Sportmonks between endpoint; restrict to upcoming “not started/finished” via states if desired.
    # We’ll rely on their default upcoming for the window; filter client-side too.
    qs = {
        "api_token": SPORTMONKS_TOKEN,
        "leagues": str(league_id),
        # include odds flags etc. not required here; keep payload lean
        "per_page": "200",
    }
    return f"{API_BASE}/{urllib.parse.quote(start_date)}/{urllib.parse.quote(end_date)}?{urllib.parse.urlencode(qs)}"

def _is_future(fx: Dict[str, Any]) -> bool:
    # Keep fixtures that have not started (state_id 1 = Not started in Sportmonks v3)
    # Also sanity-check by start timestamp in the future.
    try:
        st_ts = int(fx.get("starting_at_timestamp") or 0)
    except Exception:
        st_ts = 0
    state_id = fx.get("state_id")
    now_ts = int(time.time())
    return (state_id in (1, None)) and (st_ts == 0 or st_ts > now_ts)

def _sort_key(fx: Dict[str, Any]):
    # sort by start time, then league, then id
    return (
        int(fx.get("starting_at_timestamp") or 0),
        int(fx.get("league_id") or 0),
        int(fx.get("id") or 0),
    )

def _write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# -------------------------
# Main
# -------------------------

def main() -> int:
    _ensure_env()
    generated_at_utc, start_date, end_date = _dates_utc()

    all_fixtures: List[Dict[str, Any]] = []
    per_league: Dict[int, List[Dict[str, Any]]] = {lid: [] for lid in ALLOWED_LEAGUES}

    # Fetch per league (keeps us under paging/rate pressure and lets us persist by-league files)
    for lid in ALLOWED_LEAGUES:
        url = _build_url(start_date, end_date, lid)
        try:
            payload = _http_get(url)
        except Exception as e:
            print(f"[warn] league {lid}: request failed: {e}", file=sys.stderr)
            continue

        # Sportmonks v3 returns {"data": [...]} at top-level
        items = payload.get("data") or []
        # filter to future/not started
        items = [fx for fx in items if _is_future(fx)]
        items.sort(key=_sort_key)

        per_league[lid] = items
        all_fixtures.extend(items)

        # Write per-league JSON (overwrite)
        _write_json(BASE_DIR / "by_league" / f"{lid}.json", {
            "generated_at_utc": generated_at_utc,
            "start_date": start_date,
            "end_date": end_date,
            "league_id": str(lid),
            "count": len(items),
            "data": items,
        })

        # small delay between requests
        time.sleep(PER_REQUEST_SLEEP)

    # Dedup in case an item appears twice (shouldn’t, but safe)
    # Use dict by id
    dedup: Dict[int, Dict[str, Any]] = {}
    for fx in all_fixtures:
        fid = int(fx.get("id") or 0)
        if fid:
            dedup[fid] = fx
    merged = list(dedup.values())
    merged.sort(key=_sort_key)

    # Write merged latest.json (overwrite)
    latest_obj = {
        "generated_at_utc": generated_at_utc,
        "start_date": start_date,
        "end_date": end_date,
        "league_ids": [str(l) for l in ALLOWED_LEAGUES],
        "count": len(merged),
        "data": merged,
    }
    _write_json(BASE_DIR / "latest.json", latest_obj)

    # Human-readable files
    # 1) summary.txt
    summary_lines = [
        f"fixtures = Time (UTC): {generated_at_utc}",
        f"Window    : {start_date} -> {end_date}",
        f"Leagues   : {','.join(str(l) for l in ALLOWED_LEAGUES)}",
        f"Fixtures  : {len(merged)} (written {len(merged)})",
        "",
    ]
    _write_text(BASE_DIR / "summary.txt", "\n".join(summary_lines).rstrip() + "\n")

    # 2) fixtures.txt — full list, one per line
    list_lines = []
    for fx in merged:
        fid = fx.get("id")
        start_str = fx.get("starting_at") or ""
        name = fx.get("name") or ""
        list_lines.append(f"{fid} | {start_str} | {name}")
    # If you ever want to clip, set FIXTURE_LIST_LIMIT; default = 0 (no clip)
    limit = int(os.getenv("FIXTURE_LIST_LIMIT", "0"))
    if limit > 0:
        list_lines = list_lines[:limit]
    pretty = "\n".join(summary_lines + list_lines)
    _write_text(BASE_DIR / "fixtures.txt", pretty.rstrip() + "\n")

    # Console echo (useful in Actions logs)
    print("\n".join(summary_lines[:4]))
    print(f"(Saved to {BASE_DIR}/latest.json, by_league/*.json, summary.txt, fixtures.txt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
