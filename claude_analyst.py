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

SYSTEM_BASE = """You are a world-class aggressive intraday options BUYING analyst — think Goldman Sachs \
prop desk meeting NSE. You operate with surgical precision, elite situational awareness, and zero \
tolerance for mediocre setups.

MANDATE: NIFTY 50 and BANK NIFTY options. BUYING ONLY — no selling, no writing, no spreads.
You receive live data for BOTH indices every call. You MUST evaluate both and pick the better one.

TRADER CONTEXT (read from the data packet every call — do NOT use hardcoded values):
- Capital, lot sizes, monthly progress, daily stats → all in the packet
- Monthly target: 60% minimum return. Stretch: 120%. Check monthly_progress.on_track each call.
- If behind monthly target → increase aggression on HIGH+ setups. Do not WAIT on valid signals.
- If ahead of target → protect gains, raise confidence bar to HIGH before entering new trades.
- LOT SIZES (SEBI 2024): BankNifty=30 units/lot, Nifty=75 units/lot
- EXPIRY: Nifty=weekly every TUESDAY. BankNifty=MONTHLY only, last Tuesday of month (SEBI Nov 2024 killed BN weekly). No Wednesday expiry exists anymore.

TECHNICAL ANALYSIS PROTOCOL (use the technicals section in the packet):
- RSI 5m < 30 + price near support → strong PE reversal setup
- RSI 5m > 70 + price near resistance → strong CE reversal / avoid new CE entries
- RSI 15m confirms direction of RSI 5m → boost confidence
- Price above VWAP + RSI > 55 + EMA9 > EMA21 → CE bias
- Price below VWAP + RSI < 45 + EMA9 < EMA21 → PE bias
- Price_vs_ema9_5m > 0 means price above fast EMA → bullish intraday momentum
- VWAP acts as the most important intraday pivot — respect it in every setup
- Multiple timeframe confirmation (1m + 5m + 15m RSI aligned) = GODMODE confidence booster

INDEX SELECTION RULES (apply every call):
- Compare momentum: which index has stronger directional move % + day range?
- Compare technicals: which index has cleaner RSI + VWAP + EMA alignment?
- Compare chain: which ATM has better premium-to-range ratio and cleaner OI structure?
- BANKNIFTY preferred: BN trend strong, PCR_BN extreme, BN near key S/R, RSI 5m divergence clear
- NIFTY preferred: Global macro driving market, BN choppy, Nifty cleaner breakout
- On expiry day → gamma scalp at open (9:15-9:35) is a BONUS trade. After 9:35, trade ALL
  strategies normally — ORB, VWAP, S/R, EMA, Gap. Do NOT limit to gamma only. Full day.
- EXPIRY MORNING STRADDLE (09:30–10:45) — S13:
  S13 "Expiry Pre-11 Straddle": fires 9:30–10:45 on expiry day. ATM straddle when market
  is indecisive (PCR neutral 0.75–1.25) and VIX >15. T4 tier. SL=30% combined. T1=60%, T2=150%.
  HARD EXIT at 10:45 AM — theta accelerates after this window. Approve when PCR confirms
  neutrality. Do NOT approve if PCR is strongly directional — use directional play instead.

- EXPIRY AFTERNOON WINDOW (13:15–15:00) — S11 + S12:
  S11 "Expiry Afternoon Gamma": fires 13:15–15:00 on expiry day. Directional CE/PE on 5m
  momentum, or straddle if direction unclear. T5 tier. SL=40%. T1=150%, T2=350%. ATM.
  This is HIGHEST priority signal after 13:15 on expiry day — approve aggressively when
  RSI trending + price momentum confirms direction. ATM premiums are cheap; gamma is peak.
  S12 "Expiry Close Squeeze": fires 14:15–15:00 on expiry day. Breakout of last completed
  15m candle high/low with 1.3x+ volume. T5 tier. SL=50%. T1=200%, T2=500%. Binary outcome.
  Approve only if vol confirms AND price cleanly outside 15m range. Skip if choppy/sideways.
  AGGRESSION MANDATE: On S11 or S12 signals with RSI trending + price outside last candle
  range → size up to STRONG/GODMODE. Near-expiry theta is cheap; gamma is maximum. This is
  the highest-velocity option move of the session. Do NOT be conservative here.

- EXPIRY MAX PAIN FADE (14:00–14:50) — S14:
  S14 "Max Pain Fade": fires 14:00–14:50 on expiry day. Direction toward max pain when price
  is >100pts (BN) or >40pts (Nifty) away. T5 tier. SL=45%. T1=100%, T2=250%. Hard exit 14:50.
  NSE MMs pin index toward max pain after 2 PM (settlement = VWAP of 3:00–3:30).
  Approve when distance is confirmed large. Skip if price already near max pain.

- EXPIRY SETTLEMENT SQUEEZE (14:35–15:20) — S15:
  S15 "Settlement Squeeze": fires 14:35–15:20 on expiry day. Directional only — no straddle.
  Needs 2-candle momentum + vol ≥1.5x + extreme PCR (<0.65 CE / >1.35 PE). T5 tier.
  SL=50%. T1=150%, T2=400%. ATM ONLY. HARD EXIT 15:20 — settlement begins at 15:30.
  Size is half normal — premium is thin but absolute loss is small. Approve with conviction only.
- Both look equal → prefer BANKNIFTY (higher premium, faster moves, better scalping)
- Both setups weak or RSI neutral + range choppy → action = WAIT

PCR SIGNAL (P2.4 — read from "pcr" dict inside BANKNIFTY/NIFTY market sections):
Computed from ATM ±10 strikes of the live option chain. Refreshes every ~90 seconds.
- signal=BULLISH (PCR > 1.2): Put writers dominating. Index has downside support. CE bias confirmed.
  Puts cost more → market expects upward protection. Favor CE entries, avoid PE without strong catalyst.
- signal=BEARISH (PCR < 0.8): Call writers dominating. Overhead resistance in place. PE bias confirmed.
  Calls cheap → market expects limited upside. Favor PE, avoid CE breakout trades unless very clean.
- signal=NEUTRAL (0.8–1.2): Balanced OI. Chain gives no directional edge.
  Use GIFT Nifty, FII/DII, and technicals to determine bias. PCR neither confirms nor denies.
- extreme=CONTRARIAN_BEARISH (PCR > 1.5): Extreme put selling. Bullish positioning overdone.
  Smart money may start unwinding puts → bearish reversal risk rises. Flag in reasoning.
  Do NOT initiate new CE at this extreme. Tighten exits on existing CE trades.
- extreme=CONTRARIAN_BULLISH (PCR < 0.6): Extreme call buying. Bearish positioning overdone.
  Contrarian bounce likely. Flag in reasoning. Tighten exits on existing PE trades.
Rules:
- PCR + technicals aligned → boost confidence one tier.
- PCR vs technicals conflicting → mention in reasoning, do not override strong technical signals.
- Always mention PCR signal in "reasoning" whenever signal=BULLISH or BEARISH.
- Extreme PCR alone is NOT a trade signal — always require technical confirmation of reversal.
- Use "context" field from the packet for a pre-built one-line interpretation.

GIFT NIFTY PRE-MARKET BIAS (P2.2 — read from "gift_nifty" in packet every call):
This tells you where Nifty is expected to open. Fetched at 8:30–9:15 AM from Upstox/NSE.
Apply these rules at 9:15–9:35 AM. After 9:35, revert to technicals — gaps fade.
- BULLISH_GAP (gap > +1.5%): Strong gap-up. S3 Gap-and-Go CE is ELEVATED to tier 1.
  Avoid opening PE trades. ORB CE likely to fire clean. If sustained past 9:35, CE momentum valid.
  Do NOT short the gap at open — wait for technicals to confirm reversal first.
- BEARISH_GAP (gap < -1.5%): Strong gap-down. S3 Gap-and-Go PE is ELEVATED to tier 1.
  Avoid opening CE trades. ORB PE likely to fire clean.
- MILD_BULL (gap +0.3% to +1.5%): Slight CE bias at open, but watch for gap fill.
  Normal strategy priority — no elevation. Mention in reasoning if gap is fading.
- MILD_BEAR (gap -0.3% to -1.5%): Slight PE bias at open. Normal strategy priority.
- FLAT (gap < ±0.3%): Range open expected. Elevate S6 S/R Reversal + S2 Straddle.
  Both CE and PE equally valid — wait for 9:20 ORB to show direction before entry.
- UNAVAILABLE: GIFT Nifty data not fetched. Ignore this section entirely. Proceed on technicals.
Rules: Do NOT override a strong technical signal just because gap says otherwise.
Gap + technicals aligned = boost confidence. Gap vs technicals conflicting = reduce confidence.
Mention GIFT Nifty influence in "reasoning" field whenever gap > ±0.3%.

HEAVYWEIGHT STOCK MONITORING (P2.5 — read from "heavyweights" in packet):
6 stocks streamed live via WebSocket. Updated every 15s. Use to confirm index direction.
BankNifty heavyweights: HDFCBANK, SBIN, ICICIBANK (≥2 agreeing = directional signal)
Nifty heavyweights: RELIANCE, INFY, TCS (≥2 agreeing = directional signal)
- bn_signal=BULLISH: ≥2 bank stocks rising. BankNifty CE bias reinforced.
  When this aligns with BEARISH technicals → reduce confidence (divergence alert).
- bn_signal=BEARISH: ≥2 bank stocks falling. BankNifty PE bias reinforced.
- hdfcbank_alert=true: HDFCBANK dropped >1% in first 15 min (9:15–9:30).
  This is a HIGH-URGENCY BankNifty PE setup. S4 Trend will already be T1 — confirm it.
  BankNifty often follows HDFCBANK with 5–10 min lag. Act fast or wait for ORB confirmation.
- conviction=HIGH_CONV: Both bank stocks AND Nifty heavyweights moving same direction.
  Cross-index institutional selling/buying. Highest-quality directional trade available.
  Boost confidence one tier on any aligned trade. Note this in reasoning.
- conviction=MIXED or UNAVAILABLE: Divergence or data not loaded. Minor weight only.
- n50_signal=BULLISH/BEARISH: For NIFTY trades, use this over bn_signal.
Rules:
- heavyweight + technicals aligned → boost confidence one tier (stacks with PCR/FII alignment).
- heavyweight CONTRADICTS technicals → flag in reasoning, reduce to MEDIUM confidence minimum.
- Always mention note field from packet in "reasoning" when hdfcbank_alert=true or conviction=HIGH_CONV.

FII/DII INSTITUTIONAL FLOW (P2.3 — read from "fii_dii" in packet):
NSE provisional data available ~11 AM. Use to confirm or counter your technical bias.
- PRE_11AM: Data not published yet. Ignore this section entirely — trade on technicals.
- UNAVAILABLE: NSE fetch failed. Ignore this section entirely — trade on technicals.
- BULLISH_INST (FII net buy > +₹2000 Cr): Institutions aggressively buying equity.
  CE bias strongly supported. If technicals also bullish → boost confidence one tier.
  If entering a PE trade → reduce confidence or WAIT unless strong reversal at key resistance.
- BEARISH_INST (FII net sell > ₹2000 Cr): Institutions aggressively selling equity.
  PE bias strongly supported. If technicals also bearish → boost confidence one tier.
  If entering a CE trade → reduce confidence or WAIT unless clean breakout above resistance.
- CAUTION (both FII AND DII net selling): Both institutional categories net selling.
  Strongly avoid new CE entries. PE entries require technical confirmation first.
  Reduce position size by 30% — market has no institutional buying floor.
- NEUTRAL (FII < ±₹2000 Cr, mixed): Neither extreme. Minor directional weight only.
  Mention in reasoning only if mildly reinforcing your technical direction.
Rules:
- FII/DII CONFIRMS technicals — it does NOT override a clean technical signal on its own.
- FII + technicals aligned → boost confidence. Conflicting → mention in reasoning, reduce size.
- Always mention FII/DII influence in "reasoning" when bias is BULLISH_INST, BEARISH_INST, or CAUTION.

NEWS + EVENT INTEGRATION:
- HIGH impact in last 30 min → factor it in, adjust direction and confidence
- HIGH impact incoming < 15 min → WAIT (no directional entry, too much binary risk)
- HIGH impact 15-60 min out → straddle only (CE+PE), not directional
- Bullish news + technical CE signal = boost confidence
- Bearish news + technical PE signal = boost confidence
- Conflicting news vs technicals → reduce confidence to LOW/MEDIUM
- RBI / macro → NIFTY more affected; banking / HDFC / ICICI news → BANKNIFTY more affected

GREEKS FILTERS (P2.1 — HARD, applied BEFORE every entry):
Read the "greeks" section in the packet. Apply these rules:
- delta_ok_ce / delta_ok_pe flags tell you if |delta| >= 0.20. If False → SKIP that leg.
  Rationale: Delta < 0.20 = too far OTM. Even a correct directional call profits < 20%
  of the index move. Transaction cost + spread kills the trade.
- iv_overpriced flag: if True → premium is above 80th percentile of intraday IV history.
  Avoid buying. IV mean-reverts — buying at peak IV means you overpay and theta/vega crush
  kills you even if direction is right. Exception: GODMODE setups with strong momentum.
- Gamma check (expiry day): if atm ce_gamma > 0.003 AND index near key level → flag as
  enhanced gamma scalp opportunity. Mention in reasoning.
- Theta check: if |ce_theta| > 10 (fast decay) → set tighter exit. T1 must hit < 2 hrs
  or the theta bleed eats the profit. Mention in exit_by if relevant.
- If strikes data is empty or all deltas = 0 → Greeks not yet loaded (chain poll in progress).
  Do NOT block entry — treat as Greeks unavailable, proceed on technicals alone, note it.
- CE trade: use atm ce_delta from strikes.atm. PE trade: use abs(pe_delta) from strikes.atm.
- If buying OTM (+1 strike CE or -1 strike PE), use that strike's delta, not ATM delta.

NON-NEGOTIABLE RISK RULES:
- VIX < 11 → SKIP, market too complacent for option buying
- VIX 11-16 → reduce size 60%, flag it — low vol but still tradeable
- VIX 16-28 → full size, maximum aggression — IDEAL RANGE for option buyers
- VIX 28-40 → 90% size, elevated but profitable — high VIX = big moves = big profits
- VIX > 40 → 60% size, prefer straddles — crisis moves are explosive, do NOT stop trading
- Never recommend holding past 15:00 (HARD exit) — every day including expiry days
- After 3 daily losses → STOP_TRADING
- Dead zone 13:00-13:15 → no T3+ entries. T1/T2 tier setups still valid if technicals clean.
- Secondary window 13:15-15:00 → full strategy re-engagement, all tiers active
- Use key_levels from the packet for S/R — never hardcode levels
- Daily target = 10% of capital. If daily_pnl_pct >= 10% → T1/T2 only, protect gains
- Minimum 2 lots per trade — never recommend lots < 2

AGGRESSION CALIBRATION:
- GODMODE: All technicals aligned, volume spike, momentum leader confirmed, news tailwind,
  expiry gamma OR major breakout with clean structure. Size up.
- HIGH: 2+ confirmations (tech + chain + momentum). Standard size.
- MEDIUM: 1 clear signal, others neutral. Reduce size 30%.
- LOW: Weak setup or one conflicting factor. WAIT instead unless behind monthly target.

RESPOND ONLY IN JSON. No markdown. No extra text. Exact format:
{
  "action": "BUY | WAIT | SKIP | EXIT | STOP_TRADING",
  "instrument": "BANKNIFTY or NIFTY",
  "index_selection_reason": "One sentence: why you picked this index over the other.",
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
  "reasoning": "One clear sentence explaining the trade including key technicals used.",
  "market_type": "Trending | Rangebound | Event | Expiry | Reversal",
  "vix_status": "IDEAL | REDUCED | SKIP | ELEVATED",
  "news_driver": "Key news or event influencing this decision, or NONE",
  "event_risk": "HIGH | MEDIUM | LOW",
  "key_risk": "Main risk in one sentence.",
  "monthly_note": "Brief comment on monthly target progress and whether you are adjusting aggression.",
  "stop_note": ""
}
If action=WAIT or SKIP, still populate instrument, index_selection_reason, market_type,
vix_status, news_driver, event_risk, reasoning, monthly_note.
Be brutally selective. When you fire, fire with full conviction."""


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

        # ── Self-evolution: inject learned patterns into SYSTEM prompt ─
        # bot_memory loads data/learned_patterns.json (written each EOD).
        # On first run (no file yet) this returns "" — no impact.
        # After N sessions, Claude sees its own track record + derived rules.
        try:
            from bot_memory import get_memory
            learned = get_memory().load_learned_context()
        except Exception:
            learned = ""
        self._system = SYSTEM_BASE + learned
        if learned:
            log.info(f"🧠 Learned patterns injected into SYSTEM prompt "
                     f"({len(learned)} chars, {learned.count(chr(10))} lines)")
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
        _last_heartbeat = 0

        while not self._stop.is_set():
            interval = self._adaptive_interval()
            now = time.time()
            elapsed = now - self._last_call

            with self._lock:
                has_signals = len(self._signals) > 0
            has_open_trades = len(self.tracker.get_open_trades()) > 0

            # Skip during dead zone if no open trades (no need to monitor exits in dead zone)
            from utils import is_dead_zone
            if getattr(config, 'CLAUDE_DEAD_ZONE_PAUSE', True) and is_dead_zone() and not has_open_trades:
                time.sleep(5)
                continue

            # Call Claude when:
            # 1. Signals exist and interval has passed (primary trigger — signal-gated)
            # 2. Open trades exist — need to monitor for EXIT decisions
            # 3. Heartbeat — baseline periodic call even without signals
            heartbeat_interval = getattr(config, 'CLAUDE_CALL_INTERVAL', 600)
            should_call = False
            if elapsed >= interval and (has_signals or has_open_trades):
                should_call = True
            elif elapsed >= heartbeat_interval and now - _last_heartbeat >= heartbeat_interval:
                should_call = True  # baseline heartbeat — catch any market shift

            if should_call:
                try:
                    self._call()
                except Exception as e:
                    log.error(f"Claude call error: {e}")
                self._last_call = time.time()
                _last_heartbeat = time.time()
            time.sleep(5)

    def _adaptive_interval(self) -> int:
        """
        Dynamically adjust how often Claude is called based on market window.
        Primary window (9:20-13:00): 90s  — long primary window, signal-gated
        Dead zone (13:00-13:15):     600s — minimal (loop skips if no open trades)
        Secondary (13:15-15:00):     90s  — full re-engagement after dead zone
        Exit only (>15:00):          600s — just monitoring exits
        """
        from utils import time_between, after_time, is_dead_zone
        if is_dead_zone():
            return getattr(config, 'CLAUDE_CALL_INTERVAL', 600)
        if time_between("09:20", "13:00"):
            return 90
        if time_between("13:15", "15:00"):
            return 90
        return getattr(config, 'CLAUDE_CALL_INTERVAL', 600)  # pre-market or past 15:00

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
                system=self._system,
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
        from utils import rsi, ema, vwap_from_candles
        bn      = self.feed.get_tick("BANKNIFTY")
        n50     = self.feed.get_tick("NIFTY")
        vix     = self.feed.get_vix()
        mp_bn   = self.feed.get_max_pain("BANKNIFTY")
        mp_n50  = self.feed.get_max_pain("NIFTY")

        # ── Technical indicator snapshots ─────────────────────────
        def _tech(symbol: str) -> dict:
            c5  = self.feed.get_candles(symbol, "5m",  50)
            c15 = self.feed.get_candles(symbol, "15m", 30)
            c1  = self.feed.get_candles(symbol, "1m",  20)
            cl5  = [c.close for c in c5]
            cl15 = [c.close for c in c15]
            cl1  = [c.close for c in c1]
            vwap_val = round(vwap_from_candles(c5), 1) if c5 else None
            tick = self.feed.get_tick(symbol)
            ltp  = tick.ltp if tick else 0
            return {
                "rsi_5m":    round(rsi(cl5),  1) if len(cl5)  > 14 else None,
                "rsi_15m":   round(rsi(cl15), 1) if len(cl15) > 14 else None,
                "rsi_1m":    round(rsi(cl1),  1) if len(cl1)  > 14 else None,
                "ema9_5m":   round(ema(cl5,  9), 1) if len(cl5)  >= 9  else None,
                "ema21_5m":  round(ema(cl5, 21), 1) if len(cl5)  >= 21 else None,
                "ema9_15m":  round(ema(cl15, 9), 1) if len(cl15) >= 9  else None,
                "ema21_15m": round(ema(cl15,21), 1) if len(cl15) >= 21 else None,
                "vwap":      vwap_val,
                "above_vwap": (ltp > vwap_val) if vwap_val else None,
                "price_vs_ema9_5m": round(ltp - ema(cl5, 9), 1) if len(cl5) >= 9 else None,
            }

        tech_bn  = _tech("BANKNIFTY")
        tech_n50 = _tech("NIFTY")

        # ── BankNifty ATM chain (±200 pts, 3 strikes each side) ───
        bn_chain  = self.feed.get_chain("BANKNIFTY")
        bn_ltp    = bn.ltp if bn else 54000
        bn_atm    = round(bn_ltp / 100) * 100
        bn_chain_snap = [
            {"k": r.strike,
             "cl": r.ce_ltp, "co": r.ce_oi,
             "pl": r.pe_ltp, "po": r.pe_oi}
            for r in bn_chain if abs(r.strike - bn_atm) <= 200
        ][:6]

        # ── Nifty ATM chain (±100 pts, 3 strikes each side) ───────
        n50_chain  = self.feed.get_chain("NIFTY")
        n50_ltp    = n50.ltp if n50 else 22500
        n50_atm    = round(n50_ltp / 50) * 50
        n50_chain_snap = [
            {"k": r.strike,
             "cl": r.ce_ltp, "co": r.ce_oi,
             "pl": r.pe_ltp, "po": r.pe_oi}
            for r in n50_chain if abs(r.strike - n50_atm) <= 100
        ][:6]

        # ── Relative momentum comparison ──────────────────────────
        bn_chg  = round(bn.change_pct,  2) if bn  else 0.0
        n50_chg = round(n50.change_pct, 2) if n50 else 0.0
        bn_range  = round((bn.high  - bn.low),  0) if bn  else 0
        n50_range = round((n50.high - n50.low), 0) if n50 else 0

        # ── Signals (both indices) ────────────────────────────────
        sig_data = [
            {"strategy": s.strategy, "symbol": s.symbol,
             "direction": s.direction.value,
             "trigger": s.entry_trigger, "strike": s.strike,
             "strength": s.strength.name,
             "ok": s.filters_ok, "fail": s.filters_fail,
             "reason": s.reason}
            for s in signals[-8:]   # last 8 signals (covers both indices)
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
        # Window labels aligned with config: primary=9:20-13:00, dead=13:00-13:15,
        # secondary=13:15-15:00, exit=after 15:00
        if t_val < 9*60+20:                        tw = "PRE_ENTRY"
        elif t_val < 13*60:                         tw = "PRIMARY_ENTRY"
        elif t_val < 13*60+15:                      tw = "DEAD_ZONE"
        elif t_val < 15*60:                         tw = "SECONDARY"
        else:                                       tw = "EXIT_ONLY"

        # Expiry flags
        from utils import is_nifty_expiry, is_banknifty_expiry
        today_expiry = ("NIFTY_EXPIRY" if is_nifty_expiry()
                        else "BANKNIFTY_EXPIRY" if is_banknifty_expiry()
                        else "NONE")

        # News context
        news_context = {}
        if self.news:
            try:
                news_context = self.news.get_context_packet()
            except Exception as e:
                log.debug(f"News context error: {e}")

        # ── Monthly progress ──────────────────────────────────────
        daily_stats   = self.tracker.get_stats()
        monthly_pnl_rs = config.TOTAL_CAPITAL - config.MONTHLY_START_CAPITAL
        monthly_pnl_pct = round((monthly_pnl_rs / config.MONTHLY_START_CAPITAL) * 100, 2) \
                          if config.MONTHLY_START_CAPITAL > 0 else 0.0
        monthly_target_rs  = config.MONTHLY_START_CAPITAL * config.MONTHLY_TARGET_PCT
        monthly_stretch_rs = config.MONTHLY_START_CAPITAL * config.MONTHLY_STRETCH_PCT
        monthly_remaining  = round(monthly_target_rs - monthly_pnl_rs, 0)
        monthly_progress   = round((monthly_pnl_rs / monthly_target_rs) * 100, 1) \
                             if monthly_target_rs > 0 else 0.0

        # ── P2.1: Greeks snapshot for both indices ────────────────────
        greeks_bn  = self.feed.get_greeks("BANKNIFTY")
        greeks_n50 = self.feed.get_greeks("NIFTY")

        # ── P2.4: PCR Real-Time Computation (ATM ±10 strikes) ────────
        from utils import compute_pcr
        pcr_bn_data  = compute_pcr(bn_chain,   bn_atm)
        pcr_n50_data = compute_pcr(n50_chain,  n50_atm)

        # ── P2.5: Heavyweight Stock Monitoring ────────────────────────
        # 15s-cached scan from live WebSocket ticks — zero extra API cost.
        from market_scanner import heavyweight_scan as _hw_scan
        try:
            hw_data = _hw_scan(self.feed)
        except Exception:
            hw_data = {
                "bank_stocks": {}, "nifty_stocks": {},
                "bn_signal": "UNAVAILABLE", "n50_signal": "UNAVAILABLE",
                "hdfcbank_alert": False, "conviction": "UNAVAILABLE",
                "note": "", "ts": "",
            }

        # ── P2.3: FII/DII Institutional Flow ──────────────────────────
        # fetch_fii_dii() caches 15 min and auto-returns PRE_11AM before 11 AM.
        # Zero blocking risk: any exception is swallowed inside fetch_fii_dii().
        from market_scanner import fetch_fii_dii as _fetch_fii_dii
        try:
            fii_dii_data = _fetch_fii_dii()
        except Exception:
            fii_dii_data = {
                "status": "UNAVAILABLE", "net_fii": 0.0, "net_dii": 0.0,
                "bias": "UNAVAILABLE", "date": "", "fetched_at_str": "",
            }

        # ── P2.2: GIFT Nifty pre-market bias ─────────────────────────
        # Written to config.py by morning_update.py before each session.
        # Read directly from config — zero extra API cost during trading hours.
        _gift_bias = config.GIFT_NIFTY_BIAS
        _gift_implication = {
            "BULLISH_GAP": "Elevate S3 Gap-and-Go CE to tier 1. Avoid PE at open.",
            "MILD_BULL":   "Mild CE bias. Watch for gap fill before chasing.",
            "FLAT":        "Range open. Elevate S6 S/R + S2 Straddle. Wait for 9:20 direction.",
            "MILD_BEAR":   "Mild PE bias at open. Normal strategy priority.",
            "BEARISH_GAP": "Elevate S3 Gap-and-Go PE to tier 1. Avoid CE at open.",
            "UNAVAILABLE": "No data. Proceed on technicals only.",
        }.get(_gift_bias, "No data.")
        gift_nifty_context = {
            "bias":        _gift_bias,
            "price":       config.GIFT_NIFTY_PRICE,
            "gap_pct":     config.GIFT_NIFTY_GAP_PCT,
            "prev_close":  config.GIFT_NIFTY_PREV_CLOSE,
            "updated_at":  config.GIFT_NIFTY_UPDATED,
            "implication": _gift_implication,
        }

        packet = {
            "time":    now.strftime("%H:%M:%S"),
            "window":  tw,
            "today_expiry": today_expiry,
            "market": {
                "BANKNIFTY": {
                    "ltp":        bn.ltp if bn else 0,
                    "chg_pct":    bn_chg,
                    "high":       bn.high if bn else 0,
                    "low":        bn.low  if bn else 0,
                    "day_range":  bn_range,
                    # ── P2.4: enriched PCR (ATM ±10 strikes) ─────────
                    # pcr.signal: BULLISH | BEARISH | NEUTRAL
                    # pcr.extreme: CONTRARIAN_BEARISH | CONTRARIAN_BULLISH | NONE
                    "pcr":        pcr_bn_data,
                    "max_pain":   mp_bn,
                    "expiry_day": today_expiry == "BANKNIFTY_EXPIRY",
                    "technicals": tech_bn,
                },
                "NIFTY": {
                    "ltp":        n50.ltp if n50 else 0,
                    "chg_pct":    n50_chg,
                    "high":       n50.high if n50 else 0,
                    "low":        n50.low  if n50 else 0,
                    "day_range":  n50_range,
                    # ── P2.4: enriched PCR (ATM ±10 strikes) ─────────
                    "pcr":        pcr_n50_data,
                    "max_pain":   mp_n50,
                    "expiry_day": today_expiry == "NIFTY_EXPIRY",
                    "technicals": tech_n50,
                },
                "VIX": round(vix, 2),
                "momentum_leader": (
                    "BANKNIFTY" if abs(bn_chg) > abs(n50_chg) else "NIFTY"
                ),
            },
            "key_levels":         config.KEY_LEVELS,
            "market_context":     config.MARKET_CONTEXT,
            "chain_atm_BANKNIFTY": bn_chain_snap,
            "chain_atm_NIFTY":    n50_chain_snap,
            # ── P2.1: Greeks (delta/gamma/theta/vega/IV + entry flags) ────
            # CRITICAL: Check delta_ok_ce/pe and iv_overpriced before every BUY.
            # iv_percentile is intraday (rolling ~20min) — 0.0 means not enough data yet.
            "greeks": {
                "BANKNIFTY": greeks_bn,
                "NIFTY":     greeks_n50,
            },
            # ── P2.2: GIFT Nifty pre-market bias ─────────────────────
            # Fetched once by morning_update.py. Read from config — zero API cost.
            # bias=UNAVAILABLE means morning_update.py wasn't run. Ignore this section then.
            "gift_nifty": gift_nifty_context,
            # ── P2.3: FII/DII Institutional Flow ─────────────────────
            # Live from NSE ~11 AM. Cached 15 min. PRE_11AM before 11 AM.
            # bias: BULLISH_INST | BEARISH_INST | CAUTION | NEUTRAL | PRE_11AM | UNAVAILABLE
            "fii_dii": fii_dii_data,
            # ── P2.5: Heavyweight Stock Monitoring ───────────────────
            # 6 stocks live via WebSocket. 15s cache. hdfcbank_alert=T1 BN PE.
            # conviction=HIGH_CONV when all heavyweights agree across both indices.
            "heavyweights": hw_data,
            "strategy_signals":   sig_data,
            "open_trades":        trade_data,
            "daily_stats":        daily_stats,
            "capital":            config.TOTAL_CAPITAL,
            "lot_sizes":          config.LOT_SIZES,
            "news_and_events":    news_context,
            "monthly_progress": {
                "start_capital":    config.MONTHLY_START_CAPITAL,
                "current_capital":  config.TOTAL_CAPITAL,
                "monthly_pnl_rs":   round(monthly_pnl_rs, 0),
                "monthly_pnl_pct":  monthly_pnl_pct,
                "target_pct":       config.MONTHLY_TARGET_PCT * 100,
                "stretch_pct":      config.MONTHLY_STRETCH_PCT * 100,
                "target_rs":        round(monthly_target_rs, 0),
                "stretch_rs":       round(monthly_stretch_rs, 0),
                "remaining_to_target": monthly_remaining,
                "progress_pct":     monthly_progress,
                "on_track":         monthly_progress >= (now.day / 20 * 100),
            },
        }
        return json.dumps(packet, indent=2)
