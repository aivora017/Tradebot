# ================================================================
#  order_manager.py — Upstox Order Execution
#  LIVE: places real orders via Upstox API
#  PAPER: simulates fills at mid-price
#  SIGNAL: no orders placed, signals only
# ================================================================

import time
import requests
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

import config
from logger_setup import get_logger
from utils import next_expiry, lot_size

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
        self.mode    = config.EXECUTION_MODE
        self._headers = {
            "Authorization": f"Bearer {config.UPSTOX_ACCESS_TOKEN}",
            "Content-Type":  "application/json",
            "Accept":        "application/json",
        }
        self._paper_order_count = 0
        log.info(f"OrderManager initialized in {self.mode} mode.")

    def buy_option(self, symbol: str, strike: int, option_type: str,
                   expiry_str: str, lots: int,
                   order_type: str = "MARKET",
                   limit_price: float = 0.0) -> OrderResult:
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
                                  expiry_str, lots, order_type, limit_price)

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
            return {"status": "complete", "price": 0}
        try:
            r = requests.get(
                f"{self.BASE}/order/details",
                headers=self._headers,
                params={"order_id": order_id},
                timeout=5
            )
            return r.json().get("data", {})
        except Exception as e:
            log.error(f"Order status check failed: {e}")
            return {}

    def get_option_ltp(self, instrument_key: str) -> float:
        try:
            r = requests.get(
                f"{self.BASE}/market-quote/ltp",
                headers=self._headers,
                params={"instrument_key": instrument_key},
                timeout=5
            )
            data = r.json().get("data", {})
            for v in data.values():
                return float(v.get("last_price", 0))
        except Exception as e:
            log.debug(f"LTP fetch error: {e}")
        return 0.0

    def build_instrument_key(self, symbol: str, strike: int,
                              option_type: str, expiry_str: str) -> str:
        try:
            dt  = datetime.strptime(expiry_str, "%d %b")
            exp = dt.replace(year=datetime.now().year).strftime("%d%b%y").upper()
            sym = "BANKNIFTY" if "BANK" in symbol else "NIFTY"
            return f"NSE_FO|{sym}{exp}{int(strike)}{option_type}"
        except Exception:
            return ""

    def _live_buy(self, symbol, strike, option_type, expiry_str,
                  lots, order_type, limit_price) -> OrderResult:
        try:
            ls    = lot_size(symbol)
            qty   = lots * ls
            instr = self.build_instrument_key(symbol, strike, option_type, expiry_str)

            payload = {
                "quantity":         qty,
                "product":          "D",
                "validity":         "DAY",
                "price":            limit_price if order_type == "LIMIT" else 0,
                "tag":              "warroom_bot",
                "instrument_token": instr,
                "order_type":       order_type,
                "transaction_type": "BUY",
                "disclosed_quantity": 0,
                "trigger_price":    0,
                "is_amo":           False,
            }

            r = requests.post(
                f"{self.BASE}/order/place",
                headers=self._headers,
                json=payload,
                timeout=8
            )
            resp = r.json()

            if r.status_code == 200 and resp.get("status") == "success":
                order_id = resp.get("data", {}).get("order_id", "")
                log.info(f"✅ LIVE ORDER PLACED: {order_id} | {symbol} {strike}{option_type} {lots}L")
                time.sleep(1)
                fill = self._get_fill_price(order_id)
                return OrderResult(
                    success=True, order_id=order_id,
                    symbol=symbol, strike=strike, option_type=option_type,
                    qty=qty, price=fill, mode="LIVE"
                )
            else:
                err = resp.get("errors", resp.get("message", "Unknown error"))
                log.error(f"❌ LIVE ORDER FAILED: {err}")
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
        try:
            payload = {
                "quantity":         qty,
                "product":          "D",
                "validity":         "DAY",
                "price":            limit_price if order_type == "LIMIT" else 0,
                "tag":              "warroom_bot",
                "instrument_token": instrument_key,
                "order_type":       order_type,
                "transaction_type": "SELL",
                "disclosed_quantity": 0,
                "trigger_price":    0,
                "is_amo":           False,
            }
            r    = requests.post(f"{self.BASE}/order/place",
                                 headers=self._headers, json=payload, timeout=8)
            resp = r.json()
            if r.status_code == 200 and resp.get("status") == "success":
                oid = resp.get("data", {}).get("order_id", "")
                log.info(f"✅ CLOSE ORDER PLACED: {oid}")
                time.sleep(1)
                fill = self._get_fill_price(oid)
                return OrderResult(success=True, order_id=oid,
                                   symbol=instrument_key, strike=0, option_type="",
                                   qty=qty, price=fill, mode="LIVE")
            else:
                err = resp.get("errors", "Unknown")
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
        try:
            for _ in range(5):
                status = self.get_order_status(order_id)
                if status.get("status") in ("complete", "traded"):
                    return float(status.get("average_price", 0))
                time.sleep(0.5)
        except Exception:
            pass
        return 0.0
