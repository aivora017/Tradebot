# ================================================================
#  market_scanner.py — Pre-Market Morning Intelligence
#  Runs at 8:45 AM. Gathers all pre-market data and produces
#  today's war plan automatically.
# ================================================================

import requests
from datetime import datetime
from typing import Dict, Optional, List
from dataclasses import dataclass, field

import config
from logger_setup import get_logger
from utils import vix_zone, next_expiry, time_between, is_banknifty_expiry, is_nifty_expiry

log = get_logger("Scanner")

def _get_headers() -> dict:
    """
    FIX (BUG-4): Always read fresh token from config.
    Module-level HEADERS dict was set at import time and became stale after
    morning_prep.py refreshed the token. Now reads current token every call.
    """
    return {
        "Authorization": f"Bearer {config.UPSTOX_ACCESS_TOKEN}",
        "Accept":        "application/json",
    }

# ── FII/DII feed (P2.3) ───────────────────────────────────────────
NSE_FII_DII_URL = "https://www.nseindia.com/api/fiidiiTradeReact"
NSE_HOME_URL    = "https://www.nseindia.com"
_NSE_HEADERS    = {
    "User-Agent":      ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"),
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.nseindia.com/market-data/fii-dii-trading-activity",
}
_fii_dii_cache: dict = {"data": None, "fetched_at": None}


def fetch_fii_dii() -> dict:
    """Fetch FII/DII provisional data from NSE India (~11 AM IST).

    Returns dict:
        status       : "OK" | "PRE_11AM" | "UNAVAILABLE"
        net_fii      : float  — FII/FPI net equity (₹ Cr, + = buying)
        net_dii      : float  — DII net equity (₹ Cr, + = buying)
        bias         : "BULLISH_INST" | "BEARISH_INST" | "CAUTION" | "NEUTRAL" |
                       "PRE_11AM" | "UNAVAILABLE"
        date         : str    — trading date from NSE response
        fetched_at_str: str   — "HH:MM IST"

    Bias rules (from config thresholds):
        FII > +₹2000 Cr        → BULLISH_INST
        FII < -₹2000 Cr        → BEARISH_INST
        FII < 0 AND DII < 0    → CAUTION   (both institutions net selling)
        otherwise              → NEUTRAL
    Cached for 15 min (FII_CACHE_TTL_SECS) to avoid hammering NSE.
    """
    global _fii_dii_cache
    now = datetime.now()

    # Too early — NSE publishes provisional data around 11 AM
    if now.hour < 11:
        return {
            "status":         "PRE_11AM",
            "net_fii":        0.0,
            "net_dii":        0.0,
            "bias":           "PRE_11AM",
            "date":           "",
            "fetched_at_str": "",
        }

    # Serve cache if still fresh
    if (_fii_dii_cache["data"] is not None
            and _fii_dii_cache["fetched_at"] is not None):
        age = (now - _fii_dii_cache["fetched_at"]).total_seconds()
        if age < config.FII_CACHE_TTL_SECS:
            return _fii_dii_cache["data"]

    # ── Live fetch from NSE ───────────────────────────────────────
    try:
        sess = requests.Session()
        sess.headers.update(_NSE_HEADERS)
        # Warm the session to get NSE cookies (required for API access)
        sess.get(NSE_HOME_URL, timeout=8)
        r = sess.get(NSE_FII_DII_URL, timeout=10)
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")

        rows     = r.json()
        net_fii  = 0.0
        net_dii  = 0.0
        date_str = ""

        for row in rows:
            cat = row.get("category", "").upper()
            # netValue may be formatted with commas: "2,345.67"
            try:
                net = float(str(row.get("netValue", "0")).replace(",", "") or 0)
            except (ValueError, TypeError):
                net = 0.0
            if "FII" in cat or "FPI" in cat:
                net_fii  = net
                date_str = row.get("date", "")
            elif "DII" in cat:
                net_dii  = net

        # Classify institutional bias
        if net_fii > config.FII_BULLISH_THRESH:
            bias = "BULLISH_INST"
        elif net_fii < config.FII_BEARISH_THRESH:
            bias = "BEARISH_INST"
        elif net_fii < 0 and net_dii < 0:
            bias = "CAUTION"      # both institutions net selling → de-risk
        else:
            bias = "NEUTRAL"

        result = {
            "status":         "OK",
            "net_fii":        round(net_fii, 2),
            "net_dii":        round(net_dii, 2),
            "bias":           bias,
            "date":           date_str,
            "fetched_at_str": now.strftime("%H:%M IST"),
        }
        _fii_dii_cache = {"data": result, "fetched_at": now}
        log.info(
            f"FII/DII: FII={net_fii:+.0f} Cr  "
            f"DII={net_dii:+.0f} Cr  → {bias}"
        )
        return result

    except Exception as e:
        log.warning(f"FII/DII fetch failed: {e}")
        result = {
            "status":         "UNAVAILABLE",
            "net_fii":        0.0,
            "net_dii":        0.0,
            "bias":           "UNAVAILABLE",
            "date":           "",
            "fetched_at_str": "",
            "error":          str(e),
        }
        # Cache failures too — avoid hammering NSE on repeated errors
        _fii_dii_cache = {"data": result, "fetched_at": now}
        return result


# ── Heavyweight Stock Scanner (P2.5) ─────────────────────────────
# Lightweight 15-second cache — avoids recomputing on every strategy tick.
_hw_cache: dict = {"data": None, "ts": None}


def heavyweight_scan(feed) -> dict:
    """Scan BankNifty + Nifty heavyweight stocks for directional alignment.

    Uses live ticks already streaming via WebSocket (WATCH_KEYS includes stocks).
    Cached for HW_SCAN_CACHE_SECS (15s) to avoid per-tick recomputation.

    Returns dict:
        bank_stocks:     {HDFCBANK: {ltp, chg_pct, dir}, SBIN: ..., ICICIBANK: ...}
        nifty_stocks:    {RELIANCE: {ltp, chg_pct, dir}, INFY: ..., TCS: ...}
        bn_signal:       "BULLISH" | "BEARISH" | "MIXED" | "UNAVAILABLE"
        n50_signal:      "BULLISH" | "BEARISH" | "MIXED" | "UNAVAILABLE"
        hdfcbank_alert:  bool — True if HDFCBANK dropped > HW_DROP_ALERT_PCT in first 15m
        conviction:      "HIGH_CONV" | "STANDARD" | "MIXED" | "UNAVAILABLE"
        note:            str — human-readable summary line for Claude
        ts:              str — HH:MM when computed
    """
    global _hw_cache
    now = datetime.now()

    # Serve cache if fresh
    if (_hw_cache["data"] is not None and _hw_cache["ts"] is not None):
        age = (now - _hw_cache["ts"]).total_seconds()
        if age < config.HW_SCAN_CACHE_SECS:
            return _hw_cache["data"]

    def _stock_data(symbols: list) -> dict:
        out = {}
        for sym in symbols:
            tick = feed.get_tick(sym)
            if tick and tick.prev_close > 0 and tick.ltp > 0:
                chg = round((tick.ltp - tick.prev_close) / tick.prev_close * 100, 2)
                out[sym] = {
                    "ltp":     round(tick.ltp, 2),
                    "chg_pct": chg,
                    "dir":     "UP" if chg > 0 else "DOWN",
                }
            else:
                out[sym] = {"ltp": 0.0, "chg_pct": 0.0, "dir": "FLAT"}
        return out

    bank_data  = _stock_data(config.BANK_HEAVYWEIGHTS)
    nifty_data = _stock_data(config.NIFTY_HEAVYWEIGHTS)

    def _direction_signal(data_dict: dict) -> str:
        """Return BULLISH/BEARISH/MIXED/UNAVAILABLE based on agreement count."""
        active = [v for v in data_dict.values() if v["ltp"] > 0]
        if not active:
            return "UNAVAILABLE"
        up = sum(1 for v in active if v["dir"] == "UP")
        dn = sum(1 for v in active if v["dir"] == "DOWN")
        if up >= config.HW_AGREE_COUNT:
            return "BULLISH"
        if dn >= config.HW_AGREE_COUNT:
            return "BEARISH"
        return "MIXED"

    bn_signal  = _direction_signal(bank_data)
    n50_signal = _direction_signal(nifty_data)

    # ── HDFCBANK early warning (9:15–9:30) ───────────────────────
    hdfc_chg   = bank_data.get("HDFCBANK", {}).get("chg_pct", 0.0)
    hdfc_alert = (
        time_between("09:15", "09:30") and
        hdfc_chg < -config.HW_DROP_ALERT_PCT
    )

    # ── Overall conviction ────────────────────────────────────────
    if bn_signal == "UNAVAILABLE" and n50_signal == "UNAVAILABLE":
        conviction = "UNAVAILABLE"
    elif bn_signal in ("BULLISH", "BEARISH") and n50_signal == bn_signal:
        conviction = "HIGH_CONV"
    elif bn_signal == "MIXED" or n50_signal == "MIXED":
        conviction = "MIXED"
    else:
        conviction = "STANDARD"

    # ── Summary note for Claude reasoning ────────────────────────
    parts = []
    if hdfc_alert:
        parts.append(
            f"HDFCBANK -{abs(hdfc_chg):.1f}% in first 15m → BankNifty PUT tier elevated"
        )
    if conviction == "HIGH_CONV":
        parts.append(
            f"All heavyweights {bn_signal}: high-conviction {bn_signal} index trade"
        )
    elif bn_signal != "UNAVAILABLE":
        parts.append(f"Banks {bn_signal}, Nifty heavyweights {n50_signal}")
    note = "; ".join(parts) if parts else ""

    result = {
        "bank_stocks":    bank_data,
        "nifty_stocks":   nifty_data,
        "bn_signal":      bn_signal,
        "n50_signal":     n50_signal,
        "hdfcbank_alert": hdfc_alert,
        "conviction":     conviction,
        "note":           note,
        "ts":             now.strftime("%H:%M"),
    }
    _hw_cache = {"data": result, "ts": now}
    return result


@dataclass
class MorningIntel:
    date_str:       str
    day_of_week:    str
    nifty_prev:     float
    bn_prev:        float
    vix_prev:       float
    sgx_nifty:      float
    pcr_nifty:      float
    pcr_bn:         float
    max_pain_nifty: float
    max_pain_bn:    float
    nifty_expiry:   str
    bn_expiry:      str
    is_nifty_expiry:bool
    is_bn_expiry:   bool
    market_type:    str
    recommended_strategy: str
    bias:           str
    key_levels:     Dict
    notes:          str
    todays_events:  List  = field(default_factory=list)
    high_impact_news: List = field(default_factory=list)

    def summary(self) -> str:
        gift_str = (
            f"{self.sgx_nifty:.0f} ({config.GIFT_NIFTY_GAP_PCT:+.2f}%) → {config.GIFT_NIFTY_BIAS}"
            if self.sgx_nifty > 0 else "NOT FETCHED"
        )
        lines = [
            f"╔══ WAR PLAN — {self.date_str} ({self.day_of_week}) ══",
            f"║ BankNifty prev:  {self.bn_prev:.0f}",
            f"║ Nifty prev:      {self.nifty_prev:.0f}",
            f"║ India VIX:       {self.vix_prev:.1f} → {vix_zone(self.vix_prev)}",
            f"║ GIFT Nifty:      {gift_str}",
            f"║ PCR Nifty/BN:    {self.pcr_nifty:.2f} / {self.pcr_bn:.2f}",
            f"║ Max Pain N/BN:   {self.max_pain_nifty:.0f} / {self.max_pain_bn:.0f}",
            f"║ Nifty Expiry:    {self.nifty_expiry} {'← TODAY' if self.is_nifty_expiry else ''}",
            f"║ BN Expiry:       {self.bn_expiry} {'← TODAY' if self.is_bn_expiry else ''}",
            f"║ Market Type:     {self.market_type}",
            f"║ Bias:            {self.bias}",
            f"║ Strategy:        {self.recommended_strategy}",
            f"╠══ NOTES ══",
            f"║ {self.notes}",
        ]
        if self.todays_events:
            lines.append("╠══ TODAY'S EVENTS ══")
            for e in self.todays_events:
                if isinstance(e, dict):
                    lines.append(f"║ {e.get('time','')} [{e.get('impact','')}] {e.get('event','')}")
        if self.high_impact_news:
            lines.append("╠══ HIGH IMPACT NEWS ══")
            for n in self.high_impact_news[:3]:
                if isinstance(n, dict):
                    lines.append(f"║ [{n.get('source','')}] {n.get('headline','')[:80]}")
        lines.append("╚══════════════════════════════════════")
        return "\n".join(lines)


class MorningScanner:

    def run(self, news_feed=None) -> MorningIntel:
        log.info("Running morning pre-market scan…")
        today = datetime.now()

        pcr_n,  mp_n  = self._get_pcr_maxpain("NIFTY")
        pcr_bn, mp_bn = self._get_pcr_maxpain("BANKNIFTY")

        _, nifty_exp_str = next_expiry("NIFTY")
        _, bn_exp_str    = next_expiry("BANKNIFTY")
        is_n_exp  = is_nifty_expiry()     # weekly Tuesday (any Tuesday = Nifty expiry)
        is_bn_exp = is_banknifty_expiry() # LAST Tuesday of month only

        market_type, bias, strategy, notes = self._determine_market_type(
            pcr_n, pcr_bn, is_n_exp, is_bn_exp
        )

        # ── P2.2: Weave GIFT Nifty bias into morning intelligence ────
        gift_bias    = config.GIFT_NIFTY_BIAS      # written by morning_update.py
        gift_price   = config.GIFT_NIFTY_PRICE
        gift_gap_pct = config.GIFT_NIFTY_GAP_PCT
        gift_updated = config.GIFT_NIFTY_UPDATED

        if gift_bias not in ("UNAVAILABLE", ""):
            gap_sign = "+" if gift_gap_pct >= 0 else ""
            notes += (
                f" | 🌐 GIFT Nifty {gift_price:.0f} "
                f"({gap_sign}{gift_gap_pct:.2f}%) → {gift_bias}"
            )
            # Override market_type and bias if GIFT shows a strong gap
            if gift_bias == "BULLISH_GAP":
                if bias == "NEUTRAL":
                    bias = "BULLISH"
                if market_type == "Rangebound":
                    market_type = "Trending"
                    strategy = "S3 Gap-and-Go CE elevated + ORB CE. Avoid PE at open."
                notes += " | Elevate S3 Gap-and-Go CE. Strong gap — CE momentum preferred."
            elif gift_bias == "BEARISH_GAP":
                if bias == "NEUTRAL":
                    bias = "BEARISH"
                if market_type == "Rangebound":
                    market_type = "Trending"
                    strategy = "S3 Gap-and-Go PE elevated + ORB PE. Avoid CE at open."
                notes += " | Elevate S3 Gap-and-Go PE. Strong gap down — PE momentum preferred."
            elif gift_bias == "FLAT":
                notes += " | Flat open — elevate S6 S/R + S2 Straddle. Wait for 9:20 direction."
            elif gift_bias == "MILD_BULL":
                notes += " | Mild CE bias at open. Watch for gap fill before chasing."
            elif gift_bias == "MILD_BEAR":
                notes += " | Mild PE bias at open. Normal strategy priority."
        else:
            notes += " | ⚠️ GIFT Nifty not fetched — run morning_update.py before 9 AM."

        # ── News enrichment ──────────────────────────────────────
        todays_events    = []
        high_impact_news = []
        if news_feed:
            try:
                news_feed.force_poll()
                ctx              = news_feed.get_context_packet()
                todays_events    = ctx.get("upcoming_events", [])
                high_impact_news = ctx.get("high_impact_news", [])
                sentiment        = ctx.get("news_sentiment", "NEUTRAL")
                event_risk       = ctx.get("event_risk", "")

                if event_risk.startswith("HIGH") and market_type == "Rangebound":
                    market_type = "Event"
                    strategy    = "Straddle on Event or WAIT — high event risk"

                if sentiment == "BEARISH" and config.MARKET_CONTEXT.get("bn_daily_trend") == "DOWNTREND":
                    notes += " | 🔴 News BEARISH + BN downtrend = strong PE bias"
                elif sentiment == "BULLISH":
                    notes += " | 🟢 News BULLISH — watch CE setups"

                if event_risk.startswith("HIGH"):
                    notes += f" | ⚠️ EVENT RISK: {event_risk}"

                log.info(f"News: {len(high_impact_news)} HIGH | {len(todays_events)} events | sentiment={sentiment}")
            except Exception as e:
                log.error(f"News enrichment error: {e}")

        intel = MorningIntel(
            date_str=today.strftime("%d %b %Y"),
            day_of_week=today.strftime("%A"),
            nifty_prev=0,
            bn_prev=0,
            vix_prev=0,
            sgx_nifty=config.GIFT_NIFTY_PRICE,   # P2.2: populated from morning_update.py
            pcr_nifty=pcr_n,
            pcr_bn=pcr_bn,
            max_pain_nifty=mp_n,
            max_pain_bn=mp_bn,
            nifty_expiry=nifty_exp_str,
            bn_expiry=bn_exp_str,
            is_nifty_expiry=is_n_exp,
            is_bn_expiry=is_bn_exp,
            market_type=market_type,
            recommended_strategy=strategy,
            bias=bias,
            key_levels=config.KEY_LEVELS,
            notes=notes,
            todays_events=todays_events,
            high_impact_news=high_impact_news,
        )

        log.info(f"Morning scan: {market_type} | Bias: {bias}")
        log.info(intel.summary())
        return intel

    def _get_pcr_maxpain(self, symbol: str):
        try:
            exp_iso, _ = next_expiry(symbol)   # first elem is already YYYY-MM-DD ISO string
            key = config.NIFTY_INDEX_KEY if symbol == "NIFTY" else config.BANKNIFTY_INDEX_KEY
            r   = requests.get(
                f"{config.UPSTOX_BASE_URL}/option/chain",
                headers=_get_headers(),
                params={"instrument_key": key, "expiry_date": exp_iso},
                timeout=10
            )
            if r.status_code != 200:
                return 1.0, 0.0

            rows    = r.json().get("data", [])
            ce_oi   = sum(int(row.get("call_options", {}).get("market_data", {}).get("oi", 0)) for row in rows)
            pe_oi   = sum(int(row.get("put_options",  {}).get("market_data", {}).get("oi", 0)) for row in rows)
            pcr     = round(pe_oi / ce_oi, 3) if ce_oi > 0 else 1.0

            chain = []
            for row in rows:
                ce_md = row.get("call_options", {}).get("market_data", {})
                pe_md = row.get("put_options",  {}).get("market_data", {})
                chain.append({
                    "strike": float(row.get("strike_price", 0)),
                    "ce_oi":  int(ce_md.get("oi", 0)),
                    "pe_oi":  int(pe_md.get("oi", 0)),
                })
            mp = self._max_pain(chain)
            return pcr, mp
        except Exception as e:
            log.error(f"PCR/MaxPain {symbol}: {e}")
            return 1.0, 0.0

    @staticmethod
    def _max_pain(chain: list) -> float:
        if not chain:
            return 0.0
        best, mp_strike = float("inf"), 0.0
        for test in chain:
            pain = sum(
                max(0, test["strike"] - r["strike"]) * r["pe_oi"] +
                max(0, r["strike"] - test["strike"]) * r["ce_oi"]
                for r in chain
            )
            if pain < best:
                best, mp_strike = pain, test["strike"]
        return mp_strike

    def _determine_market_type(self, pcr_n, pcr_bn, is_n_exp, is_bn_exp):
        notes_parts = []
        bias        = "NEUTRAL"
        market_type = "Rangebound"
        strategy    = "S/R Reversal Bounce"

        if is_n_exp:
            market_type = "Expiry"
            strategy    = "All strategies + Gamma Scalp bonus at open (9:15–9:35)"
            notes_parts.append("⚡ NIFTY EXPIRY — Gamma bonus 9:15-9:35, then ALL strategies run normally. Exit by 14:00.")
        elif is_bn_exp:
            market_type = "Expiry"
            strategy    = "All strategies + Gamma Scalp BN bonus at open (9:15–9:35)"
            notes_parts.append("⚡ BANKNIFTY EXPIRY — Gamma bonus 9:15-9:35, then ALL strategies run normally. Exit by 14:00.")

        avg_pcr = (pcr_n + pcr_bn) / 2
        if avg_pcr > config.PARAMS.PCR_EXTREME_BULL:
            bias = "BULLISH"
            notes_parts.append(f"PCR {avg_pcr:.2f} extreme bullish — watch for CE buying.")
        elif avg_pcr > config.PARAMS.PCR_BULLISH_THRESH:
            bias = "BULLISH"
            notes_parts.append(f"PCR {avg_pcr:.2f} bullish — favor CE on dips.")
        elif avg_pcr < config.PARAMS.PCR_EXTREME_BEAR:
            bias = "BEARISH"
            notes_parts.append(f"PCR {avg_pcr:.2f} extreme bearish — watch for PE buying.")
        elif avg_pcr < config.PARAMS.PCR_BEARISH_THRESH:
            bias = "BEARISH"
            notes_parts.append(f"PCR {avg_pcr:.2f} bearish — favor PE on bounces.")
        else:
            notes_parts.append(f"PCR {avg_pcr:.2f} neutral — wait for direction after open.")

        day = datetime.now().weekday()
        if day == 0:
            notes_parts.append("Monday: Watch for gap fade or continuation.")
        elif day == 2:
            notes_parts.append("Wednesday: Historically high trending day. ORB preferred.")
        elif day == 4:
            notes_parts.append("Friday: Trend days common. Continuation trades preferred.")

        ctx = config.MARKET_CONTEXT
        if ctx.get("bn_daily_trend") == "DOWNTREND":
            notes_parts.append(
                f"BN DAILY DOWNTREND intact. Bias = PE on resistance. "
                f"Watch {config.KEY_LEVELS['BANKNIFTY']['resistance'][0]:.0f}–"
                f"{config.KEY_LEVELS['BANKNIFTY']['resistance'][1]:.0f} for rejection."
            )

        if market_type == "Rangebound":
            if bias == "BEARISH":
                strategy = "S/R Reversal PE at resistance + ORB PE if break confirms"
            elif bias == "BULLISH":
                strategy = "S/R Reversal CE at support + ORB CE if break confirms"

        return market_type, bias, strategy, " | ".join(notes_parts)
