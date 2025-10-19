#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Value Singles — 1+ SOT (Sportmonks, Bet365)

Qualify if ANY of:
  • 5/5   (last 5 all >=1 SOT)
  • 7/10  (last 10 with >=7 hits)
  • 4/5   (last 5 with >=4 hits) AND team is a strong favourite (ML <= FAV_MAX, default 2.50)

Price filter:
  • Bet365 Over 0.5 SOT >= MIN_DEC_PRICE (default 1.72)

Underdog filter (drop underdogs):
  • Team ML must be < TEAM_UNDERDOG_MAX (default 3.50)

Inputs (already in your repo):
  • Fixtures:        data/fixtures/{league_id}.json
  • Player SOT:      data/player_shots_on_target/by_league/{league_id}.json
  • Odds (Bet365):   data/odds/b365/fixtures/{fixture_id}.json

Env:
  • LEAGUE_IDS          (comma separated; if empty autodiscovers from SOT folder)
  • MIN_DEC_PRICE       default 1.72
  • TEAM_UNDERDOG_MAX   default 3.50
  • FAV_MAX             default 2.50
  • WINDOW_DAYS         default 7 (only fixtures within this window)

Output: prints ranked picks (reason + position), sorted by (reason strength, price desc)
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ----- Paths -----
ROOT      = Path(".")
FIX_DIR   = ROOT / "data" / "fixtures"
SOT_DIR   = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_FIX  = ROOT / "data" / "odds" / "b365" / "fixtures"

# ----- Config (env) -----
MIN_DEC_PRICE     = float(os.getenv("MIN_DEC_PRICE", "1.72"))
TEAM_UNDERDOG_MAX = float(os.getenv("TEAM_UNDERDOG_MAX", "3.50"))
FAV_MAX           = float(os.getenv("FAV_MAX", "2.50"))
WINDOW_DAYS       = int(os.getenv("WINDOW_DAYS", "7"))

# ----- Time helpers -----
def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s:
        return None
    try:
        if "T" not in s:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

def within_window(starting_at: str, days: int) -> bool:
    if not days:
        return True
    ko = parse_dt_utc(starting_at)
    if not ko:
        return False
    now = now_utc()
    return now <= ko <= (now + dt.timedelta(days=days))

# ----- String helpers -----
def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")

def norm(s: str) -> str:
    s = strip_accents(s).lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    return re.sub(r"\s+", " ", s).strip()

GENERIC_TEAM_TOKENS = {
    "fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca",
    "the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"
}

def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b:
        return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return False
    if ta == tb:
        return True
    if ta.issubset(tb) or tb.issubset(ta):
        return True
    inter = ta & tb
    union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5:
        return True
    if len(inter) >= 2:
        return True
    return False

# ----- IO -----
def read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def discover_league_ids() -> List[int]:
    lids = set()
    for p in SOT_DIR.glob("*.json"):
        try:
            lids.add(int(p.stem))
        except:
            pass
    return sorted(lids)

def load_fixtures(lid: int) -> List[dict]:
    blob = read_json(FIX_DIR / f"{lid}.json") or {}
    return blob.get("fixtures") or []

# ----- History checks (latest_first sequences) -----
def hits_last(series: List[int], n: int, threshold: int = 1) -> Tuple[int, int, List[int]]:
    xs = [x for x in (series or []) if isinstance(x, int)]
    if len(xs) < n:
        return (0, len(xs), xs[:len(xs)])
    window = xs[:n]
    hits = sum(1 for v in window if v >= threshold)
    return (hits, n, window)

def qualifies_reason(series: List[int], team_ml: Optional[float]) -> Optional[Tuple[str, List[int]]]:
    # 5/5 perfect
    h, total, win = hits_last(series, 5, 1)
    if total >= 5 and h == 5:
        return ("5/5", win)
    # 7/10 (>=7)
    h, total, win = hits_last(series, 10, 1)
    if total >= 10 and h >= 7:
        return (f"{h}/10", win)   # prints exact, e.g., 7/10, 8/10, 9/10, 10/10 (though 10/10 is rare for SOT)
    # 4/5 if strong favourite
    h, total, win = hits_last(series, 5, 1)
    if total >= 5 and h >= 4 and (team_ml is not None) and (team_ml <= FAV_MAX):
        return ("4/5 (fav ≤2.5)", win)
    return None

# ----- Odds parsing -----
def to_float(v) -> Optional[float]:
    try:
        if v in (None, "", "N/A"):
            return None
        return float(v)
    except Exception:
        return None

MATCH_WINNER_KEYS = [
    "match winner","full time result","win/draw/win","wdw","1x2","match odds","result","3 way","90 minutes","regular time result"
]
def is_match_winner(desc: str) -> bool:
    s = norm(desc)
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def is_sot_market(desc: str) -> bool:
    s = norm(desc)
    return ("shot" in s) and (("on target" in s) or ("on-target" in s) or ("sot" in s))

def extract_team_side_for_fixture(fx: dict, team_name: str) -> Optional[str]:
    parts = fx.get("participants") or []
    for p in parts:
        nm = (p or {}).get("name") or ""
        if team_names_match(nm, team_name):
            meta = (p or {}).get("meta") or {}
            loc = (meta.get("location") or "").lower()
            if loc in ("home","away"):
                return loc
    return None

def extract_team_ml(rows: List[dict], side: str) -> Optional[float]:
    home_vals, away_vals = [], []
    for r in rows or []:
        if r.get("bookmaker_id") != 2:
            continue
        if not is_match_winner(r.get("market_description","")):
            continue
        lab = (r.get("label") or "").strip().lower()
        val = to_float(r.get("value"))
        if val is None:
            continue
        if lab in ("1","home","1 (home)"):
            home_vals.append(val)
        elif lab in ("2","away","2 (away)"):
            away_vals.append(val)
    h = min(home_vals) if home_vals else None
    a = min(away_vals) if away_vals else None
    return h if side == "home" else a

def extract_player_sot_over_point5(rows: List[dict], player_name: str) -> Optional[float]:
    want_name = norm(player_name)
    best = None
    for r in rows or []:
        if r.get("bookmaker_id") != 2:
            continue
        if r.get("stopped"):
            continue
        if not is_sot_market(r.get("market_description","")):
            continue
        cand = norm(r.get("name") or r.get("total") or "")
        if not cand or cand != want_name:
            continue
        lab = r.get("label")
        try:
            labf = float(lab)
        except Exception:
            continue
        if not math.isclose(labf, 0.5, abs_tol=1e-9):
            continue
        price = to_float(r.get("value"))
        if price is None:
            continue
        if (best is None) or (price > best + 1e-12):
            best = price
    return best

# ----- Main -----
def main():
    # leagues
    env_lids = os.getenv("LEAGUE_IDS", "").strip()
    if env_lids:
        LEAGUE_IDS = [int(x) for x in env_lids.split(",") if x.strip()]
    else:
        LEAGUE_IDS = discover_league_ids()

    print(f"Generated at (UTC): {now_utc().isoformat()}")
    print(f"Criteria: 5/5 OR 7/10 OR 4/5 (fav ≤{FAV_MAX:.2f}) | Over 0.5 SOT ≥ {MIN_DEC_PRICE:.2f} | Team ML < {TEAM_UNDERDOG_MAX:.2f} | Window={WINDOW_DAYS} days\n")

    picks: List[dict] = []
    checked = 0

    for lid in LEAGUE_IDS:
        # SOT histories
        blob = read_json(SOT_DIR / f"{lid}.json") or {}
        players = blob.get("players") or []
        # fixtures
        fixtures = load_fixtures(lid)

        for rec in players:
            series = rec.get("on_target_last_n") or []
            name   = rec.get("name") or ""
            team   = rec.get("team_name") or rec.get("team") or ""
            pos    = rec.get("position_tag") or ""
            if not name or not team:
                continue

            # find upcoming fixture for this team
            fx_sel = None
            for fx in fixtures:
                if within_window(fx.get("starting_at"), WINDOW_DAYS):
                    # match by participants set
                    parts = fx.get("participants") or []
                    if any(team_names_match(team, (p or {}).get("name") or "") for p in parts):
                        fx_sel = fx
                        break
            if not fx_sel:
                continue

            side = extract_team_side_for_fixture(fx_sel, team)
            if side not in ("home","away"):
                continue

            fid = int(fx_sel.get("id") or 0)
            if not fid:
                continue

            # odds for that fixture
            odds_blob = read_json(ODDS_FIX / f"{fid}.json") or {}
            rows = odds_blob.get("odds") or []

            # team ML (drop big underdogs and also used for 4/5 favourite rule)
            team_ml = extract_team_ml(rows, side)
            if team_ml is None or team_ml >= TEAM_UNDERDOG_MAX:
                continue

            # reason (history)
            reason = qualifies_reason(series, team_ml)
            if not reason:
                continue
            reason_str, window_used = reason  # e.g. "5/5" or "7/10" or "4/5 (fav ≤2.5)"

            # price — Over 0.5 SOT
            price = extract_player_sot_over_point5(rows, name)
            if price is None or price < MIN_DEC_PRICE:
                continue

            checked += 1
            picks.append({
                "player": name,
                "team": team,
                "position": pos,
                "fixture": fx_sel.get("name") or "",
                "kickoff": (fx_sel.get("starting_at") or "").replace("T"," ").replace("Z",""),
                "side": side,
                "price": float(price),
                "team_ml": float(team_ml),
                "reason": reason_str,
                "series_used": window_used,                       # the exact window we evaluated
                "series_n": len([x for x in series if isinstance(x,int)]),
                "league_id": lid,
            })

    # rank: 5/5 strongest, then 7/10 (with higher first), then 4/5 fav; then by price desc
    def reason_rank(r: str) -> Tuple[int, int]:
        # higher number => stronger
        if r == "5/5":
            return (3, 5)
        if r.endswith("/10"):
            try:
                hits = int(r.split("/")[0])
            except:
                hits = 7
            return (2, hits)  # 10/10 > 9/10 > 8/10 > 7/10
        if r.startswith("4/5"):
            return (1, 4)
        return (0, 0)

    picks.sort(key=lambda x: (reason_rank(x["reason"]), x["price"]), reverse=True)

    print(f"Candidates kept after filters: {len(picks)}\n")
    if not picks:
        print("No SOT value singles found.")
        return

    print("===== VALUE SINGLES — Player 1+ SOT =====")
    for r in picks:
        ser = ",".join(map(str, r["series_used"]))
        pos = f" [{r['position']}]" if r.get("position") else ""
        print(
            f" • {r['player']}{pos} — {r['team']} | {r['fixture']} @ {r['kickoff']} | "
            f"SOT Over 0.5 @ {r['price']:.3f} | Team ML {r['team_ml']:.3f} | "
            f"{r['reason']} | series{len(r['series_used'])}: {ser} | n={r['series_n']}"
        )

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
