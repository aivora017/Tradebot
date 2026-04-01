# WAR ROOM — NSE Options Trading Bot

An autonomous intraday options trading bot for NSE (NIFTY 50 + BankNifty), combining real-time market data, technical analysis, institutional flow signals, and AI-assisted trade decisions via the Anthropic Claude API.

> **Mode:** Paper trading (simulated). Live trading requires an Upstox broker account.

---

## Features

### Market Intelligence
- **Dual-index coverage** — NIFTY 50 and BankNifty tracked simultaneously; Claude picks the best index each decision cycle
- **Real-time option chain** — Upstox REST API, polled every 30 seconds
- **Live Greeks** — Delta, Gamma, Theta, Vega, IV for both indices (skip if delta < 0.20 or IV percentile > 80)
- **GIFT Nifty pre-market bias** — 5 bias levels: BULLISH_GAP / MILD_BULL / FLAT / MILD_BEAR / BEARISH_GAP
- **FII/DII institutional flow** — NSE live feed, 15-min cache, bias overlay on every decision
- **PCR real-time computation** — ATM ±10 strike window with CONTRARIAN extreme alerts
- **Heavyweight stock monitoring** — HDFCBANK, SBIN, ICICIBANK, RELIANCE, INFY, TCS sentiment
- **Expiry auto-detection** — Nifty weekly (Tuesday), BankNifty monthly last-Tuesday, discovered live via Upstox contracts API

### Strategy Engine — 15 Strategies
| ID | Strategy | Tier | Window |
|----|----------|------|--------|
| S1 | ORB Breakout | T1 | 09:20–11:00 |
| S2 | Long Straddle | T4 | VIX > 18 / event days |
| S3 | Gap and Go | T2 | 09:15–11:30 |
| S4 | Trend Continuation | T3 | All day |
| S5 | 15-Min Breakout | T2 | 09:15–09:30 |
| S6 | S/R Reversal | T2 | All day |
| S7 | VWAP Pullback | T3 | All day |
| S8 | 5-Min Engulfing (BankNifty) | T2 | All day |
| S9 | Gamma Scalp Expiry | T5 | 09:15–11:00 (expiry only) |
| S10 | EMA 9/21 Crossover | T3 | 09:45–13:30 |
| S11 | Expiry Afternoon Gamma | T5 | 13:15–15:00 (expiry only) |
| S12 | Expiry Close Squeeze | T5 | 14:15–15:00 (expiry only) |
| S13 | Expiry Pre-11 Straddle | T4 | 09:30–10:45 (expiry only) |
| S14 | Max Pain Fade | T5 | 14:00–14:50 (expiry only) |
| S15 | Settlement Squeeze | T5 | 14:35–15:20 (expiry only) |

### Risk Management
- Per-trade sizing by tier: T1–T3 = 5% capital, T4 = 4% (straddle), T5 = 2% (gamma)
- Daily stop: 5% loss — all positions closed, no new trades
- Weekly stop: 10% cumulative loss — pause trading
- Max 2 concurrent trades; max 6 trades per day
- 3 consecutive losses — stop for the day
- VIX framework: < 12 skip | 12–16 half size | 16–22 full (ideal) | 22–28 straddle preference | > 28 reduce + straddle
- Trail SL to breakeven on T1 hit
- Compounded capital: saved on shutdown, loaded on next startup

### AI Decision Layer (Claude)
- Claude called every 60–300 seconds (adaptive: primary / late / dead zone)
- Full market packet per call: both index chains, Greeks, PCR, FII/DII, GIFT Nifty, heavyweight sentiment, VIX, all active signals, monthly P&L progress
- Outputs: BUY / SKIP / CLOSE with confidence tier (GODMODE / HIGH / MEDIUM / LOW)
- Confidence maps to position sizing multiplier

### Self-Evolving Memory
- Every closed trade stored with full context: strategy, P&L, VIX zone, bias alignment, Claude reasoning
- EOD analysis generates `data/learned_patterns.json` — strategy win rates, time slot perf, VIX zone perf
- Learned rules injected into Claude SYSTEM prompt at next session start
- Bot reads its own track record and auto-adjusts confidence thresholds

### Live Dashboard
- Flask + ngrok web dashboard (`warroom_live.html`)
- Real-time: market data, open trades, P&L, signal feed, Claude decision log
- Manual commands: force Claude call, close trade, reset day, open manual trade

---

## Architecture

```
main.py                 Entry point, main loop, orchestrator
├── data_feed.py        Upstox REST + V3 WebSocket, option chain, Greeks
├── strategy_engine.py  All 15 strategies, signal generation
├── claude_analyst.py   Claude API calls, packet builder, adaptive SYSTEM prompt
├── risk_manager.py     Sizing, VIX rules, daily/weekly stops
├── order_manager.py    Paper/live order placement
├── trade_tracker.py    Open trades, P&L, trail SL
├── market_scanner.py   Pre-market scanner, FII/DII, heavyweights
├── morning_setup.py    Auto-patches config.py with live S/R levels
├── morning_update.py   GIFT Nifty pre-market bias
├── bot_memory.py       Self-evolving trade memory + EOD analysis
├── api_bridge.py       Flask server (port 5003), dashboard endpoints
├── dashboard.py        Dashboard state builder
├── news_feed.py        Economic calendar + event polling
├── notifications.py    Telegram alerts
├── logger_setup.py     Logging config
└── utils.py            EMA, RSI, VWAP, ATM strike, lot size helpers

backtest.py             Backtesting engine (Bachelier premium simulation)
backtest_data.py        Upstox historical candle cache
backtest_feed.py        BacktestFeed mock (mirrors DataFeed interface)
gate_check.py           GO LIVE / NOT READY verdict (5 gate checks)
paper_report.py         2-week paper trading scorecard (XLSX output)
```

---

## Setup

### Prerequisites
- Python 3.10+
- [Upstox developer account](https://upstox.com) with API app created
- Anthropic API key
- Telegram bot token + chat ID
- ngrok (free tier works)

### Install

```bash
git clone https://github.com/aivora017/Tradebot.git
cd Tradebot
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r requirements.txt
```

### Configure

```bash
cp .env.example .env
```

Fill in `.env`:

```env
UPSTOX_API_KEY=your_api_key
UPSTOX_API_SECRET=your_api_secret
UPSTOX_REDIRECT_URI=http://127.0.0.1:8765/callback
ANTHROPIC_API_KEY=your_anthropic_key
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
EXECUTION_MODE=PAPER
TOTAL_CAPITAL=50000
MONTHLY_START_CAPITAL=50000
MONTHLY_TARGET_PCT=0.60
```

### Start

```bash
start.bat        # Windows — double-click or run in terminal
```

Startup sequence (automated):
1. Kills stale bot/ngrok processes
2. `get_token.py` — Upstox OAuth browser login (~30 seconds)
3. `morning_setup.py` — fetches live S/R levels, patches `config.py`
4. Launches `main.py` in a new terminal
5. Starts ngrok tunnel on port 5003
6. Opens Chrome to the dashboard

---

## Dashboard Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Live dashboard UI |
| `/api/health` | GET | Bot alive + mode check |
| `/api/snapshot` | GET | Full market state JSON |
| `/api/command` | POST | `force_claude` / `close_trade` / `reset_day` / `open_manual` |
| `/api/claude` | POST | Anthropic API proxy (no CORS) |

---

## Paper Trading Results (as of April 2026)

| Metric | Value |
|--------|-------|
| Capital | ₹50,000 |
| Target | +60% |
| Cumulative trades | 20 |
| Win rate | 65% |
| Avg P&L per trade | +43.2% |
| Top performer | EMA Crossover — 80% win rate |

---

## NSE Reference (2026)
- Nifty weekly expiry: **Tuesday** (changed September 2025, was Thursday)
- BankNifty expiry: **Monthly last Tuesday** (weekly discontinued)
- Lot sizes: NIFTY = 75 | BANKNIFTY = 30 (effective January 2026)

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Language | Python 3.10+ |
| Broker | Upstox REST v2/v3 + V3 WebSocket |
| AI | Anthropic Claude (claude-sonnet-4-20250514) |
| Dashboard | Flask + ngrok |
| Alerts | Telegram Bot API |
| Data | JSON (trade memory, learned patterns) |
| Backtesting | Custom Bachelier premium simulator |

---

## License
MIT
