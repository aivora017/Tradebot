#!/usr/bin/env python3
# ================================================================
#  paper_report.py — 2-Week Paper Trading Performance Report
#
#  Run anytime:  python paper_report.py
#  Run for date: python paper_report.py --from 2026-03-24 --to 2026-04-07
#
#  Reads data/trade_journal.json and prints a full scorecard:
#  - Per-day P&L
#  - Cumulative P&L curve vs monthly target
#  - Win rate, avg RR, best/worst trade
#  - Per-strategy breakdown
#  - Claude confidence breakdown
#  - Each trade with full notes (Claude reasoning)
# ================================================================

import json
import sys
import argparse
from datetime import datetime, date
from pathlib import Path
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────
JOURNAL_FILE    = "data/trade_journal.json"
BASE_CAPITAL    = 50_000.0
MONTHLY_TARGET  = 0.60      # 60% minimum
MONTHLY_STRETCH = 1.20      # 120% stretch

# ANSI colors
GRN  = "\033[92m"
RED  = "\033[91m"
YLW  = "\033[93m"
BLU  = "\033[94m"
CYN  = "\033[96m"
WHT  = "\033[97m"
DIM  = "\033[2m"
BLD  = "\033[1m"
RST  = "\033[0m"


def color_pnl(val: float, text: str = None) -> str:
    t = text or (f"₹{val:+,.0f}" if abs(val) >= 1 else f"{val:+.1f}%")
    return f"{GRN}{t}{RST}" if val >= 0 else f"{RED}{t}{RST}"


def bar(pct: float, width: int = 30) -> str:
    filled = int(min(pct / 100, 1.0) * width)
    return f"[{GRN}{'█' * filled}{DIM}{'░' * (width - filled)}{RST}]"


def load_journal(from_date: date = None, to_date: date = None):
    p = Path(JOURNAL_FILE)
    if not p.exists():
        print(f"{RED}No journal found at {JOURNAL_FILE}. Run the bot first.{RST}")
        sys.exit(0)
    with open(p) as f:
        raw = json.load(f)

    trades = []
    for d in raw:
        try:
            d["entry_time"] = datetime.fromisoformat(d["entry_time"])
            if d.get("exit_time"):
                d["exit_time"] = datetime.fromisoformat(d["exit_time"])
            trades.append(d)
        except Exception:
            continue

    # Filter by date range
    if from_date:
        trades = [t for t in trades if t["entry_time"].date() >= from_date]
    if to_date:
        trades = [t for t in trades if t["entry_time"].date() <= to_date]
    return trades


def print_header(trades, from_date, to_date):
    print()
    print(f"{BLD}{CYN}{'='*68}{RST}")
    print(f"{BLD}{CYN}   ⚡  WAR ROOM — 2-WEEK PAPER TRADING REPORT{RST}")
    if from_date or to_date:
        fd = str(from_date) if from_date else "start"
        td = str(to_date)   if to_date   else "today"
        print(f"{DIM}   Period: {fd} → {td}{RST}")
    print(f"{DIM}   Base Capital: ₹{BASE_CAPITAL:,.0f} | "
          f"Target: +{MONTHLY_TARGET*100:.0f}% | Stretch: +{MONTHLY_STRETCH*100:.0f}%{RST}")
    print(f"{BLD}{CYN}{'='*68}{RST}")
    print()


def overall_stats(trades):
    closed = [t for t in trades if not t.get("is_open", True)]
    if not closed:
        print(f"{YLW}No closed trades in this period.{RST}\n")
        return

    total_pnl  = sum(
        (t["exit_premium"] - t["entry_premium"]) * t["qty"]
        for t in closed if t["entry_premium"] > 0
    )
    wins   = [t for t in closed if t["exit_premium"] > t["entry_premium"]]
    losses = [t for t in closed if t["exit_premium"] <= t["entry_premium"]]
    n      = len(closed)
    win_rate = len(wins) / n * 100 if n > 0 else 0

    avg_win  = sum((t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
                   for t in wins)   / len(wins)   if wins   else 0
    avg_loss = sum((t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
                   for t in losses) / len(losses) if losses else 0
    rrr      = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    capital_now = BASE_CAPITAL + total_pnl
    return_pct  = (total_pnl / BASE_CAPITAL) * 100
    target_rs   = BASE_CAPITAL * MONTHLY_TARGET
    remaining   = target_rs - total_pnl
    progress    = (total_pnl / target_rs) * 100 if target_rs > 0 else 0

    best  = max(closed, key=lambda t: t["exit_premium"] - t["entry_premium"])
    worst = min(closed, key=lambda t: t["exit_premium"] - t["entry_premium"])

    print(f"{BLD}{WHT}── OVERALL SUMMARY ─────────────────────────────────────────{RST}")
    print(f"  Total Trades : {BLD}{n}{RST}  |  Wins: {GRN}{len(wins)}{RST}  |  Losses: {RED}{len(losses)}{RST}")
    print(f"  Win Rate     : {BLD}{win_rate:.1f}%{RST}")
    print(f"  Avg Win      : {GRN}+{avg_win:.1f}%{RST}  |  Avg Loss: {RED}{avg_loss:.1f}%{RST}")
    print(f"  Reward:Risk  : {BLD}{rrr:.2f}{RST}")
    print(f"  Total P&L    : {color_pnl(total_pnl)} ({color_pnl(return_pct, f'{return_pct:+.1f}%')})")
    print(f"  Capital Now  : {BLD}₹{capital_now:,.0f}{RST}")
    print()
    print(f"  Monthly Goal : ₹{target_rs:,.0f} (+{MONTHLY_TARGET*100:.0f}%)")
    prog_pct = min(progress, 100)
    print(f"  Progress     : {bar(prog_pct)} {progress:.1f}%")
    if remaining > 0:
        print(f"  Remaining    : {YLW}₹{remaining:,.0f} to reach target{RST}")
    else:
        print(f"  Status       : {GRN}🎯 TARGET REACHED! (₹{abs(remaining):,.0f} above target){RST}")
    print()

    def fmt_trade(t):
        pnl_pct = (t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
        return (f"{t['symbol']} {t['strike']}{t['option_type']} | "
                f"Entry ₹{t['entry_premium']:.0f} → Exit ₹{t['exit_premium']:.0f} | "
                f"{pnl_pct:+.1f}% | {t['strategy']} | {t['exit_reason']}")

    best_pct  = (best["exit_premium"]  - best["entry_premium"])  / best["entry_premium"]  * 100
    worst_pct = (worst["exit_premium"] - worst["entry_premium"]) / worst["entry_premium"] * 100
    print(f"  {GRN}Best Trade   :{RST} {fmt_trade(best)}")
    print(f"  {RED}Worst Trade  :{RST} {fmt_trade(worst)}")
    print()
    return total_pnl, closed


def daily_breakdown(trades):
    closed = [t for t in trades if not t.get("is_open", True)]
    if not closed:
        return

    by_day = defaultdict(list)
    for t in closed:
        d = t["entry_time"].date()
        by_day[d].append(t)

    print(f"{BLD}{WHT}── DAILY BREAKDOWN ─────────────────────────────────────────{RST}")
    cumulative = 0.0
    print(f"  {'Date':<12} {'Trades':>6} {'W':>4} {'L':>4} {'Daily P&L':>12} {'Cumul P&L':>12} {'%':>7}")
    print(f"  {'-'*12} {'-'*6} {'-'*4} {'-'*4} {'-'*12} {'-'*12} {'-'*7}")

    for day in sorted(by_day):
        day_trades = by_day[day]
        day_pnl = sum(
            (t["exit_premium"] - t["entry_premium"]) * t["qty"]
            for t in day_trades if t["entry_premium"] > 0
        )
        wins   = sum(1 for t in day_trades if t["exit_premium"] > t["entry_premium"])
        losses = len(day_trades) - wins
        cumulative += day_pnl
        cum_pct = (cumulative / BASE_CAPITAL) * 100
        day_color = GRN if day_pnl >= 0 else RED

        print(
            f"  {day.strftime('%d %b %Y'):<12} "
            f"{len(day_trades):>6} "
            f"{GRN}{wins:>4}{RST} "
            f"{RED}{losses:>4}{RST} "
            f"{day_color}₹{day_pnl:>+9,.0f}{RST} "
            f"{color_pnl(cumulative, f'₹{cumulative:>+9,.0f}')} "
            f"{color_pnl(cum_pct, f'{cum_pct:>+5.1f}%')}"
        )
    print()


def strategy_breakdown(trades):
    closed = [t for t in trades if not t.get("is_open", True)]
    if not closed:
        return

    by_strategy = defaultdict(list)
    for t in closed:
        by_strategy[t.get("strategy", "Unknown")].append(t)

    print(f"{BLD}{WHT}── BY STRATEGY ─────────────────────────────────────────────{RST}")
    print(f"  {'Strategy':<22} {'N':>4} {'Win%':>6} {'Avg%':>8} {'Total P&L':>12}")
    print(f"  {'-'*22} {'-'*4} {'-'*6} {'-'*8} {'-'*12}")

    rows = []
    for strat, ts in by_strategy.items():
        n      = len(ts)
        wins   = sum(1 for t in ts if t["exit_premium"] > t["entry_premium"])
        avg_pct = sum((t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
                      for t in ts if t["entry_premium"] > 0) / n
        pnl    = sum((t["exit_premium"] - t["entry_premium"]) * t["qty"] for t in ts)
        rows.append((strat, n, wins/n*100, avg_pct, pnl))

    for strat, n, wr, avg_pct, pnl in sorted(rows, key=lambda x: -x[4]):
        avg_color = GRN if avg_pct >= 0 else RED
        print(
            f"  {strat:<22} {n:>4} "
            f"{GRN if wr >= 50 else RED}{wr:>5.0f}%{RST} "
            f"{avg_color}{avg_pct:>+7.1f}%{RST} "
            f"{color_pnl(pnl, f'₹{pnl:>+9,.0f}')}"
        )
    print()


def confidence_breakdown(trades):
    closed = [t for t in trades if not t.get("is_open", True)]
    if not closed:
        return

    # Parse confidence from notes field
    by_conf = defaultdict(list)
    for t in closed:
        notes = t.get("notes", "")
        conf  = "UNKNOWN"
        for c in ["GODMODE", "HIGH", "MEDIUM", "LOW"]:
            if c in notes:
                conf = c
                break
        by_conf[conf].append(t)

    print(f"{BLD}{WHT}── BY CONFIDENCE LEVEL ─────────────────────────────────────{RST}")
    print(f"  {'Confidence':<12} {'N':>4} {'Win%':>6} {'Avg%':>8} {'Total P&L':>12}")
    print(f"  {'-'*12} {'-'*4} {'-'*6} {'-'*8} {'-'*12}")

    for conf in ["GODMODE", "HIGH", "MEDIUM", "LOW", "UNKNOWN"]:
        ts = by_conf.get(conf, [])
        if not ts:
            continue
        n      = len(ts)
        wins   = sum(1 for t in ts if t["exit_premium"] > t["entry_premium"])
        avg_pct = sum((t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
                      for t in ts if t["entry_premium"] > 0) / n
        pnl    = sum((t["exit_premium"] - t["entry_premium"]) * t["qty"] for t in ts)
        conf_colors = {"GODMODE": GRN+BLD, "HIGH": GRN, "MEDIUM": YLW, "LOW": RED, "UNKNOWN": DIM}
        cc = conf_colors.get(conf, "")
        print(
            f"  {cc}{conf:<12}{RST} {n:>4} "
            f"{GRN if wins/n*100 >= 50 else RED}{wins/n*100:>5.0f}%{RST} "
            f"{GRN if avg_pct >= 0 else RED}{avg_pct:>+7.1f}%{RST} "
            f"{color_pnl(pnl, f'₹{pnl:>+9,.0f}')}"
        )
    print()


def index_breakdown(trades):
    closed = [t for t in trades if not t.get("is_open", True)]
    if not closed:
        return

    by_idx = defaultdict(list)
    for t in closed:
        by_idx[t.get("symbol", "?")].append(t)

    print(f"{BLD}{WHT}── BY INDEX ────────────────────────────────────────────────{RST}")
    for idx, ts in sorted(by_idx.items()):
        n      = len(ts)
        wins   = sum(1 for t in ts if t["exit_premium"] > t["entry_premium"])
        pnl    = sum((t["exit_premium"] - t["entry_premium"]) * t["qty"] for t in ts)
        avg_pct = sum((t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100
                      for t in ts if t["entry_premium"] > 0) / n
        print(
            f"  {BLD}{idx:<12}{RST} {n} trades | "
            f"Win: {GRN}{wins}{RST}/{n} | "
            f"Avg: {GRN if avg_pct >= 0 else RED}{avg_pct:+.1f}%{RST} | "
            f"P&L: {color_pnl(pnl)}"
        )
    print()


def trade_log(trades, show_notes=True):
    closed = sorted(
        [t for t in trades if not t.get("is_open", True)],
        key=lambda t: t["entry_time"]
    )
    open_t = [t for t in trades if t.get("is_open", True)]

    print(f"{BLD}{WHT}── TRADE LOG ({len(closed)} closed, {len(open_t)} open) ───────────────────{RST}")
    for i, t in enumerate(closed, 1):
        pnl_pct = (t["exit_premium"] - t["entry_premium"]) / t["entry_premium"] * 100 \
                  if t["entry_premium"] > 0 else 0
        pnl_rs  = (t["exit_premium"] - t["entry_premium"]) * t["qty"]
        icon    = "✅" if pnl_pct >= 0 else "❌"
        dt      = t["entry_time"].strftime("%d %b %H:%M")
        dur_min = int((t["exit_time"] - t["entry_time"]).total_seconds() / 60) \
                  if t.get("exit_time") else 0
        print(
            f"\n  {icon} {BLD}#{i} [{t['id']}]{RST} "
            f"{dt} | {t['symbol']} {t['strike']}{t['option_type']} | "
            f"{DIM}{t['strategy']}{RST}"
        )
        print(
            f"     Entry ₹{t['entry_premium']:.0f} → Exit ₹{t['exit_premium']:.0f} | "
            f"Qty {t['qty']} | {dur_min}m | "
            f"{color_pnl(pnl_pct, f'{pnl_pct:+.1f}%')} | "
            f"{color_pnl(pnl_rs)} | "
            f"{DIM}{t.get('exit_reason','?')}{RST}"
        )
        if show_notes and t.get("notes"):
            note = t["notes"][:200] + ("…" if len(t["notes"]) > 200 else "")
            print(f"     {DIM}Claude: {note}{RST}")

    if open_t:
        print(f"\n  {YLW}⏳ OPEN POSITIONS ({len(open_t)}){RST}")
        for t in open_t:
            unreal = (t["current_premium"] - t["entry_premium"]) * t["qty"]
            pnl_pct = (t["current_premium"] - t["entry_premium"]) / t["entry_premium"] * 100 \
                      if t["entry_premium"] > 0 else 0
            print(
                f"     [{t['id']}] {t['symbol']} {t['strike']}{t['option_type']} | "
                f"Entry ₹{t['entry_premium']:.0f} | "
                f"Unrealised: {color_pnl(pnl_pct, f'{pnl_pct:+.1f}%')} {color_pnl(unreal)}"
            )
    print()


def main():
    parser = argparse.ArgumentParser(description="WAR ROOM Paper Trading Report")
    parser.add_argument("--from", dest="from_date", default=None,
                        help="Start date YYYY-MM-DD")
    parser.add_argument("--to",   dest="to_date",   default=None,
                        help="End date YYYY-MM-DD")
    parser.add_argument("--no-notes", action="store_true",
                        help="Hide Claude reasoning notes in trade log")
    args = parser.parse_args()

    from_date = date.fromisoformat(args.from_date) if args.from_date else None
    to_date   = date.fromisoformat(args.to_date)   if args.to_date   else None

    trades = load_journal(from_date, to_date)

    print_header(trades, from_date, to_date)
    overall_stats(trades)
    daily_breakdown(trades)
    strategy_breakdown(trades)
    confidence_breakdown(trades)
    index_breakdown(trades)
    trade_log(trades, show_notes=not args.no_notes)

    print(f"{BLD}{CYN}{'='*68}{RST}")
    print(f"{DIM}Run with --from YYYY-MM-DD --to YYYY-MM-DD to filter by period{RST}")
    print(f"{DIM}Run with --no-notes to hide Claude reasoning{RST}")
    print()


if __name__ == "__main__":
    main()
