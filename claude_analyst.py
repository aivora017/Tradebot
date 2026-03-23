# ================================================================
#  claude_analyst.py — Live Market Data + News → Claude → Decision
# ================================================================

import json
import time
import threading
from datetime import datetime
from typing import Optional, List, Callable

import anthropic
import config
from logger_setup import get_logger

log = get_logger("Claude")

SYSTEM = """You are an aggressive intraday options BUYING analyst for an experienced NSE trader.
Trade NIFTY 50 and BANK NIFTY options. BUYING ONLY — no selling, no writing, no spreads.

TRADER CONTEXT:
- 3 years experience, NOT a beginner
- Capital: Rs.50,000 starting, compounding to Rs.14 lakhs in 12 months
- Monthly target: 60-120% returns via compounding
- LOT SIZES (SEBI 2024): BankNifty=30 units/lot, Nifty=75 units/lot
- EXPIRY: BankNifty=Wednesday, Nifty=Tuesday (changed Sep 2025)
- Risk per trade: 5% of capital (dynamic, grows with capital)

NEWS + EVENT INTEGRATION RULES:
- HIGH impact news in last 30 min → mention in reasoning, adjust confidence/direction
- Upcoming HIGH impact event < 15 min → action = WAIT (no directional entry)
- Upcoming HIGH impact event 15-60 min → prefer straddle, not directional
- Bearish news + technical PE signal = boost confidence
- Bullish news + technical CE signal = boost confidence
- Conflicting news vs technicals = reduce confidence to LOW/MEDIUM
- News sentiment BEARISH + BN downtrend → strong PE bias

RULES YOU MUST ENFORCE:
- VIX < 12 → SKIP, no trades at all
- VIX 12-16 → reduce size 50%, flag it
- VIX 16-22 → full size, aggressive
- VIX > 28 → SKIP
- Never recommend holding past 2:00 PM (HARD exit)
- Expiry day → all exits by 11:00 AM, no exceptions
- After 3 daily losses → STOP_TRADING
- Dead zone 11:30-13:30 → no new entries
- BN resistance 54500/55000 = strong PE entry zones (March 2026 context)
- BN support 53500/53000 = strong CE entry zones

RESPOND ONLY IN JSON. No markdown. No extra text. Exact format:
{
  "action": "BUY | WAIT | SKIP | EXIT | STOP_TRADING",
  "instrument": "BANKNIFTY or NIFTY",
  "strike": 54000,
  "option_type": "CE or PE or CE+PE",
  "expiry": "26 Mar",
  "lots": 1,
  "entry_at_index": 54200,
  "sl_premium_pct": 20,
  "sl_index_level": 54500,
  "t1_premium_pct": 50,
  "t2_premium_pct": 100,
  "exit_by": "14:00",
  "position_size_pct": 5,
  "strategy": "ORB Breakout",
  "tier": 1,
  "confidence": "LOW | MEDIUM | HIGH | GODMODE",
  "reasoning": "One clear sentence explaining the trade.",
  "market_type": "Trending | Rangebound | Event | Expiry | Reversal",
  "vix_status": "IDEAL | REDUCED | SKIP | ELEVATED",
  "news_driver": "Key news or event influencing this decision, or NONE",
  "event_risk": "HIGH | MEDIUM | LOW",
  "key_risk": "Main risk in one sentence.",
  "stop_note": ""
}
If action=WAIT or SKIP, still populate market_type, vix_status, news_driver, event_risk, reasoning.
Be aggressive. Be specific. No vague answers."""


class ClaudeAnalyst:

    def __init__(self, feed, tracker, news_feed=None):
        self.feed      = feed
        self.tracker   = tracker
        self.news      = news_feed
        self.client    = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        self._signals: List        = []
        self._last_resp: Optional[dict] = None
        self._last_call: float     = 0
        self._lock     = threading.Lock()
        self._callbacks: List[Callable] = []
        self._stop     = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log.info("Claude analyst started.")

    def stop(self):
        self._stop.set()

    def on_decision(self, fn: Callable):
        self._callbacks.append(fn)

    def add_signal(self, sig):
        with self._lock:
            self._signals.append(sig)
            if len(self._signals) > 20:
                self._signals = self._signals[-20:]

    def get_last(self) -> Optional[dict]:
        return self._last_resp

    def force_call(self) -> Optional[dict]:
        self._call()
        return self._last_resp

    def _loop(self):
        while not self._stop.is_set():
            if time.time() - self._last_call >= config.CLAUDE_CALL_INTERVAL:
                try:
                    self._call()
                except Exception as e:
                    log.error(f"Claude call error: {e}")
                self._last_call = time.time()
            time.sleep(5)

    def _call(self):
        try:
            with self._lock:
                sigs = list(self._signals)
            trades = self.tracker.get_open_trades()
            packet = self._build_packet(sigs, trades)

            log.info("Calling Claude API…")
            t0 = time.time()

            resp = self.client.messages.create(
                model=config.CLAUDE_MODEL,
                max_tokens=config.CLAUDE_MAX_TOKENS,
                system=SYSTEM,
                messages=[{
                    "role":    "user",
                    "content": f"Live market snapshot:\n{packet}\n\nGive me your decision now."
                }]
            )

            latency = int((time.time() - t0) * 1000)
            raw     = resp.content[0].text.strip()

            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.lower().startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            decision = json.loads(raw)
            decision["_ts"]      = datetime.now().strftime("%H:%M:%S")
            decision["_latency"] = latency

            self._last_resp = decision
            action = decision.get("action", "WAIT")
            conf   = decision.get("confidence", "")
            reason = decision.get("reasoning", "")[:80]
            log.info(f"Claude: {action} [{conf}] | {reason} | {latency}ms")

            for cb in self._callbacks:
                try:
                    cb(decision)
                except Exception as e:
                    log.debug(f"Decision callback: {e}")

        except json.JSONDecodeError as e:
            log.error(f"Claude JSON parse error: {e}")
        except Exception as e:
            log.error(f"Claude API error: {e}")

    def _build_packet(self, signals: list, trades: list) -> str:
        bn      = self.feed.get_tick("BANKNIFTY")
        n50     = self.feed.get_tick("NIFTY")
        vix     = self.feed.get_vix()
        pcr_bn  = self.feed.get_pcr("BANKNIFTY")
        pcr_n50 = self.feed.get_pcr("NIFTY")
        mp_bn   = self.feed.get_max_pain("BANKNIFTY")
        mp_n50  = self.feed.get_max_pain("NIFTY")
        chain   = self.feed.get_chain("BANKNIFTY")

        bn_ltp = bn.ltp if bn else 54000
        bn_atm = round(bn_ltp / 100) * 100
        chain_snap = [
            {"strike": r.strike,
             "ce_ltp": r.ce_ltp, "ce_oi": r.ce_oi, "ce_iv": r.ce_iv,
             "pe_ltp": r.pe_ltp, "pe_oi": r.pe_oi, "pe_iv": r.pe_iv}
            for r in chain if abs(r.strike - bn_atm) <= 400
        ][:10]

        sig_data = [
            {"strategy": s.strategy, "direction": s.direction.value,
             "trigger": s.entry_trigger, "strike": s.strike,
             "strength": s.strength.name,
             "ok": s.filters_ok, "fail": s.filters_fail,
             "reason": s.reason}
            for s in signals[-5:]
        ]

        trade_data = [
            {"id": t.id, "symbol": t.symbol,
             "strike": t.strike, "type": t.option_type,
             "entry_prem": t.entry_premium,
             "cur_prem": t.current_premium,
             "pnl_pct": round(t.pnl_pct * 100, 1),
             "sl_pct": t.sl_pct * 100,
             "entry_time": t.entry_time.strftime("%H:%M")}
            for t in trades
        ]

        now = datetime.now()
        h, m = now.hour, now.minute
        t_val = h * 60 + m
        if t_val < 9*60+20:       tw = "PRE_ENTRY"
        elif t_val <= 11*60:      tw = "PRIMARY_ENTRY"
        elif t_val <= 11*60+30:   tw = "LATE_PRIMARY"
        elif t_val <= 13*60+30:   tw = "DEAD_ZONE"
        elif t_val <= 14*60:      tw = "SECONDARY"
        else:                     tw = "EXIT_ONLY"

        # News context
        news_context = {}
        if self.news:
            try:
                news_context = self.news.get_context_packet()
            except Exception as e:
                log.debug(f"News context error: {e}")

        packet = {
            "time":    now.strftime("%H:%M:%S"),
            "window":  tw,
            "market": {
                "BANKNIFTY": {
                    "ltp":     bn.ltp if bn else 0,
                    "chg_pct": round(bn.change_pct, 2) if bn else 0,
                    "high":    bn.high if bn else 0,
                    "low":     bn.low if bn else 0,
                },
                "NIFTY": {
                    "ltp":     n50.ltp if n50 else 0,
                    "chg_pct": round(n50.change_pct, 2) if n50 else 0,
                    "high":    n50.high if n50 else 0,
                    "low":     n50.low if n50 else 0,
                },
                "VIX":       round(vix, 2),
                "PCR_BN":    round(pcr_bn, 2),
                "PCR_NIFTY": round(pcr_n50, 2),
                "max_pain":  {"BANKNIFTY": mp_bn, "NIFTY": mp_n50},
            },
            "key_levels":       config.KEY_LEVELS,
            "market_context":   config.MARKET_CONTEXT,
            "chain_atm_bn":     chain_snap,
            "strategy_signals": sig_data,
            "open_trades":      trade_data,
            "daily_stats":      self.tracker.get_stats(),
            "capital":          config.TOTAL_CAPITAL,
            "lot_sizes":        config.LOT_SIZES,
            "news_and_events":  news_context,
        }
        return json.dumps(packet, indent=2)
