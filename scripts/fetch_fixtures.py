#!/usr/bin/env python3
"""
Fetch upcoming fixtures from SportMonks and write JSON files to the repo.

Env vars:
  SPORTMONKS_TOKEN  (required)  -> your API token (set as a repo secret)
  LEAGUE_IDS        (optional)  -> comma-separated league IDs to include
  DAYS_AHEAD        (optional)  -> number of days ahead to fetch (max 100, default 21)
  START_DATE        (optional)  -> YYYY-MM-DD; defaults to today (UTC)
  OUT_DIR           (optional)  -> output directory (default: data/fixtures)

Outputs:
  - {OUT_DIR}/latest.json                          -> all upcoming fixtures (combined)
  - {OUT_DIR}/by_league/{league_id}.json           -> upcoming fixtures per league
  - {OUT_DIR}/summary.txt                          -> small human-readable summary
"""
import os
import sys
import json
import time
import pathlib
from typing import Dict, List, Any
from datetime import datetime, timedelta, timezone

import requests

API_BASE = "https://api.sportmonks.com/v3/football/fixtures/between/{start}/{end}"

# Default leagues (your list)
DEFAULT_LEAGUES = "8,9,384,387,82,301,564,567,600"


def utc_today_str() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def clamp_days(n: int, lo: int, hi: int) -> int:
    return max(lo, min(n, hi))


def parse_env() -> Dict[str, Any]:
    token = os.getenv("SPORTMONKS_TOKEN", "").strip()
    if not token:
        print("ERROR: SPORTMONKS_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    leagues_csv = os.getenv("LEAGUE_IDS", DEFAULT_LEAGUES)
    league_ids = [x.strip() for x in leagues_csv.split(",") if x.strip()]

    days_ahead = clamp_days(int(os.getenv("DAYS_AHEAD", "21")), 1, 100)

    start_date = os.getenv("START_DATE", utc_today_str())
    # end_date is inclusive in SportMonks date-path style; keep within 100 days
    end_date_dt = datetime.fromisoformat(start_date) + timedelta(days=days_ahead)
    end_date = end_date_dt.date().isoformat()

    out_dir = os.getenv("OUT_DIR", "data/fixtures")

    cfg = {
        "token": token,
        "league_ids": league_ids,
        "leagues_csv": ",".join(league_ids),
        "days_ahead": days_ahead,
        "start_date": start_date,
        "end_date": end_date,
        "out_dir": out_dir,
        "per_page": 50,
    }
    return cfg


def safe_makedirs(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def request_with_retry(url: str, params: Dict[str, Any], max_tries: int = 5) -> requests.Response:
    """Basic retry with 429 handling and backoff."""
    backoff = 2
    for attempt in range(1, max_tries + 1):
        resp = requests.get(url, params=params, timeout=60)
        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", str(backoff)))
            time.sleep(retry_after)
            backoff = min(backoff * 2, 60)
            continue
        if 200 <= resp.status_code < 300:
            return resp
        # transient 5xx
        if 500 <= resp.status_code < 600 and attempt < max_tries:
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue
        # give up
        resp.raise_for_status()
    return resp  # type: ignore


def fetch_all_pages(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = API_BASE.format(start=cfg["start_date"], end=cfg["end_date"])
    fixtures: List[Dict[str, Any]] = []
    seen_ids = set()

    page = 1
    while True:
        params = {
            "api_token": cfg["token"],
            "filters": f"fixtureLeagues:{cfg['leagues_csv']}",
            "order": "asc",
            "per_page": cfg["per_page"],
            "page": page,
        }
        resp = request_with_retry(url, params)
        payload = resp.json()

        data = payload.get("data", [])
        # de-dup just in case
        added = 0
        for fx in data:
            fx_id = fx.get("id")
            if fx_id in seen_ids:
                continue
            fixtures.append(fx)
            seen_ids.add(fx_id)
            added += 1

        # pagination heuristics: prefer 'pagination.has_more'; fallback to len(data)<per_page
        pagination = payload.get("pagination") or payload.get("meta", {}).get("pagination")
        if pagination is not None:
            has_more = pagination.get("has_more")
            if has_more:
                page += 1
                continue
            else:
                break
        else:
            if len(data) == cfg["per_page"]:
                page += 1
                continue
            break

    return fixtures


def parse_start(when: str) -> datetime:
    # SportMonks returns "YYYY-MM-DD HH:MM:SS" in UTC
    dt = datetime.strptime(when, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=timezone.utc)


def filter_upcoming(fixtures: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    now = datetime.now(timezone.utc)
    result: List[Dict[str, Any]] = []
    for fx in fixtures:
        start_str = fx.get("starting_at")
        if not start_str:
            continue
        try:
            start_dt = parse_start(start_str)
        except Exception:
            continue
        if start_dt >= now:
            result.append(fx)
    # sort by start time
    result.sort(key=lambda x: x.get("starting_at", ""))
    return result


def group_by_league(fixtures: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for fx in fixtures:
        lg = str(fx.get("league_id"))
        grouped.setdefault(lg, []).append(fx)
    return grouped


def main() -> None:
    cfg = parse_env()

    print(
        f"Fetching fixtures {cfg['start_date']} -> {cfg['end_date']} "
        f"for leagues [{cfg['leagues_csv']}]"
    )

    raw = fetch_all_pages(cfg)
    upcoming = filter_upcoming(raw)
    grouped = group_by_league(upcoming)

    out_root = pathlib.Path(cfg["out_dir"])
    safe_makedirs(out_root)
    by_league_dir = out_root / "by_league"
    safe_makedirs(by_league_dir)

    # Write combined
    combined_path = out_root / "latest.json"
    with combined_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "start_date": cfg["start_date"],
                "end_date": cfg["end_date"],
                "league_ids": cfg["league_ids"],
                "count": len(upcoming),
                "data": upcoming,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    # Write per-league
    total_written = 0
    for league_id, items in grouped.items():
        p = by_league_dir / f"{league_id}.json"
        with p.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "league_id": int(league_id),
                    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                    "start_date": cfg["start_date"],
                    "end_date": cfg["end_date"],
                    "count": len(items),
                    "data": items,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
        total_written += len(items)

    # Simple summary for logs
    summary = out_root / "summary.txt"
    with summary.open("w", encoding="utf-8") as f:
        lines = [
            f"Time (UTC): {datetime.now(timezone.utc).isoformat()}",
            f"Window    : {cfg['start_date']} -> {cfg['end_date']}",
            f"Leagues   : {cfg['leagues_csv']}",
            f"Fixtures  : {len(upcoming)} (written {total_written})",
            "",
        ]
        # top 10 lines for quick view
        for fx in upcoming[:10]:
            lines.append(f"{fx.get('id')} | {fx.get('starting_at')} | {fx.get('name')}")
        f.write("\n".join(lines))

    print(f"Wrote {len(upcoming)} upcoming fixtures")
    print(f"- {combined_path}")
    print(f"- {by_league_dir}/*")
    print(f"- {summary}")


if __name__ == "__main__":
    main()
