# ================================================================
#  strategy_engine.py — All 10 Strategies + Real-Time Signals
#  Runs on every tick. Returns actionable Signal objects.
# ================================================================

import threading
from collections import deque
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
        # Trend day detection: rolling window of (timestamp, direction_str) tuples
        self._trend_signals: deque = deque()
        self._is_trend_day:  bool  = False

    def evaluate(self, tick: Tick) -> List[Signal]:
        signals = []
        vix = self.feed.get_vix()

        if vix > 0 and vix < config.VIX_AVOID:
            return []
        if is_hard_exit_time():
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
            self._s11_expiry_afternoon, self._s12_expiry_close_squeeze,
            self._s13_expiry_pre11_straddle, self._s14_max_pain_fade,
            self._s15_settlement_squeeze,
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

        # Dead zone soft filter: T3+ blocked, T1/T2 pass through
        if is_dead_zone():
            signals = [s for s in signals if s.tier.value <= 2]

        # Trend day tracker: rolling 60-min window of signal directions
        if signals:
            now_ts = datetime.now()
            cutoff_ts = now_ts.timestamp() - getattr(config, 'TREND_DAY_WINDOW_MINS', 60) * 60
            for sig in signals:
                self._trend_signals.append((now_ts, sig.direction.value))
            while self._trend_signals and self._trend_signals[0][0].timestamp() < cutoff_ts:
                self._trend_signals.popleft()
            recent = [d for _, d in self._trend_signals]
            ce_count = sum(1 for d in recent if "CE" in d)
            pe_count = sum(1 for d in recent if "PE" in d)
            threshold = getattr(config, 'TREND_DAY_SIGNAL_COUNT', 3)
            was_trend = self._is_trend_day
            self._is_trend_day = max(ce_count, pe_count) >= threshold
            if self._is_trend_day and not was_trend:
                log.info(f"🚀 TREND DAY activated: CE={ce_count} PE={pe_count} in last {getattr(config,'TREND_DAY_WINDOW_MINS',60)}min")

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
        if not time_between("09:45", config.PRIMARY_ENTRY_END):
            return None
        if len(c15) < 5:
            return None
        closes15 = [c.close for c in c15[-6:]]
        e9 = ema(closes15, 9)
        near_ema = abs(price - e9) / e9 < 0.003
        td = trend_direction(closes15)
        ok, fail = [], []

        # ── P2.5: Heavyweight stock confirmation ──────────────────
        # Fetch the 15s-cached scan — zero extra API cost per tick.
        hw = {}
        try:
            from market_scanner import heavyweight_scan
            hw = heavyweight_scan(self.feed)
        except Exception:
            pass  # scan unavailable — proceed on technicals only

        is_bn = "BANK" in symbol.upper()
        hw_signal  = hw.get("bn_signal",      "UNAVAILABLE") if is_bn else hw.get("n50_signal", "UNAVAILABLE")
        hdfc_alert = hw.get("hdfcbank_alert", False)
        conviction = hw.get("conviction",     "UNAVAILABLE")

        if td == "UP" and near_ema and c5[-1].is_bullish:
            ok.append(f"Uptrend + 9EMA {e9:.0f}")
            # Tier upgrade: T3 → T2 when heavyweights also BULLISH (high-conviction)
            tier   = Tier.T2 if hw_signal == "BULLISH" else Tier.T3
            if hw_signal == "BULLISH":
                ok.append(f"HW BULLISH ({conviction}): stocks confirm uptrend")
            elif hw_signal == "BEARISH":
                fail.append("HW BEARISH: stocks contradict CE signal")
            return self._sig(
                "Trend Continuation", symbol, Direction.CE, tier,
                price, otm_call(price, symbol, 1),
                sl_p=0.22, sl_i=min(c.low for c in c15[-3:]),
                t1=0.80, t2=1.50, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Pullback to 9EMA {e9:.0f} in uptrend; HW={hw_signal}",
                ok=ok, fail=fail, vix=vix
            )
        elif td == "DOWN" and near_ema and not c5[-1].is_bullish:
            ok.append(f"Downtrend + rejection {e9:.0f}")
            # Tier upgrade: T3 → T2 when heavyweights BEARISH
            # Extra uplift: T2 → T1 if HDFCBANK early drop alert on BANKNIFTY PE
            if hw_signal == "BEARISH":
                ok.append(f"HW BEARISH ({conviction}): stocks confirm downtrend")
                if is_bn and hdfc_alert:
                    tier = Tier.T1
                    ok.append(f"HDFCBANK drop alert: BN PE elevated to T1")
                else:
                    tier = Tier.T2
            elif hw_signal == "BULLISH":
                fail.append("HW BULLISH: stocks contradict PE signal")
                tier = Tier.T3
            else:
                tier = Tier.T3
            return self._sig(
                "Trend Continuation", symbol, Direction.PE, tier,
                price, otm_put(price, symbol, 1),
                sl_p=0.22, sl_i=max(c.high for c in c15[-3:]),
                t1=0.80, t2=1.50, exit_by=config.HARD_EXIT_TIME,
                size=config.RISK_PER_TRADE_T1_3 * vix_size_multiplier(vix),
                reason=f"Rejection off 9EMA {e9:.0f} in downtrend; HW={hw_signal}",
                ok=ok, fail=fail, vix=vix
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
        if v <= 0:
            return None   # no candle volume data yet (REST-only mode)
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
        td   = trend_direction([c.close for c in c15[-4:]], lookback=4)
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
        from utils import is_nifty_expiry, is_banknifty_expiry
        # Nifty: weekly gamma every Tuesday
        # BankNifty: monthly gamma ONLY on last Tuesday of month — is_banknifty_expiry() handles this
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        # Time gate: if EXPIRY_GAMMA_ALL_DAY is True, gamma fires all day (not just 9:15-9:35).
        # This captures the full expiry gamma effect — not just the open.
        all_day = getattr(config, 'EXPIRY_GAMMA_ALL_DAY', False)
        if not all_day and not time_between(config.MARKET_OPEN, "09:35"):
            return None
        # Even in all-day mode, respect primary entry window (no gamma past 3 PM exit)
        if all_day and not time_between(config.MARKET_OPEN, config.PRIMARY_ENTRY_END):
            return None
        if vix < config.VIX_IDEAL_LOW:
            return None
        atm = atm_strike(price, symbol)
        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        return self._sig(
            "Gamma Scalp", symbol, Direction.BOTH, Tier.T5,
            price, atm,   # ATM straddle — was wrongly atm+lot_size (invalid strike)
            sl_p=config.SL_PREMIUM_T5, sl_i=0, t1=2.00, t2=5.00,
            exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
            reason=f"{expiry_label} expiry gamma play. VIX {vix:.1f}. Exit by {config.EXPIRY_EXIT_TIME}.",
            ok=[f"{expiry_label} expiry day", f"VIX {vix:.1f}"], fail=[], vix=vix
        )

    # ── S10: EMA 9/21 Crossover ───────────────────────────────────
    # NOTE: Uses 5m candles for BOTH symbols (was 15m for Nifty — never fired).
    # Window extended to 13:30 so enough candles accumulate (needs 23 min).
    # 5m candles from 9:15: need 23 candles = 9:15 + 115min = 11:10 AM minimum.
    # So this fires 11:10 AM – 13:30 PM (primary late + secondary window).

    def _s10_ema(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        # Runs from 9:45 through the full session (primary + secondary window).
        # Dead zone (13:00–13:15) is handled separately — T3 gets filtered in evaluate().
        if not time_between("09:45", config.HARD_EXIT_TIME):
            return None
        if is_dead_zone():
            return None
        candles = c5   # always 5m — 15m never accumulated enough bars intraday
        if len(candles) < 23:                                  # was 25, min needed = 23
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

    # ── S11: Expiry Afternoon Gamma (13:15–15:00) ─────────────────
    # Post-dead-zone gamma acceleration. The 1:30–3:00 PM window is the
    # single highest-velocity option move of the session on expiry day.
    # ATM straddle when direction unclear; directional CE/PE on momentum.

    def _s11_expiry_afternoon(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        from utils import is_nifty_expiry, is_banknifty_expiry
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        if not time_between(config.SECONDARY_ENTRY_START, "15:00"):
            return None
        if vix < config.VIX_IDEAL_LOW:
            return None
        if len(c5) < 4:
            return None

        atm          = atm_strike(price, symbol)
        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        td           = trend_direction([c.close for c in c5[-4:]], lookback=4)

        if td == "UP":
            return self._sig(
                "Expiry Afternoon Gamma", symbol, Direction.CE, Tier.T5,
                price, atm,
                sl_p=0.40, sl_i=0, t1=1.50, t2=3.50,
                exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
                reason=f"{expiry_label} expiry afternoon CE. VIX {vix:.1f}. Post-dead-zone momentum UP.",
                ok=[f"{expiry_label} expiry", "Afternoon momentum UP", f"VIX {vix:.1f}"],
                fail=[], vix=vix
            )
        elif td == "DOWN":
            return self._sig(
                "Expiry Afternoon Gamma", symbol, Direction.PE, Tier.T5,
                price, atm,
                sl_p=0.40, sl_i=0, t1=1.50, t2=3.50,
                exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
                reason=f"{expiry_label} expiry afternoon PE. VIX {vix:.1f}. Post-dead-zone momentum DOWN.",
                ok=[f"{expiry_label} expiry", "Afternoon momentum DOWN", f"VIX {vix:.1f}"],
                fail=[], vix=vix
            )
        else:
            # No clear direction — straddle captures the gamma move either way
            return self._sig(
                "Expiry Afternoon Gamma", symbol, Direction.BOTH, Tier.T5,
                price, atm,
                sl_p=config.SL_PREMIUM_T5, sl_i=0, t1=1.50, t2=3.50,
                exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
                reason=f"{expiry_label} expiry afternoon straddle. VIX {vix:.1f}. No clear direction.",
                ok=[f"{expiry_label} expiry", "Afternoon gamma window", f"VIX {vix:.1f}"],
                fail=[], vix=vix
            )

    # ── S12: Expiry Close Squeeze (14:15–15:00) ──────────────────
    # Final hour of expiry. Options are near-zero premium with max gamma.
    # A 50pt Nifty move at 2:30 PM is worth 5x on ATM. Only fire on
    # confirmed breakout of last 15m range with volume confirmation.

    def _s12_expiry_close_squeeze(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        from utils import is_nifty_expiry, is_banknifty_expiry
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        if not time_between("14:15", config.EXPIRY_EXIT_TIME):
            return None
        if vix < config.VIX_IDEAL_LOW:
            return None
        if len(c15) < 2 or len(c5) < 3:
            return None

        atm          = atm_strike(price, symbol)
        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        last15       = c15[-2]   # last completed 15m candle = clean breakout reference
        vm           = volume_mult(c5, 15)

        if price > last15.high and vm >= 1.3:
            return self._sig(
                "Expiry Close Squeeze", symbol, Direction.CE, Tier.T5,
                price, atm,
                sl_p=0.50, sl_i=last15.high * 0.998, t1=2.00, t2=5.00,
                exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
                reason=f"{expiry_label} expiry squeeze CE. Break >{last15.high:.0f} vol {vm:.1f}x.",
                ok=[f"{expiry_label} expiry close", f"Break {last15.high:.0f}", f"Vol {vm:.1f}x"],
                fail=[], vix=vix
            )
        elif price < last15.low and vm >= 1.3:
            return self._sig(
                "Expiry Close Squeeze", symbol, Direction.PE, Tier.T5,
                price, atm,
                sl_p=0.50, sl_i=last15.low * 1.002, t1=2.00, t2=5.00,
                exit_by=config.EXPIRY_EXIT_TIME, size=config.RISK_PER_TRADE_T5,
                reason=f"{expiry_label} expiry squeeze PE. Break <{last15.low:.0f} vol {vm:.1f}x.",
                ok=[f"{expiry_label} expiry close", f"Break {last15.low:.0f}", f"Vol {vm:.1f}x"],
                fail=[], vix=vix
            )
        return None

    # ── S13: Expiry Pre-11 Straddle (09:30–10:45) ────────────────
    # Expiry mornings are often indecisive 9:30–10:30. Gamma is HIGH.
    # ATM straddle captures breakout in either direction before the
    # theta wall hits at 10:45 AM. Only fires when PCR is neutral
    # (no strong directional consensus) and VIX gives enough premium.

    def _s13_expiry_pre11_straddle(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        from utils import is_nifty_expiry, is_banknifty_expiry
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        if not time_between("09:30", "10:45"):
            return None
        if vix < getattr(config, 'S13_STRADDLE_VIX_MIN', 15.0):
            return None
        if len(c5) < 3:
            return None

        # PCR check — neutral zone only (directional PCR → use S9 instead)
        pcr_min = getattr(config, 'S13_STRADDLE_PCR_MIN', 0.75)
        pcr_max = getattr(config, 'S13_STRADDLE_PCR_MAX', 1.25)
        pcr_val = 0.0
        try:
            pcr_val = float(self.feed.get_pcr(symbol))
        except Exception:
            pcr_val = 0.0   # PCR unavailable — fire on VIX alone
        if pcr_val != 0.0 and not (pcr_min <= pcr_val <= pcr_max):
            return None   # directional PCR → skip straddle, let directional strategies fire

        atm          = atm_strike(price, symbol)
        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        ok           = [f"{expiry_label} expiry", f"VIX {vix:.1f}"]
        if pcr_val != 0.0:
            ok.append(f"PCR {pcr_val:.2f} neutral")

        size = getattr(config, 'RISK_PER_TRADE_T4', 0.25) * 0.7  # 70% of straddle size

        return self._sig(
            "Expiry Pre-11 Straddle", symbol, Direction.BOTH, Tier.T4,
            price, atm,
            sl_p=0.30, sl_i=0, t1=0.60, t2=1.50,
            exit_by="10:45",
            size=size,
            reason=f"{expiry_label} expiry pre-11 straddle. VIX {vix:.1f}. Gamma+indecision window.",
            ok=ok, fail=[], vix=vix
        )

    # ── S14: Max Pain Fade (14:00–14:50) ─────────────────────────
    # After 2 PM on expiry day, NSE market makers pin the index toward
    # max pain (the strike with maximum open interest). Settlement is
    # VWAP of last 30 min (3:00–3:30). If price is far from max pain,
    # directional trade toward it is statistically supported.

    def _s14_max_pain_fade(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        from utils import is_nifty_expiry, is_banknifty_expiry
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        if not time_between("14:00", "14:50"):
            return None
        if len(c5) < 2:
            return None

        sym_key   = "BANKNIFTY" if "BANK" in symbol else "NIFTY"
        max_pain  = config.KEY_LEVELS.get(sym_key, {}).get("max_pain", 0.0)
        if not max_pain or max_pain <= 0:
            return None   # max_pain not set in config — skip

        is_bn     = "BANK" in symbol
        threshold = getattr(config, 'MAX_PAIN_THRESHOLD_BN',    100) if is_bn \
               else getattr(config, 'MAX_PAIN_THRESHOLD_NIFTY',  40)

        distance  = price - max_pain
        if abs(distance) <= threshold:
            return None   # too close — no clear fade edge

        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        atm          = atm_strike(price, symbol)
        size         = getattr(config, 'RISK_PER_TRADE_T5', 0.20) * 0.8

        if distance > threshold:
            # price significantly ABOVE max pain → fade with PE (expect pullback to pain)
            return self._sig(
                "Max Pain Fade", symbol, Direction.PE, Tier.T5,
                price, atm,
                sl_p=0.45, sl_i=0, t1=1.00, t2=2.50,
                exit_by="14:50",
                size=size,
                reason=(f"{expiry_label} price {price:.0f} is {distance:.0f}pts above "
                        f"max pain {max_pain:.0f}. Fade PE to pin."),
                ok=[f"{expiry_label} expiry", f"Price {distance:.0f}pts above max pain {max_pain:.0f}"],
                fail=[], vix=vix
            )
        else:
            # price significantly BELOW max pain → CE (expect rally to pain)
            return self._sig(
                "Max Pain Fade", symbol, Direction.CE, Tier.T5,
                price, atm,
                sl_p=0.45, sl_i=0, t1=1.00, t2=2.50,
                exit_by="14:50",
                size=size,
                reason=(f"{expiry_label} price {price:.0f} is {abs(distance):.0f}pts below "
                        f"max pain {max_pain:.0f}. Fade CE to pin."),
                ok=[f"{expiry_label} expiry", f"Price {abs(distance):.0f}pts below max pain {max_pain:.0f}"],
                fail=[], vix=vix
            )

    # ── S15: Settlement Squeeze (14:35–15:20) ────────────────────
    # Final 45 min of expiry. NSE settlement = VWAP of 3:00–3:30.
    # Large position adjustments create last-minute directional momentum.
    # Only fire on confirmed 2-candle momentum + elevated volume + extreme PCR.
    # ATM ONLY — OTM is near-zero premium at this point.

    def _s15_settlement_squeeze(self, tick, symbol, price, c5, c15, c1, vix) -> Optional[Signal]:
        from utils import is_nifty_expiry, is_banknifty_expiry
        if is_nifty_expiry() and symbol != "NIFTY":
            return None
        if is_banknifty_expiry() and symbol != "BANKNIFTY":
            return None
        if not (is_nifty_expiry() or is_banknifty_expiry()):
            return None
        if not time_between("14:35", "15:20"):
            return None
        if vix > 35:
            return None   # extreme VIX = slippage too high in last 45 min
        if len(c5) < 3:
            return None

        vm = volume_mult(c5, 10)
        vol_min = getattr(config, 'S15_SETTLEMENT_VOL_MULT', 1.5)
        if vm < vol_min:
            return None

        # Require 2 consecutive same-direction 5m candles (momentum confirmation)
        last2 = c5[-3:-1]  # last 2 completed candles (not current forming)
        if len(last2) < 2:
            return None
        both_bull = all(c.is_bullish for c in last2)
        both_bear = all(not c.is_bullish for c in last2)
        if not (both_bull or both_bear):
            return None

        # PCR extreme check — adds conviction; fire without it if unavailable
        pcr_val = 0.0
        try:
            pcr_val = float(self.feed.get_pcr(symbol))
        except Exception:
            pcr_val = 0.0

        pcr_bull = getattr(config, 'S15_SETTLEMENT_PCR_BULL', 0.65)
        pcr_bear = getattr(config, 'S15_SETTLEMENT_PCR_BEAR', 1.35)

        expiry_label = "Nifty" if symbol == "NIFTY" else "BankNifty"
        atm          = atm_strike(price, symbol)
        size         = getattr(config, 'RISK_PER_TRADE_T5', 0.20) * 0.5   # 50% of gamma size

        if both_bull:
            pcr_ok = (pcr_val == 0.0 or pcr_val < pcr_bull)
            if not pcr_ok:
                return None
            ok = [f"{expiry_label} expiry", f"2-candle bull momentum", f"Vol {vm:.1f}x"]
            if pcr_val != 0.0:
                ok.append(f"PCR {pcr_val:.2f} bullish")
            return self._sig(
                "Settlement Squeeze", symbol, Direction.CE, Tier.T5,
                price, atm,
                sl_p=0.50, sl_i=0, t1=1.50, t2=4.00,
                exit_by="15:20",
                size=size,
                reason=f"{expiry_label} settlement squeeze CE. 2-candle UP + vol {vm:.1f}x.",
                ok=ok, fail=[], vix=vix
            )
        else:  # both_bear
            pcr_ok = (pcr_val == 0.0 or pcr_val > pcr_bear)
            if not pcr_ok:
                return None
            ok = [f"{expiry_label} expiry", f"2-candle bear momentum", f"Vol {vm:.1f}x"]
            if pcr_val != 0.0:
                ok.append(f"PCR {pcr_val:.2f} bearish")
            return self._sig(
                "Settlement Squeeze", symbol, Direction.PE, Tier.T5,
                price, atm,
                sl_p=0.50, sl_i=0, t1=1.50, t2=4.00,
                exit_by="15:20",
                size=size,
                reason=f"{expiry_label} settlement squeeze PE. 2-candle DOWN + vol {vm:.1f}x.",
                ok=ok, fail=[], vix=vix
            )

    # ── Helpers ───────────────────────────────────────────────────

    def _sig(self, strategy, symbol, direction, tier, price, strike,
             sl_p, sl_i, t1, t2, exit_by, size, reason, ok, fail, vix) -> Signal:
        _, expiry_str = next_expiry(symbol)
        # NO upper cap on size — capital is deployed in full via lots_to_buy().
        # Trend day: extend T2 target by TREND_DAY_T2_MULTIPLIER (default 1.5×)
        if self._is_trend_day:
            t2 = t2 * getattr(config, 'TREND_DAY_T2_MULTIPLIER', 1.5)
        return Signal(
            strategy=strategy, symbol=symbol, direction=direction,
            tier=tier, strength=self._strength(vix, ok, fail, direction),
            entry_trigger=price, strike=int(strike), expiry=expiry_str,
            sl_premium_pct=sl_p, sl_index=sl_i,
            t1_pct=t1, t2_pct=t2, exit_by=exit_by, size_pct=size,
            reason=reason, filters_ok=ok, filters_fail=fail,
        )

    def _strength(self, vix: float, ok: List[str], fail: List[str],
                  direction: Optional[Direction] = None) -> Strength:
        score  = len(ok) * 2 - len(fail) * 3
        score += 2 if config.VIX_IDEAL_LOW <= vix <= config.VIX_IDEAL_HIGH else 0
        score += 1 if vix >= 18 else 0
        # Session bias multiplier: aligned signals score higher, counter signals score lower
        bias = getattr(config, 'SESSION_BIAS', 'NEUTRAL')
        if direction and bias != 'NEUTRAL':
            dir_val = direction.value if hasattr(direction, 'value') else str(direction)
            aligned = (
                (bias == 'BULLISH' and 'CE' in dir_val) or
                (bias == 'BEARISH' and 'PE' in dir_val) or
                (dir_val == 'CE+PE')  # straddles are always neutral vs bias
            )
            counter = (
                (bias == 'BULLISH' and dir_val == 'PE') or
                (bias == 'BEARISH' and dir_val == 'CE')
            )
            bull_mult = getattr(config, 'SESSION_BIAS_MULTIPLIER',    1.4)
            bear_mult = getattr(config, 'SESSION_COUNTER_MULTIPLIER', 0.7)
            if aligned:
                score = int(score * bull_mult)
            elif counter:
                score = int(score * bear_mult)
        if score >= 8: return Strength.GODMODE
        if score >= 5: return Strength.STRONG
        if score >= 2: return Strength.MODERATE
        return Strength.WEAK

    def _resolve(self, tick: Tick) -> Optional[str]:
        from utils import normalize_symbol
        return normalize_symbol(tick.symbol)

    _EXPIRY_AFTERNOON_STRATEGIES = {
        "Expiry Afternoon Gamma", "Expiry Close Squeeze",
        "Expiry Pre-11 Straddle", "Max Pain Fade", "Settlement Squeeze",
    }

    def _cooldown_ok(self, symbol: str, strategy: str) -> bool:
        key  = f"{symbol}_{strategy}"
        last = self._last.get(key)
        if not last:
            return True
        # Expiry afternoon strategies fire on 120s cooldown (shorter — fast intraday moves)
        if strategy in self._EXPIRY_AFTERNOON_STRATEGIES:
            from utils import is_expiry_day
            if is_expiry_day():
                cooldown = getattr(config, 'EXPIRY_COOLDOWN_SECS', 120)
            else:
                cooldown = self.SIGNAL_COOLDOWN
        # Trend day: reduce cooldown to allow more frequent signals
        elif self._is_trend_day:
            cooldown = getattr(config, 'TREND_DAY_COOLDOWN_SECS', 150)
        else:
            cooldown = self.SIGNAL_COOLDOWN
        return (datetime.now() - last).total_seconds() > cooldown

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
        self._trend_signals.clear()
        self._is_trend_day = False
        log.info("Strategy engine daily reset.")
