#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, json
from common import (
    today_utc, days_ahead, daterange_str,
    fixtures_by_date, LEAGUES, DATE_FMT, league_name
)

DATA_DIR = os.environ.get("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
OUT_PATH = os.path.join(DATA_DIR, "fixtures.json")

def main():
    start = today_utc()
    end   = days_ahead(start, 15)  # next 6 days inclusive
    dates = daterange_str(start, end)
    league_set = set(LEAGUES.keys())

    all_fixtures = []
    for d in dates:
        fxs = fixtures_by_date(d, league_filter=league_set)
        # keep small, relevant shape
        for fx in fxs:
            all_fixtures.append({
                "id": fx.get("id"),
                "league_id": fx.get("league_id"),
                "league_name": league_name(fx.get("league_id")),
                "starting_at": fx.get("starting_at"),
                "name": fx.get("name"),
                "participants": fx.get("participants"),
                "state": fx.get("state"),
            })

    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"generated_at": start.strftime(DATE_FMT),
                   "fixtures": all_fixtures}, f, ensure_ascii=False)
    print(f"[OK] fixtures={len(all_fixtures)}  -> {OUT_PATH}")

if __name__ == "__main__":
    main()
