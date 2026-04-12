# ================================================================
#  order_manager.py — Upstox Order Execution
#  LIVE: places real orders via Upstox API v2
#  PAPER: simulates fills at mid-price
#  SIGNAL: no orders placed, signals only
#
#  BUG FIXES (2026-04-08):
#  - product: "D" → "I"  (Intraday MIS for F&O, was Delivery/NRML)
#  - instrument_key: now taken from chain data, never constructed manually
#  - buy_option() accepts pre-resolved instrument_key from data_feed
#  - _get_fill_price: 8 polls (was 5), "traded" removed (not a valid status)
#  - _get_headers(): reads fresh token each call (no stale auth)
#  - get_live_positions(): syncs open F&O positions from Upstox account
#  - get_order_book(): fetches today's order book for reconciliation
# ================================================================

import time
import requests
from datetime import datetime
from typing import Optional, List
from dataclasses import dataclass

import config
from logger_setup import get_logger
from utils import lot_size

log = get_logger("Orders")


@dataclass
class OrderResult:
    success:     bool
    order_id:    str
    symbol:      str
    strike:      int
    option_type: str
    qty:         int
    price:       float
    mode:        str
    error:       str = ""
    ts:          datetime = None

    def __post_init__(self):
        if self.ts is None:
            self.ts = datetime.now()


class OrderManager:
    BASE = config.UPSTOX_BASE_URL

    def __init__(self):
        self.mode = config.EXECUTION_MODE
        self._paper_order_count = 0
        log.info(f"OrderManager initialized in {self.mode} mode.")

    def _get_headers(self) -> dict:
        """Always read fresh token — never cache. Token refreshes every morning."""
        return {
            "Authorization": f"Bearer {config.UPSTOX_ACCESS_TOKEN}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }

    def buy_option(self, symbol: str, strike: int, option_type: str,
                   expiry_str: str, lots: int,
                   order_type: str = "MARKET",
                   limit_price: float = 0.0,
                   instrument_key: str = "") -> OrderResult:
        """
        Place a BUY order.

        Args:
            instrument_key: REQUIRED for LIVE mode — must come from data_feed.
                            get_instrument_key_for_strike(). Never construct manually.
                            If empty in LIVE mode, order will fail gracefully.
        """
        log.info(f"BUY ORDER: {symbol} {strike}{option_type} {lots}L [{self.mode}]")

        if self.mode == "SIGNAL":
            return OrderResult(
                success=True, order_id="SIGNAL_MODE",
                symbol=symbol, strike=strike, option_type=option_type,
                qty=lots * lot_size(symbol), price=limit_price or 0.0,
                mode="SIGNAL"
            )
        if self.mode == "PAPER":
            return self._paper_buy(symbol, strike, option_type, lots, limit_price)
        if self.mode == "LIVE":
            return self._live_buy(symbol, strike, option_type,
                                  lots, order_type, limit_price, instrument_key)

        return OrderResult(success=False, order_id="", symbol=symbol,
                           strike=strike, option_type=option_type,
                           qty=0, price=0, mode=self.mode,
                           error="Unknown execution mode")

    def close_position(self, instrument_key: str, qty: int,
                       order_type: str = "MARKET",
                       limit_price: float = 0.0) -> OrderResult:
        log.info(f"CLOSE ORDER: {instrument_key} qty={qty} [{self.mode}]")

        if self.mode in ("SIGNAL", "PAPER"):
            return OrderResult(
                success=True, order_id=f"CLOSE_{self.mode}",
                symbol=instrument_key, strike=0, option_type="",
                qty=qty, price=limit_price or 0.0, mode=self.mode
            )
        return self._live_close(instrument_key, qty, order_type, limit_price)

    def get_order_status(self, order_id: str) -> dict:
        if self.mode != "LIVE":
            return {"status": "complete", "average_price": 0}
        try:
            r = requests.get(
                f"{self.BASE}/order/details",
                headers=self._get_headers(),
                params={"order_id": order_id},
                timeout=5
            )
            return r.json().get("data", {})
        except Exception as e:
            log.error(f"Order status check failed: {e}")
            return {}

    def get_option_ltp(self, instrument_key: str) -> float:
        """Fetch real-time LTP for an option via REST (fallback when chain data is stale)."""
        try:
            r = requests.get(
                f"{self.BASE}/market-quote/ltp",
                headers=self._get_headers(),
                params={"instrument_key": instrument_key},
                timeout=5
            )
            data = r.json().get("data", {})
            for v in data.values():
                return float(v.get("last_price", 0))
        except Exception as e:
            log.debug(f"LTP fetch error: {e}")
        return 0.0

    def get_live_positions(self) -> List[dict]:
        """
        Fetch current open F&O positions from Upstox account.
        Used for startup reconciliation in LIVE mode.

        Returns list of position dicts with keys:
            trading_symbol, quantity.day, average_price, last_price,
            pnl, unrealised_gain, instrument_token, product
        """
        if self.mode != "LIVE":
            return []
        try:
            r = requests.get(
                f"{self.BASE}/portfolio/short-term-positions",
                headers=self._get_headers(),
                timeout=8
            )
            if r.status_code == 200 and r.json().get("status") == "success":
                positions = r.json().get("data", [])
                # Filter: only F&O (NSE_FO) with non-zero net quantity
                fo_positions = [
                    p for p in positions
                    if "FO" in p.get("exchange", "") or "NFO" in p.get("exchange", "")
                    and (p.get("quantity", {}).get("day", 0) or
                         p.get("quantity", 0)) != 0
                ]
                log.info(f"Live positions fetched: {len(fo_positions)} open F&O positions")
                return fo_positions
            else:
                log.warning(f"Positions fetch: {r.status_code} | {r.text[:200]}")
                return []
        except Exception as e:
            log.error(f"Live positions fetch error: {e}")
            return []

    def get_order_book(self) -> List[dict]:
        """
        Fetch today's complete order book from Upstox.
        Used for trade reconciliation.
        Endpoint: GET /v2/order/retrieve-all
        """
        if self.mode != "LIVE":
            return []
        try:
            r = requests.get(
                f"{self.BASE}/order/retrieve-all",
                headers=self._get_headers(),
                timeout=8
            )
            if r.status_code == 200 and r.json().get("status") == "success":
                orders = r.json().get("data", [])
                log.info(f"Order book fetched: {len(orders)} orders today")
                return orders
            else:
                log.warning(f"Order book fetch: {r.status_code} | {r.text[:200]}")
                return []
        except Exception as e:
            log.error(f"Order book fetch error: {e}")
            return []

    def _live_buy(self, symbol, strike, option_type,
                  lots, order_type, limit_price, instrument_key) -> OrderResult:
        """
        Place live BUY order on Upstox.
        FIX: product="I" (Intraday MIS) — correct for F&O intraday.
        FIX: instrument_token = instrument_key from chain (not constructed).
        """
        try:
            ls  = lot_size(symbol)
            qty = lots * ls

            # CRITICAL: instrument_key must come from chain data.
            if not instrument_key:
                err = (f"instrument_key is empty for {symbol} {strike} {option_type}. "
                       f"Chain data may not be loaded yet. Trade aborted.")
                log.error(f"❌ {err}")
                return OrderResult(
                    success=False, order_id="", symbol=symbol,
                    strike=strike, option_type=option_type,
                    qty=0, price=0, mode="LIVE", error=err
                )

            payload = {
                "quantity":           qty,
                "product":            "I",       # FIX: Intraday MIS (was "D" = Delivery/NRML)
                "validity":           "DAY",
                "price":              limit_price if order_type == "LIMIT" else 0,
                "tag":                "warroom_bot",
                "instrument_token":   instrument_key,  # FIX: from chain, not constructed
                "order_type":         order_type,
                "transaction_type":   "BUY",
                "disclosed_quantity": 0,
                "trigger_price":      0,
                "is_amo":             False,
            }

            log.info(f"Placing LIVE BUY: {instrument_key} | qty={qty} | "
                     f"type={order_type} | price={limit_price}")

            r = requests.post(
                f"{self.BASE}/order/place",
                headers=self._get_headers(),
                json=payload,
                timeout=8
            )
            resp = r.json()

            if r.status_code == 200 and resp.get("status") == "success":
                order_id = resp.get("data", {}).get("order_id", "")
                log.info(f"✅ LIVE ORDER PLACED: {order_id} | "
                         f"{symbol} {strike}{option_type} {lots}L")
                fill = self._get_fill_price(order_id)
                return OrderResult(
                    success=True, order_id=order_id,
                    symbol=symbol, strike=strike, option_type=option_type,
                    qty=qty, price=fill, mode="LIVE"
                )
            else:
                err = resp.get("errors", resp.get("message", "Unknown error"))
                log.error(f"❌ LIVE ORDER FAILED: {err} | "
                          f"payload instrument_token={instrument_key}")
                return OrderResult(
                    success=False, order_id="", symbol=symbol,
                    strike=strike, option_type=option_type,
                    qty=0, price=0, mode="LIVE", error=str(err)
                )
        except Exception as e:
            log.error(f"Live order exception: {e}")
            return OrderResult(
                success=False, order_id="", symbol=symbol,
                strike=strike, option_type=option_type,
                qty=0, price=0, mode="LIVE", error=str(e)
            )

    def _live_close(self, instrument_key, qty, order_type, limit_price) -> OrderResult:
        """
        Place live SELL order to close a position.
        FIX: product="I" (Intraday MIS — matches the buy product type).
        """
        try:
            payload = {
                "quantity":           qty,
                "product":            "I",      # FIX: must match buy product (was "D")
                "validity":           "DAY",
                "price":              limit_price if order_type == "LIMIT" else 0,
                "tag":                "warroom_bot",
                "instrument_token":   instrument_key,
                "order_type":         order_type,
                "transaction_type":   "SELL",
                "disclosed_quantity": 0,
                "trigger_price":      0,
                "is_amo":             False,
            }
            log.info(f"Placing LIVE SELL: {instrument_key} | qty={qty}")
            r    = requests.post(f"{self.BASE}/order/place",
                                 headers=self._get_headers(), json=payload, timeout=8)
            resp = r.json()
            if r.status_code == 200 and resp.get("status") == "success":
                oid = resp.get("data", {}).get("order_id", "")
                log.info(f"✅ CLOSE ORDER PLACED: {oid}")
                fill = self._get_fill_price(oid)
                return OrderResult(success=True, order_id=oid,
                                   symbol=instrument_key, strike=0, option_type="",
                                   qty=qty, price=fill, mode="LIVE")
            else:
                err = resp.get("errors", resp.get("message", "Unknown"))
                log.error(f"❌ CLOSE ORDER FAILED: {err}")
                return OrderResult(success=False, order_id="", symbol=instrument_key,
                                   strike=0, option_type="", qty=0, price=0,
                                   mode="LIVE", error=str(err))
        except Exception as e:
            log.error(f"Close order exception: {e}")
            return OrderResult(success=False, order_id="", symbol=instrument_key,
                               strike=0, option_type="", qty=0, price=0,
                               mode="LIVE", error=str(e))

    def _paper_buy(self, symbol, strike, option_type, lots, limit_price) -> OrderResult:
        self._paper_order_count += 1
        fake_price = limit_price if limit_price > 0 else 200.0
        oid = f"PAPER_{self._paper_order_count:04d}"
        log.info(f"📄 PAPER BUY: {oid} | {symbol} {strike}{option_type} @ ₹{fake_price}")
        return OrderResult(
            success=True, order_id=oid, symbol=symbol,
            strike=strike, option_type=option_type,
            qty=lots * lot_size(symbol), price=fake_price, mode="PAPER"
        )

    def _get_fill_price(self, order_id: str) -> float:
        """
        Poll order status until filled.
        FIX: 8 polls (was 5), 0.3s initial wait, only "complete" is valid fill status.
        "traded" is NOT a valid Upstox order status — removed.
        """
        try:
            time.sleep(0.3)  # brief wait before first poll
            for _ in range(8):
                status = self.get_order_status(order_id)
                if status.get("status") == "complete":
                    price = float(status.get("average_price", 0))
                    log.info(f"Order {order_id} filled @ ₹{price:.2f}")
                    return price
                if status.get("status") in ("rejected", "cancelled"):
                    log.error(f"Order {order_id} {status['status']}: "
                              f"{status.get('status_message', '')}")
                    return 0.0
                time.sleep(0.5)
        except Exception:
            pass
        log.warning(f"Fill price not confirmed for {order_id} after 4s polling")
        return 0.0
