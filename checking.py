#!/usr/bin/env python3
"""
CodeCraft Network - Order Status Checker (INLINE VERSION)

Just edit:
- CODECRAFT_API_KEY
- REFERENCE_ID
- MODE  ("regular" or "bigtime")

Then run:
  python codecraft_status_check_inline.py
"""

import json
from urllib import request as urlrequest
from urllib.error import HTTPError, URLError

# ============================================================
# 🔧 EDIT THESE THREE VALUES ONLY
# ============================================================

CODECRAFT_API_KEY = "260109122317-?cZT8C-1AE8bv-LiNnt5-6A8s6Q-4j8kO6"
REFERENCE_ID = "API027215211528b5d18"   # example
MODE = "bigtime"                       # "regular" or "bigtime"

BASE_URL = "https://api.codecraftnetwork.com/api"
TIMEOUT = 45
# ============================================================


def _json_loads_safe(b: bytes):
    try:
        return json.loads(b.decode("utf-8", errors="replace"))
    except Exception:
        return {"raw": b.decode("utf-8", errors="replace")}


def check_status():
    mode = MODE.strip().lower()
    if mode not in ("regular", "bigtime"):
        raise ValueError("MODE must be 'regular' or 'bigtime'")

    endpoint = "/response_regular.php" if mode == "regular" else "/response_big_time.php"
    url = BASE_URL.rstrip("/") + endpoint

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }

    payload = {"reference_id": REFERENCE_ID}
    data = json.dumps(payload).encode("utf-8")

    req = urlrequest.Request(url, data=data, headers=headers, method="GET")

    try:
        with urlrequest.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read() or b""
            parsed = _json_loads_safe(body)
            return resp.status, parsed

    except HTTPError as e:
        body = e.read() or b""
        parsed = _json_loads_safe(body)
        return e.code, parsed

    except URLError as e:
        return None, {"error": str(e)}


def main():
    print("============================================================")
    print("CodeCraft Order Status Check (INLINE)")
    print("============================================================")
    print(f"Mode:        {MODE}")
    print(f"Reference:   {REFERENCE_ID}")
    print("------------------------------------------------------------")

    status, response = check_status()

    print(f"HTTP Status: {status}")
    print("------------------------------------------------------------")
    print("Raw Response:")
    print(json.dumps(response, indent=2, ensure_ascii=False))

    # Friendly summary
    data = response.get("data") if isinstance(response, dict) else None
    if isinstance(data, dict):
        print("------------------------------------------------------------")
        print("Summary:")
        print(f"  Beneficiary:  {data.get('beneficiary')}")
        print(f"  Network:      {data.get('network')}")
        print(f"  Gig:          {data.get('gig')}")
        print(f"  Price:        {data.get('price')}")
        print(f"  Order Status: {data.get('order_status')}")
        print(f"  Date:         {data.get('order_date')}")
        print(f"  Time:         {data.get('order_time')}")

    print("============================================================")


if __name__ == "__main__":
    main()
