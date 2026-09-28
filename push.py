# push.py
import json
import sys
import time
from typing import Any, Dict

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    import cloudscraper  # pip install cloudscraper
except ImportError:
    print("cloudscraper is not installed. Run:  pip install cloudscraper", file=sys.stderr)
    sys.exit(1)

API_KEY = "0e7434520859996d4b758c7c77e22013690fc9ae"
ENDPOINT = "https://toppily.com/api/v1/check-console-balance"

# Toggle this to True if you want strict SSL verification
VERIFY_SSL = False  # using cloudscraper + verify=False to bypass cert issues as requested

def make_scraper():
    """
    Create a Cloudflare-aware session with sane defaults and retries.
    """
    scraper = cloudscraper.create_scraper(
        browser={
            "browser": "chrome",
            "platform": "windows",
            "mobile": False,
        },
        delay=10,              # politeness delay when challenged
        interpreter="nodejs",  # if available, helps with tougher challenges
    )

    # A bit more resilient retry logic at the HTTPAdapter level
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retries = Retry(
        total=5,
        connect=5,
        read=5,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"])
    )
    adapter = HTTPAdapter(max_retries=retries)

    scraper.mount("https://", adapter)
    scraper.mount("http://", adapter)

    # Default headers
    scraper.headers.update({
        "x-api-key": API_KEY,
        "accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
    })
    return scraper

def fetch_balance(scraper, timeout: float = 30.0) -> Dict[str, Any]:
    resp = scraper.get(ENDPOINT, timeout=timeout, verify=VERIFY_SSL)
    try:
        resp.raise_for_status()
    except Exception as e:
        # Include response text to aid debugging
        text = (resp.text or "").strip()
        raise SystemExit(f"[HTTP {resp.status_code}] {e}\nServer said:\n{text[:1000]}")

    try:
        return resp.json()
    except ValueError:
        raise SystemExit("Response was not valid JSON:\n" + resp.text[:1000])

def main():
    scraper = make_scraper()

    try:
        data = fetch_balance(scraper)
    except Exception as e:
        print(f"❌ Request failed: {e}")
        sys.exit(1)

    # Pretty print
    status = data.get("status")
    message = data.get("message")
    console_bal = data.get("userConsoleWalletBalance")
    normal_bal = data.get("userNormalBalance")

    print("✅ Toppily Balance Check")
    print(f"Status : {status}")
    print(f"Message: {message}")
    print(f"Console Wallet Balance: {console_bal}")
    print(f"Normal Balance        : {normal_bal}")
    print("\nRaw JSON:")
    print(json.dumps(data, indent=2))

if __name__ == "__main__":
    main()
