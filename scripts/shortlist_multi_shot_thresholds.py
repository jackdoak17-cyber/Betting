#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Read previously saved shots JSONL (data/shots_stats_<LEAGUE_ID>.jsonl)
and print *exclusive* shortlists for >=1, >=2, >=3 shots with:
  - Last 10: 10/10 (100%) and ≥9/10 (≥90%)
  - Last 5 : 5/5 (100%) and ≥4/5 (≥80%)
Exactly one appearance bucket per player (priority top→bottom).

USAGE:
  python scripts/shortlist_multi_shot_thresholds.py <LEAGUE_ID>
"""
import sys, json, os
from typing import List, Dict

def load_jsonl(path: str) -> List[dict]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            try:
                rows.append(json.loads(line))
            except Exception:
                pass
    return rows

def pick_first_bucket(rows: List[dict], thr_label: str):
    """
    thr_label in {"1p","2p","3p"}
    returns dict of lists: {
       "L10_100": [...], "L10_90": [...],
       "L5_100": [...],  "L5_80": [...]
    }
    """
    used = set()
    b10_100, b10_90, b5_100, b5_80 = [], [], [], []
    hit10_key = f"hit10_{thr_label}"
    hit5_key  = f"hit5_{thr_label}"

    # priority: 10/10 → ≥9/10 → 5/5 → ≥4/5
    for r in rows:
        if r["apps10"] == 10 and r.get(hit10_key, 0.0) == 100.0:
            b10_100.append(r); used.add(r["player_id"])
    for r in rows:
        if r["player_id"] in used: continue
        if r["apps10"] == 10 and r.get(hit10_key, 0.0) >= 90.0:
            b10_90.append(r); used.add(r["player_id"])
    for r in rows:
        if r["player_id"] in used: continue
        if r["apps5"] == 5 and r.get(hit5_key, 0.0) == 100.0:
            b5_100.append(r); used.add(r["player_id"])
    for r in rows:
        if r["player_id"] in used: continue
        if r["apps5"] == 5 and r.get(hit5_key, 0.0) >= 80.0:
            b5_80.append(r); used.add(r["player_id"])

    def fmt(l: List[dict]) -> List[str]:
        l.sort(key=lambda x: (-x.get(hit10_key,0.0), -x.get(hit5_key,0.0), x.get("pos",""), x.get("player_id")))
        res = []
        for s in l:
            res.append(f"  {s['display']} — apps10:{s['apps10']}, hit10:{s.get(hit10_key,0.0):.1f}%"
                       f"  | last5:{s['apps5']} apps, hit5:{s.get(hit5_key,0.0):.1f}%")
        return res

    return {
        "L10_100": fmt(b10_100),
        "L10_90":  fmt(b10_90),
        "L5_100":  fmt(b5_100),
        "L5_80":   fmt(b5_80),
    }

def main():
    if len(sys.argv) < 2:
        print("usage: shortlist_multi_shot_thresholds.py LEAGUE_ID", file=sys.stderr)
        sys.exit(2)
    lid = int(sys.argv[1])
    path = f"data/shots_stats_{lid}.jsonl"
    if not os.path.isfile(path):
        print(f"ERROR: not found {path}. Run stats_shots.py {lid} first.", file=sys.stderr)
        sys.exit(1)

    rows = load_jsonl(path)
    if not rows:
        print("No rows loaded.")
        return

    print(f"\n=== League {lid} — Multi-threshold shortlists from cached stats ===\n")

    for thr, label in [(1,"1p"), (2,"2p"), (3,"3p")]:
        out = pick_first_bucket(rows, label)
        header = {1:"≥1 shot", 2:"≥2 shots", 3:"≥3 shots"}[thr]
        print(f"▶ {header} — Last 10: 10/10")
        print("\n".join(out["L10_100"]) if out["L10_100"] else "  (none)")
        print(f"\n▶ {header} — Last 10: ≥9/10")
        print("\n".join(out["L10_90"]) if out["L10_90"] else "  (none)")
        print(f"\n▶ {header} — Last 5: 5/5")
        print("\n".join(out["L5_100"]) if out["L5_100"] else "  (none)")
        print(f"\n▶ {header} — Last 5: ≥4/5")
        print("\n".join(out["L5_80"]) if out["L5_80"] else "  (none)")
        print("\n")

if __name__ == "__main__":
    main()
