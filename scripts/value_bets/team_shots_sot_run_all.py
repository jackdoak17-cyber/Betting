#!/usr/bin/env python3
"""Run all Team Shots & SOT versions (V2–V4) in sequence.

This helper simply invokes each versioned workflow so reports and bet sheets
stay in sync. It is intended for scheduled runs; see the cron example below.

Cron example (UTC):
  0 1,16 * * * /usr/bin/python /workspace/Betting/scripts/value_bets/team_shots_sot_run_all.py >> /workspace/Betting/logs/team_shots_run_all.log 2>&1

Environment variables are passed through to the underlying scripts unchanged.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATHS = [
    ROOT / "scripts" / "value_bets" / "Team bets V2" / "team_shots_sot_v2.py",
    ROOT / "scripts" / "value_bets" / "Team bets V3" / "team_shots_sot_v3.py",
    ROOT / "scripts" / "value_bets" / "Team bets V4" / "team_shots_sot_v4.py",
]


def run_script(path: Path) -> None:
    print(f"\n=== Running {path} ===")
    if not path.exists():
        raise FileNotFoundError(f"Missing script: {path}")
    subprocess.run([sys.executable, str(path)], check=True)


def main() -> None:
    for path in SCRIPT_PATHS:
        run_script(path)


if __name__ == "__main__":
    main()
