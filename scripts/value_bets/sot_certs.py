#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SOT certs (Sportmonks, Bet365) — robust version
Qualify if: 10/10 OR 9/10 OR 8/10 OR 7/7 (Shots On Target >= 1)
Filters:
  • Price: Over 0.5 SOT >= MIN_DEC_PRICE (default 1.30)
  • Team ML: ML < TEAM_WIN_MAX (default 3.50)
Reads:
  • player SOT:     data/player_shots_on_target/by_league/{league_id}.json
  • odds (primary): data/odds/b365/{league_id}.json
  • odds (fallback):data/odds/b365/fixtures/{fixture_id}.json
  • fixtures (optional for window check if not in odds blob): data/fixtures/{league_id}.json
Env:
  • LEAGUE_IDS     (comma sep; default = auto-discover from player_shots_on_target/by_league/*.json)
  • MIN_DEC_PRICE  (default 1.30)
  • TEAM_WIN_MAX   (default 3.50)
  • WINDOW_DAYS    (default 7)
  • FORCE_BOOKMAKER_ID=2 to only use Bet365 rows (default 2)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Iterable, Any

ROOT = Path(".")
SOT_DIR     = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_LEAGUE = ROOT / "data" / "odds" / "b365"
ODDS_FIX    = ODDS_LEAGUE / "fixtures"
FIX_DIR     = ROOT / "data" / "fixtures"

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX", "3.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))
FORCE_BID     = int(os.getenv("FORCE_BOOKMAKER_ID", "2"))  # 2 = Bet365

# ------------- time helpers -------------
def now_utc() -> dt.datetime: return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s: return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        try:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        except Exception:
            return None

def upcoming_within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    dt_k = parse_dt_utc(starting_at)
    if not dt_k: return False
    now = now_utc()
    return now <= dt_k <= (now + dt.timedelta(days=days))

# ------------- string helpers -------------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^\w\s\.-]", " ", s)
    return norm_spaces(s)

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}
def team_tokens(name: str): return {t for t in norm(name).split() if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb or ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    return (len(inter) / max(1, len(union)) >= 0.5) or (len(inter) >= 2)

def parse_fixture_teams(fixture_name: str) -> Tuple[str, str]:
    if not fixture_name: return "",""
    for sep in (" vs ", " v ", " VS ", " Vs ", " - "):
        if sep in fixture_name:
            a, b = fixture_name.split(sep, 1)
            return a.strip(), b.strip()
    return "", ""

# ------------- IO helpers -------------
def read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def discover_league_ids() -> List[int]:
    lids = set()
    for p in SOT_DIR.glob("*.json"):
        try: lids.add(int(p.stem))
        except: pass
    return sorted(lids)

# ------------- odds parsing -------------
def to_float(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"): return None
        return float(v)
    except Exception:
        return None

MATCH_WINNER_KEYS = [
    "match winner","full time result","win/draw/win","wdw","1x2","match odds","result","3 way","90 minutes","regular time result"
]
def is_match_winner(desc: str) -> bool:
    s = norm(desc); return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def is_sot_market(desc: str) -> bool:
    s = norm(desc)
    # allow variants: "player shots on target", "shots on-target - player", "SOT"
    return ("shot" in s and "target" in s) or ("sot" in s)

def _row_text(row: dict) -> str:
    fields = ["label","name","original_label","market_description","outcome","outcome_name","header","description"]
    return " ".join([str(row.get(f,"")) for f in fields]).lower()

def _line_is_point5(row: dict) -> bool:
    t = to_float(row.get("total"))
    if t is not None: return math.isclose(t, 0.5, abs_tol=1e-6)
    try:
        l = float(row.get("label"))
        return math.isclose(l, 0.5, abs_tol=1e-6)
    except Exception:
        pass
    blob = _row_text(row).replace(",", ".")
    return "0.5" in blob

def _is_over_row(row: dict) -> Optional[bool]:
    txt = _row_text(row)
    if re.search(r"\bunder\b", txt): return False
    if re.search(r"\bover\b",  txt): return True
    if "+0.5" in txt or "0.5+" in txt or "0,5+" in txt: return True
    return None

def _cleanup_label(label: str) -> str:
    return re.sub(r"(?:\s*\([^)]*\))+$", "", label or "").strip()

def _person_part(label: str) -> str:
    s = _cleanup_label(label or "")
    m = re.split(r"\b(?:-?\s*over|-?\s*under|\s+o\/u|\s+o\d+|\s+u\d+)\b", s, flags=re.IGNORECASE)
    return m[0].strip() if m else s

def label_matches_player(row: dict, player_name: str) -> bool:
    # try several fields; allow aliases like surname, initial+surname etc.
    want = norm(player_name).replace(".", "")
    candidates = [
        row.get("name",""), row.get("original_label",""), row.get("label",""),
        row.get("outcome_name",""), row.get("header",""), row.get("description",""),
        row.get("total",""),
    ]
    for cand in candidates:
        s = norm(_person_part(str(cand))).replace(".", "")
        if not s: continue
        # exact or subset token match (surname matching typical feed formats)
        stoks = set(s.split()); wtoks = set(want.split())
        if s == want or stoks.issubset(wtoks) or wtoks.issubset(stoks):
            return True
    return False

def extract_team_ml_from_rows(rows: List[dict], side: str) -> Optional[float]:
    home_vals, away_vals = [], []
    for r in rows or []:
        if FORCE_BID and int(r.get("bookmaker_id") or 0) != FORCE_BID:
            continue
        if not is_match_winner(r.get("market_description","")):
            continue
        lab = (r.get("label") or "").strip().lower()
        val = to_float(r.get("value"))
        if val is None: continue
        if lab in ("1","home","1 (home)"): home_vals.append(val)
        elif lab in ("2","away","2 (away)"): away_vals.append(val)
    h = min(home_vals) if home_vals else None
    a = min(away_vals) if away_vals else None
    return h if side == "home" else a

def best_over05_player_sot(rows: List[dict], player_name: str) -> Optional[float]:
    cands: List[Tuple[Optional[bool], float]] = []
    for r in rows or []:
        if FORCE_BID and int(r.get("bookmaker_id") or 0) != FORCE_BID:
            continue
        if r.get("stopped"):  # market closed
            continue
        if not is_sot_market(r.get("market_description","")):
            continue
        if not _line_is_point5(r):
            continue
        if not label_matches_player(r, player_name):
            continue
        price = to_float(r.get("value"))
        if price is None: continue
        cands.append((_is_over_row(r), price))
    if not cands:
        return None
    exp_over = [p for flag, p in cands if flag is True]
    if exp_over:
        return min(exp_over)
    amb = [p for flag, p in cands if flag is None]
    if amb:
        return min(amb)
    return None

# ------------- tiers -------------
def tier_info(series: List[int]) -> Tuple[Optional[str], int, List[int], int]:
    xs = [x for x in (series or []) if isinstance(x, int)]
    if len(xs) >= 10:
        last10 = xs[:10]
        c10 = sum(1 for v in last10 if v >= 1)
        if c10 == 10: return ("10/10", 10, last10, c10)
        if c10 == 9:  return ("9/10",  10, last10, c10)
        if c10 == 8:  return ("8/10",  10, last10, c10)
    if len(xs) >= 7:
        last7 = xs[:7]
        c7 = sum(1 for v in last7 if v >= 1)
        if c7 == 7:   return ("7/7",    7, last7,  c7)
    return (None, 0, [], 0)

# ------------- main -------------
def main():
    # choose leagues
    env_leagues = os.getenv("LEAGUE_IDS", "").strip()
    if env_leagues:
        league_ids = [int(x) for x in env_leagues.split(",") if x.strip()]
    else:
        league_ids = discover_league_ids()

    generated_at = now_utc().isoformat()
    print(f"Generated at (UTC): {generated_at}")
    print(f"Criteria: SOT certs (10/10,9/10,8/10 or 7/7) | O0.5 >= {MIN_DEC_PRICE:.2f} | Team ML < {TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS}d")

    picks: List[dict] = []

    for lid in league_ids:
        sot_blob = read_json(SOT_DIR / f"{lid}.json") or {}
        players = sot_blob.get("players") or []
        odds_blob = read_json(ODDS_LEAGUE / f"{lid}.json") or {}
        odds_fixtures = odds_blob.get("fixtures") or []

        # If odds blob lacks times, keep fixtures as fallback for window
        fixtures_fallback = (read_json(FIX_DIR / f"{lid}.json") or {}).get("fixtures") or []

        for rec in players:
            series = rec.get("on_target_last_n") or rec.get("sot_last_n") or rec.get("shots_on_target_last_n") or []
            tier, used_total, used_series, hits = tier_info(series)
            if not tier:
                continue

            tname = rec.get("team_name") or rec.get("team") or ""
            pname = rec.get("name") or rec.get("player_name") or ""
            if not (tname and pname):
                continue

            # Find the player's next fixture from odds blob (primary)
            chosen_fx = None
            side = None
            for fx in odds_fixtures:
                fname = fx.get("name") or ""
                home, away = parse_fixture_teams(fname)
                if not (home and away):
                    continue
                if team_names_match(tname, home):
                    side = "home"
                elif team_names_match(tname, away):
                    side = "away"
                else:
                    continue

                # window test: prefer odds start time, fallback to fixtures file time
                st = fx.get("starting_at") or ""
                if not st:
                    # fallback: look up in fixtures file by name
                    for ff in fixtures_fallback:
                        nm = ff.get("name") or ""
                        if nm and nm == fname:
                            st = ff.get("starting_at") or ""
                            break
                if not upcoming_within_window(st, WINDOW_DAYS):
                    continue

                chosen_fx = fx
                break

            if not chosen_fx or not side:
                # fallback scan with fixtures file + per-fixture odds (legacy layout)
                # try to locate a matching upcoming fixture and then read data/odds/b365/fixtures/{fid}.json
                for ff in fixtures_fallback:
                    if not upcoming_within_window(ff.get("starting_at"), WINDOW_DAYS):
                        continue
                    parts = ff.get("participants") or []
                    if len(parts) >= 2:
                        home = (parts[0] or {}).get("name") or ""
                        away = (parts[1] or {}).get("name") or ""
                    else:
                        home, away = parse_fixture_teams(ff.get("name") or "")
                    if team_names_match(tname, home):
                        side = "home"
                    elif team_names_match(tname, away):
                        side = "away"
                    else:
                        continue
                    fid = ff.get("id") or ff.get("fixture_id")
                    if not fid:
                        continue
                    odds_fallback = read_json(ODDS_FIX / f"{int(fid)}.json") or {}
                    rows_fb = odds_fallback.get("odds") or []
                    if not rows_fb:
                        continue
                    # apply filters on fallback rows
                    tml = extract_team_ml_from_rows(rows_fb, side)
                    if tml is None or not (tml < TEAM_WIN_MAX):
                        continue
                    price = best_over05_player_sot(rows_fb, pname)
                    if price is None or price < MIN_DEC_PRICE:
                        continue
                    picks.append({
                        "player": pname, "team": tname,
                        "fixture": ff.get("name") or "",
                        "kickoff": (ff.get("starting_at") or "").replace("T"," ").replace("Z",""),
                        "side": side, "price": float(price), "team_ml": float(tml),
                        "tier": tier, "tier_hits": hits, "tier_total": used_total,
                        "series_used": used_series, "series_full_n": len([x for x in series if isinstance(x,int)]),
                        "league_id": lid,
                    })
                    chosen_fx = None  # already appended via fallback
                    break

                # if still no luck, skip
                if not chosen_fx:
                    continue

            # odds rows from the league-level odds blob
            rows = chosen_fx.get("odds") or []
            if not rows:
                continue

            # team ML filter
            tml = extract_team_ml_from_rows(rows, side)
            if tml is None or not (tml < TEAM_WIN_MAX):
                continue

            # Player O0.5 SOT price
            price = best_over05_player_sot(rows, pname)
            if price is None or price < MIN_DEC_PRICE:
                continue

            picks.append({
                "player": pname, "team": tname,
                "fixture": chosen_fx.get("name") or "",
                "kickoff": (chosen_fx.get("starting_at") or "").replace("T"," ").replace("Z",""),
                "side": side, "price": float(price), "team_ml": float(tml),
                "tier": tier, "tier_hits": hits, "tier_total": used_total,
                "series_used": used_series, "series_full_n": len([x for x in series if isinstance(x,int)]),
                "league_id": lid,
            })

    # sort by tier then price
    tier_rank = {"10/10": 4, "9/10": 3, "8/10": 2, "7/7": 1}
    picks.sort(key=lambda r: (-tier_rank.get(r["tier"], 0), -r["price"], r["player"]))

    if not picks:
        print("No SOT certs found (after odds/ML).")
        return

    print("===== SOT CERTS — Player 1+ SOT =====")
    for r in picks:
        ser = ",".join(map(str, r["series_used"]))
        print(
            f" • {r['player']} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
            f"SOT Over 0.5 @ {r['price']:.3f} | Team ML {r['team_ml']:.3f} | "
            f"tier {r['tier']} ({r['tier_hits']}/{r['tier_total']}) | "
            f"series{r['tier_total']}: {ser} | n={r['series_full_n']}"
        )

if __name__ == "__main__":
    try:
        import math
        main()
    except KeyboardInterrupt:
        pass
