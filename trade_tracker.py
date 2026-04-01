# ================================================================
#  trade_tracker.py — Trade Lifecycle + P&L + Journal
# ================================================================

import json
import uuid
import threading
from datetime import datetime
from typing import Optional, List, Dict
from dataclasses import dataclass, field, asdict

import config
from logger_setup import get_logger

log = get_logger("Tracker")


@dataclass
class Trade:
    id:              str
    symbol:          str
    strike:          int
    option_type:     str
    expiry:          str
    strategy:        str
    tier:            int
    direction:       str
    entry_index:     float
    entry_premium:   float
    current_premium: float
    qty:             int
    lots:            int
    capital_locked:  float
    sl_pct:          float
    sl_index:        float
    t1_pct:          float
    t2_pct:          float
    exit_by:         str
    instrument_key:  str    = ""
    order_id:        str    = ""
    entry_time:      datetime = field(default_factory=datetime.now)
    exit_time:       Optional[datetime] = None
    exit_premium:    float  = 0.0
    exit_reason:     str    = ""
    t1_booked:       bool   = False
    t1_qty_booked:   int    = 0
    is_open:         bool   = True
    notes:           str    = ""

    @property
    def pnl_pct(self) -> float:
        if self.entry_premium == 0:
            return 0.0
        return (self.current_premium - self.entry_premium) / self.entry_premium

    @property
    def pnl_rs(self) -> float:
        return (self.current_premium - self.entry_premium) * self.qty

    @property
    def realized_pnl_rs(self) -> float:
        if self.is_open or self.entry_premium == 0:
            return 0.0
        return (self.exit_premium - self.entry_premium) * self.qty

    @property
    def pnl_pct_display(self) -> str:
        p = self.pnl_pct * 100
        sign = "+" if p >= 0 else ""
        return f"{sign}{p:.1f}%"

    def sl_hit(self) -> bool:
        return self.is_open and self.pnl_pct <= -self.sl_pct

    def t1_hit(self) -> bool:
        return self.is_open and not self.t1_booked and self.pnl_pct >= self.t1_pct

    def t2_hit(self) -> bool:
        return self.is_open and self.pnl_pct >= self.t2_pct

    def time_exit_due(self) -> bool:
        if not self.is_open:
            return False
        now_t = datetime.now().time()
        h, m  = map(int, self.exit_by.split(":"))
        from datetime import time as dtime
        return now_t >= dtime(h, m)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_time"]  = self.exit_time.isoformat() if self.exit_time else None
        return d


class TradeTracker:

    def __init__(self):
        self._trades:      Dict[str, Trade] = {}
        self._lock         = threading.RLock()
        self._daily_pnl:   float  = 0.0
        self._daily_count: int    = 0
        self._loss_streak: int    = 0
        self._stopped:     bool   = False
        self._alerts:      List[dict] = []
        self._load()

    def open_trade(self, symbol: str, strike: int, option_type: str,
                   expiry: str, strategy: str, tier: int, direction: str,
                   entry_index: float, entry_premium: float, qty: int,
                   lots: int, capital_locked: float, sl_pct: float,
                   sl_index: float, t1_pct: float, t2_pct: float,
                   exit_by: str, instrument_key: str = "",
                   order_id: str = "", notes: str = "") -> Optional[Trade]:
        with self._lock:
            if self._stopped:
                log.warning("Stop flag active — no new trades.")
                return None
            if self._daily_count >= config.MAX_DAILY_TRADES:
                log.warning(f"Daily trade limit ({config.MAX_DAILY_TRADES}) reached.")
                return None
            open_count = sum(1 for t in self._trades.values() if t.is_open)
            if open_count >= config.MAX_CONCURRENT:
                log.warning(f"Max concurrent trades ({config.MAX_CONCURRENT}) reached.")
                return None

            tid = str(uuid.uuid4())[:8].upper()
            trade = Trade(
                id=tid, symbol=symbol, strike=strike,
                option_type=option_type, expiry=expiry,
                strategy=strategy, tier=tier, direction=direction,
                entry_index=entry_index, entry_premium=entry_premium,
                current_premium=entry_premium, qty=qty, lots=lots,
                capital_locked=capital_locked, sl_pct=sl_pct,
                sl_index=sl_index, t1_pct=t1_pct, t2_pct=t2_pct,
                exit_by=exit_by, instrument_key=instrument_key,
                order_id=order_id, notes=notes,
            )
            self._trades[tid] = trade
            self._daily_count += 1

            msg = (f"✅ TRADE OPENED [{tid}] | "
                   f"{symbol} {strike}{option_type} | "
                   f"Premium ₹{entry_premium:.0f} | "
                   f"Qty {qty} | SL {sl_pct*100:.0f}%")
            log.info(msg)
            self._push_alert("OPENED", tid, msg)
            self._save()
            return trade

    def update_premium(self, trade_id: str, current_premium: float):
        with self._lock:
            t = self._trades.get(trade_id)
            if t and t.is_open:
                t.current_premium = current_premium
                # Only warn if there is a meaningful SL set (> 1%) to avoid spam
                # when SL is trailed to breakeven (0.0) and price oscillates near entry
                if t.sl_pct > 0.01 and t.pnl_pct <= -(t.sl_pct * 0.8) and t.pnl_pct > -(t.sl_pct):
                    self._push_alert("SL_WARNING", trade_id,
                                     f"⚠️ SL WARNING [{trade_id}]: P&L {t.pnl_pct_display}")

    def update_sl(self, trade_id: str, new_sl_pct: float):
        """
        Update SL percentage on an open trade.
        Used for trail SL to breakeven after T1 hit:
          new_sl_pct = 0.0 → SL triggers when premium falls back to entry (breakeven).
        Thread-safe. No-op if trade is closed.
        """
        with self._lock:
            t = self._trades.get(trade_id)
            if t and t.is_open:
                old_sl = t.sl_pct
                t.sl_pct = new_sl_pct
                log.info(f"🔒 SL trailed [{trade_id}]: {old_sl*100:.0f}% → {new_sl_pct*100:.0f}% (breakeven)")

    def book_t1(self, trade_id: str, exit_premium: float, qty_booked: int):
        with self._lock:
            t = self._trades.get(trade_id)
            if not t or not t.is_open or t.t1_booked:
                return
            t.t1_booked     = True
            t.t1_qty_booked = qty_booked
            pnl_p = (exit_premium - t.entry_premium) / t.entry_premium * 100
            msg = (f"📈 T1 BOOKED [{trade_id}]: +{pnl_p:.1f}% | "
                   f"Exit ₹{exit_premium:.0f} | Qty {qty_booked}")
            log.info(msg)
            self._push_alert("T1_BOOKED", trade_id, msg)

    def close_trade(self, trade_id: str, exit_premium: float, reason: str):
        closed_trade = None
        with self._lock:
            t = self._trades.get(trade_id)
            if not t or not t.is_open:
                return
            t.is_open      = False
            t.exit_time    = datetime.now()
            t.exit_premium = exit_premium
            t.exit_reason  = reason

            pnl_pct = (exit_premium - t.entry_premium) / t.entry_premium * 100
            pnl_rs  = t.realized_pnl_rs
            self._daily_pnl += pnl_rs

            if pnl_pct < 0:
                self._loss_streak += 1
            else:
                self._loss_streak = 0

            if self._loss_streak >= config.LOSS_STREAK_STOP:
                self._stopped = True
                stop_msg = (f"🔴 STOP TRADING: {config.LOSS_STREAK_STOP} consecutive losses.")
                log.warning(stop_msg)
                self._push_alert("STOP_TRADING", trade_id, stop_msg)

            emoji = "✅" if pnl_pct >= 0 else "❌"
            msg = (f"{emoji} TRADE CLOSED [{trade_id}] | {reason} | "
                   f"P&L: {'+' if pnl_pct >= 0 else ''}{pnl_pct:.1f}% | "
                   f"₹{pnl_rs:+.0f}")
            log.info(msg)
            self._push_alert("CLOSED", trade_id, msg)
            self._save()
            closed_trade = t  # capture reference for memory recording (outside lock)

        # ── Persistent memory: record this trade for self-evolution ───
        # Done outside the tracker lock to avoid deadlocks with bot_memory's own lock.
        if closed_trade is not None:
            try:
                from bot_memory import get_memory
                get_memory().record_closed_trade(closed_trade)
            except Exception as _mem_err:
                log.debug(f"Memory record skipped: {_mem_err}")

    def check_exits(self) -> List[dict]:
        alerts = []
        with self._lock:
            for t in self._trades.values():
                if not t.is_open:
                    continue
                if t.sl_hit():
                    alerts.append({
                        "type": "SL_HIT", "trade_id": t.id, "trade": t,
                        "message": (f"🔴 SL HIT [{t.id}]: "
                                    f"{t.symbol} {t.strike}{t.option_type} | "
                                    f"P&L {t.pnl_pct_display}")
                    })
                elif t.t1_hit():
                    alerts.append({
                        "type": "T1_HIT", "trade_id": t.id, "trade": t,
                        "message": (f"🟡 T1 [{t.id}]: "
                                    f"{t.symbol} {t.strike}{t.option_type} | "
                                    f"+{t.pnl_pct*100:.1f}%")
                    })
                elif t.t2_hit():
                    alerts.append({
                        "type": "T2_HIT", "trade_id": t.id, "trade": t,
                        "message": (f"🟢 T2 [{t.id}]: "
                                    f"{t.symbol} {t.strike}{t.option_type} | "
                                    f"+{t.pnl_pct*100:.1f}%")
                    })
                if t.time_exit_due():
                    alerts.append({
                        "type": "TIME_EXIT", "trade_id": t.id, "trade": t,
                        "message": (f"⏰ TIME EXIT [{t.id}]: "
                                    f"Exit by {t.exit_by}")
                    })
        return alerts

    def get_open_trades(self) -> List[Trade]:
        with self._lock:
            return [t for t in self._trades.values() if t.is_open]

    def get_all_trades(self) -> List[Trade]:
        with self._lock:
            return list(self._trades.values())

    def get_trade(self, tid: str) -> Optional[Trade]:
        with self._lock:
            return self._trades.get(tid)

    def get_daily_pnl(self) -> float:
        return self._daily_pnl

    def get_daily_pnl_pct(self) -> float:
        return (self._daily_pnl / config.TOTAL_CAPITAL) * 100 if config.TOTAL_CAPITAL else 0.0

    def get_daily_count(self) -> int:
        return self._daily_count

    def is_stopped(self) -> bool:
        return self._stopped or self.get_daily_pnl_pct() <= -(config.MAX_DAILY_LOSS_PCT * 100)

    def pop_alerts(self) -> List[dict]:
        with self._lock:
            a, self._alerts = self._alerts[:], []
            return a

    def get_stats(self) -> dict:
        with self._lock:
            closed = [t for t in self._trades.values() if not t.is_open]
            wins   = [t for t in closed if t.exit_premium > t.entry_premium]
            n      = len(closed)
            return {
                "total_trades":  n,
                "wins":          len(wins),
                "losses":        n - len(wins),
                "win_rate_pct":  round(len(wins) / n * 100, 1) if n > 0 else 0.0,
                "daily_pnl_rs":  round(self._daily_pnl, 2),
                "daily_pnl_pct": round(self.get_daily_pnl_pct(), 2),
                "daily_count":   self._daily_count,
                "loss_streak":   self._loss_streak,
                "open_trades":   len(self.get_open_trades()),
                "stopped":       self.is_stopped(),
            }

    def reset_day(self):
        with self._lock:
            self._daily_pnl   = 0.0
            self._daily_count = 0
            self._loss_streak = 0
            self._stopped     = False
            self._alerts      = []
        log.info("Trade tracker daily reset.")

    def _push_alert(self, atype: str, tid: str, msg: str):
        self._alerts.append({
            "type": atype, "trade_id": tid,
            "message": msg, "ts": datetime.now().strftime("%H:%M:%S")
        })

    def _save(self):
        try:
            import os
            os.makedirs("data", exist_ok=True)
            with open(config.TRADE_JOURNAL, "w") as f:
                json.dump([t.to_dict() for t in self._trades.values()],
                          f, indent=2, default=str)
        except Exception as e:
            log.error(f"Journal save error: {e}")

    def _load(self):
        try:
            with open(config.TRADE_JOURNAL) as f:
                data = json.load(f)
            for d in data:
                try:
                    d["entry_time"] = datetime.fromisoformat(d["entry_time"])
                    if d.get("exit_time"):
                        d["exit_time"] = datetime.fromisoformat(d["exit_time"])
                    t = Trade(**d)
                    self._trades[t.id] = t
                    if t.is_open:
                        self._daily_count += 1
                        self._daily_pnl   += 0  # open trades: unrealised, skip
                    else:
                        self._daily_pnl += t.realized_pnl_rs
                except Exception as te:
                    log.warning(f"Skipping malformed trade entry: {te}")
            log.info(f"Restored {len(self._trades)} trades from journal "
                     f"({len(self.get_open_trades())} open).")
        except FileNotFoundError:
            pass
        except Exception as e:
            log.error(f"Journal load error: {e}")
