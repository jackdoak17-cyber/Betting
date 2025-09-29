# scripts/common.py
# Shared helpers for SportMonks calls + small utilities.
# This version is 429-aware: it waits until the rate limit resets and then retries.

from __future__ import annotations
import os
import time
import random
import datetime as dt
from typing import Dict, Optional, List, Tuple

import requests

# ------------ API config ------------
API_BASE = "https://api.sportmonks.com/v3"
SPORT = "football"
API_TOKEN = os.getenv("SPORTMONKS_TOKEN") or os.getenv("SPORTMONKS_API_TOKEN") or "YOUR_TOKEN_HERE"

TIMEOUT = 25
RETRIES = 6          # total attempts (including after 429 sleep)
BASE_SLEEP = 1.6     # base backoff for transient errors
JITTER = 0.4
MAX_WAIT_UNTIL = 3700  # cap any single 429 sleep to ~1h 1m so jobs don't hang forever

# ------------ tiny in-memory cache ------------
class Memo:
    def __init__(self):
        self.store: Dict[str, dict] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: dict):
        self.store[key] = value

memo = Memo()

# ------------ rate-limit–aware HTTP ------------
def _seconds_until_next_hour_utc() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    nxt = (now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1))
    return max(1, int((nxt - now).total_seconds()))

def http_get_rl(url: str, params: dict) -> requests.Response:
    """
    GET with robust handling:
      - 429 Too Many Requests -> sleep until reset (header or next UTC hour), then retry
      - 5xx -> exponential backoff
      - other non-200 -> raise with response body excerpt
    """
    attempt = 0
    last_err: Optional[Exception] = None

    while attempt < RETRIES:
        try:
            r = requests.get(url, params=params, timeout=TIMEOUT)
            if r.status_code == 200:
                return r

            # Server hiccups: backoff and retry
            if r.status_code in (500, 502, 503, 504):
                attempt += 1
                sleep = BASE_SLEEP * (1.8 ** attempt) + random.uniform(0, JITTER)
                print(f"[HTTP {r.status_code}] {url} — retrying in {sleep:.1f}s", flush=True)
                time.sleep(min(sleep, 30))
                continue

            # Rate limit: wait until reset, then retry
            if r.status_code == 429:
                reset_hdr = r.headers.get("X-RateLimit-Reset") or r.headers.get("x-ratelimit-reset")
                if reset_hdr:
                    try:
                        reset_ts = int(reset_hdr)
                        now = int(time.time())
                        wait_s = max(1, min(MAX_WAIT_UNTIL, reset_ts - now + 1))
                    except Exception:
                        wait_s = _seconds_until_next_hour_utc()
                else:
                    # Fallback: assume hourly window
                    wait_s = _seconds_until_next_hour_utc()

                print(f"[RL] 429 for {url}. Sleeping {wait_s}s until reset…", flush=True)
                time.sleep(wait_s)
                attempt += 1
                continue

            # Other client errors: raise with JSON/text
            try:
                j = r.json()
            except Exception:
                j = {"message": r.text[:300]}
            raise requests.HTTPError(f"{r.status_code} {r.reason} for {r.url}\nResponse JSON: {j}")

        except requests.RequestException as e:
            last_err = e
            attempt += 1
            sleep = BASE_SLEEP * (1.8 ** attempt) + random.uniform(0, JITTER)
            print(f"[NET] {url} exception: {e}. Retrying in {sleep:.1f}s", flush=True)
            time.sleep(min(sleep, 30))

    if last_err:
        raise last_err
    raise RuntimeError("http_get_rl exhausted without success")

def cached_get(url: str, params: Optional[dict] = None) -> dict:
    """Memoized GET that adds api_token and uses 429-aware http_get_rl."""
    params = dict(params or {})
    params["api_token"] = API_TOKEN
    key = url + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    cached = memo.get(key)
    if cached is not None:
        return cached
    r = http_get_rl(url, params)
    j = r.json()
    memo.set(key, j)
    return j

def api_get(path: str, params: Optional[dict] = None) -> dict:
    """SportMonks path wrapper, uses cached_get."""
    url = f"{API_BASE}/{SPORT}/{path.lstrip('/')}"
    return cached_get(url, params or {})

# ------------ small date/utility helpers (used across scripts) ------------
DATE_FMT = "%Y-%m-%d"

def today_utc() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()

def days_ahead(d: dt.date, n: int) -> dt.date:
    return d + dt.timedelta(days=n)

def daterange_str(start: dt.date, end_inclusive: dt.date) -> List[str]:
    out: List[str] = []
    d = start
    while d <= end_inclusive:
        out.append(d.strftime(DATE_FMT))
        d += dt.timedelta(days=1)
    return out

def pos_id_to_label(position_id: Optional[int]) -> str:
    return {24: "GK", 25: "DEF", 26: "MID", 27: "FWD"}.get(position_id or 0, "?")

def pick_home_away(participants: List[dict]) -> Tuple[Optional[dict], Optional[dict]]:
    home = next((p for p in participants if (p.get("meta") or {}).get("location") == "home"), None)
    away = next((p for p in participants if (p.get("meta") or {}).get("location") == "away"), None)
    return home, away
