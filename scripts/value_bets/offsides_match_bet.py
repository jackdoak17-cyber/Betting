# ================= scripts/value_bets/offsides_match_bet.py =================
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Team Offsides — OVER value (two lists: OVERALL and HOME/AWAY)

Reads (local files only):
- Fixtures:                    data/fixtures/{league_id}.json
- Team offense series:         data/team_stats/by_league/{league_id}.json
- Opponent-allowed series:     data/team_opponent_stats/by_league/{league_id}.json
- Bet365 odds from Sportmonks: data/odds/b365/{league_id}.json

Market targeted:
- Bet365 "Team Offsides" (market_id 286). We select OVER only.

Two result sections:
1) OVERALL — uses venue-agnostic last-N for team_offsides and opp_offsides_allowed
2) HOME/AWAY — uses venue-adjusted splits based on locations_last_n

Keep a pick if ANY is true (and sample size >= MIN_SAMPLE on the contributing side[s]):
- combo >= MIN_COMBO  (combo = avg(team_over%, oppA_over%))
- team_over% >= MIN_ONE
- oppA_over% >= MIN_ONE

Ranking:
- Within each section independently: rank by primary % (combo if present, else max(team%,oppA%)), then price desc.

Env (optional):
- LEAGUE_IDS      CSV of league IDs to scan (default: auto from fixtures dir)
- WINDOW_DAYS     default 10
- MIN_DEC_PRICE   default 1.20
- MIN_COMBO       default 0.50
- MIN_ONE         default 0.65
- MIN_SAMPLE      default 4
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- Config ----------
ROOT       = Path(".")
FIX_DIR    = ROOT / "data" / "fixtures"
TS_DIR     = ROOT / "data" / "team_stats" / "by_league"
OPP_DIR    = ROOT / "data" / "team_opponent_stats" / "by_league"
ODDS_DIR   = ROOT / "data" / "odds" / "b365"
OUT_PATH   = Path("data/value_bets/offsides_match_bet.txt"); OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "10"))
MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.20"))
MIN_COMBO     = float(os.getenv("MIN_COMBO", "0.50"))
MIN_ONE       = float(os.getenv("MIN_ONE",   "0.65"))
MIN_SAMPLE    = int(os.getenv("MIN_SAMPLE",  "4"))

TEAM_OFFSIDES_MARKET_ID = 286  # Bet365 "Team Offsides"

# ---------- String utils / matching ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TOK = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
               "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TOK}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; uni = ta | tb
    if len(inter) / max(1, len(uni)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in (" vs ", " v ", " - ", " VS ", " Vs "):
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

def within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    if not starting_at: return True
    try:
        dt_utc = dt.datetime.strptime(starting_at[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return True
    now = dt.datetime.now(dt.timezone.utc)
    return now <= dt_utc <= (now + dt.timedelta(days=days))

# ---------- IO ----------
def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

def discover_league_ids() -> List[int]:
    ids = []
    for p in FIX_DIR.glob("*.json"):
        try: ids.append(int(p.stem))
        except: pass
    return sorted(set(ids))

def index_by_team(blob: dict) -> Dict[str, dict]:
    m: Dict[str, dict] = {}
    for t in (blob.get("teams") or []):
        nm = t.get("team_name")
        if not nm: continue
        m[norm(nm)] = t
    return m

# ---------- Odds helpers ----------
def is_team_offsides_row(row: dict) -> bool:
    mid = row.get("market_id")
    if isinstance(mid, int) and mid == TEAM_OFFSIDES_MARKET_ID:
        return True
    md = norm(row.get("market_description") or "")
    return ("offsides" in md and "team" in md)

def label_to_side(label: Optional[str]) -> Optional[str]:
    s = (label or "").strip().lower()
    if s in {"1","home","home (1)","team 1"}: return "home"
    if s in {"2","away","away (2)","team 2"}: return "away"
    return None

def row_is_over(row: dict) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("total","label","original_label","name")).lower()
    return ("over" in text) and ("under" not in text)

def parse_line(row: dict) -> Optional[float]:
    h = row.get("handicap")
    try:
        if h is not None:
            return float(h)
    except Exception:
        pass
    for k in ("total","label","original_label","name"):
        s = str(row.get(k) or "")
        m = re.search(r"([-+]?\d+(?:\.\d+)?)", s)
        if m:
            try: return float(m.group(1))
            except Exception: pass
    return None

def parse_price(row: dict) -> Optional[float]:
    try:
        return float(row.get("value"))
    except Exception:
        return None

# ---------- Rate helpers ----------
def over_threshold(line: float) -> int:
    # e.g. Over 2.0 -> need 3+, Over 2.5 -> need 3+
    if float(line).is_integer():
        return int(line) + 1
    return math.ceil(float(line))

def over_rate(seq: List[int], line: float) -> Optional[Tuple[int,int,float]]:
    if not seq: return None
    xs = [x for x in seq if isinstance(x, int)]
    if not xs: return None
    thr = over_threshold(line)
    hits = sum(1 for x in xs if x >= thr)
    n = len(xs)
    return hits, n, (hits / n) if n else None

def over_rate_split(seq: List[int], locs: List[str], want: str, line: float) -> Optional[Tuple[int,int,float]]:
    if not seq or not locs: return None
    m = min(len(seq), len(locs))
    filt = [seq[i] for i in range(m) if (locs[i] or "").lower() == want]
    if not filt: return None
    return over_rate(filt, line)

def combo_avg(team_over, oppA_over) -> Optional[Tuple[float, Tuple[int,int], Tuple[int,int]]]:
    t_pct = team_over[2] if team_over else None
    a_pct = oppA_over[2] if oppA_over else None
    if t_pct is None and a_pct is None:
        return None
    if t_pct is None:
        return a_pct, (0,0), (oppA_over[0], oppA_over[1])
    if a_pct is None:
        return t_pct, (team_over[0], team_over[1]), (0,0)
    return (t_pct + a_pct) / 2.0, (team_over[0], team_over[1]), (oppA_over[0], oppA_over[1])

# ---------- Main ----------
def main():
    # leagues to scan
    if os.getenv("LEAGUE_IDS"):
        league_ids = [int(x) for x in os.getenv("LEAGUE_IDS").split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    rows_overall: List[dict] = []
    rows_ha: List[dict] = []

    for lid in league_ids:
        fx_path   = FIX_DIR / f"{lid}.json"
        odds_path = ODDS_DIR / f"{lid}.json"
        ts_path   = TS_DIR / f"{lid}.json"
        opp_path  = OPP_DIR / f"{lid}.json"
        if not (fx_path.exists() and odds_path.exists() and ts_path.exists() and opp_path.exists()):
            continue

        fixtures = load_json(fx_path).get("fixtures") or []
        odds_blob = load_json(odds_path)
        odds_by_fixture = {int(f.get("fixture_id")): f for f in (odds_blob.get("fixtures") or []) if isinstance(f.get("fixture_id"), int)}

        ts_idx  = index_by_team(load_json(ts_path))
        opp_idx = index_by_team(load_json(opp_path))

        for fx in fixtures:
            if not isinstance(fx, dict): continue
            fid = fx.get("id") or fx.get("fixture_id")
            name = fx.get("name") or ""
            starting_at = fx.get("starting_at") or ""
            if WINDOW_DAYS and not within_window(starting_at, WINDOW_DAYS):
                continue

            home, away = parse_fixture_teams(name)
            if not home or not away:
                continue

            odds_fx = odds_by_fixture.get(int(fid)) if isinstance(fid, int) else None
            if not odds_fx:
                continue
            rows_odds = odds_fx.get("odds") or []
            if not isinstance(rows_odds, list):
                continue

            for row in rows_odds:
                if not is_team_offsides_row(row):
                    continue
                if not row_is_over(row):
                    continue

                side = label_to_side(row.get("label"))
                if side not in {"home","away"}:
                    text = " ".join([str(row.get("name") or ""), str(row.get("total") or ""), str(row.get("original_label") or "")]).lower()
                    if "home" in text and "away" not in text: side = "home"
                    elif "away" in text and "home" not in text: side = "away"
                    else:
                        continue

                line  = parse_line(row)
                price = parse_price(row)
                if line is None or price is None or price < MIN_DEC_PRICE:
                    continue

                team_nm = home if side=="home" else away
                opp_nm  = away if side=="home" else home

                # Find rows (with fuzzy fallback)
                t_rec = ts_idx.get(norm(team_nm))
                a_rec = opp_idx.get(norm(opp_nm))
                if not t_rec:
                    for r in ts_idx.values():
                        if team_names_match(team_nm, r.get("team_name","")):
                            t_rec = r; break
                if not a_rec:
                    for r in opp_idx.values():
                        if team_names_match(opp_nm, r.get("team_name","")):
                            a_rec = r; break
                if not (t_rec and a_rec):
                    continue

                team_seq = [x for x in (t_rec.get("offsides_last_n") or []) if isinstance(x, int)]
                team_locs= [ (s or "").lower() for s in (t_rec.get("locations_last_n") or []) ]
                oppA_seq = [x for x in (a_rec.get("opp_offsides_last_n") or []) if isinstance(x, int)]
                oppA_locs= [ (s or "").lower() for s in (a_rec.get("locations_last_n") or []) ]

                # OVERALL metrics
                t_over_all = over_rate(team_seq, line)
                a_over_all = over_rate(oppA_seq, line)
                combo_all  = combo_avg(t_over_all, a_over_all)

                # H/A metrics
                team_want = "home" if side == "home" else "away"
                opp_want  = "away" if side == "home" else "home"
                t_over_ha = over_rate_split(team_seq, team_locs, team_want, line)
                a_over_ha = over_rate_split(oppA_seq, oppA_locs, opp_want,  line)
                combo_ha  = combo_avg(t_over_ha, a_over_ha)

                def enough(t): return (t is not None) and (t[1] >= MIN_SAMPLE)
                # -------- OVERALL section trigger --------
                overall_keep = False
                overall_score = 0.0
                if combo_all and ((t_over_all and enough(t_over_all)) or (a_over_all and enough(a_over_all))) and combo_all[0] >= MIN_COMBO:
                    overall_keep = True
                    overall_score = max(overall_score, combo_all[0])
                if t_over_all and enough(t_over_all) and t_over_all[2] >= MIN_ONE:
                    overall_keep = True
                    overall_score = max(overall_score, t_over_all[2])
                if a_over_all and enough(a_over_all) and a_over_all[2] >= MIN_ONE:
                    overall_keep = True
                    overall_score = max(overall_score, a_over_all[2])

                # -------- H/A section trigger --------
                ha_keep = False
                ha_score = 0.0
                if combo_ha and ((t_over_ha and enough(t_over_ha)) or (a_over_ha and enough(a_over_ha))) and combo_ha[0] >= MIN_COMBO:
                    ha_keep = True
                    ha_score = max(ha_score, combo_ha[0])
                if t_over_ha and enough(t_over_ha) and t_over_ha[2] >= MIN_ONE:
                    ha_keep = True
                    ha_score = max(ha_score, t_over_ha[2])
                if a_over_ha and enough(a_over_ha) and a_over_ha[2] >= MIN_ONE:
                    ha_keep = True
                    ha_score = max(ha_score, a_over_ha[2])

                entry_base = {
                    "fixture": name,
                    "kickoff": starting_at,
                    "team": team_nm,
                    "opp": opp_nm,
                    "side": side,
                    "line": float(line),
                    "price": float(price),
                }

                if overall_keep:
                    rows_overall.append({
                        **entry_base,
                        "score": overall_score,
                        "t_over": t_over_all, "a_over": a_over_all,
                        "combo": combo_all[0] if combo_all else None,
                    })

                if ha_keep:
                    rows_ha.append({
                        **entry_base,
                        "score": ha_score,
                        "t_over": t_over_ha, "a_over": a_over_ha,
                        "combo": combo_ha[0] if combo_ha else None,
                    })

    # Rank within each section
    rows_overall.sort(key=lambda r: (-(r["score"] or 0.0), -r["price"], r["fixture"], r["team"], r["line"]))
    rows_ha.sort(key=lambda r: (-(r["score"] or 0.0), -r["price"], r["fixture"], r["team"], r["line"]))

    # Render
    def fmt_pair(t):
        if not t: return "n/a"
        hits, n, pct = t
        return f"{hits}/{n} ({pct*100:5.1f}%)"

    lines = []
    lines.append(f"Generated at (UTC): {dt.datetime.utcnow().isoformat(timespec='seconds')}")
    lines.append(f"Window={WINDOW_DAYS} days | Min price={MIN_DEC_PRICE:.2f} | MIN_COMBO={MIN_COMBO:.2f} | MIN_ONE={MIN_ONE:.2f} | MIN_SAMPLE={MIN_SAMPLE}")
    lines.append("")

    # OVERALL section
    lines.append("===== OVERALL — Team Offsides OVER candidates (ranked) =====")
    if not rows_overall:
        lines.append("(no candidates)")
    else:
        for r in rows_overall:
            lines.append(
                f" • {r['team']} — Offsides Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']}"
            )
            combo_str = f" | combo={(r['combo']*100):5.1f}%" if r.get("combo") is not None else ""
            lines.append(
                f"    Overall: team {fmt_pair(r['t_over'])}, oppA {fmt_pair(r['a_over'])}{combo_str}"
            )

    lines.append("")
    # HOME/AWAY section
    lines.append("===== HOME/AWAY — Team Offsides OVER candidates (ranked) =====")
    if not rows_ha:
        lines.append("(no candidates)")
    else:
        for r in rows_ha:
            lines.append(
                f" • {r['team']} — Offsides Over {r['line']:.1f} @ {r['price']:.3f} | {r['fixture']} | side={r['side']}"
            )
            combo_str = f" | combo={(r['combo']*100):5.1f}%" if r.get("combo") is not None else ""
            lines.append(
                f"    H/A    : team {fmt_pair(r['t_over'])}, oppA {fmt_pair(r['a_over'])}{combo_str}"
            )

    out = "\n".join(lines).rstrip() + "\n"
    OUT_PATH.write_text(out, encoding="utf-8")
    print(out, end="")

if __name__ == "__main__":
    main()
