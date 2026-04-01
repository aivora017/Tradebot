# ================================================================
#  backtest_feed.py — BacktestFeed: Drop-in mock of MarketDataFeed
# ================================================================
#
#  Implements the same public interface as MarketDataFeed so that
#  StrategyEngine can be run unmodified against historical data.
#
#  Usage (driven by backtest.py):
#
#    feed = BacktestFeed()
#    feed.load("NIFTY",     nifty_bars)      # List[BarRow]
#    feed.load("BANKNIFTY", banknifty_bars)
#
#    for bar in nifty_bars:
#        feed.advance("NIFTY", bar)          # sets utils._SIMULATED_NOW
#        tick = feed.make_tick("NIFTY")      # build synthetic Tick from bar
#        signals = engine.evaluate(tick)
#
#  Stubs (return safe defaults — not used in strategy logic):
#    get_chain()         → []
#    get_pcr()           → 1.0
#    get_max_pain()      → 0.0
#    get_iv_percentile() → 0.0
#    get_greeks()        → minimal safe dict
# ================================================================

import utils                          # needed to set _SIMULATED_NOW
from collections import deque, defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from data_feed import Candle, Tick, OptionRow
from backtest_data import BarRow


# ── 15-minute bar aggregation ─────────────────────────────────────

def _to_15m(bars_5m: List[Candle]) -> List[Candle]:
    """
    Aggregate 5-minute Candle list into 15-minute Candle list.
    Groups by 15-min bucket (floor to nearest :00/:15/:30/:45).
    """
    if not bars_5m:
        return []

    buckets: dict = {}
    for c in bars_5m:
        m = c.ts.minute
        bucket_min = (m // 15) * 15
        bucket_ts  = c.ts.replace(minute=bucket_min, second=0, microsecond=0)
        if bucket_ts not in buckets:
            buckets[bucket_ts] = {
                "open":   c.open,
                "high":   c.high,
                "low":    c.low,
                "close":  c.close,
                "volume": c.volume,
            }
        else:
            b = buckets[bucket_ts]
            b["high"]   = max(b["high"],   c.high)
            b["low"]    = min(b["low"],    c.low)
            b["close"]  = c.close
            b["volume"] += c.volume

    return [
        Candle(
            open   = v["open"],
            high   = v["high"],
            low    = v["low"],
            close  = v["close"],
            volume = v["volume"],
            ts     = ts,
        )
        for ts, v in sorted(buckets.items())
    ]


# ── BacktestFeed ───────────────────────────────────────────────────

class BacktestFeed:
    """
    Stateful mock of MarketDataFeed.

    State advances one 5m bar at a time via advance().
    StrategyEngine reads candles and ticks from this object
    without knowing it's backed by historical data.
    """

    def __init__(self):
        # Pre-loaded bars per symbol: {symbol: [BarRow, ...]}
        self._all_bars:   Dict[str, List[BarRow]] = {}
        # Index of the CURRENT bar (exclusive upper bound for get_candles)
        self._cursor:     Dict[str, int] = {}
        # Current bar's BarRow (used for get_tick)
        self._current:    Dict[str, BarRow] = {}
        # VIX override (set per day from config or user-supplied)
        self._vix: float = 15.0

    # ── Setup ─────────────────────────────────────────────────────

    def load(self, symbol: str, bars: List[BarRow]) -> None:
        """Load pre-fetched bars for a symbol. Call before replay."""
        self._all_bars[symbol]  = bars
        self._cursor[symbol]    = 0
        self._current[symbol]   = bars[0] if bars else None

    def set_vix(self, vix: float) -> None:
        """Override VIX for the current bar. Default = 15.0."""
        self._vix = vix

    # ── Replay control ────────────────────────────────────────────

    def advance(self, symbol: str, bar: BarRow) -> None:
        """
        Advance to the given bar:
          1. Find bar's index in the pre-loaded list and update cursor.
          2. Set utils._SIMULATED_NOW to bar.dt so all time helpers
             (is_primary_window, is_expiry_day, etc.) use bar time.
        """
        bars = self._all_bars.get(symbol, [])
        # Find the position of this bar by datetime
        for i, b in enumerate(bars):
            if b.dt == bar.dt:
                self._cursor[symbol]  = i + 1   # exclusive: bars[:i+1]
                self._current[symbol] = bar
                break
        # Drive simulated clock from the first-loaded symbol's bar
        utils._SIMULATED_NOW = bar.dt

    def reset_day(self, symbol: str) -> None:
        """Reset cursor for a new trading day (does NOT clear all_bars)."""
        self._cursor[symbol]  = 0
        self._current[symbol] = None

    # ── MarketDataFeed public API ─────────────────────────────────

    def get_tick(self, symbol: str) -> Optional[Tick]:
        """Return synthetic Tick from current bar."""
        bar = self._current.get(symbol)
        if bar is None:
            return None
        return Tick(
            symbol     = symbol,
            ltp        = bar.close,
            open       = bar.open,
            high       = bar.high,
            low        = bar.low,
            prev_close = self._get_prev_close(symbol),
            volume     = bar.volume,
            oi         = 0,
            bid        = bar.close - 0.5,
            ask        = bar.close + 0.5,
            ts         = bar.dt,
        )

    def make_tick(self, symbol: str) -> Optional[Tick]:
        """Alias for get_tick — used by backtest runner for clarity."""
        return self.get_tick(symbol)

    def get_ltp(self, symbol: str) -> float:
        t = self.get_tick(symbol)
        return t.ltp if t else 0.0

    def get_vix(self) -> float:
        return self._vix

    def get_candles(self, symbol: str, interval: str = "5m", n: int = 100) -> List[Candle]:
        """Return candles from bars up to (not including) current cursor."""
        bars = self._all_bars.get(symbol, [])
        cursor = self._cursor.get(symbol, 0)
        # Include bars up to but not past current bar
        window = bars[max(0, cursor - 300):cursor]   # cap at 300 5m bars

        # Convert BarRow → Candle
        candles_5m = [
            Candle(
                open   = b.open,
                high   = b.high,
                low    = b.low,
                close  = b.close,
                volume = b.volume,
                ts     = b.dt,
            )
            for b in window
        ]

        if interval == "15m":
            result = _to_15m(candles_5m)
        elif interval == "1m":
            result = candles_5m   # no 1m data; use 5m as proxy
        else:
            result = candles_5m

        return result[-n:]

    def get_chain(self, symbol: str) -> List[OptionRow]:
        """No option chain data in backtest mode — return empty list."""
        return []

    def get_pcr(self, symbol: str = "BANKNIFTY") -> float:
        return 1.0

    def get_max_pain(self, symbol: str) -> float:
        return 0.0

    def get_iv_percentile(self, symbol: str) -> float:
        return 0.0

    def get_greeks(self, symbol: str) -> dict:
        """Return minimal safe greeks dict so delta/IV filters pass."""
        import config
        step = 100 if "BANK" in symbol else 50
        bar  = self._current.get(symbol)
        atm  = (round(bar.close / step) * step) if bar else 0
        # In backtest: delta = 0.5 (ATM), IV not overpriced → all trades pass delta check
        return {
            "atm_strike":    atm,
            "iv_percentile": 0.0,
            "atm_ce_iv":     15.0,
            "atm_pe_iv":     15.0,
            "delta_ok_ce":   True,
            "delta_ok_pe":   True,
            "iv_overpriced": False,
            "strikes":       {},
        }

    def is_connected(self) -> bool:
        return True

    # ── Internals ─────────────────────────────────────────────────

    def _get_prev_close(self, symbol: str) -> float:
        """Return close of the bar immediately before the day's first bar."""
        bars   = self._all_bars.get(symbol, [])
        cursor = self._cursor.get(symbol, 0)
        if cursor < 2:
            # First bar of the entire dataset
            return bars[0].open if bars else 0.0

        # Walk back to find the last bar from the PREVIOUS trading day
        current_bar = bars[cursor - 1]
        current_day = current_bar.dt.date()
        for i in range(cursor - 2, -1, -1):
            if bars[i].dt.date() != current_day:
                return bars[i].close
        # All bars are same day — use the first bar's open
        return bars[0].open
