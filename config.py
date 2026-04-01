# ================================================================
#  config.py — MASTER CONFIGURATION
#  WAR ROOM | Nifty & BankNifty Option Buying Bot
#  Edit this file before running. All settings live here.
# ================================================================

import os
from dataclasses import dataclass, field
from typing import Dict, List
from dotenv import load_dotenv

load_dotenv()

# ── BROKER — UPSTOX ─────────────────────────────────────────────
UPSTOX_API_KEY      = os.getenv("UPSTOX_API_KEY",      "")
UPSTOX_API_SECRET   = os.getenv("UPSTOX_API_SECRET",   "")
UPSTOX_ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")
UPSTOX_REDIRECT_URI = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1")
UPSTOX_BASE_URL     = "https://api.upstox.com/v2"
UPSTOX_BASE_URL_V3  = "https://api.upstox.com/v3"
UPSTOX_WS_URL       = "wss://api.upstox.com/v3/feed/market-data-feed"

# ── CLAUDE AI ────────────────────────────────────────────────────
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL         = "claude-sonnet-4-6"
CLAUDE_MAX_TOKENS    = 700     # JSON response rarely exceeds 500 tokens — was 1200

# ── TELEGRAM ALERTS ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID",   "")
TELEGRAM_ENABLED    = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ── CAPITAL & SIZING ─────────────────────────────────────────────
TOTAL_CAPITAL        = float(os.getenv("TRADING_CAPITAL", "50000"))

# ── AGGRESSIVE POSITION SIZING — strength-based capital deployment ─
# No fixed % cap. Deploy available capital based on signal conviction.
# lots = floor(available_capital × strength_pct / (premium × lot_size))
# Minimum 2 lots always. No upper lot limit.
STRENGTH_CAPITAL_PCT = {
    "GODMODE":  0.50,   # 50% of available — highest conviction only
    "STRONG":   0.35,   # 35% of available
    "MODERATE": 0.25,   # 25% of available
    "WEAK":     0.15,   # 15% of available — still trades, just smaller
}

# Legacy fields kept for risk_manager backward compat — now overridden by STRENGTH_CAPITAL_PCT
RISK_PER_TRADE_T1_3  = 0.35    # fallback for T1-3 if strength unavailable
RISK_PER_TRADE_T4    = 0.25    # fallback for straddles
RISK_PER_TRADE_T5    = 0.20    # fallback for gamma plays

MAX_DAILY_LOSS_PCT   = 0.05    # 5% daily → HARD STOP — the only real constraint
MAX_WEEKLY_LOSS_PCT  = 0.10    # 10% weekly loss limit
MAX_CONCURRENT       = 3       # max 3 simultaneous trades — was 2
MAX_DAILY_TRADES     = 10      # max trades per day — was 6
LOSS_STREAK_STOP     = 3       # 3 consecutive losses → stop day (protects against blow-up)
MIN_LOTS_PER_TRADE   = 2       # minimum 2 lots — non-negotiable
DAILY_TARGET_PCT     = 0.10    # 10% daily soft target — after hit, T3+ blocked, T1/T2 continue

# ── MONTHLY TARGETS ───────────────────────────────────────────────
# Trading days per month ≈ 20. Monthly goal: 60% minimum (aggressive compounding)
MONTHLY_TARGET_PCT   = 0.60    # 60% minimum monthly return target
MONTHLY_STRETCH_PCT  = 1.20    # 120% stretch goal (best case month)
# Set this to the capital value at START of current month (update on 1st of each month)
MONTHLY_START_CAPITAL = float(os.getenv("MONTHLY_START_CAPITAL",
                                         os.getenv("TRADING_CAPITAL", "50000")))

# ── OPTION SL / TARGET RULES ─────────────────────────────────────
SL_PREMIUM_T1_3      = 0.25    # 25% SL on premium — slightly wider, less noise stops
SL_PREMIUM_T4        = 0.30    # 30% SL (straddle)
SL_PREMIUM_T5        = 0.50    # 50% SL (gamma play — wide intentionally, moves are explosive)
T1_BOOK_QTY_PCT      = 0.50    # book 50% quantity at T1
T1_PREMIUM_GAIN      = 0.50    # T1 = 50% premium gain → book partial + trail SL to breakeven
T2_PREMIUM_GAIN      = 1.20    # T2 = 120% premium gain (was 80%) — let winners run
# Trail SL: after T1 hit, remaining position SL moves to entry premium (breakeven)
TRAIL_SL_TO_BREAKEVEN = True   # enabled — locks in T1 profit, T2 becomes free money

# ── TIME RULES ────────────────────────────────────────────────────
# Aggressive: trade the full session. Dead zone REMOVED as hard block.
# Only 1 hard rule remains: exit before market close to avoid illiquid exits.
MARKET_OPEN           = "09:15"
ORB_WINDOW_END        = "09:20"
PRIMARY_ENTRY_START   = "09:20"
PRIMARY_ENTRY_END     = "13:00"   # was 11:00 — full morning session
LATE_ENTRY_END        = "14:30"
DEAD_ZONE_START       = "13:00"   # not a hard block — reduced confidence only (see risk_manager)
DEAD_ZONE_END         = "13:15"   # 15-min lunch skip, not 2 hours
SECONDARY_ENTRY_START = "13:15"
SECONDARY_ENTRY_END   = "15:00"
HARD_EXIT_TIME        = "15:00"   # was 14:00 — captures afternoon momentum + expiry gamma
EXPIRY_EXIT_TIME      = "15:10"   # expiry day: stay until 15:10 for gamma explosion close
MARKET_CLOSE          = "15:30"
PRE_MARKET_SCAN_TIME  = "08:45"

# ── VIX FRAMEWORK ────────────────────────────────────────────────
# As an aggressive OPTION BUYER: high VIX = high premium + big moves = opportunity.
# We NEVER zero out trades on high VIX. We adjust strategy, not stop trading.
VIX_AVOID             = 11.0   # truly complacent market — no premium worth buying
VIX_REDUCED_SIZE      = 14.0   # 11–14 = 60% size (low vol, options cheap but moves small)
VIX_IDEAL_LOW         = 14.0   # 14–28 = FULL SIZE — this is our zone
VIX_IDEAL_HIGH        = 28.0   # extended from 22 — current Indian market is 18–25
VIX_STRADDLE_ZONE     = 22.0   # above 22 = prefer straddles alongside directional plays
VIX_SPREAD_ONLY       = 40.0   # above 40 = extreme crisis — prefer straddles, 60% size (NOT zero)

# Expiry day mode — all-day gamma, extended exit, reduced cooldown
EXPIRY_GAMMA_ALL_DAY  = True   # S9 gamma fires all session, not just 9:15–9:35
EXPIRY_COOLDOWN_SECS  = 120    # faster re-entry on expiry (vs 300s normal)

# ── S13: Pre-11 Expiry Straddle ───────────────────────────────────
S13_STRADDLE_VIX_MIN         = 15.0   # min VIX to fire pre-11 straddle
S13_STRADDLE_PCR_MIN         = 0.75   # PCR neutral lower bound
S13_STRADDLE_PCR_MAX         = 1.25   # PCR neutral upper bound

# ── S14: Max Pain Fade ────────────────────────────────────────────
MAX_PAIN_THRESHOLD_BN        = 100    # BankNifty: pts away from max_pain triggers S14
MAX_PAIN_THRESHOLD_NIFTY     = 40     # Nifty: pts away from max_pain triggers S14

# ── S15: Settlement Squeeze ───────────────────────────────────────
S15_SETTLEMENT_VOL_MULT      = 1.5    # min vol multiplier for last-30min squeeze
S15_SETTLEMENT_PCR_BULL      = 0.65   # PCR below this = strong CE bias
S15_SETTLEMENT_PCR_BEAR      = 1.35   # PCR above this = strong PE bias

# Session bias — set at market open from morning scan, flows into signal scoring
# Values: "BULLISH" | "BEARISH" | "NEUTRAL"
SESSION_BIAS          = "NEUTRAL"   # updated at runtime by morning scan
SESSION_BIAS_MULTIPLIER = 1.4       # aligned signals get tier score × 1.4
SESSION_COUNTER_MULTIPLIER = 0.7    # counter-trend signals get tier score × 0.7

# Trend day detection — 3+ same-direction signals in 60 min = trend day
TREND_DAY_SIGNAL_COUNT  = 3         # triggers trend day mode
TREND_DAY_WINDOW_MINS   = 60        # rolling window to count signals
TREND_DAY_T2_MULTIPLIER = 1.5       # T2 target extends by 1.5× on trend day
TREND_DAY_COOLDOWN_SECS = 150       # faster entries on trend days

# Claude API — signal-driven, not timer-driven
CLAUDE_CALL_INTERVAL    = 600       # heartbeat interval when no signals (was 120)
CLAUDE_DEAD_ZONE_PAUSE  = True      # no Claude calls 13:00–13:15 (lunch skip)

# ── NSE INSTRUMENT KEYS ──────────────────────────────────────────
NIFTY_INDEX_KEY       = "NSE_INDEX|Nifty 50"
BANKNIFTY_INDEX_KEY   = "NSE_INDEX|Nifty Bank"
VIX_INDEX_KEY         = "NSE_INDEX|India VIX"
NIFTY_FUT_KEY         = "NSE_FO|NIFTY"
BANKNIFTY_FUT_KEY     = "NSE_FO|BANKNIFTY"

# ── HEAVYWEIGHT STOCK KEYS (P2.5) ─────────────────────────────────
# Subscribed via the same WebSocket — ticks flow through data_feed
# Stored as _ticks["HDFCBANK"], _ticks["SBIN"], etc. (raw_sym passthrough)
HDFCBANK_KEY          = "NSE_EQ|HDFCBANK"
SBIN_KEY              = "NSE_EQ|SBIN"
ICICIBANK_KEY         = "NSE_EQ|ICICIBANK"
RELIANCE_KEY          = "NSE_EQ|RELIANCE"
INFY_KEY              = "NSE_EQ|INFY"
TCS_KEY               = "NSE_EQ|TCS"

BANK_HEAVYWEIGHTS     = ["HDFCBANK", "SBIN", "ICICIBANK"]   # BankNifty drivers
NIFTY_HEAVYWEIGHTS    = ["RELIANCE", "INFY", "TCS"]          # Nifty drivers

HW_DROP_ALERT_PCT     = 1.0   # HDFCBANK single-stock % drop in first 15m → BN PUT elevate
HW_AGREE_COUNT        = 2     # min stocks moving same direction = directional signal
HW_SCAN_CACHE_SECS    = 15    # re-compute heavyweight scan at most once per 15s

WATCH_KEYS = [
    NIFTY_INDEX_KEY,
    BANKNIFTY_INDEX_KEY,
    VIX_INDEX_KEY,
    # ── Heavyweight stocks (P2.5) ─────────────────────────────────
    HDFCBANK_KEY,
    SBIN_KEY,
    ICICIBANK_KEY,
    RELIANCE_KEY,
    INFY_KEY,
    TCS_KEY,
]

# ── LOT SIZES (SEBI 2024 UPDATE — CORRECTED) ─────────────────────
# IMPORTANT: These were updated by SEBI in Nov 2024 effective April 2025
# Old: NIFTY=25, BANKNIFTY=15 — those are WRONG now
LOT_SIZES = {
    "NIFTY":     75,    # Updated April 2025
    "BANKNIFTY": 30,    # Updated April 2025
}

# ── EXPIRY SCHEDULE (UPDATED SEP 2025 + SEBI NOV 2024) ───────────
# SEBI Nov 2024: BankNifty WEEKLY expiry discontinued. BN = MONTHLY only.
# NSE Sep 2025: All expiries moved to Tuesday.
# Nifty:     Weekly expiry every TUESDAY
# BankNifty: Monthly expiry on LAST TUESDAY of each month (no weekly)
NIFTY_EXPIRY_WEEKDAY      = 1   # Tuesday — weekly
BANKNIFTY_EXPIRY_WEEKDAY  = 1   # Tuesday — but MONTHLY only (last Tue of month)
BANKNIFTY_EXPIRY_MONTHLY  = True  # Flag: BN is monthly, not weekly

# ── KEY S/R LEVELS (update every morning pre-market) ─────────────
KEY_LEVELS: Dict[str, Dict[str, List[float]]] = {
    "BANKNIFTY": {
        "resistance": [53000.0, 53700.0, 54100.0],  # Auto-updated 01 Apr 13:29
        "support":    [50800.0, 51000.0, 51500.0],
        "max_pain":   55000.0,
    },
    "NIFTY": {
        "resistance": [22900.0, 23000.0, 23050.0],  # Auto-updated 01 Apr 13:29
        "support":    [22300.0, 22550.0, 22700.0],
        "max_pain":   23100.0,
    },
}

# ── STRATEGY PARAMETERS ──────────────────────────────────────────
# Loosened from conservative → aggressive. More signals fire. Less filtering.
@dataclass
class Params:
    # ORB Strategy — was 2.0 vol, too rare
    ORB_VOLUME_MULT:         float = 1.5    # was 2.0
    ORB_MAX_GAP_PCT:         float = 0.012  # was 0.008 — allow bigger gaps
    ORB_NIFTY_MAX_RANGE:     float = 120.0  # was 80 — wider ORB range valid
    ORB_BN_MAX_RANGE:        float = 350.0  # was 250

    # Gap and Go — loosen volume, allow wider gap
    GAP_MIN_PCT:             float = 0.002  # was 0.003 — smaller gaps also valid
    GAP_MAX_PCT:             float = 0.015  # was 0.008 — large gaps = trend day
    GAP_PULLBACK_HOLD:       float = 0.40   # was 0.50 — less pullback needed
    GAP_VOL_MULT:            float = 1.1    # was 1.3

    # VWAP Pullback
    VWAP_SL_BREACH_PCT:      float = 0.004  # was 0.003 — give more room
    VWAP_VOL_MULT:           float = 1.1    # was 1.3

    # EMA Crossover — RSI filter was too tight (55/45)
    EMA_FAST:                int   = 9
    EMA_SLOW:                int   = 21
    EMA_CE_RSI_MIN:          float = 50.0   # was 55 — fire on neutral RSI too
    EMA_PE_RSI_MAX:          float = 50.0   # was 45 — symmetric
    EMA_VOL_MULT:            float = 1.2    # was 1.5

    # S/R Reversal
    SR_NIFTY_TOLERANCE:      float = 40.0   # was 30
    SR_BN_TOLERANCE:         float = 100.0  # was 80

    # Engulfing (BankNifty)
    SCALP_SL_PREMIUM:        float = 0.20   # was 0.15 — more breathing room
    SCALP_VOL_MULT:          float = 1.1    # was 1.3

    # Gamma (expiry day) — all-day, not just open
    GAMMA_MAX_PREMIUM_PCT:   float = 0.008  # was 0.005 — allow pricier ATM options
    GAMMA_NIFTY_MOVE_MIN:    float = 60.0   # was 80 — smaller move enough on expiry
    GAMMA_BN_MOVE_MIN:       float = 150.0  # was 200

    # Straddle
    STRADDLE_ENTRY_MINS_PRE: int   = 20
    STRADDLE_MAX_IV_PCTILE:  float = 85.0   # was 80 — allow slightly elevated IV
    STRADDLE_COMBINED_SL:    float = 0.30   # was 0.25

    # PCR signals
    PCR_BULLISH_THRESH:      float = 1.2
    PCR_BEARISH_THRESH:      float = 0.8
    PCR_EXTREME_BULL:        float = 1.5
    PCR_EXTREME_BEAR:        float = 0.6

    # Volume averaging window
    VOL_AVG_WINDOW:          int   = 20

PARAMS = Params()

# ── EXECUTION MODE ────────────────────────────────────────────────
# "LIVE"   — actually place orders via Upstox API
# "PAPER"  — simulate trades, no real orders
# "SIGNAL" — show signals only, you trade manually
EXECUTION_MODE = os.getenv("EXECUTION_MODE", "PAPER")

# ── GIFT NIFTY PRE-MARKET BIAS (P2.2) ────────────────────────────
# Updated by morning_update.py every session before main.py starts.
# DO NOT edit manually — morning_update.py owns these values.
# BIAS values: BULLISH_GAP | MILD_BULL | FLAT | MILD_BEAR | BEARISH_GAP | UNAVAILABLE
GIFT_NIFTY_PRICE      = 0.0
GIFT_NIFTY_GAP_PCT    = 0.0
GIFT_NIFTY_BIAS       = "UNAVAILABLE"
GIFT_NIFTY_PREV_CLOSE = 0.0
GIFT_NIFTY_UPDATED    = ""          # "HH:MM IST" timestamp of last successful fetch
# Gap thresholds
GIFT_GAP_STRONG = 1.5   # % — above = BULLISH_GAP / below = BEARISH_GAP
GIFT_GAP_MILD   = 0.3   # % — above = MILD_BULL/BEAR, below = FLAT

# ── PCR SIGNAL THRESHOLDS (P2.4) ──────────────────────────────────
# Computed from ATM ±PCR_ATM_STRIKES strikes in live option chain.
# compute_pcr() in utils.py applies these thresholds.
PCR_BULLISH      = 1.2    # PCR > 1.2  → put writers active, index supported, CE bias
PCR_BEARISH      = 0.8    # PCR < 0.8  → call writers cap index, PE bias
PCR_EXTREME_BEAR = 1.5    # PCR > 1.5  → extreme put selling → contrarian BEARISH alert
PCR_EXTREME_BULL = 0.6    # PCR < 0.6  → extreme call buying  → contrarian BULLISH alert
PCR_ATM_STRIKES  = 10     # number of strikes each side of ATM to include

# ── FII/DII INSTITUTIONAL FLOW (P2.3) ─────────────────────────────
# NSE publishes provisional FII/DII data ~11 AM IST every trading day.
# fetch_fii_dii() in market_scanner.py reads this and caches for 15 min.
FII_BULLISH_THRESH   = 2000.0   # FII net buy > +₹2000 Cr  → BULLISH_INST
FII_BEARISH_THRESH   = -2000.0  # FII net sell > ₹2000 Cr   → BEARISH_INST
FII_AVAILABLE_AFTER  = "11:00"  # Data not available before this time
FII_CACHE_TTL_SECS   = 900      # Refresh every 15 min (avoid hammering NSE)

# ── GREEKS POLLING (P2.1) ─────────────────────────────────────────
# Option chain already polled every ~30s in _chain_loop.
# These constants gate Claude's entry decisions based on Greeks.
OC_POLL_INTERVAL  = 30      # seconds between option chain polls (informational)
DELTA_MIN_ENTRY   = 0.20    # never buy option with |delta| < this (too far OTM)
IV_PCTILE_HIGH    = 80.0    # intraday IV percentile above this = premium overpriced
IV_HISTORY_WINDOW = 40      # rolling samples for IV percentile (~20 min at 30s)

# ── LOGGING ───────────────────────────────────────────────────────
LOG_LEVEL         = "INFO"
LOG_FILE          = "logs/warroom.log"
TRADE_JOURNAL     = "data/trade_journal.json"
PERFORMANCE_FILE  = "data/performance.json"

# ── DASHBOARD ─────────────────────────────────────────────────────
DASHBOARD_REFRESH_HZ = 2

# ── MARKET CONTEXT (update weekly) ───────────────────────────────
MARKET_CONTEXT = {
    "bn_daily_trend":    "SIDEWAYS",    # Auto-updated 01 Apr 13:29
    "bn_bounce_day":     0,
    "bn_bounce_from":    50275,
    "bn_bounce_origin":  "April 01",
    "nifty_daily_trend": "SIDEWAYS",
    "monthly_bias":      "NEUTRAL",
    "vix_trend":         "ELEVATED",
}
