# ================================================================
#  dashboard.py — Real-Time Rich Terminal Dashboard
#  Full-screen TUI. Refreshes 2x per second.
# ================================================================

import time
from datetime import datetime
from typing import List, Optional

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.align import Align
from rich import box

import config
from utils import vix_zone, fmt_rs, fmt_pct, is_primary_window, is_dead_zone, is_expiry_day
from strategy_engine import Signal, Strength, Direction

console = Console()


def _pnl_text(pct: float) -> Text:
    s = fmt_pct(pct)
    if pct >= 50:  return Text(s, style="bold green")
    if pct >= 10:  return Text(s, style="green")
    if pct >= 0:   return Text(s, style="dim green")
    if pct > -10:  return Text(s, style="yellow")
    return Text(s, style="bold red")


def _strength_style(s: Strength) -> str:
    return {
        Strength.GODMODE:  "bold magenta",
        Strength.STRONG:   "bold green",
        Strength.MODERATE: "yellow",
        Strength.WEAK:     "dim white",
    }.get(s, "white")


def _vix_style(vix: float) -> str:
    z = vix_zone(vix)
    return {"AVOID": "bold red", "REDUCED": "yellow",
            "IDEAL": "bold green", "ELEVATED": "yellow",
            "EXTREME": "bold red"}.get(z, "white")


class Dashboard:

    def __init__(self, feed, tracker, analyst, signals_ref: list,
                 morning_intel=None):
        self.feed    = feed
        self.tracker = tracker
        self.analyst = analyst
        self.signals = signals_ref
        self.intel   = morning_intel
        self._running = False

    def start(self):
        self._running = True
        with Live(self._build(), refresh_per_second=config.DASHBOARD_REFRESH_HZ,
                  screen=True, console=console) as live:
            while self._running:
                try:
                    live.update(self._build())
                    time.sleep(1 / config.DASHBOARD_REFRESH_HZ)
                except KeyboardInterrupt:
                    break

    def stop(self):
        self._running = False

    def _build(self) -> Layout:
        lay = Layout()
        lay.split_column(
            Layout(name="hdr", size=5),
            Layout(name="body"),
            Layout(name="ftr", size=3),
        )
        lay["body"].split_row(
            Layout(name="left",  ratio=2),
            Layout(name="right", ratio=3),
        )
        lay["left"].split_column(
            Layout(name="market", size=16),
            Layout(name="trades"),
        )
        lay["right"].split_column(
            Layout(name="claude",  size=18),
            Layout(name="signals"),
        )

        lay["hdr"].update(self._header())
        lay["market"].update(self._market())
        lay["trades"].update(self._trades())
        lay["claude"].update(self._claude())
        lay["signals"].update(self._signals_panel())
        lay["ftr"].update(self._footer())
        return lay

    def _header(self) -> Panel:
        now   = datetime.now()
        vix   = self.feed.get_vix()
        stats = self.tracker.get_stats()
        ws    = "🟢 WS" if self.feed.is_connected() else "🔴 WS"
        stop  = "[bold red]⛔ STOP[/]" if stats["stopped"] else "[bold green]✅ ACTIVE[/]"
        mode  = f"[cyan]{config.EXECUTION_MODE}[/]"

        txt = Text()
        txt.append("⚡ WAR ROOM  ", style="bold red")
        txt.append(f"{now.strftime('%a %d %b %H:%M:%S')}  ", style="bold white")
        txt.append(f"VIX: {vix:.1f} {vix_zone(vix)}  ", style=_vix_style(vix))
        txt.append(f"PCR BN:{self.feed.get_pcr('BANKNIFTY'):.2f}  ", style="cyan")
        txt.append(f"Trades:{stats['daily_count']}/{config.MAX_DAILY_TRADES}  ")
        pnl_c = "green" if stats["daily_pnl_rs"] >= 0 else "red"
        txt.append(f"P&L: {fmt_rs(stats['daily_pnl_rs'])} ({stats['daily_pnl_pct']:+.1f}%)  ",
                   style=pnl_c)
        txt.append(f"W:{stats['wins']} L:{stats['losses']}  ")
        txt.append(f"{ws}  ")
        txt.append(f"{mode}  ")
        txt.append(stop)
        return Panel(Align.center(txt), style="bold", box=box.HEAVY_EDGE)

    def _market(self) -> Panel:
        bt  = self.feed.get_tick("BANKNIFTY")
        nt  = self.feed.get_tick("NIFTY")
        vix = self.feed.get_vix()

        tbl = Table(show_header=True, header_style="bold cyan",
                    box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("",      width=12, style="bold")
        tbl.add_column("LTP",   justify="right", width=9)
        tbl.add_column("Chg%",  justify="right", width=8)
        tbl.add_column("High",  justify="right", width=9)
        tbl.add_column("Low",   justify="right", width=9)
        tbl.add_column("Range", justify="right", width=7)

        for name, tick in [("BANKNIFTY", bt), ("NIFTY 50", nt)]:
            if tick:
                chg = tick.change_pct
                rng = tick.high - tick.low
                cs  = "green" if chg >= 0 else "red"
                tbl.add_row(name, f"{tick.ltp:,.0f}",
                            Text(f"{chg:+.2f}%", style=cs),
                            f"{tick.high:,.0f}", f"{tick.low:,.0f}", f"{rng:.0f}")
            else:
                tbl.add_row(name, "—", "—", "—", "—", "—")

        tbl.add_row("", "", "", "", "", "")
        bn_res = config.KEY_LEVELS["BANKNIFTY"]["resistance"]
        bn_sup = config.KEY_LEVELS["BANKNIFTY"]["support"]
        tbl.add_row("[red]BN Resist[/]",
                    str(int(bn_res[0])), str(int(bn_res[1])), str(int(bn_res[2])), "", "")
        tbl.add_row("[green]BN Support[/]",
                    str(int(bn_sup[0])), str(int(bn_sup[1])), str(int(bn_sup[2])), "", "")
        tbl.add_row(
            f"VIX: [bold]{vix:.1f}[/]",
            Text(vix_zone(vix), style=_vix_style(vix)),
            "MaxPain",
            f"{self.feed.get_max_pain('BANKNIFTY'):.0f}",
            f"{self.feed.get_max_pain('NIFTY'):.0f}", ""
        )
        return Panel(tbl, title="[bold cyan]LIVE MARKET[/]", border_style="cyan")

    def _trades(self) -> Panel:
        trades = self.tracker.get_open_trades()
        if not trades:
            return Panel(Align.center(Text("No open trades", style="dim")),
                         title="[bold yellow]OPEN TRADES[/]", border_style="yellow")

        tbl = Table(show_header=True, header_style="bold yellow",
                    box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("ID",    width=8)
        tbl.add_column("Trade", width=18)
        tbl.add_column("Entry", justify="right", width=7)
        tbl.add_column("CMP",   justify="right", width=7)
        tbl.add_column("P&L",   justify="right", width=9)
        tbl.add_column("SL",    justify="right", width=6)
        tbl.add_column("Exit@", width=6)
        tbl.add_column("Strat", width=14)

        for t in trades:
            pct = t.pnl_pct * 100
            tbl.add_row(
                t.id,
                f"{t.symbol[:2]} {t.strike}{t.option_type} {t.expiry}",
                f"{t.entry_premium:.0f}",
                f"{t.current_premium:.0f}",
                _pnl_text(pct),
                f"{t.sl_pct*100:.0f}%",
                t.exit_by,
                t.strategy[:14],
            )
        return Panel(tbl, title="[bold yellow]OPEN TRADES[/]", border_style="yellow")

    def _claude(self) -> Panel:
        resp = self.analyst.get_last()
        if not resp:
            return Panel(
                Align.center(Text("Waiting for Claude… (every 2 min)", style="dim")),
                title="[bold magenta]⚡ CLAUDE DECISION[/]", border_style="magenta"
            )

        action = resp.get("action", "WAIT")
        conf   = resp.get("confidence", "")
        reason = resp.get("reasoning", "")
        risk   = resp.get("key_risk", "")
        mtype  = resp.get("market_type", "")
        vstat  = resp.get("vix_status", "")
        ts     = resp.get("_ts", "")
        lat    = resp.get("_latency", 0)

        astyle = {"BUY": "bold green", "WAIT": "yellow", "SKIP": "bold red",
                  "EXIT": "orange1", "STOP_TRADING": "bold red"}.get(action, "white")

        tbl = Table(show_header=False, box=box.SIMPLE, expand=True)
        tbl.add_column("k", style="dim", width=16)
        tbl.add_column("v")

        tbl.add_row("Action",     Text(f"▶ {action}", style=astyle))
        tbl.add_row("Confidence", Text(conf, style="bold magenta" if conf == "GODMODE" else "green"))
        tbl.add_row("Market",     mtype)
        tbl.add_row("VIX Zone",   Text(vstat, style="green" if vstat == "IDEAL" else "yellow"))

        if action == "BUY":
            inst   = resp.get("instrument", "")
            strike = resp.get("strike", 0)
            otype  = resp.get("option_type", "")
            entry  = resp.get("entry_at_index", 0)
            sl_p   = resp.get("sl_premium_pct", 0)
            t1     = resp.get("t1_premium_pct", 0)
            t2     = resp.get("t2_premium_pct", 0)
            size   = resp.get("position_size_pct", 0)
            strat  = resp.get("strategy", "")
            tier   = resp.get("tier", "")
            tbl.add_row("", "")
            tbl.add_row("Instrument", Text(f"★ {inst} {strike} {otype}", style="bold green"))
            tbl.add_row("Entry@Index", f"{entry}")
            tbl.add_row("SL / T1 / T2", f"{sl_p}% / {t1}% / {t2}%")
            tbl.add_row("Size",         f"{size}% capital")
            tbl.add_row("Strategy",     f"{strat} (Tier {tier})")

        tbl.add_row("", "")
        tbl.add_row("Reason",  Text(reason[:100], style="italic"))
        tbl.add_row("Risk",    Text(risk[:80], style="dim red"))
        tbl.add_row("Updated", f"{ts}  [{lat}ms]")

        return Panel(tbl, title="[bold magenta]⚡ CLAUDE DECISION[/]", border_style="magenta")

    def _signals_panel(self) -> Panel:
        recent = self.signals[-10:] if self.signals else []
        if not recent:
            return Panel(Align.center(Text("No signals yet…", style="dim")),
                         title="[bold green]STRATEGY SIGNALS[/]", border_style="green")

        tbl = Table(show_header=True, header_style="bold green",
                    box=box.SIMPLE_HEAVY, expand=True)
        tbl.add_column("Time",     width=6)
        tbl.add_column("Strategy", width=18)
        tbl.add_column("Sym",      width=6)
        tbl.add_column("Dir",      width=5)
        tbl.add_column("Strike",   justify="right", width=7)
        tbl.add_column("Str",      width=10)
        tbl.add_column("Reason",   ratio=1)

        for sig in reversed(recent):
            d  = sig.direction.value
            ds = "green" if "CE" in d else ("red" if "PE" in d else "magenta")
            tbl.add_row(
                sig.ts.strftime("%H:%M"),
                sig.strategy[:18],
                sig.symbol[:6],
                Text(d, style=ds),
                str(sig.strike),
                Text(sig.strength.name, style=_strength_style(sig.strength)),
                sig.reason[:55],
            )
        return Panel(tbl, title="[bold green]STRATEGY ENGINE[/]", border_style="green")

    def _footer(self) -> Panel:
        now = datetime.now()
        hm  = now.hour * 60 + now.minute

        if hm < 9*60+20:     zone = "[yellow]PRE-MARKET — OBSERVE ONLY[/]"
        elif hm <= 11*60:    zone = "[bold green]✅ PRIMARY ENTRY WINDOW[/]"
        elif hm <= 11*60+30: zone = "[yellow]LATE PRIMARY — SELECTIVE[/]"
        elif hm <= 13*60+30: zone = "[bold red]🔴 DEAD ZONE — NO ENTRIES[/]"
        elif hm <= 14*60:    zone = "[yellow]SECONDARY WINDOW[/]"
        else:                zone = "[bold red]⏰ EXIT ONLY — CLOSE ALL[/]"

        expiry_note = ""
        if is_expiry_day():
            expiry_note = f" [bold yellow]⚡ EXPIRY DAY — TRADE NORMALLY, EXIT {config.EXPIRY_EXIT_TIME}[/]"

        txt = Text()
        txt.append(f"  {zone}{expiry_note}   ")
        txt.append(" [q]Quit  [c]Force Claude  [r]Reset Day  [h]Help  ", style="dim")
        return Panel(txt, box=box.SIMPLE)
