#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Compute goals-related 'value flags' from cached H2H stats.

Inputs (local files only)
-------------------------
- data/h2h/by_league/{league_id}.json
  Schema (as produced by scripts/h2h_last2_fetch.py):
    {
      "count": <int>,
      "fixtures": [
        {
          "fixture_id": <int>,
          "starting_at": "YYYY-MM-DD HH:MM:SS",
          "home_id": <int>, "home_name": <str>,
          "away_id": <int>, "away_name": <str>,
          "pair_key": "<min>_<max>",
          "cache_present": true,
          "fetched_at": "ISO",
          "lastN_meta": [
            {"starting_at":"...", "home_goals":<int>, "away_goals":<int>}, ...
          ],
          "vectors": {
            "home": {
              "goals":[...], "shots":[...], "sot":[...], "corners":[...],
              "fouls":[...], "offsides":[...], "yellow":[...], "red":[...],
              "possession":[...]
            },
            "away": { ... same keys ... }
          }
        }, ...
      ]
    }

Outputs
-------
- data/value_bets/goals/by_league/{league_id}.json
- data/value_bets/goals/combined.json
- data/value_bets/goals/summary.txt     (human-readable rollup)

Environment (optional)
----------------------
- VB_LASTN                (int, default: 5)   -> trim H2H sequences if longer
- VB_MIN_SAMPLES          (int, default: 3)   -> min H2H matches to consider
- VB_O25_MIN_RATE         (float, default: 0.60)
- VB_BTTS_MIN_RATE        (float, default: 0.60)
- VB_O35_MIN_RATE         (float, default: 0.40)
- VB_U25_MAX_RATE         (float, default: 0.20)  -> Under 2.5 if O2.5 <= this
- VB_SOT_BOOST            (float, default: 0.5)   -> add to confidence if avg SOT total >= 10
- VB_SOT_SUPPRESS         (float, default: 0.5)   -> subtract if avg SOT total <= 6
- VB_INCLUDE_LEAGUES      (csv of league_ids)     -> if set, restrict to these leagues only

Notes
-----
- Uses only H2H cache; if a fixture lacks usable H2H (below VB_MIN_SAMPLES),
  it will be included with flags set to False and reason explaining insufficiency.
"""

from __future__ import annotations
import os, json, math
from pathlib import Path
from typing import Any, Dict, List, Optional
from statistics import mean, median

ROOT = Path(".")
H2H_DIR = ROOT / "data" / "h2h" / "by_league"
OUT_DIR = ROOT / "data" / "value_bets" / "goals"
OUT_LEAGUE = OUT_DIR / "by_league"
OUT_LEAGUE.mkdir(parents=True, exist_ok=True)

# ---------------- env / thresholds ----------------
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default

VB_LASTN        = _env_int("VB_LASTN", 5)
VB_MIN_SAMPLES  = _env_int("VB_MIN_SAMPLES", 3)
VB_O25_MIN_RATE = _env_float("VB_O25_MIN_RATE", 0.60)
VB_BTTS_MIN_RATE= _env_float("VB_BTTS_MIN_RATE", 0.60)
VB_O35_MIN_RATE = _env_float("VB_O35_MIN_RATE", 0.40)
VB_U25_MAX_RATE = _env_float("VB_U25_MAX_RATE", 0.20)
VB_SOT_BOOST    = _env_float("VB_SOT_BOOST", 0.5)  # add to confidence if avg SOT total >= 10
VB_SOT_SUPPRESS = _env_float("VB_SOT_SUPPRESS", 0.5)  # subtract if avg SOT total <= 6

def _env_league_filter() -> Optional[List[int]]:
    s = os.getenv("VB_INCLUDE_LEAGUES", "").strip()
    if not s:
        return None
    out: List[int] = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok: continue
        try: out.append(int(tok))
        except: pass
    return out or None

LEAGUE_FILTER = _env_league_filter()

# ---------------- io helpers ----------------
def _load_json(p: Path) -> Optional[dict]:
    if not p.is_file(): return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None

def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    tmp.replace(path)

# ---------------- math helpers ----------------
def _clean_nums(xs: List[Optional[int]]) -> List[int]:
    return [int(x) for x in xs if isinstance(x, (int, float))]

def _rate(hits: int, total: int) -> float:
    if total <= 0: return 0.0
    return round(hits / total, 3)

def _fmt_rate(hits: int, total: int) -> str:
    return f"{hits}/{total}" if total > 0 else "0/0"

def _conf_bucket(base_rate: float, sot_avg_total: Optional[float]) -> str:
    """
    Make a simple confidence heuristic:
    - Start at base_rate (e.g., 0.6)
    - If SOT total avg >= 10, +VB_SOT_BOOST
    - If SOT total avg <= 6,  -VB_SOT_SUPPRESS
    Map to {low, medium, high}.
    """
    adj = base_rate
    if sot_avg_total is not None:
        if sot_avg_total >= 10:
            adj += VB_SOT_BOOST
        elif sot_avg_total <= 6:
            adj -= VB_SOT_SUPPRESS

    if adj >= 1.0: adj = 0.999  # clamp for sanity
    if adj >= 0.75:
        return "high"
    if adj >= 0.55:
        return "medium"
    return "low"

# ---------------- core compute ----------------
def _lastn(seq: List[Optional[int]], n: int) -> List[Optional[int]]:
    if n <= 0: return []
    return list(seq[:n])

def _fixture_flags_from_h2h(row: dict) -> dict:
    """
    Build flags strictly from H2H vectors.
    Returns a dict suitable to embed in output JSON for one fixture.
    """
    home = (row.get("vectors") or {}).get("home") or {}
    away = (row.get("vectors") or {}).get("away") or {}

    # Trim to VB_LASTN from most recent (vectors already newest->older in cache)
    Hg = _lastn(home.get("goals") or [], VB_LASTN)
    Ag = _lastn(away.get("goals") or [], VB_LASTN)
    Hsot = _lastn(home.get("sot") or [], VB_LASTN)
    Asot = _lastn(away.get("sot") or [], VB_LASTN)

    # Cleaned numerics
    Hg_n = _clean_nums(Hg)
    Ag_n = _clean_nums(Ag)

    n = min(len(Hg), len(Ag))
    # if either side had unknown entries, totals list will skip those indices
    totals: List[int] = []
    btts_mask: List[bool] = []
    for i in range(n):
        h = Hg[i]; a = Ag[i]
        if isinstance(h, (int, float)) and isinstance(a, (int, float)):
            totals.append(int(h)+int(a))
            btts_mask.append((int(h) > 0 and int(a) > 0))

    # SOT averages (help confidence)
    Hsot_n = _clean_nums(Hsot)
    Asot_n = _clean_nums(Asot)
    sot_avg_total = None
    if Hsot_n and Asot_n:
        sot_avg_total = round(mean(Hsot_n) + mean(Asot_n), 2)
    elif Hsot_n:
        sot_avg_total = round(mean(Hsot_n), 2)
    elif Asot_n:
        sot_avg_total = round(mean(Asot_n), 2)

    used = min(len(Hg_n), len(Ag_n), len(totals))
    if used < VB_MIN_SAMPLES:
        return {
            "h2h_available": False,
            "reason": f"insufficient H2H samples (have {used}, need {VB_MIN_SAMPLES})",
            "h2h": {
                "lastN": used,
                "home_goals": Hg[:used],
                "away_goals": Ag[:used],
                "totals": totals[:used],
                "avg_total": None,
                "median_total": None,
                "o25_hits": 0, "o25_rate": 0.0,
                "o35_hits": 0, "o35_rate": 0.0,
                "btts_hits": 0, "btts_rate": 0.0,
                "sot_avg_total": sot_avg_total,
            },
            "flags": { "over_2_5": False, "over_3_5": False, "btts": False, "under_2_5": False },
            "reasons": []
        }

    # metrics
    t_used = totals[:used]
    avg_total = round(mean(t_used), 2) if t_used else None
    med_total = round(median(t_used), 2) if t_used else None

    o25_hits = sum(1 for v in t_used if v >= 3)
    o35_hits = sum(1 for v in t_used if v >= 4)
    btts_hits = sum(1 for i in range(used) if i < len(btts_mask) and btts_mask[i])

    o25_rate = _rate(o25_hits, used)
    o35_rate = _rate(o35_hits, used)
    btts_rate= _rate(btts_hits, used)

    # flags & reasons
    reasons: List[str] = []
    flag_o25 = o25_rate >= VB_O25_MIN_RATE
    flag_btts= btts_rate >= VB_BTTS_MIN_RATE
    flag_o35 = o35_rate >= VB_O35_MIN_RATE
    flag_u25 = (o25_rate <= VB_U25_MAX_RATE)

    if flag_o25:
        reasons.append(f"O2.5 {o25_rate:.2f} ({_fmt_rate(o25_hits, used)} in H2H)")
    if flag_btts:
        reasons.append(f"BTTS {btts_rate:.2f} ({_fmt_rate(btts_hits, used)})")
    if flag_o35:
        reasons.append(f"O3.5 {o35_rate:.2f} ({_fmt_rate(o35_hits, used)})")
    if flag_u25 and not flag_o25:  # only surface U2.5 if not also O2.5
        reasons.append(f"U2.5 signal: O2.5 only {o25_rate:.2f} ({_fmt_rate(o25_hits, used)})")

    # confidence buckets (for the strongest of the raised flags)
    conf: Dict[str,str] = {}
    if flag_o25: conf["over_2_5"] = _conf_bucket(o25_rate, sot_avg_total)
    if flag_btts: conf["btts"] = _conf_bucket(btts_rate, sot_avg_total)
    if flag_o35: conf["over_3_5"] = _conf_bucket(o35_rate, sot_avg_total)
    if flag_u25 and not flag_o25:
        # invert confidence slightly: lower total + low SOT = higher
        base = 1.0 - o25_rate
        conf["under_2_5"] = _conf_bucket(base, (0 if sot_avg_total is None else sot_avg_total))

    return {
        "h2h_available": True,
        "h2h": {
            "lastN": used,
            "home_goals": Hg[:used],
            "away_goals": Ag[:used],
            "totals": t_used,
            "avg_total": avg_total,
            "median_total": med_total,
            "o25_hits": o25_hits, "o25_rate": o25_rate,
            "o35_hits": o35_hits, "o35_rate": o35_rate,
            "btts_hits": btts_hits, "btts_rate": btts_rate,
            "sot_avg_total": sot_avg_total,
        },
        "flags": {
            "over_2_5": flag_o25,
            "over_3_5": flag_o35,
            "btts": flag_btts,
            "under_2_5": flag_u25 and not flag_o25
        },
        "confidence": conf,
        "reasons": reasons
    }

def process_league(lid: int, blob: dict) -> dict:
    out_fixtures: List[dict] = []
    for fx in (blob.get("fixtures") or []):
        row = {
            "fixture_id": fx.get("fixture_id") or fx.get("id"),
            "starting_at": fx.get("starting_at"),
            "home_id": fx.get("home_id"),
            "home_name": fx.get("home_name"),
            "away_id": fx.get("away_id"),
            "away_name": fx.get("away_name"),
            "pair_key": fx.get("pair_key"),
        }
        flags = _fixture_flags_from_h2h(fx)
        row.update(flags)
        out_fixtures.append(row)

    payload = {
        "league_id": lid,
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "lastN_used": VB_LASTN,
        "min_samples": VB_MIN_SAMPLES,
        "thresholds": {
            "o25_min_rate": VB_O25_MIN_RATE,
            "o35_min_rate": VB_O35_MIN_RATE,
            "btts_min_rate": VB_BTTS_MIN_RATE,
            "u25_max_rate": VB_U25_MAX_RATE,
        },
        "fixtures": out_fixtures,
    }
    return payload

def main():
    league_files = sorted(H2H_DIR.glob("*.json"))
    if LEAGUE_FILTER:
        league_files = [p for p in league_files if p.stem.isdigit() and int(p.stem) in LEAGUE_FILTER]

    per_league_payloads: List[dict] = []
    for p in league_files:
        lid = None
        try: lid = int(p.stem)
        except: continue
        blob = _load_json(p) or {}
        if not blob.get("fixtures"):
            # still write an empty shell for visibility
            per_league_payloads.append({
                "league_id": lid,
                "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
                "lastN_used": VB_LASTN,
                "min_samples": VB_MIN_SAMPLES,
                "thresholds": {
                    "o25_min_rate": VB_O25_MIN_RATE,
                    "o35_min_rate": VB_O35_MIN_RATE,
                    "btts_min_rate": VB_BTTS_MIN_RATE,
                    "u25_max_rate": VB_U25_MAX_RATE,
                },
                "fixtures": []
            })
            continue

        payload = process_league(lid, blob)
        _write_json(OUT_LEAGUE / f"{lid}.json", payload)
        per_league_payloads.append(payload)

    # combined
    combined = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat(),
        "lastN_used": VB_LASTN,
        "min_samples": VB_MIN_SAMPLES,
        "leagues": [pl["league_id"] for pl in per_league_payloads],
        "fixtures": [
            {"league_id": pl["league_id"], **fx}
            for pl in per_league_payloads
            for fx in (pl.get("fixtures") or [])
        ],
    }
    _write_json(OUT_DIR / "combined.json", combined)

    # summary.txt (simple, not a "post")
    lines: List[str] = []
    lines.append(f"Generated (UTC): {combined['generated_at']}")
    lines.append(f"Leagues: {', '.join(str(x) for x in combined['leagues'])}")
    lines.append(f"Using H2H lastN={VB_LASTN}, min_samples={VB_MIN_SAMPLES}")
    lines.append("")
    def picklist(key: str) -> List[dict]:
        return [fx for fx in combined["fixtures"] if (fx.get("flags") or {}).get(key)]
    o25 = picklist("over_2_5")
    o35 = picklist("over_3_5")
    btts= picklist("btts")
    u25 = picklist("under_2_5")

    def fmt(fx: dict) -> str:
        h = fx.get("home_name"); a = fx.get("away_name")
        lid = fx.get("league_id")
        h2h = fx.get("h2h") or {}
        return f"[L{lid}] {h} vs {a} — O2.5:{h2h.get('o25_rate',0):.2f} BTTS:{h2h.get('btts_rate',0):.2f} AvgTot:{h2h.get('avg_total')}"
    if o25:
        lines.append("=== Over 2.5 flags ===")
        for fx in o25: lines.append(" - " + fmt(fx))
        lines.append("")
    if btts:
        lines.append("=== BTTS flags ===")
        for fx in btts: lines.append(" - " + fmt(fx))
        lines.append("")
    if o35:
        lines.append("=== Over 3.5 flags ===")
        for fx in o35: lines.append(" - " + fmt(fx))
        lines.append("")
    if u25:
        lines.append("=== Under 2.5 signals ===")
        for fx in u25: lines.append(" - " + fmt(fx))
        lines.append("")

    (OUT_DIR / "summary.txt").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    print(f"[OK] wrote {OUT_DIR/'combined.json'} + {OUT_DIR/'summary.txt'} and per-league JSONs")

if __name__ == "__main__":
    main()
