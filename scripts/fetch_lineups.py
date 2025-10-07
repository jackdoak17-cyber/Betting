#!/usr/bin/env python3
import os, sys, time, json, csv, datetime as dt
from urllib.parse import urlencode
import requests

# -------------------------
# Config
# -------------------------
TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not TOKEN:
    print("[ERROR] SPORTMONKS_TOKEN is not set.")
    sys.exit(1)

# Keep this list in sync with your fixtures step (you added Eredivisie=72 and Superliga=271)
LEAGUE_IDS = [8, 9, 82, 301, 564, 567, 384, 387, 600, 72, 271]

DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "5"))   # you set to 5
BASE = "https://api.sportmonks.com/v3/football"
HEADERS = {"accept": "application/json"}

OUT_DIR = "data"
os.makedirs(OUT_DIR, exist_ok=True)

# polite rate-limit settings
SLEEP_BETWEEN_CALLS = 0.6   # seconds between API requests
MAX_RETRIES = 4

# -------------------------
# Helpers
# -------------------------
def backoff_sleep(attempt):
    # exponential with jitter
    t = (1.6 ** attempt) + (0.15 * attempt)
    time.sleep(t)

def http_get(url, params=None):
    if params is None:
        params = {}
    params["api_token"] = TOKEN
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                print(f"[WARN] {url} -> {r.status_code}. retry {attempt+1}/{MAX_RETRIES}")
                backoff_sleep(attempt)
                continue
            # hard error
            print(f"[ERROR] {url} -> {r.status_code} {r.text[:180]}")
            return None
        except requests.RequestException as e:
            print(f"[NET] {url} exception: {e}. retry {attempt+1}/{MAX_RETRIES}")
            backoff_sleep(attempt)
    print(f"[ERROR] exhausted retries: {url}")
    return None

def today_utc_date_str():
    return dt.datetime.utcnow().date().isoformat()

def date_plus_days_str(days):
    return (dt.datetime.utcnow().date() + dt.timedelta(days=days)).isoformat()

def list_fixtures_between(start_date, end_date, league_ids):
    """Return a list of fixtures (basic fields) for the window."""
    all_fx = []
    for lid in league_ids:
        url = f"{BASE}/fixtures/between/{start_date}/{end_date}"
        params = {"filters": f"league_id:{lid}", "include": "participants"}
        data = http_get(url, params=params)
        time.sleep(SLEEP_BETWEEN_CALLS)
        if not data or not isinstance(data, dict) or "data" not in data:
            continue
        for fx in data.get("data", []):
            # compact record
            all_fx.append({
                "id": fx.get("id"),
                "league_id": lid,
                "league_name": (fx.get("league", {}) or {}).get("name") or fx.get("league_name"),
                "starting_at": fx.get("starting_at"),
                "name": fx.get("name"),
                "participants": fx.get("participants"),
                "state": fx.get("state"),
            })
    return all_fx

def fetch_fixture_with_lineups(fid):
    """Get full fixture with lineups & players attached."""
    url = f"{BASE}/fixtures/{fid}"
    params = {"include": "lineups;lineups.player;participants"}
    data = http_get(url, params=params)
    time.sleep(SLEEP_BETWEEN_CALLS)
    return data

def normalize_team_side(meta):
    # Sportmonks adds meta.location: "home"/"away"
    try:
        loc = ((meta or {}).get("location") or "").lower()
    except:
        loc = ""
    return loc if loc in ("home", "away") else ""

def extract_xi_rows(fx_payload):
    """Return a list of dicts for starters + bench with player names, team, side, role."""
    out = []
    root = fx_payload.get("data") if isinstance(fx_payload, dict) else None
    if not isinstance(root, dict):
        return out

    participants = (root.get("participants") or {}).get("data") or []
    team_id_to = {}
    for p in participants:
        team_id_to[p.get("id")] = {
            "team_id": p.get("id"),
            "team_name": p.get("name"),
            "side": normalize_team_side((p.get("meta") or {})),
        }

    lineups = (root.get("lineups") or {}).get("data") or []
    for lu in lineups:
        team_id = lu.get("team_id")
        team = team_id_to.get(team_id, {})
        entries = (lu.get("lineup") or [])  # sometimes Sportmonks nests players under lineup[]; sometimes on player directly
        # Some payloads put players in lu["players"]["data"] with role flags; we’ll defensively check both.
        if not entries and isinstance(lu.get("players"), dict):
            entries = (lu["players"].get("data") or [])

        for ent in entries:
            # try common fields
            player = ent.get("player") or {}
            if isinstance(player, dict) and "data" in player:
                player = player["data"] or {}
            player_name = player.get("display_name") or player.get("common_name") or player.get("fullname") or player.get("name") or ""
            pos = ent.get("position") or ent.get("formation_position") or ent.get("detailed_position") or ""
            is_starter = bool(ent.get("type") == "starting") or bool(ent.get("starter") is True) or bool(ent.get("is_starting") is True)
            on_bench = bool(ent.get("type") == "bench") or bool(ent.get("bench") is True)

            role = "starter" if is_starter else ("bench" if on_bench else "unknown")

            out.append({
                "fixture_id": root.get("id"),
                "fixture_name": root.get("name"),
                "starting_at": root.get("starting_at"),
                "league_id": root.get("league_id"),
                "team_id": team.get("team_id"),
                "team_name": team.get("team_name"),
                "side": team.get("side"),
                "player_id": player.get("id"),
                "player_name": player_name,
                "position": pos,
                "role": role
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

# -------------------------
# Main
# -------------------------
def main():
    start = today_utc_date_str()
    end = date_plus_days_str(DAYS_AHEAD)
    print(f"Fetching fixtures for {start} → {end} (next {DAYS_AHEAD} days)…")

    fixtures = list_fixtures_between(start, end, LEAGUE_IDS)
    print(f"[fixtures] got: {len(fixtures)}")

    # Save the raw fixture list for reference
    fixtures_out = {
        "generated_at": today_utc_date_str(),
        "fixtures": fixtures
    }
    write_json(os.path.join(OUT_DIR, "fixtures_window.json"), fixtures_out)

    if not fixtures:
        print("[result] no fixtures in window; done.")
        return

    all_rows = []
    checked = 0
    for fx in fixtures:
        fid = fx.get("id")
        if not fid:
            continue
        checked += 1
        payload = fetch_fixture_with_lineups(fid)
        if not payload:
            continue
        rows = extract_xi_rows(payload)
        if rows:
            all_rows.extend(rows)
        # light throttle already applied in fetch call

    print(f"[lineups] fixtures checked: {checked}, rows collected: {len(all_rows)}")

    # Output files
    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    json_path = os.path.join(OUT_DIR, f"lineups_{stamp}.json")
    csv_path = os.path.join(OUT_DIR, f"lineups_{stamp}.csv")

    write_json(json_path, {"generated_at": stamp, "rows": all_rows})
    write_csv(csv_path, all_rows, [
        "fixture_id","fixture_name","starting_at","league_id",
        "team_id","team_name","side",
        "player_id","player_name","position","role"
    ])

    print(f"[ok] wrote {json_path}")
    print(f"[ok] wrote {csv_path}")

if __name__ == "__main__":
    main()
