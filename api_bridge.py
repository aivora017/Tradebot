# ================================================================
#  api_bridge.py — WAR ROOM Browser API Bridge
#  Reads live data from the running WARRoom bot instance.
#  Exposes via Flask → ngrok → browser dashboard.
#
#  INTEGRATION (add 3 lines to main.py):
#    Import:  from api_bridge import APIBridge
#    Init:    self.api_bridge = APIBridge(self)     # after self.dashboard=...
#    Start:   self.api_bridge.start()               # after self.analyst.start()
#
#  Then: ngrok http 5003
#  Paste https URL into warroom_live.html Setup tab.
# ================================================================

import threading
from datetime import datetime
from typing import Optional

try:
    from flask import Flask, jsonify, request
    from flask_cors import CORS
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False

try:
    import config
    from logger_setup import get_logger
    log = get_logger("APIBridge")
except Exception:
    import logging
    log = logging.getLogger("APIBridge")


class APIBridge:
    PORT = 5003

    def __init__(self, bot):
        self.bot  = bot
        self._app = None
        self._thread: Optional[threading.Thread] = None
        if not FLASK_AVAILABLE:
            log.error("Flask not installed. Run: pip install flask flask-cors")
            return
        self._build_app()

    def _build_app(self):
        app = Flask("warroom_bridge")
        CORS(app, origins="*")
        self._app = app

        @app.route("/")
        def dashboard():
            import os
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warroom_live.html")
            try:
                with open(html_path, "r", encoding="utf-8") as f:
                    return f.read(), 200, {"Content-Type": "text/html"}
            except FileNotFoundError:
                return "warroom_live.html not found", 404

        @app.route("/api/health")
        def health():
            return jsonify({
                "status": "ok",
                "ws_connected": self.bot.feed.is_connected(),
                "mode": config.EXECUTION_MODE,
                "capital": config.TOTAL_CAPITAL,
                "time": datetime.now().strftime("%H:%M:%S"),
            })

        @app.route("/api/snapshot")
        def snapshot():
            return jsonify(self._build_snapshot())

        @app.route("/api/chain/<symbol>")
        def chain(symbol):
            rows = self.bot.feed.get_chain(symbol.upper())
            return jsonify([{
                "strike": r.strike, "expiry": r.expiry,
                "ce_ltp": r.ce_ltp, "ce_oi": r.ce_oi, "ce_iv": r.ce_iv,
                "pe_ltp": r.pe_ltp, "pe_oi": r.pe_oi, "pe_iv": r.pe_iv,
            } for r in rows])

        @app.route("/api/claude", methods=["POST"])
        def claude_proxy():
            """Proxy Claude API calls from browser — keeps API key server-side."""
            import requests as req
            body = request.get_json(force=True, silent=True) or {}
            try:
                r = req.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key":         config.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type":      "application/json",
                    },
                    json=body,
                    timeout=30,
                )
                return (r.content, r.status_code,
                        {"Content-Type": "application/json",
                         "Access-Control-Allow-Origin": "*"})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @app.route("/api/claude", methods=["OPTIONS"])
        def claude_proxy_opts():
            return "", 200, {
                "Access-Control-Allow-Origin":  "*",
                "Access-Control-Allow-Methods": "POST, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type",
            }

        @app.route("/api/command", methods=["POST"])
        def command():
            body = request.get_json(force=True, silent=True) or {}
            cmd  = body.get("cmd", "")

            if cmd == "force_claude":
                threading.Thread(
                    target=lambda: self.bot.analyst.force_call(), daemon=True
                ).start()
                return jsonify({"ok": True, "msg": "Claude call triggered"})

            elif cmd == "close_trade":
                tid   = body.get("trade_id", "")
                trade = self.bot.tracker.get_trade(tid)
                if trade and trade.is_open:
                    self.bot._close_trade(trade, "BROWSER_MANUAL", manual=True)
                    return jsonify({"ok": True, "msg": f"Trade {tid} closed"})
                return jsonify({"ok": False, "msg": f"Trade {tid} not found"})

            elif cmd == "reset_day":
                self.bot.tracker.reset_day()
                self.bot.engine.reset_day()
                return jsonify({"ok": True, "msg": "Day reset"})

            elif cmd == "open_manual":
                from utils import next_expiry, lot_size
                sym    = body.get("symbol", "BANKNIFTY")
                strike = int(body.get("strike", 0))
                otype  = body.get("type", "PE")
                prem   = float(body.get("premium", 0))
                _, expiry = next_expiry(sym)
                trade = self.bot.tracker.open_trade(
                    symbol=sym, strike=strike, option_type=otype,
                    expiry=expiry, strategy="Manual_Browser", tier=2, direction=otype,
                    entry_index=self.bot.feed.get_ltp(sym),
                    entry_premium=prem, qty=lot_size(sym), lots=1,
                    capital_locked=config.TOTAL_CAPITAL * 0.05,
                    sl_pct=0.20, sl_index=0, t1_pct=0.50, t2_pct=0.80,
                    exit_by=config.HARD_EXIT_TIME,
                )
                return jsonify({"ok": bool(trade), "trade_id": trade.id if trade else None})

            return jsonify({"ok": False, "msg": f"Unknown: {cmd}"})

    def _build_snapshot(self) -> dict:
        bot = self.bot
        now = datetime.now()

        bn  = bot.feed.get_tick("BANKNIFTY")
        n50 = bot.feed.get_tick("NIFTY")
        vix = bot.feed.get_vix()

        bn_data = {
            "ltp": bn.ltp if bn else 0,
            "chg_pct": round(bn.change_pct, 2) if bn else 0,
            "high": bn.high if bn else 0,
            "low":  bn.low  if bn else 0,
        }
        n50_data = {
            "ltp": n50.ltp if n50 else 0,
            "chg_pct": round(n50.change_pct, 2) if n50 else 0,
            "high": n50.high if n50 else 0,
            "low":  n50.low  if n50 else 0,
        }

        pcr_bn  = bot.feed.get_pcr("BANKNIFTY")
        pcr_n50 = bot.feed.get_pcr("NIFTY")
        mp_bn   = bot.feed.get_max_pain("BANKNIFTY")
        mp_n50  = bot.feed.get_max_pain("NIFTY")

        chain   = bot.feed.get_chain("BANKNIFTY")
        bn_ltp  = bn.ltp if bn else 54000
        bn_atm  = round(bn_ltp / 100) * 100
        atm_chain = [
            {"strike": r.strike,
             "ce_ltp": r.ce_ltp, "ce_oi": r.ce_oi, "ce_iv": r.ce_iv,
             "pe_ltp": r.pe_ltp, "pe_oi": r.pe_oi, "pe_iv": r.pe_iv}
            for r in chain if abs(r.strike - bn_atm) <= 300
        ][:8]

        c5_bn = bot.feed.get_candles("BANKNIFTY", "5m", 3)
        last_c5 = {
            "open": c5_bn[-1].open, "high": c5_bn[-1].high,
            "low":  c5_bn[-1].low,  "close": c5_bn[-1].close,
            "bullish": c5_bn[-1].is_bullish, "range": round(c5_bn[-1].range, 1),
        } if c5_bn else {}

        with bot._lock:
            sigs = list(bot._signals[-10:])

        sig_data = [{
            "strategy": s.strategy, "symbol": s.symbol,
            "direction": s.direction.value, "strike": s.strike,
            "strength": s.strength.name, "entry_trigger": s.entry_trigger,
            "sl_pct": round(s.sl_premium_pct * 100, 0),
            "t1_pct": round(s.t1_pct * 100, 0),
            "t2_pct": round(s.t2_pct * 100, 0),
            "reason": s.reason, "ok": s.filters_ok, "fail": s.filters_fail,
            "ts": s.ts.strftime("%H:%M:%S"),
        } for s in sigs]

        trades = [{
            "id": t.id, "instrument": t.symbol, "strike": t.strike,
            "option_type": t.option_type, "expiry": t.expiry,
            "strategy": t.strategy, "tier": t.tier,
            "entry_premium": t.entry_premium, "current_premium": t.current_premium,
            "entry_index": t.entry_index,
            "pnl_pct": round(t.pnl_pct * 100, 2),
            "pnl_rs": round(t.pnl_rs, 0),
            "sl_pct": round(t.sl_pct * 100, 0),
            "t1_pct": round(t.t1_pct * 100, 0),
            "t2_pct": round(t.t2_pct * 100, 0),
            "t1_booked": t.t1_booked,
            "exit_by": t.exit_by,
            "entry_time": t.entry_time.strftime("%H:%M"),
            "lots": t.lots, "qty": t.qty,
        } for t in bot.tracker.get_open_trades()]

        from utils import is_expiry_day, vix_zone
        from strategy_engine import ORB
        orb_state = {}
        if ORB.is_set("BANKNIFTY"):
            orb = ORB.get("BANKNIFTY")
            orb_state = {
                "high": orb.get("h", 0), "low": orb.get("l", 0),
                "range": round(orb.get("h", 0) - orb.get("l", 0), 0),
                "triggered": ORB.triggered("BANKNIFTY"),
            }

        morning = {}
        if bot._morning_intel:
            m = bot._morning_intel
            morning = {
                "market_type": m.market_type, "bias": m.bias,
                "recommended_strategy": m.recommended_strategy,
                "notes": m.notes[:200],
                "is_bn_expiry": m.is_bn_expiry,
                "is_nifty_expiry": m.is_nifty_expiry,
            }

        h, m_val = now.hour, now.minute
        t_val = h * 60 + m_val
        if t_val < 9*60+20:      tw = "PRE_MARKET"
        elif t_val <= 11*60:     tw = "PRIMARY"
        elif t_val <= 11*60+30:  tw = "LATE_PRIMARY"
        elif t_val <= 13*60+30:  tw = "DEAD_ZONE"
        elif t_val <= 14*60:     tw = "SECONDARY"
        else:                    tw = "EXIT_ONLY"

        return {
            "time": now.strftime("%H:%M:%S"),
            "date": now.strftime("%d %b %Y"),
            "window": tw,
            "ws_connected": bot.feed.is_connected(),
            "execution_mode": config.EXECUTION_MODE,
            "expiry_day": is_expiry_day(),
            "market": {
                "BANKNIFTY": bn_data, "NIFTY": n50_data,
                "VIX": round(vix, 2), "vix_zone": vix_zone(vix),
                "PCR_BN": round(pcr_bn, 3), "PCR_NIFTY": round(pcr_n50, 3),
                "max_pain_BN": mp_bn, "max_pain_NIFTY": mp_n50,
            },
            "key_levels": config.KEY_LEVELS,
            "market_context": config.MARKET_CONTEXT,
            "atm_chain": atm_chain,
            "orb": orb_state,
            "last_5m_candle": last_c5,
            "signals": sig_data,
            "open_trades": trades,
            "daily_stats": bot.tracker.get_stats(),
            "claude_last": bot.analyst.get_last(),
            "morning_intel": morning,
            "capital": config.TOTAL_CAPITAL,
        }

    def start(self):
        if not FLASK_AVAILABLE or not self._app:
            log.error("Flask missing. Run: pip install flask flask-cors")
            return
        self._thread = threading.Thread(
            target=lambda: self._app.run(
                host="0.0.0.0", port=self.PORT,
                debug=False, use_reloader=False, threaded=True
            ),
            daemon=True, name="APIBridge"
        )
        self._thread.start()
        log.info(f"API Bridge → http://0.0.0.0:{self.PORT}")
        log.info(f"Run ngrok: ngrok http {self.PORT}")
