#!/usr/bin/env python3
"""
Fetch fixtures for a date window and save JSON/CSV.

ENV:
  SPORTMONKS_TOKEN  (required)
  DAYS_AHEAD        (optional, default 14)
  FILTER_LEAGUES    (optional, comma list; if set, request is limited to those leagues)
"""

import csv
import datetime as dt
import json
import os
import sys
import time
from typing import Dict, List, Optional
import requests

API_BASE = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not TOKEN:
    print("ERROR: SPORTMONKS_TOKEN is not set", file=sys.stderr)
    sys.exit(2)

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))
_filter_env = os.getenv("FILTER_LEAGUES", "").strip()
FILTER_LEAGUES: Optional[List[int]] = (
    [int(x) for x in _filter_env.split(",") if x.strip().isdigit()] if _filter_env else None
)

DATA_DIR = "data"
BY_DAY_DIR = os.path.join(DATA_DIR, "fixtures_by_day")
os.makedirs(BY_DAY_DIR, exist_ok=True)

def today_utc_date() -> dt.date:
    return dt.datetime.utcnow().date()

def get_between(start: dt.date, end: dt.date, leagues: Optional[List[int]]) -> List[Dict]:
    """Call fixtures/between with pagination; include leagues filter only if provided."""
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    page = 1
    all_fx: List[Dict] = []
    while True:
        params = {
            "api_token": TOKEN,
            "include": "participants;state",
            "page": page,
        }
        if leagues:
            params["leagues"] = ",".join(map(str, leagues))

        url = f"{API_BASE}/fixtures/between/{start_str}/{end_str}"
        # DEBUG: exact URL being sent
        req_url = requests.Request("GET", url, params=params).prepare().url
        print(f"DEBUG URL (page {page}): {req_url}")

        resp = requests.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = max(int(resp.headers.get("Retry-After", "5")), 5)
            print(f"429 rate limited, sleeping {wait}s…")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        payload = resp.json()

        data = payload.get("data") or payload.get("response") or payload
        if not isinstance(data, list):
            data = payload.get("data", {}).get("data", []) or []

        print(f"  page {page}: received {len(data)} fixtures")
        all_fx.extend(data)

        pag = payload.get("pagination") or payload.get("meta", {}).get("pagination")
        if not pag:
            break
        has_more = pag.get("has_more")
        next_page = pag.get("next_page")
        current = pag.get("current_page") or page
        total_pages = pag.get("total_pages") or pag.get("total_pages_count")

        if has_more and next_page:
            page = int(next_page)
            continue
        if total_pages and current < total_pages:
            page += 1
            continue
        break

    print(f"TOTAL fixtures collected: {len(all_fx)}")
    return all_fx

def save_json(path: str, obj: Dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))

def save_csv(path: str, fixtures: List[Dict]):
    cols = ["id","league_id","league_name","starting_at","home_team","away_team","state"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for fx in fixtures:
            parts = fx.get("participants") or []
            home = next((p for p in parts if (p.get("meta") or {}).get("location") == "home"), {})
            away = next((p for p in parts if (p.get("meta") or {}).get("location") == "away"), {})
            state = (fx.get("state") or {}).get("name")
            w.writerow([
                fx.get("id"),
                fx.get("league_id"),
                fx.get("league_name"),
                fx.get("starting_at"),
                home.get("name"),
                away.get("name"),
                state,
            ])

def split_by_day(fixtures: List[Dict]) -> Dict[str, List[Dict]]:
    by_day: Dict[str, List[Dict]] = {}
    for fx in fixtures:
        ts = fx.get("starting_at", "")
        day = ts.split(" ")[0] if " " in ts else ts[:10]
        by_day.setdefault(day, []).append(fx)
    return by_day

def main():
    start = today_utc_date()
    end = start + dt.timedelta(days=DAYS_AHEAD)
    print(f"Fetching fixtures for {start} → {end} (UTC). "
          f"League filter: {'ALL' if not FILTER_LEAGUES else ','.join(map(str,FILTER_LEAGUES))}")

    fixtures = get_between(start, end, FILTER_LEAGUES)

    window_obj = {
        "generated_at": dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "window_start": start.strftime("%Y-%m-%d"),
        "window_end": end.strftime("%Y-%m-%d"),
        "fixtures": fixtures,
        "leagues": (FILTER_LEAGUES or "ALL"),
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    json_path = os.path.join(DATA_DIR, "fixtures_window.json")
    csv_path = os.path.join(DATA_DIR, "fixtures_window.csv")
    save_json(json_path, window_obj)
    save_csv(csv_path, fixtures)
    print(f"Saved {json_path} and {csv_path}")

    per_day = split_by_day(fixtures)
    for day, flist in per_day.items():
        save_json(os.path.join(BY_DAY_DIR, f"{day}.json"), {"date": day, "fixtures": flist})
    print(f"Wrote {len(per_day)} day files to {BY_DAY_DIR}")

if __name__ == "__main__":
    main()
