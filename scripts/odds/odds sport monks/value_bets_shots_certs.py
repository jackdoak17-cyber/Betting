#!/usr/bin/env python3
"""
value_bets_shots_certs.py

End-to-end, no-surprises runner for "shots certs" (player prop) value scanning.

- Source of truth for fixtures: data/fixtures/latest.json
- Odds source: local stubs (if present) in data/odds/by_fixture/{fixture_id}.json
  *This script does NOT call external APIs.* It is safe to run offline.
- Candidate list (optional):
    - data/candidates/shots_series7_all1.csv
    - or data/candidates/shots_series7_all1.json
  If missing, the pipeline still runs and emits an empty shortlist.

Outputs (created if not present):
- reports/props_latest.csv
- reports/props_latest.json
- reports/digest_latest.md

All times are UTC. No third-party packages required (stdlib only).
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# ----------------------------
# Configuration (overridable via CLI)
# ----------------------------
DEFAULT_FIXTURE_FILE = Path("data/fixtures/latest.json")
DEFAULT_CANDIDATE_CSV = Path("data/candidates/shots_series7_all1.csv")
DEFAULT_CANDIDATE_JSON = Path("data/candidates/shots_series7_all1.json")
DEFAULT_ODDS_DIR = Path("data/odds/by_fixture")
REPORTS_DIR = Path("reports")

DEFAULT_STALE_HOURS = 12  # warn if fixtures file is older than this
NOW_UTC = datetime.now(timezone.utc)


# ----------------------------
# Data models
# ----------------------------
@dataclass
class Fixture:
    fixture_id: int
    league_id: Optional[int]
    kickoff_utc: datetime
    home_team: str
    away_team: str


@dataclass
class Candidate:
    player: str
    market: str  # 'shots' or 'shots_on_target' (case-insensitive)
    # If both are set, 'line' is used as the target and 'min_line' as a floor.
    line: Optional[float] = None
    min_line: Optional[float] = None


@dataclass
class OddsRow:
    fixture_id: int
    player: str
    market: str            # 'shots' or 'shots_on_target' (case-insensitive)
    line: float            # numerical line, e.g., 0.5, 1.0, 1.5
    selection: str         # 'Over' or 'Under'
    price_decimal: float   # decimal odds, e.g., 1.85
    bookmaker: str


# ----------------------------
# Utilities
# ----------------------------
def _safe_float(x) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _parse_utc_ts(s: str) -> Optional[datetime]:
    """
    Parse "YYYY-MM-DD HH:MM:SS" or ISO8601 with or without 'Z' as UTC.
    """
    if not s:
        return None
    try:
        # Try "YYYY-MM-DD HH:MM:SS"
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:
        # ISO8601 (with or without Z)
        if s.endswith("Z"):
            s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s).astimezone(timezone.utc)
    except Exception:
        return None


def _ensure_reports_dir() -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ----------------------------
# Fixtures
# ----------------------------
def load_local_fixtures(path: Path, stale_hours: int) -> Tuple[Dict[int, Fixture], Dict, List[str]]:
    """
    Returns:
        fixtures_by_id: {fixture_id: Fixture}
        meta: meta dict from file
        warnings: list of warning strings
    """
    warnings: List[str] = []
    if not path.exists():
        raise FileNotFoundError(f"Fixtures file not found: {path.resolve()}")

    raw = _read_json(path)
    if not raw:
        raise ValueError(f"Could not parse JSON from {path.resolve()}")

    meta = raw.get("meta", {})
    gen_at = meta.get("generated_at")

    if gen_at:
        try:
            iso = gen_at.replace("Z", "+00:00")
            gen_dt = datetime.fromisoformat(iso).astimezone(timezone.utc)
            age_hours = (NOW_UTC - gen_dt).total_seconds() / 3600.0
            if age_hours > stale_hours:
                warnings.append(
                    f"[WARN] Fixtures snapshot is {age_hours:.1f}h old "
                    f"(generated_at={gen_dt.isoformat()}); consider refreshing."
                )
        except Exception:
            warnings.append("[WARN] Could not parse meta.generated_at; skipping staleness check.")

    fixtures_by_id: Dict[int, Fixture] = {}
    for f in raw.get("fixtures", []):
        fixture_id = f.get("id")
        league_id = f.get("league_id")
        kickoff = _parse_utc_ts(f.get("starting_at"))
        # fallback to timestamp if needed
        if not kickoff:
            ts = f.get("starting_at_timestamp")
            try:
                kickoff = datetime.fromtimestamp(int(ts), tz=timezone.utc) if ts else None
            except Exception:
                kickoff = None

        # Parse participants (home/away)
        home_team, away_team = "HOME", "AWAY"
        for p in f.get("participants", []):
            loc = (p.get("meta") or {}).get("location", "").lower().strip()
            name = p.get("name") or p.get("short_code") or "UNKNOWN"
            if loc == "home":
                home_team = name
            elif loc == "away":
                away_team = name

        if fixture_id is None or kickoff is None:
            # Skip malformed entries quietly
            continue

        fixtures_by_id[int(fixture_id)] = Fixture(
            fixture_id=int(fixture_id),
            league_id=int(league_id) if league_id is not None else None,
            kickoff_utc=kickoff,
            home_team=str(home_team),
            away_team=str(away_team),
        )

    return fixtures_by_id, meta, warnings


# ----------------------------
# Candidates
# ----------------------------
def load_candidates(csv_path: Path, json_path: Path) -> List[Candidate]:
    """
    Flexible loader. Accepts either CSV or JSON; if both exist, CSV wins.
    Expected fields (case-insensitive):
      - player (str) [required]
      - market (str) 'shots' | 'shots_on_target' [required]
      - line (float) [optional]
      - min_line (float) [optional]
    """
    if csv_path.exists():
        rows: List[Candidate] = []
        try:
            with csv_path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    player = (r.get("player") or "").strip()
                    market = (r.get("market") or "").strip().lower()
                    if not player or not market:
                        continue
                    line = _safe_float(r.get("line"))
                    min_line = _safe_float(r.get("min_line"))
                    rows.append(Candidate(player=player, market=market, line=line, min_line=min_line))
            return rows
        except Exception:
            # Fall through to try JSON
            pass

    if json_path.exists():
        try:
            data = _read_json(json_path)
            out: List[Candidate] = []
            for r in (data or []):
                player = (r.get("player") or "").strip()
                market = (r.get("market") or "").strip().lower()
                if not player or not market:
                    continue
                line = _safe_float(r.get("line"))
                min_line = _safe_float(r.get("min_line"))
                out.append(Candidate(player=player, market=market, line=line, min_line=min_line))
            return out
        except Exception:
            return []

    # No candidates file found → empty list is fine
    return []


# ----------------------------
# Odds (local stub reader)
# ----------------------------
def _parse_normalized_odds_rows(raw: Iterable[dict], fallback_fixture_id: int) -> List[OddsRow]:
    """
    Accepts an iterable of dicts that (ideally) match our OddsRow schema.
    Missing/extra fields are tolerated; non-parsable rows are skipped.
    """
    out: List[OddsRow] = []
    for r in raw:
        try:
            fixture_id = int(r.get("fixture_id", fallback_fixture_id))
            player = str(r.get("player") or "").strip()
            market = str(r.get("market") or "").strip().lower()
            line = _safe_float(r.get("line"))
            selection = str(r.get("selection") or "").strip().title()  # normalize to 'Over'/'Under'
            price = _safe_float(r.get("price_decimal"))
            bookmaker = str(r.get("bookmaker") or "").strip() or "BOOK"

            if not player or not market or line is None or price is None:
                continue
            if selection not in {"Over", "Under"}:
                # default to Over if unspecified
                selection = "Over"

            out.append(
                OddsRow(
                    fixture_id=fixture_id,
                    player=player,
                    market=market,
                    line=float(line),
                    selection=selection,
                    price_decimal=float(price),
                    bookmaker=bookmaker,
                )
            )
        except Exception:
            # Skip bad rows safely
            continue
    return out


def load_local_odds_for_fixture(odds_dir: Path, fixture_id: int) -> List[OddsRow]:
    """
    Attempts to read odds from data/odds/by_fixture/{fixture_id}.json.
    Two accepted shapes:
      1) Direct list[dict] matching normalized OddsRow keys.
      2) Object containing a 'rows' list with normalized dicts.
    Any other shapes will be ignored gracefully.
    """
    file_path = odds_dir / f"{fixture_id}.json"
    if not file_path.exists():
        return []

    raw = _read_json(file_path)
    if raw is None:
        return []

    if isinstance(raw, list):
        return _parse_normalized_odds_rows(raw, fixture_id)
    if isinstance(raw, dict):
        rows = raw.get("rows")
        if isinstance(rows, list):
            return _parse_normalized_odds_rows(rows, fixture_id)

    # Unknown structure → skip
    return []


def load_odds_for_fixtures(odds_dir: Path, fixture_ids: Iterable[int]) -> List[OddsRow]:
    all_rows: List[OddsRow] = []
    for fid in fixture_ids:
        all_rows.extend(load_local_odds_for_fixture(odds_dir, fid))
    return all_rows


# ----------------------------
# Shortlisting / matching
# ----------------------------
def build_candidate_index(cands: List[Candidate]) -> Dict[Tuple[str, str], Candidate]:
    """
    Canonical key: (player_lower, market_lower)
    If duplicates exist, prefer the one with a concrete 'line' over only 'min_line'.
    """
    idx: Dict[Tuple[str, str], Candidate] = {}
    for c in cands:
        key = (c.player.strip().lower(), c.market.strip().lower())
        prev = idx.get(key)
        if prev is None:
            idx[key] = c
        else:
            # Prefer more specific
            prev_has_line = prev.line is not None
            cur_has_line = c.line is not None
            if cur_has_line and not prev_has_line:
                idx[key] = c
    return idx


def shortlist_rows(odds_rows: List[OddsRow], cand_index: Dict[Tuple[str, str], Candidate]) -> List[OddsRow]:
    """
    Keep rows that:
      - have selection == 'Over'
      - match a candidate by (player, market)
      - meet the candidate's required line constraint (>= min_line and/or == line if specified)
    """
    if not cand_index:
        # No candidates → empty shortlist (by design)
        return []

    out: List[OddsRow] = []
    for r in odds_rows:
        key = (r.player.strip().lower(), r.market.strip().lower())
        c = cand_index.get(key)
        if not c:
            continue
        if r.selection != "Over":
            continue

        ok = True
        if c.min_line is not None:
            ok = ok and (r.line >= float(c.min_line))
        if c.line is not None:
            # When a precise line is requested, require exact match within small epsilon
            ok = ok and (abs(r.line - float(c.line)) <= 1e-9)

        if ok:
            out.append(r)
    return out


# ----------------------------
# Reporting
# ----------------------------
def write_csv(rows: List[OddsRow], fixtures: Dict[int, Fixture], path: Path) -> None:
    _ensure_reports_dir()
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "fixture_id",
                "kickoff_utc",
                "league_id",
                "home_team",
                "away_team",
                "player",
                "market",
                "line",
                "selection",
                "price_decimal",
                "bookmaker",
            ]
        )
        for r in rows:
            fx = fixtures.get(r.fixture_id)
            kickoff = fx.kickoff_utc.isoformat() if fx else ""
            league_id = fx.league_id if fx else ""
            home = fx.home_team if fx else ""
            away = fx.away_team if fx else ""
            writer.writerow(
                [
                    r.fixture_id,
                    kickoff,
                    league_id,
                    home,
                    away,
                    r.player,
                    r.market,
                    f"{r.line:g}",
                    r.selection,
                    f"{r.price_decimal:.3f}",
                    r.bookmaker,
                ]
            )


def write_json(rows: List[OddsRow], fixtures: Dict[int, Fixture], path: Path) -> None:
    _ensure_reports_dir()
    out: List[dict] = []
    for r in rows:
        fx = fixtures.get(r.fixture_id)
        out.append(
            {
                "fixture_id": r.fixture_id,
                "kickoff_utc": fx.kickoff_utc.isoformat() if fx else None,
                "league_id": fx.league_id if fx else None,
                "home_team": fx.home_team if fx else None,
                "away_team": fx.away_team if fx else None,
                "player": r.player,
                "market": r.market,
                "line": r.line,
                "selection": r.selection,
                "price_decimal": r.price_decimal,
                "bookmaker": r.bookmaker,
            }
        )
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")


def render_digest(
    fixtures: Dict[int, Fixture],
    considered_fixture_ids: List[int],
    odds_rows: List[OddsRow],
    shortlisted: List[OddsRow],
    warnings: List[str],
) -> str:
    # Coverage stats
    total_fixtures = len(considered_fixture_ids)
    fixtures_with_any_odds = {r.fixture_id for r in odds_rows}
    fixtures_with_shortlist = {r.fixture_id for r in shortlisted}
    fixtures_no_odds = [fid for fid in considered_fixture_ids if fid not in fixtures_with_any_odds]

    lines: List[str] = []
    lines.append("# Shots Certs — Digest (UTC)")
    lines.append("")
    lines.append(f"- Generated at: {NOW_UTC.isoformat()}")
    lines.append(f"- Fixtures considered (not started yet): {total_fixtures}")
    lines.append(f"- Fixtures with any player prop odds: {len(fixtures_with_any_odds)}")
    lines.append(f"- Shortlisted selections: {len(shortlisted)}")
    if warnings:
        lines.append("")
        lines.append("## Warnings")
        for w in warnings:
            lines.append(f"- {w}")

    # Notable gaps
    if fixtures_no_odds:
        lines.append("")
        lines.append("## Fixtures with no odds found")
        for fid in fixtures_no_odds[:50]:  # cap listing to avoid huge walls
            fx = fixtures.get(fid)
            if fx:
                lines.append(
                    f"- {fid} — {fx.home_team} vs {fx.away_team} "
                    f"({fx.kickoff_utc.isoformat()}, league={fx.league_id})"
                )
            else:
                lines.append(f"- {fid}")

    # Shortlist preview
    if shortlisted:
        lines.append("")
        lines.append("## Shortlist (sample of up to 50)")
        for r in shortlisted[:50]:
            fx = fixtures.get(r.fixture_id)
            kickoff = fx.kickoff_utc.isoformat() if fx else "?"
            lines.append(
                f"- {r.player} — {r.market} o{r.line:g} @ {r.price_decimal:.2f} "
                f"({r.bookmaker}) | {fx.home_team if fx else '?'} vs {fx.away_team if fx else '?'} | {kickoff}"
            )

    return "\n".join(lines)


def write_digest(text: str, path: Path) -> None:
    _ensure_reports_dir()
    path.write_text(text, encoding="utf-8")


# ----------------------------
# Main
# ----------------------------
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scan shots/shots-on-target player props for precomputed 'certs' candidates."
    )
    parser.add_argument(
        "--fixtures",
        type=Path,
        default=DEFAULT_FIXTURE_FILE,
        help=f"Path to fixtures JSON (default: {DEFAULT_FIXTURE_FILE})",
    )
    parser.add_argument(
        "--odds-dir",
        type=Path,
        default=DEFAULT_ODDS_DIR,
        help=f"Directory with per-fixture odds stubs (default: {DEFAULT_ODDS_DIR})",
    )
    parser.add_argument(
        "--candidates-csv",
        type=Path,
        default=DEFAULT_CANDIDATE_CSV,
        help=f"Optional candidates CSV (default: {DEFAULT_CANDIDATE_CSV})",
    )
    parser.add_argument(
        "--candidates-json",
        type=Path,
        default=DEFAULT_CANDIDATE_JSON,
        help=f"Optional candidates JSON (default: {DEFAULT_CANDIDATE_JSON})",
    )
    parser.add_argument(
        "--stale-hours",
        type=int,
        default=DEFAULT_STALE_HOURS,
        help=f"Warn if fixtures file older than this many hours (default: {DEFAULT_STALE_HOURS})",
    )
    parser.add_argument(
        "--csv-out",
        type=Path,
        default=REPORTS_DIR / "props_latest.csv",
        help="Output CSV path (default: reports/props_latest.csv)",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=REPORTS_DIR / "props_latest.json",
        help="Output JSON path (default: reports/props_latest.json)",
    )
    parser.add_argument(
        "--digest-out",
        type=Path,
        default=REPORTS_DIR / "digest_latest.md",
        help="Output digest markdown path (default: reports/digest_latest.md)",
    )

    args = parser.parse_args(argv)

    # Load fixtures
    fixtures_by_id, meta, warnings = load_local_fixtures(args.fixtures, args.stale_hours)

    # Only consider fixtures that have NOT started yet
    upcoming_ids = [
        fid
        for fid, fx in fixtures_by_id.items()
        if fx.kickoff_utc > NOW_UTC
    ]
    upcoming_ids.sort()

    # Load odds from local stubs (if present)
    odds_rows = load_odds_for_fixtures(args.odds_dir, upcoming_ids)

    # Load candidates (optional)
    candidates = load_candidates(args.candidates_csv, args.candidates_json)
    cand_index = build_candidate_index(candidates)

    # Shortlist
    shortlisted = shortlist_rows(odds_rows, cand_index)

    # Persist outputs
    write_csv(shortlisted, fixtures_by_id, args.csv_out)
    write_json(shortlisted, fixtures_by_id, args.json_out)

    digest_text = render_digest(
        fixtures=fixtures_by_id,
        considered_fixture_ids=upcoming_ids,
        odds_rows=odds_rows,
        shortlisted=shortlisted,
        warnings=warnings,
    )
    write_digest(digest_text, args.digest_out)

    # Console summary
    print("=== Shots Certs Run Complete ===")
    print(f"Fixtures considered: {len(upcoming_ids)}")
    print(f"Odds rows loaded:   {len(odds_rows)}")
    print(f"Shortlisted rows:   {len(shortlisted)}")
    if warnings:
        for w in warnings:
            print(w)
    print(f"CSV:    {args.csv_out}")
    print(f"JSON:   {args.json_out}")
    print(f"Digest: {args.digest_out}")

    # Exit code 0 even if no data — the run is still "successful".
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.", file=sys.stderr)
        sys.exit(130)
