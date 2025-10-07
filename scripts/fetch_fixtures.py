#!/usr/bin/env python3
import os, sys, json, csv, time
from datetime import datetime, timedelta
from urllib.parse import urlencode
import requests

API_BASE = "https://api.sportmonks.com/v3/football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")

# 14-day window by default (UTC)
WINDOW_DAYS = int(os.getenv("FIXTURE_WINDOW_DAYS", "14"))
START_DATE = os.getenv("WINDOW_START")  # YYYY-MM-DD (optional)
END_DATE   = os.getenv("WINDOW_END")    # YYYY-MM-DD (optional)

# Leagues to include (your list)
LEAGUES = [
    8,   # Premier League
    9,   # Championship
    384, # Serie A
    387, # Serie B
    82,  # Bundesliga
    301, # Ligue 1
    564, # La Liga
    567, # La Liga 2
    600, # Süper Lig
    72,  # Eredivisie
    271, # Superliga
]

OUT_JSON = "data/fixtures_window.json"
OUT_CSV  = "data/fixtures_window.csv"
DEBUG_DIR = "data/debug"

INCLUDES = "participants,state"
PER_PAGE = 100  # safe, high page size

def compute_window():
    if START_DATE and END_DATE:
        return START_DATE, END_DATE
    start = datetime.utcnow()
    end   = start + timedelta(days=WINDOW_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def get_all_between(start_date: str, end_date: str) -> list[dict]:
    """
    Fetch ALL fixtures between start/end, across ALL pages.
    We avoid server-side league filtering (which has been flaky)
    and filter client-side instead.
    """
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN env var not set", file=sys.stderr)
        sys.exit(1)

    os.makedirs(DEBUG_DIR, exist_ok=True)

    headers = {"Accept": "application/json"}
    base_url = f"{API_BASE}/fixtures/between/{start_date}/{end_date}"

    fixed_params = {
        "api_token": API_TOKEN,
        "include": INCLUDES,
        "per_page": PER_PAGE,
        # DO NOT pass state/leagues filters here — fetch everything, then filter locally.
    }

    fixtures = []
    page = 1
    while True:
        params = fixed_params | {"page": page}
        url = f"{base_url}?{urlencode(params)}"
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code} on page {page}: {resp.text[:400]}", file=sys.stderr)
            break

        data = resp.json() or {}

        # Save raw page to help debug any future surprises
        with open(os.path.join(DEBUG_DIR, f"raw_page_{page}.json"), "w", encoding="utf-8") as df:
            json.dump(data, df, ensure_ascii=False, indent=2)

        page_items = data.get("data") or data.get("fixtures") or []
        fixtures.extend(page_items)

        # Sportmonks v3 pagination shapes
        meta = data.get("meta") or {}
        pagination = meta.get("pagination") or {}
        links = pagination.get("links") or {}
        next_url = links.get("next") or pagination.get("next_page_url")

        print(f"DEBUG: page {page} -> {len(page_items)} items; total so far {len(fixtures)}")

        if not next_url:
            break
        page += 1
        time.sleep(0.20)  # be polite

    return fixtures

def filter_by_leagues(fixtures: list[dict], league_ids: list[int]) -> list[dict]:
    wanted = set(league_ids)
    out = [f for f in fixtures if f.get("league_id") in wanted]
    return out

def flatten_fixture(row: dict) -> dict:
    home, away = None, None
    parts = row.get("participants") or []
    for p in parts:
        loc = (p.get("meta") or {}).get("location")
        if loc == "home": home = p.get("name")
        if loc == "away": away = p.get("name")

    return {
        "id": row.get("id"),
        "league_id": row.get("league_id"),
        "name": row.get("name") or (f"{home} vs {away}" if (home or away) else ""),
        "starting_at": row.get("starting_at"),
        "state_id": row.get("state_id"),
        "venue_id": row.get("venue_id"),
    }

def write_outputs(start_date: str, end_date: str, fixtures: list[dict]):
    os.makedirs("data", exist_ok=True)

    payload = {
        "generated_at": datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "window_start": start_date,
        "window_end": end_date,
        "fixtures": fixtures,
        "leagues": LEAGUES,
    }
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    rows = [flatten_fixture(x) for x in fixtures]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        fields = ["id","league_id","name","starting_at","state_id","venue_id"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Wrote {len(fixtures)} fixtures to {OUT_JSON} and {OUT_CSV}")

def main():
    s, e = compute_window()
    print(f"Fetching ALL fixtures for {s} → {e} (UTC); then filtering to leagues: {LEAGUES}")

    all_fx = get_all_between(s, e)
    print(f"DEBUG: total fixtures from API in window = {len(all_fx)}")

    fx = filter_by_leagues(all_fx, LEAGUES)
    print(f"DEBUG: fixtures after league filter ({len(LEAGUES)} leagues) = {len(fx)}")

    # De-dupe by id just in case
    seen, unique = set(), []
    for f in fx:
        fid = f.get("id")
        if fid not in seen:
            unique.append(f)
            seen.add(fid)

    write_outputs(s, e, unique)

if __name__ == "__main__":
    main()
