# ================================================================
#  morning_prep.py — One-Command Morning Setup
#  Run this ONCE every morning before 9 AM instead of manually
#  updating config.py.
#
#  Does everything automatically:
#    1. Refreshes Upstox access token
#    2. Fetches live BN/Nifty levels from Upstox REST
#    3. Pulls option chain → calculates PCR, Max Pain, key S/R
#    4. Detects trend from recent closes
#    5. Checks India VIX
#    6. Scans news feed for high-impact events
#    7. Updates config.py KEY_LEVELS + MARKET_CONTEXT in-place
#    8. Prints today's war plan
#
#  Usage:
#    python morning_prep.py
#
#  Token refresh is interactive (browser login) — do it first thing.
#  Everything else is fully automatic.
# ================================================================

import os
import re
import sys
import json
import time
import requests
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv, set_key

load_dotenv()

ENV_FILE   = ".env"
CONFIG_FILE = "config.py"

BASE_URL = "https://api.upstox.com/v2"

# ── Colours for terminal output ───────────────────────────────────
G  = "\033[92m"   # green
Y  = "\033[93m"   # yellow
R  = "\033[91m"   # red
B  = "\033[94m"   # blue
M  = "\033[95m"   # magenta
W  = "\033[0m"    # reset
BD = "\033[1m"    # bold

def p(msg, col=W):
    print(f"{col}{msg}{W}")

def banner(title):
    print(f"\n{BD}{B}{'='*60}{W}")
    print(f"{BD}{B}  {title}{W}")
    print(f"{BD}{B}{'='*60}{W}\n")

# ── Step 1: Token Refresh ─────────────────────────────────────────

def refresh_token():
    banner("STEP 1 — Upstox Token Refresh")
    key    = os.getenv("UPSTOX_API_KEY", "")
    secret = os.getenv("UPSTOX_API_SECRET", "")
    redir  = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1")

    if not key or not secret:
        p("❌ UPSTOX_API_KEY or UPSTOX_API_SECRET missing in .env", R)
        sys.exit(1)

    url = (f"https://api.upstox.com/v2/login/authorization/dialog"
           f"?response_type=code&client_id={key}&redirect_uri={redir}")
    p("Opening browser for Upstox login…", Y)
    webbrowser.open(url)
    p("Login → copy the FULL redirect URL from address bar", Y)
    p("It looks like: https://127.0.0.1/?code=XXXXXXXX\n", Y)

    redirect = input("Paste redirect URL: ").strip()
    parsed   = urlparse(redirect)
    code     = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        p("❌ Could not find code in URL.", R)
        sys.exit(1)

    p(f"Exchanging code {code[:10]}…", Y)
    r = requests.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={"code": code, "client_id": key, "client_secret": secret,
              "redirect_uri": redir, "grant_type": "authorization_code"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code == 200:
        token = r.json().get("access_token", "")
        # Remove quotes if present
        token = token.strip("'\"")
        set_key(ENV_FILE, "UPSTOX_ACCESS_TOKEN", token)
        # Reload env
        load_dotenv(override=True)
        p(f"✅ Token saved: {token[:20]}…", G)
        return token
    else:
        p(f"❌ Token exchange failed: {r.status_code} | {r.text[:200]}", R)
        sys.exit(1)

# ── Upstox helpers ────────────────────────────────────────────────

def get_headers():
    token = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip("'\"")
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

def fetch_ltp(instrument_key: str) -> float:
    try:
        r = requests.get(
            f"{BASE_URL}/market-quote/ltp",
            headers=get_headers(),
            params={"instrument_key": instrument_key},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            for v in data.values():
                return float(v.get("last_price", 0))
    except Exception as e:
        p(f"  LTP fetch error: {e}", Y)
    return 0.0

def fetch_ohlc(instrument_key: str, interval: str = "day", days: int = 10) -> list:
    """Fetch historical OHLC candles."""
    try:
        to_date   = datetime.now().strftime("%Y-%m-%d")
        from_date = (datetime.now() - timedelta(days=days+5)).strftime("%Y-%m-%d")
        r = requests.get(
            f"{BASE_URL}/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}",
            headers=get_headers(),
            timeout=8,
        )
        if r.status_code == 200:
            candles = r.json().get("data", {}).get("candles", [])
            # Each candle: [timestamp, open, high, low, close, volume, oi]
            return candles
    except Exception as e:
        p(f"  OHLC fetch error: {e}", Y)
    return []

def _last_tuesday_of_month(year: int, month: int) -> datetime:
    """Last Tuesday of given month (for BankNifty monthly expiry)."""
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    d = last_day
    while datetime(year, month, d).weekday() != 1:  # 1 = Tuesday
        d -= 1
    return datetime(year, month, d)


def fetch_option_chain(symbol: str) -> dict:
    """Fetch option chain → returns {pcr, max_pain, resistance_levels, support_levels}"""
    try:
        # FIX (BUG-8): Correct expiry logic per SEBI 2024 + NSE Sep 2025 changes
        # NIFTY:     weekly Tuesday
        # BANKNIFTY: monthly last Tuesday (NOT every Wednesday)
        today = datetime.now()
        if "BANK" in symbol.upper():
            # BN: monthly last Tuesday
            last_tue = _last_tuesday_of_month(today.year, today.month)
            cutoff = today.replace(hour=15, minute=30, second=0, microsecond=0)
            if last_tue.date() < today.date() or (last_tue.date() == today.date() and today >= cutoff):
                next_month = today.month + 1
                next_year  = today.year + (1 if next_month > 12 else 0)
                next_month = next_month if next_month <= 12 else 1
                last_tue = _last_tuesday_of_month(next_year, next_month)
            expiry_dt = last_tue
        else:
            # NIFTY: next weekly Tuesday
            days_ahead = (1 - today.weekday()) % 7  # 1 = Tuesday
            if days_ahead == 0 and today.hour >= 15:
                days_ahead = 7
            expiry_dt = today + timedelta(days=days_ahead)
        expiry_iso = expiry_dt.strftime("%Y-%m-%d")

        key = ("NSE_INDEX|Nifty Bank" if "BANK" in symbol
               else "NSE_INDEX|Nifty 50")
        r = requests.get(
            f"{BASE_URL}/option/chain",
            headers=get_headers(),
            params={"instrument_key": key, "expiry_date": expiry_iso},
            timeout=10,
        )
        if r.status_code != 200:
            return {}

        rows        = r.json().get("data", [])
        total_ce_oi = total_pe_oi = 0
        chain       = []

        for row in rows:
            ce    = row.get("call_options", {}).get("market_data", {})
            pe    = row.get("put_options",  {}).get("market_data", {})
            ce_oi = int(ce.get("oi", 0))
            pe_oi = int(pe.get("oi", 0))
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            chain.append({
                "strike": float(row.get("strike_price", 0)),
                "ce_oi":  ce_oi,
                "pe_oi":  pe_oi,
                "ce_iv":  float(ce.get("iv", 0)),
            })

        pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

        # Max pain
        max_pain = _calc_max_pain(chain)

        # Top OI strikes = key S/R levels
        # Resistance = top CE OI strikes above current price
        # Support    = top PE OI strikes below current price
        return {
            "pcr":       pcr,
            "max_pain":  max_pain,
            "chain":     chain,
            "expiry":    expiry_dt.strftime("%d %b"),
        }

    except Exception as e:
        p(f"  Option chain error [{symbol}]: {e}", Y)
        return {}

def _calc_max_pain(chain: list) -> float:
    if not chain:
        return 0.0
    best, mp = float("inf"), 0.0
    for test in chain:
        pain = sum(
            max(0, test["strike"] - r["strike"]) * r["pe_oi"] +
            max(0, r["strike"] - test["strike"]) * r["ce_oi"]
            for r in chain
        )
        if pain < best:
            best, mp = pain, test["strike"]
    return mp

def calc_key_levels(chain: list, ltp: float, symbol: str) -> dict:
    """Derive resistance and support levels from OI concentration."""
    if not chain or ltp == 0:
        return {"resistance": [], "support": []}

    step = 100 if "BANK" in symbol else 50

    # Strikes above LTP sorted by CE OI descending → resistance
    above = sorted([r for r in chain if r["strike"] > ltp],
                   key=lambda x: x["ce_oi"], reverse=True)
    # Strikes below LTP sorted by PE OI descending → support
    below = sorted([r for r in chain if r["strike"] < ltp],
                   key=lambda x: x["pe_oi"], reverse=True)

    # Pick top 3 unique levels, rounded to nearest step
    resist = []
    for r in above:
        lv = round(r["strike"] / step) * step
        if lv not in resist:
            resist.append(lv)
        if len(resist) >= 3:
            break

    support = []
    for r in below:
        lv = round(r["strike"] / step) * step
        if lv not in support:
            support.append(lv)
        if len(support) >= 3:
            break

    # Ensure we always have 3 levels even if OI data is sparse
    while len(resist) < 3:
        last = resist[-1] if resist else round(ltp/step)*step
        resist.append(last + step)
    while len(support) < 3:
        last = support[-1] if support else round(ltp/step)*step
        support.append(last - step)

    resist.sort()
    support.sort(reverse=True)

    return {
        "resistance": [float(r) for r in resist],
        "support":    [float(s) for s in support],
    }

def detect_trend(candles: list) -> str:
    """Determine daily trend from last 5 OHLC candles."""
    if len(candles) < 4:
        return "SIDEWAYS"
    closes = [float(c[4]) for c in candles[:5]]  # most recent first
    closes.reverse()  # oldest first
    if closes[-1] > closes[0] and closes[-1] > closes[-2]:
        return "UPTREND"
    if closes[-1] < closes[0] and closes[-1] < closes[-2]:
        return "DOWNTREND"
    return "SIDEWAYS"

def fetch_vix_ltp() -> float:
    return fetch_ltp("NSE_INDEX|India VIX")

# ── Step 1.5: Fetch Live Account Balance ──────────────────────────

def fetch_live_funds() -> float:
    """
    Fetches actual available margin from Upstox and updates TRADING_CAPITAL
    + MONTHLY_START_CAPITAL in .env so the bot starts with the real balance.

    Upstox /v2/user/funds-and-margin response structure:
      data.equity.available_margin  — cash free for new trades (use this)
      data.equity.used_margin       — margin already locked in open positions
      data.equity.payin_amount      — cash added today (payin)
      data.equity.notional_cash     — ledger balance before live P&L
    NO 'net' field exists — don't use it.

    Returns the available capital (float). Falls back to existing .env value on error.
    """
    banner("STEP 1.5 — Fetching Live Account Balance")
    fallback = float(os.getenv("TRADING_CAPITAL", "50000"))
    try:
        # Call WITHOUT segment param — most reliable, returns full equity+commodity block
        r = requests.get(
            f"{BASE_URL}/user/get-funds-and-margin",
            headers=get_headers(),
            timeout=10,
        )
        raw = r.text[:500]

        if r.status_code != 200:
            p(f"❌  Funds fetch HTTP {r.status_code}", R)
            p(f"    Raw response: {raw}", Y)
            p("    Using existing TRADING_CAPITAL from .env", Y)
            return fallback

        resp = r.json()
        if resp.get("status") != "success":
            p(f"❌  API returned non-success: {raw}", R)
            p("    Using existing TRADING_CAPITAL from .env", Y)
            return fallback

        data = resp.get("data", {})

        # Handle two possible structures Upstox may return:
        #  1. data = {"equity": {...}, "commodity": {...}}  ← standard
        #  2. data = {"available_margin": ..., ...}        ← when segment param used
        if "equity" in data:
            equity = data["equity"]
        else:
            # Flat structure — treat the whole data dict as equity
            equity = data

        available = float(equity.get("available_margin", 0) or 0)
        used       = float(equity.get("used_margin",       0) or 0)
        payin      = float(equity.get("payin_amount",      0) or 0)
        notional   = float(equity.get("notional_cash",     0) or 0)

        p(f"  ✅ Available Margin (free for new trades): ₹{available:>12,.2f}", G)
        p(f"  ℹ️  Used Margin (locked in positions):      ₹{used:>12,.2f}", B)
        p(f"  ℹ️  Payin today:                            ₹{payin:>12,.2f}", B)
        p(f"  ℹ️  Notional cash (ledger):                 ₹{notional:>12,.2f}", B)

        # Capital for new trades = available_margin only
        # Never add used_margin — that's already deployed
        capital = available

        if capital <= 0:
            # Could be after-hours (0 available) — use notional as fallback
            if notional > 0:
                capital = notional
                p(f"\n  ⚠️  available_margin=0, using notional_cash: ₹{capital:,.2f}", Y)
            else:
                p(f"\n  ⚠️  All funds values are 0. Raw data dump:", Y)
                p(f"    {json.dumps(data, indent=2)[:400]}", Y)
                p("    Using existing TRADING_CAPITAL from .env", Y)
                return fallback

        set_key(ENV_FILE, "TRADING_CAPITAL",     str(round(capital, 2)))
        set_key(ENV_FILE, "MONTHLY_START_CAPITAL", str(round(capital, 2)))
        load_dotenv(override=True)

        p(f"\n  💰 TRADING_CAPITAL     → ₹{capital:,.2f}", G)
        p(f"  💰 MONTHLY_START_CAPITAL → ₹{capital:,.2f}", G)
        return capital

    except Exception as e:
        p(f"❌  Exception in fetch_live_funds: {e}", R)
        p("    Using existing TRADING_CAPITAL from .env", Y)
        return fallback


# ── Step 2: Fetch all market data ─────────────────────────────────

def fetch_market_data():
    banner("STEP 2 — Fetching Live Market Data")

    p("Fetching BankNifty LTP…", Y)
    bn_ltp = fetch_ltp("NSE_INDEX|Nifty Bank")
    p(f"  BankNifty: {bn_ltp:,.0f}" if bn_ltp else "  BankNifty: N/A (market closed)", G if bn_ltp else Y)

    p("Fetching Nifty 50 LTP…", Y)
    n50_ltp = fetch_ltp("NSE_INDEX|Nifty 50")
    p(f"  Nifty 50:  {n50_ltp:,.0f}" if n50_ltp else "  Nifty 50: N/A (market closed)", G if n50_ltp else Y)

    p("Fetching India VIX…", Y)
    vix = fetch_vix_ltp()
    p(f"  VIX: {vix:.2f}" if vix else "  VIX: N/A", G if vix else Y)

    p("Fetching BN option chain…", Y)
    bn_chain  = fetch_option_chain("BANKNIFTY")
    p(f"  BN  PCR: {bn_chain.get('pcr', 'N/A')} | MaxPain: {bn_chain.get('max_pain', 'N/A')}", G)

    p("Fetching Nifty option chain…", Y)
    n50_chain = fetch_option_chain("NIFTY")
    p(f"  N50 PCR: {n50_chain.get('pcr', 'N/A')} | MaxPain: {n50_chain.get('max_pain', 'N/A')}", G)

    p("Fetching BN historical candles (trend detection)…", Y)
    bn_candles  = fetch_ohlc("NSE_INDEX|Nifty Bank", days=15)
    n50_candles = fetch_ohlc("NSE_INDEX|Nifty 50",   days=15)

    bn_trend  = detect_trend(bn_candles)  if bn_candles  else "DOWNTREND"
    n50_trend = detect_trend(n50_candles) if n50_candles else "DOWNTREND"
    p(f"  BN trend:  {bn_trend}", G)
    p(f"  N50 trend: {n50_trend}", G)

    # Derive key levels from OI or fallback to price-based
    if bn_chain.get("chain") and bn_ltp:
        bn_levels  = calc_key_levels(bn_chain["chain"],  bn_ltp,  "BANKNIFTY")
    else:
        # Fallback: price-based round numbers
        b = round(bn_ltp/500)*500 if bn_ltp else 54000
        bn_levels = {
            "resistance": [b+500, b+1000, b+1500],
            "support":    [b-500, b-1000, b-1500],
        }

    if n50_chain.get("chain") and n50_ltp:
        n50_levels = calc_key_levels(n50_chain["chain"], n50_ltp, "NIFTY")
    else:
        n = round(n50_ltp/200)*200 if n50_ltp else 22800
        n50_levels = {
            "resistance": [n+200, n+400, n+600],
            "support":    [n-200, n-400, n-600],
        }

    return {
        "bn_ltp":       bn_ltp,
        "n50_ltp":      n50_ltp,
        "vix":          vix,
        "bn_pcr":       bn_chain.get("pcr", 1.0),
        "n50_pcr":      n50_chain.get("pcr", 1.0),
        "bn_max_pain":  bn_chain.get("max_pain", 0.0),
        "n50_max_pain": n50_chain.get("max_pain", 0.0),
        "bn_trend":     bn_trend,
        "n50_trend":    n50_trend,
        "bn_resist":    bn_levels["resistance"],
        "bn_support":   bn_levels["support"],
        "n50_resist":   n50_levels["resistance"],
        "n50_support":  n50_levels["support"],
        "bn_expiry":    bn_chain.get("expiry", ""),
        "n50_expiry":   n50_chain.get("expiry", ""),
        "bn_candles":   bn_candles,
    }

# ── Block replacer (brace-counting — regex \{.*?\} is non-greedy and
#    stops at the first inner } instead of the outer one, corrupting config) ──

def _replace_block(content: str, start_marker: str, new_block: str) -> str:
    """Replace a Python assignment block using brace counting, not regex."""
    idx = content.find(start_marker)
    if idx == -1:
        return content          # marker not found — leave unchanged

    brace_start = content.find('{', idx)
    if brace_start == -1:
        return content

    depth, pos, brace_end = 0, brace_start, brace_start
    while pos < len(content):
        if content[pos] == '{':
            depth += 1
        elif content[pos] == '}':
            depth -= 1
            if depth == 0:
                brace_end = pos
                break
        pos += 1
    else:
        return content          # unmatched braces — leave unchanged

    return content[:idx] + new_block + content[brace_end + 1:]


# ── Step 3: Update config.py ──────────────────────────────────────

def update_config(data: dict):
    banner("STEP 3 — Updating config.py")

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # ── Update KEY_LEVELS ─────────────────────────────────────────
    br = data["bn_resist"]
    bs = data["bn_support"]
    nr = data["n50_resist"]
    ns = data["n50_support"]
    bmp = data["bn_max_pain"]
    nmp = data["n50_max_pain"]

    new_levels = (
        f'KEY_LEVELS: Dict[str, Dict[str, List[float]]] = {{\n'
        f'    "BANKNIFTY": {{\n'
        f'        "resistance": [{br[0]:.1f}, {br[1]:.1f}, {br[2]:.1f}],\n'
        f'        "support":    [{bs[0]:.1f}, {bs[1]:.1f}, {bs[2]:.1f}],\n'
        f'        "max_pain":   {bmp:.1f},\n'
        f'    }},\n'
        f'    "NIFTY": {{\n'
        f'        "resistance": [{nr[0]:.1f}, {nr[1]:.1f}, {nr[2]:.1f}],\n'
        f'        "support":    [{ns[0]:.1f}, {ns[1]:.1f}, {ns[2]:.1f}],\n'
        f'        "max_pain":   {nmp:.1f},\n'
        f'    }},\n'
        f'}}'
    )

    # Replace KEY_LEVELS block (brace-counting — safe for nested dicts)
    content = _replace_block(
        content,
        'KEY_LEVELS: Dict[str, Dict[str, List[float]]] = ',
        new_levels,
    )

    # ── Update MARKET_CONTEXT ────────────────────────────────────
    bn_trend   = data["bn_trend"]
    n50_trend  = data["n50_trend"]
    vix        = data["vix"]
    today      = datetime.now()
    month_bias = "BEARISH" if bn_trend == "DOWNTREND" else ("BULLISH" if bn_trend == "UPTREND" else "NEUTRAL")

    # Detect VIX trend label
    if vix >= 22:
        vix_trend = "ELEVATED"
    elif vix >= 16:
        vix_trend = "STABLE"
    else:
        vix_trend = "LOW"

    # Bounce day counter — detect from candles if available
    bounce_day = 1
    bounce_from = 0.0
    bounce_origin = today.strftime("%B %d")
    candles = data.get("bn_candles", [])
    if candles and len(candles) >= 2:
        # Find the recent low (last 5 days)
        recent = candles[:5]
        lows   = [(float(c[3]), c[0][:10]) for c in recent]
        min_low, min_date = min(lows, key=lambda x: x[0])
        bounce_from   = min_low
        # Count trading days since that low
        try:
            low_dt = datetime.strptime(min_date, "%Y-%m-%d")
            diff = (today - low_dt).days
            bounce_day = max(1, min(diff, 10))
        except Exception:
            bounce_day = 1
        bounce_origin = datetime.strptime(min_date, "%Y-%m-%d").strftime("%B %d") if min_date else bounce_origin

    new_context = (
        f'MARKET_CONTEXT = {{\n'
        f'    "bn_daily_trend":    "{bn_trend}",\n'
        f'    "bn_bounce_day":     {bounce_day},\n'
        f'    "bn_bounce_from":    {bounce_from:.0f},\n'
        f'    "bn_bounce_origin":  "{bounce_origin}",\n'
        f'    "nifty_daily_trend": "{n50_trend}",\n'
        f'    "monthly_bias":      "{month_bias}",\n'
        f'    "vix_trend":         "{vix_trend}",\n'
        f'}}'
    )

    content = _replace_block(content, 'MARKET_CONTEXT = ', new_context)

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    p("✅ KEY_LEVELS updated", G)
    p(f"   BN  resistance: {br}", G)
    p(f"   BN  support:    {bs}", G)
    p(f"   BN  max pain:   {bmp:.0f}", G)
    p(f"   N50 resistance: {nr}", G)
    p(f"   N50 support:    {ns}", G)
    p(f"   N50 max pain:   {nmp:.0f}", G)
    p("✅ MARKET_CONTEXT updated", G)
    p(f"   BN trend:  {bn_trend}", G)
    p(f"   N50 trend: {n50_trend}", G)
    p(f"   VIX trend: {vix_trend}", G)

# ── Step 4: Print today's war plan ───────────────────────────────

def print_war_plan(data: dict):
    banner("TODAY'S WAR PLAN")
    now = datetime.now()
    vix = data["vix"]

    if vix < 12:
        vix_label = f"{R}AVOID — DO NOT TRADE{W}"
    elif vix < 16:
        vix_label = f"{Y}REDUCED — 50% size only{W}"
    elif vix <= 22:
        vix_label = f"{G}IDEAL — full size, aggressive{W}"
    elif vix <= 28:
        vix_label = f"{Y}ELEVATED — prefer straddles{W}"
    else:
        vix_label = f"{R}EXTREME — no pure buying{W}"

    bn_trend = data["bn_trend"]
    bias = "PE on bounces to resistance" if bn_trend == "DOWNTREND" else (
           "CE on dips to support" if bn_trend == "UPTREND" else "Wait for direction")

    print(f"  {BD}Date:{W}        {now.strftime('%A %d %b %Y')}")
    print(f"  {BD}BankNifty:{W}   {data['bn_ltp']:,.0f}")
    print(f"  {BD}Nifty 50:{W}    {data['n50_ltp']:,.0f}")
    print(f"  {BD}India VIX:{W}   {vix:.2f} → {vix_label}")
    print(f"  {BD}PCR BN:{W}      {data['bn_pcr']:.3f}")
    print(f"  {BD}PCR Nifty:{W}   {data['n50_pcr']:.3f}")
    print(f"  {BD}BN Expiry:{W}   {data['bn_expiry']}")
    print(f"  {BD}N50 Expiry:{W}  {data['n50_expiry']}")
    print()
    print(f"  {BD}BN Trend:{W}    {bn_trend}")
    print(f"  {BD}Bias:{W}        {bias}")
    print()
    print(f"  {BD}BN Resistance:{W} {data['bn_resist']}")
    print(f"  {BD}BN Support:{W}    {data['bn_support']}")
    print()

    # Day of week edge
    wd = now.weekday()
    edges = {
        0: "Monday — watch for gap fade or continuation. Gap and Go if GIFT aligned.",
        1: "Tuesday — Nifty expiry day. Gamma scalp bonus at 9:15-9:35, then ALL strategies run normally until 14:00.",
        2: "Wednesday — High trending day historically. ORB + EMA preferred. (BN expiry = monthly last Tuesday, NOT Wednesday.)",
        3: "Thursday — High trending day historically. ORB + Trend Continuation preferred.",
        4: "Friday — Trend days common. Good for continuation trades.",
    }
    print(f"  {BD}Day Edge:{W}    {edges.get(wd, '')}")
    print()
    print(f"  {BD}{G}Ready. Run: python main.py{W}")
    print()

# ── Step 5: Telegram Chat ID Auto-Fix ────────────────────────────

def fix_telegram_chat_id():
    banner("STEP 5 — Telegram Notification Check")
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not bot_token:
        p("⚠  TELEGRAM_BOT_TOKEN not in .env — skipping", Y)
        return

    # Fetch recent updates to find the correct chat_id
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{bot_token}/getUpdates",
            params={"limit": 20, "allowed_updates": ["message"]},
            timeout=8,
        )
    except Exception as e:
        p(f"⚠  Telegram getUpdates error: {e}", Y)
        return

    if r.status_code != 200:
        p(f"⚠  Telegram API error {r.status_code} — check BOT_TOKEN", Y)
        return

    results = r.json().get("result", [])
    if not results:
        p("⚠  No Telegram messages found.", Y)
        p("   → Open Telegram, find your bot, send /start", Y)
        p("   → Then re-run morning_prep.py to auto-save the chat_id", Y)
        return

    # Use the most recent message's chat_id
    chat_id = None
    for update in reversed(results):
        msg = update.get("message", {})
        if msg:
            chat_id = msg.get("chat", {}).get("id")
            break

    if not chat_id:
        p("⚠  Could not extract chat_id from updates", Y)
        return

    current = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if str(chat_id) == str(current):
        p(f"✅ TELEGRAM_CHAT_ID already correct: {chat_id}", G)
    else:
        set_key(ENV_FILE, "TELEGRAM_CHAT_ID", str(chat_id))
        load_dotenv(override=True)
        p(f"✅ TELEGRAM_CHAT_ID fixed: {current or 'MISSING'} → {chat_id}", G)

    # Send a test message to confirm connectivity
    try:
        test = requests.post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json={
                "chat_id":    chat_id,
                "text":       "✅ WAR ROOM connected. Morning prep complete — bot starting shortly.",
                "parse_mode": "HTML",
            },
            timeout=8,
        )
        if test.status_code == 200:
            p("✅ Test Telegram message sent ✓", G)
        else:
            p(f"⚠  Test message failed: {test.status_code} {test.text[:100]}", Y)
    except Exception as e:
        p(f"⚠  Test message error: {e}", Y)


# ── Main ─────────────────────────────────────────────────────────

def main():
    print(f"\n{BD}{M}")
    print("╔══════════════════════════════════════════════════════╗")
    print("║     ⚡  WAR ROOM — MORNING PREP                      ║")
    print("║     Auto-updates token + config every morning        ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(W)

    # Token refresh
    refresh_token()

    # Fetch live account balance → update .env TRADING_CAPITAL
    fetch_live_funds()

    # Fetch market data
    data = fetch_market_data()

    # Update config.py
    update_config(data)

    # Print war plan
    print_war_plan(data)

    # Auto-fix Telegram chat_id + send connectivity test
    fix_telegram_chat_id()


if __name__ == "__main__":
    main()
