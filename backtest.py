#!/usr/bin/env python3
# ================================================================
#  backtest.py — Phase 3: Historical Replay Engine
#  WAR ROOM Trading Bot — 3-month backtest on 5m candle data
# ================================================================
#
#  Replays 3 months of 5-minute Nifty + BankNifty bars through the
#  live StrategyEngine (zero code changes to strategy logic).
#  For each signal generated, simulates the trade outcome using:
#    - ATM premium estimation (Bachelier approximation)
#    - Index-level SL from signal.sl_index
#    - T1/T2 index levels derived from signal.t1_pct / t2_pct
#
#  Usage:
#    python backtest.py                         # uses cached bars
#    python backtest.py --refresh               # re-fetch from Upstox
#    python backtest.py --symbol NIFTY          # single symbol
#    python backtest.py --vix 18                # override VIX (default 15)
#    python backtest.py --output backtest_results.xlsx
#
#  Known Limitations:
#    - VIX is static (no historical VIX data; configurable via --vix)
#    - Option chain stubs (delta=0.5, no IV filter) — all signals pass entry filter
#    - 300s cooldown based on wall clock, so each strategy fires ≤1x per day per symbol
#    - 1m candles backed by 5m data (some strategies may under-trigger)
#    - No slippage model (entry = signal bar close; exit = SL/T bar close)
# ================================================================

import sys
import os
import math
import argparse
import logging
from datetime import datetime, timedelta, date, time as dtime
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field

# ── Logging ────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,
    format="%(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("backtest")

# Silence noisy sub-loggers during replay
for noisy in ("Strategy", "DataFeed", "RiskManager"):
    logging.getLogger(noisy).setLevel(logging.ERROR)


# ── Imports ────────────────────────────────────────────────────────
import utils                              # must import before patching _SIMULATED_NOW
import config
from backtest_data import load_candles, load_candles_by_day, BarRow
from backtest_feed import BacktestFeed
from strategy_engine import StrategyEngine, Signal, ORB, Direction


# ── Constants ──────────────────────────────────────────────────────
DELTA_ATM    = 0.50     # ATM option delta (Bachelier ATM = 0.5 N(0)=0.5)
MARKET_CLOSE = dtime(15, 30)
MARKET_OPEN  = dtime(9, 15)
ORB_END      = dtime(9, 20)

STRATEGY_NAMES = [
    "ORB Breakout", "Straddle Event", "Gap and Go",
    "Trend Continuation", "15-Min Breakout", "S/R Reversal",
    "VWAP Pullback", "5-Min Engulfing", "Gamma Scalp", "EMA Crossover",
]


# ── Trade result dataclass ─────────────────────────────────────────

@dataclass
class TradeResult:
    date:        str
    symbol:      str
    strategy:    str
    direction:   str
    tier:        int
    entry_price: float        # index price at entry
    entry_prem:  float        # estimated ATM premium at entry
    sl_index:    float        # SL index level from signal
    t1_index:    float        # T1 target index level
    t2_index:    float        # T2 target index level
    exit_reason: str          # "SL" | "T1" | "T2" | "TIME" | "EOD"
    exit_price:  float        # index price at exit
    pnl_pct:     float        # % gain/loss on premium (e.g. 0.5 = +50%)
    bars_held:   int
    ts:          datetime


# ── Premium estimator (Bachelier ATM approximation) ─────────────────

def estimate_premium(spot: float, vix: float, days_to_exp: float) -> float:
    """
    ATM option premium ≈ 0.4 × σ × S × √T
    where σ = VIX/100 (annualised vol), T = days_to_exp/252

    This is the Bachelier (normal) ATM approximation — accurate to ~5%
    for short-dated options. Better than Black-Scholes for near-expiry.

    Returns the estimated CE or PE premium (they're ~equal ATM).
    """
    if vix <= 0 or days_to_exp <= 0:
        return max(spot * 0.005, 10.0)   # fallback: 0.5% of spot
    sigma = vix / 100.0
    T     = days_to_exp / 252.0
    prem  = 0.4 * sigma * spot * math.sqrt(T)
    return max(prem, 1.0)


def days_to_next_expiry(symbol: str, bar_dt: datetime) -> float:
    """
    Returns calendar days from bar_dt to next expiry for the symbol.
    Nifty: next Tuesday. BankNifty: next last-Tuesday-of-month.
    Uses utils._now() which is set to bar_dt during replay.
    """
    utils._SIMULATED_NOW = bar_dt
    exp_date_str, _ = utils.next_expiry(symbol)
    exp_dt = datetime.strptime(exp_date_str, "%Y-%m-%d")
    delta  = (exp_dt.date() - bar_dt.date()).days
    return max(delta, 0.5)   # floor at 0.5 days (same-day expiry)


# ── Index target levels from signal ──────────────────────────────────

def _compute_targets(sig: Signal, entry_prem: float) -> Tuple[float, float, float]:
    """
    Returns (sl_index, t1_index, t2_index) for the simulated trade.

    SL:  use signal.sl_index directly (set by strategy logic)
    T1/T2: convert premium % to index points using ATM delta
           index_move = premium × target_pct / DELTA_ATM
    """
    sl_index = sig.sl_index

    # Index move corresponding to T1/T2 premium gain
    t1_index_move = entry_prem * sig.t1_pct / DELTA_ATM
    t2_index_move = entry_prem * sig.t2_pct / DELTA_ATM

    if sig.direction in (Direction.CE,):
        # CE: need UP move
        t1_index = sig.entry_trigger + t1_index_move
        t2_index = sig.entry_trigger + t2_index_move
    else:
        # PE: need DOWN move
        t1_index = sig.entry_trigger - t1_index_move
        t2_index = sig.entry_trigger - t2_index_move

    return sl_index, t1_index, t2_index


# ── Trade simulator ──────────────────────────────────────────────────

def simulate_trade(
    sig:         Signal,
    entry_bar:   BarRow,
    future_bars: List[BarRow],
    vix:         float,
) -> TradeResult:
    """
    Walk forward through future_bars bar-by-bar and determine outcome.

    Exit conditions (first hit wins):
      1. Index crosses sl_index → LOSS (-sl_premium_pct)
      2. Index crosses t1_index → WIN (t1_pct)
      3. Index crosses t2_index → WIN (t2_pct, full target)
      4. Time >= hard_exit_time → close at current premium (TIME exit)
      5. No more bars (EOD) → close at last premium (EOD exit)

    Premium P&L is estimated as:
      entry_prem × pnl_pct (from signal targets)
    """
    days_to_exp = days_to_next_expiry(sig.symbol, entry_bar.dt)
    entry_prem  = estimate_premium(entry_bar.close, vix, days_to_exp)
    sl_idx, t1_idx, t2_idx = _compute_targets(sig, entry_prem)

    is_ce = sig.direction in (Direction.CE,)
    hard_exit = datetime.strptime(config.HARD_EXIT_TIME, "%H:%M").time()

    for bars_held, bar in enumerate(future_bars, start=1):
        lo, hi = bar.low, bar.high

        # Hard exit by time
        if bar.dt.time() >= hard_exit:
            days_rem = days_to_next_expiry(sig.symbol, bar.dt)
            cur_prem = estimate_premium(bar.close, vix, days_rem)
            pnl_pct  = (cur_prem - entry_prem) / entry_prem
            return TradeResult(
                date=entry_bar.dt.strftime("%Y-%m-%d"),
                symbol=sig.symbol, strategy=sig.strategy,
                direction=sig.direction.value,
                tier=sig.tier.value,
                entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                exit_reason="TIME", exit_price=bar.close,
                pnl_pct=round(pnl_pct, 4), bars_held=bars_held,
                ts=entry_bar.dt,
            )

        if is_ce:
            # CE: SL if index drops below sl_index; T1/T2 if index rises to target
            if lo <= sl_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="SL", exit_price=sl_idx,
                    pnl_pct=-sig.sl_premium_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )
            if hi >= t2_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="T2", exit_price=t2_idx,
                    pnl_pct=sig.t2_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )
            if hi >= t1_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="T1", exit_price=t1_idx,
                    pnl_pct=sig.t1_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )
        else:
            # PE: SL if index rises above sl_index; T1/T2 if index drops to target
            if sl_idx > 0 and hi >= sl_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="SL", exit_price=sl_idx,
                    pnl_pct=-sig.sl_premium_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )
            if lo <= t2_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="T2", exit_price=t2_idx,
                    pnl_pct=sig.t2_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )
            if lo <= t1_idx:
                return TradeResult(
                    date=entry_bar.dt.strftime("%Y-%m-%d"),
                    symbol=sig.symbol, strategy=sig.strategy,
                    direction=sig.direction.value, tier=sig.tier.value,
                    entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
                    sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
                    exit_reason="T1", exit_price=t1_idx,
                    pnl_pct=sig.t1_pct, bars_held=bars_held,
                    ts=entry_bar.dt,
                )

    # End of day — close at last bar's estimated premium
    last = future_bars[-1] if future_bars else entry_bar
    days_rem = days_to_next_expiry(sig.symbol, last.dt)
    cur_prem = estimate_premium(last.close, vix, days_rem)
    pnl_pct  = (cur_prem - entry_prem) / entry_prem
    return TradeResult(
        date=entry_bar.dt.strftime("%Y-%m-%d"),
        symbol=sig.symbol, strategy=sig.strategy,
        direction=sig.direction.value, tier=sig.tier.value,
        entry_price=entry_bar.close, entry_prem=round(entry_prem, 2),
        sl_index=round(sl_idx, 2), t1_index=round(t1_idx, 2), t2_index=round(t2_idx, 2),
        exit_reason="EOD", exit_price=last.close,
        pnl_pct=round(pnl_pct, 4), bars_held=len(future_bars),
        ts=entry_bar.dt,
    )


# ── Stats aggregator ─────────────────────────────────────────────────

@dataclass
class StrategyStats:
    strategy:   str
    trades:     int = 0
    wins:       int = 0
    losses:     int = 0
    total_pnl:  float = 0.0
    win_pnl:    float = 0.0
    loss_pnl:   float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_win(self) -> float:
        return self.win_pnl / self.wins if self.wins else 0.0

    @property
    def avg_loss(self) -> float:
        return self.loss_pnl / self.losses if self.losses else 0.0

    @property
    def expectancy(self) -> float:
        """E = (win_rate × avg_win) - (loss_rate × |avg_loss|)"""
        wr  = self.win_rate
        lr  = 1.0 - wr
        avg_w = self.avg_win
        avg_l = abs(self.avg_loss)
        return (wr * avg_w) - (lr * avg_l)

    @property
    def profit_factor(self) -> float:
        return abs(self.win_pnl / self.loss_pnl) if self.loss_pnl else float("inf")


def _update_stats(stats: Dict[str, StrategyStats], r: TradeResult):
    if r.strategy not in stats:
        stats[r.strategy] = StrategyStats(strategy=r.strategy)
    s = stats[r.strategy]
    s.trades    += 1
    s.total_pnl += r.pnl_pct
    if r.pnl_pct >= 0:
        s.wins    += 1
        s.win_pnl += r.pnl_pct
    else:
        s.losses    += 1
        s.loss_pnl  += r.pnl_pct


# ── Replay loop ──────────────────────────────────────────────────────

def run_backtest(
    symbols:       List[str],
    vix:           float     = 15.0,
    force_refresh: bool      = False,
) -> Tuple[List[TradeResult], Dict[str, StrategyStats]]:
    """
    Full 3-month replay.

    Returns:
        trades:     every simulated trade result
        stats:      per-strategy aggregated statistics
    """
    # ── Load historical bars ──────────────────────────────────────
    print(f"\n📊  Loading historical candles (force_refresh={force_refresh})…")
    bars_by_day: Dict[str, Dict[str, List[BarRow]]] = {}
    for sym in symbols:
        print(f"  {sym}…", end=" ", flush=True)
        bars_by_day[sym] = load_candles_by_day(sym, force_refresh=force_refresh)
        total = sum(len(v) for v in bars_by_day[sym].values())
        print(f"{total} bars across {len(bars_by_day[sym])} days")

    # ── Get sorted list of trading days (union of both symbols) ──
    all_days = sorted(set(
        day for sym in symbols for day in bars_by_day[sym]
    ))
    print(f"  Total trading days: {len(all_days)}")

    # ── Init feed and engine ──────────────────────────────────────
    feed   = BacktestFeed()
    engine = StrategyEngine(feed)

    # Pre-load ALL bars (not day-by-day) so advance() can find indices
    for sym in symbols:
        all_sym_bars: List[BarRow] = []
        for day in all_days:
            all_sym_bars.extend(bars_by_day[sym].get(day, []))
        feed.load(sym, all_sym_bars)

    trades: List[TradeResult]          = []
    stats:  Dict[str, StrategyStats]   = {}

    print(f"\n🔄  Replaying {len(all_days)} days…\n")

    for day_str in all_days:
        # ── Reset engine + ORB at start of day ─────────────────
        engine.reset_day()

        day_bars: Dict[str, List[BarRow]] = {}
        for sym in symbols:
            day_bars[sym] = bars_by_day[sym].get(day_str, [])

        if not any(day_bars.values()):
            continue

        # Use NIFTY bars as the primary clock; BANKNIFTY is driven in parallel
        primary_sym  = symbols[0]
        primary_bars = day_bars[primary_sym]

        for i, bar in enumerate(primary_bars):
            # Skip pre-market and post-market bars
            bar_time = bar.dt.time()
            if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
                continue

            # Set simulated time + advance feed for all symbols
            for sym in symbols:
                sym_bars = day_bars[sym]
                # Find matching or closest bar for this timestamp
                matching = next((b for b in sym_bars if b.dt == bar.dt), None)
                if matching:
                    feed.advance(sym, matching)
            feed.set_vix(vix)

            # Build ticks for each symbol and evaluate
            for sym in symbols:
                tick = feed.make_tick(sym)
                if tick is None:
                    continue

                signals = engine.evaluate(tick)

                for sig in signals:
                    # Look ahead: all bars for this symbol after entry bar (same day)
                    sym_bars = day_bars[sym]
                    entry_idx = next(
                        (j for j, b in enumerate(sym_bars) if b.dt == bar.dt),
                        None
                    )
                    if entry_idx is None:
                        continue

                    future_bars = sym_bars[entry_idx + 1:]
                    if not future_bars:
                        continue

                    result = simulate_trade(sig, bar, future_bars, vix)
                    trades.append(result)
                    _update_stats(stats, result)

        # Progress tick
        sys.stdout.write(f"\r  Day {all_days.index(day_str)+1}/{len(all_days)}: {day_str}  ")
        sys.stdout.flush()

    # Restore live mode
    utils._SIMULATED_NOW = None
    print(f"\n\n✅  Backtest complete. {len(trades)} trades simulated.\n")
    return trades, stats


# ── Terminal summary ─────────────────────────────────────────────────

def print_summary(stats: Dict[str, StrategyStats], trades: List[TradeResult]):
    """Rich-style terminal table of per-strategy results."""
    try:
        from rich.console import Console
        from rich.table import Table
        from rich import box

        console = Console()
        table   = Table(
            title  = "📈 WAR ROOM Backtest Results — Per-Strategy",
            box    = box.ROUNDED,
            show_header=True, header_style="bold cyan",
        )
        table.add_column("Strategy",        style="bold white", min_width=18)
        table.add_column("Trades",          justify="right")
        table.add_column("Win%",            justify="right", style="green")
        table.add_column("Avg Win",         justify="right", style="green")
        table.add_column("Avg Loss",        justify="right", style="red")
        table.add_column("Expectancy",      justify="right")
        table.add_column("Profit Factor",   justify="right")
        table.add_column("Total P&L",       justify="right")

        all_stats = sorted(stats.values(), key=lambda s: s.expectancy, reverse=True)
        for s in all_stats:
            color    = "green" if s.expectancy > 0 else "red"
            exp_str  = f"[{color}]{s.expectancy*100:+.1f}%[/{color}]"
            pf_str   = f"{s.profit_factor:.2f}" if s.profit_factor < 99 else "∞"
            table.add_row(
                s.strategy,
                str(s.trades),
                f"{s.win_rate*100:.0f}%",
                f"{s.avg_win*100:+.1f}%",
                f"{s.avg_loss*100:.1f}%",
                exp_str,
                pf_str,
                f"{s.total_pnl*100:+.1f}%",
            )

        console.print(table)

        # ── Exit reason breakdown ──────────────────────────────
        reasons: Dict[str, int] = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print("\nExit reasons:", " | ".join(f"{k}: {v}" for k, v in sorted(reasons.items())))
        wins = sum(1 for t in trades if t.pnl_pct >= 0)
        print(f"Overall win rate: {wins}/{len(trades)} = {wins/len(trades)*100:.1f}%\n")

    except ImportError:
        # Fallback: plain table
        print(f"\n{'Strategy':<22} {'Trades':>6} {'Win%':>6} {'E(%)':>7} {'PF':>6}")
        print("-" * 55)
        for s in sorted(stats.values(), key=lambda x: x.expectancy, reverse=True):
            pf = f"{s.profit_factor:.2f}" if s.profit_factor < 99 else " inf"
            print(f"{s.strategy:<22} {s.trades:>6} {s.win_rate*100:>5.0f}%"
                  f" {s.expectancy*100:>+6.1f}% {pf:>6}")


# ── XLSX output ───────────────────────────────────────────────────────

def save_xlsx(
    trades: List[TradeResult],
    stats:  Dict[str, StrategyStats],
    output_path: str,
):
    """Write per-trade log and per-strategy summary to XLSX."""
    try:
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, numbers
        from openpyxl.utils import get_column_letter
    except ImportError:
        print("⚠️  openpyxl not installed. Run: pip install openpyxl --break-system-packages")
        return

    wb = openpyxl.Workbook()

    # ── Sheet 1: Per-Trade Log ───────────────────────────────────
    ws1 = wb.active
    ws1.title = "Trade Log"

    hdr = ["Date", "Symbol", "Strategy", "Direction", "Tier",
           "Entry Price", "Entry Prem", "SL Index", "T1 Index", "T2 Index",
           "Exit Reason", "Exit Price", "P&L%", "Bars Held"]
    for col, h in enumerate(hdr, 1):
        cell = ws1.cell(row=1, column=col, value=h)
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(horizontal="center")

    green_fill = PatternFill("solid", fgColor="C6EFCE")
    red_fill   = PatternFill("solid", fgColor="FFC7CE")

    for row_idx, t in enumerate(trades, 2):
        vals = [
            t.date, t.symbol, t.strategy, t.direction, t.tier,
            t.entry_price, t.entry_prem, t.sl_index, t.t1_index, t.t2_index,
            t.exit_reason, t.exit_price, round(t.pnl_pct * 100, 2), t.bars_held,
        ]
        fill = green_fill if t.pnl_pct >= 0 else red_fill
        for col_idx, val in enumerate(vals, 1):
            cell      = ws1.cell(row=row_idx, column=col_idx, value=val)
            cell.fill = fill

    # Auto-width
    for col in ws1.columns:
        max_len = max((len(str(c.value)) for c in col if c.value), default=10)
        ws1.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 2, 30)

    # ── Sheet 2: Strategy Summary ────────────────────────────────
    ws2 = wb.create_sheet("Strategy Summary")

    hdr2 = ["Strategy", "Trades", "Wins", "Losses", "Win%",
            "Avg Win%", "Avg Loss%", "Expectancy%", "Profit Factor", "Total P&L%"]
    for col, h in enumerate(hdr2, 1):
        cell = ws2.cell(row=1, column=col, value=h)
        cell.font      = Font(bold=True, color="FFFFFF")
        cell.fill      = PatternFill("solid", fgColor="1F4E79")
        cell.alignment = Alignment(horizontal="center")

    for row_idx, s in enumerate(
        sorted(stats.values(), key=lambda x: x.expectancy, reverse=True), 2
    ):
        pf = s.profit_factor if s.profit_factor < 999 else 999
        ws2.append([
            s.strategy, s.trades, s.wins, s.losses,
            round(s.win_rate * 100, 1),
            round(s.avg_win * 100, 2),
            round(s.avg_loss * 100, 2),
            round(s.expectancy * 100, 2),
            round(pf, 2),
            round(s.total_pnl * 100, 2),
        ])
        # Colour expectancy cell
        exp_cell = ws2.cell(row=row_idx, column=8)
        exp_cell.fill = green_fill if s.expectancy >= 0 else red_fill

    for col in ws2.columns:
        max_len = max((len(str(c.value)) for c in col if c.value), default=10)
        ws2.column_dimensions[get_column_letter(col[0].column)].width = min(max_len + 2, 28)

    # ── Sheet 3: Monthly Breakdown ───────────────────────────────
    ws3 = wb.create_sheet("Monthly Breakdown")
    monthly: Dict[str, Dict] = {}
    for t in trades:
        month = t.date[:7]   # "2025-12"
        if month not in monthly:
            monthly[month] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
        monthly[month]["trades"] += 1
        if t.pnl_pct >= 0:
            monthly[month]["wins"] += 1
        monthly[month]["total_pnl"] += t.pnl_pct

    ws3.append(["Month", "Trades", "Wins", "Win%", "Total P&L%"])
    for m, d in sorted(monthly.items()):
        wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
        ws3.append([m, d["trades"], d["wins"], round(wr, 1), round(d["total_pnl"] * 100, 2)])

    wb.save(output_path)
    print(f"📁  XLSX saved → {output_path}")


# ── Entry point ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="WAR ROOM Backtest Engine")
    parser.add_argument("--symbol",  default="BOTH",  help="NIFTY | BANKNIFTY | BOTH")
    parser.add_argument("--vix",     type=float, default=15.0, help="Static VIX for premium estimation")
    parser.add_argument("--refresh", action="store_true", help="Force re-fetch from Upstox")
    parser.add_argument("--output",  default="backtest_results.xlsx",
                        help="XLSX output filename (saved in backtest_cache/)")
    args = parser.parse_args()

    if args.symbol.upper() == "BOTH":
        symbols = ["NIFTY", "BANKNIFTY"]
    elif args.symbol.upper() in ("NIFTY", "BANKNIFTY"):
        symbols = [args.symbol.upper()]
    else:
        print(f"Unknown symbol: {args.symbol}. Use NIFTY, BANKNIFTY, or BOTH.")
        sys.exit(1)

    print("=" * 60)
    print("  WAR ROOM — Phase 3 Backtesting Engine")
    print(f"  Symbols : {', '.join(symbols)}")
    print(f"  VIX     : {args.vix}")
    print(f"  Refresh : {args.refresh}")
    print("=" * 60)

    trades, stats = run_backtest(
        symbols       = symbols,
        vix           = args.vix,
        force_refresh = args.refresh,
    )

    print_summary(stats, trades)

    # Save XLSX to backtest_cache dir
    from pathlib import Path
    output_dir  = Path(__file__).parent / "backtest_cache"
    output_path = str(output_dir / args.output)
    save_xlsx(trades, stats, output_path)

    # Append to context.md
    try:
        ctx_path = Path(__file__).parent.parent / "memory" / "context.md"
        total_trades = len(trades)
        best = max(stats.values(), key=lambda s: s.expectancy) if stats else None
        summary = (
            f"\n[{datetime.now().strftime('%Y-%m-%d')}] - "
            f"Phase 3 backtest run: {total_trades} trades simulated — "
            f"Best strategy: {best.strategy if best else 'N/A'} "
            f"({best.expectancy*100:+.1f}% expectancy)" if best else ""
        )
        with open(ctx_path, "a") as f:
            f.write(summary + "\n")
    except Exception:
        pass


if __name__ == "__main__":
    main()
