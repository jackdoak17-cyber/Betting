#!/usr/bin/env python3
"""
Fetch fixtures for a date window from Sportmonks v3 and save:
- artifacts/fixtures_window.json  (clean, merged list)
- artifacts/fixtures_window.csv   (simple CSV)
- artifacts/debug/raw_league_<LEAGUEID>.json  (raw payloads per league & page)
- artifacts/debug/errors.json     (any API errors captured)

Env:
  SPORTMONKS_TOKEN   (required)
  DAYS_AHEAD         (default 14)
  DAYS_BACK          (default 0)
  LEAGUE_IDS         (optional CSV, overrides default set)

CLI (optional overrides):
  python scripts/fetch_fixtures.py --days 14 --back 0 --leagues 8,9,564
"""

import os, sys, json, csv, time, argparse, datetime as dt
import requests
from urllib.parse import urlencode

API = "https://api.sportmonks.com/v3/football"
TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()

DEFAULT_LEAGUES = [
    8,   # Premier League
    9,   # Championship
    82,  # Bundesliga
    301, # Ligue 1
    564, # La Liga
    567, # La Liga 2
    384, # Serie A
    387, # Serie B
    600, # Süper Lig
    72,  # Eredivisie
    271, # Superliga
]

def iso_date(d: dt.date) -> str:
    return d.strftime("%Y-%m-%d")

def today_utc_date() -> dt.date:
    return dt.datetime.utcnow().date()

def ensure_dirs():
    os.makedirs("artifacts/debug", exist_ok=True)

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=int(os.getenv("DAYS_AHEAD", "14")))
    p.add_argument("--back", type=int, default=int(os.getenv("DAYS_BACK", "0")))
    p.add_argument("--leagues", type=str, default=os.getenv("LEAGUE_IDS", ""))
    return p.parse_args()

def get_league_list(arg_leagues: str):
    if arg_leagues:
        try:
            return [int(x) for x in arg_leagues.split(",") if x.strip()]
        except ValueError:
            pass
    return DEFAULT_LEAGUES[:]

def request_with_retry(url, params, max_retries=4):
    """Basic retry with 429/backoff and network errors."""
    headers = {"accept": "application/json"}
    attempt = 0
    while True:
        try:
            r = requests.get(url, params=params, timeout=30, headers=headers)
            rl = r.headers.get("X-RateLimit-Remaining")
            rr = r.headers.get("Retry-After")
            print(f"[HTTP] {r.status_code} {r.url}  (X-Remain={rl}, Retry-After={rr})")
            if r.status_code == 200:
                return r
            if r.status_code in (429, 500, 502, 503, 504) and attempt < max_retries:
                wait = int(rr) if rr and rr.isdigit() else (2 ** attempt + 1)
                print(f"[RETRY] sleeping {wait}s…")
                time.sleep(wait)
                attempt += 1
                continue
            return r
        except requests.RequestException as e:
            if attempt >= max_retries:
                print(f"[NET-FAIL] {e}")
                raise
            wait = 2 ** attempt + 1
            print(f"[NET] {e} → retry in {wait}s")
            time.sleep(wait)
            attempt += 1

def fetch_fixtures_between(from_date: str, to_date: str, league_id: int, errors: list):
    """
    Calls: /fixtures/between/{from}/{to}
    Filters by single league each time (reliable) and paginates.
    Returns combined list of fixtures.
    Also writes raw per-page JSON to artifacts/debug/raw_league_<id>_p<page>.json
    """
    out = []
    page = 1
    while True:
        params = {
            "api_token": TOKEN,
            "include": "participants",   # teams (home/away)
            "page": page,
            "per_page": 100,
            "tz": "UTC",
            "leagues": league_id,        # filter by league
        }
        url = f"{API}/fixtures/between/{from_date}/{to_date}"
        r = request_with_retry(url, params)
        raw_name = f"artifacts/debug/raw_league_{league_id}_p{page}.json"
        try:
            payload = r.json()
        except Exception:
            payload = {"_parse_error": True, "_text": r.text[:4000]}
        with open(raw_name, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

        if r.status_code != 200:
            errors.append({
                "league": league_id,
                "page": page,
                "status": r.status_code,
                "text_preview": r.text[:400],
            })
            break

        data = payload.get("data")
        if isinstance(data, list):
            out.extend(data)
        elif data is None:
            # Some errors put message in 'message'
            msg = payload.get("message", "no data field")
            errors.append({"league": league_id, "page": page, "status": 200, "message": msg})
            break
        else:
            # Unknown shape
            errors.append({"league": league_id, "page": page, "status": 200, "message": "unexpected data shape"})
            break

        # pagination
        meta = payload.get("meta") or {}
        pag = meta.get("pagination") or {}
        cur = pag.get("current_page")
        tot = pag.get("total_pages")
        if cur and tot and int(cur) < int(tot):
            page += 1
            continue
        # Also handle "links" style next
        links = meta.get("links") or {}
        if links.get("next"):
            page += 1
            continue
        break
    return out

def flatten_fixture_row(fx: dict):
    # build a simple CSV row
    fid = fx.get("id")
    lid = fx.get("league_id")
    lname = fx.get("league", {}).get("name") or fx.get("league_name") or ""
    start = fx.get("starting_at") or fx.get("time", {}).get("starting_at")
    name = fx.get("name", "")
    # participants come either in data list or on fx["participants"]
    parts = fx.get("participants") or []
    home, away = "", ""
    for p in parts:
        loc = (p.get("meta") or {}).get("location")
        if loc == "home":
            home = p.get("name") or ""
        elif loc == "away":
            away = p.get("name") or ""
    return {
        "fixture_id": fid,
        "league_id": lid,
        "league_name": lname,
        "starting_at": start,
        "home": home,
        "away": away,
        "name": name,
    }

def main():
    if not TOKEN:
        print("ERROR: SPORTMONKS_TOKEN is not set in env.")
        sys.exit(1)

    ensure_dirs()
    args = parse_args()

    leagues = get_league_list(args.leagues)
    today = today_utc_date()
    start_date = iso_date(today - dt.timedelta(days=args.back))
    end_date = iso_date(today + dt.timedelta(days=args.days))

    print(f"Window: {start_date} → {end_date}  (leagues={leagues})")

    errors = []
    merged = []
    for lid in leagues:
        try:
            fixtures = fetch_fixtures_between(start_date, end_date, league_id=lid, errors=errors)
            print(f"[LEAGUE {lid}] fixtures: {len(fixtures)}")
            merged.extend(fixtures)
        except Exception as e:
            msg = str(e)
            print(f"[ERROR] league {lid}: {msg}")
            errors.append({"league": lid, "exception": msg})

    # Dedup by fixture id
    seen = set()
    uniq = []
    for fx in merged:
        fid = fx.get("id")
        if fid and fid not in seen:
            uniq.append(fx)
            seen.add(fid)

    # Write clean JSON
    clean = {
        "generated_at": dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ"),
        "window_start": start_date,
        "window_end": end_date,
        "fixtures": uniq,
        "leagues": leagues,
    }
    with open("artifacts/fixtures_window.json", "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)

    # Write CSV
    fieldnames = ["fixture_id", "league_id", "league_name", "starting_at", "home", "away", "name"]
    with open("artifacts/fixtures_window.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for fx in uniq:
            w.writerow(flatten_fixture_row(fx))

    # Write errors (if any)
    if errors:
        with open("artifacts/debug/errors.json", "w", encoding="utf-8") as f:
            json.dump(errors, f, ensure_ascii=False, indent=2)
        print(f"[DONE] fixtures={len(uniq)}  (with errors; see artifacts/debug/errors.json)")
    else:
        print(f"[DONE] fixtures={len(uniq)}")
        
if __name__ == "__main__":
    main()
