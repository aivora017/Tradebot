# ================================================================
#  notifications.py — Telegram Push Alerts
# ================================================================

import threading
import requests
from datetime import datetime
from typing import Optional

import config
from logger_setup import get_logger

log = get_logger("Notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self):
        self.enabled = config.TELEGRAM_ENABLED
        self.token   = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        if self.enabled:
            log.info("Telegram notifications enabled ✓")
        else:
            log.info("Telegram disabled (set BOT_TOKEN + CHAT_ID in .env to enable)")

    def send(self, msg: str, parse_mode: str = "HTML"):
        if not self.enabled:
            return
        threading.Thread(target=self._send_sync, args=(msg, parse_mode),
                         daemon=True).start()

    def _send_sync(self, msg: str, parse_mode: str):
        try:
            r = requests.post(
                TELEGRAM_API.format(token=self.token),
                json={"chat_id": self.chat_id, "text": msg,
                      "parse_mode": parse_mode},
                timeout=8
            )
            if r.status_code != 200:
                log.debug(f"Telegram error: {r.text[:100]}")
        except Exception as e:
            log.debug(f"Telegram send failed: {e}")

    def signal(self, symbol, strike, otype, direction, strategy, strength,
               entry, sl_pct, t1, t2, reason, exit_by):
        emoji = "🟢" if "CE" in otype else "🔴"
        self.send(
            f"{emoji} <b>SIGNAL — {strength}</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"🎯 Strategy: {strategy}\n"
            f"📍 Entry trigger: {entry:.0f}\n"
            f"🛡 SL: {sl_pct:.0f}% | T1: {t1:.0f}% | T2: {t2:.0f}%\n"
            f"⏰ Exit by: {exit_by}\n"
            f"💡 {reason}\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def trade_opened(self, tid, symbol, strike, otype, premium, lots, strategy):
        emoji = "🟢" if "CE" in otype else "🔴"
        self.send(
            f"{emoji} <b>TRADE OPENED [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"💰 Premium: ₹{premium:.0f} | Lots: {lots}\n"
            f"🎯 Strategy: {strategy}\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def sl_hit(self, tid, symbol, strike, otype, pnl_pct, pnl_rs):
        self.send(
            f"❌ <b>SL HIT [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"📉 P&L: {pnl_pct:.1f}% | ₹{pnl_rs:.0f}\n"
            f"👉 Place SELL order now\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def t1_hit(self, tid, symbol, strike, otype, pnl_pct):
        self.send(
            f"🟡 <b>T1 REACHED [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"📈 P&L: +{pnl_pct:.1f}%\n"
            f"👉 SELL 50% quantity. Move SL to cost.\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def t2_hit(self, tid, symbol, strike, otype, pnl_pct):
        self.send(
            f"🟢 <b>T2 HIT [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"🚀 P&L: +{pnl_pct:.1f}%\n"
            f"👉 Trail stop with index level.\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def time_exit(self, tid, symbol, strike, otype, exit_by):
        self.send(
            f"⏰ <b>TIME EXIT [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"🚨 Must exit by {exit_by} — CLOSE NOW\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def trade_closed(self, tid, symbol, strike, otype, pnl_pct, pnl_rs, reason):
        emoji = "✅" if pnl_pct >= 0 else "❌"
        self.send(
            f"{emoji} <b>TRADE CLOSED [{tid}]</b>\n"
            f"📊 {symbol} {strike} {otype}\n"
            f"P&L: {'+' if pnl_pct>=0 else ''}{pnl_pct:.1f}% | ₹{pnl_rs:+.0f}\n"
            f"Reason: {reason}\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def claude_decision(self, action, symbol, strike, otype, confidence, reasoning):
        emoji = {"BUY": "⚡", "WAIT": "⏳", "SKIP": "⏭️",
                 "EXIT": "🚪", "STOP_TRADING": "🛑"}.get(action, "ℹ️")
        self.send(
            f"{emoji} <b>CLAUDE: {action}</b> [{confidence}]\n"
            f"{'📊 ' + symbol + ' ' + str(strike) + ' ' + otype if action == 'BUY' else ''}\n"
            f"💡 {reasoning}\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def morning_plan(self, nifty, bn, vix, pcr, market_type, top_strategy):
        self.send(
            f"🌅 <b>MORNING WAR PLAN</b>\n"
            f"📊 Nifty: {nifty:.0f} | BN: {bn:.0f}\n"
            f"🌡 VIX: {vix:.1f} | PCR: {pcr:.2f}\n"
            f"🎯 Market type: {market_type}\n"
            f"⚡ Focus strategy: {top_strategy}\n"
            f"<i>Follow the rules. Protect the capital.</i>"
        )

    def daily_summary(self, pnl_rs, pnl_pct, wins, losses, trades):
        emoji = "✅" if pnl_rs >= 0 else "❌"
        self.send(
            f"{emoji} <b>DAY SUMMARY</b>\n"
            f"💰 P&L: ₹{pnl_rs:+.0f} ({pnl_pct:+.1f}%)\n"
            f"📊 Trades: {trades} | W: {wins} L: {losses}\n"
            f"Win rate: {round(wins/trades*100) if trades else 0}%\n"
            f"<i>Review journal. Come back stronger.</i>"
        )

    def stop_trading_alert(self, reason):
        self.send(
            f"🛑 <b>STOP TRADING</b>\n"
            f"⚠️ {reason}\n"
            f"No more trades today. Close all open positions.\n"
            f"<i>{datetime.now().strftime('%H:%M:%S')}</i>"
        )

    def test(self):
        self.send("🤖 <b>WAR ROOM BOT STARTED</b>\nAll systems online. PAPER mode. Watching markets.")
