# ================================================================
#  data_feed.py — Live Market Data via Upstox V3 WebSocket + REST
#
#  Based on official Upstox V3 API documentation:
#  https://upstox.com/developer/api-documentation/v3/get-market-data-feed
#
#  V3 KEY FACTS (from docs):
#  1. Auth: GET /v3/feed/market-data-feed/authorize → one-time wss:// URL
#  2. Subscription: must be sent as BINARY bytes (not text)
#  3. V3 message structure: feeds[key].fullFeed.marketFF (NOT ff.marketFF)
#  4. Close price (prev close) = ltpc.cp in WS OR ohlc.close in REST
#  5. REST full quotes: GET /v2/market-quote/quotes
#     - Response key format: "NSE_INDEX:Nifty 50" (colon, not pipe)
#     - Fields: last_price, ohlc.{open,high,low,close}, volume, net_change
#  6. REST OHLC v3: GET /v3/market-quote/ohlc — faster for bulk OHLC
# ================================================================

import json
import time
import threading
import requests
import websocket
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Callable
from dataclasses import dataclass, field

import config
from logger_setup import get_logger
from utils import next_expiry, normalize_symbol, change_pct

log = get_logger("DataFeed")


@dataclass
class Tick:
    symbol:    str
    ltp:       float
    open:      float
    high:      float
    low:       float
    prev_close: float
    volume:    int
    oi:        int
    bid:       float
    ask:       float
    ts:        datetime = field(default_factory=datetime.now)

    @property
    def change_pct(self) -> float:
        return change_pct(self.ltp, self.prev_close)

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass
class Candle:
    open:   float
    high:   float
    low:    float
    close:  float
    volume: int
    ts:     datetime

    @property
    def body_size(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def range(self) -> float:
        return self.high - self.low


@dataclass
class OptionRow:
    strike:    float
    expiry:    str
    ce_ltp:    float
    ce_oi:     int
    ce_oi_chg: int
    ce_iv:     float
    ce_vol:    int
    pe_ltp:    float
    pe_oi:     int
    pe_oi_chg: int
    pe_iv:     float
    pe_vol:    int


class CandleBuilder:
    def __init__(self, interval_sec: int):
        self.interval = interval_sec
        self.candles:  Dict[str, deque] = defaultdict(lambda: deque(maxlen=300))
        self._current: Dict[str, dict]  = {}
        self._lock = threading.Lock()

    def _bucket(self, ts: datetime) -> datetime:
        e = int(ts.timestamp())
        return datetime.fromtimestamp((e // self.interval) * self.interval)

    def push(self, symbol: str, tick: Tick) -> Optional[Candle]:
        b = self._bucket(tick.ts)
        completed = None
        with self._lock:
            cur = self._current.get(symbol)
            if cur is None or cur["b"] != b:
                if cur:
                    c = Candle(cur["o"], cur["h"], cur["l"], cur["c"], cur["v"], cur["b"])
                    self.candles[symbol].append(c)
                    completed = c
                self._current[symbol] = {
                    "b": b, "o": tick.ltp, "h": tick.ltp,
                    "l": tick.ltp, "c": tick.ltp, "v": tick.volume
                }
            else:
                cur["h"] = max(cur["h"], tick.ltp)
                cur["l"] = min(cur["l"], tick.ltp)
                cur["c"] = tick.ltp
                cur["v"] += tick.volume
        return completed

    def get(self, symbol: str, n: int = 100, include_partial: bool = True) -> List[Candle]:
        with self._lock:
            candles = list(self.candles[symbol])
            if include_partial:
                cur = self._current.get(symbol)
                if cur:
                    candles.append(Candle(cur["o"], cur["h"], cur["l"],
                                          cur["c"], cur["v"], cur["b"]))
            return candles[-n:]

    def reset(self, symbol: str):
        with self._lock:
            self.candles[symbol].clear()
            self._current.pop(symbol, None)


class MarketDataFeed:
    """
    Upstox V3 market data feed.
    - Primary: WebSocket V3 (real-time ticks, ~ms latency)
    - Fallback: REST polling every 3s (when WS unavailable)
    - Both deliver Nifty, BankNifty, VIX with full OHLC + prev_close
    """

    # Map internal symbol name → Upstox instrument key
    INSTRUMENT_KEYS = {
        "NIFTY":     config.NIFTY_INDEX_KEY,
        "BANKNIFTY": config.BANKNIFTY_INDEX_KEY,
        "India_VIX": config.VIX_INDEX_KEY,
    }

    def __init__(self):
        self._ws:        Optional[websocket.WebSocketApp] = None
        self._connected: bool = False
        self._ticks:     Dict[str, Tick] = {}
        self._vix:       float = 0.0
        self._pcr:       Dict[str, float] = {"NIFTY": 1.0, "BANKNIFTY": 1.0}
        self._chain:     Dict[str, List[OptionRow]] = {}
        self._max_pain:  Dict[str, float] = {}
        self._lock       = threading.RLock()
        self._callbacks: List[Callable[[Tick], None]] = []
        self._stop       = threading.Event()

        self.c1m  = CandleBuilder(60)
        self.c5m  = CandleBuilder(300)
        self.c15m = CandleBuilder(900)

        self._headers = {
            "Authorization": f"Bearer {config.UPSTOX_ACCESS_TOKEN}",
            "Accept": "application/json",
        }

    # ── Public API ────────────────────────────────────────────────

    def start(self):
        log.info("Starting market data feed…")
        threading.Thread(target=self._ws_loop,       daemon=True).start()
        threading.Thread(target=self._rest_ltp_loop, daemon=True).start()
        threading.Thread(target=self._chain_loop,    daemon=True).start()
        log.info("Data feed threads launched.")

    def stop(self):
        self._stop.set()
        if self._ws:
            try: self._ws.close()
            except Exception: pass

    def on_tick(self, fn: Callable[[Tick], None]):
        self._callbacks.append(fn)

    def get_tick(self, symbol: str) -> Optional[Tick]:
        with self._lock:
            return self._ticks.get(symbol)

    def get_ltp(self, symbol: str) -> float:
        t = self.get_tick(symbol)
        return t.ltp if t else 0.0

    def get_vix(self) -> float:
        with self._lock:
            return self._vix

    def get_pcr(self, symbol: str = "BANKNIFTY") -> float:
        with self._lock:
            return self._pcr.get(symbol, 1.0)

    def get_chain(self, symbol: str) -> List[OptionRow]:
        with self._lock:
            return list(self._chain.get(symbol, []))

    def get_max_pain(self, symbol: str) -> float:
        with self._lock:
            return self._max_pain.get(symbol, 0.0)

    def get_candles(self, symbol: str, interval: str = "5m", n: int = 100) -> List[Candle]:
        builder = {"1m": self.c1m, "5m": self.c5m, "15m": self.c15m}.get(interval, self.c5m)
        return builder.get(symbol, n)

    def is_connected(self) -> bool:
        return self._connected

    def subscribe_strikes(self, keys: List[str]):
        """Subscribe to additional option strike keys via WS."""
        if self._ws and self._connected:
            self._ws_subscribe(keys)

    # ── V3 WebSocket ──────────────────────────────────────────────

    def _get_ws_auth_url(self) -> str:
        """
        Fetch one-time authorized WS URL.
        Docs: GET /v3/feed/market-data-feed/authorize
        Returns wss:// URL with single-use token.
        """
        try:
            r = requests.get(
                f"{config.UPSTOX_BASE_URL_V3}/feed/market-data-feed/authorize",
                headers=self._headers,
                timeout=10
            )
            if r.status_code == 200:
                url = r.json().get("data", {}).get("authorized_redirect_uri", "")
                if url:
                    log.info("WS auth URL obtained ✓")
                    return url
            log.error(f"WS auth failed: {r.status_code} | {r.text[:200]}")
        except Exception as e:
            log.error(f"WS auth error: {e}")
        return ""

    def _ws_loop(self):
        """Connect to Upstox V3 WebSocket with exponential backoff."""
        retry = 0
        while not self._stop.is_set():
            try:
                ws_url = self._get_ws_auth_url()
                if not ws_url:
                    wait = min(30 * (2 ** min(retry, 4)), 300)
                    log.warning(f"WS auth failed. Retry in {wait}s (REST fallback active)")
                    time.sleep(wait)
                    retry = min(retry + 1, 5)
                    continue

                self._ws = websocket.WebSocketApp(
                    ws_url,
                    on_open=self._on_ws_open,
                    on_message=self._on_ws_message,
                    on_error=self._on_ws_error,
                    on_close=self._on_ws_close,
                )
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
                retry = 0

            except Exception as e:
                log.error(f"WS loop error: {e}")

            self._connected = False
            if not self._stop.is_set():
                wait = min(30 * (2 ** min(retry, 4)), 300)
                log.warning(f"WS disconnected. Reconnect in {wait}s (REST covers data)")
                time.sleep(wait)
                retry = min(retry + 1, 5)

    def _on_ws_open(self, ws):
        self._connected = True
        log.info("WebSocket V3 connected ✓")
        # Subscribe to all watch keys
        self._ws_subscribe(config.WATCH_KEYS)

    def _ws_subscribe(self, keys: List[str]):
        """
        V3 CRITICAL: subscription message must be sent as BINARY bytes.
        Docs: "The WebSocket request message should be sent in binary format"
        """
        msg = {
            "guid": "warroom_sub",
            "method": "sub",
            "data": {
                "mode": "full",
                "instrumentKeys": keys
            }
        }
        if self._ws:
            # Send as binary bytes — required by V3
            self._ws.send(json.dumps(msg).encode("utf-8"), websocket.ABNF.OPCODE_BINARY)
            log.info(f"Subscribed (binary) to {len(keys)} instruments.")

    def _on_ws_message(self, ws, raw):
        """
        V3 delivers protobuf binary → SDK decodes to JSON dict OR
        can be JSON text for market_info first message.
        V3 structure: feeds[key].fullFeed.marketFF.ltpc.ltp (NOT ff.marketFF)
        """
        try:
            # Try to decode as protobuf using upstox SDK
            if isinstance(raw, bytes):
                decoded = False
                # Try official SDK protobuf decoder
                for proto_module in [
                    "upstox_client.feeder.proto.MarketDataFeedV3_pb2",
                    "upstox_client.feeder.proto.MarketDataFeed_pb2",
                ]:
                    try:
                        parts = proto_module.rsplit(".", 1)
                        mod = __import__(parts[0], fromlist=[parts[1]])
                        pb  = getattr(mod, parts[1])
                        feed_response = pb.FeedResponse()
                        feed_response.ParseFromString(raw)
                        for key, feed_data in feed_response.feeds.items():
                            self._process_proto_v3(key, feed_data)
                        decoded = True
                        break
                    except Exception:
                        continue

                if not decoded:
                    # Try JSON fallback (market_info is sent as JSON)
                    try:
                        msg = json.loads(raw.decode("utf-8"))
                        self._process_json_message(msg)
                    except Exception:
                        pass
            else:
                # Text message
                msg = json.loads(raw)
                self._process_json_message(msg)

        except Exception as e:
            log.debug(f"WS message error: {e}")

    def _process_json_message(self, msg: dict):
        """
        Process V3 JSON messages.
        V3 structure for live_feed:
        {
          "type": "live_feed",
          "feeds": {
            "NSE_INDEX|Nifty 50": {
              "fullFeed": {
                "indexFF": {          ← for INDEX instruments
                  "ltpc": {"ltp": 22700, "cp": 23114},
                  "marketOHLC": {"ohlc": [{"interval":"1d","open":...}]}
                }
              }
            }
          }
        }
        """
        msg_type = msg.get("type", "")

        if msg_type == "market_info":
            log.info(f"Market status received: {list(msg.get('marketInfo', {}).get('segmentStatus', {}).items())[:3]}")
            return

        if msg_type != "live_feed":
            return

        feeds = msg.get("feeds", {})
        for instrument_key, feed_data in feeds.items():
            try:
                raw_sym = instrument_key.split("|")[-1]
                symbol  = normalize_symbol(raw_sym) or raw_sym.replace(" ", "_")

                # V3 full mode: fullFeed.marketFF (FO) or fullFeed.indexFF (INDEX)
                full_feed = feed_data.get("fullFeed", {})
                mff = full_feed.get("marketFF") or full_feed.get("indexFF") or {}

                # Also handle direct ltpc (LTPC mode)
                if not mff:
                    ltpc_direct = feed_data.get("ltpc", {})
                    if ltpc_direct:
                        ltp = float(ltpc_direct.get("ltp", 0) or 0)
                        cp  = float(ltpc_direct.get("cp",  ltp) or ltp)
                        if ltp > 0:
                            self._store_tick(symbol, ltp, ltp, ltp, ltp, cp, 0, 0, ltp, ltp)
                    continue

                ltpc = mff.get("ltpc", {})
                ltp  = float(ltpc.get("ltp", 0) or 0)
                if ltp == 0:
                    continue

                cp = float(ltpc.get("cp", ltp) or ltp)  # prev close price

                # OHLC
                open_ = high = low = ltp
                ohlc_list = mff.get("marketOHLC", {}).get("ohlc", [])
                for ohlc in ohlc_list:
                    if ohlc.get("interval") == "1d":
                        open_ = float(ohlc.get("open",  ltp) or ltp)
                        high  = float(ohlc.get("high",  ltp) or ltp)
                        low   = float(ohlc.get("low",   ltp) or ltp)
                        break

                # Volume / OI
                volume = int(mff.get("vtt", 0) or 0)
                oi     = int(mff.get("oi",  0) or 0)

                # Bid/Ask from marketLevel
                bid = ask = ltp
                market_level = mff.get("marketLevel", {})
                quotes = market_level.get("bidAskQuote", [])
                if quotes:
                    bid = float(quotes[0].get("bidP", ltp) or ltp)
                    ask = float(quotes[0].get("askP", ltp) or ltp)

                self._store_tick(symbol, ltp, open_, high, low, cp, volume, oi, bid, ask)
                log.debug(f"WS V3 tick: {symbol}={ltp:.1f} cp={cp:.1f}")

            except Exception as e:
                log.debug(f"V3 feed process error {instrument_key}: {e}")

    def _process_proto_v3(self, key: str, feed_data):
        """Process protobuf decoded feed (SDK handles decoding, we read the object)."""
        try:
            raw_sym = key.split("|")[-1]
            symbol  = normalize_symbol(raw_sym) or raw_sym.replace(" ", "_")

            # V3 proto: fullFeed → marketFF or indexFF
            ltp = cp = open_ = high = low = 0.0
            volume = oi = 0

            try:
                ff = feed_data.fullFeed
                mff = ff.marketFF if ff.HasField("marketFF") else ff.indexFF
                ltp = float(mff.ltpc.ltp)
                cp  = float(mff.ltpc.cp) or ltp
                volume = int(mff.vtt or 0)
                oi     = int(mff.oi  or 0)
                for ohlc in mff.marketOHLC.ohlc:
                    if ohlc.interval == "1d":
                        open_ = float(ohlc.open) or ltp
                        high  = float(ohlc.high) or ltp
                        low   = float(ohlc.low)  or ltp
                        break
            except Exception:
                return

            if ltp == 0:
                return

            if open_ == 0: open_ = ltp
            if high  == 0: high  = ltp
            if low   == 0: low   = ltp

            self._store_tick(symbol, ltp, open_, high, low, cp, volume, oi, ltp, ltp)
            log.debug(f"WS proto V3: {symbol}={ltp:.1f}")

        except Exception as e:
            log.debug(f"Proto V3 process error {key}: {e}")

    def _store_tick(self, symbol: str, ltp: float, open_: float, high: float,
                    low: float, prev_close: float, volume: int, oi: int,
                    bid: float, ask: float):
        """Store tick, preserve intraday high/low, fire callbacks."""
        now = datetime.now()
        tick = Tick(
            symbol=symbol, ltp=ltp,
            open=open_, high=high, low=low,
            prev_close=prev_close,
            volume=volume, oi=oi,
            bid=bid, ask=ask, ts=now,
        )
        with self._lock:
            existing = self._ticks.get(symbol)
            if existing:
                # Preserve session high/low from intraday
                tick.high = max(existing.high, high) if existing.high > 0 else high
                tick.low  = min(existing.low,  low)  if existing.low  > 0 else low
                if existing.open > 0:
                    tick.open = existing.open
                if existing.prev_close > 0 and prev_close == ltp:
                    tick.prev_close = existing.prev_close  # keep real prev_close
            self._ticks[symbol] = tick
            if "VIX" in symbol.upper():
                self._vix = ltp

        self.c1m.push(symbol, tick)
        self.c5m.push(symbol, tick)
        self.c15m.push(symbol, tick)

        for cb in self._callbacks:
            try: cb(tick)
            except Exception as e: log.debug(f"Tick callback: {e}")

    def _on_ws_error(self, ws, error):
        log.error(f"WS error: {error}")
        self._connected = False

    def _on_ws_close(self, ws, *args):
        log.warning("WS closed")
        self._connected = False

    # ── REST LTP Fallback ─────────────────────────────────────────
    # Endpoint: GET /v2/market-quote/quotes
    # Docs response key: "NSE_INDEX:Nifty 50" (colon separator)
    # Fields: last_price, ohlc.{open,high,low,close}, volume, net_change
    # ohlc.close = PREVIOUS SESSION'S CLOSE (prev_close)

    def _rest_ltp_loop(self):
        """
        REST fallback: polls /v2/market-quote/quotes every 3s.
        Only updates ticks when WS data is stale (>5s).
        Provides full OHLC + correct prev_close for change% calculation.
        """
        # Build comma-separated instrument keys for the query
        all_keys = ",".join(self.INSTRUMENT_KEYS.values())
        _logged   = False

        while not self._stop.is_set():
            try:
                now = datetime.now()

                # Skip if WS is delivering fresh data for both indices
                ws_fresh = all(
                    sym in self._ticks and
                    (now - self._ticks[sym].ts).total_seconds() < 5
                    for sym in ["NIFTY", "BANKNIFTY"]
                )
                if ws_fresh:
                    time.sleep(3)
                    continue

                # Fetch full market quotes — includes OHLC and prev_close
                r = requests.get(
                    f"{config.UPSTOX_BASE_URL}/market-quote/quotes",
                    headers=self._headers,
                    params={"instrument_key": all_keys},
                    timeout=6
                )

                if r.status_code != 200:
                    log.debug(f"REST quote error {r.status_code}: {r.text[:100]}")
                    time.sleep(3)
                    continue

                data = r.json().get("data", {})
                if not data:
                    time.sleep(3)
                    continue

                # Log exact response structure once
                if not _logged:
                    _logged = True
                    log.info(f"REST response keys: {list(data.keys())}")
                    sample = next(iter(data.values()), {})
                    log.info(f"REST sample fields: {list(sample.keys())}")
                    log.info(f"REST sample ohlc: {sample.get('ohlc', {})}")

                for sym, ikey in self.INSTRUMENT_KEYS.items():
                    # V2 REST response uses colon: "NSE_INDEX:Nifty 50"
                    colon_key = ikey.replace("|", ":")
                    val = data.get(colon_key) or data.get(ikey) or {}
                    if not val:
                        continue

                    ltp = float(val.get("last_price") or 0)
                    if ltp == 0:
                        continue

                    ohlc = val.get("ohlc", {})
                    open_      = float(ohlc.get("open",  ltp) or ltp)
                    high_      = float(ohlc.get("high",  ltp) or ltp)
                    low_       = float(ohlc.get("low",   ltp) or ltp)
                    # prev_close: ohlc.close = prev session close for most instruments
                    # For indices, use net_change: prev_close = last_price - net_change
                    ohlc_close  = float(ohlc.get("close", 0) or 0)
                    net_change  = float(val.get("net_change", 0) or 0)
                    if net_change != 0:
                        prev_close = round(ltp - net_change, 2)
                    elif ohlc_close > 0 and abs(ohlc_close - ltp) > 0.01:
                        prev_close = ohlc_close
                    else:
                        prev_close = ltp  # fallback — change% will show 0
                    volume     = int(val.get("volume") or 0)

                    with self._lock:
                        existing = self._ticks.get(sym)
                        if existing is None or (now - existing.ts).total_seconds() > 4:
                            self._store_tick(sym, ltp, open_, high_, low_,
                                           prev_close, volume, 0, ltp, ltp)
                            log.debug(
                                f"REST → {sym} LTP={ltp:.1f} "
                                f"H={high_:.0f} L={low_:.0f} "
                                f"CP={prev_close:.1f} "
                                f"chg={change_pct(ltp, prev_close):+.2f}%"
                            )

            except Exception as e:
                log.debug(f"REST loop error: {e}")
            time.sleep(3)

    # ── Option Chain ──────────────────────────────────────────────

    def _chain_loop(self):
        while not self._stop.is_set():
            try:
                self._fetch_chain("NIFTY")
                time.sleep(3)
                self._fetch_chain("BANKNIFTY")
            except Exception as e:
                log.error(f"Chain poll error: {e}")
            time.sleep(27)

    def _get_nearest_expiry(self, instrument_key: str) -> tuple:
        """
        Calls Option Contracts API to discover available expiries.
        Returns (expiry_iso, expiry_str) of the nearest upcoming expiry.
        """
        try:
            r = requests.get(
                f"{config.UPSTOX_BASE_URL}/option/contract",
                headers=self._headers,
                params={"instrument_key": instrument_key},
                timeout=8
            )
            if r.status_code != 200:
                return None, None
            data = r.json().get("data", [])
            if not data:
                return None, None
            # Get unique expiry dates, sorted ascending
            today = datetime.now().date()
            expiries = sorted(set(
                c["expiry"] for c in data
                if c.get("expiry") and datetime.strptime(c["expiry"], "%Y-%m-%d").date() >= today
            ))
            if not expiries:
                return None, None
            nearest = expiries[0]
            expiry_str = datetime.strptime(nearest, "%Y-%m-%d").strftime("%d %b")
            log.debug(f"Option contracts: nearest expiry={nearest} (from {len(expiries)} available)")
            return nearest, expiry_str
        except Exception as e:
            log.debug(f"Option contracts fetch error: {e}")
            return None, None

    def _fetch_chain(self, symbol: str):
        try:
            key = config.NIFTY_INDEX_KEY if symbol == "NIFTY" else config.BANKNIFTY_INDEX_KEY

            # Step 1: Discover the actual nearest expiry via Option Contracts API
            # This is the correct approach — don't hardcode/guess expiry dates
            expiry_iso, expiry_str = self._get_nearest_expiry(key)
            if not expiry_iso:
                # Fallback to calculated expiry
                expiry_iso, expiry_str = next_expiry(symbol)
                log.debug(f"Chain {symbol}: using calculated expiry {expiry_iso}")
            else:
                log.debug(f"Chain {symbol}: using discovered expiry {expiry_iso}")

            log.debug(f"Fetching chain {symbol} expiry={expiry_iso} key={key}")
            r = requests.get(
                f"{config.UPSTOX_BASE_URL}/option/chain",
                headers=self._headers,
                params={"instrument_key": key, "expiry_date": expiry_iso},
                timeout=10
            )
            if r.status_code != 200:
                log.warning(f"Chain {symbol} HTTP {r.status_code}: {r.text[:200]}")
                return

            rows = []
            total_ce_oi = total_pe_oi = 0

            for row in r.json().get("data", []):
                # market_data fields per official docs:
                # ltp, volume, oi, close_price, bid_price, ask_price, prev_oi
                # IV is under option_greeks.iv NOT market_data
                ce_data    = row.get("call_options", {})
                pe_data    = row.get("put_options",  {})
                ce         = ce_data.get("market_data", {})
                pe         = pe_data.get("market_data", {})
                ce_greeks  = ce_data.get("option_greeks", {})
                pe_greeks  = pe_data.get("option_greeks", {})
                ce_oi = int(ce.get("oi") or 0)
                pe_oi = int(pe.get("oi") or 0)
                total_ce_oi += ce_oi
                total_pe_oi += pe_oi
                # oi_day_change = oi - prev_oi (prev_oi is the correct field)
                ce_prev_oi = int(ce.get("prev_oi") or ce_oi)
                pe_prev_oi = int(pe.get("prev_oi") or pe_oi)
                rows.append(OptionRow(
                    strike    = float(row.get("strike_price", 0)),
                    expiry    = expiry_str,
                    ce_ltp    = float(ce.get("ltp")         or 0),
                    ce_oi     = ce_oi,
                    ce_oi_chg = ce_oi - ce_prev_oi,
                    ce_iv     = float(ce_greeks.get("iv")   or 0),  # IV in option_greeks
                    ce_vol    = int(ce.get("volume")         or 0),
                    pe_ltp    = float(pe.get("ltp")         or 0),
                    pe_oi     = pe_oi,
                    pe_oi_chg = pe_oi - pe_prev_oi,
                    pe_iv     = float(pe_greeks.get("iv")   or 0),  # IV in option_greeks
                    pe_vol    = int(pe.get("volume")         or 0),
                ))

            if not rows:
                log.warning(f"Chain {symbol}: 0 rows returned for expiry {expiry_iso}")
                return

            pcr = round(total_pe_oi / total_ce_oi, 3) if total_ce_oi > 0 else 1.0
            mp  = self._max_pain_calc(rows)

            with self._lock:
                self._chain[symbol]    = rows
                self._pcr[symbol]      = pcr
                self._max_pain[symbol] = mp

            log.debug(f"Chain {symbol} | Rows={len(rows)} | PCR={pcr} | MaxPain={mp:.0f}")

        except Exception as e:
            log.error(f"Chain {symbol} error: {e}")

    @staticmethod
    def _max_pain_calc(chain: List[OptionRow]) -> float:
        if not chain:
            return 0.0
        best = float("inf")
        pain_strike = 0.0
        for test in chain:
            p = sum(
                max(0, test.strike - r.strike) * r.pe_oi +
                max(0, r.strike - test.strike) * r.ce_oi
                for r in chain
            )
            if p < best:
                best = p
                pain_strike = test.strike
        return pain_strike
