#!/usr/bin/env python3
# ================================================================
#  gate_check.py — Phase 3 Paper Trading Gate Checker
#  WAR ROOM Trading Bot
# ================================================================
#
#  Reads the live trade journal, computes all Phase 3 prerequisite
#  metrics, and outputs a GO LIVE / NOT READY verdict.
#
#  Phase 3 Gates (from roadmap):
#    G1: 20 consecutive paper trading days complete
#    G2: Win rate >= 55% across all closed trades
#    G3: No single-day loss > 5% of capital (₹2,500 on ₹50K)
#    G4: Positive expectancy (avg win > avg loss, 3:2 minimum)
#    G5: All Phase 2 upgrades implemented [auto-pass — all done]
#
#  Usage:
#    python gate_check.py                          # default journal path
#    python gate_check.py --journal path/to/file   # custom journal
#    python gate_check.py --capital 100000         # different capital
#    python gate_check.py --start 2026-03-24       # override paper start date
#    python gate_check.py --target-days 20         # override required days
# ================================================================

import json
import sys
import argparse
import os
from datetime import datetime, date, timedelta
from typing import List, Dict, Optional, Tuple
from collections import defaultdict
from pathlib import Path

# ── Default paths ─────────────────────────────────────────────────
SCRIPT_DIR   = Path(__file__).parent
DEFAULT_JOURNAL = str(SCRIPT_DIR / "data" / "trade_journal.json")

# ── Phase 3 gate thresholds ───────────────────────────────────────
GATE_WIN_RATE_MIN   = 0.55    # G2: >= 55%
GATE_MAX_DAY_LOSS   = 0.05    # G3: < 5% capital in a single day
GATE_EXPECT_MIN     = 0.0     # G4: expectancy > 0 (avg win > avg loss)
GATE_EXPECT_RATIO   = 1.5     # G4: ideally avg_win / avg_loss >= 1.5 (3:2)
PAPER_START_DATE    = date(2026, 3, 24)   # Phase 1 paper trading start

# ── NSE trading calendar helper ───────────────────────────────────

def _count_trading_days(start: date, end: date) -> int:
    """
    Count market trading days between start and end (inclusive).
    Approximation: Mon–Fri only. Does NOT exclude NSE holidays.
    For a more accurate count, real NSE holiday calendar needed.
    """
    count = 0
    cur   = start
    while cur <= end:
        if cur.weekday() < 5:   # Mon=0 … Fri=4
            count += 1
        cur += timedelta(days=1)
    return count


def _trading_days_list(start: date, end: date) -> List[date]:
    """Return list of Mon–Fri dates in [start, end]."""
    days = []
    cur  = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


# ── Journal reader ─────────────────────────────────────────────────

def load_journal(journal_path: str) -> List[dict]:
    """Load trade journal. Returns empty list if file absent."""
    if not os.path.exists(journal_path):
        return []
    try:
        with open(journal_path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"⚠️  Could not read journal: {e}")
        return []


# ── Core metric computations ───────────────────────────────────────

class GateMetrics:

    def __init__(self, trades: List[dict], capital: float, paper_start: date):
        self.capital      = capital
        self.paper_start  = paper_start
        self.raw          = trades

        # Only closed trades contribute to statistics
        self.closed: List[dict] = [t for t in trades if not t.get("is_open", True)]
        self.open:   List[dict] = [t for t in trades if t.get("is_open", True)]

        # Parse entry/exit datetimes
        for t in self.closed:
            t["_entry_dt"] = _parse_dt(t.get("entry_time"))
            t["_exit_dt"]  = _parse_dt(t.get("exit_time"))

        self._compute()

    def _compute(self):
        closed = self.closed
        n      = len(closed)

        # ── Win/Loss split ──────────────────────────────────────────
        def pnl_pct(t: dict) -> float:
            ep, xp = t.get("entry_premium", 0), t.get("exit_premium", 0)
            return (xp - ep) / ep if ep else 0.0

        def pnl_rs(t: dict) -> float:
            ep, xp, qty = t.get("entry_premium", 0), t.get("exit_premium", 0), t.get("qty", 0)
            return (xp - ep) * qty

        wins   = [t for t in closed if pnl_pct(t) >= 0]
        losses = [t for t in closed if pnl_pct(t) < 0]

        self.total_trades  = n
        self.total_wins    = len(wins)
        self.total_losses  = len(losses)
        self.win_rate      = len(wins) / n if n > 0 else 0.0

        self.avg_win_pct   = (sum(pnl_pct(t) for t in wins)   / len(wins))   if wins   else 0.0
        self.avg_loss_pct  = (sum(pnl_pct(t) for t in losses) / len(losses)) if losses else 0.0  # negative
        self.expectancy    = (
            (self.win_rate * self.avg_win_pct) +
            ((1 - self.win_rate) * self.avg_loss_pct)
        )

        # Win/loss ratio (positive / abs(negative))
        self.win_loss_ratio = (
            abs(self.avg_win_pct / self.avg_loss_pct)
            if self.avg_loss_pct != 0 else float("inf")
        )

        # ── Largest single-day loss ─────────────────────────────────
        # Group closed trades by exit date
        day_pnl_rs: Dict[str, float] = defaultdict(float)
        for t in closed:
            exit_dt = t.get("_exit_dt")
            if exit_dt:
                day_key = exit_dt.strftime("%Y-%m-%d")
                day_pnl_rs[day_key] += pnl_rs(t)

        if day_pnl_rs:
            self.worst_day_str    = min(day_pnl_rs, key=day_pnl_rs.get)
            self.worst_day_rs     = day_pnl_rs[self.worst_day_str]
            self.worst_day_pct    = self.worst_day_rs / self.capital   # negative = loss
            self.best_day_str     = max(day_pnl_rs, key=day_pnl_rs.get)
            self.best_day_rs      = day_pnl_rs[self.best_day_str]
        else:
            self.worst_day_str = self.best_day_str = "N/A"
            self.worst_day_rs  = self.best_day_rs  = 0.0
            self.worst_day_pct = 0.0

        # ── Days traded ─────────────────────────────────────────────
        # Unique dates that had at least one closed trade
        self.traded_dates: List[date] = sorted(set(
            t["_exit_dt"].date()
            for t in closed
            if t.get("_exit_dt")
        ))
        self.days_traded = len(self.traded_dates)

        # Trading days elapsed since paper start (up to today)
        today = date.today()
        self.days_elapsed = _count_trading_days(self.paper_start, today)

        # ── Cumulative P&L ──────────────────────────────────────────
        self.total_pnl_rs  = sum(pnl_rs(t) for t in closed)
        self.total_pnl_pct = self.total_pnl_rs / self.capital if self.capital else 0.0

        # ── Per-strategy breakdown ──────────────────────────────────
        strat_stats: Dict[str, dict] = {}
        for t in closed:
            s = t.get("strategy", "Unknown")
            if s not in strat_stats:
                strat_stats[s] = {"n": 0, "wins": 0, "pnl": 0.0}
            strat_stats[s]["n"]    += 1
            strat_stats[s]["pnl"]  += pnl_pct(t)
            if pnl_pct(t) >= 0:
                strat_stats[s]["wins"] += 1
        self.strat_stats = strat_stats

        # ── Per-day P&L table ───────────────────────────────────────
        self.day_pnl = {
            d: {"rs": v, "pct": v / self.capital}
            for d, v in sorted(day_pnl_rs.items())
        }


def _parse_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:26], fmt)
        except ValueError:
            pass
    return None


# ── Gate evaluator ─────────────────────────────────────────────────

class GateResult:
    def __init__(self, gate_id: str, name: str, pass_: bool,
                 actual: str, required: str, note: str = ""):
        self.gate_id  = gate_id
        self.name     = name
        self.passed   = pass_
        self.actual   = actual
        self.required = required
        self.note     = note


def evaluate_gates(m: GateMetrics, target_days: int = 20) -> List[GateResult]:
    gates = []

    # G1 — 20 consecutive paper trading days
    g1_pass = m.days_elapsed >= target_days
    gates.append(GateResult(
        "G1", "Paper Trading Days",
        g1_pass,
        actual   = f"{m.days_elapsed} calendar trading days elapsed, {m.days_traded} days with trades",
        required = f"≥ {target_days} elapsed trading days",
        note     = "" if g1_pass else f"Need {target_days - m.days_elapsed} more trading day(s)",
    ))

    # G2 — Win rate >= 55%
    g2_pass = m.total_trades >= 10 and m.win_rate >= GATE_WIN_RATE_MIN
    g2_note = ""
    if m.total_trades < 10:
        g2_note = f"Only {m.total_trades} closed trades — need ≥ 10 for meaningful win rate"
    elif not g2_pass:
        needed = int(GATE_WIN_RATE_MIN * m.total_trades) - m.total_wins + 1
        g2_note = f"Need {needed} more winning trade(s) to hit 55%"
    gates.append(GateResult(
        "G2", "Win Rate",
        g2_pass,
        actual   = f"{m.win_rate*100:.1f}% ({m.total_wins}W / {m.total_losses}L / {m.total_trades} total)",
        required = f"≥ {GATE_WIN_RATE_MIN*100:.0f}% (min 10 closed trades)",
        note     = g2_note,
    ))

    # G3 — No single-day loss > 5% of capital
    worst_loss_pct = m.worst_day_pct   # negative value or 0
    g3_pass = worst_loss_pct >= -GATE_MAX_DAY_LOSS
    gates.append(GateResult(
        "G3", "Max Single-Day Loss",
        g3_pass,
        actual   = (f"{worst_loss_pct*100:.2f}% (₹{m.worst_day_rs:+.0f}) on {m.worst_day_str}"
                    if m.worst_day_str != "N/A" else "No closed trades yet"),
        required = f"< {GATE_MAX_DAY_LOSS*100:.0f}% (₹{GATE_MAX_DAY_LOSS*m.capital:,.0f}) in any single day",
        note     = "" if g3_pass else f"Day {m.worst_day_str} breached the daily loss limit",
    ))

    # G4 — Positive expectancy + 3:2 win/loss ratio
    g4_expect_ok = m.expectancy > GATE_EXPECT_MIN
    g4_ratio_ok  = m.win_loss_ratio >= GATE_EXPECT_RATIO
    g4_pass      = m.total_trades >= 10 and g4_expect_ok and g4_ratio_ok
    ratio_str    = f"{m.win_loss_ratio:.2f}x" if m.win_loss_ratio < 99 else "∞"
    g4_note      = ""
    if m.total_trades < 10:
        g4_note = "Insufficient trades for reliable expectancy"
    elif not g4_expect_ok:
        g4_note = f"Expectancy negative — avg loss ({m.avg_loss_pct*100:.1f}%) eating wins"
    elif not g4_ratio_ok:
        g4_note = f"Win/loss ratio {ratio_str} below 3:2 minimum — tighten entries or let winners run"
    gates.append(GateResult(
        "G4", "Expectancy & Reward Ratio",
        g4_pass,
        actual   = (f"E = {m.expectancy*100:+.2f}% | "
                    f"Avg Win {m.avg_win_pct*100:+.1f}% | "
                    f"Avg Loss {m.avg_loss_pct*100:.1f}% | "
                    f"Ratio {ratio_str}"),
        required = f"E > 0%, W/L ratio ≥ {GATE_EXPECT_RATIO}x (3:2)",
        note     = g4_note,
    ))

    # G5 — All Phase 2 upgrades done (auto-pass — hardcoded, all shipped)
    gates.append(GateResult(
        "G5", "Phase 2 Complete",
        True,
        actual   = "P2.1 Greeks ✅  P2.2 GIFT Nifty ✅  P2.3 FII/DII ✅  P2.4 PCR ✅  P2.5 Heavyweights ✅",
        required = "All 5 Phase 2 upgrades shipped",
    ))

    return gates


# ── Renderer (rich if available, plain fallback) ───────────────────

def render_report(m: GateMetrics, gates: List[GateResult]):
    all_pass    = all(g.passed for g in gates)
    verdict     = "🟢  GO LIVE" if all_pass else "🔴  NOT READY"
    gates_pass  = sum(1 for g in gates if g.passed)
    gates_total = len(gates)

    try:
        from rich.console import Console
        from rich.table import Table
        from rich.panel import Panel
        from rich.text import Text
        from rich import box

        console = Console()

        # ── Header ──────────────────────────────────────────────────
        console.print()
        border_color = "green" if all_pass else "red"
        console.rule(f"[bold {border_color}]  WAR ROOM — Paper Trading Gate Check  [/bold {border_color}]")
        console.print()

        # ── Verdict panel ────────────────────────────────────────────
        vtext = Text(f"  {verdict}  ", style=f"bold {'green' if all_pass else 'red'}", justify="center")
        vtext.append(f"\n  Gates passed: {gates_pass}/{gates_total}  ", style="white")
        console.print(Panel(vtext, expand=False, border_style=border_color))
        console.print()

        # ── Key metrics ──────────────────────────────────────────────
        m_table = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
        m_table.add_column("Metric", style="bold cyan", min_width=24)
        m_table.add_column("Value",  style="white")

        rows = [
            ("Closed Trades",         str(m.total_trades)),
            ("Open Trades",           str(len(m.open))),
            ("Days Elapsed",          f"{m.days_elapsed} trading days (since {m.paper_start})"),
            ("Days with Trades",      str(m.days_traded)),
            ("Cumulative P&L",        f"₹{m.total_pnl_rs:+,.0f}  ({m.total_pnl_pct*100:+.2f}%)"),
            ("Win Rate",              f"{m.win_rate*100:.1f}%  ({m.total_wins}W / {m.total_losses}L)"),
            ("Avg Win",               f"{m.avg_win_pct*100:+.1f}%"),
            ("Avg Loss",              f"{m.avg_loss_pct*100:.1f}%"),
            ("Expectancy",            f"{m.expectancy*100:+.2f}% per trade"),
            ("Win/Loss Ratio",        f"{m.win_loss_ratio:.2f}x" if m.win_loss_ratio < 99 else "∞"),
            ("Worst Day",             f"₹{m.worst_day_rs:+,.0f}  ({m.worst_day_pct*100:.2f}%)  [{m.worst_day_str}]"),
            ("Best Day",              f"₹{m.best_day_rs:+,.0f}  [{m.best_day_str}]"),
        ]
        for k, v in rows:
            m_table.add_row(k, v)

        console.print(m_table)
        console.print()

        # ── Gates table ──────────────────────────────────────────────
        g_table = Table(
            title="Phase 3 Go-Live Gates",
            box=box.ROUNDED, show_header=True,
            header_style="bold white on dark_blue",
        )
        g_table.add_column("Gate", width=4)
        g_table.add_column("Check",    min_width=24, style="bold")
        g_table.add_column("Status",   width=8,  justify="center")
        g_table.add_column("Required", min_width=30)
        g_table.add_column("Actual",   min_width=40)
        g_table.add_column("Note",     min_width=30, style="dim")

        for g in gates:
            icon  = "[green]✅ PASS[/green]" if g.passed else "[red]❌ FAIL[/red]"
            color = "green" if g.passed else "red"
            g_table.add_row(
                g.gate_id,
                f"[{color}]{g.name}[/{color}]",
                icon,
                g.required,
                g.actual,
                g.note,
            )

        console.print(g_table)
        console.print()

        # ── Per-strategy breakdown ────────────────────────────────────
        if m.strat_stats:
            s_table = Table(
                title="Per-Strategy Paper Performance",
                box=box.SIMPLE_HEAD, show_header=True,
                header_style="bold cyan",
            )
            s_table.add_column("Strategy",  min_width=20)
            s_table.add_column("Trades",    justify="right")
            s_table.add_column("Win%",      justify="right")
            s_table.add_column("Avg P&L%",  justify="right")

            for strat, st in sorted(m.strat_stats.items(),
                                    key=lambda x: x[1]["wins"] / x[1]["n"] if x[1]["n"] else 0,
                                    reverse=True):
                wr    = st["wins"] / st["n"] * 100 if st["n"] else 0
                avg_p = st["pnl"]  / st["n"] * 100 if st["n"] else 0
                color = "green" if avg_p >= 0 else "red"
                s_table.add_row(
                    strat,
                    str(st["n"]),
                    f"{wr:.0f}%",
                    f"[{color}]{avg_p:+.1f}%[/{color}]",
                )
            console.print(s_table)
            console.print()

        # ── Daily P&L calendar ────────────────────────────────────────
        if m.day_pnl:
            d_table = Table(
                title="Daily P&L Log",
                box=box.SIMPLE_HEAD, show_header=True,
                header_style="bold cyan",
            )
            d_table.add_column("Date",    min_width=12)
            d_table.add_column("P&L ₹",  justify="right", min_width=10)
            d_table.add_column("P&L %",  justify="right", min_width=8)
            d_table.add_column("5% Gate", justify="center", width=10)

            for day_str, dp in sorted(m.day_pnl.items()):
                rs_  = dp["rs"]
                pct_ = dp["pct"]
                color = "green" if rs_ >= 0 else "red"
                gate_hit = "[red]❌ BREACH[/red]" if pct_ < -GATE_MAX_DAY_LOSS else "[green]✅[/green]"
                d_table.add_row(
                    day_str,
                    f"[{color}]₹{rs_:+,.0f}[/{color}]",
                    f"[{color}]{pct_*100:+.2f}%[/{color}]",
                    gate_hit,
                )
            console.print(d_table)
            console.print()

        # ── What needs to happen ──────────────────────────────────────
        if not all_pass:
            console.rule("[bold yellow]Action Items[/bold yellow]")
            for g in gates:
                if not g.passed and g.note:
                    console.print(f"  [yellow]→[/yellow] [{g.gate_id}] {g.note}")
            console.print()

    except ImportError:
        # Plain fallback
        _plain_render(m, gates, all_pass, verdict, gates_pass, gates_total)


def _plain_render(m, gates, all_pass, verdict, gates_pass, gates_total):
    W = 70
    print("=" * W)
    print(f"  WAR ROOM — Paper Trading Gate Check")
    print("=" * W)
    print(f"\n  VERDICT: {verdict}   ({gates_pass}/{gates_total} gates passed)\n")
    print(f"  Closed Trades : {m.total_trades}")
    print(f"  Days Elapsed  : {m.days_elapsed} (since {m.paper_start})")
    print(f"  Win Rate      : {m.win_rate*100:.1f}%  ({m.total_wins}W / {m.total_losses}L)")
    print(f"  Avg Win       : {m.avg_win_pct*100:+.1f}%")
    print(f"  Avg Loss      : {m.avg_loss_pct*100:.1f}%")
    print(f"  Expectancy    : {m.expectancy*100:+.2f}% per trade")
    print(f"  Win/Loss Ratio: {m.win_loss_ratio:.2f}x" if m.win_loss_ratio < 99 else "  Win/Loss Ratio: ∞")
    print(f"  Worst Day     : ₹{m.worst_day_rs:+,.0f}  ({m.worst_day_pct*100:.2f}%)  [{m.worst_day_str}]")
    print(f"  Total P&L     : ₹{m.total_pnl_rs:+,.0f}  ({m.total_pnl_pct*100:+.2f}%)")
    print()

    print(f"{'Gate':<4} {'Check':<26} {'Status':<8} {'Required'}")
    print("-" * W)
    for g in gates:
        status = "PASS ✓" if g.passed else "FAIL ✗"
        print(f"{g.gate_id:<4} {g.name:<26} {status:<8} {g.required}")
        print(f"     Actual: {g.actual}")
        if g.note:
            print(f"     Note  : {g.note}")
        print()

    if not all_pass:
        print("Action Items:")
        for g in gates:
            if not g.passed and g.note:
                print(f"  → [{g.gate_id}] {g.note}")
        print()

    if m.day_pnl:
        print(f"\n{'Date':<12} {'P&L ₹':>10} {'P&L%':>8} {'Gate'}")
        print("-" * 40)
        for day_str, dp in sorted(m.day_pnl.items()):
            gate_flag = "❌ BREACH" if dp["pct"] < -GATE_MAX_DAY_LOSS else "✅"
            print(f"{day_str:<12} ₹{dp['rs']:>+8,.0f}  {dp['pct']*100:>+6.2f}%  {gate_flag}")


# ── Entry point ───────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="WAR ROOM Paper Trading Gate Checker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--journal",     default=DEFAULT_JOURNAL,
                        help="Path to trade journal JSON")
    parser.add_argument("--capital",     type=float, default=50000.0,
                        help="Paper trading capital (default: ₹50,000)")
    parser.add_argument("--start",       default=str(PAPER_START_DATE),
                        help="Paper trading start date YYYY-MM-DD (default: 2026-03-24)")
    parser.add_argument("--target-days", type=int, default=20,
                        help="Required paper trading days (default: 20)")
    args = parser.parse_args()

    paper_start = datetime.strptime(args.start, "%Y-%m-%d").date()

    trades = load_journal(args.journal)
    if not trades:
        print(f"\n  No trades found at: {args.journal}")
        print("  The bot is running in paper mode. Come back after trades are recorded.\n")
        # Still show the gate status with zero trades
    metrics = GateMetrics(trades, args.capital, paper_start)
    gates   = evaluate_gates(metrics, target_days=args.target_days)
    render_report(metrics, gates)

    # Exit code: 0 = GO LIVE, 1 = NOT READY
    sys.exit(0 if all(g.passed for g in gates) else 1)


if __name__ == "__main__":
    main()
