"""
fyers_auth.py
Handles Fyers OAuth2 login, token saving and loading.
Run this file directly to do the daily login.
"""

import json
import os
import webbrowser
from datetime import datetime, date
from fyers_apiv3 import fyersModel

# ─── Load config ───────────────────────────────────────────────────────────────
def load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r") as f:
        return json.load(f)

CONFIG = load_config()
CREDS  = CONFIG["fyers_credentials"]
TOKEN_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          CONFIG["token_file"])

# ─── Token helpers ─────────────────────────────────────────────────────────────
def save_token(access_token: str):
    """Save token with today's date so we know when it expires."""
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    data = {
        "access_token": access_token,
        "saved_date": str(date.today())
    }
    with open(TOKEN_FILE, "w") as f:
        json.dump(data, f, indent=2)
    print(f"✅ Token saved successfully for {date.today()}")


def _jwt_exp_seconds_left(jwt: str) -> float:
    """Decode the JWT `exp` claim and return seconds until expiry.
    Returns negative if expired, math.inf if undecodable (so caller can
    fall back to legacy behaviour). Fyers tokens are short-lived
    (~5-6 hours) and expire around 06:00 IST regardless of saved_date."""
    import base64, math, json, time
    if not jwt or jwt.count(".") != 2:
        return math.inf
    try:
        payload_b64 = jwt.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        if not exp:
            return math.inf
        return exp - time.time()
    except Exception:
        return math.inf


def load_token():
    """Load token if its JWT exp is still in the future, else return None.
    Previously checked saved_date == today which falsely returned valid
    tokens long after the JWT had expired (Fyers tokens expire ~06:00 IST,
    not at midnight). Now properly decodes the JWT exp claim."""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE, "r") as f:
        data = json.load(f)
    jwt = data.get("access_token")
    if not jwt:
        return None
    seconds_left = _jwt_exp_seconds_left(jwt)
    if seconds_left < 0:
        print(f"⚠️  Token JWT expired {abs(seconds_left)/3600:.1f}h ago. "
              f"Need fresh login.")
        return None
    if seconds_left < 600:
        print(f"⚠️  Token JWT expires in {seconds_left/60:.0f} minutes. "
              f"Login again for a fresh one.")
        return None
    return jwt


def is_token_valid():
    """Check if we have a valid (unexpired-JWT) token."""
    return load_token() is not None


# ─── Login flow ────────────────────────────────────────────────────────────────
def do_login():
    """
    Full Fyers OAuth2 login flow.
    Opens browser → user logs in → pastes auth code → token saved.
    """
    print("\n" + "="*60)
    print("  FYERS DAILY LOGIN")
    print("="*60)

    # Step 1: Generate the login URL
    session = fyersModel.SessionModel(
        client_id      = CREDS["client_id"],
        secret_key     = CREDS["secret_key"],
        redirect_uri   = CREDS["redirect_uri"],
        response_type  = CREDS["response_type"],
        grant_type     = CREDS["grant_type"]
    )
    auth_url = session.generate_authcode()

    print("\n📌 Step 1: Your browser will open the Fyers login page.")
    print("           Log in with your Fyers credentials.")
    print("\n📌 Step 2: After logging in, you will be redirected to a URL.")
    print("           Copy the FULL URL from your browser address bar.")
    print("           It will look like:")
    print("           https://yourredirect.com/?code=XXXXXXX&state=None")
    print("\nOpening browser now...")

    webbrowser.open(auth_url)

    # Step 3: User pastes the redirect URL
    print("\n" + "-"*60)
    redirect_url = input("📋 Paste the full redirect URL here and press Enter:\n> ").strip()

    # Extract auth code from URL
    if "auth_code=" in redirect_url:
        auth_code = redirect_url.split("auth_code=")[1].split("&")[0]
    elif "code=" in redirect_url:
        auth_code = redirect_url.split("code=")[1].split("&")[0]
    else:
        print("❌ Could not find auth code in URL. Please try again.")
        return None

    # Step 4: Generate access token
    session.set_token(auth_code)
    response = session.generate_token()

    if response.get("code") == 200 or "access_token" in response:
        access_token = response["access_token"]
        save_token(access_token)
        print("\n✅ Login successful! App is ready to run.")
        return access_token
    else:
        print(f"\n❌ Login failed: {response}")
        return None


def get_fyers_client():
    """
    Returns an authenticated Fyers client.
    If token is valid for today, uses saved token.
    Otherwise, prompts for login.
    """
    token = load_token()
    if not token:
        print("\n🔐 No valid token found. Starting login process...")
        token = do_login()
        if not token:
            raise Exception("❌ Login failed. Cannot start app without valid token.")

    client = fyersModel.FyersModel(
        client_id    = CREDS["client_id"],
        token        = token,
        is_async     = False,
        log_path     = ""
    )
    return client


# ─── Run directly for daily login ──────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    force = "--force" in sys.argv or "-f" in sys.argv
    print("\n🚀 Starting Fyers Login...")
    if force:
        print("   --force flag: skipping existing-token check, doing fresh login.")
        do_login()
    else:
        token = load_token()
        if token:
            secs = _jwt_exp_seconds_left(token)
            from datetime import datetime
            import pytz
            IST = pytz.timezone("Asia/Kolkata")
            print(f"\n✅ Logged in. Token JWT valid for "
                  f"{secs/3600:.1f} more hours.")
            print(f"   To force a fresh login anyway: python core/fyers_auth.py --force")
        else:
            do_login()
