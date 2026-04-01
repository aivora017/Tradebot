# ================================================================
#  bot_memory.py — Persistent Trade Memory + Self-Evolution Engine
#
#  The bot remembers every trade it ever took:
#    - What strategy fired, when, why
#    - Full market context at entry (VIX, bias, PCR, FII, Greeks)
#    - What Claude's exact confidence + reasoning was
#    - What happened (outcome: T1/T2/SL/TIME_EXIT, P&L%)
#
#  EOD analysis derives patterns:
#    - Strategy win rates (+ avoid flags for underperformers)
#    - Time slot performance
#    - VIX zone performance
#    - Session bias alignment vs counter-trend
#    - Natural language rules Claude applies next session
#
#  Self-evolution loop:
#    main.py start → ClaudeAnalyst loads learned context into SYSTEM
#    main.py stop  → bot_memory.run_eod_analysis() derives new patterns
#    Next session  → Claude sees its own track record and adjusts decisions
# ================================================================

import json
import os
import threading
from datetime import datetime
from typing import Optional, List, Dict

from logger_setup import get_logger

log = get_logger("BotMemory")

MEMORY_FILE   = "data/trade_memory.json"
PATTERNS_FILE = "data/learned_patterns.json"
EVOLUTION_LOG = "data/evolution_log.md"
MIN_SAMPLES   = 5   # minimum trades before drawing statistical conclusions


class BotMemory:
    """
    Singleton. Instantiated once at module level.
    Thread-safe: _lock protects _memory and _entry_contexts.
    """

    def __init__(self):
        self._lock            = threading.Lock()
        self._entry_contexts: Dict[str, dict] = {}  # trade_id → full context at entry
        self._memory:         List[dict]       = []  # all closed trade records (cumulative)
        self._load()

    # ── Public API ────────────────────────────────────────────────

    def attach_entry_context(self, trade_id: str, context: dict):
        """
        Call immediately after open_trade() succeeds.
        Captures the full market context at the moment of trade entry:
          - Claude's decision (confidence, reasoning, strategy)
          - Market state (VIX, session_bias, gift_bias, fii_bias, pcr_signal)
          - Technicals (rsi, ema_alignment, trend)
          - Time, expiry day flag
        """
        with self._lock:
            self._entry_contexts[trade_id] = {
                **context,
                "captured_at": datetime.now().isoformat(),
            }
        log.debug(f"📌 Context attached for trade [{trade_id}]")

    def record_closed_trade(self, trade):
        """
        Called from trade_tracker.close_trade().
        Merges entry context + trade outcome into persistent memory store.
        Immediately persists to data/trade_memory.json.
        """
        with self._lock:
            ctx = self._entry_contexts.pop(trade.id, {})

        entry_time = trade.entry_time
        exit_time  = trade.exit_time or datetime.now()
        duration   = int((exit_time - entry_time).total_seconds() / 60)

        pnl_pct = 0.0
        if trade.entry_premium > 0:
            pnl_pct = (trade.exit_premium - trade.entry_premium) / trade.entry_premium * 100

        # Classify outcome
        if pnl_pct >= trade.t2_pct * 100:
            outcome = "T2_HIT"
        elif trade.t1_booked:
            outcome = "T1_HIT"
        elif pnl_pct < 0:
            outcome = "SL_HIT"
        else:
            outcome = "TIME_EXIT"

        record = {
            # ── Identity ──────────────────────────────────────────────
            "trade_id":       trade.id,
            "date":           entry_time.strftime("%Y-%m-%d"),
            "time_of_entry":  entry_time.strftime("%H:%M"),
            "time_of_exit":   exit_time.strftime("%H:%M"),
            "duration_mins":  duration,
            # ── Trade parameters ──────────────────────────────────────
            "strategy":       trade.strategy,
            "symbol":         trade.symbol,
            "direction":      trade.option_type,
            "tier":           trade.tier,
            "strike":         trade.strike,
            "lots":           trade.lots,
            "entry_premium":  round(trade.entry_premium, 2),
            "exit_premium":   round(trade.exit_premium,  2),
            "sl_pct":         round(trade.sl_pct * 100, 1),
            "t1_pct":         round(trade.t1_pct * 100, 1),
            "t2_pct":         round(trade.t2_pct * 100, 1),
            # ── Outcome ───────────────────────────────────────────────
            "outcome":        outcome,
            "pnl_pct":        round(pnl_pct, 2),
            "pnl_rs":         round(trade.realized_pnl_rs, 2),
            "exit_reason":    trade.exit_reason,
            "t1_booked":      trade.t1_booked,
        }

        # Merge entry context (VIX, bias, PCR, FII, RSI, Claude reasoning…)
        for k, v in ctx.items():
            if k != "captured_at" and k not in record:
                record[k] = v

        with self._lock:
            self._memory.append(record)

        self._save()
        emoji = "✅" if pnl_pct > 0 else "❌"
        log.info(
            f"📝 Memory saved [{trade.id}] {trade.strategy} → "
            f"{outcome} {pnl_pct:+.1f}% {emoji}"
        )

    def run_eod_analysis(self) -> Optional[dict]:
        """
        EOD pattern analysis. Call from main.py stop().
        Reads ALL accumulated closed trades, derives patterns, writes:
          data/learned_patterns.json  — machine-readable stats + rules
          data/evolution_log.md       — human-readable append-only journal
        Returns the patterns dict (or None if not enough data).
        """
        with self._lock:
            closed = [r for r in self._memory if r.get("outcome")]

        n = len(closed)
        if n < 3:
            log.info(f"🧠 Only {n} trades in memory — skipping EOD analysis (need ≥3).")
            return None

        log.info(f"🧠 Running EOD analysis on {n} trades…")

        wins    = [r for r in closed if r["pnl_pct"] > 0]
        win_rate = len(wins) / n
        avg_pnl  = sum(r["pnl_pct"] for r in closed) / n

        strategy_perf  = self._analyze_by_key(closed, "strategy")
        time_slot_perf = self._analyze_by_slot(closed)
        vix_zone_perf  = self._analyze_by_vix(closed)
        bias_alignment = self._analyze_bias_alignment(closed)
        rules          = self._derive_rules(
            closed, strategy_perf, time_slot_perf, bias_alignment
        )

        patterns = {
            "last_updated":          datetime.now().isoformat(),
            "total_trades":          n,
            "win_rate":              round(win_rate, 3),
            "avg_pnl_pct":           round(avg_pnl, 2),
            "strategy_performance":  strategy_perf,
            "time_slot_performance": time_slot_perf,
            "vix_zone_performance":  vix_zone_perf,
            "session_bias_alignment": bias_alignment,
            "rules_learned":         rules,
        }

        os.makedirs("data", exist_ok=True)
        with open(PATTERNS_FILE, "w") as f:
            json.dump(patterns, f, indent=2)

        self._append_evolution_log(patterns)

        log.info(
            f"🧠 EOD complete: {n} trades | WR {win_rate:.1%} | "
            f"Avg P&L {avg_pnl:+.1f}% | {len(rules)} rules learned"
        )
        for r in rules:
            log.info(f"   📖 {r}")

        return patterns

    def load_learned_context(self) -> str:
        """
        Returns compact learned-pattern string for injection into Claude's SYSTEM prompt.
        Called at ClaudeAnalyst.__init__() — runs once per session.
        Max ~450 tokens. Returns "" if no patterns file or < MIN_SAMPLES trades.
        """
        try:
            if not os.path.exists(PATTERNS_FILE):
                return ""
            with open(PATTERNS_FILE) as f:
                p = json.load(f)
        except Exception:
            return ""

        n = p.get("total_trades", 0)
        if n < MIN_SAMPLES:
            return ""

        updated  = p.get("last_updated", "")[:10]
        wr       = p.get("win_rate", 0) * 100
        avg_pnl  = p.get("avg_pnl_pct", 0)

        lines = [
            "",
            f"=== BOT SELF-LEARNED PATTERNS (updated {updated} | {n} trades total) ===",
            f"Overall win rate: {wr:.1f}% | Avg P&L per trade: {avg_pnl:+.1f}%",
            "",
            "STRATEGY TRACK RECORD (apply to confidence scoring):",
        ]

        sp = p.get("strategy_performance", {})
        for strat, d in sorted(sp.items(), key=lambda x: -x[1]["trades"]):
            flag = d.get("flag", "")
            lines.append(
                f"  {strat}: {d['trades']}T | {d['win_rate']*100:.0f}%WR | "
                f"avg {d['avg_pnl_pct']:+.1f}% | {flag}"
            )

        rules = p.get("rules_learned", [])
        if rules:
            lines.append("")
            lines.append("SELF-LEARNED RULES (apply automatically, these override defaults):")
            for i, rule in enumerate(rules[:7], 1):
                lines.append(f"  {i}. {rule}")

        tp = p.get("time_slot_performance", {})
        if tp:
            best  = max(tp.items(), key=lambda x: x[1].get("win_rate", 0))
            worst = min(tp.items(), key=lambda x: x[1].get("win_rate", 1))
            lines.append("")
            lines.append(
                f"TIME SLOTS: Best={best[0]} ({best[1]['win_rate']*100:.0f}%WR, "
                f"{best[1]['trades']}T) | "
                f"Weakest={worst[0]} ({worst[1]['win_rate']*100:.0f}%WR, "
                f"{worst[1]['trades']}T)"
            )

        ba = p.get("session_bias_alignment", {})
        if ba.get("aligned_trades", 0) >= 5 and ba.get("counter_trades", 0) >= 5:
            lines.append(
                f"BIAS ALIGNMENT: Aligned trades WR={ba['aligned_wr']*100:.0f}% | "
                f"Counter-trend WR={ba['counter_wr']*100:.0f}% — "
                + ("STRONG alignment edge — favour aligned signals" if
                   ba["aligned_wr"] - ba["counter_wr"] > 0.15 else
                   "Alignment edge weak — technicals > bias")
            )

        lines.append("=== END SELF-LEARNED PATTERNS ===")
        return "\n".join(lines)

    # ── Analysis helpers ──────────────────────────────────────────

    def _analyze_by_key(self, closed: list, key: str) -> dict:
        """Group trades by a string key, compute win_rate + avg_pnl."""
        groups: Dict[str, dict] = {}
        for r in closed:
            k = r.get(key, "Unknown") or "Unknown"
            if k not in groups:
                groups[k] = {"trades": 0, "wins": 0, "pnl_sum": 0.0}
            groups[k]["trades"] += 1
            groups[k]["wins"]   += 1 if r["pnl_pct"] > 0 else 0
            groups[k]["pnl_sum"] += r["pnl_pct"]

        result = {}
        for k, d in groups.items():
            t  = d["trades"]
            wr = d["wins"] / t
            avg = d["pnl_sum"] / t
            result[k] = {
                "trades":      t,
                "wins":        d["wins"],
                "win_rate":    round(wr, 3),
                "avg_pnl_pct": round(avg, 2),
                "avoid":       wr < 0.35 and t >= MIN_SAMPLES,
                "flag": (
                    "❌ AVOID"     if wr < 0.35 and t >= MIN_SAMPLES else
                    "⚠️ WEAK"      if wr < 0.50 and t >= MIN_SAMPLES else
                    "✅ WORKING"   if wr >= 0.60 and t >= MIN_SAMPLES else
                    "📊 TRACKING"
                ),
            }
        return result

    def _analyze_by_slot(self, closed: list) -> dict:
        def slot(t: str) -> str:
            if not t:
                return "unknown"
            try:
                h = int(t.split(":")[0])
            except ValueError:
                return "unknown"
            if h < 10:
                return "09:15-10:00"
            if h < 11:
                return "10:00-11:00"
            if h < 12:
                return "11:00-12:00"
            if h < 13:
                return "12:00-13:00"
            if h < 14:
                return "13:00-14:00"
            return "14:00-15:10"

        groups: Dict[str, dict] = {}
        for r in closed:
            s = slot(r.get("time_of_entry", ""))
            if s not in groups:
                groups[s] = {"trades": 0, "wins": 0, "pnl_sum": 0.0}
            groups[s]["trades"] += 1
            groups[s]["wins"]   += 1 if r["pnl_pct"] > 0 else 0
            groups[s]["pnl_sum"] += r["pnl_pct"]

        return {
            s: {
                "trades":   d["trades"],
                "win_rate": round(d["wins"] / d["trades"], 3),
                "avg_pnl":  round(d["pnl_sum"] / d["trades"], 2),
            }
            for s, d in groups.items()
        }

    def _analyze_by_vix(self, closed: list) -> dict:
        def bucket(v) -> str:
            if v is None:
                return "unknown"
            try:
                v = float(v)
            except (TypeError, ValueError):
                return "unknown"
            if v < 14:
                return "<14"
            if v < 22:
                return "14-22"
            if v < 28:
                return "22-28"
            return ">28"

        groups: Dict[str, dict] = {}
        for r in closed:
            b = bucket(r.get("vix"))
            if b not in groups:
                groups[b] = {"trades": 0, "wins": 0, "pnl_sum": 0.0}
            groups[b]["trades"] += 1
            groups[b]["wins"]   += 1 if r["pnl_pct"] > 0 else 0
            groups[b]["pnl_sum"] += r["pnl_pct"]

        return {
            b: {
                "trades":   d["trades"],
                "win_rate": round(d["wins"] / d["trades"], 3),
                "avg_pnl":  round(d["pnl_sum"] / d["trades"], 2),
            }
            for b, d in groups.items()
        }

    def _analyze_bias_alignment(self, closed: list) -> dict:
        aligned = [r for r in closed if self._is_aligned(r)]
        counter = [r for r in closed if self._is_counter(r)]
        a_wr = sum(1 for r in aligned if r["pnl_pct"] > 0) / len(aligned) if aligned else 0.0
        c_wr = sum(1 for r in counter if r["pnl_pct"] > 0) / len(counter) if counter else 0.0
        return {
            "aligned_trades": len(aligned),
            "aligned_wr":     round(a_wr, 3),
            "counter_trades": len(counter),
            "counter_wr":     round(c_wr, 3),
        }

    def _is_aligned(self, r: dict) -> bool:
        bias = r.get("session_bias", "NEUTRAL")
        d    = r.get("direction", "")
        return (
            (bias == "BULLISH" and "CE" in d) or
            (bias == "BEARISH" and "PE" in d) or
            bias == "NEUTRAL" or
            d == "CE+PE"
        )

    def _is_counter(self, r: dict) -> bool:
        bias = r.get("session_bias", "NEUTRAL")
        d    = r.get("direction", "")
        return (bias == "BULLISH" and d == "PE") or (bias == "BEARISH" and d == "CE")

    def _derive_rules(
        self,
        closed: list,
        strategy_perf: dict,
        time_slot_perf: dict,
        bias_alignment: dict,
    ) -> List[str]:
        rules = []

        # Rule type 1: Underperforming strategies → avoid / reduce
        for strat, d in strategy_perf.items():
            if d["avoid"]:
                rules.append(
                    f"{strat} has {d['win_rate']*100:.0f}% WR in {d['trades']} trades — "
                    f"size down to WEAK or SKIP this setup"
                )

        # Rule type 2: High-performing strategies → size up
        for strat, d in strategy_perf.items():
            if d["win_rate"] >= 0.65 and d["trades"] >= MIN_SAMPLES:
                rules.append(
                    f"{strat} wins {d['win_rate']*100:.0f}% in {d['trades']} trades — "
                    f"size up to STRONG/GODMODE when this fires"
                )

        # Rule type 3: Bad time slots
        for slot, d in time_slot_perf.items():
            if d["trades"] >= MIN_SAMPLES and d["win_rate"] < 0.35:
                rules.append(
                    f"Avoid new entries in {slot} window "
                    f"({d['win_rate']*100:.0f}% WR, {d['trades']} trades)"
                )

        # Rule type 4: Bias alignment edge
        a, c = bias_alignment.get("aligned_trades", 0), bias_alignment.get("counter_trades", 0)
        if a >= MIN_SAMPLES and c >= MIN_SAMPLES:
            a_wr = bias_alignment.get("aligned_wr", 0)
            c_wr = bias_alignment.get("counter_wr", 0)
            if a_wr - c_wr > 0.20:
                rules.append(
                    f"Bias alignment edge is STRONG: aligned={a_wr*100:.0f}%WR vs "
                    f"counter={c_wr*100:.0f}%WR — deprioritise counter-trend trades on strong bias days"
                )

        # Rule type 5: VIX zone insight (best performing zone)
        # Computed inline from closed trades for accuracy
        vix_buckets: Dict[str, list] = {}
        for r in closed:
            try:
                v = float(r.get("vix") or 0)
            except (TypeError, ValueError):
                continue
            b = "14-22" if 14 <= v < 22 else "22-28" if 22 <= v < 28 else "<14" if v < 14 else ">28"
            vix_buckets.setdefault(b, []).append(r["pnl_pct"])

        for b, pnls in vix_buckets.items():
            if len(pnls) >= MIN_SAMPLES:
                wr = sum(1 for p in pnls if p > 0) / len(pnls)
                if wr >= 0.70:
                    rules.append(
                        f"VIX zone {b} is your sweet spot: {wr*100:.0f}% WR "
                        f"in {len(pnls)} trades — full size in this zone"
                    )

        return rules[:8]   # cap to keep SYSTEM prompt lean

    # ── Persistence ───────────────────────────────────────────────

    def _save(self):
        try:
            os.makedirs("data", exist_ok=True)
            with self._lock:
                data = list(self._memory)
            with open(MEMORY_FILE, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Memory save error: {e}")

    def _load(self):
        try:
            with open(MEMORY_FILE) as f:
                self._memory = json.load(f)
            log.info(f"🧠 Loaded {len(self._memory)} trades from persistent memory.")
        except FileNotFoundError:
            self._memory = []
            log.info("🧠 No prior memory found — starting fresh.")
        except Exception as e:
            log.error(f"Memory load error: {e}")
            self._memory = []

    def _append_evolution_log(self, patterns: dict):
        n       = patterns["total_trades"]
        wr      = patterns["win_rate"] * 100
        avg_pnl = patterns["avg_pnl_pct"]
        today   = datetime.now().strftime("%Y-%m-%d %H:%M IST")
        rules   = patterns.get("rules_learned", [])

        entry  = f"\n## {today} — EOD Analysis ({n} cumulative trades)\n"
        entry += f"**Win rate:** {wr:.1f}%  |  **Avg P&L:** {avg_pnl:+.1f}%\n\n"

        entry += "### Strategy Performance\n"
        for strat, d in patterns.get("strategy_performance", {}).items():
            entry += (
                f"- {strat}: {d['trades']}T | {d['win_rate']*100:.0f}%WR | "
                f"avg {d['avg_pnl_pct']:+.1f}% | {d.get('flag','')}\n"
            )

        sp = patterns.get("time_slot_performance", {})
        if sp:
            entry += "\n### Time Slot Performance\n"
            for slot, d in sorted(sp.items()):
                entry += f"- {slot}: {d['trades']}T | {d['win_rate']*100:.0f}%WR | avg {d['avg_pnl']:+.1f}%\n"

        if rules:
            entry += "\n### Rules Learned This Session\n"
            for r in rules:
                entry += f"- {r}\n"

        entry += "\n---\n"

        os.makedirs("data", exist_ok=True)
        with open(EVOLUTION_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        log.info(f"📖 Evolution log updated: {EVOLUTION_LOG}")


# ── Module-level singleton ────────────────────────────────────────
# Import and use: from bot_memory import get_memory, bot_memory
bot_memory = BotMemory()


def get_memory() -> BotMemory:
    return bot_memory
