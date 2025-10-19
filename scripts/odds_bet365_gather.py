#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, json, time, datetime as dt
from pathlib import Path
from typing import List, Dict, Optional, Tuple
import requests

API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"

API_TOKEN = (os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN") or os.getenv("SM_TOKEN"))
BOOKMAKER_ID = int(os.getenv("SM_BOOKMAKER_ID", "2"))  # Bet365
DEFAULT_LEAGUES = [301, 384, 387, 564, 567, 600, 8, 82, 9]
LEAGUE_IDS = [int(x) for x in (os.getenv("LEAGUE_IDS") or ",".join(map(str, DEFAULT_LEAGUES))).split(",") if x.strip()]
MARKETS = os.getenv("SM_MARKETS")  # e.g. "1,2,12"
PER_PAGE = int(os.getenv("SM_PER_PAGE", "50"))
TIMEOUT = int(os.getenv("SM_TIMEOUT", "20"))
RETRIES = int(os.getenv("SM_RETRIES", "2"))

# Force which key to use: "auto" (default), "filter", or "filters"
FILTER_KEY_MODE = os.getenv("SM_FILTER_KEY", "auto").strip().lower()

ROOT = Path(".")
FIX_DIR = ROOT / "data" / "fixtures"
OUT_ROOT = ROOT / "data" / "odds" / "b365"
BY_LEAGUE_DIR = OUT_ROOT / "by_league"
PER_FIXTURE_DIR = OUT_ROOT / "fixtures"

def write_json(p: Path, obj: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    tmp.replace(p)

def write_text(p: Path, text: str):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
    tmp.replace(p)

def load_fixtures_for_league(league_id: int) -> Dict:
    p = FIX_DIR / f"{league_id}.json"
    if not p.exists():
        return {"league_id": league_id, "fixtures": [], "league_name": str(league_id)}
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)

def read_pagination_meta(meta: dict) -> Tuple[Optional[int], Optional[bool]]:
    if not isinstance(meta, dict): return (None, None)
    if meta.get("last_page"):
        try: return (int(meta["last_page"]), None)
        except: pass
    pag = (meta.get("pagination") or {})
    pages = None
    if pag.get("total_pages"):
        try: pages = int(pag["total_pages"])
        except: pages = None
    has_more = meta.get("has_more", pag.get("has_more"))
    return (pages, bool(has_more) if has_more is not None else None)

def safe_json(response: requests.Response) -> dict:
    """Always return a dict; include raw text on decode issues."""
    try:
        j = response.json()
        if isinstance(j, dict):
            return j
        # Rarely, API may return a list; wrap it.
        return {"data": j}
    except ValueError:
        txt = response.text[:500] if response and response.text else ""
        return {"_non_json": True, "_status": response.status_code, "_text": txt}

def api_get(path: str, params: dict) -> dict:
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    params = {**params, "api_token": API_TOKEN}
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(0.6 * (attempt + 1)); continue
            r.raise_for_status()
            return safe_json(r)
        except (requests.Timeout, requests.ConnectionError):
            if attempt < RETRIES: time.sleep(0.5 * (attempt + 1)); continue
            raise
        except requests.HTTPError:
            if attempt < RETRIES: time.sleep(0.5 * (attempt + 1)); continue
            raise
    return {}

def build_filter_value() -> str:
    parts = [f"bookmakers:{BOOKMAKER_ID}"]
    if MARKETS: parts.append(f"markets:{MARKETS}")
    return ";".join(parts)

def try_fetch_fixture(fid: int, filter_key: str) -> Tuple[List[dict], dict]:
    """Return (rows, debug_meta)."""
    out: List[dict] = []
    page = 1
    debug_meta = {"attempt_filter_key": filter_key, "pages": 0, "first_page_status": None, "first_page_body": None}
    first = True
    while True:
        j = api_get(f"odds/pre-match/fixtures/{fid}", {
            filter_key: build_filter_value(),
            "per_page": PER_PAGE,
            "order": "asc",
            "page": page
        })
        if first:
            # keep a tiny snapshot for troubleshooting
            debug_meta["first_page_status"] = j.get("_status")
            if j.get("_non_json"):
                debug_meta["first_page_body"] = (j.get("_text") or "")[:180]
        first = False

        rows = j.get("data") or []
        meta = j.get("meta") or {}
        if not isinstance(rows, list):
            # unexpected shape; capture and treat as no rows
            debug_meta["first_page_body"] = debug_meta.get("first_page_body") or str(j)[:180]
            rows = []
        out.extend(rows)
        pages, has_more = read_pagination_meta(meta)
        debug_meta["pages"] = pages or debug_meta["pages"]
        if pages:
            if page >= pages: break
            page += 1
        elif has_more is not None:
            if not has_more: break
            page += 1
        else:
            if len(rows) < PER_PAGE: break
            page += 1
    return out, debug_meta

def autodetect_filter_key(sample_fid: int) -> str:
    if FILTER_KEY_MODE in ("filter", "filters"):
        return FILTER_KEY_MODE
    # Try 'filter' first (matches your working snippet), then 'filters'
    rows, meta = try_fetch_fixture(sample_fid, "filter")
    if rows or (meta.get("first_page_status") not in (204, None)):
        return "filter"
    rows2, meta2 = try_fetch_fixture(sample_fid, "filters")
    return "filters" if (rows2 or (meta2.get("first_page_status") not in (204, None))) else "filter"

def main():
    if not API_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN not set.", file=sys.stderr); sys.exit(1)
    generated_at = dt.datetime.now(dt.timezone.utc).isoformat()

    # Load fixtures and pick a sample for auto-detect
    league_fixtures, league_names, fixture_to_league = {}, {}, {}
    for lid in LEAGUE_IDS:
        payload = load_fixtures_for_league(lid)
        fixtures = payload.get("fixtures") or []
        league_fixtures[lid] = fixtures
        league_names[lid] = payload.get("league_name") or str(lid)
        for fx in fixtures:
            fid = int(fx.get("id")); fixture_to_league[fid] = lid

    if not fixture_to_league:
        print("No fixtures found in data/fixtures — nothing to do."); return

    sample_fid = next(iter(fixture_to_league.keys()))
    chosen_key = autodetect_filter_key(sample_fid)
    print(f"[info] Using filter key: {chosen_key}  (sample fixture {sample_fid})")

    # Replace the simple fetch to force the chosen key for all calls
    def fetch_b365_for_fixture(fid: int) -> Tuple[List[dict], dict]:
        return try_fetch_fixture(fid, chosen_key)

    results: Dict[int, List[dict]] = {}
    errors: Dict[int, str] = {}
    debug_no_odds: Dict[int, dict] = {}

    for i, fid in enumerate(fixture_to_league.keys(), 1):
        try:
            rows, meta = fetch_b365_for_fixture(fid)
            results[fid] = rows
            if not rows:
                debug_no_odds[fid] = meta
            write_json(PER_FIXTURE_DIR / f"{fid}.json", {
                "fixture_id": fid,
                "bookmaker_id": BOOKMAKER_ID,
                "fetched_at": generated_at,
                "filter_key_used": chosen_key,
                "odds": rows,
                "debug": debug_no_odds.get(fid)
            })
        except Exception as e:
            errors[fid] = f"{type(e).__name__}: {e}"
        time.sleep(0.12)  # gentle pacing

    leagues_summary = []
    total_rows = total_fixtures = 0
    for lid in LEAGUE_IDS:
        fixtures = league_fixtures.get(lid, [])
        league_name = league_names.get(lid, str(lid))
        league_rows = []
        for fx in fixtures:
            fid = int(fx.get("id"))
            league_rows.append({
                "fixture_id": fid,
                "name": fx.get("name"),
                "starting_at": fx.get("starting_at"),
                "league_id": lid,
                "bookmaker_id": BOOKMAKER_ID,
                "odds": results.get(fid, []),
                "error": errors.get(fid)
            })
        payload = {
            "league_id": lid,
            "league_name": league_name,
            "generated_at": generated_at,
            "bookmaker_id": BOOKMAKER_ID,
            "fixture_count": len(fixtures),
            "fixtures_with_odds": sum(1 for r in league_rows if r.get("odds")),
            "odds_row_count": sum(len(r.get("odds") or []) for r in league_rows),
            "fixtures": league_rows
        }
        total_fixtures += len(fixtures)
        total_rows += payload["odds_row_count"]
        write_json(OUT_ROOT / f"{lid}.json", payload)
        write_json(BY_LEAGUE_DIR / f"{lid}.json", payload)
        leagues_summary.append({
            "league_id": lid, "league_name": league_name,
            "fixtures": len(fixtures),
            "fixtures_with_odds": payload["fixtures_with_odds"],
            "odds_rows": payload["odds_row_count"]
        })

    latest = {
        "generated_at": generated_at,
        "bookmaker_id": BOOKMAKER_ID,
        "league_ids": LEAGUE_IDS,
        "total_fixtures_seen": total_fixtures,
        "total_odds_rows": total_rows,
        "errors": errors,
        "leagues": leagues_summary,
        "filter_value": build_filter_value(),
        "filter_key_used": chosen_key
    }
    write_json(OUT_ROOT / "latest.json", latest)

    lines = [
        f"Time (UTC): {generated_at}",
        f"Bookmaker : {BOOKMAKER_ID}",
        f"Leagues   : {','.join(map(str, LEAGUE_IDS))}",
        f"Fixtures  : {total_fixtures}",
        f"Odds rows : {total_rows}",
        "", "Per-league counts:"
    ]
    for s in leagues_summary:
        lines.append(f"  - {s['league_id']:>3} ({s['league_name']}): "
                     f"fixtures={s['fixtures']} fixtures_with_odds={s['fixtures_with_odds']} odds_rows={s['odds_rows']}")
    if errors:
        lines.append(""); lines.append("Errors:")
        for fid, msg in list(errors.items())[:25]:
            lines.append(f"  fixture {fid}: {msg}")
    write_text(OUT_ROOT / "odds.txt", "\n".join(lines))
    print("\n".join(lines))

if __name__ == "__main__":
    main()
