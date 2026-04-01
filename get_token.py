# ================================================================
#  get_token.py — Run EVERY MORNING before 9 AM to refresh Upstox token
#  Usage: python get_token.py
# ================================================================
import os, webbrowser, requests
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv, set_key

load_dotenv()

KEY    = os.getenv("UPSTOX_API_KEY",    "")
SECRET = os.getenv("UPSTOX_API_SECRET", "")
REDIR  = os.getenv("UPSTOX_REDIRECT_URI", "https://127.0.0.1")
ENV    = ".env"

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
    if r.status_code == 200:
        token = r.json().get("access_token", "")
        set_key(ENV, "UPSTOX_ACCESS_TOKEN", token)
        print(f"\n✅ Token saved to .env: {token[:20]}…")

        # ── Auto-run morning setup ────────────────────────────
        print("\n4. Running morning auto-setup (levels, context)…")
        try:
            # Reload env so morning_setup sees the new token
            load_dotenv(override=True)
            import morning_setup
            morning_setup.run()
        except Exception as e:
            print(f"   ⚠️  morning_setup error: {e}")
            print("   Run manually: python morning_setup.py")
    else:
        print(f"❌ Token exchange failed: {r.status_code} | {r.text[:200]}")

if __name__ == "__main__":
    run()
