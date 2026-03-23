# ================================================================
#  strategy_engine.py — All 10 Strategies + Real-Time Signals
#  Runs on every tick. Returns actionable Signal objects.
# ================================================================

import threading
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from enum import Enum

import config
from logger_setup import get_logger
from utils import (
    ema, rsi, vwap_from_candles, avg_volume, volume_mult,
    near_level, atm_strike, otm_call, otm_put, lot_size,
    is_primary_window, is_dead_zone, is_expiry_day,
    is_hard_exit_time, time_between, trend_direction,
    next_expiry, vix_size_multiplier, vix_zone
)
from data_feed import Tick, Candle, MarketDataFeed

log = get_logger("Strategy")


class Direction(Enum):
    CE   = "CE"
    PE   = "PE"
    BOTH = "CE+PE"


class Tier(Enum):
    T1 = 1
    T2 = 2
    T3 = 3
    T4 = 4
    T5 = 5


class Strength(Enum):
    WEAK     = 1
    MODERATE = 2
    STRONG   = 3
    GODMODE  = 4


@dataclass
class Signal:
    strategy:       str
    symbol:         str
    direction:      Direction
    tier:           Tier
    strength:       Strength
    entry_trigger:  float
    strike:         int
    expiry:         str
    sl_premium_pct: float
    sl_index:       float
    t1_pct:         float
    t2_pct:         float
    exit_by:        str
    size_pct:       float
    reason:         str
    filters_ok:     List[str] = field(default_factory=list)
    filters_fail:   List[str] = field(default_factory=list)
    ts:             datetime  = field(default_factory=datetime.now)

    @property
    def option_type(self) -> str:
        return self.direction.value

    def __str__(self):
        return (f"[{self.strength.name}] {self.strategy} | "
                f"{self.symbol} {self.strike} {self.option_type} | "
                f"Entry@{self.entry_trigger:.0f} | "
                f"SL:{self.sl_premium_pct*100:.0f}% | "
                f"T1:{self.t1_pct*100:.0f}% T2:{self.t2_pct*100:.0f}%")


class ORBState:
    def __init__(self):
        self._data:      Dict[str, dict] = {}
        self._triggered: Dict[str, bool] = {}
        self._lock = threading.Lock()

    def set(self, symbol: str, high: float, low: float, avg_vol: float):
        with self._lock:
            self._data[symbol] = {"h": high, "l": low, "avg_vol": avg_vol}
            self._triggered[symbol] = False
        log.info(f"ORB set {symbol}: H={high:.0f} L={low:.0f}")

    def is_set(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._data

    def triggered(self, symbol: str) -> bool:
        with self._lock:
            return self._triggered.get(symbol, False)

    def mark_triggered(self, symbol: str):
        with self._lock:
            self._triggered[symbol] = True

    def get(self, symbol: str) -> dict:
        with self._lock:
            return self._data.get(symbol, {})

    def reset(self):
        with self._lock:
            self._data.clear()
            self._triggered.clear()


ORB = ORBState()


class StrategyEngine:
    SIGNAL_COOLDOWN = 300

    def __init__(self, feed: MarketDataFeed):
        self.feed  = feed
        self._last: Dict[str, datetime] = {}
        self._lock = threading.Lock()

    def evaluate(self, tick: Tick) -> List[Signal]:
        signals = []
        vix = self.feed.get_vix()

        if vix > 0 and vix < config.VIX_AVOID:
            return []
        if is_hard_exit_time():
            return []
        if is_dead_zone():
            return []

        symbol = self._resolve(tick)
        if not symbol:
            return []

        price = tick.ltp
        c5    = self.feed.get_candles(symbol, "5m")
        c15   = self.feed.get_candles(symbol, "15m")
        c1    = self.feed.get_candles(symbol, "1m")

        if len(c5) < 2:
            return []

        if time_between(config.MARKET_OPEN, config.ORB_WINDOW_END):
            self._build_orb(symbol, c5)

        runners = [
            self._s1_orb, self._s2_straddle, self._s3_gap_go,
            self._s4_trend_cont, self._s5_15min_break, self._s6_sr_reversal,
            self._s7_vwap, self._s8_engulfing, self._s9_gamma, self._s10_ema,
        ]

        for fn in runners:
            try:
                sig = fn(tick, symbol, price, c5, c15, c1, vix)
                if sig and self._cooldown_ok(symbol, sig.strategy):
                    signals.append(sig)
                    self._update_cooldown(symbol, sig.strategy)
                    log.info(f"🎯 {sig}")
            except Exception as e:
                log.debug(f"{fn.__name__} error: {e}")

        return signals

    # ── S1: ORB Breakout ─────────────────────────────────────────

    def _s1_orb(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between(config.PRIMARY_ENTRY_START, config.PRIMARY_ENTRY_END):
            return None
        if not ORB.is_set(symbol) or ORB.triggered(symbol):
            return None

        orb = ORB.get(symbol)
        h, l = orb["h"], orb["l"]
        rng  = h - l
        max_range = (config.PARAMS.ORB_BN_MAX_RANGE if "BANK" in symbol
                     else config.PARAMS.ORB_NIFTY_MAX_RANGE)
        ok, fail = [], []

        if rng <= max_range:
            ok.append(f"Range {rng:.0f}pts ok")
        else:
            return None

        vm = volume_mult(c5, 20)
        if vm >= config.PARAMS.ORB_VOLUME_MULT:
            ok.append(f"Vol {vm:.1f}x confirmed")
        else:
            fail.append(f"Vol weak {vm:.1f}x")

        if price > h and not fail:
            ORB.mark_triggered(symbol)
            return self._sig(
                "ORB Breakout", symbol, Direction.CE, Tier.T1, price, atm_strike(price, symbol),
                sl_p=config.SL_PREMIUM_T1_3, sl_i=h - rng * 0.3,
                t1=config.T1_PREMIUM_GAIN, t2=config.T2_PREMIUM_GAIN,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"ORB breakout above {h:.0f} with {vm:.1f}x vol",
                ok=ok, fail=fail, vix=vix
            )
        elif price < l and not fail:
            ORB.mark_triggered(symbol)
            return self._sig(
                "ORB Breakout", symbol, Direction.PE, Tier.T1, price, atm_strike(price, symbol),
                sl_p=config.SL_PREMIUM_T1_3, sl_i=l + rng * 0.3,
                t1=config.T1_PREMIUM_GAIN, t2=config.T2_PREMIUM_GAIN,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"ORB breakdown below {l:.0f} with {vm:.1f}x vol",
                ok=ok, fail=fail, vix=vix
            )
        return None

    # ── S2: Straddle ─────────────────────────────────────────────

    def _s2_straddle(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if vix < config.VIX_IDEAL_LOW:
            return None
        if not time_between(config.PRIMARY_ENTRY_START, "09:50"):
            return None
        if not is_expiry_day() and vix < 20:
            return None
        return self._sig(
            "Straddle Event", symbol, Direction.BOTH, Tier.T4, price, atm_strike(price, symbol),
            sl_p=config.SL_PREMIUM_T4, sl_i=0, t1=0.50, t2=1.50,
            exit_by=config.HARD_EXIT_TIME, size=config.RISK_PER_TRADE_T4,
            reason=f"VIX {vix:.1f} elevated + expiry — straddle",
            ok=[f"VIX {vix:.1f}"], fail=[], vix=vix
        )

    # ── S3: Gap and Go ───────────────────────────────────────────

    def _s3_gap_go(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between(config.PRIMARY_ENTRY_START, "11:30"):
            return None
        prev_close = tick.prev_close
        if prev_close == 0:
            return None
        gap = (tick.open - prev_close) / prev_close
        ok, fail = [], []

        if not (config.PARAMS.GAP_MIN_PCT <= abs(gap) <= config.PARAMS.GAP_MAX_PCT):
            return None
        ok.append(f"Gap {gap*100:.2f}% in range")

        direction = Direction.CE if gap > 0 else Direction.PE
        midpt = prev_close + (tick.open - prev_close) * config.PARAMS.GAP_PULLBACK_HOLD
        holding = (direction == Direction.CE and price >= midpt) or \
                  (direction == Direction.PE and price <= midpt)
        if not holding:
            return None
        ok.append("Holding 50%+ of gap")

        vm = volume_mult(c5, 15)
        if vm >= config.PARAMS.GAP_VOL_MULT:
            ok.append(f"Vol {vm:.1f}x ok")
        else:
            fail.append(f"Vol {vm:.1f}x weak")
            return None

        return self._sig(
            "Gap and Go", symbol, direction, Tier.T2, price, atm_strike(price, symbol),
            sl_p=config.SL_PREMIUM_T1_3, sl_i=prev_close, t1=0.40, t2=0.70,
            exit_by="11:30",
            size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
            reason=f"Gap {gap*100:.2f}% holding, continuation expected",
            ok=ok, fail=fail, vix=vix
        )

    # ── S4: Trend Continuation ────────────────────────────────────

    def _s4_trend_cont(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between("09:45", "11:30"):
            return None
        if len(c15) < 5:
            return None
        closes15 = [c.close for c in c15[-6:]]
        e9 = ema(closes15, 9)
        near_ema = abs(price - e9) / e9 < 0.003
        td = trend_direction(closes15)
        ok, fail = [], []

        if td == "UP" and near_ema and c5[-1].is_bullish:
            ok.append(f"Uptrend + 9EMA {e9:.0f}")
            return self._sig(
                "Trend Continuation", symbol, Direction.CE, Tier.T3,
                price, otm_call(price, symbol, 1),
                sl_p=0.22, sl_i=min(c.low for c in c15[-3:]),
                t1=0.80, t2=1.50, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Pullback to 9EMA {e9:.0f} in uptrend", ok=ok, fail=fail, vix=vix
            )
        elif td == "DOWN" and near_ema and not c5[-1].is_bullish:
            ok.append(f"Downtrend + rejection {e9:.0f}")
            return self._sig(
                "Trend Continuation", symbol, Direction.PE, Tier.T3,
                price, otm_put(price, symbol, 1),
                sl_p=0.22, sl_i=max(c.high for c in c15[-3:]),
                t1=0.80, t2=1.50, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Rejection off 9EMA {e9:.0f} in downtrend", ok=ok, fail=fail, vix=vix
            )
        return None

    # ── S5: 15-Min Breakout ───────────────────────────────────────

    def _s5_15min_break(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between("09:30", "10:00"):
            return None
        if len(c15) < 2:
            return None
        first = c15[0]
        rng   = first.range
        max_r = (config.PARAMS.ORB_BN_MAX_RANGE if "BANK" in symbol
                 else config.PARAMS.ORB_NIFTY_MAX_RANGE)
        if rng > max_r:
            return None
        vm = volume_mult(c5, 10)
        if vm < 1.5:
            return None

        if price > first.high:
            return self._sig(
                "15-Min Breakout", symbol, Direction.CE, Tier.T2,
                price, atm_strike(price, symbol),
                sl_p=0.20, sl_i=first.low, t1=0.40, t2=0.80,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Break above 15m high {first.high:.0f} vol {vm:.1f}x",
                ok=[f"Range {rng:.0f}pts", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        elif price < first.low:
            return self._sig(
                "15-Min Breakout", symbol, Direction.PE, Tier.T2,
                price, atm_strike(price, symbol),
                sl_p=0.20, sl_i=first.high, t1=0.40, t2=0.80,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Break below 15m low {first.low:.0f} vol {vm:.1f}x",
                ok=[f"Range {rng:.0f}pts", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        return None

    # ── S6: S/R Reversal ─────────────────────────────────────────

    def _s6_sr_reversal(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between(config.PRIMARY_ENTRY_START, config.PRIMARY_ENTRY_END):
            return None
        sym_key = "BANKNIFTY" if "BANK" in symbol else "NIFTY"
        levels  = config.KEY_LEVELS.get(sym_key, {})
        tol     = (config.PARAMS.SR_BN_TOLERANCE if "BANK" in symbol
                   else config.PARAMS.SR_NIFTY_TOLERANCE)
        if len(c5) < 2:
            return None
        cur, prev = c5[-1], c5[-2]

        for level in levels.get("resistance", []):
            if near_level(price, level, tol):
                if not cur.is_bullish and prev.is_bullish:
                    return self._sig(
                        "S/R Reversal", symbol, Direction.PE, Tier.T2,
                        price, atm_strike(price, symbol),
                        sl_p=config.SL_PREMIUM_T4, sl_i=level + tol,
                        t1=0.50, t2=1.00, exit_by=config.HARD_EXIT_TIME,
                        size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                        reason=f"Bearish rejection at resistance {level:.0f}",
                        ok=[f"Near resistance {level:.0f}", "Bearish engulfing"],
                        fail=[], vix=vix
                    )
        for level in levels.get("support", []):
            if near_level(price, level, tol):
                if cur.is_bullish and not prev.is_bullish:
                    return self._sig(
                        "S/R Reversal", symbol, Direction.CE, Tier.T2,
                        price, atm_strike(price, symbol),
                        sl_p=config.SL_PREMIUM_T4, sl_i=level - tol,
                        t1=0.50, t2=1.00, exit_by=config.HARD_EXIT_TIME,
                        size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                        reason=f"Bullish bounce at support {level:.0f}",
                        ok=[f"Near support {level:.0f}", "Bullish reversal"], fail=[], vix=vix
                    )
        return None

    # ── S7: VWAP Pullback ─────────────────────────────────────────

    def _s7_vwap(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between("09:45", config.PRIMARY_ENTRY_END):
            return None
        if len(c5) < 5 or len(c15) < 3:
            return None
        v   = vwap_from_candles(c5)
        td  = trend_direction([c.close for c in c15[-5:]])
        vm  = volume_mult(c5, 15)
        cur = c5[-1]
        near_vwap = abs(price - v) / v < 0.002

        if td == "UP" and near_vwap and cur.is_bullish and vm >= config.PARAMS.VWAP_VOL_MULT:
            return self._sig(
                "VWAP Pullback", symbol, Direction.CE, Tier.T3,
                price, otm_call(price, symbol, 1),
                sl_p=config.SL_PREMIUM_T1_3,
                sl_i=v * (1 - config.PARAMS.VWAP_SL_BREACH_PCT),
                t1=0.60, t2=1.20, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Bounce off VWAP {v:.0f} in uptrend vol {vm:.1f}x",
                ok=["Uptrend", f"VWAP bounce {v:.0f}", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        elif td == "DOWN" and near_vwap and not cur.is_bullish and vm >= config.PARAMS.VWAP_VOL_MULT:
            return self._sig(
                "VWAP Pullback", symbol, Direction.PE, Tier.T3,
                price, otm_put(price, symbol, 1),
                sl_p=config.SL_PREMIUM_T1_3,
                sl_i=v * (1 + config.PARAMS.VWAP_SL_BREACH_PCT),
                t1=0.60, t2=1.20, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Rejection at VWAP {v:.0f} in downtrend vol {vm:.1f}x",
                ok=["Downtrend", f"VWAP rejection {v:.0f}", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        return None

    # ── S8: 5-Min Engulfing (BN only) ────────────────────────────

    def _s8_engulfing(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if "BANK" not in symbol:
            return None
        if not time_between("09:30", config.PRIMARY_ENTRY_END):
            return None
        if len(c5) < 3 or len(c15) < 3:
            return None
        cur, prev = c5[-1], c5[-2]
        td   = trend_direction([c.close for c in c15[-4:]])
        vm   = volume_mult(c5, 15)
        bull_eng = (cur.open <= prev.close and cur.close >= prev.open
                    and cur.is_bullish and not prev.is_bullish)
        bear_eng = (cur.open >= prev.close and cur.close <= prev.open
                    and not cur.is_bullish and prev.is_bullish)

        if bull_eng and td == "UP" and vm >= config.PARAMS.SCALP_VOL_MULT:
            return self._sig(
                "5-Min Engulfing", symbol, Direction.CE, Tier.T1,
                price, atm_strike(price, symbol),
                sl_p=config.PARAMS.SCALP_SL_PREMIUM, sl_i=cur.low,
                t1=0.35, t2=0.60, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * 0.6 * vix_size_multiplier(vix),
                reason=f"Bullish engulfing + 15m uptrend vol {vm:.1f}x",
                ok=["Bull engulf", "Uptrend", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        elif bear_eng and td == "DOWN" and vm >= config.PARAMS.SCALP_VOL_MULT:
            return self._sig(
                "5-Min Engulfing", symbol, Direction.PE, Tier.T1,
                price, atm_strike(price, symbol),
                sl_p=config.PARAMS.SCALP_SL_PREMIUM, sl_i=cur.high,
                t1=0.35, t2=0.60, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * 0.6 * vix_size_multiplier(vix),
                reason=f"Bearish engulfing + 15m downtrend vol {vm:.1f}x",
                ok=["Bear engulf", "Downtrend", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        return None

    # ── S9: Gamma Scalp (expiry day) ─────────────────────────────

    def _s9_gamma(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not is_expiry_day():
            return None
        if not time_between(config.MARKET_OPEN, "09:35"):
            return None
        if vix < config.VIX_IDEAL_LOW:
            return None
        ls  = lot_size(symbol)
        atm = atm_strike(price, symbol)
        return self._sig(
            "Gamma Scalp", symbol, Direction.BOTH, Tier.T5,
            price, atm + ls,
            sl_p=config.SL_PREMIUM_T5, sl_i=0, t1=2.00, t2=5.00,
            exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
            reason=f"Expiry day gamma. VIX {vix:.1f}. Exit by 11AM.",
            ok=["Expiry day", f"VIX {vix:.1f}"], fail=[], vix=vix
        )

    # ── S10: EMA 9/21 Crossover ───────────────────────────────────

    def _s10_ema(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        if not time_between("09:45", config.PRIMARY_ENTRY_END):
            return None
        candles = c5 if "BANK" in symbol else c15
        if len(candles) < 25:
            return None
        closes   = [c.close for c in candles]
        e_fast   = ema(closes[-22:], config.PARAMS.EMA_FAST)
        e_fast_p = ema(closes[-23:], config.PARAMS.EMA_FAST)
        e_slow   = ema(closes[-22:], config.PARAMS.EMA_SLOW)
        e_slow_p = ema(closes[-23:], config.PARAMS.EMA_SLOW)
        rsi_val  = rsi(closes[-20:])
        vm       = volume_mult(candles, 20)

        cross_up   = e_fast_p < e_slow_p and e_fast > e_slow
        cross_down = e_fast_p > e_slow_p and e_fast < e_slow

        if cross_up and rsi_val >= config.PARAMS.EMA_CE_RSI_MIN and vm >= config.PARAMS.EMA_VOL_MULT:
            return self._sig(
                "EMA Crossover", symbol, Direction.CE, Tier.T3,
                price, atm_strike(price, symbol),
                sl_p=0.20, sl_i=e_slow, t1=0.60, t2=1.20,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"EMA {config.PARAMS.EMA_FAST}/{config.PARAMS.EMA_SLOW} bullish cross RSI={rsi_val:.0f}",
                ok=["EMA cross up", f"RSI {rsi_val:.0f}", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        elif cross_down and rsi_val <= config.PARAMS.EMA_PE_RSI_MAX and vm >= config.PARAMS.EMA_VOL_MULT:
            return self._sig(
                "EMA Crossover", symbol, Direction.PE, Tier.T3,
                price, atm_strike(price, symbol),
                sl_p=0.20, sl_i=e_slow, t1=0.60, t2=1.20,
                exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"EMA {config.PARAMS.EMA_FAST}/{config.PARAMS.EMA_SLOW} bearish cross RSI={rsi_val:.0f}",
                ok=["EMA cross down", f"RSI {rsi_val:.0f}", f"Vol {vm:.1f}x"], fail=[], vix=vix
            )
        return None

    # ── Helpers ───────────────────────────────────────────────────

    def _sig(self, strategy, symbol, direction, tier, price, strike,
             sl_p, sl_i, t1, t2, exit_by, size, reason, ok, fail, vix) -> Signal:
        _, expiry_str = next_expiry(symbol)
        size = min(size, config.RISK_PER_TRADE_T1_3)
        return Signal(
            strategy=strategy, symbol=symbol, direction=direction,
            tier=tier, strength=self._strength(vix, ok, fail),
            entry_trigger=price, strike=int(strike), expiry=expiry_str,
            sl_premium_pct=sl_p, sl_index=sl_i,
            t1_pct=t1, t2_pct=t2, exit_by=exit_by, size_pct=size,
            reason=reason, filters_ok=ok, filters_fail=fail,
        )

    def _strength(self, vix: float, ok: List[str], fail: List[str]) -> Strength:
        score  = len(ok) * 2 - len(fail) * 3
        score += 2 if config.VIX_IDEAL_LOW <= vix <= config.VIX_IDEAL_HIGH else 0
        score += 1 if vix >= 18 else 0
        if score >= 8: return Strength.GODMODE
        if score >= 5: return Strength.STRONG
        if score >= 2: return Strength.MODERATE
        return Strength.WEAK

    def _resolve(self, tick: Tick) -> Optional[str]:
        from utils import normalize_symbol
        return normalize_symbol(tick.symbol)

    def _cooldown_ok(self, symbol: str, strategy: str) -> bool:
        key  = f"{symbol}_{strategy}"
        last = self._last.get(key)
        if not last:
            return True
        return (datetime.now() - last).total_seconds() > self.SIGNAL_COOLDOWN

    def _update_cooldown(self, symbol: str, strategy: str):
        self._last[f"{symbol}_{strategy}"] = datetime.now()

    def _build_orb(self, symbol: str, c5: List[Candle]):
        if not ORB.is_set(symbol) and c5:
            c  = c5[-1]
            av = avg_volume(c5, 20) if len(c5) >= 5 else c.volume
            ORB.set(symbol, c.high, c.low, av)

    def reset_day(self):
        ORB.reset()
        self._last.clear()
        log.info("Strategy engine daily reset.")
