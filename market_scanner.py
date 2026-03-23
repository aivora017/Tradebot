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
from utils import vix_zone, next_expiry

log = get_logger("Scanner")

HEADERS = {
    "Authorization": f"Bearer {config.UPSTOX_ACCESS_TOKEN}",
    "Accept": "application/json",
}


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
        lines = [
            f"╔══ WAR PLAN — {self.date_str} ({self.day_of_week}) ══",
            f"║ BankNifty prev:  {self.bn_prev:.0f}",
            f"║ Nifty prev:      {self.nifty_prev:.0f}",
            f"║ India VIX:       {self.vix_prev:.1f} → {vix_zone(self.vix_prev)}",
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
        is_n_exp  = today.weekday() == config.NIFTY_EXPIRY_WEEKDAY
        is_bn_exp = today.weekday() == config.BANKNIFTY_EXPIRY_WEEKDAY

        market_type, bias, strategy, notes = self._determine_market_type(
            pcr_n, pcr_bn, is_n_exp, is_bn_exp
        )

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
            sgx_nifty=0,
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
            _, expiry_str = next_expiry(symbol)
            exp_iso = datetime.strptime(expiry_str, "%d %b").replace(
                year=datetime.now().year).strftime("%Y-%m-%d")
            key = config.NIFTY_INDEX_KEY if symbol == "NIFTY" else config.BANKNIFTY_INDEX_KEY
            r   = requests.get(
                f"{config.UPSTOX_BASE_URL}/option/chain",
                headers=HEADERS,
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
            strategy    = "Gamma Scalp (9:15–9:35 ONLY, exit by 11 AM)"
            notes_parts.append("⚠️ NIFTY EXPIRY TODAY — Gamma play only. Exit by 11AM.")
        elif is_bn_exp:
            market_type = "Expiry"
            strategy    = "Gamma Scalp BN (9:15–9:35 ONLY, exit by 11 AM)"
            notes_parts.append("⚠️ BANKNIFTY EXPIRY TODAY — Gamma play only. Exit by 11AM.")

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
