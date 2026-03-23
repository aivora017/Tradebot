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
CLAUDE_MODEL         = "claude-sonnet-4-20250514"
CLAUDE_MAX_TOKENS    = 1200
CLAUDE_CALL_INTERVAL = 120      # seconds between Claude calls

# ── TELEGRAM ALERTS ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID",   "")
TELEGRAM_ENABLED    = bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)

# ── CAPITAL & SIZING ─────────────────────────────────────────────
TOTAL_CAPITAL        = float(os.getenv("TRADING_CAPITAL", "50000"))
RISK_PER_TRADE_T1_3  = 0.05    # 5% of capital — Tier 1,2,3
RISK_PER_TRADE_T4    = 0.04    # 4% total for straddle (2% each leg)
RISK_PER_TRADE_T5    = 0.02    # 2% max — gamma plays ONLY
MAX_DAILY_LOSS_PCT   = 0.05    # 5% daily → stop trading
MAX_WEEKLY_LOSS_PCT  = 0.10    # 10% weekly loss limit
MAX_CONCURRENT       = 2       # max open trades at once
MAX_DAILY_TRADES     = 3       # max trades per day (psychological rule)
LOSS_STREAK_STOP     = 3       # consecutive losses → stop day

# ── OPTION SL / TARGET RULES ─────────────────────────────────────
SL_PREMIUM_T1_3      = 0.20    # 20% SL on premium (ORB, Gap, VWAP)
SL_PREMIUM_T4        = 0.25    # 25% SL (S/R, Straddle)
SL_PREMIUM_T5        = 0.50    # 50% SL (Gamma play)
T1_BOOK_QTY_PCT      = 0.50    # book 50% quantity at T1
T1_PREMIUM_GAIN      = 0.50    # T1 = 50% premium gain → book partial
T2_PREMIUM_GAIN      = 0.80    # T2 = 80% premium gain → trail

# ── TIME RULES (HARD — NON-NEGOTIABLE) ───────────────────────────
MARKET_OPEN           = "09:15"
ORB_WINDOW_END        = "09:20"
PRIMARY_ENTRY_START   = "09:20"
PRIMARY_ENTRY_END     = "11:00"
LATE_ENTRY_END        = "11:30"
DEAD_ZONE_START       = "11:30"
DEAD_ZONE_END         = "13:30"
SECONDARY_ENTRY_START = "13:30"
SECONDARY_ENTRY_END   = "14:30"
HARD_EXIT_TIME        = "14:00"    # ALL trades must be closed by this time
EXPIRY_EXIT_TIME      = "11:00"    # expiry day — close by 11 AM no exception
MARKET_CLOSE          = "15:30"
PRE_MARKET_SCAN_TIME  = "08:45"   # run morning scanner at this time

# ── VIX FRAMEWORK ────────────────────────────────────────────────
VIX_AVOID             = 12.0   # below this = do not trade at all
VIX_REDUCED_SIZE      = 16.0   # 12–16 = 50% position size
VIX_IDEAL_LOW         = 16.0   # 16–22 = full size, ideal zone
VIX_IDEAL_HIGH        = 22.0
VIX_STRADDLE_ZONE     = 22.0   # 22–28 = prefer straddles
VIX_SPREAD_ONLY       = 28.0   # above = avoid pure buying

# ── NSE INSTRUMENT KEYS ──────────────────────────────────────────
NIFTY_INDEX_KEY       = "NSE_INDEX|Nifty 50"
BANKNIFTY_INDEX_KEY   = "NSE_INDEX|Nifty Bank"
VIX_INDEX_KEY         = "NSE_INDEX|India VIX"
NIFTY_FUT_KEY         = "NSE_FO|NIFTY"
BANKNIFTY_FUT_KEY     = "NSE_FO|BANKNIFTY"

WATCH_KEYS = [
    NIFTY_INDEX_KEY,
    BANKNIFTY_INDEX_KEY,
    VIX_INDEX_KEY,
]

# ── LOT SIZES (SEBI 2024 UPDATE — CORRECTED) ─────────────────────
# IMPORTANT: These were updated by SEBI in Nov 2024 effective April 2025
# Old: NIFTY=25, BANKNIFTY=15 — those are WRONG now
LOT_SIZES = {
    "NIFTY":     75,    # Updated April 2025
    "BANKNIFTY": 30,    # Updated April 2025
}

# ── EXPIRY SCHEDULE (UPDATED SEP 2025) ───────────────────────────
# SEBI changed Nifty weekly expiry from Thursday to Tuesday (Sep 2025)
NIFTY_EXPIRY_WEEKDAY      = 1   # Tuesday (Mon=0) — changed Sep 2025
BANKNIFTY_EXPIRY_WEEKDAY  = 2   # Wednesday (unchanged)

# ── KEY S/R LEVELS (update every morning pre-market) ─────────────
KEY_LEVELS: Dict[str, Dict[str, List[float]]] = {
    "BANKNIFTY": {
        "resistance": [54000.0, 54500.0, 54900.0],  # Auto-updated 23 Mar 10:10
        "support":    [51800.0, 51700.0, 51600.0],
        "max_pain":   53600.0,
    },
    "NIFTY": {
        "resistance": [22700.0, 23000.0, 23300.0],  # Auto-updated 23 Mar 10:10
        "support":    [22400.0, 22500.0, 22600.0],
        "max_pain":   23300.0,
    },
}

# ── STRATEGY PARAMETERS ──────────────────────────────────────────
@dataclass
class Params:
    # ORB Strategy
    ORB_VOLUME_MULT:         float = 2.0
    ORB_MAX_GAP_PCT:         float = 0.008
    ORB_NIFTY_MAX_RANGE:     float = 80.0
    ORB_BN_MAX_RANGE:        float = 250.0

    # Gap and Go
    GAP_MIN_PCT:             float = 0.003
    GAP_MAX_PCT:             float = 0.008
    GAP_PULLBACK_HOLD:       float = 0.50
    GAP_VOL_MULT:            float = 1.3

    # VWAP Pullback
    VWAP_SL_BREACH_PCT:      float = 0.003
    VWAP_VOL_MULT:           float = 1.3

    # EMA Crossover
    EMA_FAST:                int   = 9
    EMA_SLOW:                int   = 21
    EMA_CE_RSI_MIN:          float = 55.0
    EMA_PE_RSI_MAX:          float = 45.0
    EMA_VOL_MULT:            float = 1.5

    # S/R Reversal
    SR_NIFTY_TOLERANCE:      float = 30.0
    SR_BN_TOLERANCE:         float = 80.0

    # Engulfing (BankNifty)
    SCALP_SL_PREMIUM:        float = 0.15
    SCALP_VOL_MULT:          float = 1.3

    # Gamma (expiry day)
    GAMMA_MAX_PREMIUM_PCT:   float = 0.005
    GAMMA_NIFTY_MOVE_MIN:    float = 80.0
    GAMMA_BN_MOVE_MIN:       float = 200.0

    # Straddle
    STRADDLE_ENTRY_MINS_PRE: int   = 20
    STRADDLE_MAX_IV_PCTILE:  float = 80.0
    STRADDLE_COMBINED_SL:    float = 0.25

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

# ── LOGGING ───────────────────────────────────────────────────────
LOG_LEVEL         = "INFO"
LOG_FILE          = "logs/warroom.log"
TRADE_JOURNAL     = "data/trade_journal.json"
PERFORMANCE_FILE  = "data/performance.json"

# ── DASHBOARD ─────────────────────────────────────────────────────
DASHBOARD_REFRESH_HZ = 2

# ── MARKET CONTEXT (update weekly) ───────────────────────────────
MARKET_CONTEXT = {
    "bn_daily_trend":    "DOWNTREND",    # Auto-updated 23 Mar 10:10
    "bn_bounce_day":     0,
    "bn_bounce_from":    53427,
    "bn_bounce_origin":  "March 23",
    "nifty_daily_trend": "DOWNTREND",
    "monthly_bias":      "BEARISH",
    "vix_trend":         "ELEVATED",
}
