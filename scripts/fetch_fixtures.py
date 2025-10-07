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

# Leagues to include (from your message)
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

INCLUDES = "participants,state"
PER_PAGE = 100  # max page size allowed by Sportmonks v3

def utc_today_str():
    return datetime.utcnow().strftime("%Y-%m-%d")

def compute_window():
    if START_DATE and END_DATE:
        return START_DATE, END_DATE
    start = datetime.utcnow()
    end   = start + timedelta(days=WINDOW_DAYS)
    return start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")

def get_between(start_date: str, end_date: str, leagues: list[int]) -> list[dict]:
    """
    Server-side filter by leagues and paginate until no next_page.
    """
    headers = {"Accept": "application/json"}
    fixtures = []
    page = 1

    # Build the base URL once with fixed params
    base_url = f"{API_BASE}/fixtures/between/{start_date}/{end_date}"
    fixed_params = {
        "api_token": API_TOKEN,
        "include": INCLUDES,
        "per_page": PER_PAGE,
        # IMPORTANT: let the API do the league filtering
        "leagues": ",".join(str(x) for x in leagues),
        # Do NOT pass state filter; future fixtures are NS anyway and we don’t want to
        # accidentally exclude anything if Sportmonks changes state ids.
    }

    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    while True:
        params = fixed_params | {"page": page}
        url = f"{base_url}?{urlencode(params)}"
        resp = requests.get(url, headers=headers, timeout=30)

        if resp.status_code != 200:
            print(f"ERROR: HTTP {resp.status_code} from Sportmonks on page {page}: {resp.text[:400]}", file=sys.stderr)
            break

        data = resp.json() or {}
        page_items = data.get("data") or data.get("fixtures") or []
        fixtures.extend(page_items)

        # Sportmonks v3 pagination: meta / pagination / current_page, last_page, next_page_url
        meta = data.get("meta") or {}
        pagination = meta.get("pagination") or {}
        next_page_url = pagination.get("links", {}).get("next") or pagination.get("next_page_url")

        # Log lightweight debug
        print(f"DEBUG: page {page} -> {len(page_items)} items; total so far {len(fixtures)}")

        if not next_page_url:
            break
        page += 1
        # small polite delay
        time.sleep(0.20)

    return fixtures

def flatten_fixture(row: dict) -> dict:
    # Try to extract home/away names from included participants (if present)
    home, away = None, None
    parts = row.get("participants") or row.get("participants", [])
    if parts:
        for p in parts:
            meta = p.get("meta") or {}
            loc = meta.get("location")
            if loc == "home": home = p.get("name")
            if loc == "away": away = p.get("name")

    return {
        "id": row.get("id"),
        "league_id": row.get("league_id"),
        "name": row.get("name") or (f"{home} vs {away}" if home or away else None),
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

    # CSV – one row per fixture (small subset of fields, easy to consume)
    rows = [flatten_fixture(x) for x in fixtures]
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else
                           ["id","league_id","name","starting_at","state_id","venue_id"])
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Wrote {len(fixtures)} fixtures to {OUT_JSON} and {OUT_CSV}")

def main():
    s, e = compute_window()
    print(f"Fetching fixtures for {s} → {e} (UTC). Leagues: {LEAGUES}")
    fixtures = get_between(s, e, LEAGUES)

    # Safety: dedupe by id
    seen = set()
    unique = []
    for fx in fixtures:
        fid = fx.get("id")
        if fid not in seen:
            unique.append(fx)
            seen.add(fid)

    write_outputs(s, e, unique)

if __name__ == "__main__":
    main()
