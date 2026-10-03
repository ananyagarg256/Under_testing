"""
Test that your Roostoo API keys work.
Keys are read from the .env file, never written in code.

Check the Roostoo API docs for the exact base URL, endpoints and header
names; the values below follow their documented v3 format.
"""
import os
import time
import hmac
import hashlib

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env into environment variables

API_KEY = os.getenv("ROOSTOO_API_KEY")
API_SECRET = os.getenv("ROOSTOO_API_SECRET")
BASE_URL = "https://mock-api.roostoo.com"

if not API_KEY or not API_SECRET:
    raise SystemExit("Keys not found. Is your .env file in this folder?")


def sign(params: dict) -> str:
    """Create the HMAC-SHA256 signature from the request parameters."""
    # Sort params alphabetically and join as key=value&key=value
    query = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()


# 1. Public request: no keys needed
r = requests.get(f"{BASE_URL}/v3/serverTime", timeout=10)
print("Server time:", r.json())

# 2. Signed request: proves your key + secret are valid
params = {"timestamp": str(int(time.time() * 1000))}
headers = {
    "RST-API-KEY": API_KEY,
    "MSG-SIGNATURE": sign(params),
}
r = requests.get(f"{BASE_URL}/v3/balance", params=params, headers=headers, timeout=10)
print("Balance:", r.json())