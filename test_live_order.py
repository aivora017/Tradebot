# ================================================================
#  test_live_order.py — Full live order pipeline test
#  Tests: chain fetch → instrument_key → place → status → cancel
#
#  Usage: python test_live_order.py
#  Run from the trading/ directory AFTER morning token refresh.
#  Places 1 lot LIMIT at ₹0.05 (won't fill) — cancels immediately.
# ================================================================

import requests, json, time, sys, os
from dotenv import load_dotenv

load_dotenv(override=True)

# Read token directly from .env (immune to stale Windows env vars)
TOKEN = ""
try:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    for line in open(env_path, encoding="utf-8", errors="ignore"):
        line = line.strip().rstrip("\r\n")
        if line.startswith("UPSTOX_ACCESS_TOKEN="):
            TOKEN = line.split("=", 1)[1].strip().strip("'\"")
            break
except Exception as e:
    print(f"❌ Could not read .env: {e}")
    sys.exit(1)

if not TOKEN:
    print("❌ UPSTOX_ACCESS_TOKEN missing. Run get_token.py first.")
    sys.exit(1)

HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept":        "application/json",
    "Content-Type":  "application/json",
}
BASE = "https://api.upstox.com/v2"

G = "\033[92m"; R = "\033[91m"; Y = "\033[93m"; B = "\033[94m"; W = "\033[0m"; BD = "\033[1m"

def p(msg, color=W): print(f"{color}{msg}{W}")
def ok(msg):  p(f"  ✅ {msg}", G)
def err(msg): p(f"  ❌ {msg}", R)
def info(msg): p(f"  ℹ  {msg}", B)

p(f"\n{'='*60}", BD)
p("  WAR ROOM — LIVE ORDER PIPELINE TEST", BD)
p(f"  Token: {TOKEN[:25]}…", W)
p(f"{'='*60}\n", BD)

# ── STEP 1: Fetch Nifty LTP ───────────────────────────────────────
p("[1/5] Fetching Nifty LTP…", Y)
r = requests.get(f"{BASE}/market-quote/ltp", headers=HEADERS,
    params={"instrument_key": "NSE_INDEX|Nifty 50"}, timeout=8)
if r.status_code != 200:
    err(f"LTP fetch failed: HTTP {r.status_code} — {r.text[:200]}")
    sys.exit(1)

data = r.json().get("data", {})
ltp = 0.0
for v in data.values():
    ltp = float(v.get("last_price", 0))

if ltp <= 0:
    err("LTP = 0. Market may be closed or token expired.")
    sys.exit(1)

ok(f"Nifty LTP = ₹{ltp:,.1f}")

# ── STEP 2: Fetch option chain and pick deep OTM CE ───────────────
p("\n[2/5] Fetching option chain for 2026-04-13…", Y)
r2 = requests.get(f"{BASE}/option/chain", headers=HEADERS,
    params={"instrument_key": "NSE_INDEX|Nifty 50", "expiry_date": "2026-04-13"}, timeout=12)
chain_data = r2.json().get("data", [])

if not chain_data:
    err("Option chain empty. Check token / expiry date.")
    sys.exit(1)

ok(f"Chain loaded: {len(chain_data)} rows")

# Pick deepest cheap OTM CE (strike 400-700 above LTP, has a real instrument_key)
best = None
for row in sorted(chain_data, key=lambda x: float(x.get("strike_price", 0))):
    strike = float(row.get("strike_price", 0))
    if not (ltp + 400 <= strike <= ltp + 800):
        continue
    ce    = row.get("call_options", {})
    ikey  = ce.get("instrument_key", "")
    ltp_c = float((ce.get("market_data") or {}).get("ltp", 0) or 0)
    if ikey and ltp_c > 0:
        best = {"strike": int(strike), "ikey": ikey, "ltp": ltp_c}
        break

if not best:
    err("Could not find a valid deep OTM option with instrument_key.")
    sys.exit(1)

ok(f"Test option: NIFTY {best['strike']} CE | ikey={best['ikey']} | LTP=₹{best['ltp']}")
info(f"Will place LIMIT BUY at ₹0.05 (market is ₹{best['ltp']:.1f}) — will NOT fill")

# ── STEP 3: Place limit order ─────────────────────────────────────
p("\n[3/5] Placing LIMIT BUY order (₹0.05)…", Y)
payload = {
    "quantity":           65,          # 1 lot Nifty (confirmed lot_size=65 via Upstox instruments API)
    "product":            "I",         # Intraday MIS
    "validity":           "DAY",
    "price":              0.05,        # Impossible fill price
    "tag":                "warroom_test",
    "instrument_token":   best["ikey"],
    "order_type":         "LIMIT",
    "transaction_type":   "BUY",
    "disclosed_quantity": 0,
    "trigger_price":      0,
    "is_amo":             False,
}
print(f"  Payload: {json.dumps(payload)}")
r3 = requests.post(f"{BASE}/order/place", headers=HEADERS, json=payload, timeout=8)
resp3 = r3.json()
print(f"  HTTP: {r3.status_code}")
print(f"  Response: {json.dumps(resp3, indent=4)}")

order_id = resp3.get("data", {}).get("order_id", "")
if not order_id:
    errors = resp3.get("errors", resp3.get("message", "unknown"))
    err(f"Order placement FAILED: {errors}")
    p("\n⚠️  Diagnose the error above and share with Claude.", Y)
    sys.exit(1)

ok(f"ORDER PLACED ✅ — order_id = {order_id}")

# ── STEP 4: Check order status ────────────────────────────────────
p("\n[4/5] Checking order status…", Y)
time.sleep(1.5)
r4 = requests.get(f"{BASE}/order/details", headers=HEADERS,
    params={"order_id": order_id}, timeout=8)
status_data = r4.json().get("data", {})
status = status_data.get("status", "unknown")
filled = status_data.get("average_price", 0)
ok(f"Status = {status} | filled_at = ₹{filled}")

if status == "complete":
    p(f"\n  ⚠️  Order FILLED at ₹{filled} — this is unexpected at ₹0.05!", R)
    p("  Placing SELL to close immediately…", Y)
    rc = requests.post(f"{BASE}/order/place", headers=HEADERS, json={
        **payload, "transaction_type": "SELL",
        "price": best["ltp"] - 5,  # market-ish sell
    }, timeout=8)
    p(f"  Close response: {rc.json()}", Y)

# ── STEP 5: Cancel order ──────────────────────────────────────────
p("\n[5/5] Cancelling order…", Y)
r5 = requests.delete(f"{BASE}/order/cancel", headers=HEADERS,
    params={"order_id": order_id}, timeout=8)
cancel_resp = r5.json()
print(f"  HTTP: {r5.status_code}")
print(f"  Response: {json.dumps(cancel_resp, indent=4)}")

cancel_ok = cancel_resp.get("status") == "success" or r5.status_code == 200
if cancel_ok:
    ok("Order cancelled")
else:
    err(f"Cancel may have failed: {cancel_resp}")

# ── Final Summary ─────────────────────────────────────────────────
p(f"\n{'='*60}", BD)
p("  TEST RESULTS", BD)
p(f"{'='*60}", BD)
ok(f"Chain fetch:        {len(chain_data)} rows, instrument_key resolved")
ok(f"Order placement:    order_id={order_id}")
ok(f"Order status:       {status}")
print(f"  Cancel:             {'✅ OK' if cancel_ok else '⚠️  CHECK MANUALLY'}")
p(f"\n{'='*60}", BD)

if order_id and cancel_ok:
    p("  ✅ FULL PIPELINE VERIFIED — bot can place + cancel live orders", G)
else:
    p("  ⚠️  PARTIAL — check errors above", Y)
p(f"{'='*60}\n", BD)
