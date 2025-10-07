#!/usr/bin/env python3
import os, sys, time, json, csv, datetime as dt
import requests

TOKEN = os.getenv("SPORTMONKS_TOKEN", "").strip()
if not TOKEN:
    print("[ERROR] SPORTMONKS_TOKEN is not set."); sys.exit(1)

# Comma-separated league IDs to fetch lineups for (default: EPL, Championship, La Liga)
LEAGUE_IDS_ENV = os.getenv("LEAGUE_IDS", "8,9,564").strip()
try:
    TARGET_LEAGUES = {int(x) for x in LEAGUE_IDS_ENV.split(",") if x.strip()}
except ValueError:
    print(f"[ERROR] invalid LEAGUE_IDS env: {LEAGUE_IDS_ENV!r}"); sys.exit(1)

BASE = "https://api.sportmonks.com/v3/football"
HEADERS = {"accept": "application/json"}

OUT_DIR = "data"
os.makedirs(OUT_DIR, exist_ok=True)

SLEEP_BETWEEN_CALLS = float(os.getenv("LINEUPS_SLEEP", "0.7"))  # gentle on rate limits
MAX_RETRIES = 4
FIXTURES_FILE = os.path.join(OUT_DIR, "fixtures_window.json")

def http_get(url, params=None):
    params = dict(params or {})
    params["api_token"] = TOKEN
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=25)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                backoff = (1.8 ** attempt) + 0.4*attempt
                print(f"[WARN] {url} -> {r.status_code}. retry {attempt+1}/{MAX_RETRIES} in {backoff:.1f}s")
                time.sleep(backoff)
                continue
            print(f"[ERROR] {url} -> {r.status_code} {r.text[:180]}")
            return None
        except requests.RequestException as e:
            backoff = (1.8 ** attempt) + 0.4*attempt
            print(f"[NET] {url} exception {e}. retry {attempt+1}/{MAX_RETRIES} in {backoff:.1f}s")
            time.sleep(backoff)
    print(f"[ERROR] exhausted retries: {url}")
    return None

def fetch_fixture_with_lineups(fid):
    url = f"{BASE}/fixtures/{fid}"
    params = {"include": "lineups;lineups.player;participants"}
    data = http_get(url, params)
    time.sleep(SLEEP_BETWEEN_CALLS)
    return data

def normalize_team_side(meta):
    try:
        loc = ((meta or {}).get("location") or "").lower()
    except Exception:
        loc = ""
    return loc if loc in ("home","away") else ""

def extract_xi_rows(payload):
    out = []
    root = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(root, dict):
        return out
    participants_block = root.get("participants") or {}
    participants = participants_block.get("data") if isinstance(participants_block, dict) else []
    team_info = {}
    for p in participants or []:
        team_info[p.get("id")] = {
            "team_id": p.get("id"),
            "team_name": p.get("name"),
            "side": normalize_team_side((p.get("meta") or {})),
        }

    lineups_block = root.get("lineups") or {}
    lineups = lineups_block.get("data") if isinstance(lineups_block, dict) else []
    for lu in lineups or []:
        team_id = lu.get("team_id")
        team = team_info.get(team_id, {})
        # Sportmonks sometimes uses "lineup" list OR players.data
        entries = (lu.get("lineup") or [])
        if not entries and isinstance(lu.get("players"), dict):
            entries = lu["players"].get("data") or []
        for ent in entries:
            player = ent.get("player") or {}
            if isinstance(player, dict) and "data" in player:
                player = player["data"] or {}
            name = (player.get("display_name") or player.get("common_name")
                    or player.get("fullname") or player.get("name") or "")
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
                "player_name": name,
                "position": ent.get("position") or ent.get("formation_position") or ent.get("detailed_position") or "",
                "role": role,
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

def main():
    if not os.path.isfile(FIXTURES_FILE):
        print(f"[ERROR] Missing {FIXTURES_FILE}. Run scripts/fetch_fixtures.py first.")
        sys.exit(1)

    with open(FIXTURES_FILE, "r", encoding="utf-8") as f:
        fx_blob = json.load(f)
    fixtures = fx_blob.get("fixtures") or []
    # Filter to target leagues
    fixtures = [fx for fx in fixtures if (fx.get("league_id") in TARGET_LEAGUES)]
    print(f"[lineups] targeting leagues={sorted(TARGET_LEAGUES)} | fixtures_to_check={len(fixtures)}")

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

    stamp = dt.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    json_path = os.path.join(OUT_DIR, f"lineups_{stamp}.json")
    csv_path = os.path.join(OUT_DIR, f"lineups_{stamp}.csv")

    write_json(json_path, {"generated_at": stamp, "leagues": sorted(TARGET_LEAGUES), "rows": all_rows})
    write_csv(csv_path, all_rows, [
        "fixture_id","fixture_name","starting_at","league_id",
        "team_id","team_name","side","player_id","player_name","position","role"
    ])

    print(f"[lineups] fixtures_checked={checked}, rows_collected={len(all_rows)}")
    print(f"[ok] wrote {json_path}")
    print(f"[ok] wrote {csv_path}")

if __name__ == "__main__":
    main()
