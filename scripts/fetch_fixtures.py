#!/usr/bin/env python3
import os
import sys
import json
import csv
import pathlib
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qs

import requests

API_BASE = "https://api.sportmonks.com/v3/football/fixtures/between"
INCLUDE = "participants;state"  # keep it small/cheap

TOKEN = os.getenv("SPORTMONKS_TOKEN")
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN is not set in the environment.", file=sys.stderr)
    sys.exit(1)

# Optional comma-separated league IDs from env, e.g. "8,9,564"
LEAGUE_FILTER = os.getenv("FILTER_LEAGUES", "").strip()
LEAGUE_FILTER = ",".join([p.strip() for p in LEAGUE_FILTER.split(",") if p.strip()]) or None

DATA_DIR = pathlib.Path("data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

def dt_utc_today():
    return datetime.now(timezone.utc).date()

def fmt(d):
    return d.strftime("%Y-%m-%d")

def fetch_between(start_date, end_date, leagues=None):
    """
    Fetch fixtures between start_date and end_date (inclusive) with robust pagination.
    Returns a list of fixture dicts.
    """
    fixtures = []
    page = 1
    params = {
        "api_token": TOKEN,
        "include": INCLUDE,
        "page": page,
    }
    # If you want to filter at the API, pass leagues (comma-separated string)
    if leagues:
        params["leagues"] = leagues

    url = f"{API_BASE}/{fmt(start_date)}/{fmt(end_date)}"
    while True:
        params["page"] = page
        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code} for page {page}: {resp.text[:500]}", file=sys.stderr)
            sys.exit(1)

        payload = resp.json()
        # SportMonks v3 returns arrays in 'data'
        batch = payload.get("data") or []
        fixtures.extend(batch)

        meta = payload.get("meta") or {}
        next_page = meta.get("next_page")

        # DEBUG lines (safe to leave in)
        if page == 1:
            dbg_url = resp.url
            print(f"DEBUG URL (page 1): {dbg_url}")
        print(f"  page {page}: received {len(batch)} fixtures")

        if not next_page:
            break

        # Handle both int and URL forms for next_page
        if isinstance(next_page, int):
            page = next_page
        elif isinstance(next_page, str):
            # Try to extract ?page=X from the URL; if missing, just increment
            try:
                q = parse_qs(urlparse(next_page).query)
                page = int((q.get("page") or [page + 1])[0])
            except Exception:
                page += 1
        else:
            # Unrecognized form — stop gracefully
            break

    return fixtures

def save_outputs(start_date, end_date, fixtures):
    # JSON
    out_json = {
        "generated_at": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "window_start": fmt(start_date),
        "window_end": fmt(end_date),
        "fixtures": fixtures,
        "leagues": [int(x) for x in (LEAGUE_FILTER.split(",") if LEAGUE_FILTER else [])],
    }
    json_path = DATA_DIR / "fixtures_window.json"
    json_path.write_text(json.dumps(out_json, indent=2))
    print(f"Wrote {json_path} ({len(fixtures)} fixtures)")

    # CSV (compact)
    csv_path = DATA_DIR / "fixtures_window.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "league_id", "league_name", "starting_at", "home_team", "away_team", "state"])
        for fx in fixtures:
            lid = fx.get("league_id")
            league_name = fx.get("league_name") or ""
            starting_at = fx.get("starting_at") or ""
            parts = fx.get("participants") or []
            home, away = "", ""
            for p in parts:
                loc = ((p.get("meta") or {}).get("location") or "").lower()
                if loc == "home":
                    home = p.get("name") or ""
                elif loc == "away":
                    away = p.get("name") or ""
            state = (fx.get("state") or {}).get("name") or ""
            w.writerow([fx.get("id"), lid, league_name, starting_at, home, away, state])
    print(f"Wrote {csv_path}")

def main():
    # Default 14-day window
    start = dt_utc_today()
    end = start + timedelta(days=14)

    print(f"Fetching fixtures for {fmt(start)} → {fmt(end)} (UTC). League filter: {LEAGUE_FILTER or 'ALL'}")

    fixtures = fetch_between(start, end, leagues=LEAGUE_FILTER)

    # If you chose not to filter at the API level but still want to filter locally:
    if not LEAGUE_FILTER and os.getenv("FILTER_LOCALLY") == "1":
        keep = set(int(x) for x in (os.getenv("FILTER_LEAGUES", "").split(",") if os.getenv("FILTER_LEAGUES") else []) if x.strip())
        if keep:
            fixtures = [fx for fx in fixtures if int(fx.get("league_id") or 0) in keep]

    save_outputs(start, end, fixtures)

if __name__ == "__main__":
    main()
