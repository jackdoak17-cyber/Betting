#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
SOT certs (Sportmonks, Bet365) — clear tier output
- Qualify if: 10/10 OR 9/10 OR 8/10 OR 7/7 (Shots On Target >= 1)
- Price filter: Over 0.5 SOT >= MIN_DEC_PRICE (default 1.30)
- Team ML filter: ML < TEAM_WIN_MAX (default 3.50)
- Uses your stored files:
    fixtures:              data/fixtures/{league_id}.json
    player SOT histories:  data/player_shots_on_target/by_league/{league_id}.json
    odds per fixture:      data/odds/b365/fixtures/{fixture_id}.json  (bookmaker_id=2 only)

Env:
  LEAGUE_IDS     (comma sep; default = auto-discover from player_shots_on_target/by_league/*.json)
  MIN_DEC_PRICE  (default 1.30)
  TEAM_WIN_MAX   (default 3.50)
  WINDOW_DAYS    (default 7) — only consider upcoming fixtures within this window
"""

import os, re, json, math, datetime as dt, unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(".")
FIX_DIR    = ROOT / "data" / "fixtures"
SOT_DIR    = ROOT / "data" / "player_shots_on_target" / "by_league"
ODDS_FIX   = ROOT / "data" / "odds" / "b365" / "fixtures"

MIN_DEC_PRICE = float(os.getenv("MIN_DEC_PRICE", "1.30"))
TEAM_WIN_MAX  = float(os.getenv("TEAM_WIN_MAX", "3.50"))
WINDOW_DAYS   = int(os.getenv("WINDOW_DAYS", "7"))

def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)

def parse_dt_utc(s: str) -> Optional[dt.datetime]:
    if not s: return None
    try:
        # fixtures `starting_at` looks like "YYYY-MM-DD HH:MM:SS"
        if "T" not in s:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc)
        # ISO-ish with Z
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None

# ---------- string helpers ----------
def strip_accents(s: str) -> str:
    return ''.join(c for c in unicodedata.normalize('NFD', s or '') if unicodedata.category(c) != 'Mn')

def norm(s: str) -> str:
    s = strip_accents(s or "").lower()
    s = re.sub(r"[^a-z0-9\s\.-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

GENERIC_TEAM_TOKENS = {"fc","cf","afc","sc","cd","ud","ac","as","ss","ssc","us","uc","rc","rcd","ca","the","club","de","del","la","las","los","calcio","united","city","saint","st","bk"}

def team_tokens(name: str):
    toks = set(norm(name).split())
    return {t for t in toks if t not in GENERIC_TEAM_TOKENS}

def team_names_match(a: str, b: str) -> bool:
    if not a or not b: return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb: return False
    if ta == tb: return True
    if ta.issubset(tb) or tb.issubset(ta): return True
    inter = ta & tb; union = ta | tb
    if len(inter) / max(1, len(union)) >= 0.5: return True
    if len(inter) >= 2: return True
    return False

# ---------- IO ----------
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

def load_fixtures(lid: int) -> List[dict]:
    blob = read_json(FIX_DIR / f"{lid}.json") or {}
    return blob.get("fixtures") or []

def index_fixtures_by_team(lid: int) -> List[dict]:
    return load_fixtures(lid)

def upcoming_within_window(starting_at: str, days: int) -> bool:
    if not days: return True
    dt_k = parse_dt_utc(starting_at)
    if not dt_k: return False
    now = now_utc()
    return now <= dt_k <= (now + dt.timedelta(days=days))

# ---------- odds parsing (Sportmonks rows) ----------
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
    s = norm(desc)
    return bool(s) and any(k in s for k in MATCH_WINNER_KEYS)

def is_sot_market(desc: str) -> bool:
    s = norm(desc)
    # very strict: must contain both "shot" and a form of "on target"
    return ("shot" in s) and (("on target" in s) or ("on-target" in s) or ("sot" in s))

def extract_team_ml(rows: List[dict], side: str) -> Optional[float]:
    """
    Sportmonks per-row shape seen in your data:
      { market_description, label, value, ... }
    We'll look for match-winner rows; label expected ["1","X","2"] or ["home","draw","away"].
    """
    home_vals, away_vals = [], []
    for r in rows or []:
        if r.get("bookmaker_id") != 2:  # Bet365 only
            continue
        if not is_match_winner(r.get("market_description","")):
            continue
        lab = (r.get("label") or "").strip().lower()
        val = to_float(r.get("value"))
        if val is None: continue
        if lab in ("1", "home", "1 (home)"):
            home_vals.append(val)
        elif lab in ("2", "away", "2 (away)"):
            away_vals.append(val)
    h = min(home_vals) if home_vals else None
    a = min(away_vals) if away_vals else None
    return h if side == "home" else a

def extract_player_sot_over_point5(rows: List[dict], player_name: str) -> Optional[float]:
    """
    Find best price for Over 0.5 SOT for `player_name`.
    We match using row["market_description"] ~ SOT, and either row["name"] or row["total"] equals the player.
    We require label == "0.5" (float) and not stopped.
    """
    want_name = norm(player_name)
    best = None
    for r in rows or []:
        if r.get("bookmaker_id") != 2:  # Bet365
            continue
        if r.get("stopped"):            # market closed
            continue
        if not is_sot_market(r.get("market_description","")):
            continue

        # Player matching: prefer "name", fallback to "total"
        cand = norm(r.get("name") or r.get("total") or "")
        if not cand or cand != want_name:
            continue

        # Line must be 0.5
        lab = r.get("label")
        try:
            labf = float(lab)
        except Exception:
            continue
        if not math.isclose(labf, 0.5, rel_tol=0.0, abs_tol=1e-9):
            continue

        price = to_float(r.get("value"))
        if price is None:
            continue
        if (best is None) or (price > best + 1e-12):
            best = price
    return best

# ---------- tier logic (NEW: clear window used in output) ----------
def tier_info(series: List[int]) -> Tuple[Optional[str], int, List[int], int]:
    """
    Return (tier_label, used_total, used_series, hits) or (None,0,[],0).
    - If 10+ entries, evaluate last10: 10/10, 9/10, 8/10.
    - Else if 7+ entries, evaluate last7: 7/7.
    """
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

# ---------- main ----------
def main():
    # league selection
    env_leagues = os.getenv("LEAGUE_IDS", "").strip()
    if env_leagues:
        LEAGUE_IDS = [int(x) for x in env_leagues.split(",") if x.strip()]
    else:
        LEAGUE_IDS = discover_league_ids()

    generated_at = now_utc().isoformat()
    print(f"Generated at (UTC): {generated_at}")
    print(f"Criteria: SOT certs (10/10,9/10,8/10 or 7/7) | Over 0.5 SOT >= {MIN_DEC_PRICE:.2f} | Team ML < {TEAM_WIN_MAX:.2f} | Window={WINDOW_DAYS} days")

    picks: List[dict] = []
    candidates_checked = 0

    for lid in LEAGUE_IDS:
        sot_blob = read_json(SOT_DIR / f"{lid}.json") or {}
        players = sot_blob.get("players") or []
        fixtures = index_fixtures_by_team(lid)

        for rec in players:
            # Prepare history
            series = rec.get("on_target_last_n") or []
            tier, used_total, used_series, hits = tier_info(series)
            if not tier:
                continue

            # find upcoming fixture for this player's team
            tname = rec.get("team_name") or rec.get("team") or ""
            if not tname:
                continue

            # choose the first upcoming match within window
            chosen_fx = None
            side = None
            for fx in fixtures:
                if not upcoming_within_window(fx.get("starting_at"), WINDOW_DAYS):
                    continue
                parts = fx.get("participants") or []
                if len(parts) < 2:
                    continue
                home = (parts[0] or {}).get("name") or ""
                away = (parts[1] or {}).get("name") or ""
                if team_names_match(tname, home):
                    chosen_fx, side = fx, "home"; break
                if team_names_match(tname, away):
                    chosen_fx, side = fx, "away"; break
            if not chosen_fx or not side:
                continue

            fid = int(chosen_fx.get("id") or 0)
            if not fid:
                continue

            # read Bet365 odds for that fixture
            odds_blob = read_json(ODDS_FIX / f"{fid}.json") or {}
            rows = odds_blob.get("odds") or []

            # team ML filter
            tml = extract_team_ml(rows, side)
            if tml is None or not (tml < TEAM_WIN_MAX):
                continue

            # Over 0.5 SOT price for this player
            player_name = rec.get("name") or ""
            price = extract_player_sot_over_point5(rows, player_name)
            if price is None or price < MIN_DEC_PRICE:
                continue

            # Passed all filters — record
            name = chosen_fx.get("name") or ""
            starting_at = chosen_fx.get("starting_at") or ""
            candidates_checked += 1
            picks.append({
                "player": player_name,
                "team": tname,
                "fixture": name,
                "kickoff": starting_at.replace("T"," ").replace("Z",""),
                "side": side,
                "price": float(price),
                "team_ml": float(tml),
                "tier": tier,
                "tier_hits": hits,
                "tier_total": used_total,
                "series_used": used_series,
                "series_full_n": len([x for x in series if isinstance(x,int)]),
                "league_id": lid,
            })

    # Sort: better tiers first, then price desc
    tier_rank = {"10/10": 4, "9/10": 3, "8/10": 2, "7/7": 1}
    picks.sort(key=lambda r: (-tier_rank.get(r["tier"], 0), -r["price"], r["player"]))

    print(f"Candidates scanned (qualified by history before odds/ML): {len(picks)}\n")
    if not picks:
        print("No SOT certs found.")
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
        main()
    except KeyboardInterrupt:
        pass
