#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dump Sportmonks odds market catalogs for your subscription (Standard + Premium if available).
Outputs:
  - data/odds/markets/standard_markets.json / .csv
  - data/odds/markets/premium_markets.json  / .csv  (if endpoint accessible)
  - data/odds/markets/search_probe.json     (results for 'shots','fouls','tackles','passes')
Env:
  SPORTMONKS_TOKEN (required)
"""
import os, json, csv, requests, pathlib

BASE = "https://api.sportmonks.com/v3"
TOKEN = os.getenv("SPORTMONKS_TOKEN")
HEAD = {"Accept": "application/json"}
OUTDIR = pathlib.Path("data/odds/markets"); OUTDIR.mkdir(parents=True, exist_ok=True)

def get(url):
    r = requests.get(url, headers=HEAD, timeout=30)
    if r.status_code != 200:
        print(f"[HTTP {r.status_code}] {url} :: {r.text[:200]}")
        return None
    return r.json()

def paginate_markets(url):
    items = []
    while url:
        data = get(url + (("&" if "?" in url else "?") + f"api_token={TOKEN}&per_page=50&order=asc"))
        if not data: break
        items.extend(data.get("data", []))
        nxt = (data.get("pagination") or {}).get("next_page")
        url = nxt if (data.get("pagination") or {}).get("has_more") else None
    return items

def write_json_csv(rows, stem):
    (OUTDIR / f"{stem}.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    with (OUTDIR / f"{stem}.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id","legacy_id","name","developer_name","has_winning_calculations"])
        for r in rows:
            w.writerow([r.get("id"), r.get("legacy_id"), r.get("name"),
                        r.get("developer_name"), r.get("has_winning_calculations")])

def search(name):
    url = f"{BASE}/odds/markets/search/{name}?api_token={TOKEN}"
    j = get(url)
    return j.get("data") if isinstance(j, dict) else None

def main():
    if not TOKEN:
        raise SystemExit("ERROR: SPORTMONKS_TOKEN not set.")
    # Standard markets
    std = paginate_markets(f"{BASE}/odds/markets")
    print(f"[MARKETS] Standard: {len(std)}")
    write_json_csv(std, "standard_markets")

    # Premium markets (if accessible for your plan)
    prem = paginate_markets(f"{BASE}/odds/markets/premium")
    if prem:
        print(f"[MARKETS] Premium: {len(prem)}")
        write_json_csv(prem, "premium_markets")
    else:
        print("[MARKETS] Premium: not available for this token or returned 0.")

    # Probe a few player-prop names you asked about
    probes = {}
    for term in ["Shots", "Shots on Target", "Cards", "Assists", "Fouls", "Tackles", "Passes"]:
        probes[term] = search(term)
    (OUTDIR / "search_probe.json").write_text(json.dumps(probes, indent=2), encoding="utf-8")
    print("[SEARCH] Probe results written to data/odds/markets/search_probe.json")

if __name__ == "__main__":
    main()
