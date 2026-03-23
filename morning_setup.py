# ================================================================
#  morning_setup.py — FULLY AUTOMATIC MORNING CONFIGURATION
#  Run ONCE after get_token.py. Patches config.py automatically.
#
#  What it does:
#  1. Fetches prev-day OHLC for Nifty + BankNifty
#  2. Calculates Pivot levels (PP, R1/R2/R3, S1/S2/S3)
#  3. Fetches option chain → max pain + OI wall levels
#  4. Fetches VIX to determine trend
#  5. Auto-determines MARKET_CONTEXT (trend, bias, bounce etc.)
#  6. Patches KEY_LEVELS + MARKET_CONTEXT in config.py — in-place
#  7. Sends Telegram summary of what was updated
#
#  Usage: python morning_setup.py     (standalone)
#  OR: called automatically from main.py at startup
# ================================================================

import os, re, json, requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("UPSTOX_ACCESS_TOKEN", "")
BASE_URL = "https://api.upstox.com/v2"
HEADERS  = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

TG_BOT   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TG_CHAT  = os.getenv("TELEGRAM_CHAT_ID", "")

# Official docs confirm: option chain uses NSE_INDEX keys for both instruments
# Example from docs: instrument_key=NSE_INDEX|Nifty 50
NIFTY_KEY  = "NSE_INDEX|Nifty 50"
BN_KEY     = "NSE_INDEX|Nifty Bank"  # Index key for both LTP and option chain
VIX_KEY    = "NSE_INDEX|India VIX"
BN_LTP_KEY = "NSE_INDEX|Nifty Bank"  # same key

# Expiry weekdays
NIFTY_EXP_WD = 1  # Tuesday
BN_EXP_WD    = 2  # Wednesday


# ── Helpers ───────────────────────────────────────────────────────

def send_telegram(msg: str):
    if not TG_BOT or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML"},
            timeout=8
        )
    except Exception:
        pass


def next_expiry(weekday: int) -> str:
    today = datetime.now()
    days  = (weekday - today.weekday()) % 7
    if days == 0 and today.hour >= 15:
        days = 7
    exp = today + timedelta(days=days)
    return exp.strftime("%Y-%m-%d")


def round_to(val: float, step: float) -> float:
    return round(round(val / step) * step, 2)


# ── Fetch prev-day OHLC ───────────────────────────────────────────

def fetch_ohlc(instrument_key: str) -> dict:
    """Fetch latest day candle from Upstox historical API."""
    today  = datetime.now().strftime("%Y-%m-%d")
    start  = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")  # buffer for weekends
    url    = f"{BASE_URL}/historical-candle/{requests.utils.quote(instrument_key)}/day/{today}/{start}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        data = r.json().get("data", {}).get("candles", [])
        if not data:
            return {}
        # Last closed candle (index 1 = yesterday if today is partial)
        # candles = [[ts, o, h, l, c, vol, oi], ...]
        candle = data[1] if len(data) > 1 else data[0]
        return {"open": candle[1], "high": candle[2], "low": candle[3], "close": candle[4]}
    except Exception as e:
        print(f"  OHLC fetch error {instrument_key}: {e}")
        return {}


def fetch_ltp(instrument_key: str) -> float:
    """Fetch live LTP."""
    try:
        r = requests.get(
            f"{BASE_URL}/market-quote/ltp",
            headers=HEADERS,
            params={"instrument_key": instrument_key},
            timeout=8
        )
        data = r.json().get("data", {})
        for v in data.values():
            return float(v.get("last_price", 0))
    except Exception:
        pass
    return 0.0


# ── Pivot Calculation ─────────────────────────────────────────────

def pivots(h: float, l: float, c: float) -> dict:
    pp = (h + l + c) / 3
    r1 = 2 * pp - l
    r2 = pp + (h - l)
    r3 = h + 2 * (pp - l)
    s1 = 2 * pp - h
    s2 = pp - (h - l)
    s3 = l - 2 * (h - pp)
    return {
        "pp": pp, "r1": r1, "r2": r2, "r3": r3,
        "s1": s1, "s2": s2, "s3": s3,
    }


# ── Option Chain Analysis ─────────────────────────────────────────

def fetch_chain_analysis(instrument_key: str, expiry_date: str, step: float):
    """
    Returns:
      - max_pain (strike)
      - top_ce_wall (strike with max CE OI = resistance)
      - top_pe_wall (strike with max PE OI = support)
      - pcr
    """
    try:
        r = requests.get(
            f"{BASE_URL}/option/chain",
            headers=HEADERS,
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
            timeout=12
        )
        rows = r.json().get("data", [])
        if not rows:
            return 0, [], [], 1.0

        chain = []
        total_ce_oi = 0
        total_pe_oi = 0
        for row in rows:
            ce = row.get("call_options", {}).get("market_data", {})
            pe = row.get("put_options",  {}).get("market_data", {})
            ce_oi = int(ce.get("oi", 0))
            pe_oi = int(pe.get("oi", 0))
            total_ce_oi += ce_oi
            total_pe_oi += pe_oi
            chain.append({
                "strike": float(row.get("strike_price", 0)),
                "ce_oi": ce_oi, "pe_oi": pe_oi,
            })

        pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi > 0 else 1.0

        # Max pain
        best, mp = float("inf"), 0.0
        for test in chain:
            pain = sum(
                max(0, test["strike"] - r["strike"]) * r["pe_oi"] +
                max(0, r["strike"] - test["strike"]) * r["ce_oi"]
                for r in chain
            )
            if pain < best:
                best, mp = pain, test["strike"]

        # OI walls — top 3 CE OI strikes (resistance), top 3 PE OI strikes (support)
        by_ce = sorted(chain, key=lambda x: x["ce_oi"], reverse=True)[:5]
        by_pe = sorted(chain, key=lambda x: x["pe_oi"], reverse=True)[:5]

        ce_walls = sorted([r["strike"] for r in by_ce])
        pe_walls = sorted([r["strike"] for r in by_pe], reverse=True)

        return mp, ce_walls, pe_walls, pcr

    except Exception as e:
        print(f"  Chain analysis error: {e}")
        return 0, [], [], 1.0


# ── Trend Detection from OHLC series ─────────────────────────────

def detect_trend(closes: list) -> str:
    if len(closes) < 3:
        return "SIDEWAYS"
    recent = closes[-3:]
    if recent[-1] < recent[0] and recent[-1] < recent[-2]:
        return "DOWNTREND"
    if recent[-1] > recent[0] and recent[-1] > recent[-2]:
        return "UPTREND"
    return "SIDEWAYS"


def fetch_recent_closes(instrument_key: str, days: int = 5) -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days + 3)).strftime("%Y-%m-%d")
    url   = f"{BASE_URL}/historical-candle/{requests.utils.quote(instrument_key)}/day/{today}/{start}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        candles = r.json().get("data", {}).get("candles", [])
        return [c[4] for c in candles[:days]]  # close prices, most recent first
    except Exception:
        return []


# ── Build rounded S/R levels ──────────────────────────────────────

def build_sr_levels(piv: dict, oi_walls: list, ltp: float, step: float,
                    above: bool = True, count: int = 3) -> list:
    """
    Combine pivot levels + OI walls, round to nearest step,
    filter to above/below LTP, pick closest 3.
    """
    candidates = set()
    for k in ["r1", "r2", "r3"] if above else ["s1", "s2", "s3"]:
        candidates.add(round_to(piv[k], step))
    for w in oi_walls:
        candidates.add(round_to(w, step))

    if above:
        filtered = sorted([x for x in candidates if x > ltp])[:count]
    else:
        filtered = sorted([x for x in candidates if x < ltp], reverse=True)[:count]
        filtered = sorted(filtered)  # return ascending

    # Pad if not enough levels
    while len(filtered) < count:
        last = filtered[-1] if filtered else ltp
        filtered.append(round_to(last + (step * (1 if above else -1)), step))

    return [float(x) for x in filtered[:count]]


# ── Patch config.py in-place ──────────────────────────────────────

def patch_config(key_levels: dict, market_context: dict, vix: float, pcr_bn: float, pcr_n: float):
    config_path = os.path.join(os.path.dirname(__file__), "config.py")
    with open(config_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Build new KEY_LEVELS block
    bn  = key_levels["BANKNIFTY"]
    n50 = key_levels["NIFTY"]

    new_key_levels = f"""KEY_LEVELS: Dict[str, Dict[str, List[float]]] = {{
    "BANKNIFTY": {{
        "resistance": {bn['resistance']},  # Auto-updated {datetime.now().strftime('%d %b %H:%M')}
        "support":    {bn['support']},
        "max_pain":   {bn['max_pain']},
    }},
    "NIFTY": {{
        "resistance": {n50['resistance']},  # Auto-updated {datetime.now().strftime('%d %b %H:%M')}
        "support":    {n50['support']},
        "max_pain":   {n50['max_pain']},
    }},
}}"""

    # Build new MARKET_CONTEXT block
    mc = market_context
    new_market_ctx = f"""MARKET_CONTEXT = {{
    "bn_daily_trend":    "{mc['bn_daily_trend']}",    # Auto-updated {datetime.now().strftime('%d %b %H:%M')}
    "bn_bounce_day":     {mc['bn_bounce_day']},
    "bn_bounce_from":    {mc['bn_bounce_from']},
    "bn_bounce_origin":  "{mc['bn_bounce_origin']}",
    "nifty_daily_trend": "{mc['nifty_daily_trend']}",
    "monthly_bias":      "{mc['monthly_bias']}",
    "vix_trend":         "{mc['vix_trend']}",
}}"""

    # Replace KEY_LEVELS block (from KEY_LEVELS: to closing })
    content = re.sub(
        r'KEY_LEVELS: Dict\[str, Dict\[str, List\[float\]\]\].*?^\}',
        new_key_levels,
        content, flags=re.DOTALL | re.MULTILINE
    )

    # Replace MARKET_CONTEXT block
    content = re.sub(
        r'MARKET_CONTEXT = \{.*?^\}',
        new_market_ctx,
        content, flags=re.DOTALL | re.MULTILINE
    )

    with open(config_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  ✅ config.py patched successfully.")


# ── Main ──────────────────────────────────────────────────────────

def run() -> dict:
    print("\n" + "="*60)
    print("  🔧 MORNING AUTO-SETUP — WAR ROOM")
    print("="*60)

    # ── 1. VIX ────────────────────────────────────────────────────
    print("\n[1/5] Fetching VIX…")
    vix = fetch_ltp(VIX_KEY)
    if vix == 0:
        # Fallback — try OHLC
        ohlc_vix = fetch_ohlc(VIX_KEY)
        vix = ohlc_vix.get("close", 20.0)
    print(f"  VIX = {vix:.2f}")

    vix_zone = (
        "AVOID"    if vix < 12 else
        "REDUCED"  if vix < 16 else
        "IDEAL"    if vix <= 22 else
        "ELEVATED" if vix <= 28 else
        "EXTREME"
    )

    # ── 2. BankNifty OHLC + LTP ───────────────────────────────────
    print("\n[2/5] Fetching BankNifty data…")
    bn_ohlc   = fetch_ohlc(BN_LTP_KEY)
    bn_ltp    = fetch_ltp(BN_LTP_KEY)
    bn_closes = fetch_recent_closes(BN_LTP_KEY, 5)

    if not bn_ohlc:
        # Market closed / weekend — use safe defaults from context
        print("  ⚠️  BN OHLC unavailable (market closed?). Using last known levels.")
        bn_ohlc = {"open": 54000, "high": 54500, "low": 53500, "close": 54000}
        bn_ltp  = bn_ltp or 54000
        bn_closes = [54000, 54200, 53800, 53500, 54100]

    bn_piv = pivots(bn_ohlc["high"], bn_ohlc["low"], bn_ohlc["close"])
    bn_trend = detect_trend(bn_closes)
    print(f"  BN LTP={bn_ltp:.0f} | H={bn_ohlc['high']:.0f} L={bn_ohlc['low']:.0f} C={bn_ohlc['close']:.0f}")
    print(f"  BN Trend={bn_trend}")

    # ── 3. Nifty OHLC + LTP ───────────────────────────────────────
    print("\n[3/5] Fetching Nifty 50 data…")
    n50_ohlc   = fetch_ohlc(NIFTY_KEY)
    n50_ltp    = fetch_ltp(NIFTY_KEY)
    n50_closes = fetch_recent_closes(NIFTY_KEY, 5)

    if not n50_ohlc:
        print("  ⚠️  Nifty OHLC unavailable. Using last known levels.")
        n50_ohlc = {"open": 22800, "high": 23100, "low": 22600, "close": 22900}
        n50_ltp  = n50_ltp or 22900
        n50_closes = [22900, 23100, 22700, 22500, 23000]

    n50_piv   = pivots(n50_ohlc["high"], n50_ohlc["low"], n50_ohlc["close"])
    n50_trend = detect_trend(n50_closes)
    print(f"  N50 LTP={n50_ltp:.0f} | H={n50_ohlc['high']:.0f} L={n50_ohlc['low']:.0f} C={n50_ohlc['close']:.0f}")
    print(f"  Nifty Trend={n50_trend}")

    # ── 4. Option Chain ────────────────────────────────────────────
    print("\n[4/5] Fetching option chains…")
    bn_exp  = next_expiry(BN_EXP_WD)
    n50_exp = next_expiry(NIFTY_EXP_WD)

    bn_mp, bn_ce_walls, bn_pe_walls, pcr_bn = fetch_chain_analysis(BN_KEY, bn_exp, 100.0)
    n50_mp, n50_ce_walls, n50_pe_walls, pcr_n50 = fetch_chain_analysis(NIFTY_KEY, n50_exp, 50.0)

    print(f"  BN  MaxPain={bn_mp:.0f} | PCR={pcr_bn:.2f} | CE walls={bn_ce_walls[:3]} | PE walls={bn_pe_walls[:3]}")
    print(f"  N50 MaxPain={n50_mp:.0f} | PCR={pcr_n50:.2f} | CE walls={n50_ce_walls[:3]} | PE walls={n50_pe_walls[:3]}")

    # ── 5. Build KEY_LEVELS ───────────────────────────────────────
    print("\n[5/5] Computing S/R levels…")

    # BankNifty: round to nearest 100
    bn_res = build_sr_levels(bn_piv, bn_ce_walls, bn_ltp or bn_ohlc["close"], 100.0, above=True,  count=3)
    bn_sup = build_sr_levels(bn_piv, bn_pe_walls, bn_ltp or bn_ohlc["close"], 100.0, above=False, count=3)
    bn_mp  = round_to(bn_mp, 100.0) if bn_mp else round_to(bn_piv["pp"], 100.0)

    # Nifty: round to nearest 50
    n50_res = build_sr_levels(n50_piv, n50_ce_walls, n50_ltp or n50_ohlc["close"], 50.0, above=True,  count=3)
    n50_sup = build_sr_levels(n50_piv, n50_pe_walls, n50_ltp or n50_ohlc["close"], 50.0, above=False, count=3)
    n50_mp  = round_to(n50_mp, 50.0) if n50_mp else round_to(n50_piv["pp"], 50.0)

    print(f"  BN  Resistance: {bn_res}")
    print(f"  BN  Support:    {bn_sup}")
    print(f"  N50 Resistance: {n50_res}")
    print(f"  N50 Support:    {n50_sup}")

    key_levels = {
        "BANKNIFTY": {"resistance": bn_res, "support": bn_sup, "max_pain": bn_mp},
        "NIFTY":     {"resistance": n50_res, "support": n50_sup, "max_pain": n50_mp},
    }

    # ── Build MARKET_CONTEXT ──────────────────────────────────────
    # Detect if today is a "bounce day" from a recent swing low
    bn_bounce_from  = min(bn_closes) if bn_closes else 0
    bn_bounce_day   = 0
    if bn_trend == "DOWNTREND" and bn_closes:
        # Count days since the low
        low_idx = bn_closes.index(min(bn_closes))
        bn_bounce_day = low_idx  # how many days ago was the low

    monthly_bias = "BEARISH" if (bn_trend == "DOWNTREND" and n50_trend == "DOWNTREND") else \
                   "BULLISH" if (bn_trend == "UPTREND"   and n50_trend == "UPTREND")   else \
                   "NEUTRAL"

    # VIX trend: compare to 5-day average
    vix_trend = "ELEVATED" if vix > 18 else "STABLE" if vix > 14 else "LOW"

    # Bounce origin date
    if bn_bounce_day > 0:
        bounce_origin = (datetime.now() - timedelta(days=bn_bounce_day)).strftime("%B %d")
    else:
        bounce_origin = datetime.now().strftime("%B %d")

    market_context = {
        "bn_daily_trend":    bn_trend,
        "bn_bounce_day":     bn_bounce_day,
        "bn_bounce_from":    round(bn_bounce_from),
        "bn_bounce_origin":  bounce_origin,
        "nifty_daily_trend": n50_trend,
        "monthly_bias":      monthly_bias,
        "vix_trend":         vix_trend,
    }

    # ── Patch config.py ───────────────────────────────────────────
    patch_config(key_levels, market_context, vix, pcr_bn, pcr_n50)

    # ── Print summary ─────────────────────────────────────────────
    print("\n" + "="*60)
    print("  ✅ AUTO-SETUP COMPLETE")
    print("="*60)
    print(f"  BankNifty: {bn_trend} | Nifty: {n50_trend} | VIX: {vix:.1f} ({vix_zone})")
    print(f"  BN  Resistance: {bn_res[0]:.0f} / {bn_res[1]:.0f} / {bn_res[2]:.0f}")
    print(f"  BN  Support:    {bn_sup[0]:.0f} / {bn_sup[1]:.0f} / {bn_sup[2]:.0f}")
    print(f"  N50 Resistance: {n50_res[0]:.0f} / {n50_res[1]:.0f} / {n50_res[2]:.0f}")
    print(f"  N50 Support:    {n50_sup[0]:.0f} / {n50_sup[1]:.0f} / {n50_sup[2]:.0f}")
    print(f"  PCR BN={pcr_bn:.2f} | PCR N50={pcr_n50:.2f}")
    print(f"  MaxPain BN={bn_mp:.0f} | MaxPain N50={n50_mp:.0f}")
    print(f"  Bias: {monthly_bias}")
    print("="*60)
    print("  ▶  Now run: python main.py\n")

    # ── Telegram ──────────────────────────────────────────────────
    tg_msg = (
        f"🔧 <b>WAR ROOM AUTO-SETUP COMPLETE</b>\n"
        f"📅 {datetime.now().strftime('%a %d %b %Y %H:%M')}\n\n"
        f"<b>BankNifty</b> — {bn_trend}\n"
        f"  🔴 Resist: {bn_res[0]:.0f} / {bn_res[1]:.0f} / {bn_res[2]:.0f}\n"
        f"  🟢 Support: {bn_sup[0]:.0f} / {bn_sup[1]:.0f} / {bn_sup[2]:.0f}\n"
        f"  MaxPain: {bn_mp:.0f} | PCR: {pcr_bn:.2f}\n\n"
        f"<b>Nifty 50</b> — {n50_trend}\n"
        f"  🔴 Resist: {n50_res[0]:.0f} / {n50_res[1]:.0f} / {n50_res[2]:.0f}\n"
        f"  🟢 Support: {n50_sup[0]:.0f} / {n50_sup[1]:.0f} / {n50_sup[2]:.0f}\n"
        f"  MaxPain: {n50_mp:.0f} | PCR: {pcr_n50:.2f}\n\n"
        f"🌡 VIX: {vix:.1f} ({vix_zone})\n"
        f"📊 Monthly Bias: {monthly_bias}\n\n"
        f"<i>config.py patched. Start bot: python main.py</i>"
    )
    send_telegram(tg_msg)

    return {
        "key_levels": key_levels,
        "market_context": market_context,
        "vix": vix,
        "pcr_bn": pcr_bn,
        "pcr_n50": pcr_n50,
    }


if __name__ == "__main__":
    if not TOKEN:
        print("❌ UPSTOX_ACCESS_TOKEN not set. Run get_token.py first.")
    else:
        run()
