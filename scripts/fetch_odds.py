#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Generic pre-match odds gatherer — Sportmonks v3
- Works for ANY bookmaker by passing env:
    BOOKMAKER_ID   (e.g. 2=Bet365, 19=Paddy Power, 23=Unibet)
    BOOKMAKER_SLUG (e.g. b365, paddypower, unibet)  [optional; auto from ID]
    BOOKMAKER_NAME (e.g. Bet365, Paddy Power, Unibet) [optional; auto from ID]
- Reads fixture IDs from data/fixtures/{league_id}.json

For each fixture:
  GET /v3/football/odds/pre-match/fixtures/{fixture_id}
    params = { api_token, filter=f"bookmakers:{BOOKMAKER_ID}" }

Writes (per bookmaker slug):
  data/odds/{slug}/fixtures/{fixture_id}.json
  data/odds/{slug}/{league_id}.json
  data/odds/{slug}/by_league/{league_id}.json
  data/odds/{slug}/latest.json
  data/odds/{slug}/odds.txt
"""

import os, sys, json, time, datetime as dt
from pathlib import Path
from typing import Dict, List, Tuple, Optional
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

API_TOKEN = (
    os.getenv("SPORTMONKS_TOKEN")
    or os.getenv("SPORTMONKS_API_TOKEN")
    or os.getenv("SM_TOKEN")
)

# ---- Bookmaker selection (env) ----
def _bm_map():
    return {
        2:  ("b365",       "Bet365"),
        19: ("paddypower", "Paddy Power"),
        23: ("unibet",     "Unibet"),
    }

BOOKMAKER_ID = int(os.getenv("BOOKMAKER_ID", "2"))
BOOKMAKER_SLUG = os.getenv("BOOKMAKER_SLUG") or _bm_map().get(BOOKMAKER_ID, (str(BOOKMAKER_ID),))[0]
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME") or _bm_map().get(BOOKMAKER_ID, (None, f"Bookmaker {BOOKMAKER_ID}"))[1]

# ---- Leagues ----
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()
]

# ---- pacing / paths ----
SLEEP = float(os.getenv("SM_SLEEP", "0.05"))  # seconds between calls
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
OUT_ROOT = ROOT / "data" / "odds" / BOOKMAKER_SLUG
BY_LEAGUE_DIR = OUT_ROOT / "by_league"
PER_FIXTURE_DIR = OUT_ROOT / "fixtures"

def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tmp.replace(path)

def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(path)

def load_fixtures_for_league(league_id: int) -> Dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.exists():
        return {"league_id": league_id, "league_name": str(league_id), "fixtures": []}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def get_odds_for_fixture(fixture_id: int) -> Tuple[List[dict], Optional[str], int]:
    url = f"{API_BASE}/{SPORT}/odds/pre-match/fixtures/{fixture_id}"
    params = {
        "api_token": API_TOKEN,
        "filter": f"bookmakers:{BOOKMAKER_ID}",
    }
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        return [], f"RequestException: {e}", 0

    status = r.status_code
    if status == 200:
        try:
            j = r.json()
        except ValueError:
            return [], f"Non-JSON body: {r.text[:150]}", status
        data = j.get("data", [])
        if not isinstance(data, list):
            return [], f"Unexpected JSON shape: {str(j)[:160]}", status
        return data, None, status

    if status == 204:  # no content yet
        return [], None, status

    return [], f"HTTP {status}: {r.text[:160]}", status

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
        sys.exit(1)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    print(f"Time (UTC): {generated_at}")
    print(f"Bookmaker : {BOOKMAKER_ID} ({BOOKMAKER_NAME})")
    print(f"Output dir: {OUT_ROOT}")
    print(f"Leagues   : {','.join(map(str, LEAGUE_IDS))}")

    leagues_summary = []
    total_fixtures = 0
    total_rows = 0

    for lid in LEAGUE_IDS:
        payload = load_fixtures_for_league(lid)
        league_name = payload.get("league_name") or str(lid)
        fixtures = payload.get("fixtures") or []

        league_rows = []
        for fx in fixtures:
            fid = int(fx.get("id") or fx.get("fixture_id") or 0)
            name = fx.get("name")
            starting_at = fx.get("starting_at")

            rows, err, status = get_odds_for_fixture(fid)

            # Per-fixture save
            write_json(PER_FIXTURE_DIR / f"{fid}.json", {
                "fixture_id": fid,
                "bookmaker_id": BOOKMAKER_ID,
                "requested_at": generated_at,
                "status": status,
                "odds": rows,
                "error": err,
            })

            league_rows.append({
                "fixture_id": fid,
                "league_id": lid,
                "name": name,
                "starting_at": starting_at,
                "bookmaker_id": BOOKMAKER_ID,
                "odds": rows,
                "error": err,
            })
            total_fixtures += 1
            total_rows += len(rows)

            line = f"Fixture {fid} ({name}): {len(rows)} odds rows"
            if status != 200: line += f"  [status {status}]"
            if err: line += f"  [err: {err}]"
            print(line)

            time.sleep(SLEEP)

        league_payload = {
            "league_id": lid,
            "league_name": league_name,
            "generated_at": generated_at,
            "bookmaker_id": BOOKMAKER_ID,
            "fixture_count": len(fixtures),
            "fixtures_with_odds": sum(1 for r in league_rows if r.get("odds")),
            "odds_row_count": sum(len(r.get("odds") or []) for r in league_rows),
            "fixtures": league_rows,
        }
        write_json(OUT_ROOT / f"{lid}.json", league_payload)
        write_json(BY_LEAGUE_DIR / f"{lid}.json", league_payload)

        leagues_summary.append({
            "league_id": lid,
            "league_name": league_name,
            "fixtures": len(fixtures),
            "fixtures_with_odds": league_payload["fixtures_with_odds"],
            "odds_rows": league_payload["odds_row_count"],
        })

    latest = {
        "generated_at": generated_at,
        "bookmaker_id": BOOKMAKER_ID,
        "bookmaker_slug": BOOKMAKER_SLUG,
        "bookmaker_name": BOOKMAKER_NAME,
        "league_ids": LEAGUE_IDS,
        "total_fixtures_seen": total_fixtures,
        "total_odds_rows": total_rows,
        "leagues": leagues_summary,
    }
    write_json(OUT_ROOT / "latest.json", latest)

    # Human-readable summary
    lines = [
        f"Time (UTC): {generated_at}",
        f"Bookmaker : {BOOKMAKER_ID} ({BOOKMAKER_NAME})",
        f"Output dir: {OUT_ROOT.as_posix()}",
        f"Leagues   : {','.join(map(str, LEAGUE_IDS))}",
        f"Fixtures  : {total_fixtures}",
        f"Odds rows : {total_rows}",
        "",
        "Per-league counts:",
    ]
    for s in leagues_summary:
        lines.append(
            f"  - {s['league_id']:>3} ({s['league_name']}): "
            f"fixtures={s['fixtures']} fixtures_with_odds={s['fixtures_with_odds']} odds_rows={s['odds_rows']}"
        )
    write_text(OUT_ROOT / "odds.txt", "\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
