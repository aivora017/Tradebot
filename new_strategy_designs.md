# New Strategy Designs — S13 / S14 / S15
# WAR ROOM | Stage 2 Checkpoint | 2026-04-01
# ─────────────────────────────────────────────
# STATUS: DESIGN COMPLETE — ready for strategy_engine.py implementation

## S13 — Expiry Pre-11 Straddle
Function:  _s13_expiry_pre11_straddle
Window:    09:30 – 10:45 (expiry days only)
Direction: BOTH (ATM straddle)
Tier:      T4
Logic:
  - is_nifty_expiry() or is_banknifty_expiry()
  - time_between("09:30", "10:45")
  - VIX > config.S13_STRADDLE_VIX_MIN (15.0)
  - PCR between config.S13_STRADDLE_PCR_MIN (0.75) and config.S13_STRADDLE_PCR_MAX (1.25)
    → if PCR unavailable, skip check (fire on VIX alone)
  - len(c5) >= 3
SL:        0.30 (combined premium)
T1:        0.60 (book 70%)
T2:        1.50
Exit:      "10:45"  ← HARD — theta wall hits after this
Size:      config.RISK_PER_TRADE_T4 * 0.7  (~17.5% capital)
PCR source: self.feed.get_pcr(symbol) with try/except → 0 fallback

## S14 — Max Pain Fade
Function:  _s14_max_pain_fade
Window:    14:00 – 14:50 (expiry days only)
Direction: CE if price < max_pain | PE if price > max_pain
Tier:      T5
Logic:
  - is_nifty_expiry() or is_banknifty_expiry()
  - time_between("14:00", "14:50")
  - sym_key = "BANKNIFTY" / "NIFTY"
  - max_pain = config.KEY_LEVELS.get(sym_key, {}).get("max_pain", 0)
  - threshold = config.MAX_PAIN_THRESHOLD_BN (100) or config.MAX_PAIN_THRESHOLD_NIFTY (40)
  - |price - max_pain| > threshold AND max_pain != 0
  - Direction: price > max_pain → PE | price < max_pain → CE
SL:        0.45
T1:        1.00 (book 60%)
T2:        2.50
Exit:      "14:50"  ← HARD before final volatility spike
Size:      config.RISK_PER_TRADE_T5 * 0.8  (~16% capital)

## S15 — Settlement Squeeze
Function:  _s15_settlement_squeeze
Window:    14:35 – 15:20 (expiry days only)
Direction: CE or PE (directional only — NO straddle this late)
Tier:      T5
Logic:
  - is_nifty_expiry() or is_banknifty_expiry()
  - time_between("14:35", "15:20")
  - VIX <= 35 (extreme VIX = skip — slippage too high)
  - len(c5) >= 3
  - Last 2 completed 5m candles both same direction (bullish → CE | bearish → PE)
  - volume_mult(c5, 10) >= config.S15_SETTLEMENT_VOL_MULT (1.5)
  - PCR extreme: pcr_val < config.S15_SETTLEMENT_PCR_BULL (0.65) for CE
                 pcr_val > config.S15_SETTLEMENT_PCR_BEAR (1.35) for PE
    → if PCR unavailable, skip PCR check (use momentum alone)
SL:        0.50  (wide — premium thin, absolute loss small)
T1:        1.50 (book 70%)
T2:        4.00
Exit:      "15:20"  ← HARD — NSE settlement begins 15:30
Size:      config.RISK_PER_TRADE_T5 * 0.5  (~10% capital)

## Config Params to Add
MAX_PAIN_THRESHOLD_BN        = 100    # pts from max pain triggers S14 on BankNifty
MAX_PAIN_THRESHOLD_NIFTY     = 40     # pts from max pain triggers S14 on Nifty
S13_STRADDLE_VIX_MIN         = 15.0   # min VIX for pre-11 straddle
S13_STRADDLE_PCR_MIN         = 0.75   # PCR lower bound (neutral zone)
S13_STRADDLE_PCR_MAX         = 1.25   # PCR upper bound (neutral zone)
S15_SETTLEMENT_VOL_MULT      = 1.5    # vol multiplier for settlement squeeze
S15_SETTLEMENT_PCR_BULL      = 0.65   # PCR below this → CE signal
S15_SETTLEMENT_PCR_BEAR      = 1.35   # PCR above this → PE signal

## Changes to strategy_engine.py
1. runners list: add _s13, _s14, _s15
2. _EXPIRY_AFTERNOON_STRATEGIES: add all 3 new names
3. Add methods S13/S14/S15 after S12, before # ── Helpers ───

[2026-04-01] Stage 2 COMPLETE — design doc written
