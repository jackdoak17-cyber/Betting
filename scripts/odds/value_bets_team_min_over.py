name: value-bets-team-min-over

on:
  workflow_dispatch:
  schedule:
    - cron: '17 7 * * *'

jobs:
  value-bets-team-min-over:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Debug repo tree
        run: |
          set -xe
          pwd
          git rev-parse --short HEAD || true
          ls -la
          echo "---- shallow tree (first 200 files) ----"
          find . -maxdepth 3 -type f | sed 's|^\./||' | sort | head -n 200

      - name: Pre-flight checks (script path + secret)
        env:
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
        run: |
          set -e
          if [ -z "$ODDS_API_KEY" ]; then
            echo "::error::Missing ODDS_API_KEY secret (Settings > Secrets and variables > Actions)"
            exit 1
          fi
          if [ ! -f scripts/odds/value_bets_team_min_over.py ]; then
            echo "::error::Missing file: scripts/odds/value_bets_team_min_over.py"
            exit 1
          fi
          echo "Pre-flight OK."

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install deps
        run: |
          python -m pip install --upgrade pip
          if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
          pip install requests

      - name: Rebuild team-lines (p80/p100)
        run: |
          if [ -f scripts/build_fixture_team_lines.py ]; then
            python scripts/build_fixture_team_lines.py
          else
            echo "build_fixture_team_lines.py not found - skipping"
          fi

      - name: Find Team MIN Over candidates
        env:
          ODDS_API_KEY: ${{ secrets.ODDS_API_KEY }}
          MIN_DEC_PRICE: '1.20'
          CAPTURE_FLOOR: '1.10'
          WINDOW_DAYS: '7'
          BOOKMAKERS: 'Bet365'
        run: |
          python scripts/odds/value_bets_team_min_over.py

      - name: Commit & push
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add data/team_lines data/value_bets
          if git diff --cached --quiet; then
            echo "No changes to commit."
          else
            git commit -m "feat(team-min-over): refresh $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
            git push
          fi
