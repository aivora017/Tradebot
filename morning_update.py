# ================================================================
#  morning_update.py — Fully Automatic Morning Setup
#  Run this ONCE every morning before 9 AM.
#  It does EVERYTHING automatically:
#    1. Refreshes Upstox access token
#    2. Fetches live option chain from NSE
#    3. Calculates key S/R levels from OI concentration
#    4. Computes max pain
#    5. Gets PCR, VIX, prev close
#    6. Updates config.py KEY_LEVELS + MARKET_CONTEXT
#    7. Prints the war plan for the day
#
#  Usage:  python morning_update.py
#  Then:   python main.py
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

UPSTOX_BASE = "https://api.upstox.com/v2"
NSE_BASE    = "https://www.nseindia.com/api"

NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com",
}

# ── Colours for terminal output ───────────────────────────────────
def green(s):  return f"\033[92m{s}\033[0m"
def yellow(s): return f"\033[93m{s}\033[0m"
def red(s):    return f"\033[91m{s}\033[0m"
def bold(s):   return f"\033[1m{s}\033[0m"
def cyan(s):   return f"\033[96m{s}\033[0m"


# ── STEP 1: Token Refresh ─────────────────────────────────────────

def refresh_token():
    print(bold("\n╔══ STEP 1: Upstox Token Refresh ══"))
    key    = os.getenv("UPSTOX_API_KEY", "")
    secret = os.getenv("UPSTOX_API_SECRET", "")
    redir  = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1")

    if not key or not secret:
        print(red("❌ UPSTOX_API_KEY or UPSTOX_API_SECRET missing from .env"))
        sys.exit(1)

    # Check if existing token still valid (expires at 3:30 AM next day)
    existing = os.getenv("UPSTOX_ACCESS_TOKEN", "")
    if existing and _token_valid(existing):
        print(green(f"✅ Token still valid — skipping refresh"))
        return existing

    url = (f"https://api.upstox.com/v2/login/authorization/dialog"
           f"?response_type=code&client_id={key}&redirect_uri={redir}")

    print(f"  Opening browser for Upstox login...")
    webbrowser.open(url)
    print(yellow("  Login → copy the full redirect URL from address bar"))
    print(yellow("  It looks like: https://127.0.0.1/?code=XXXXXXXX\n"))

    redirect = input("  Paste redirect URL: ").strip()
    parsed   = urlparse(redirect)
    code     = parse_qs(parsed.query).get("code", [None])[0]

    if not code:
        print(red("❌ Could not find code in URL"))
        sys.exit(1)

    r = requests.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={"code": code, "client_id": key, "client_secret": secret,
              "redirect_uri": redir, "grant_type": "authorization_code"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    if r.status_code == 200:
        token = r.json().get("access_token", "")
        set_key(ENV_FILE, "UPSTOX_ACCESS_TOKEN", token)
        # Reload env
        load_dotenv(override=True)
        print(green(f"  ✅ Token saved: {token[:25]}…"))
        return token
    else:
        print(red(f"  ❌ Token exchange failed: {r.status_code}"))
        sys.exit(1)


def _token_valid(token: str) -> bool:
    """JWT tokens have expiry in payload. Check if still valid for 2+ hours."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload = parts[1] + "=="  # padding
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode())
        exp = decoded.get("exp", 0)
        # Valid if more than 2 hours remaining
        return (exp - time.time()) > 7200
    except Exception:
        return False


# ── STEP 2: Fetch Market Data ─────────────────────────────────────

def fetch_nse_option_chain(symbol: str, session: requests.Session):
    """Fetch NSE option chain. Returns parsed data."""
    try:
        sym_nse = "BANKNIFTY" if "BANK" in symbol else "NIFTY"
        r = session.get(
            f"{NSE_BASE}/option-chain-indices",
            params={"symbol": sym_nse},
            timeout=10
        )
        if r.status_code != 200:
            return None
        return r.json()
    except Exception as e:
        print(red(f"  NSE chain error for {symbol}: {e}"))
        return None


def fetch_upstox_indices(token: str):
    """Fetch live index prices from Upstox."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    keys    = "NSE_INDEX|Nifty 50,NSE_INDEX|Nifty Bank,NSE_INDEX|India VIX"
    result  = {}
    try:
        r = requests.get(
            f"{UPSTOX_BASE}/market-quote/ltp",
            headers=headers,
            params={"instrument_key": keys},
            timeout=8
        )
        if r.status_code == 200:
            data = r.json().get("data", {})
            for k, v in data.items():
                ltp = v.get("last_price", 0)
                if "Bank" in k:
                    result["BANKNIFTY"] = ltp
                elif "VIX" in k:
                    result["VIX"] = ltp
                elif "Nifty 50" in k:
                    result["NIFTY"] = ltp
    except Exception as e:
        print(yellow(f"  Upstox LTP error (using NSE fallback): {e}"))
    return result


def parse_chain(data: dict, symbol: str):
    """Extract key levels from option chain OI data."""
    if not data:
        return {}

    records = data.get("records", {}).get("data", [])
    ul      = data.get("records", {}).get("underlyingValue", 0)

    step    = 100 if "BANK" in symbol else 50
    atm     = round(ul / step) * step

    total_ce_oi = total_pe_oi = 0
    oi_by_strike = {}

    for row in records:
        ce = row.get("CE", {})
        pe = row.get("PE", {})
        strike = float(row.get("strikePrice", 0))
        ce_oi  = int(ce.get("openInterest", 0))
        pe_oi  = int(pe.get("openInterest", 0))
        total_ce_oi += ce_oi
        total_pe_oi += pe_oi
        oi_by_strike[strike] = {"ce_oi": ce_oi, "pe_oi": pe_oi,
                                 "ce_iv": ce.get("impliedVolatility", 0),
                                 "pe_iv": pe.get("impliedVolatility", 0)}

    pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0

    # Max pain
    max_pain = _calc_max_pain(oi_by_strike)

    # Resistance = top 3 CE OI strikes ABOVE atm
    above = {s: v["ce_oi"] for s, v in oi_by_strike.items() if s > atm and v["ce_oi"] > 0}
    resistance = sorted(above, key=above.get, reverse=True)[:5]
    resistance = sorted(resistance)[:3]

    # Support = top 3 PE OI strikes BELOW atm
    below = {s: v["pe_oi"] for s, v in oi_by_strike.items() if s < atm and v["pe_oi"] > 0}
    support = sorted(below, key=below.get, reverse=True)[:5]
    support = sorted(support, reverse=True)[:3]

    # If not enough data, use ATM-based defaults
    step_r = 100 if "BANK" in symbol else 50
    if len(resistance) < 3:
        resistance = [atm + step_r, atm + step_r*2, atm + step_r*3]
    if len(support) < 3:
        support = [atm - step_r, atm - step_r*2, atm - step_r*3]

    resistance = sorted(resistance)
    support    = sorted(support, reverse=True)

    return {
        "ul": ul,
        "atm": atm,
        "pcr": pcr,
        "max_pain": max_pain,
        "resistance": resistance,
        "support": support,
    }


def _calc_max_pain(oi_by_strike: dict) -> float:
    if not oi_by_strike:
        return 0.0
    strikes = list(oi_by_strike.keys())
    best = float("inf")
    mp   = 0.0
    for test in strikes:
        pain = sum(
            max(0, test - s) * v["pe_oi"] +
            max(0, s - test) * v["ce_oi"]
            for s, v in oi_by_strike.items()
        )
        if pain < best:
            best = pain
            mp   = test
    return mp


def detect_trend(ul: float, prev_close: float, resistance: list) -> str:
    """Simple trend detection from price position."""
    if ul <= 0 or prev_close <= 0:
        return "DOWNTREND"  # Default to bearish as per March 2026 context
    chg = (ul - prev_close) / prev_close * 100
    # Check if price near resistance
    near_res = any(abs(ul - r) / r < 0.005 for r in resistance)
    if chg < -1.5:
        return "DOWNTREND"
    elif chg > 1.5:
        return "UPTREND"
    elif near_res:
        return "DOWNTREND"  # At resistance in downtrend = bearish
    return "SIDEWAYS"


# ── STEP 3: Update config.py ──────────────────────────────────────

def update_config(bn: dict, n50: dict, vix: float):
    """Patch KEY_LEVELS and MARKET_CONTEXT in config.py."""
    print(bold("\n╔══ STEP 3: Updating config.py ══"))

    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        content = f.read()

    # ── KEY_LEVELS ────────────────────────────────────────────────
    bn_res  = [float(x) for x in sorted(bn.get("resistance", [54500, 55000, 55700]))]
    bn_sup  = [float(x) for x in sorted(bn.get("support", [53500, 53000, 52500]), reverse=True)]
    bn_mp   = float(bn.get("max_pain", 54000))
    n50_res = [float(x) for x in sorted(n50.get("resistance", [23100, 23400, 23800]))]
    n50_sup = [float(x) for x in sorted(n50.get("support", [22600, 22200, 21900]), reverse=True)]
    n50_mp  = float(n50.get("max_pain", 22700))

    today = datetime.now().strftime("%d %b %Y")
    new_levels = f'''KEY_LEVELS: Dict[str, Dict[str, List[float]]] = {{
    "BANKNIFTY": {{
        "resistance": [{bn_res[0]}, {bn_res[1]}, {bn_res[2]}],  # Auto-updated {today}
        "support":    [{bn_sup[0]}, {bn_sup[1]}, {bn_sup[2]}],
        "max_pain":   {bn_mp},
    }},
    "NIFTY": {{
        "resistance": [{n50_res[0]}, {n50_res[1]}, {n50_res[2]}],  # Auto-updated {today}
        "support":    [{n50_sup[0]}, {n50_sup[1]}, {n50_sup[2]}],
        "max_pain":   {n50_mp},
    }},
}}'''

    content = re.sub(
        r'KEY_LEVELS: Dict\[str, Dict\[str, List\[float\]\]\] = \{.*?\}',
        new_levels,
        content,
        flags=re.DOTALL
    )

    # ── MARKET_CONTEXT ────────────────────────────────────────────
    bn_ul    = bn.get("ul", 54000)
    bn_trend = detect_trend(bn_ul, bn.get("prev_close", bn_ul), bn_res)
    vix_trend = "ELEVATED" if vix > 18 else ("FALLING" if vix < 15 else "STABLE")

    # Estimate bounce day (rough — based on distance from recent low)
    bounce_from = bn.get("recent_low", 53757)
    bounce_day  = max(1, round((bn_ul - bounce_from) / 200))

    new_context = f'''MARKET_CONTEXT = {{
    "bn_daily_trend":    "{bn_trend}",
    "bn_bounce_day":     {bounce_day},
    "bn_bounce_from":    {bounce_from},
    "bn_bounce_origin":  "Auto",
    "nifty_daily_trend": "{detect_trend(n50.get('ul', 22870), n50.get('prev_close', 22870), n50_res)}",
    "monthly_bias":      "{'BEARISH' if bn_trend == 'DOWNTREND' else 'BULLISH'}",
    "vix_trend":         "{vix_trend}",
}}'''

    content = re.sub(
        r'MARKET_CONTEXT = \{.*?\}',
        new_context,
        content,
        flags=re.DOTALL
    )

    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(content)

    print(green(f"  ✅ KEY_LEVELS updated"))
    print(green(f"  ✅ MARKET_CONTEXT updated — trend: {bn_trend}, VIX: {vix_trend}"))


# ── STEP 4: Print War Plan ────────────────────────────────────────

def print_war_plan(bn: dict, n50: dict, vix: float):
    bn_ul  = bn.get("ul", 0)
    n50_ul = n50.get("ul", 0)
    pcr_bn = bn.get("pcr", 1.0)
    mp_bn  = bn.get("max_pain", 0)

    bias = "BEARISH" if pcr_bn < 0.9 else ("BULLISH" if pcr_bn > 1.2 else "NEUTRAL")
    vix_zone = ("SKIP" if vix < 12 else
                "REDUCED (trade 50% size)" if vix < 16 else
                "IDEAL ✅" if vix <= 22 else
                "ELEVATED (prefer straddle)" if vix <= 28 else "EXTREME (skip)")

    print(bold(cyan(f"""
╔══════════════════════════════════════════════════════╗
║         WAR PLAN — {datetime.now().strftime('%A %d %b %Y')}
╠══════════════════════════════════════════════════════╣
║  BankNifty:  {bn_ul:,.0f}
║  Nifty 50:   {n50_ul:,.0f}
║  India VIX:  {vix:.2f}  →  {vix_zone}
║  PCR BN:     {pcr_bn:.3f}  →  {bias}
║  Max Pain:   {mp_bn:,.0f}
╠══ BN Key Levels ════════════════════════════════════╣
║  Resistance: {bn.get('resistance', [])}
║  Support:    {bn.get('support', [])}
╠══ Nifty Key Levels ══════════════════════════════════╣
║  Resistance: {n50.get('resistance', [])}
║  Support:    {n50.get('support', [])}
╠══ Today's Bias ══════════════════════════════════════╣
║  {'⬇ PE on bounces to BN resistance — daily downtrend intact' if bias == 'BEARISH' else '⬆ CE on dips to support' if bias == 'BULLISH' else '◆ Wait for direction — neutral PCR'}
╠══ config.py ═════════════════════════════════════════╣
║  ✅ KEY_LEVELS auto-updated
║  ✅ MARKET_CONTEXT auto-updated
╚══════════════════════════════════════════════════════╝
    """)))
    print(f"  Now run: {green('python main.py')}")
    print()


# ── MAIN ──────────────────────────────────────────────────────────

def main():
    print(bold(cyan("\n⚡ WAR ROOM — Morning Auto-Update")))
    print(f"  {datetime.now().strftime('%A %d %b %Y %H:%M IST')}\n")

    # Step 1: Token
    token = refresh_token()

    # Step 2: Fetch data
    print(bold("\n╔══ STEP 2: Fetching Live Market Data ══"))

    # NSE session (for option chain)
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=8)
        print(green("  ✅ NSE session established"))
    except Exception:
        print(yellow("  ⚠ NSE session failed — will use Upstox only"))

    # Upstox index prices
    prices = fetch_upstox_indices(token)
    bn_ltp  = prices.get("BANKNIFTY", 0)
    n50_ltp = prices.get("NIFTY", 0)
    vix     = prices.get("VIX", 0)

    if bn_ltp > 0:
        print(green(f"  ✅ BankNifty: {bn_ltp:,.0f}"))
    else:
        print(yellow("  ⚠ BankNifty LTP unavailable (market closed?)"))
        bn_ltp = 54000  # fallback

    if n50_ltp > 0:
        print(green(f"  ✅ Nifty:     {n50_ltp:,.0f}"))
    else:
        n50_ltp = 22870  # fallback

    if vix > 0:
        print(green(f"  ✅ India VIX: {vix:.2f}"))
    else:
        vix = 21.5  # fallback

    # Option chain data
    print("  Fetching BankNifty option chain…")
    bn_data  = fetch_nse_option_chain("BANKNIFTY", session)
    bn  = parse_chain(bn_data, "BANKNIFTY")
    if not bn:
        # Upstox-based fallback — use ATM arithmetic
        step = 100
        atm  = round(bn_ltp / step) * step
        bn   = {
            "ul": bn_ltp, "atm": atm, "pcr": 0.82, "max_pain": atm - 100,
            "resistance": [atm+step, atm+step*2, atm+step*3],
            "support":    [atm-step, atm-step*2, atm-step*3],
            "prev_close": bn_ltp,
        }
        print(yellow("  ⚠ Using arithmetic fallback for BN levels"))
    else:
        bn["ul"] = bn_ltp or bn.get("ul", 54000)
        bn["prev_close"] = bn.get("ul", bn_ltp)
        print(green(f"  ✅ BN chain: PCR={bn['pcr']} MaxPain={bn['max_pain']}"))

    print("  Fetching Nifty option chain…")
    n50_data = fetch_nse_option_chain("NIFTY", session)
    n50 = parse_chain(n50_data, "NIFTY")
    if not n50:
        step = 50
        atm  = round(n50_ltp / step) * step
        n50  = {
            "ul": n50_ltp, "atm": atm, "pcr": 0.79, "max_pain": atm,
            "resistance": [atm+step, atm+step*2, atm+step*3],
            "support":    [atm-step, atm-step*2, atm-step*3],
            "prev_close": n50_ltp,
        }
        print(yellow("  ⚠ Using arithmetic fallback for Nifty levels"))
    else:
        n50["ul"] = n50_ltp or n50.get("ul", 22870)
        n50["prev_close"] = n50.get("ul", n50_ltp)
        print(green(f"  ✅ Nifty chain: PCR={n50['pcr']} MaxPain={n50['max_pain']}"))

    # Step 3: Update config.py
    update_config(bn, n50, vix)

    # Step 4: War plan
    print_war_plan(bn, n50, vix)


if __name__ == "__main__":
    main()
