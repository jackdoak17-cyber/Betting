#!/usr/bin/env python3
import os, sys, argparse, requests
from datetime import datetime

BASE = "https://api.sportmonks.com/v3/football"

def token():
    t = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN")
    if not t:
        print("Error: set SPORTMONKS_TOKEN (or SPORTMONKS_API_TOKEN).", file=sys.stderr)
        sys.exit(0)  # don't fail CI
    return t

def api_get(path, params=None, timeout=20):
    params = dict(params or {})
    params.setdefault("api_token", token())
    url = f"{BASE}/{path.lstrip('/')}"
    r = requests.get(url, params=params, timeout=timeout)
    if r.status_code in (403,404):
        return {"data": None, "_status": r.status_code, "_body": r.text}
    try:
        r.raise_for_status()
    except requests.HTTPError:
        return {"data": None, "_status": r.status_code, "_body": r.text}
    try:
        return r.json()
    except Exception:
        return {"data": None, "_status": "non-json", "_body": r.text[:500]}

def parse_date(s):
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.min

def fetch_league_with_seasons(league_id: int):
    data = api_get(f"leagues/{league_id}", params={"include": "seasons;currentSeason"})
    if data.get("data"):
        return data
    seasons = api_get("seasons", params={"filter": f"league_id:{league_id}", "per_page": 200})
    league  = api_get(f"leagues/{league_id}")
    current = api_get(f"leagues/{league_id}", params={"include": "currentSeason"})
    return {
        "data": {
            **(league.get("data") or {}),
            "seasons": seasons.get("data") or [],
            "currentSeason": (current.get("data") or {}).get("currentSeason")
        }
    }

def normalize_seasons(obj):
    league_name, current_id = "", None
    if not obj or not obj.get("data"): return league_name, [], current_id
    d = obj["data"]
    league_name = d.get("name") or f"League {d.get('id')}"
    cur = d.get("currentSeason")
    if isinstance(cur, dict): current_id = cur.get("id")
    seasons = d.get("seasons") or []
    if isinstance(seasons, dict) and "data" in seasons: seasons = seasons["data"]
    seasons = list(seasons) if isinstance(seasons, list) else []
    seasons.sort(key=lambda s: parse_date(s.get("starting_at","")), reverse=True)
    return league_name, seasons, current_id

def main():
    ap = argparse.ArgumentParser(description="Print season IDs for leagues (no guessing).")
    ap.add_argument("--league-ids", type=int, nargs="+", default=[8])
    ap.add_argument("--current-only", action="store_true")
    ap.add_argument("--ids-only", action="store_true")
    args = ap.parse_args()

    any_output = False
    for lid in args.league_ids:
        data = fetch_league_with_seasons(lid)
        league_name, seasons, current_id = normalize_seasons(data)

        if args.current_only:
            if current_id:
                if args.ids_only:
                    print(f"{lid}:{current_id}")
                else:
                    label = next((s.get("name") for s in seasons if s.get("id")==current_id), "")
                    print(f"{league_name} (league_id={lid}) — current: {current_id}" + (f" [{label}]" if label else ""))
                any_output = True
            else:
                print(f"{league_name or f'League {lid}'} (league_id={lid}) — current season not found / not in plan.")
            continue

        if not seasons:
            print(f"{league_name or f'League {lid}'} (league_id={lid}) — no seasons found.")
            continue

        any_output = True
        if args.ids_only:
            print(f"{lid}:{','.join(str(s.get('id')) for s in seasons if s.get('id'))}")
        else:
            print(f"{league_name} (league_id={lid}) — seasons (newest first):")
            for s in seasons:
                sid = s.get("id"); name = s.get("name","")
                sa = s.get("starting_at",""); ea = s.get("ending_at","")
                star = " (current)" if current_id and sid == current_id else ""
                print(f"  - {sid}: {name} [{sa} → {ea}]{star}")
            print()

    if not any_output:
        print("[INFO] No seasons printed (check token/plan/league IDs).")

if __name__ == "__main__":
    main()
