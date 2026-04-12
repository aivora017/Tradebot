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
            self._check_daily_target,
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

    def _check_daily_target(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        """Soft gate: once daily 10% target is hit, only allow tier 1-2 high-confidence entries."""
        pnl_pct = self.tracker.get_daily_pnl_pct()
        if pnl_pct >= (config.DAILY_TARGET_PCT * 100):
            # Target hit — only allow ORB and Gap-and-Go (tier 1-2) to keep compounding
            if tier > 2:
                return False, f"Daily 10% target hit ({pnl_pct:.1f}%) — protecting gains, tier {tier} blocked"
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
            return False, f"VIX {vix:.1f} too low — avoid all option buying (< {config.VIX_AVOID})"
        # EXTREME VIX (>40): vix_size_multiplier() already reduces to 60% size.
        # We do NOT block here — extreme VIX produces explosive moves for option buyers.
        # Strategies should prefer straddles/CE+PE in EXTREME — handled by Claude + S2/S9.
        return True, ""

    def _check_time(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        if is_hard_exit_time():
            return False, f"Past {config.HARD_EXIT_TIME} — exit only, no new entries"
        # Dead zone: soft gate — T1/T2 still allowed, T3+ blocked
        if is_dead_zone() and tier >= 3:
            return False, (f"Dead zone {config.DEAD_ZONE_START}–{config.DEAD_ZONE_END} "
                           f"— tier {tier} blocked (T1/T2 pass through)")
        # Expiry gamma: if EXPIRY_GAMMA_ALL_DAY is False, restrict to 9:15–9:35
        if (is_expiry_day() and tier == 5
                and not getattr(config, 'EXPIRY_GAMMA_ALL_DAY', False)
                and not time_between("09:15", "09:35")):
            return False, "Expiry gamma play only valid 9:15–9:35 (EXPIRY_GAMMA_ALL_DAY=False)"
        return True, ""

    def _check_capital(self, symbol, tier, size, vix) -> Tuple[bool, str]:
        capital_needed = config.TOTAL_CAPITAL * size if size > 0 else 0
        if capital_needed <= 0:
            # size_pct may be 0 from signal — not a blocker, position_size() computes real cap
            return True, ""
        locked = sum(t.capital_locked for t in self.tracker.get_open_trades())
        available = config.TOTAL_CAPITAL - locked
        if capital_needed > available:
            return False, f"Insufficient capital (need ₹{capital_needed:.0f}, free ₹{available:.0f})"
        return True, ""

    def position_size(self, tier: int, vix: float, premium: float,
                      symbol: str, strength: str = "MODERATE") -> Tuple[float, int]:
        """
        Strength-based capital deployment × VIX multiplier.
        Uses AVAILABLE capital (total - locked in open trades) so sizing
        naturally adapts as positions accumulate.

        strength → STRENGTH_CAPITAL_PCT:
          GODMODE=60% / STRONG=45% / MODERATE=35% / WEAK=20% of available.
        VIX multiplier scales allocation (IDEAL=1.0, ELEVATED=0.9, EXTREME=0.6).
        Lots = floor(allocated / cost_per_lot), floored at MIN_LOTS_PER_TRADE.
        """
        from utils import vix_size_multiplier, lot_size, lots_to_buy, capital_at_risk

        # Available = total minus capital locked in currently open trades
        locked    = sum(t.capital_locked for t in self.tracker.get_open_trades())
        available = max(config.TOTAL_CAPITAL - locked, 0)

        base_cap  = capital_at_risk(symbol, tier, strength, available_capital=available)
        cap       = base_cap * vix_size_multiplier(vix)
        ls        = lot_size(symbol)
        lots      = lots_to_buy(cap, premium, symbol) if premium > 0 else config.MIN_LOTS_PER_TRADE
        lots      = max(lots, config.MIN_LOTS_PER_TRADE)

        pct_used  = config.STRENGTH_CAPITAL_PCT.get(strength.upper(), 0.35)
        raw_lots  = int(cap / (premium * ls)) if (premium > 0 and ls > 0) else 0
        log.info(
            f"💰 Sizing [{strength}] | "
            f"total=₹{config.TOTAL_CAPITAL:,.0f} locked=₹{locked:,.0f} avail=₹{available:,.0f} | "
            f"alloc={pct_used*100:.0f}%→₹{cap:,.0f} | "
            f"₹{premium:.0f}×{ls}sh=₹{premium*ls:,.0f}/lot | "
            f"raw={raw_lots}→final={lots} lots"
        )
        return cap, lots
