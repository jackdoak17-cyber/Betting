#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Sportmonks v3 – Fixtures (+odds) between dates, filtered by leagues, bookmakers, and markets.

Key fixes vs. your previous script:
  • Use query param 'filter' (singular) not 'filters'
  • Use filter key 'fixtureLeagues' for leagues
  • No /bookmakers/search/{name} endpoint – fetch all and match locally
  • Proper pagination via ?page and 'meta.has_more'
  • Include odds with nested bookmaker/market so you can see names in one call

This prints:
  [FIXTURES] Retrieved N fixtures across X leagues (range)
  [ODDS] Fixtures with odds payloads: A / B   (A = fixtures that actually returned odds rows, B = fixtures that have has_odds==True)
  [DEBUG] Bookmakers seen (top): ...
  [DEBUG] Top markets returned: ...
"""

from __future__ import annotations
import os
import sys
import time
import json
from typing import Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests


BASE_URL = "https://api.sportmonks.com/v3"
FOOTBALL_BASE = f"{BASE_URL}/football"
ODDS_BASE = f"{BASE_URL}/odds"

# -----------------------------
# --- CONFIGURE THESE VALUES ---
# -----------------------------

# 1) API token: set env var SPORTMONKS_TOKEN or paste directly below
API_TOKEN = os.environ.get("SPORTMONKS_TOKEN", "PASTE_YOUR_TOKEN_HERE")

# 2) Leagues you care about (example IDs – replace with your own)
LEAGUE_IDS = [
    # EPL=8, LaLiga=564, etc — replace with your 7 leagues:
    8, 564, 82, 384, 301, 3015, 271
]

# 3) Date range (UTC) — next 7 days
START_DATE = datetime.now(timezone.utc).date()
END_DATE = (START_DATE + timedelta(days=7))

# 4) Odds filtering (optional but recommended)
#    If you leave either list empty, the filter will not be applied for that dimension.
#    Use bookmaker IDs (use fetch_bookmakers() to discover). Example: bet365=2, Pinnacle=3xx (varies per account), etc.
BOOKMAKER_IDS: List[int] = [2]           # e.g., only bet365. Add others if you want broader coverage.
MARKET_IDS: List[int] = [268]            # 268 = "Player Shots" (example). Add more to increase coverage.

# -----------------------------
# --- END USER CONFIG -------
# -----------------------------


def http_get(url: str, params: Dict, timeout: int = 20) -> Dict:
    """GET JSON with basic error handling."""
    headers = {"Accept": "application/json"}
    r = requests.get(url, params=params, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code} error for {r.url}\n{r.text}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Failed to parse JSON for {r.url}: {e}\nBody: {r.text[:1000]}") from e


def fetch_bookmakers(token: str) -> Dict[int, str]:
    """
    Fetch all bookmakers once. Returns {id: name}.
    Use this to map names locally rather than calling a non-existent search endpoint.
    """
    url = f"{ODDS_BASE}/bookmakers"
    params = {"api_token": token, "order": "asc"}
    data = http_get(url, params)
    arr = data.get("data", []) or []
    return {row["id"]: row.get("name", f"bookmaker:{row['id']}") for row in arr if isinstance(row, dict) and "id" in row}


def build_filter(league_ids: List[int],
                 market_ids: Optional[List[int]] = None,
                 bookmaker_ids: Optional[List[int]] = None) -> str:
    """
    Build the single 'filter' param string for Sportmonks v3.
    Example result: "fixtureLeagues:8,564,markets:268,bookmakers:2,14"
    """
    parts = []
    if league_ids:
        parts.append(f"fixtureLeagues:{','.join(map(str, league_ids))}")
    if market_ids:
        parts.append(f"markets:{','.join(map(str, market_ids))}")
    if bookmaker_ids:
        parts.append(f"bookmakers:{','.join(map(str, bookmaker_ids))}")
    return ",".join(parts)


def fetch_fixtures_between(token: str,
                           start_date: datetime,
                           end_date: datetime,
                           league_ids: List[int],
                           market_ids: Optional[List[int]] = None,
                           bookmaker_ids: Optional[List[int]] = None,
                           per_page: int = 50,
                           order: str = "asc") -> List[Dict]:
    """
    Fetch fixtures between two dates with odds included, filtered by leagues and optional odds filters.
    Paginates until meta.has_more is False.
    """
    url = f"{FOOTBALL_BASE}/fixtures/between/{start_date:%Y-%m-%d}/{end_date:%Y-%m-%d}"
    include = "participants;odds.bookmaker;odds.market"  # odds + nested bookmaker/market so names are present
    params = {
        "api_token": token,
        "include": include,
        "per_page": per_page,
        "order": order,
        # IMPORTANT: v3 expects 'filter' (singular)
        "filter": build_filter(league_ids, MARKET_IDS if market_ids else None, BOOKMAKER_IDS if bookmaker_ids else None),
    }

    all_rows: List[Dict] = []
    page = 1
    while True:
        params["page"] = page
        payload = http_get(url, params)
        rows = payload.get("data", []) or []
        all_rows.extend(rows)

        meta = payload.get("meta", {}) or {}
        has_more = meta.get("has_more")
        # Fallback if meta.has_more is missing: stop when returned < per_page
        if has_more is False or (has_more is None and len(rows) < per_page):
            break

        page += 1
        # small politeness delay to avoid spiking rate limits
        time.sleep(0.08)

    return all_rows


def summarize_odds(fixtures: List[Dict]) -> Tuple[int, int, Counter, Counter]:
    """
    Returns:
      fixtures_with_odds_payloads: number of fixtures that actually returned non-empty odds array
      fixtures_has_odds_flag: number of fixtures where has_odds == True
      bookmaker_counter: counts of bookmaker names observed across all returned odds rows
      market_counter: counts of market names observed across all returned odds rows
    """
    fixtures_has_odds_flag = 0
    fixtures_with_odds_payloads = 0
    bookmaker_counter = Counter()
    market_counter = Counter()

    for fx in fixtures:
        if fx.get("has_odds"):
            fixtures_has_odds_flag += 1

        odds_rows = fx.get("odds") or []
        if isinstance(odds_rows, list) and len(odds_rows) > 0:
            fixtures_with_odds_payloads += 1
            for odd in odds_rows:
                # odd may already contain nested 'bookmaker' / 'market' due to include
                bm_name = None
                mk_name = None
                if isinstance(odd, dict):
                    bk = odd.get("bookmaker") or {}
                    mk = odd.get("market") or {}
                    bm_name = bk.get("name") or (f"id:{bk.get('id')}" if bk.get("id") else None)
                    mk_name = mk.get("name") or (f"id:{mk.get('id')}" if mk.get("id") else None)
                if bm_name:
                    bookmaker_counter[bm_name] += 1
                if mk_name:
                    market_counter[mk_name] += 1

    return fixtures_with_odds_payloads, fixtures_has_odds_flag, bookmaker_counter, market_counter


def main():
    if not API_TOKEN or API_TOKEN == "PASTE_YOUR_TOKEN_HERE":
        print("ERROR: Add your Sportmonks API token (env SPORTMONKS_TOKEN or inline in the script).")
        sys.exit(1)

    # Optional: fetch all bookmakers so you can confirm IDs/names printed later
    try:
        all_bm = fetch_bookmakers(API_TOKEN)
    except Exception as e:
        print(f"[WARN] Could not fetch bookmakers list (continuing): {e}")
        all_bm = {}

    start_utc = START_DATE
    end_utc = END_DATE

    fixtures = fetch_fixtures_between(
        token=API_TOKEN,
        start_date=start_utc,
        end_date=end_utc,
        league_ids=LEAGUE_IDS,
        market_ids=MARKET_IDS if MARKET_IDS else None,
        bookmaker_ids=BOOKMAKER_IDS if BOOKMAKER_IDS else None,
        per_page=50,
        order="asc",
    )

    leagues_seen = {fx.get("league_id") for fx in fixtures if isinstance(fx, dict)}
    print(f"[FIXTURES] Retrieved {len(fixtures)} fixtures across {len(leagues_seen)} leagues ({start_utc} ➜ {end_utc}).")

    # Odds summary
    fixtures_with_odds_payloads, fixtures_has_odds_flag, bm_counter, mk_counter = summarize_odds(fixtures)

    print(f"[ODDS] Fixtures with odds payloads: {fixtures_with_odds_payloads} / {fixtures_has_odds_flag}")

    # Explain the common reason counts can differ:
    #  - fixtures_has_odds_flag counts fixtures where 'has_odds' is True (ANY market / ANY bookmaker)
    #  - fixtures_with_odds_payloads counts fixtures where your *filters* returned actual rows
    # If you restrict BOOKMAKER_IDS or MARKET_IDS, you'll often see fewer payloads than has_odds.
    if BOOKMAKER_IDS or MARKET_IDS:
        print("       Note: filtering by"
              f"{' bookmakers='+','.join(map(str, BOOKMAKER_IDS)) if BOOKMAKER_IDS else ''}"
              f"{' markets='+','.join(map(str, MARKET_IDS)) if MARKET_IDS else ''}"
              " reduces how many fixtures return rows even if has_odds is True.")

    # Top-N debug
    def top(counter: Counter, n=6) -> List[Tuple[str, int]]:
        return counter.most_common(n)

    bm_top = top(bm_counter, 6)
    mk_top = top(mk_counter, 6)

    # Pretty-print top lists
    if bm_top:
        pretty_bm = ", ".join(f"{name}({count})" for name, count in bm_top)
        print(f"[DEBUG] Bookmakers seen (top): {pretty_bm}")
    else:
        print("[DEBUG] Bookmakers seen (top): none")

    if mk_top:
        pretty_mk = ", ".join(f"{name}({count})" for name, count in mk_top)
        print(f"[DEBUG] Top markets returned: {pretty_mk}")
    else:
        print("[DEBUG] Top markets returned: none")

    # Optional: detailed echo of your actual request params for reproducibility
    filter_echo = build_filter(LEAGUE_IDS, MARKET_IDS if MARKET_IDS else None, BOOKMAKER_IDS if BOOKMAKER_IDS else None)
    print("\n[DEBUG] Request echo:")
    print(f"        endpoint: {FOOTBALL_BASE}/fixtures/between/{START_DATE:%Y-%m-%d}/{END_DATE:%Y-%m-%d}")
    print(f"        include : participants;odds.bookmaker;odds.market")
    print(f"        filter  : {filter_echo}")
    print(f"        per_page: 50, order: asc")

    # Optional sanity list when payloads are empty but has_odds=True
    empties = []
    for fx in fixtures:
        if fx.get("has_odds") and not (fx.get("odds") or []):
            # This fixture has odds somewhere in the ecosystem, but your filters did not return rows
            empties.append(fx.get("id"))
    if empties:
        print(f"[DEBUG] has_odds=True but no rows after filtering (sample up to 10): {empties[:10]}")

    # Uncomment to dump one sample fixture with odds for inspection
    # for fx in fixtures:
    #     if fx.get("odds"):
    #         print(json.dumps(fx, indent=2)[:4000])
    #         break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
