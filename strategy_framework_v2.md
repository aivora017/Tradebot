# Options Buying Bot — Strategy Framework v2
# Sourav Shaw WAR ROOM | NSE Nifty + BankNifty | Updated 2026-04-01
# ─────────────────────────────────────────────────────────────────
# STATUS: STAGE 1 COMPLETE — Framework Document
# STAGE 2 NEXT: New strategy designs (S13-S15)
# STAGE 3 NEXT: Code implementation in strategy_engine.py
# ─────────────────────────────────────────────────────────────────

## SECTION 1 — BASELINE WIN RATES (Research-Verified)

| Approach                          | Win Rate | Source                          |
|-----------------------------------|----------|---------------------------------|
| Random ATM CE (no filter)         | 34.38%   | NSE 10yr data study             |
| Directional + volume filter       | 55–60%   | Multi-source backtests          |
| Gamma blast timed (expiry PM)     | 50–55%   | 0DTE analysis adapted to India  |
| VWAP reclaim + institutional bias | 58–62%   | FII flow + VWAP studies         |
| Straddle on high VIX event        | 45–55%   | IV crush vs move analysis       |
| Max pain fade (2–2:45 PM expiry)  | 52–58%   | NSE settlement mechanics        |

Key insight: Without at least 2 confirming signals, ATM CE win rate is ~34%.
Every strategy in this bot MUST stack 2+ signals before entry.

---

## SECTION 2 — MARKET CONDITION CLASSIFICATION

Classify market at 9:20 AM using morning_setup + market_scanner output.

| Market Type   | Signal Combination                                     | Best Strategies        |
|---------------|--------------------------------------------------------|------------------------|
| TRENDING      | GIFT gap >0.4%, FII >+2000Cr, EMA9 > EMA21, heavyweights aligned | S3, S4, S5, S10 |
| SIDEWAYS      | GIFT gap <0.2%, FII neutral, PCR 0.8–1.2, VIX 14–18   | S1, S6, S7, S2         |
| VOLATILE      | VIX >22, wide GIFT gap, heavyweights diverging         | S2, S9 (size 0.6×)     |
| EXPIRY_NIFTY  | date = last Thu of month (Nifty)                       | S9, S11, S12, S13, S14 |
| EXPIRY_BN     | date = last Tue of month (BankNifty)                   | S9, S11, S12, S13, S14 |

VIX Guard Rails:
- VIX < 12: SKIP all options buying — theta kills premium, no movement
- VIX 12–14: Size 0.7×, only T1 targets
- VIX 14–22: IDEAL ZONE — full size, all targets
- VIX 22–30: Size 0.8×, prefer straddles, tighter SLs
- VIX > 30: Size 0.5×, ATM only, NO OTM, NO strangle
- VIX > 40: Size 0.4×, spreads only (current bot: size 0.6× via extreme_vix multiplier)

---

## SECTION 3 — INDEX SELECTION RULES

| Condition                              | Pick           | Reason                                    |
|----------------------------------------|----------------|-------------------------------------------|
| Expiry day                             | That index     | Gamma/theta event = highest probability   |
| Non-expiry, BN momentum > N50 momentum | BANKNIFTY      | Higher ATR = bigger premium moves         |
| Non-expiry, both flat                  | NIFTY          | Cleaner moves, lower slippage             |
| VIX > 25                               | NIFTY          | BN OTM slippage 5–10%+ in volatile market |
| Heavyweight alert (HDFCBANK -1%+ early)| BANKNIFTY      | Outsized impact on BN                     |

ATR Reference:
- BankNifty: 150–250 pt avg daily ATR (options ATM premium ₹150–400)
- Nifty:      50–100 pt avg daily ATR  (options ATM premium ₹80–200)

---

## SECTION 4 — CORE INTRADAY STRATEGIES (S1–S10)

### S1 — Opening Range Breakout (ORB)
**Window:** 9:15–10:30 AM | **Type:** Trending, Gap days
**Entry:** First 15m candle (9:15–9:30) defines ORB range.
         Price breaks above ORB high on 5m close → CE
         Price breaks below ORB low on 5m close → PE
**Confirm:** Volume ≥ 1.2× avg AND VIX 14–25
**Strike:** ATM (delta 0.45–0.55)
**SL:** 30% of premium (tight — ORB is high conviction or fast fail)
**T1:** 80% gain (book 50%)
**T2:** 180% gain (full exit)
**Trail SL:** After T1 hit → SL to breakeven
**Time exit:** 10:45 AM if no T1 hit
**Edge:** Gap-up days +14% probability boost; FII bullish adds +8%

### S2 — ATM Straddle Buy
**Window:** 9:20–10:15 AM | **Type:** High VIX / pre-news / sideways
**Entry:** Buy ATM CE + ATM PE simultaneously when:
         VIX > 16 AND (PCR neutral 0.8–1.2 OR event in next 60 min)
**Strike:** Exact ATM for both legs
**SL:** 25% on COMBINED premium (not individual legs)
**T1:** 50% combined gain (book 60% of position)
**T2:** 120% combined gain
**Trail:** After T1 → trail combined SL at 20% gain
**Time exit:** 11:30 AM (IV crush window opens — MUST exit before)
**WARNING:** Never hold straddle past 11:30 AM on non-expiry day
**Edge:** Works best with VIX 18–28; fails in trending low-VIX markets

### S3 — Gap-and-Go
**Window:** 9:15–9:45 AM | **Type:** Trending gap days
**Entry:** Gap up >0.5% → CE on first green 5m candle after 9:20
         Gap down >0.5% → PE on first red 5m candle after 9:20
**Confirm:** GIFT Nifty aligned (same direction) AND FII not strongly opposite
**Strike:** ATM or 1 OTM (if gap >0.8%, OTM gives better R:R)
**SL:** 40% of premium
**T1:** 100% (book 50%)
**T2:** 250% (full exit)
**Trail:** After T1 → SL to breakeven
**Time exit:** 10:00 AM (gap-fills are quick; stale signals die fast)
**Failure:** Gap above/below prev close then immediate reversal → EXIT instantly
**Edge:** Works only first 30 min; do NOT re-enter same signal after 9:45

### S4 — Trend Continuation
**Window:** 9:30 AM–1:00 PM | **Type:** Trending market (trending=True)
**Entry:** Pull-back to EMA9 on 5m chart in trend direction
         Bull trend: price dips to EMA9, bounces, volume spike → CE
         Bear trend: price pops to EMA9, rejects, volume spike → PE
**Confirm:** EMA9 > EMA21 (bull) or EMA9 < EMA21 (bear) on 15m
            FII flow aligned OR heavyweight stocks aligned
**Strike:** ATM (trending days → ATM moves fastest)
**SL:** 35% of premium
**T1:** 90% (book 40%)
**T2:** 220% (remaining 60%)
**T3 (trend day):** 350% (if trend_day flag active — target upgraded)
**Trail:** T1 hit → SL to entry; T2 hit → trail at T1 level
**Time exit:** 1:00 PM
**Heavyweight boost:** If heavyweights aligned → T2 target bumped to T3 (350%)

### S5 — 15-Minute Candle Breakout
**Window:** 9:45 AM–11:30 AM | **Type:** Post-opening consolidation breaks
**Entry:** Last completed 15m candle high/low.
         Price + volume (≥1.3×) breaks above 15m high → CE
         Price + volume breaks below 15m low → PE
**Confirm:** Not in dead zone, VIX not <12
**Strike:** ATM or 1 ITM (ITM delta 0.60–0.65 gives better fill in volatile)
**SL:** 35% of premium
**T1:** 80% (book 50%)
**T2:** 200% (remaining)
**Trail:** After T1 → SL to cost
**Time exit:** 12:00 PM

### S6 — Support/Resistance Reversal
**Window:** 9:30 AM–1:00 PM | **Type:** Sideways / range-bound
**Entry:** Price hits key S/R level (from morning_setup levels dict) with:
         Rejection candle (engulf or pin bar on 5m) + volume >1.2× avg
         At support → CE | At resistance → PE
**Confirm:** PCR bullish at support (<0.7 for CE) or bearish at resistance (>1.3 for PE)
**Strike:** ATM
**SL:** 40% of premium
**T1:** 90% (book 50%)
**T2:** 220%
**Trail:** After T1 → SL to breakeven
**Time exit:** 13:00 PM
**Note:** Requires accurate S/R levels in config — check morning_setup output

### S7 — VWAP Reclaim / Rejection
**Window:** 9:30 AM–1:30 PM | **Type:** All conditions except extreme VIX
**Entry:**
  RECLAIM (bullish): Price was below VWAP, crosses back above on 5m close → CE
  REJECTION (bearish): Price was above VWAP, crosses back below on 5m close → PE
**Confirm:** At least 2 candles below/above before cross
            Volume ≥ 1.1× avg on cross candle
**Strike:** ATM
**SL:** 35% of premium
**T1:** 80% (book 50%)
**T2:** 180%
**Trail:** After T1 → SL to entry
**Time exit:** 2:00 PM
**Enhanced with FII:** If FII strongly bullish + VWAP reclaim → size 1.3× normal

### S8 — Engulfing Candle
**Window:** 9:30 AM–12:00 PM | **Type:** All conditions
**Entry:** Bullish engulf on 5m → CE | Bearish engulf on 5m → PE
         Engulf = current body fully covers prior candle body
**Confirm:** Volume > prior candle AND at key level or VWAP proximity
**Strike:** ATM
**SL:** 40% of premium
**T1:** 80% (book 50%)
**T2:** 200%
**Trail:** After T1 → SL to cost
**Time exit:** 12:30 PM (engulfing signals decay fast)

### S9 — Expiry Gamma Morning
**Window:** 9:15–10:30 AM, expiry days only | **Type:** Expiry-specific
**Entry:** Same as respective index expiry session + ATM straddle or directional
**Logic:** Gamma is highest at open on expiry days; early directional = highest R:R
**Strike:** ATM (NEVER OTM on gamma plays — need high delta for quick fills)
**SL:** 40%
**T1:** 130% (book 50%)
**T2:** 300%
**Trail:** After T1 → SL to cost
**Exit:** EXPIRY_EXIT_TIME (currently 14:00)
**Note:** On expiry days, S9 runs all day (EXPIRY_GAMMA_ALL_DAY=True)

### S10 — EMA Crossover
**Window:** 9:45 AM–1:30 PM | **Type:** Trending + sideways
**Entry:** EMA9 crosses above EMA21 on 5m chart → CE
          EMA9 crosses below EMA21 on 5m chart → PE
**Confirm:** 23 candles minimum required, volume spike on cross candle
**Strike:** ATM
**SL:** 35%
**T1:** 80% (book 50%)
**T2:** 200%
**Trail:** After T1 → SL to cost
**Time exit:** 1:30 PM

---

## SECTION 5 — EXPIRY-DAY STRATEGIES (S9, S11–S15)

### EXPIRY DAY TIME MAP

```
09:15 ─── S9 GAMMA MORNING ─────────── 10:30
09:30 ─── S13 PRE-11 STRADDLE ──────── 10:45
10:45 ─── DEAD (theta, no momentum) ── 13:15
13:15 ─── S11 EXPIRY AFTERNOON ─────── 15:00
14:00 ─── S14 MAX PAIN FADE ─────────── 14:45
14:15 ─── S12 EXPIRY CLOSE SQUEEZE ─── 15:10
14:30 ─── S15 SETTLEMENT SQUEEZE ───── 15:20
```

### THETA DECAY MAP (Expiry Day)
```
09:15 → 100% of morning premium exists
11:00 → ~60% of premium destroyed
14:00 → ~83% destroyed
15:00 → ~95% destroyed
15:20 → ~97–99% → AVOID BUYING
```
**Rule:** Never BUY options after 14:50 on expiry day.
**Rule:** T2 targets MUST be adjusted down after 14:00 (premium left is thin).

### S11 — Expiry Afternoon Gamma (ALREADY CODED)
**Window:** 13:15–15:00 | **Cooldown:** 120s
**Already implemented in strategy_engine.py**
**Parameters:** SL 40%, T1 150%, T2 350%, exit EXPIRY_EXIT_TIME
**Entry logic:** Trend direction from last 4 candles (5m). CE/PE/straddle.

### S12 — Expiry Close Squeeze (ALREADY CODED)
**Window:** 14:15–15:10 | **Cooldown:** 120s
**Already implemented in strategy_engine.py**
**Parameters:** SL 50%, T1 200%, T2 500%, exit 15:10

### S13 — Pre-11 Expiry Straddle [NEW — TO IMPLEMENT]
**Window:** 9:30–10:45 AM, expiry days only
**Rationale:** On expiry, market is often indecisive 9:30–10:30 AM. ATM
             straddle captures the breakout whichever direction it goes.
             After 10:45 AM theta accelerates — must exit before.
**Entry trigger:**
  - is_expiry_day() = True
  - VIX > 15 (need some movement for straddle to work)
  - PCR between 0.75 and 1.25 (neutral — if PCR is extreme, go directional instead)
  - No open expiry trades already (avoid overlap)
**Strike:** Exact ATM for both CE and PE
**Position size:** 0.7× normal size (expiry straddle risk control)
**SL:** 30% of COMBINED premium
**T1:** 60% combined gain (book 70% of position — take profits early)
**T2:** 150% combined gain (remaining 30%)
**Trail:** After T1 → trail combined at 40% gain
**Time exit:** 10:45 AM HARD EXIT (regardless of P&L — theta wall)
**Note:** If one leg is up 80%+ before T1 trigger, close that leg and trail PE as breakeven

### S14 — Max Pain Pin Fade [NEW — TO IMPLEMENT]
**Window:** 14:00–14:50 PM, expiry days only
**Rationale:** NSE settlement is VWAP of 3:00–3:30 PM. Market makers pin
             the index toward max pain level after 2 PM. If current price
             is >100 pts from max pain (BN) or >40 pts (Nifty), directional
             toward max pain is statistically advantageous.
**Entry trigger:**
  - is_expiry_day() = True
  - time_between("14:00", "14:50")
  - BankNifty: |current_price - max_pain| > 100
  - Nifty: |current_price - max_pain| > 40
  - Direction: if price > max_pain → PE (price will fall toward pain)
               if price < max_pain → CE (price will rise toward pain)
  - Volume confirmation: last 5m vol ≥ 1.0× avg (no extra bar needed — just not dead)
**Strike:** ATM (market moving fast — need delta)
**Position size:** 0.8× (expiry caution)
**SL:** 45% of premium
**T1:** 100% gain (book 60%)
**T2:** 250% gain
**Time exit:** 14:50 PM HARD (before final volatility spike)
**Data needed:** config.MAX_PAIN_BN and config.MAX_PAIN_NIFTY (set in morning_setup.py)
**Note:** max_pain available from Upstox option chain data (max_pain_strike field)

### S15 — Settlement Squeeze [NEW — TO IMPLEMENT]
**Window:** 14:35–15:20 PM, expiry days only
**Rationale:** Final 15m of expiry. NSE settlement = VWAP of 3:00–3:30.
             Large MM position adjustments create last-minute directional
             momentum. Only trade if clear trend momentum exists.
**Entry trigger:**
  - is_expiry_day() = True
  - time_between("14:35", "15:20")
  - Strong directional move: last 2 candles (5m) both same color + vol ≥1.5× avg
  - PCR extreme: < 0.65 (CE) or > 1.35 (PE) — confirms institutional direction
  - VIX not >35 (extreme VIX = too much slippage)
**Strike:** ATM ONLY (near-expiry OTM = essentially zero — avoid)
**Position size:** 0.5× (high risk window, small size)
**SL:** 50% of premium (wide SL — premium is thin, small absolute loss)
**T1:** 150% (book 70%)
**T2:** 400%
**Time exit:** 15:20 HARD EXIT — no exceptions
**WARNING:** After 15:20, premium collapses to near-zero. Any open position
            must be force-closed regardless of P&L.

---

## SECTION 6 — TRADE LIFECYCLE MANAGEMENT (Universal Rules)

### 6.1 Entry Execution
1. Get signal from strategy engine
2. Risk manager validates: VIX check, daily loss limit, open trade count
3. ATM strike calculation: round(price / strike_gap) × strike_gap
4. Order: Market order for entry (accept 0.3–0.5% slippage on BN, 0.1–0.2% on N)
5. Record entry context to bot_memory immediately
6. Set initial SL in trade_tracker (abs premium × SL_pct)

### 6.2 Position Sizing Matrix

| Condition              | Size Multiplier | Rationale                            |
|------------------------|-----------------|--------------------------------------|
| VIX < 12               | 0 (SKIP)        | Theta kills premium, no movement     |
| VIX 12–14              | 0.7×            | Low vol, reduced size                |
| VIX 14–22              | 1.0× (base)     | Ideal zone                           |
| VIX 22–30              | 0.8×            | Elevated risk                        |
| VIX 30–40              | 0.6×            | Extreme, spreads preferred           |
| VIX > 40               | 0.4×            | Crisis — very small ATM only         |
| Expiry straddle (S13)  | 0.7×            | Two legs = higher total risk         |
| Expiry late (S14/S15)  | 0.5–0.8×        | Thin premium, fast moves             |
| FII strongly aligned   | 1.2×            | Institutional tailwind               |
| Trend day detected     | 1.1×            | Momentum confirmation                |
| GODMODE confidence     | 1.3× (cap)      | Claude highest conviction            |

### 6.3 Stop Loss Rules
- Initial SL: Set as pct of premium at entry (per-strategy table above)
- Trail to breakeven: When T1 hit → SL to 0% loss (cost price)
- Trail to T1: When T2 hit → SL moves to T1 level (lock T1 gain)
- Hard time exit: Each strategy has max hold time — ENFORCE regardless of P&L
- Emergency exit: If index moves >2% in <10 min in opposite direction → exit ALL

### 6.4 Partial Booking (T1)
- All strategies: Book 40–70% of position at T1 (per-strategy table)
- Rationale: Lock partial gain, reduce cost basis, let rest ride to T2
- Implementation: In trade_tracker — T1_HIT status → partial_close() then trail SL

### 6.5 Target Levels Philosophy
- T1 = High probability capture (60–70% chance to hit based on research)
- T2 = Full R:R capture (35–45% chance, needs trend confirmation)
- T3 = Trend day bonus (20–30% chance, only when trend_day=True)
- Never target T3 on sideways/expiry-close sessions

### 6.6 Time-Based Exit Rules (MUST ENFORCE)
| Strategy | Hard Exit     | Reason                                  |
|----------|---------------|-----------------------------------------|
| S1 ORB   | 10:45 AM      | Gap momentum dies within 90 min         |
| S2       | 11:30 AM      | IV crush window — straddle dies         |
| S3       | 10:00 AM      | Gap-go is a 30-min trade only           |
| S4-S8    | 1:00–2:00 PM  | Secondary window dead zone              |
| S9 Gamma | EXPIRY_EXIT   | Keep full day on expiry                 |
| S11      | 15:00         | Market close approach                   |
| S12      | 15:10         | Hard close                              |
| S13      | 10:45 AM      | Theta acceleration begins               |
| S14      | 14:50 PM      | Pre-final-volatility-spike              |
| S15      | 15:20 PM      | NSE settlement begins                   |

### 6.7 Emergency Exit Conditions (Override All Rules)
1. index_move > 2.0% opposite direction in < 10 min → exit ALL open options
2. VIX spikes above 40 suddenly → exit ALL
3. Daily loss > MAX_DAILY_LOSS_PCT → STOP for the day
4. trade_count >= MAX_DAILY_TRADES → STOP for the day
5. GIFT Nifty gap reversal (was +0.6% now -0.3%) → exit all morning longs

---

## SECTION 7 — STRATEGY EFFECTIVENESS BY MARKET TYPE

| Strategy | TRENDING | SIDEWAYS | VOLATILE | EXPIRY |
|----------|----------|----------|----------|--------|
| S1 ORB   | ✅ +8%   | ✅       | ❌ False breaks | ✅    |
| S2 Strad | ❌       | ✅✅     | ✅✅     | ✅ S13 variant |
| S3 Gap   | ✅✅     | ❌       | ⚠️ Gap fill risk | ✅  |
| S4 Trend | ✅✅✅  | ❌       | ❌       | ✅    |
| S5 15m   | ✅       | ✅       | ⚠️       | ✅    |
| S6 S/R   | ❌       | ✅✅     | ❌       | ⚠️    |
| S7 VWAP  | ✅       | ✅✅     | ⚠️       | ✅    |
| S8 Engulf| ✅       | ✅       | ❌       | ⚠️    |
| S9 Gamma | N/A      | N/A      | N/A      | ✅✅✅ |
| S10 EMA  | ✅✅     | ⚠️       | ❌       | ✅    |
| S11      | N/A      | N/A      | N/A      | ✅✅✅ |
| S12      | N/A      | N/A      | N/A      | ✅✅✅ |
| S13 [NEW]| N/A      | N/A      | N/A      | ✅✅   |
| S14 [NEW]| N/A      | N/A      | N/A      | ✅✅✅ |
| S15 [NEW]| N/A      | N/A      | N/A      | ✅✅   |

---

## SECTION 8 — PCR SIGNAL INTEGRATION

| PCR Range    | Market Signal        | Strategy Bias                        |
|--------------|----------------------|--------------------------------------|
| < 0.65       | Strong bullish       | CE preferred; PE only on reversal    |
| 0.65–0.80    | Mild bullish         | CE slight edge                       |
| 0.80–1.20    | Neutral              | Straddle / wait for breakout         |
| 1.20–1.35    | Mild bearish         | PE slight edge                       |
| > 1.35       | Strong bearish       | PE preferred; CE only on reversal    |
| > 1.50       | CONTRARIAN_BULLISH   | Extreme PE OI = CE reversal possible |
| < 0.55       | CONTRARIAN_BEARISH   | Extreme CE OI = PE reversal possible |

On expiry day: Use CHANGE in OI PCR (COI PCR), not just OI PCR.
If COI PCR diverges from OI PCR — trust COI PCR.

---

## SECTION 9 — IMPROVEMENTS TO EXISTING S1–S10

Based on research, apply these improvements to current strategies:

1. **S1 ORB:** Add PCR confirmation (CE only if PCR < 1.2; PE only if PCR > 0.8)
2. **S3 Gap:** Reject gap if overnight news = negative (news_feed has events)
3. **S4 Trend:** Heavyweight alignment check already coded (S4 already upgraded)
4. **S7 VWAP:** Add FII multiplier (1.3× size when FII strongly aligned)
5. **S9 Gamma:** Already runs all day on expiry via EXPIRY_GAMMA_ALL_DAY
6. **All strategies:** PCR extreme alert = override signal (if contrarian signal active, skip or reduce size 0.5×)

---

## SECTION 10 — IMPLEMENTATION CHECKLIST

### New Strategies to Code in strategy_engine.py:
- [ ] S13 _s13_expiry_pre11_straddle()
- [ ] S14 _s14_max_pain_fade()
- [ ] S15 _s15_settlement_squeeze()

### Config params to add (config.py):
- [ ] MAX_PAIN_BN = 0 (updated by morning_setup.py each expiry day)
- [ ] MAX_PAIN_NIFTY = 0 (updated by morning_setup.py each expiry day)
- [ ] MAX_PAIN_THRESHOLD_BN = 100 (pts away from max pain to trigger S14)
- [ ] MAX_PAIN_THRESHOLD_NIFTY = 40
- [ ] S13_STRADDLE_VIX_MIN = 15.0
- [ ] S13_STRADDLE_PCR_MIN = 0.75
- [ ] S13_STRADDLE_PCR_MAX = 1.25
- [ ] S15_SETTLEMENT_VOL_MULT = 1.5
- [ ] S15_SETTLEMENT_PCR_BULL = 0.65
- [ ] S15_SETTLEMENT_PCR_BEAR = 1.35

### morning_setup.py:
- [ ] Extract max_pain_strike from Upstox option chain for both BN and Nifty on expiry days
- [ ] Write to config.MAX_PAIN_BN and config.MAX_PAIN_NIFTY

### claude_analyst.py SYSTEM prompt:
- [ ] Add S13/S14/S15 awareness block
- [ ] Add max pain pinning mechanic explanation
- [ ] Add settlement squeeze rationale

---

## SECTION 11 — RISK-REWARD SUMMARY TABLE

| Strategy | SL%  | T1%  | T2%  | Approx WR | Expected Value per ₹100 risked |
|----------|------|------|------|-----------|-------------------------------|
| S1 ORB   | -30% | +80% | +180%| 55%       | +₹22                          |
| S2 Strad | -25% | +50% | +120%| 48%       | +₹12                          |
| S3 Gap   | -40% | +100%| +250%| 52%       | +₹28                          |
| S4 Trend | -35% | +90% | +220%| 58%       | +₹38                          |
| S5 15m   | -35% | +80% | +200%| 52%       | +₹24                          |
| S6 S/R   | -40% | +90% | +220%| 50%       | +₹20                          |
| S7 VWAP  | -35% | +80% | +180%| 55%       | +₹27                          |
| S8 Engulf| -40% | +80% | +200%| 50%       | +₹18                          |
| S9 Gamma | -40% | +130%| +300%| 52%       | +₹46                          |
| S10 EMA  | -35% | +80% | +200%| 53%       | +₹26                          |
| S11 ExpAM| -40% | +150%| +350%| 50%       | +₹55                          |
| S12 Close| -50% | +200%| +500%| 45%       | +₹82                          |
| S13 [NEW]| -30% | +60% | +150%| 50%       | +₹15                          |
| S14 [NEW]| -45% | +100%| +250%| 55%       | +₹37                          |
| S15 [NEW]| -50% | +150%| +400%| 48%       | +₹52                          |

Note: EV = (WR × avg_gain) - ((1-WR) × SL%). T1 booked 50-70%, remaining rides to T2.
Highest EV strategies: S12 > S11 > S9 > S15 > S14 (all expiry-day)
Best non-expiry: S4 Trend > S3 Gap > S7 VWAP

---

## SECTION 12 — STAGE COMPLETION LOG

[2026-04-01] Stage 1 COMPLETE — Framework document written (Sections 1–12)
[ ] Stage 2 — new_strategy_designs.md (S13/S14/S15 detailed code design)
[ ] Stage 3 — strategy_engine.py implementation
[ ] Stage 4 — AST verification + functional tests
