# ================================================================
#  utils.py — Shared Utilities
# ================================================================

import statistics
from datetime import datetime, time as dtime, timedelta
from typing import List, Optional
import config

# ── Backtest time simulation ──────────────────────────────────────
# In live mode: _SIMULATED_NOW is None → _now() returns datetime.now()
# In backtest mode: set utils._SIMULATED_NOW = <bar_datetime> before each tick

_SIMULATED_NOW: Optional[datetime] = None

def _now() -> datetime:
    """Return current datetime — real in live mode, simulated in backtest mode."""
    return _SIMULATED_NOW if _SIMULATED_NOW is not None else datetime.now()

# ── Time Helpers ──────────────────────────────────────────────────

def now_time() -> dtime:
    return _now().time()

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

def _last_tuesday_of_month(year: int, month: int) -> datetime:
    """Return the last Tuesday of the given month."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    d = last_day
    while datetime(year, month, d).weekday() != 1:  # 1 = Tuesday
        d -= 1
    return datetime(year, month, d)

def is_banknifty_expiry() -> bool:
    """BankNifty expires on LAST TUESDAY of month only (monthly, not weekly)."""
    today = _now()
    if today.weekday() != 1:  # Must be Tuesday
        return False
    last_tue = _last_tuesday_of_month(today.year, today.month)
    return today.date() == last_tue.date()

def is_nifty_expiry() -> bool:
    """Nifty expires every Tuesday (weekly)."""
    return _now().weekday() == config.NIFTY_EXPIRY_WEEKDAY

def is_expiry_day() -> bool:
    """True if today is Nifty weekly expiry OR BankNifty monthly expiry."""
    return is_nifty_expiry() or is_banknifty_expiry()

def next_expiry(symbol: str):
    """
    NIFTY:     next Tuesday (weekly)
    BANKNIFTY: next last-Tuesday-of-month (monthly)
    """
    import calendar
    today = _now()

    if symbol == "NIFTY":
        # Next weekly Tuesday
        days = (1 - today.weekday()) % 7  # 1 = Tuesday
        if days == 0 and today.hour >= 15:
            days = 7
        exp = today + timedelta(days=days)
        return exp.strftime("%Y-%m-%d"), exp.strftime("%d %b")
    else:
        # Next last-Tuesday-of-month
        year, month = today.year, today.month
        last_tue = _last_tuesday_of_month(year, month)
        # If today is past this month's last Tuesday (or it's today after market close)
        cutoff = today.replace(hour=15, minute=30, second=0, microsecond=0)
        if last_tue.date() < today.date() or (last_tue.date() == today.date() and today >= cutoff):
            month += 1
            if month > 12:
                month, year = 1, year + 1
            last_tue = _last_tuesday_of_month(year, month)
        return last_tue.strftime("%Y-%m-%d"), last_tue.strftime("%d %b")

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

def capital_at_risk(symbol: str, tier: int, strength: str = "MODERATE") -> float:
    """
    Strength-based capital deployment. No fixed % ceiling.
    Uses available capital × strength multiplier — lets capital compound naturally.
    """
    pct = config.STRENGTH_CAPITAL_PCT.get(strength.upper(),
          config.STRENGTH_CAPITAL_PCT["MODERATE"])
    return config.TOTAL_CAPITAL * pct

def lots_to_buy(capital: float, premium: float, symbol: str) -> int:
    """
    Aggressive: deploy all allocated capital into lots.
    No upper cap on lots — only floor at MIN_LOTS_PER_TRADE.
    """
    ls = lot_size(symbol)
    cost_per_lot = premium * ls
    if cost_per_lot <= 0:
        return config.MIN_LOTS_PER_TRADE
    lots = int(capital / cost_per_lot)
    return max(config.MIN_LOTS_PER_TRADE, lots)


# ── Daily Compounding Capital ─────────────────────────────────────

import json as _json
import os as _os

_CAPITAL_FILE = _os.path.join(
    _os.path.dirname(__file__), "data", "daily_capital.json"
)

def load_compounded_capital() -> float:
    """
    Load yesterday's closing capital for daily compounding.
    Returns TOTAL_CAPITAL from .env if no saved capital exists.
    """
    try:
        if _os.path.exists(_CAPITAL_FILE):
            with open(_CAPITAL_FILE) as f:
                data = _json.load(f)
            saved = float(data.get("capital", 0))
            saved_date = data.get("date", "")
            today_str = datetime.now().strftime("%Y-%m-%d")
            # Only use saved capital if it's from a prior session (not today)
            if saved > 0 and saved_date != today_str:
                return saved
    except Exception:
        pass
    return config.TOTAL_CAPITAL

def save_closing_capital(closing_capital: float) -> None:
    """
    Persist today's closing capital so tomorrow's session compounds on it.
    closing_capital = TOTAL_CAPITAL + daily_pnl_rs
    """
    try:
        _os.makedirs(_os.path.dirname(_CAPITAL_FILE), exist_ok=True)
        with open(_CAPITAL_FILE, "w") as f:
            _json.dump({
                "date":    datetime.now().strftime("%Y-%m-%d"),
                "capital": round(closing_capital, 2),
            }, f)
    except Exception as e:
        pass  # Non-critical — next session falls back to env capital

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

# ── PCR Computation (P2.4) ────────────────────────────────────────

def compute_pcr(chain_rows, atm: float, n_strikes: int = None) -> dict:
    """Compute Put/Call Ratio focused on ATM ±n_strikes.

    Args:
        chain_rows: list of OptionRow objects (from feed.get_chain())
        atm:        ATM strike price (float)
        n_strikes:  strikes each side to include (defaults to config.PCR_ATM_STRIKES=10)

    Returns dict:
        value:        float — PCR (PE_OI / CE_OI), rounded to 3 dp
        signal:       "BULLISH" | "BEARISH" | "NEUTRAL"
        extreme:      "CONTRARIAN_BEARISH" | "CONTRARIAN_BULLISH" | "NONE"
        context:      str — one-line interpretation for Claude
        ce_oi:        int — total CE OI included
        pe_oi:        int — total PE OI included
        strikes_used: int — number of strike rows included
    """
    if n_strikes is None:
        n_strikes = config.PCR_ATM_STRIKES

    if not chain_rows:
        return {
            "value": 1.0, "signal": "NEUTRAL", "extreme": "NONE",
            "context": "No chain data available.",
            "ce_oi": 0, "pe_oi": 0, "strikes_used": 0,
        }

    # Step size: BankNifty strikes at 100 pts, Nifty at 50 pts
    step = 100 if atm >= 40000 else 50
    lo   = atm - n_strikes * step
    hi   = atm + n_strikes * step

    total_ce_oi = 0
    total_pe_oi = 0
    count       = 0
    for row in chain_rows:
        if lo <= row.strike <= hi:
            total_ce_oi += row.ce_oi
            total_pe_oi += row.pe_oi
            count       += 1

    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

    # ── Signal classification ─────────────────────────────────────
    if pcr > config.PCR_BULLISH:
        signal = "BULLISH"
    elif pcr < config.PCR_BEARISH:
        signal = "BEARISH"
    else:
        signal = "NEUTRAL"

    # ── Extreme (contrarian) check ────────────────────────────────
    if pcr > config.PCR_EXTREME_BEAR:
        extreme = "CONTRARIAN_BEARISH"
    elif pcr < config.PCR_EXTREME_BULL:
        extreme = "CONTRARIAN_BULLISH"
    else:
        extreme = "NONE"

    # ── One-line context string for Claude ────────────────────────
    _ctx = {
        ("BULLISH",  "NONE"):              f"PCR {pcr:.2f} — put writers active, index supported. CE bias.",
        ("BULLISH",  "CONTRARIAN_BEARISH"):f"PCR {pcr:.2f} — EXTREME put selling (>{config.PCR_EXTREME_BEAR}). Contrarian BEARISH alert: smart money may unwind.",
        ("BEARISH",  "NONE"):              f"PCR {pcr:.2f} — call writers dominant, overhead capped. PE bias.",
        ("BEARISH",  "CONTRARIAN_BULLISH"):f"PCR {pcr:.2f} — EXTREME call buying (<{config.PCR_EXTREME_BULL}). Contrarian BULLISH alert: overextended bearish positioning.",
        ("NEUTRAL",  "NONE"):              f"PCR {pcr:.2f} — balanced OI, no directional edge from chain.",
    }
    context = _ctx.get((signal, extreme), f"PCR {pcr:.2f} — {signal}.")

    return {
        "value":        pcr,
        "signal":       signal,
        "extreme":      extreme,
        "context":      context,
        "ce_oi":        total_ce_oi,
        "pe_oi":        total_pe_oi,
        "strikes_used": count,
    }


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
    """
    As aggressive option buyers: HIGH VIX = BIG MOVES = OPPORTUNITY.
    Never zero out. EXTREME VIX = trade straddles at 60% size, not stop.
    """
    z = vix_zone(vix)
    return {
        "AVOID":    0.0,   # truly complacent — no premium worth buying
        "REDUCED":  0.60,  # was 0.5 — low vol but still tradeable
        "IDEAL":    1.0,   # full size
        "ELEVATED": 0.90,  # was 0.7 — high VIX = good for option buyers
        "EXTREME":  0.60,  # was 0.0 — crisis moves are explosive, trade straddles
    }.get(z, 1.0)

# ── Formatting ────────────────────────────────────────────────────

def fmt_rs(amount: float) -> str:
    if abs(amount) >= 100000:
        return f"₹{amount/100000:.1f}L"
    return f"₹{amount:,.0f}"

def fmt_pct(p: float) -> str:
    sign = "+" if p >= 0 else ""
    return f"{sign}{p:.1f}%"
