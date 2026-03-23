# ================================================================
#  utils.py — Shared Utilities
# ================================================================

import statistics
from datetime import datetime, time as dtime, timedelta
from typing import List, Optional
import config

# ── Time Helpers ──────────────────────────────────────────────────

def now_time() -> dtime:
    return datetime.now().time()

def parse_time(s: str) -> dtime:
    h, m = map(int, s.split(":"))
    return dtime(h, m)

def time_between(start_str: str, end_str: str) -> bool:
    t = now_time()
    return parse_time(start_str) <= t <= parse_time(end_str)

def after_time(time_str: str) -> bool:
    return now_time() >= parse_time(time_str)

def before_time(time_str: str) -> bool:
    return now_time() < parse_time(time_str)

def is_market_open() -> bool:
    return time_between(config.MARKET_OPEN, config.MARKET_CLOSE)

def is_primary_window() -> bool:
    return time_between(config.PRIMARY_ENTRY_START, config.PRIMARY_ENTRY_END)

def is_dead_zone() -> bool:
    return time_between(config.DEAD_ZONE_START, config.DEAD_ZONE_END)

def is_hard_exit_time() -> bool:
    return after_time(config.HARD_EXIT_TIME)

def is_expiry_day() -> bool:
    wd = datetime.now().weekday()
    return wd in (config.NIFTY_EXPIRY_WEEKDAY, config.BANKNIFTY_EXPIRY_WEEKDAY)

def is_nifty_expiry() -> bool:
    return datetime.now().weekday() == config.NIFTY_EXPIRY_WEEKDAY

def is_banknifty_expiry() -> bool:
    return datetime.now().weekday() == config.BANKNIFTY_EXPIRY_WEEKDAY

def next_expiry(symbol: str):
    today = datetime.now()
    wd = config.NIFTY_EXPIRY_WEEKDAY if symbol == "NIFTY" else config.BANKNIFTY_EXPIRY_WEEKDAY
    days = (wd - today.weekday()) % 7
    if days == 0 and today.hour >= 15:
        days = 7
    exp = today + timedelta(days=days)
    return exp.strftime("%Y-%m-%d"), exp.strftime("%d %b")

def day_of_week_name() -> str:
    return datetime.now().strftime("%A")

# ── Instrument Helpers ────────────────────────────────────────────

def lot_size(symbol: str) -> int:
    return config.LOT_SIZES.get(symbol.upper().replace(" ", "").replace("50", ""), 75)

def atm_strike(price: float, symbol: str) -> int:
    ls = lot_size(symbol)
    step = 100 if "BANK" in symbol.upper() else 50
    return round(price / step) * step

def otm_call(price: float, symbol: str, strikes: int = 1) -> int:
    step = 100 if "BANK" in symbol.upper() else 50
    return atm_strike(price, symbol) + (strikes * step)

def otm_put(price: float, symbol: str, strikes: int = 1) -> int:
    step = 100 if "BANK" in symbol.upper() else 50
    return atm_strike(price, symbol) - (strikes * step)

def normalize_symbol(raw: str) -> Optional[str]:
    s = raw.upper().replace(" ", "").replace("_", "")
    if any(x in s for x in ["BANKNIFTY", "NIFTYBANK", "BANK"]):
        return "BANKNIFTY"
    if any(x in s for x in ["NIFTY50", "NIFTY"]):
        return "NIFTY"
    return None

def capital_at_risk(symbol: str, tier: int) -> float:
    if tier in (1, 2, 3):
        return config.TOTAL_CAPITAL * config.RISK_PER_TRADE_T1_3
    elif tier == 4:
        return config.TOTAL_CAPITAL * config.RISK_PER_TRADE_T4
    elif tier == 5:
        return config.TOTAL_CAPITAL * config.RISK_PER_TRADE_T5
    return config.TOTAL_CAPITAL * 0.03

def lots_to_buy(capital: float, premium: float, symbol: str) -> int:
    ls = lot_size(symbol)
    cost_per_lot = premium * ls
    if cost_per_lot <= 0:
        return 1
    lots = int(capital / cost_per_lot)
    return max(1, lots)

# ── Technical Indicators ──────────────────────────────────────────

def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    if len(values) < period:
        return statistics.mean(values)
    k = 2.0 / (period + 1)
    result = statistics.mean(values[:period])
    for v in values[period:]:
        result = v * k + result * (1 - k)
    return result

def rsi(closes: List[float], period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    recent = deltas[-period:]
    gains  = [d for d in recent if d > 0]
    losses = [-d for d in recent if d < 0]
    ag = sum(gains)  / period if gains  else 0.0
    al = sum(losses) / period if losses else 1.0
    rs = ag / al if al > 0 else 100.0
    return round(100 - (100 / (1 + rs)), 2)

def vwap_from_candles(candles) -> float:
    if not candles:
        return 0.0
    pv = sum(((c.high + c.low + c.close) / 3.0) * c.volume for c in candles)
    tv = sum(c.volume for c in candles)
    return pv / tv if tv > 0 else 0.0

def avg_volume(candles, n: int = 20) -> float:
    vols = [c.volume for c in candles[-n:] if c.volume > 0]
    return statistics.mean(vols) if vols else 1.0

def volume_mult(candles, n: int = 20) -> float:
    if not candles:
        return 0.0
    av = avg_volume(candles, n)
    return candles[-1].volume / av if av > 0 else 0.0

def near_level(price: float, level: float, tolerance: float) -> bool:
    return abs(price - level) <= tolerance

def trend_direction(closes: List[float], lookback: int = 5) -> str:
    if len(closes) < lookback:
        return "NEUTRAL"
    subset = closes[-lookback:]
    if subset[-1] > subset[0] and subset[-1] > subset[-2]:
        return "UP"
    if subset[-1] < subset[0] and subset[-1] < subset[-2]:
        return "DOWN"
    return "NEUTRAL"

def change_pct(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0
    return round(((current - previous) / previous) * 100, 3)

# ── VIX Helpers ───────────────────────────────────────────────────

def vix_zone(vix: float) -> str:
    if vix < config.VIX_AVOID:
        return "AVOID"
    if vix < config.VIX_REDUCED_SIZE:
        return "REDUCED"
    if vix <= config.VIX_IDEAL_HIGH:
        return "IDEAL"
    if vix <= config.VIX_SPREAD_ONLY:
        return "ELEVATED"
    return "EXTREME"

def vix_size_multiplier(vix: float) -> float:
    z = vix_zone(vix)
    return {"AVOID": 0.0, "REDUCED": 0.5, "IDEAL": 1.0,
            "ELEVATED": 0.7, "EXTREME": 0.0}.get(z, 1.0)

# ── Formatting ────────────────────────────────────────────────────

def fmt_rs(amount: float) -> str:
    if abs(amount) >= 100000:
        return f"₹{amount/100000:.1f}L"
    return f"₹{amount:,.0f}"

def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"
