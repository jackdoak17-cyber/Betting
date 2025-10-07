# scripts/fetch_fixtures.py
import os
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

from common import BASE_URL, API_TOKEN, ALLOWED_LEAGUES, write_json, write_csv

# Include related data we rely on when exporting CSV
INCLUDES = "participants;state"

def _iso(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")

def _env_window():
    """
    Read window from env or default to 'today .. +14 days' (UTC).
    """
    start_env = os.getenv("FIXTURES_START")
    end_env = os.getenv("FIXTURES_END")

    if start_env and end_env:
        return start_env, end_env

    today = datetime.now(timezone.utc).date()
    start = _iso(datetime.combine(today, datetime.min.time()))
    end = _iso(datetime.combine(today + timedelta(days=14), datetime.min.time()))
    return start, end

def get_between(start_date: str, end_date: str, leagues=None):
    """
    Fetch fixtures between dates. Follows SportMonks pagination by chasing links.next.
    We also filter client-side to allowed leagues to be 100% safe.
    """
    url = f"{BASE_URL}/football/fixtures/between/{start_date}/{end_date}"

    params = {
        "api_token": API_TOKEN,   # SportMonks accepts token as query param
        "include": INCLUDES,
        "page": 1,
        # If your plan supports pre-filtering by leagues, uncomment one of these:
        # "leagues": ",".join(map(str, leagues)) if leagues else None,
        # 'filters[league_id]': ",".join(map(str, leagues)) if leagues else None,
    }

    fixtures = []
    page_count = 0

    while True:
        resp = requests.get(url, params={k: v for k, v in params.items() if v is not None}, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        page_count += 1

        data = payload.get("data") or []
        # Safety filter by league_id
        if leagues:
            data = [fx for fx in data if fx.get("league_id") in leagues]
        fixtures.extend(data)

        links = payload.get("links") or {}
        next_link = links.get("next")
        if not next_link:
            break

        # Follow absolute next page URL. After the first request, DO NOT pass params again.
        url = next_link
        params = {}
        time.sleep(0.25)  # be gentle to rate limits

    print(f"Fetched {len(fixtures)} fixtures across {page_count} pages (filtered to {len(leagues or [])} leagues).")
    return fixtures

def main():
    start, end = _env_window()
    print(f"Fetching fixtures for {start} -> {end} (UTC). League filter: {ALLOWED_LEAGUES}")

    fixtures = get_between(start, end, leagues=ALLOWED_LEAGUES)

    out = {
        "generated_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "window_start": start,
        "window_end": end,
        "fixtures": fixtures,
        "leagues": ALLOWED_LEAGUES,
    }

    os.makedirs("data", exist_ok=True)
    write_json("data/fixtures_window.json", out)
    write_csv("data/fixtures_window.csv", fixtures)
    print(f"Saved: data/fixtures_window.json & data/fixtures_window.csv")

if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as e:
        print(f"HTTPError: {getattr(e.response, 'text', str(e))}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
