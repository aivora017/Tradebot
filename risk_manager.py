# ================================================================
#  risk_manager.py — Real-Time Risk Gate
#  Every trade passes through here before execution.
# ================================================================

from datetime import datetime
from typing import Tuple

import config
from logger_setup import get_logger
from utils import (vix_zone, is_expiry_day, is_primary_window,
                   is_dead_zone, is_hard_exit_time, time_between)

log = get_logger("Risk")


class RiskManager:
    def __init__(self, tracker):
        self.tracker = tracker

    def check(self, symbol: str, tier: int, size_pct: float,
              vix: float) -> Tuple[bool, str]:
        checks = [
            self._check_stop_flag,
            self._check_daily_loss,
            self._check_daily_trades,
            self._check_concurrent,
            self._check_vix,
            self._check_time,
            self._check_capital,
        ]
        for fn in checks:
            allowed, reason = fn(symbol, tier, size_pct, vix)
            if not allowed:
                log.warning(f"🚫 RISK GATE: {reason}")
                return False, reason
        return True, "OK"

    def _check_stop_flag(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        if self.tracker.is_stopped():
            return False, "STOP TRADING flag active"
        return True, ""

    def _check_daily_loss(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        pnl_pct = self.tracker.get_daily_pnl_pct()
        if pnl_pct <= -(config.MAX_DAILY_LOSS_PCT * 100):
            return False, f"Daily loss limit hit ({pnl_pct:.1f}%)"
        return True, ""

    def _check_daily_trades(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        if self.tracker.get_daily_count() >= config.MAX_DAILY_TRADES:
            return False, f"Max {config.MAX_DAILY_TRADES} trades/day reached"
        return True, ""

    def _check_concurrent(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        if len(self.tracker.get_open_trades()) >= config.MAX_CONCURRENT:
            return False, f"Max {config.MAX_CONCURRENT} concurrent trades open"
        return True, ""

    def _check_vix(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        zone = vix_zone(vix)
        if zone == "AVOID":
            return False, f"VIX {vix:.1f} too low — avoid all option buying"
        if zone == "EXTREME":
            return False, f"VIX {vix:.1f} extreme — use spreads only"
        return True, ""

    def _check_time(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        if is_hard_exit_time():
            return False, "Past 2:00 PM — exit only, no new entries"
        if is_dead_zone():
            return False, "Dead zone 11:30–1:30 — no new entries"
        if is_expiry_day() and tier == 5 and not time_between("09:15", "09:35"):
            return False, "Expiry gamma play only valid 9:15–9:35"
        return True, ""

    def _check_capital(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        capital_needed = config.TOTAL_CAPITAL * size
        if capital_needed <= 0:
            return False, "Invalid position size"
        locked = sum(t.capital_locked for t in self.tracker.get_open_trades())
        available = config.TOTAL_CAPITAL - locked
        if capital_needed > available:
            return False, f"Insufficient capital (need ₹{capital_needed:.0f}, free ₹{available:.0f})"
        return True, ""

    def position_size(self, tier: int, vix: float, premium: float,
                      symbol: str) -> Tuple[float, int]:
        from utils import vix_size_multiplier, lot_size, lots_to_buy, capital_at_risk
        base_cap = capital_at_risk(symbol, tier)
        cap      = base_cap * vix_size_multiplier(vix)
        lots     = lots_to_buy(cap, premium, symbol) if premium > 0 else 1
        return cap, lots
