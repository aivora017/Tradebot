# ================================================================
#  get_token.py — Run EVERY MORNING before 9 AM to refresh Upstox token
#  Usage: python get_token.py
#
#  Flow:
#    1. Browser login → exchange code for token → save to .env
#    2. Fetch live Upstox funds → save TRADING_CAPITAL to .env
#    3. Run morning_setup (market levels, VIX, PCR, config.py patch)
# ================================================================
import os, webbrowser, requests
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv

load_dotenv(override=True)

KEY    = os.getenv("UPSTOX_API_KEY",    "")
SECRET = os.getenv("UPSTOX_API_SECRET", "")
REDIR  = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1")
ENV    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
BASE_URL = "https://api.upstox.com/v2"


def _write_env_key(env_path: str, key: str, value: str) -> None:
    """
    Update or append a key=value in the .env file safely.
    - Reads existing content, replaces the matching line, writes back atomically.
    - Strips Windows CRLF → writes Unix LF to avoid python-dotenv set_key CRLF bugs.
    - Never quotes the value.
    """
    try:
        try:
            with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except FileNotFoundError:
            content = ""

        # Normalize to LF
        content = content.replace("\r\n", "\n").replace("\r", "\n")
        lines = content.split("\n")

        found = False
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                new_lines.append(f"{key}={value}")
                found = True
            else:
                new_lines.append(line)

        if not found:
            # Remove trailing empty lines then append
            while new_lines and new_lines[-1].strip() == "":
                new_lines.pop()
            new_lines.append(f"{key}={value}")

        new_content = "\n".join(new_lines)
        if not new_content.endswith("\n"):
            new_content += "\n"

        # Write to temp then rename for atomicity
        tmp_path = env_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(new_content)
        os.replace(tmp_path, env_path)
    except Exception as e:
        print(f"   ⚠️  _write_env_key failed for {key}: {e}")


def _fetch_and_save_funds(token: str) -> float:
    """
    Fetch live available margin from Upstox and save to .env.
    Updates TRADING_CAPITAL and MONTHLY_START_CAPITAL.
    Returns the fetched capital (0.0 on failure).
    """
    print("\n4. Fetching live Upstox funds…")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        r = requests.get(
            f"{BASE_URL}/user/get-funds-and-margin",
            headers=headers,
            timeout=10,
        )
        print(f"   Funds API → HTTP {r.status_code}")
        if r.status_code != 200:
            print(f"   ❌ Response: {r.text[:300]}")
            return 0.0

        body = r.json()
        data = body.get("data", {})

        # Upstox returns data as a dict (equity/commodity keys) or
        # a list of dicts — handle both
        equity = {}
        if isinstance(data, dict):
            equity = data.get("equity", data)   # fallback: data itself
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("segment", "").upper() in ("SEC", "EQ", "EQUITY", ""):
                    equity = item
                    break
            if not equity and data:
                equity = data[0]

        available = float(equity.get("available_margin", 0) or 0)
        notional  = float(equity.get("notional_cash",   0) or 0)

        capital = available if available > 0 else notional
        if capital <= 0:
            print(f"   ⚠️  available_margin=0. Raw equity block: {equity}")
            print("       TRADING_CAPITAL left unchanged.")
            return 0.0

        _write_env_key(ENV, "TRADING_CAPITAL", str(round(capital, 2)))

        # MONTHLY_START_CAPITAL: only update on 1st of month OR if missing/zero.
        # Purpose: track month's opening balance for 60% monthly target calculation.
        # If set mid-month it resets the baseline — wrong.
        from datetime import datetime as _dt
        today_day = _dt.now().day
        existing_monthly = ""
        try:
            with open(ENV, "r", encoding="utf-8", errors="ignore") as _ef:
                for _el in _ef:
                    _el = _el.rstrip("\r\n").strip()
                    if _el.startswith("MONTHLY_START_CAPITAL="):
                        existing_monthly = _el.split("=", 1)[1].strip().strip("'\"")
                        break
        except Exception:
            pass

        if today_day == 1 or not existing_monthly or float(existing_monthly or 0) <= 0:
            _write_env_key(ENV, "MONTHLY_START_CAPITAL", str(round(capital, 2)))
            print(f"      MONTHLY_START_CAPITAL updated → ₹{capital:,.2f} "
                  f"({'1st of month' if today_day == 1 else 'first run / was missing'})")
        else:
            print(f"      MONTHLY_START_CAPITAL kept → ₹{float(existing_monthly):,.2f} "
                  f"(mid-month: baseline preserved)")

        load_dotenv(override=True)   # reflect new values in this process
        print(f"   ✅ Live capital fetched: ₹{capital:,.2f}")
        print(f"      TRADING_CAPITAL saved to .env")
        return capital

    except Exception as e:
        print(f"   ❌ Exception in _fetch_and_save_funds: {e}")
        return 0.0


def run():
    print("\n" + "="*55)
    print("  UPSTOX DAILY TOKEN REFRESH")
    print("="*55)
    if not KEY or not SECRET:
        print("❌ Set UPSTOX_API_KEY and UPSTOX_API_SECRET in .env")
        return

    url = (f"https://api.upstox.com/v2/login/authorization/dialog"
           f"?response_type=code&client_id={KEY}&redirect_uri={REDIR}")
    print(f"\n1. Opening browser for Upstox login…")
    webbrowser.open(url)
    print("2. Login → copy the FULL redirect URL from address bar.")
    print("   It looks like: https://127.0.0.1/?code=XXXXXXXX\n")

    redirect = input("Paste redirect URL: ").strip()
    parsed = urlparse(redirect)
    code   = parse_qs(parsed.query).get("code", [None])[0]
    if not code:
        print("❌ Could not find code in URL.")
        return

    print(f"\n3. Got code {code[:10]}… Exchanging…")
    r = requests.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={"code": code, "client_id": KEY, "client_secret": SECRET,
              "redirect_uri": REDIR, "grant_type": "authorization_code"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if r.status_code != 200:
        print(f"❌ Token exchange failed: {r.status_code} | {r.text[:200]}")
        return

    token = r.json().get("access_token", "").strip("'\"")
    _write_env_key(ENV, "UPSTOX_ACCESS_TOKEN", token)
    load_dotenv(override=True)
    print(f"\n✅ Token saved to .env: {token[:20]}…")

    # ── Step 4: Fetch live funds ──────────────────────────────
    _fetch_and_save_funds(token)

    # ── Step 5: Morning market setup (levels, VIX, config.py) ─
    print("\n5. Running morning market setup…")
    try:
        # Import AFTER load_dotenv so module-level os.getenv gets fresh token
        import morning_setup
        # Force re-read of token inside morning_setup (fixes stale HEADERS)
        morning_setup.HEADERS = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        morning_setup.TOKEN = token
        morning_setup.run()
    except Exception as e:
        print(f"   ⚠️  morning_setup error: {e}")
        print("   Run manually: python morning_setup.py")


if __name__ == "__main__":
    run()
