#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Generic pre-match odds fetcher (Sportmonks v3) — parameterized by bookmaker.

Reads fixture IDs from data/fixtures/{league_id}.json (same leagues you use).
For each fixture:
  GET /v3/football/odds/pre-match/fixtures/{fixture_id}
    params = { api_token, [filter=f"bookmakers:{BOOKMAKER_ID}"] }

ENV:
  SPORTMONKS_TOKEN   (required)
  LEAGUE_IDS         e.g. "301,384,..." (defaults to the usual 9)
  BOOKMAKER_ID       numeric id (e.g. 2=Bet365, 23=Unibet, ???=Paddy Power)
  BOOKMAKER_NAME     optional label for logs
  OUT_DIR            output root, e.g. data/odds/unibet
  SM_SLEEP           seconds between calls (default 0.05)
  SM_TIMEOUT         request timeout (default 20)
  DEBUG_BOOKMAKERS   "1" to also fetch unfiltered rows when empty and print which bookmaker_ids ARE present.

Writes:
  {OUT_DIR}/{league_id}.json
  {OUT_DIR}/by_league/{league_id}.json
  {OUT_DIR}/fixtures/{fixture_id}.json
  {OUT_DIR}/latest.json
  {OUT_DIR}/odds.txt
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

# Inputs
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [
    int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()
]

BOOKMAKER_ID = os.getenv("BOOKMAKER_ID") or os.getenv("SM_BOOKMAKER_ID") or ""
BOOKMAKER_NAME = os.getenv("BOOKMAKER_NAME") or ""
DEBUG_BOOKMAKERS = os.getenv("DEBUG_BOOKMAKERS", "0") == "1"

SLEEP = float(os.getenv("SM_SLEEP", "0.05"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"

OUT_ROOT = Path(os.getenv("OUT_DIR") or "data/odds/unknown")
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

def api_get(url: str, params: dict) -> Tuple[int, Optional[dict], Optional[str]]:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT)
    except requests.RequestException as e:
        return 0, None, f"RequestException: {e}"
    try:
        js = r.json() if r.status_code == 200 else None
    except ValueError:
        js = None
    err = None if r.status_code == 200 else (r.text[:200] if r.text else f"HTTP {r.status_code}")
    return r.status_code, js, err

def get_fixture_odds_filtered(fixture_id: int, bookmaker_id: Optional[int]) -> Tuple[List[dict], Optional[str], int]:
    url = f"{API_BASE}/{SPORT}/odds/pre-match/fixtures/{fixture_id}"
    params = { "api_token": API_TOKEN }
    if bookmaker_id:
        params["filter"] = f"bookmakers:{bookmaker_id}"
    status, js, err = api_get(url, params)
    if status == 200:
        data = js.get("data", [])
        if not isinstance(data, list):
            return [], f"Unexpected JSON shape: {str(js)[:160]}", status
        return data, None, status
    if status == 204:
        return [], None, status
    return [], err or f"HTTP {status}", status

def extract_present_bookmakers(rows: List[dict]) -> List[Dict[str, Optional[str]]]:
    # Try to glean bookmaker info from each row if present
    seen = {}
    for r in rows:
        bid = r.get("bookmaker_id")
        name = r.get("bookmaker_name") or r.get("bookmaker") or None
        if bid is None:
            continue
        key = str(bid)
        if key not in seen:
            seen[key] = {"bookmaker_id": bid, "bookmaker_name": name}
    # sorted by id for stable logs
    return sorted(seen.values(), key=lambda x: int(x["bookmaker_id"]))

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr)
        sys.exit(1)
    if not BOOKMAKER_ID:
        print("ERROR: BOOKMAKER_ID not set.", file=sys.stderr)
        sys.exit(1)
    try:
        bid_int = int(str(BOOKMAKER_ID).strip())
    except Exception:
        print(f"ERROR: BOOKMAKER_ID must be numeric, got '{BOOKMAKER_ID}'", file=sys.stderr)
        sys.exit(1)

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    PER_FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    BY_LEAGUE_DIR.mkdir(parents=True, exist_ok=True)

    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()
    label = f"{BOOKMAKER_NAME or ''}".strip()
    if label:
        print(f"Time (UTC): {generated_at}\nBookmaker : {bid_int} ({label})\nOutput dir: {OUT_ROOT}\nLeagues   : {','.join(map(str, LEAGUE_IDS))}")
    else:
        print(f"Time (UTC): {generated_at}\nBookmaker : {bid_int}\nOutput dir: {OUT_ROOT}\nLeagues   : {','.join(map(str, LEAGUE_IDS))}")

    leagues_summary = []
    total_fixtures = 0
    total_rows = 0

    for lid in LEAGUE_IDS:
        payload = load_fixtures_for_league(lid)
        league_name = payload.get("league_name") or str(lid)
        fixtures = payload.get("fixtures") or []

        league_rows = []
        for fx in fixtures:
            fid = int(fx.get("id"))
            name = fx.get("name")
            starting_at = fx.get("starting_at")

            rows, err, status = get_fixture_odds_filtered(fid, bid_int)

            extra_debug = {}
            if DEBUG_BOOKMAKERS and (status != 200 or not rows):
                # fetch unfiltered to see who *is* present
                all_rows, _, st2 = get_fixture_odds_filtered(fid, None)
                present = extract_present_bookmakers(all_rows)
                if present:
                    ids = ", ".join([f"{p['bookmaker_id']}{' '+p['bookmaker_name'] if p.get('bookmaker_name') else ''}" for p in present])
                else:
                    ids = "—"
                extra_debug["present_bookmakers"] = present
                print(f"Fixture {fid} ({name}): {len(rows)} odds rows  [status {status}]  present bookmakers: {ids}")
            else:
                print(f"Fixture {fid} ({name}): {len(rows)} odds rows")

            # Per-fixture save
            out_fixture = {
                "fixture_id": fid,
                "bookmaker_id": bid_int,
                "bookmaker_label": label or None,
                "requested_at": generated_at,
                "status": status,
                "odds": rows,
                "error": err,
            }
            if extra_debug:
                out_fixture.update(extra_debug)
            write_json(PER_FIXTURE_DIR / f"{fid}.json", out_fixture)

            league_rows.append({
                "fixture_id": fid,
                "league_id": lid,
                "name": name,
                "starting_at": starting_at,
                "bookmaker_id": bid_int,
                "odds": rows,
                "error": err,
            })
            total_fixtures += 1
            total_rows += len(rows)
            time.sleep(SLEEP)

        league_payload = {
            "league_id": lid,
            "league_name": league_name,
            "generated_at": generated_at,
            "bookmaker_id": bid_int,
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
        "bookmaker_id": bid_int,
        "bookmaker_label": label or None,
        "league_ids": LEAGUE_IDS,
        "total_fixtures_seen": total_fixtures,
        "total_odds_rows": total_rows,
        "leagues": leagues_summary,
    }
    write_json(OUT_ROOT / "latest.json", latest)

    # Human-readable summary
    lines = [
        f"Time (UTC): {generated_at}",
        f"Bookmaker : {bid_int}{(' ('+label+')') if label else ''}",
        f"Output dir: {OUT_ROOT}",
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
    try:
        main()
    except KeyboardInterrupt:
        pass
