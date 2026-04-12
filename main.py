# ================================================================
#  main.py — Master Orchestrator
#  WAR ROOM | Nifty & BankNifty Option Buying Bot
#
#  PAPER MODE by default — set EXECUTION_MODE=LIVE in .env only
#  after paper trading validation.
#
#  API Bridge (ngrok) integrated — starts automatically on port 5003.
#  Run: ngrok http 5003 → paste URL into warroom_live.html
# ================================================================

import sys
import time
import threading
from datetime import datetime
from typing import List, Optional

import config
from logger_setup import setup_logger, get_logger
from data_feed import MarketDataFeed, Tick
from strategy_engine import StrategyEngine, Signal, Direction
from trade_tracker import TradeTracker
from risk_manager import RiskManager
from order_manager import OrderManager
from claude_analyst import ClaudeAnalyst
from notifications import Notifier
from market_scanner import MorningScanner
from news_feed import NewsFeed
from api_bridge import APIBridge
from utils import is_market_open, is_hard_exit_time, lot_size, next_expiry

setup_logger()
log = get_logger("Main")


class WARRoom:
    """
    Full bot. DataFeed → StrategyEngine → [ClaudeAnalyst + RiskManager]
    → OrderManager → TradeTracker → Telegram + APIBridge (browser dashboard)
    """

    def __init__(self):
        # ── Load compounded capital (yesterday's closing capital) ─────
        from utils import load_compounded_capital
        compounded = load_compounded_capital()
        if compounded != config.TOTAL_CAPITAL:
            log.info(f"📈 Compounded capital loaded: ₹{compounded:,.0f} "
                     f"(base ₹{config.TOTAL_CAPITAL:,.0f})")
            config.TOTAL_CAPITAL = compounded

        log.info("=" * 70)
        log.info("  ⚡  WAR ROOM BOT — NIFTY / BANK NIFTY OPTION BUYER")
        log.info(f"  Mode: {config.EXECUTION_MODE} | Capital: ₹{config.TOTAL_CAPITAL:,.0f}")
        log.info(f"  Lot sizes: NIFTY={config.LOT_SIZES['NIFTY']} BN={config.LOT_SIZES['BANKNIFTY']}")
        log.info("=" * 70)

        self.feed       = MarketDataFeed()
        self.tracker    = TradeTracker()
        self.risk       = RiskManager(self.tracker)
        self.orders     = OrderManager()
        self.engine     = StrategyEngine(self.feed)
        self.news       = NewsFeed()
        self.analyst    = ClaudeAnalyst(self.feed, self.tracker, self.news)
        self.notify     = Notifier()
        self.scanner    = MorningScanner()
        self._signals:  List[Signal] = []
        self._lock      = threading.Lock()
        self._running   = False
        self._morning_intel = None

        self.api_bridge = APIBridge(self)

        from dashboard import Dashboard
        self.dashboard  = Dashboard(
            self.feed, self.tracker, self.analyst,
            self._signals, self._morning_intel
        )

    # ── Start ─────────────────────────────────────────────────────

    def start(self):
        self.tracker.reset_day()
        self.engine.reset_day()

        self.feed.on_tick(self._on_tick)
        self.analyst.on_decision(self._on_claude_decision)

        # Morning scan with news
        try:
            self._morning_intel = self.scanner.run(news_feed=self.news)
            self.notify.morning_plan(
                nifty=0, bn=0, vix=0,
                pcr=self._morning_intel.pcr_bn,
                market_type=self._morning_intel.market_type,
                top_strategy=self._morning_intel.recommended_strategy,
            )
        except Exception as e:
            log.error(f"Morning scan error: {e}")

        # Start feeds and services
        self.news.start()
        self.feed.start()
        time.sleep(2)
        self.analyst.start()
        self.api_bridge.start()   # Browser dashboard via ngrok
        self._running = True

        threading.Thread(target=self._alert_loop,    daemon=True).start()
        threading.Thread(target=self._keyboard_loop, daemon=True).start()
        threading.Thread(target=self._eod_monitor,   daemon=True).start()

        self.notify.test()
        log.info("All systems started. API Bridge on port 5003.")
        log.info("Run: ngrok http 5003 → paste URL into warroom_live.html")
        time.sleep(1)

        try:
            self.dashboard.start()
        except KeyboardInterrupt:
            pass

        self.stop()

    def stop(self):
        log.info("Shutting down WAR ROOM…")
        self._running = False
        self.feed.stop()
        self.analyst.stop()
        self.news.stop()
        self.dashboard.stop()

        stats = self.tracker.get_stats()
        self.notify.daily_summary(
            stats["daily_pnl_rs"], stats["daily_pnl_pct"],
            stats["wins"], stats["losses"], stats["daily_count"]
        )

        # ── Bot self-evolution: EOD pattern analysis ──────────────────
        try:
            from bot_memory import get_memory
            get_memory().run_eod_analysis()
        except Exception as _eod_err:
            log.warning(f"EOD analysis skipped: {_eod_err}")

        # ── Save closing capital for next session compounding ─────────
        from utils import save_closing_capital
        closing_capital = config.TOTAL_CAPITAL + stats["daily_pnl_rs"]
        save_closing_capital(closing_capital)
        log.info(f"💾 Closing capital saved: ₹{closing_capital:,.0f} "
                 f"(today's P&L: ₹{stats['daily_pnl_rs']:+.0f})")

        log.info("=" * 70)
        log.info(f"DAY SUMMARY: P&L ₹{stats['daily_pnl_rs']:+.0f} "
                 f"({stats['daily_pnl_pct']:+.1f}%) | "
                 f"W:{stats['wins']} L:{stats['losses']}")
        log.info("=" * 70)

    # ── Tick Handler ──────────────────────────────────────────────

    def _on_tick(self, tick: Tick):
        try:
            if not is_market_open():
                return
            self._update_trade_premiums(tick)
            new_signals = self.engine.evaluate(tick)
            if new_signals:
                with self._lock:
                    self._signals.extend(new_signals)
                    if len(self._signals) > 60:
                        self._signals = self._signals[-60:]
                for sig in new_signals:
                    self.analyst.add_signal(sig)
                    if sig.strength.value >= 3:
                        self.notify.signal(
                            symbol=sig.symbol, strike=sig.strike,
                            otype=sig.option_type, direction=sig.direction.value,
                            strategy=sig.strategy, strength=sig.strength.name,
                            entry=sig.entry_trigger, sl_pct=sig.sl_premium_pct*100,
                            t1=sig.t1_pct*100, t2=sig.t2_pct*100,
                            reason=sig.reason, exit_by=sig.exit_by,
                        )
        except Exception as e:
            log.error(f"Tick handler error: {e}")

    def _update_trade_premiums(self, tick: Tick):
        """
        FIX (BUG-6): Use REAL option LTP from chain data for ALL SL/T1/T2 decisions.

        Previous version used a delta × index_move approximation which was inaccurate
        and caused wrong SL triggers / missed T1/T2 exits in live mode.

        Priority order:
          1. Real LTP from option chain snapshot (chain_ltp) — updated every ~30s
          2. Real LTP via REST fallback if chain is stale (> 60s old)
          3. Delta approximation ONLY if no chain data at all (pre-market)
        """
        from utils import normalize_symbol
        sym = normalize_symbol(tick.symbol)
        if not sym:
            return
        for trade in self.tracker.get_open_trades():
            if trade.symbol != sym:
                continue

            # ── Try real LTP from chain snapshot first ──────────────────
            real_ltp = self.feed.get_option_ltp(sym, trade.strike, trade.option_type)
            if real_ltp > 0:
                self.tracker.update_premium(trade.id, real_ltp)
                log.debug(f"Premium [{trade.id}] {sym} {trade.strike}{trade.option_type}: "
                          f"real={real_ltp:.1f}")
                continue

            # ── Fallback: delta approximation (pre-market / chain not loaded) ──
            idx_move = tick.ltp - trade.entry_index
            delta    = 0.5
            otype    = trade.option_type
            if otype == "CE+PE":
                approx_prem = max(1.0, trade.entry_premium + delta * abs(idx_move) * 0.5)
            else:
                directional = (
                    1 if (otype == "CE" and idx_move > 0) or (otype == "PE" and idx_move < 0)
                    else -0.8
                )
                approx_prem = max(1.0, trade.entry_premium + delta * abs(idx_move) * directional)
            self.tracker.update_premium(trade.id, approx_prem)
            log.debug(f"Premium [{trade.id}] {sym} {trade.strike}{trade.option_type}: "
                      f"approx={approx_prem:.1f} (chain not available)")

    # ── Claude Decision Handler ────────────────────────────────────

    def _on_claude_decision(self, decision: dict):
        action = decision.get("action", "WAIT")
        conf   = decision.get("confidence", "LOW")
        reason = decision.get("reasoning", "")

        self.notify.claude_decision(
            action=action,
            symbol=decision.get("instrument", ""),
            strike=decision.get("strike", 0),
            otype=decision.get("option_type", ""),
            confidence=conf,
            reasoning=reason,
        )

        if action == "BUY":
            self._handle_claude_buy(decision)
        elif action == "EXIT":
            self._handle_claude_exit(decision)
        elif action == "STOP_TRADING":
            log.warning("⛔ Claude: STOP TRADING")
            self.notify.stop_trading_alert(reason)
        elif action == "WAIT":
            log.info(f"⏳ Claude: WAIT | {reason[:80]}")
        elif action == "SKIP":
            log.info(f"⏭️  Claude: SKIP | {reason[:80]}")

    def _handle_claude_buy(self, decision: dict):
        symbol   = decision.get("instrument", "BANKNIFTY")
        strike   = int(decision.get("strike", 0))
        otype    = decision.get("option_type", "CE")
        tier     = int(decision.get("tier", 2))
        conf     = decision.get("confidence", "MEDIUM")   # must read here — not from caller scope
        sl_p     = float(decision.get("sl_premium_pct", 20)) / 100
        t1       = float(decision.get("t1_premium_pct", 50)) / 100
        t2       = float(decision.get("t2_premium_pct", 100)) / 100
        size_pct = float(decision.get("position_size_pct", 5)) / 100
        exit_by  = decision.get("exit_by", config.HARD_EXIT_TIME)
        vix      = self.feed.get_vix()

        log.info(
            f"\n{'='*60}\n"
            f"🟢 CLAUDE BUY: {symbol} {strike} {otype}\n"
            f"   Strategy: {decision.get('strategy')} | Tier {tier}\n"
            f"   Confidence: {decision.get('confidence')}\n"
            f"   Reason: {decision.get('reasoning')}\n"
            f"   SL: {sl_p*100:.0f}% | T1: {t1*100:.0f}% | T2: {t2*100:.0f}%\n"
            f"{'='*60}"
        )

        allowed, reason = self.risk.check(symbol, tier, size_pct, vix)
        if not allowed:
            log.warning(f"🚫 Claude BUY blocked by risk: {reason}")
            return

        # Map Claude's confidence to strength-based capital allocation
        _conf_to_strength = {
            "GODMODE": "GODMODE",
            "HIGH":    "STRONG",
            "MEDIUM":  "MODERATE",
            "LOW":     "WEAK",
        }
        strength = _conf_to_strength.get(conf.upper(), "MODERATE")

        chain   = self.feed.get_chain(symbol)
        premium = 0.0
        # ── FIX (BUG-2): Get REAL instrument_key from chain — never construct it ──
        real_instrument_key = self.feed.get_instrument_key_for_strike(symbol, strike, otype)
        for row in chain:
            if row.strike == strike:
                if otype == "CE+PE":
                    premium = (row.ce_ltp or 0) + (row.pe_ltp or 0)   # straddle: both legs
                elif "CE" in otype:
                    premium = row.ce_ltp or 0
                else:
                    premium = row.pe_ltp or 0
                break
        if premium == 0:
            premium = 200.0
            log.warning(f"Using fallback premium ₹{premium}")

        if config.EXECUTION_MODE == "LIVE" and not real_instrument_key:
            log.warning(f"⚠️ instrument_key not in chain for {symbol} {strike} {otype} — "
                        f"chain may not be loaded yet. Aborting trade.")
            return

        cap, lots = self.risk.position_size(tier, vix, premium, symbol, strength)

        if config.EXECUTION_MODE in ("LIVE", "PAPER"):
            _, expiry_str = next_expiry(symbol)
            result = self.orders.buy_option(
                symbol=symbol, strike=strike, option_type=otype,
                expiry_str=expiry_str, lots=lots, order_type="MARKET",
                limit_price=premium,          # pass real premium so paper fills at correct price
                instrument_key=real_instrument_key,  # FIX: from chain, not constructed
            )
            if result.success:
                fill_price = result.price if result.price > 0 else premium
                # Build trade notes with Claude's full reasoning for journal review
                trade_notes = (
                    f"[{decision.get('confidence','?')}] "
                    f"{decision.get('reasoning', '')} | "
                    f"Mkt:{decision.get('market_type','?')} | "
                    f"VIX:{decision.get('vix_status','?')} | "
                    f"News:{decision.get('news_driver','NONE')} | "
                    f"Risk:{decision.get('key_risk','?')} | "
                    f"IdxReason:{decision.get('index_selection_reason','?')}"
                )
                trade = self.tracker.open_trade(
                    symbol=symbol, strike=strike, option_type=otype,
                    expiry=expiry_str, strategy=decision.get("strategy", "Claude"),
                    tier=tier, direction=otype,
                    entry_index=self.feed.get_ltp(symbol),
                    entry_premium=fill_price,
                    qty=lots * lot_size(symbol),
                    lots=lots, capital_locked=cap,
                    sl_pct=sl_p, sl_index=float(decision.get("sl_index_level", 0)),
                    t1_pct=t1, t2_pct=t2, exit_by=exit_by,
                    instrument_key=real_instrument_key,  # FIX: real key from chain
                    order_id=result.order_id,
                    notes=trade_notes,
                )
                if trade:
                    self.notify.trade_opened(
                        trade.id, symbol, strike, otype,
                        fill_price, lots, decision.get("strategy", "")
                    )
                    # ── Persistent memory: capture full entry context ─────────
                    try:
                        from bot_memory import get_memory
                        from utils import is_expiry_day
                        entry_ctx = {
                            # Claude's decision context
                            "strategy":              decision.get("strategy", ""),
                            "confidence":            conf,
                            "reasoning":             decision.get("reasoning", ""),
                            "market_type":           decision.get("market_type", ""),
                            "vix_status":            decision.get("vix_status", ""),
                            "news_driver":           decision.get("news_driver", "NONE"),
                            "key_risk":              decision.get("key_risk", ""),
                            "index_selection_reason": decision.get("index_selection_reason", ""),
                            # Live market state at entry
                            "vix":                   round(self.feed.get_vix(), 2),
                            "session_bias":          getattr(config, "SESSION_BIAS", "NEUTRAL"),
                            "gift_bias":             getattr(config, "GIFT_NIFTY_BIAS", ""),
                            "monthly_bias":          config.MARKET_CONTEXT.get("monthly_bias", ""),
                            "is_expiry_day":         is_expiry_day(),
                            "entry_index_price":     round(self.feed.get_ltp(symbol), 2),
                        }
                        get_memory().attach_entry_context(trade.id, entry_ctx)
                    except Exception as _mctx_err:
                        log.debug(f"Memory context attach skipped: {_mctx_err}")
            else:
                log.error(f"Order failed: {result.error}")
        else:
            log.info(f"[SIGNAL MODE] BUY {symbol} {strike}{otype} @ ~₹{premium:.0f} | {lots} lot(s)")

    def _handle_claude_exit(self, decision: dict):
        reason = decision.get("reasoning", "Claude EXIT signal")
        for trade in self.tracker.get_open_trades():
            self._close_trade(trade, reason, manual=False)

    # ── Alert Loop ────────────────────────────────────────────────

    def _alert_loop(self):
        while self._running:
            try:
                alerts = self.tracker.check_exits()
                for alert in alerts:
                    trade = alert["trade"]
                    atype = alert["type"]
                    log.warning(alert["message"])

                    if atype == "SL_HIT":
                        self.notify.sl_hit(
                            trade.id, trade.symbol, trade.strike,
                            trade.option_type, trade.pnl_pct * 100, trade.pnl_rs
                        )
                        self._close_trade(trade, "SL_HIT")

                    elif atype == "T1_HIT":
                        t1_qty = trade.qty // 2
                        self.notify.t1_hit(
                            trade.id, trade.symbol, trade.strike,
                            trade.option_type, trade.pnl_pct * 100
                        )
                        if config.EXECUTION_MODE in ("LIVE", "PAPER") and t1_qty > 0:
                            self.orders.close_position(trade.instrument_key, t1_qty)
                        self.tracker.book_t1(trade.id, trade.current_premium, t1_qty)
                        # Trail SL to breakeven: remaining half rides free
                        # SL_pct=0 → triggers when premium drops back to entry (no loss)
                        if getattr(config, 'TRAIL_SL_TO_BREAKEVEN', True):
                            self.tracker.update_sl(trade.id, 0.0)
                            log.info(f"🔒 [{trade.id}] SL trailed to breakeven — T2 runs free")

                    elif atype == "T2_HIT":
                        self.notify.t2_hit(
                            trade.id, trade.symbol, trade.strike,
                            trade.option_type, trade.pnl_pct * 100
                        )
                        log.info(f"🏆 T2 hit [{trade.id}] — closing remaining position at peak.")
                        self._close_trade(trade, "T2_HIT")

                    elif atype == "TIME_EXIT":
                        self.notify.time_exit(
                            trade.id, trade.symbol, trade.strike,
                            trade.option_type, trade.exit_by
                        )
                        self._close_trade(trade, f"TIME_EXIT_{trade.exit_by}")

                for a in self.tracker.pop_alerts():
                    log.info(f"📢 {a['message']}")

            except Exception as e:
                log.error(f"Alert loop error: {e}")
            time.sleep(2)

    def _close_trade(self, trade, reason: str, manual: bool = False):
        if config.EXECUTION_MODE in ("LIVE", "PAPER") and not manual:
            result = self.orders.close_position(
                trade.instrument_key, trade.qty - trade.t1_qty_booked
            )
            exit_price = result.price if result and result.price > 0 else trade.current_premium
        else:
            exit_price = trade.current_premium

        self.tracker.close_trade(trade.id, exit_price, reason)
        self.notify.trade_closed(
            trade.id, trade.symbol, trade.strike, trade.option_type,
            trade.pnl_pct * 100, trade.realized_pnl_rs, reason
        )

    # ── EOD Monitor ───────────────────────────────────────────────

    def _eod_monitor(self):
        while self._running:
            try:
                if is_hard_exit_time():
                    open_trades = self.tracker.get_open_trades()
                    if open_trades:
                        log.warning(f"⏰ {config.HARD_EXIT_TIME} — FORCE CLOSING ALL OPEN POSITIONS")
                        for t in open_trades:
                            self._close_trade(t, "EOD_FORCE_EXIT")
                        self.notify.send(f"⏰ <b>{config.HARD_EXIT_TIME} — All positions force-closed.</b>")
            except Exception as e:
                log.error(f"EOD monitor error: {e}")
            time.sleep(30)

    # ── Keyboard Commands ──────────────────────────────────────────

    def _keyboard_loop(self):
        print("\nType 'h' for commands.\n")
        while self._running:
            try:
                cmd = input().strip().lower()
                if not cmd:
                    continue

                if cmd == "q":
                    self.stop()
                    break
                elif cmd == "c":
                    result = self.analyst.force_call()
                    if result:
                        log.info(f"Claude: {result.get('action')} — {result.get('reasoning','')[:80]}")
                elif cmd == "r":
                    self.tracker.reset_day()
                    self.engine.reset_day()
                    log.info("Daily counters reset.")
                elif cmd == "s":
                    log.info(f"STATS: {self.tracker.get_stats()}")
                elif cmd == "h":
                    print("""
─── COMMANDS ──────────────────────────────
  q                    — quit
  c                    — force Claude call now
  r                    — reset daily counters
  s                    — print stats
  open BN 54000 PE 200 — record a manual paper trade
  close <ID>           — close a trade by ID
  h                    — this help
────────────────────────────────────────────""")
                elif cmd.startswith("open "):
                    parts = cmd.split()
                    if len(parts) >= 4:
                        sym    = "BANKNIFTY" if parts[1].upper() in ("BN","BANKNIFTY") else "NIFTY"
                        strike = int(parts[2])
                        otype  = parts[3].upper()
                        prem   = float(parts[4]) if len(parts) > 4 else 200.0
                        _, expiry = next_expiry(sym)
                        trade = self.tracker.open_trade(
                            symbol=sym, strike=strike, option_type=otype,
                            expiry=expiry, strategy="Manual", tier=2, direction=otype,
                            entry_index=self.feed.get_ltp(sym),
                            entry_premium=prem, qty=lot_size(sym), lots=1,
                            capital_locked=config.TOTAL_CAPITAL * 0.05,
                            sl_pct=0.20, sl_index=0, t1_pct=0.50, t2_pct=0.80,
                            exit_by=config.HARD_EXIT_TIME,
                        )
                        if trade:
                            log.info(f"Manual trade opened: {trade.id}")
                elif cmd.startswith("close "):
                    tid = cmd.split()[-1].upper()
                    t   = self.tracker.get_trade(tid)
                    if t and t.is_open:
                        self._close_trade(t, "MANUAL_EXIT", manual=True)
                    else:
                        log.warning(f"Trade {tid} not found or not open.")

            except (EOFError, KeyboardInterrupt):
                break
            except Exception as e:
                log.error(f"Command error: {e}")


# ── Entry Point ───────────────────────────────────────────────────

def main():
    mode_label = config.EXECUTION_MODE
    banner = f"""
╔══════════════════════════════════════════════════════════════╗
║     ⚡   WAR ROOM — NIFTY / BANKNIFTY OPTION BUYING BOT      ║
║     {mode_label} MODE | Aggressive · Precise · Rule-Based          ║
╚══════════════════════════════════════════════════════════════╝

DAILY CHECKLIST (LIVE MODE):
  ✅ python morning_prep.py   (token refresh + live balance fetch)
  ✅ .env: EXECUTION_MODE=LIVE confirmed
  ✅ .env: ANTHROPIC_API_KEY set
  ✅ KEY_LEVELS auto-updated by morning_prep.py
  ✅ TRADING_CAPITAL auto-set from Upstox funds-and-margin
  ✅ ngrok http 5003  (optional — run in separate terminal for dashboard)
  ⚠️  REAL MONEY IS DEPLOYED — ALL ORDERS ARE LIVE
"""
    print(banner)

    from dotenv import load_dotenv
    load_dotenv()

    if not config.ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY not set in .env")
        sys.exit(1)

    try:
        bot = WARRoom()
        bot.start()
    except KeyboardInterrupt:
        print("\n[Stopped by user]")
    except Exception as e:
        log.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
