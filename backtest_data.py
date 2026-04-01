# ================================================================
#  backtest_data.py — Upstox Historical Candle Fetcher & Cache
# ================================================================
#
#  Fetches 3 months of 5-minute OHLCV candles from Upstox REST API.
#  Caches each symbol to JSON in trading/backtest_cache/ to avoid
#  re-fetching on every backtest run.
#
#  API endpoint:
#    GET /v2/historical-candle/{instrumentKey}/{interval}/{to}/{from}
#    interval = 5minute
#    dates    = YYYY-MM-DD
#
#  Usage:
#    from backtest_data import load_candles
#    bars = load_candles("NIFTY", force_refresh=False)
#    # bars: list of BarRow(dt, open, high, low, close, volume)
# ================================================================

import os
import json
import logging
import requests
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from typing import List, Optional
from pathlib import Path

log = logging.getLogger("backtest_data")

# ── Config ────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent / "backtest_cache"
CACHE_DIR.mkdir(exist_ok=True)

# Upstox instrument keys for index historical data
INSTRUMENT_KEY_MAP = {
    "NIFTY":     "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}

INTERVAL   = "5minute"
LOOKBACK_DAYS = 92   # ~3 months


# ── Data Model ────────────────────────────────────────────────────

@dataclass
class BarRow:
    dt:     datetime   # bar open timestamp
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


# ── Cache helpers ─────────────────────────────────────────────────

def _cache_path(symbol: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_5m.json"

def _cache_is_fresh(symbol: str) -> bool:
    """Cache valid if file exists and was written today."""
    p = _cache_path(symbol)
    if not p.exists():
        return False
    mtime = datetime.fromtimestamp(p.stat().st_mtime).date()
    return mtime == date.today()

def _save_cache(symbol: str, bars: List[BarRow]) -> None:
    payload = [
        {
            "dt":     b.dt.strftime("%Y-%m-%d %H:%M:%S"),
            "open":   b.open,
            "high":   b.high,
            "low":    b.low,
            "close":  b.close,
            "volume": b.volume,
        }
        for b in bars
    ]
    with open(_cache_path(symbol), "w") as f:
        json.dump(payload, f)
    log.info(f"[backtest_data] Cached {len(bars)} bars for {symbol}")

def _load_cache(symbol: str) -> List[BarRow]:
    with open(_cache_path(symbol)) as f:
        raw = json.load(f)
    bars = [
        BarRow(
            dt     = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S"),
            open   = float(r["open"]),
            high   = float(r["high"]),
            low    = float(r["low"]),
            close  = float(r["close"]),
            volume = int(r["volume"]),
        )
        for r in raw
    ]
    log.info(f"[backtest_data] Loaded {len(bars)} bars from cache for {symbol}")
    return bars


# ── Upstox API fetch ──────────────────────────────────────────────

def _get_access_token() -> str:
    """Read access token from .env or environment."""
    # Try environment first
    token = os.environ.get("UPSTOX_ACCESS_TOKEN", "")
    if token:
        return token
    # Fallback: read from .env file next to this script
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("UPSTOX_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError(
        "UPSTOX_ACCESS_TOKEN not found. Set env var or add to trading/.env"
    )

def _fetch_from_upstox(symbol: str) -> List[BarRow]:
    """
    Fetch 5m candles for the last LOOKBACK_DAYS from Upstox v2 API.
    Upstox returns max 1 year of 5m data; we chunk into 30-day windows
    to stay within their per-request limit (they cap at ~100 days per call
    but the v2 spec allows the full range in one shot — we chunk defensively).
    """
    instrument_key = INSTRUMENT_KEY_MAP.get(symbol.upper())
    if not instrument_key:
        raise ValueError(f"Unknown symbol for backtest: {symbol}")

    token  = _get_access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    today    = date.today()
    from_dt  = today - timedelta(days=LOOKBACK_DAYS)

    # Upstox allows up to 1 year per request for 5-minute data
    # Use a single request; fallback to 30-day chunks if 400 received
    bars: List[BarRow] = []

    def _fetch_window(from_date: date, to_date: date) -> List[BarRow]:
        # URL-encode the instrument key
        from urllib.parse import quote
        key_enc = quote(instrument_key, safe="")
        url = (
            f"https://api.upstox.com/v2/historical-candle/{key_enc}"
            f"/{INTERVAL}/{to_date.strftime('%Y-%m-%d')}"
            f"/{from_date.strftime('%Y-%m-%d')}"
        )
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        candles = data.get("data", {}).get("candles", [])
        result  = []
        for c in candles:
            # Upstox format: [timestamp, open, high, low, close, volume, oi]
            dt_str = c[0]  # e.g. "2025-12-01T09:15:00+05:30"
            dt     = datetime.fromisoformat(dt_str).replace(tzinfo=None)
            result.append(BarRow(
                dt     = dt,
                open   = float(c[1]),
                high   = float(c[2]),
                low    = float(c[3]),
                close  = float(c[4]),
                volume = int(c[5]),
            ))
        return result

    try:
        bars = _fetch_window(from_dt, today)
        log.info(f"[backtest_data] Fetched {len(bars)} bars for {symbol} in single request")
    except requests.HTTPError as e:
        if e.response.status_code == 400:
            log.warning(f"[backtest_data] Single-window fetch failed (400). Chunking into 30d windows.")
            # Fall back to 30-day chunks
            cursor = from_dt
            while cursor < today:
                chunk_end = min(cursor + timedelta(days=30), today)
                chunk = _fetch_window(cursor, chunk_end)
                bars.extend(chunk)
                cursor = chunk_end + timedelta(days=1)
            log.info(f"[backtest_data] Chunked fetch: {len(bars)} total bars for {symbol}")
        else:
            raise

    # Sort ascending by timestamp (Upstox returns newest-first)
    bars.sort(key=lambda b: b.dt)
    return bars


# ── Public API ────────────────────────────────────────────────────

def load_candles(symbol: str, force_refresh: bool = False) -> List[BarRow]:
    """
    Return sorted 5-minute bars for the last 3 months.

    Args:
        symbol:        "NIFTY" or "BANKNIFTY"
        force_refresh: if True, re-fetch from Upstox even if cache is fresh

    Returns:
        list of BarRow, sorted ascending by dt
    """
    if not force_refresh and _cache_is_fresh(symbol):
        return _load_cache(symbol)

    bars = _fetch_from_upstox(symbol)
    if bars:
        _save_cache(symbol, bars)
    return bars


def load_candles_by_day(symbol: str, force_refresh: bool = False) -> dict:
    """
    Return {date_str: [BarRow, ...]} — bars grouped by trading day.
    Only includes days with market hours data (9:15–15:30 IST).

    Example:
        {"2025-12-02": [BarRow(...), ...], ...}
    """
    all_bars = load_candles(symbol, force_refresh=force_refresh)
    days: dict = {}
    for bar in all_bars:
        day_key = bar.dt.strftime("%Y-%m-%d")
        days.setdefault(day_key, []).append(bar)
    return days
