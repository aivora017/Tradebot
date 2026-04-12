# ================================================================
#  check_funds.py — Debug tool: prints raw Upstox funds-and-margin
#  response so you can see exactly what the API returns.
#
#  Usage: python check_funds.py
#  Run AFTER morning_prep.py has refreshed the token.
# ================================================================

import os, json, requests
from dotenv import load_dotenv

load_dotenv()

TOKEN   = os.getenv("UPSTOX_ACCESS_TOKEN", "").strip("'\"")
BASE    = "https://api.upstox.com/v2"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept":        "application/json",
}

G  = "\033[92m"
Y  = "\033[93m"
R  = "\033[91m"
B  = "\033[94m"
W  = "\033[0m"
BD = "\033[1m"

def check():
    if not TOKEN:
        print(f"{R}❌ No UPSTOX_ACCESS_TOKEN in .env — run morning_prep.py first{W}")
        return

    print(f"\n{BD}=== UPSTOX FUNDS AND MARGIN DEBUG ==={W}\n")
    print(f"Token prefix: {TOKEN[:20]}…")
    print()

    # ── Try 1: No segment param ───────────────────────────────────
    print(f"{B}[Try 1] GET /v2/user/funds-and-margin (no segment){W}")
    try:
        r = requests.get(f"{BASE}/user/get-funds-and-margin", headers=HEADERS, timeout=10)
        print(f"  HTTP: {r.status_code}")
        resp = r.json()
        print(f"  Status: {resp.get('status')}")
        data = resp.get("data", {})
        print(f"  Top-level keys in data: {list(data.keys())}")
        print()

        if "equity" in data:
            eq = data["equity"]
            print(f"  {G}data.equity keys: {list(eq.keys())}{W}")
            print(f"  {G}available_margin : ₹{eq.get('available_margin', 'N/A'):>12}{W}")
            print(f"  {B}used_margin      : ₹{eq.get('used_margin', 'N/A'):>12}{W}")
            print(f"  {B}payin_amount     : ₹{eq.get('payin_amount', 'N/A'):>12}{W}")
            print(f"  {B}notional_cash    : ₹{eq.get('notional_cash', 'N/A'):>12}{W}")
            print(f"  {B}span_margin      : ₹{eq.get('span_margin', 'N/A'):>12}{W}")
            print(f"  {B}exposure_margin  : ₹{eq.get('exposure_margin', 'N/A'):>12}{W}")
        else:
            print(f"  {Y}No 'equity' key — flat structure:{W}")
            print(f"  {json.dumps(data, indent=4)[:600]}")

        print()
        print(f"  {BD}Full raw response:{W}")
        print(f"  {json.dumps(resp, indent=2)[:1000]}")

    except Exception as e:
        print(f"  {R}Error: {e}{W}")

    print()

    # ── Try 2: With segment=SEC ───────────────────────────────────
    print(f"{B}[Try 2] GET /v2/user/funds-and-margin?segment=SEC{W}")
    try:
        r2 = requests.get(
            f"{BASE}/user/get-funds-and-margin",
            headers=HEADERS,
            params={"segment": "SEC"},
            timeout=10,
        )
        print(f"  HTTP: {r2.status_code}")
        resp2 = r2.json()
        print(f"  Status: {resp2.get('status')}")
        data2 = resp2.get("data", {})
        print(f"  Top-level keys in data: {list(data2.keys()) if isinstance(data2, dict) else type(data2)}")
        print(f"  {json.dumps(data2, indent=2)[:600]}")
    except Exception as e:
        print(f"  {R}Error: {e}{W}")

    print()
    print(f"{BD}=== END DEBUG ==={W}\n")


if __name__ == "__main__":
    check()
