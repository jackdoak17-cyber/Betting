#!/usr/bin/env python3
import os, sys, json, csv, time, datetime as dt
from collections import defaultdict
import requests

# ===================== Config =====================
TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not TOKEN:
    print("[ERROR] SPORTMONKS_TOKEN not set."); sys.exit(1)

# Keep in sync with your project list
LEAGUE_IDS = [8, 9, 82, 301, 564, 567, 384, 387, 600, 72, 271]  # EPL, Champ, Bundesliga, L1, LaLiga, LaLiga2, Serie A, B, Super Lig, Eredivisie, Superliga
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "14"))

BASE = "https://api.sportmonks.com/v3/football"
HEADERS = {"accept": "application/json"}

OUT_DIR = "data"
PER_DAY_DIR = os.path.join(OUT_DIR, "fixtures_by_day")
os.makedirs(PER_DAY_DIR, exist_ok=True)

# Rate-limit politeness
SLEEP_BETWEEN_CALLS = 0.6
MAX_RETRIES = 4

# ===================== Helpers =====================
def today_utc():
    return dt.datetime.utcnow().date()

def date_str(d):
    return d.isoformat()

def http_get(url, params):
    params = dict(params or {})
    params["api_token"] = TOKEN
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                wait = (1.6 ** attempt) + (attempt * 0.2)
                print(f"[WARN] {url} -> {r.status_code}. retry {attempt+1}/{MAX_RETRIES} in {wait:.1f}s")
                time.sleep(wait)
                continue
            print(f"[ERROR] {url} -> {r.status_code} {r.text[:180]}")
            return None
        except requests.RequestException as e:
            wait = (1.6 ** attempt) + (attempt * 0.2)
            print(f"[NET] {url} exception {e}. retry {attempt+1}/{MAX_RETRIES} in {wait:.1f}s")
            time.sleep(wait)
    print(f"[ERROR] exhausted retries for {url}")
    return None

def list_fixtures_on_day(league_id, d):
    """Fetch fixtures for one league on a given date (inclusive day)."""
    # Using 'between' with same start/end captures that date; avoids pagination traps.
    url = f"{BASE}/fixtures/between/{date_str(d)} 00:00:00/{date_str(d)} 23:59:59"
    params = {
        "filters": f"league_id:{league_id}",
        "include": "participants,league",
    }
    data = http_get(url, params)
    time.sleep(SLEEP_BETWEEN_CALLS)
    if not data or "data" not in data:
        return []

    out = []
    for fx in data["data"]:
        out.append({
            "id": fx.get("id"),
            "league_id": (fx.get("league_id") or league_id),
            "league_name": ((fx.get("league") or {}).get("name") if isinstance(fx.get("league"), dict) else None)
                           or fx.get("league_name"),
            "starting_at": fx.get("starting_at"),
            "name": fx.get("name"),
            "participants": fx.get("participants"),
            "state": fx.get("state"),
        })
    return out

def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

def write_csv(path, rows, fieldnames):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in fieldnames})

# ===================== Main =====================
def main():
    start = today_utc()
    end = start + dt.timedelta(days=DAYS_AHEAD)
    print(f"Fetching fixtures per-league per-day for {date_str(start)} → {date_str(end)} ({DAYS_AHEAD} days)…")

    dedup = {}  # fixture_id -> record
    per_day_map = defaultdict(list)

    day = start
    total_calls = 0
    while day < end:
        for lid in LEAGUE_IDS:
            rows = list_fixtures_on_day(lid, day)
            total_calls += 1
            if rows:
                per_day_map[date_str(day)].extend(rows)
                for r in rows:
                    fid = r.get("id")
                    if fid:
                        dedup[fid] = r
        day += dt.timedelta(days=1)

    # Write per-day files (helpful to split later by workflow if needed)
    for dkey, rows in per_day_map.items():
        write_json(os.path.join(PER_DAY_DIR, f"{dkey}.json"),
                   {"date": dkey, "fixtures": rows})

    # Write combined JSON + CSV
    combined = list(dedup.values())
    combined.sort(key=lambda x: (x.get("starting_at") or "", x.get("league_id") or 0, x.get("id") or 0))

    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    combined_json = {
        "generated_at": stamp,
        "window_start": date_str(start),
        "window_end": date_str(end),
        "fixtures": combined,
        "leagues": LEAGUE_IDS,
    }
    write_json(os.path.join(OUT_DIR, "fixtures_window.json"), combined_json)

    csv_rows = []
    for r in combined:
        csv_rows.append({
            "fixture_id": r.get("id"),
            "league_id": r.get("league_id"),
            "league_name": r.get("league_name"),
            "starting_at": r.get("starting_at"),
            "name": r.get("name"),
        })
    write_csv(os.path.join(OUT_DIR, "fixtures_window.csv"),
              csv_rows,
              ["fixture_id","league_id","league_name","starting_at","name"])

    print(f"[OK] total_api_calls={total_calls}, fixtures_kept={len(combined)}")
    print(f"[OK] wrote data/fixtures_window.json and data/fixtures_window.csv")
    print(f"[OK] wrote {len(per_day_map)} per-day files in data/fixtures_by_day/")

if __name__ == "__main__":
    main()
