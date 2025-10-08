#!/usr/bin/env python3
"""
Fetch upcoming fixtures from Sportmonks and persist results to the repo.

Outputs (all overwritten on each run):
- data/fixtures/latest.json                -> merged fixtures across all ALLOWED_LEAGUES
- data/fixtures/by_league/{league_id}.json -> fixtures per league
- data/fixtures/summary.txt                -> header summary (counts, dates, leagues)
- data/fixtures/fixtures.txt               -> human-readable list of all fixtures

Environment:
- SPORTMONKS_TOKEN (required)
- FIXTURE_WINDOW_DAYS (default: 21)
- FIXTURE_REQUEST_DELAY_SEC (default: 0.3)
- FIXTURE_LIST_LIMIT (default: 0 = no cap)
"""

from __future__ import annotations
import os
import sys
import json
import time
import pathlib
import datetime as dt
from typing import Dict, List, Any, Optional
import urllib.request
import urllib.parse

# -------------------------
# Config
# -------------------------

# The only leagues we care about
ALLOWED_LEAGUES = [
    8,    # Premier League
    9,    # Championship
    384,  # Serie A
    387,  # Serie B
    82,   # Bundesliga
    301,  # Ligue 1
    564,  # La Liga
    567,  # La Liga 2
    600,  # Süper Lig
]

WINDOW_DAYS = int(os.getenv("FIXTURE_WINDOW_DAYS", "21"))
BASE_DIR = pathlib.Path("data/fixtures")

SPORTMONKS_TOKEN = os.getenv("SPORTMONKS_TOKEN")
API_BASE = "https://api.sportmonks.com/v3/football/fixtures/between"

PER_REQUEST_SLEEP = float(os.getenv("FIXTURE_REQUEST_DELAY_SEC", "0.3"))

# -------------------------
# Helpers
# -------------------------

def _ensure_env() -> None:
    if not SPORTMONKS_TOKEN:
        print("ERROR: SPORTMONKS_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

def _dates_utc() -> tuple[str, str, str]:
    now = dt.datetime.now(dt.timezone.utc)
    start = now.date()
    end = (now + dt.timedelta(days=WINDOW_DAYS)).date()
    return now.isoformat(), start.isoformat(), end.isoformat()

def _http_get(url: str) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return json.loads(raw.decode("utf-8", errors="ignore"))

def _build_base_url(start_date: str, end_date: str, league_id: int) -> str:
    qs = {
        "api_token": SPORTMONKS_TOKEN,
        "leagues": str(league_id),  # some deployments ignore this; we also hard-filter client-side
        "per_page": "200",
    }
    return f"{API_BASE}/{urllib.parse.quote(start_date)}/{urllib.parse.quote(end_date)}?{urllib.parse.urlencode(qs)}"

def _next_link(payload: Dict[str, Any]) -> Optional[str]:
    """
    Try multiple common pagination shapes:
    - {"links": {"next": "https://...&page=2"}}
    - {"meta": {"next_page": 2, "current_page": 1}}
    - {"pagination": {"next_page": 2}}
    If only a page number is exposed, we return None and let the caller add ?page=.
    """
    # Direct link
    links = payload.get("links") or {}
    if isinstance(links, dict):
        nxt = links.get("next")
        if isinstance(nxt, str) and nxt.strip():
            return nxt

    # Page number hints
    for key in ("meta", "pagination"):
        meta = payload.get(key) or {}
        if isinstance(meta, dict):
            np = meta.get("next_page")
            if np:
                # signal that there is a next page, caller will attach &page=np
                return str(np)  # not a URL; caller handles
    return None

def _fetch_all_pages(base_url: str) -> List[Dict[str, Any]]:
    """
    Fetch first page and follow pagination until exhausted.
    Supports both absolute next links and page-number hints.
    """
    page_items: List[Dict[str, Any]] = []

    url = base_url
    page_param_mode = False  # switch to ?page= if we only get numeric hints

    while True:
        payload = _http_get(url)
        data = payload.get("data") or []
        if not isinstance(data, list):
            data = []

        page_items.extend(data)

        nxt = _next_link(payload)
        if not nxt:
            break

        # If nxt looks like a URL, follow it directly
        if nxt.startswith("http"):
            url = nxt
        else:
            # nxt is a page number hint
            page_param_mode = True
            parsed = urllib.parse.urlsplit(base_url)
            q = dict(urllib.parse.parse_qsl(parsed.query))
            q["page"] = nxt
            url = urllib.parse.urlunsplit((
                parsed.scheme,
                parsed.netloc,
                parsed.path,
                urllib.parse.urlencode(q),
                parsed.fragment
            ))
        time.sleep(PER_REQUEST_SLEEP)

    # Some APIs only expose total_pages without next; try a minimal fallback loop:
    if not page_items:
        # nothing fetched – at least return an empty list
        return page_items

    # Done
    return page_items

def _is_future_not_started(fx: Dict[str, Any]) -> bool:
    try:
        st_ts = int(fx.get("starting_at_timestamp") or 0)
    except Exception:
        st_ts = 0
    state_id = fx.get("state_id")
    now_ts = int(time.time())
    return (state_id in (1, None)) and (st_ts == 0 or st_ts > now_ts)

def _sort_key(fx: Dict[str, Any]):
    return (
        int(fx.get("starting_at_timestamp") or 0),
        int(fx.get("league_id") or 0),
        int(fx.get("id") or 0),
    )

def _write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

def _write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)

# -------------------------
# Main
# -------------------------

def main() -> int:
    _ensure_env()
    generated_at_utc, start_date, end_date = _dates_utc()

    all_fixtures: List[Dict[str, Any]] = []
    per_league: Dict[int, List[Dict[str, Any]]] = {}

    for lid in ALLOWED_LEAGUES:
        base_url = _build_base_url(start_date, end_date, lid)
        try:
            items = _fetch_all_pages(base_url)
        except Exception as e:
            print(f"[warn] league {lid}: request failed: {e}", file=sys.stderr)
            continue

        # **Hard filters**: only that league, only future/not-started
        filtered = []
        for fx in items:
            try:
                fx_league = int(fx.get("league_id") or 0)
            except Exception:
                fx_league = 0
            if fx_league != lid:
                continue
            if not _is_future_not_started(fx):
                continue
            filtered.append(fx)

        filtered.sort(key=_sort_key)
        per_league[lid] = filtered
        all_fixtures.extend(filtered)

        # Overwrite per-league file
        _write_json(BASE_DIR / "by_league" / f"{lid}.json", {
            "generated_at_utc": generated_at_utc,
            "start_date": start_date,
            "end_date": end_date,
            "league_id": str(lid),
            "count": len(filtered),
            "data": filtered,
        })

        time.sleep(PER_REQUEST_SLEEP)

    # Deduplicate by global fixture id
    dedup: Dict[int, Dict[str, Any]] = {}
    for fx in all_fixtures:
        try:
            fid = int(fx.get("id") or 0)
        except Exception:
            fid = 0
        if fid:
            dedup[fid] = fx
    merged = list(dedup.values())
    merged.sort(key=_sort_key)

    latest_obj = {
        "generated_at_utc": generated_at_utc,
        "start_date": start_date,
        "end_date": end_date,
        "league_ids": [str(l) for l in ALLOWED_LEAGUES],
        "count": len(merged),
        "data": merged,
    }
    _write_json(BASE_DIR / "latest.json", latest_obj)

    # Human-readable verification files
    header = [
        f"fixtures = Time (UTC): {generated_at_utc}",
        f"Window    : {start_date} -> {end_date}",
        f"Leagues   : {','.join(str(l) for l in ALLOWED_LEAGUES)}",
        f"Fixtures  : {len(merged)} (written {len(merged)})",
        "",
    ]
    _write_text(BASE_DIR / "summary.txt", "\n".join(header) + "\n")

    # fixtures.txt (full list)
    lines: List[str] = []
    for fx in merged:
        fid = fx.get("id")
        when = fx.get("starting_at") or ""
        name = fx.get("name") or ""
        lines.append(f"{fid} | {when} | {name}")

    limit = int(os.getenv("FIXTURE_LIST_LIMIT", "0"))
    if limit > 0:
        lines = lines[:limit]

    _write_text(BASE_DIR / "fixtures.txt", "\n".join(header + lines) + "\n")

    # Console echo for Actions logs
    print("\n".join(header[:4]))
    print(f"(Saved to {BASE_DIR}/latest.json, by_league/*.json, summary.txt, fixtures.txt)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
