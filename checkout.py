from flask import Blueprint, request, jsonify, session, render_template, abort
from bson import ObjectId
from datetime import datetime, timedelta
import os, uuid, random, requests, traceback, json, ast, re, threading, time
from urllib.parse import quote

from db import db
from phone_order_guard import BLOCK_NEW_NUMBERS_MESSAGE, first_blocked_phone
from routes.provider_wallet import compute_provider_cost, debit_provider, credit_provider

checkout_bp = Blueprint("checkout", __name__)

# MongoDB Collections
balances_col        = db["balances"]
orders_col          = db["orders"]
transactions_col    = db["transactions"]
services_col        = db["services"]
service_profits_col = db["service_profits"]  # per-customer overrides
provider_transactions_col = db["provider_transactions"]
users_col           = db["users"]  # ✅ for invoice view


# ===== DataConnect Provider Config (replaces old DataVerse) ===================
DATACONNECT_BASE_URL = "https://dataconnectgh.com/api/v1"
DATACONNECT_API_KEY = os.getenv(
    "DATACONNECT_API_KEY",
    "90bcf2f236b8c95547b58b531f5c597df8a061a8",  # fallback; you can remove/harden
)

# ===== CodeCraft Provider Config =============================================
CODECRAFT_BASE_URL = os.getenv("CODECRAFT_BASE_URL", "https://api.codecraftnetwork.com/api")
CODECRAFT_API_KEY = os.getenv(
    "CODECRAFT_API_KEY",
    "260109122317-?cZT8C-1AE8bv-LiNnt5-6A8s6Q-4j8kO6",
)

# ===== DataKazina Provider Config ============================================
DATAKAZINA_BASE_URL = os.getenv(
    "DATAKAZINA_BASE_URL",
    "https://reseller.dakazinabusinessconsult.com/api/v1",
)
DATAKAZINA_API_KEY = "dk_wC8jFCnsbwJJmWcMwNlxXQWNojxX43Gy"
DATAKAZINA_TIMEOUT = int(os.getenv("DATAKAZINA_TIMEOUT", "45"))

# ===== SKPlug Provider Config ===============================================
SKPLUG_BASE_URL = os.getenv(
    "SKPLUG_BASE_URL",
    "https://skplug.onrender.com/api/v1/",
)
SKPLUG_API_KEY = os.getenv(
    "SKPLUG_API_KEY",
    "270103449bf5069c331eb4511845e6b43a9e9fd7d75d57d1ba317ca9342abcd3",
)
SKPLUG_TIMEOUT = int(os.getenv("SKPLUG_TIMEOUT", "45"))

# ===== BundlePortal Provider Config =========================================
BUNDLEPORTAL_BASE_URL = os.getenv("BUNDLEPORTAL_BASE_URL", "https://api.bundleportal.com/v1")
BUNDLEPORTAL_API_KEY = os.getenv(
    "BUNDLEPORTAL_API_KEY",
    "bp_live_3aac2b1cf1fb49c081f598406220c9c2",
)
BUNDLEPORTAL_TIMEOUT = int(os.getenv("BUNDLEPORTAL_TIMEOUT", "45"))

# ===== Portal-02 Provider Config =============================================
PORTAL02_BASE_URL = "https://www.portal-02.com/api/v1"
PORTAL02_API_KEY = "dk_mJmQDFQWmDId4RT_c5HrEghcgwujPAFf"
PORTAL02_WEBHOOK_URL = "https://www.portal-02.com/api/webhooks/orders"
PORTAL02_OFFER_SLUG_MTN_NORMAL = "master_beneficiary_data_bundle"
MTN_NORMAL_SERVICE_ID = "68b8b6a7eb0ced45901c68d2"

# ===== Arkesel order alert config ============================================
ARKESEL_ORDER_SMS_API_KEY = os.getenv(
    "ARKESEL_ORDER_SMS_API_KEY",
    "TGFhVVZvU3NOclJMZFJwWWJ5U2o",
)
ARKESEL_ORDER_SMS_SENDER_ID = os.getenv("ARKESEL_ORDER_SMS_SENDER_ID", "CAMPUS DATA")
ARKESEL_ORDER_ALERT_TO = os.getenv("ARKESEL_ORDER_ALERT_TO", "0553226196")
ARKESEL_ORDER_ALERT_SERVICE_ID = os.getenv("ARKESEL_ORDER_ALERT_SERVICE_ID", "6a299f7472e6d9d109a67ad8")
ARKESEL_ORDER_ALERT_SERVICE_NAME = os.getenv("ARKESEL_ORDER_ALERT_SERVICE_NAME", "MTN MASHUP DATA")


# Network ID fallback (internal use)
NETWORK_ID_FALLBACK = {
    "MTN": 3,
    "VODAFONE": 2,
    "AIRTELTIGO": 1,
}

# ===== CodeCraft package cache ===============================================
_CODECRAFT_PKG_CACHE = {"ts": None, "regular": {}, "bigtime": {}}
CODECRAFT_PKG_TTL_SECONDS = 300


# ===== Tiny JSON logger =======================================================
def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


# ===== Helpers ================================================================
def generate_order_id():
    return f"CAMP{random.randint(100000, 999999)}"


def _money(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _split_order_documents(order_doc: dict, results: list[dict], batch_id: str):
    """Build one independently tracked, one-item Campus order per cart line."""
    docs = []
    order_ids = []
    for position, item in enumerate(results, start=1):
        line_order_id = generate_order_id()
        order_ids.append(line_order_id)
        line = dict(item)
        line["order_id"] = line_order_id
        doc = dict(order_doc)
        doc.update({
            "order_id": line_order_id,
            "batch_id": batch_id,
            "batch_position": position,
            "batch_size": len(results),
            "items": [line],
            "total_amount": round(_money(line.get("amount")), 2),
            "charged_amount": 0.0 if str(line.get("line_status") or "").startswith("skipped") else round(_money(line.get("amount")), 2),
            "profit_amount_total": round(_money(line.get("profit_amount")), 2),
        })
        docs.append(doc)
    return docs, order_ids


def _to_float(x, default=None):
    try:
        return float(x)
    except Exception:
        return default


def _clean_api_key(v):
    """
    Remove stray unicode/control chars sometimes introduced by env copy/paste.
    """
    if not isinstance(v, str):
        return ""
    # strip common invisible unicode + non-printable chars
    cleaned = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060\ufeff]", "", v)
    cleaned = re.sub(r"[^\x20-\x7E]", "", cleaned)
    return cleaned.strip()


def _normalize_sms_msisdn(raw: str | None) -> str | None:
    """
    Return Ghana MSISDN in 233XXXXXXXXX format for Arkesel delivery.
    """
    digits = re.sub(r"\D+", "", str(raw or ""))
    if not digits:
        return None
    if digits.startswith("233") and len(digits) == 12:
        return digits
    if digits.startswith("0") and len(digits) == 10:
        return "233" + digits[1:]
    if len(digits) == 9:
        return "233" + digits
    return None


def _send_arkesel_order_sms(msisdn: str, message: str) -> str:
    """
    Best-effort SMS send via Arkesel. Returns a short delivery state.
    """
    api_key = _clean_api_key(ARKESEL_ORDER_SMS_API_KEY or "")
    sender_id = (ARKESEL_ORDER_SMS_SENDER_ID or "").strip() or "CAMPUS DATA"
    if not api_key:
        return "missing_api_key"
    if not msisdn:
        return "invalid_recipient"
    if not message:
        return "empty_message"

    try:
        url = (
            "https://sms.arkesel.com/sms/api?action=send-sms"
            f"&api_key={quote(api_key)}"
            f"&to={quote(msisdn)}"
            f"&from={quote(sender_id)}"
            f"&sms={quote(message)}"
        )
        resp = requests.get(url, timeout=12)
        body = resp.text or ""
        if resp.status_code == 200 and ('"code":"ok"' in body or '"status":"success"' in body.lower()):
            return "sent"
        jlog("arkesel_order_sms_failed", http_status=resp.status_code, response=body[:500])
        return "failed"
    except Exception as e:
        jlog("arkesel_order_sms_error", error=str(e))
        return "error"


def _is_arkesel_alert_service_item(item: dict) -> bool:
    service_id = str(item.get("serviceId") or "").strip()
    if service_id and service_id == str(ARKESEL_ORDER_ALERT_SERVICE_ID or "").strip():
        return True
    service_name = str(item.get("serviceName") or "").strip().upper()
    wanted_name = str(ARKESEL_ORDER_ALERT_SERVICE_NAME or "").strip().upper()
    return bool(service_name and wanted_name and service_name == wanted_name)


def _format_gb_label(raw_value) -> str | None:
    try:
        gb = float(raw_value)
    except Exception:
        return None
    if gb <= 0:
        return None
    if gb.is_integer():
        return f"{int(gb)}GB"
    return f"{gb:g}GB"


def _format_arkesel_volume_label(item: dict) -> str:
    """
    Return a compact bundle label for the order alert, e.g. 10GB.
    """
    if not isinstance(item, dict):
        return "UNKNOWN"

    for key in ("provider_gb_size", "provider_gig", "package_size_gb", "gb", "gb_size", "volume_gb", "size_gb"):
        label = _format_gb_label(item.get(key))
        if label:
            return label

    value_obj = item.get("value_obj")
    if not isinstance(value_obj, dict):
        value_obj = _coerce_value_obj(value_obj or item.get("value"))

    if isinstance(value_obj, dict):
        for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
            label = _format_gb_label(value_obj.get(key))
            if label:
                return label

        vol = value_obj.get("volume")
        if vol not in (None, "", []):
            try:
                vol_f = float(vol)
                gb = round(vol_f / 1024.0) if vol_f > 50 else vol_f
                label = _format_gb_label(gb)
                if label:
                    return label
            except Exception:
                pass

    raw_candidates = (
        item.get("value"),
        item.get("label"),
        item.get("value_text"),
        item.get("bundle_key", {}).get("value") if isinstance(item.get("bundle_key"), dict) else None,
    )
    for raw in raw_candidates:
        if raw in (None, "", []):
            continue
        raw_text = str(raw).strip()
        gb_match = re.search(r"(\d+(?:\.\d+)?)\s*gb\b", raw_text, flags=re.I)
        if gb_match:
            label = _format_gb_label(gb_match.group(1))
            if label:
                return label

    package_size_gb = _resolve_package_size_gb(value_obj if isinstance(value_obj, dict) else {}, item)
    label = _format_gb_label(package_size_gb)
    return label or "UNKNOWN"


def _format_arkesel_order_alert_message(phone: str, volume: str, order_id: str, created_at) -> str:
    try:
        dt = created_at if isinstance(created_at, datetime) else datetime.utcnow()
        stamp = dt.strftime("%d/%m/%Y %H:%M")
    except Exception:
        stamp = datetime.utcnow().strftime("%d/%m/%Y %H:%M")
    return f"{phone} {volume} {order_id} {stamp}"


def _send_arkesel_alerts_for_order(order_doc: dict, orders_collection=None, inserted_id=None) -> list[dict]:
    """
    Send best-effort SMS alerts for matching service lines only.
    This never raises into checkout/store order creation.
    """
    alerts: list[dict] = []
    try:
        order_id = str(order_doc.get("order_id") or "").strip()
        if not order_id:
            return alerts

        target_msisdn = _normalize_sms_msisdn(ARKESEL_ORDER_ALERT_TO)
        if not target_msisdn:
            jlog("arkesel_order_sms_skipped", order_id=order_id, reason="invalid_target_number")
            return alerts

        created_at = order_doc.get("created_at") or datetime.utcnow()
        items = order_doc.get("items") or []
        for idx, item in enumerate(items, start=1):
            if not isinstance(item, dict):
                continue
            if not _is_arkesel_alert_service_item(item):
                continue

            line_status = str(item.get("line_status") or "").strip().lower()
            if line_status.startswith("skipped"):
                continue

            phone = str(item.get("phone") or "").strip()
            if not phone:
                alerts.append(
                    {
                        "line_index": idx,
                        "service_id": str(item.get("serviceId") or ""),
                        "service_name": item.get("serviceName") or "",
                        "status": "missing_phone",
                        "created_at": datetime.utcnow(),
                    }
                )
                continue

            volume = _format_arkesel_volume_label(item)
            message = _format_arkesel_order_alert_message(phone, volume, order_id, created_at)
            sms_status = _send_arkesel_order_sms(target_msisdn, message)
            alerts.append(
                {
                    "line_index": idx,
                    "service_id": str(item.get("serviceId") or ""),
                    "service_name": item.get("serviceName") or "",
                    "phone": phone,
                    "to": target_msisdn,
                    "message": message,
                    "status": sms_status,
                    "created_at": datetime.utcnow(),
                }
            )

        if alerts:
            update_q = {"_id": inserted_id} if inserted_id is not None else {"order_id": order_id}
            if orders_collection is not None:
                try:
                    orders_collection.update_one(
                        update_q,
                        {"$set": {"order_sms_alerts": alerts, "updated_at": datetime.utcnow()}},
                    )
                except Exception as e:
                    jlog("arkesel_order_sms_audit_update_failed", order_id=order_id, error=str(e))
    except Exception as e:
        jlog("arkesel_order_sms_alert_uncaught", order_id=order_doc.get("order_id"), error=str(e))
    return alerts


def _insert_order_doc_like_checkout(orders_collection, order_doc: dict):
    """
    Shared order insert hook used by checkout and store checkout.
    Persists the order first, then sends best-effort Arkesel alerts.
    """
    res = orders_collection.insert_one(order_doc)
    _send_arkesel_alerts_for_order(order_doc, orders_collection=orders_collection, inserted_id=res.inserted_id)
    return res


def _coerce_value_obj(v):
    """
    Accepts dict, JSON string, or python-dict-like string.
    Returns a dict (possibly empty).
    """
    if isinstance(v, dict):
        return v
    if not v:
        return {}
    s = str(v).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
            return d if isinstance(d, dict) else {}
        except Exception:
            try:
                d = ast.literal_eval(s)
                return d if isinstance(d, dict) else {}
            except Exception:
                return {}
    return {}


# ===== Ported number fields ==================================================
def _extract_ported_fields(item: dict) -> dict:
    if not isinstance(item, dict):
        return {}
    out = {}
    if "ported_confirmed" in item:
        out["ported_confirmed"] = bool(item.get("ported_confirmed"))
    for key in ("ported_expected_network", "ported_detected_network", "ported_prefix"):
        val = item.get(key)
        if val not in (None, ""):
            out[key] = str(val)
    return out


# ===== Profit helpers (absolute profit amount) ================================
def _get_service_default_profit_percent(service_doc):
    return _to_float(service_doc.get("default_profit_percent"), 0.0) or 0.0


def _get_customer_profit_override_percent(service_id, customer_id_obj):
    ov = service_profits_col.find_one({"service_id": service_id, "customer_id": customer_id_obj})
    return _to_float(ov.get("profit_percent"), None) if ov else None


def _effective_profit_percent(service_doc, customer_id_obj):
    override = _get_customer_profit_override_percent(service_doc["_id"], customer_id_obj)
    return override if override is not None else _get_service_default_profit_percent(service_doc)


def _pick_offer_base_amount_from_service(svc_doc, value_obj, raw_value):
    """
    Try to recover the base (wholesale) amount from the selected offer in svc_doc.offers.
    """
    try:
        offers = svc_doc.get("offers") or []
        vid = (value_obj or {}).get("id")
        vvol = (value_obj or {}).get("volume")
        for of in offers:
            of_val = of.get("value")
            of_amt = _to_float(of.get("amount"))
            if isinstance(of_val, str) and of_val.strip().startswith("{") and of_val.strip().endswith("}"):
                try:
                    of_val = json.loads(of_val)
                except Exception:
                    try:
                        of_val = ast.literal_eval(of_val)
                    except Exception:
                        pass
            if isinstance(of_val, dict):
                if (vid is not None and of_val.get("id") == vid) or (vvol is not None and of_val.get("volume") == vvol):
                    return of_amt
            else:
                if raw_value is not None and of_val == raw_value:
                    return of_amt
    except Exception:
        pass
    return None


def _derive_base_profit(amount_total, base_amount_hint, eff_percent):
    a = _money(amount_total)
    if a <= 0:
        return 0.0, 0.0
    if base_amount_hint is not None and base_amount_hint > 0:
        base = float(base_amount_hint)
        profit = round(a - base, 2)
        if profit < 0:
            profit = 0.0
            base = a
        return round(base, 2), profit
    p = _to_float(eff_percent, 0.0) or 0.0
    try:
        base = round(a / (1.0 + (p / 100.0)), 2) if p > 0 else a
    except Exception:
        base = a
    profit = round(a - base, 2)
    if profit < 0:
        profit = 0.0
        base = a
    return base, profit


# ===== Field resolvers =======================================================
def _resolve_network_id(item: dict, value_obj: dict, svc_doc: dict | None):
    """
    Internal numeric network ID, used only for duplicate guards / reporting.
    Not sent to providers.
    """
    nid = (item or {}).get("network_id") or (value_obj or {}).get("network_id")
    if nid not in (None, "", []):
        try:
            return int(nid)
        except Exception:
            pass
    if svc_doc:
        try:
            if "network_id" in svc_doc and svc_doc["network_id"] not in (None, ""):
                return int(svc_doc["network_id"])
            guess = (svc_doc.get("name") or svc_doc.get("network") or "").strip().upper()
            if guess and guess in NETWORK_ID_FALLBACK:
                return int(NETWORK_ID_FALLBACK[guess])
        except Exception:
            pass
    if not svc_doc:
        name = (item.get("serviceName") or "").strip().upper()
        if name in NETWORK_ID_FALLBACK:
            return int(NETWORK_ID_FALLBACK[name])
    return None


def _resolve_dataconnect_network(svc_doc: dict | None, item: dict) -> str | None:
    """
    Resolve generic 'network' slug we also reuse:
      - 'mtn'
      - 'telecel'
      - 'airteltigo'
    Used for routing (DataConnect vs manual processing).
    """
    doc = svc_doc

    # Fallback: look up by service name if svc_doc is missing
    if not doc:
        sname = (item.get("serviceName") or "").strip()
        if sname:
            try:
                doc = services_col.find_one(
                    {"name": sname},
                    {"service_network": 1, "network": 1, "name": 1},
                )
            except Exception:
                doc = None

    candidates = []
    if doc:
        candidates.append(doc.get("service_network"))
        candidates.append(doc.get("network"))
        candidates.append(doc.get("name"))

    candidates.append(item.get("network"))
    candidates.append(item.get("network_name"))
    candidates.append(item.get("serviceName"))

    joined = " ".join(str(c) for c in candidates if c).lower()

    if "mtn" in joined:
        return "mtn"

    # Telecel / Vodafone rebrand
    if "telecel" in joined or "vodafone" in joined:
        return "telecel"

    # AirtelTigo / AT / iShare
    if (
        "airteltigo" in joined
        or "airtel tigo" in joined
        or "airtel-tigo" in joined
        or "at - ishare" in joined
        or "i share" in joined
        or "ishare" in joined
    ):
        return "airteltigo"

    return None


def _resolve_bundleportal_network(svc_doc: dict | None, item: dict) -> str | None:
    """Return the network slug accepted by BundlePortal for this service."""
    candidates = []
    if svc_doc:
        candidates.extend((svc_doc.get("service_network"), svc_doc.get("network"), svc_doc.get("name")))
    candidates.extend((item.get("network"), item.get("network_name"), item.get("serviceName")))
    joined = " ".join(str(value) for value in candidates if value).lower()
    if "ishare" in joined or "i share" in joined or "bigtime" in joined or "big time" in joined:
        return "airteltigo"
    return _resolve_dataconnect_network(svc_doc, item)


def _resolve_codecraft_network_name(svc_doc: dict | None, item: dict) -> str | None:
    resolved = _resolve_dataconnect_network(svc_doc, item)
    if resolved == "mtn":
        return "MTN"
    if resolved == "telecel":
        return "TELECEL"
    if resolved == "airteltigo":
        return "AT"

    name = ""
    if svc_doc:
        name = " ".join(
            str(x)
            for x in (
                svc_doc.get("service_network"),
                svc_doc.get("network"),
                svc_doc.get("name"),
            )
            if x
        )
    if not name:
        name = " ".join(
            str(x)
            for x in (item.get("serviceName"), item.get("network"), item.get("network_name"))
            if x
        )
    low = name.lower()
    if "telecel" in low or "vodafone" in low:
        return "TELECEL"
    if "mtn" in low:
        return "MTN"
    if "airteltigo" in low or "tigo" in low or "ishare" in low or "i share" in low or low.startswith("at "):
        return "AT"
    return None


def _resolve_skplug_network_name(svc_doc: dict | None, item: dict) -> str | None:
    resolved = _resolve_dataconnect_network(svc_doc, item)
    if resolved == "mtn":
        return "MTN"
    if resolved == "telecel":
        return "TELECEL"
    if resolved == "airteltigo":
        return "AIRTELTIGO"

    name = ""
    if svc_doc:
        name = " ".join(
            str(x)
            for x in (
                svc_doc.get("service_network"),
                svc_doc.get("network"),
                svc_doc.get("name"),
            )
            if x
        )
    if not name:
        name = " ".join(
            str(x)
            for x in (item.get("serviceName"), item.get("network"), item.get("network_name"))
            if x
        )
    low = name.lower()
    if "telecel" in low or "vodafone" in low:
        return "TELECEL"
    if "mtn" in low:
        return "MTN"
    if "airteltigo" in low or "airtel tigo" in low or "airtel-tigo" in low or "ishare" in low:
        return "AIRTELTIGO"
    return None


def _resolve_package_size_gb(value_obj: dict, item: dict) -> int | None:
    """
    Resolve bundle size (integer GB) to use as provider "volume".
    """
    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    # 1) explicit GB fields
    for key in ("gb", "gb_size", "package_size", "volume_gb", "size_gb"):
        val = value_obj.get(key)
        if val not in (None, "", []):
            try:
                return int(float(val))
            except Exception:
                pass

    # 2) 'volume' field (can be GB or MB)
    vol = value_obj.get("volume")
    if vol not in (None, "", []):
        try:
            vol_f = float(vol)
            if vol_f > 50:
                gb = max(1, round(vol_f / 1024.0))
            else:
                gb = vol_f
            return int(gb)
        except Exception:
            pass

    # 3) Parse from item['value'] string like '1GB', '5 GB'
    raw_val = item.get("value") or ""
    if isinstance(raw_val, str):
        m = re.search(r"(\d+(?:\.\d+)?)\s*gb", raw_val.lower())
        if m:
            try:
                return int(float(m.group(1)))
            except Exception:
                pass
        m2 = re.search(r"(\d+(?:\.\d+)?)", raw_val)
        if m2:
            try:
                return int(float(m2.group(1)))
            except Exception:
                pass

    return None


def _resolve_datakazina_shared_bundle(value_obj: dict, item: dict) -> int | None:
    """
    DataKazina expects shared_bundle to be the package/offer ID.
    Prefer value_obj["id"] when present; otherwise fall back to volume or raw value.
    """
    def _parse_int(v):
        if v in (None, "", []):
            return None
        if isinstance(v, (int, float)):
            try:
                return int(float(v))
            except Exception:
                return None
        s = str(v).strip()
        if not s:
            return None
        m = re.findall(r"(\d+(?:\.\d+)?)", s)
        if not m:
            return None
        try:
            return int(float(m[0]))
        except Exception:
            return None

    if not isinstance(value_obj, dict):
        value_obj = value_obj or {}

    # 1) Explicit offer/package id (preferred)
    id_val = _parse_int(value_obj.get("id"))
    if id_val:
        return id_val

    # 2) Explicit shared_bundle if already present
    sb_val = _parse_int(value_obj.get("shared_bundle"))
    if sb_val:
        return sb_val

    # 3) Volume fallback (may be MB or GB depending on stored offer)
    vol_val = _parse_int(value_obj.get("volume"))
    if vol_val:
        # If stored as MB (e.g., 1000), map to GB count when plausible
        if vol_val >= 1000 and vol_val % 1000 == 0:
            return max(1, int(vol_val / 1000))
        return vol_val

    # 4) Raw value fallback (e.g., "1GB")
    raw_val = item.get("value") or item.get("label") or item.get("value_text")
    raw_int = _parse_int(raw_val)
    if raw_int:
        return raw_int

    return None


def _normalize_portal02_phone(phone: str) -> str:
    """
    Normalize Ghana numbers for Portal-02 only.
    - 0530xxxxxx -> 233530xxxxxx
    - 233xxxxxxxxx stays
    """
    p = re.sub(r"\s+", "", str(phone or ""))
    if p.startswith("+"):
        p = p[1:]
    if p.startswith("0") and len(p) >= 10:
        return "233" + p[1:]
    if p.startswith("233"):
        return p
    return p


def _is_mtn_normal_service(service_id_raw, svc_doc) -> bool:
    try:
        if service_id_raw and str(service_id_raw) == MTN_NORMAL_SERVICE_ID:
            return True
    except Exception:
        pass
    try:
        if svc_doc and svc_doc.get("_id") and str(svc_doc.get("_id")) == MTN_NORMAL_SERVICE_ID:
            return True
    except Exception:
        pass
    return False


def _build_bundle_key(value_obj: dict, item: dict):
    """
    Build a generic bundle key for duplicate detection.
    Returns ('bundle', <normalized_value>) or None.
    """
    val = None
    if isinstance(value_obj, dict):
        for key in ("id", "volume", "code", "package_size", "gb"):
            if value_obj.get(key) not in (None, "", []):
                val = value_obj.get(key)
                break
    if val is None:
        val = item.get("value") or item.get("label")

    if val is None:
        return None

    try:
        norm = int(float(val))
    except Exception:
        norm = str(val).strip()

    return ("bundle", norm)


# ===== Provider callers (used by background worker) ==========================
def _codecraft_get_packages_cached():
    now = time.time()
    ts = _CODECRAFT_PKG_CACHE.get("ts")
    if ts and (now - ts) < CODECRAFT_PKG_TTL_SECONDS:
        return _CODECRAFT_PKG_CACHE.get("regular", {}), _CODECRAFT_PKG_CACHE.get("bigtime", {})

    if not CODECRAFT_API_KEY:
        return {}, {}

    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/packages.php"
    headers = {
        "Accept": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        root = payload.get("data") if isinstance(payload, dict) else {}
        if not isinstance(root, dict):
            root = {}
        if isinstance(root.get("data"), dict):
            root = root.get("data") or {}

        reg_list = root.get("regular_packages") or []
        big_list = root.get("bigtime_packages") or []

        jlog(
            "codecraft_packages_raw",
            http_status=resp.status_code,
            body_len=len(text),
            root_keys=list(root.keys()) if isinstance(root, dict) else [],
            regular_count=len(reg_list) if isinstance(reg_list, list) else 0,
            bigtime_count=len(big_list) if isinstance(big_list, list) else 0,
        )

        def _pull_field(dct, keys):
            for k in keys:
                if k in dct:
                    return dct.get(k)
            return None

        def _norm_network(v):
            s = str(v or "").strip().upper()
            if not s:
                return None
            low = s.lower()
            if "mtn" in low:
                return "MTN"
            if "telecel" in low or "vodafone" in low:
                return "TELECEL"
            if "airteltigo" in low or "tigo" in low or "ishare" in low or "i share" in low or low.startswith("at"):
                return "AT"
            return s

        def _norm_gig(v):
            if v is None:
                return None
            try:
                s = str(v).strip().lower()
                if not s:
                    return None
                if "gb" in s:
                    num = re.findall(r"(\d+(?:\.\d+)?)", s)
                    return int(round(float(num[0]))) if num else None
                if "mb" in s:
                    num = re.findall(r"(\d+(?:\.\d+)?)", s)
                    if not num:
                        return None
                    mb = float(num[0])
                    return max(1, int(round(mb / 1000.0)))
                # numeric string
                val = float(s)
            except Exception:
                return None
            if val >= 100:
                return max(1, int(round(val / 1000.0)))
            return max(1, int(round(val)))

        regular_map = {}
        bigtime_map = {}

        for p in reg_list if isinstance(reg_list, list) else []:
            if not isinstance(p, dict):
                continue
            net = _pull_field(p, ("network", "Network", "operator", "provider"))
            gig = _pull_field(p, ("package", "gig", "Gig", "volume", "gb"))
            amt = _pull_field(p, ("amount", "price", "Amount", "cost"))
            net_norm = _norm_network(net)
            gig_int = _norm_gig(gig)
            if net_norm is None or gig_int is None:
                continue
            key = (net_norm, gig_int)
            regular_map[key] = _to_float(amt, None)

        for p in big_list if isinstance(big_list, list) else []:
            if not isinstance(p, dict):
                continue
            net = _pull_field(p, ("network", "Network", "operator", "provider"))
            gig = _pull_field(p, ("package", "gig", "Gig", "volume", "gb"))
            amt = _pull_field(p, ("amount", "price", "Amount", "cost"))
            net_norm = _norm_network(net)
            gig_int = _norm_gig(gig)
            if net_norm is None or gig_int is None:
                continue
            key = (net_norm, gig_int)
            bigtime_map[key] = _to_float(amt, None)

        _CODECRAFT_PKG_CACHE["ts"] = now
        _CODECRAFT_PKG_CACHE["regular"] = regular_map
        _CODECRAFT_PKG_CACHE["bigtime"] = bigtime_map
        jlog(
            "codecraft_packages_loaded",
            regular_count=len(regular_map),
            bigtime_count=len(bigtime_map),
            regular_keys=list(regular_map.keys()),
            bigtime_keys=list(bigtime_map.keys()),
        )
        return regular_map, bigtime_map
    except Exception as e:
        jlog("codecraft_packages_error", error=str(e))
        return {}, {}


def _codecraft_submit_regular(phone: str, gig: int, network: str):
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "error": "CODECRAFT API key not configured", "http_status": 500}, None
    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/initiate.php"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    body = {"recipient_number": phone, "gig": str(gig), "network": network}
    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
    jlog(
        "codecraft_submit_request",
        mode="regular",
        network=network,
        gig=gig,
        phone=masked,
        url=url,
    )
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}
        reference_id = None
        if isinstance(payload, dict):
            reference_id = payload.get("reference_id") or payload.get("referenceId")
        ok = isinstance(payload, dict) and payload.get("status") == 200 and bool(reference_id)
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        jlog(
            "codecraft_submit_response",
            mode="regular",
            ok=ok,
            network=network,
            gig=gig,
            payload=payload,
        )
        return ok, payload, reference_id
    except requests.RequestException as e:
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}, None


def _codecraft_submit_bigtime(phone: str, gig: int, network: str):
    if not CODECRAFT_API_KEY:
        return False, {"success": False, "error": "CODECRAFT API key not configured", "http_status": 500}, None
    # CodeCraft now expects both "regular" and "bigtime" traffic on the same initiate endpoint.
    url = f"{CODECRAFT_BASE_URL.rstrip('/')}/initiate.php"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": CODECRAFT_API_KEY,
    }
    body = {"recipient_number": phone, "gig": str(gig), "network": network}
    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
    jlog(
        "codecraft_submit_request",
        mode="bigtime",
        network=network,
        gig=gig,
        phone=masked,
        url=url,
    )
    try:
        resp = requests.post(url, headers=headers, json=body, timeout=45)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}
        reference_id = None
        if isinstance(payload, dict):
            reference_id = payload.get("reference_id") or payload.get("referenceId")
        ok = isinstance(payload, dict) and payload.get("status") == 200 and bool(reference_id)
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)
        jlog(
            "codecraft_submit_response",
            mode="bigtime",
            ok=ok,
            network=network,
            gig=gig,
            payload=payload,
        )
        return ok, payload, reference_id
    except requests.RequestException as e:
        return False, {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}, None


def _send_dataconnect_order(
    phone: str,
    network_id: int,
    shared_bundle: int,
    external_ref: str,
    order_id: str,
    debug_events: list,
):
    """
    Sends a single bundle order to DataConnect.

    POST https://dataconnectgh.com/api/v1/buy-other-package

    Body JSON:
        {
            "recipient_msisdn": "0551053716",
            "network_id": 3,
            "shared_bundle": 1000
        }
    """
    if not DATACONNECT_API_KEY:
        err = {
            "success": False,
            "message": "DATACONNECT API key not configured",
            "http_status": 500,
        }
        jlog("dataconnect_config_error", order_id=order_id, ref=external_ref)
        return False, err

    url = f"{DATACONNECT_BASE_URL.rstrip('/')}/buy-other-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": DATACONNECT_API_KEY,
    }
    body = {
        "recipient_msisdn": phone,
        "network_id": int(network_id),
        "shared_bundle": int(shared_bundle),
    }

    masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"

    jlog(
        "dataconnect_request_body",
        order_id=order_id,
        ref=external_ref,
        url=url,
        body={
            "recipient_msisdn": masked,
            "network_id": body["network_id"],
            "shared_bundle": body["shared_bundle"],
        },
    )

    try:
        resp = requests.post(
            url,
            headers=headers,
            json=body,
            timeout=45,
        )
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            resp.status_code in (200, 201)
            and isinstance(payload, dict)
            and bool(payload.get("success")) is True
        )
        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        dbg = {
            "status": resp.status_code,
            "body_len": len(text),
        }
        jlog("dataconnect_response", order_id=order_id, ref=external_ref, payload=payload)
        jlog("dataconnect_call", order_id=order_id, ref=external_ref, ok=ok, debug=dbg)

        debug_events.append(
            {
                "when": datetime.utcnow(),
                "stage": "dataconnect-buy-other-package",
                "ok": ok,
                "http_status": resp.status_code,
            }
        )
        return ok, payload

    except requests.RequestException as e:
        jlog(
            "dataconnect_network_error",
            order_id=order_id,
            ref=external_ref,
            error=str(e),
        )
        return False, {
            "success": False,
            "error": str(e),
            "type": "NETWORK_ERROR",
            "http_status": 599,
        }


def _datakazina_submit_single(
    recipient_msisdn: str,
    shared_bundle: int,
    incoming_api_ref: str,
    meta: dict | None = None,
):
    """
    Sends a single DataKazina order to /buy-data-package.
    Success = HTTP 2xx + {"success": true}
    """
    cleaned_key = _clean_api_key(DATAKAZINA_API_KEY or "")
    if not cleaned_key:
        jlog("datakazina_error", stage="config", error="DATAKAZINA_API_KEY not configured")
        return {
            "ok": False,
            "http_status": 500,
            "provider": "datakazina",
            "provider_reference": None,
            "response": {"success": False, "error": "DATAKAZINA API key not configured"},
            "message": "DATAKAZINA API key not configured",
        }

    if not recipient_msisdn:
        jlog("datakazina_error", stage="validation", error="recipient_msisdn missing", ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 400,
            "provider": "datakazina",
            "provider_reference": None,
            "response": {"success": False, "error": "recipient_msisdn missing"},
            "message": "recipient_msisdn missing",
        }
    try:
        shared_bundle = int(shared_bundle)
    except Exception:
        jlog("datakazina_error", stage="validation", error="shared_bundle invalid", ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 400,
            "provider": "datakazina",
            "provider_reference": None,
            "response": {"success": False, "error": "shared_bundle invalid"},
            "message": "shared_bundle invalid",
        }

    url = f"{DATAKAZINA_BASE_URL.rstrip('/')}/buy-data-package"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-api-key": cleaned_key,
    }
    body = {
        "recipient_msisdn": recipient_msisdn,
        "network_id": 3,
        "shared_bundle": int(shared_bundle),
        "incoming_api_ref": incoming_api_ref,
    }

    masked = (
        recipient_msisdn[:3] + "***" + recipient_msisdn[-2:]
        if recipient_msisdn and len(recipient_msisdn) >= 5
        else "***"
    )
    jlog(
        "datakazina_request",
        ref=incoming_api_ref,
        phone=masked,
        shared_bundle=body["shared_bundle"],
        url=url,
        meta=meta or {},
    )

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=DATAKAZINA_TIMEOUT)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        ok = (
            200 <= resp.status_code < 300
            and isinstance(payload, dict)
            and payload.get("success") is True
        )
        provider_ref = payload.get("transaction_code") if isinstance(payload, dict) else None
        message = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")
            payload.setdefault("http_status", resp.status_code)

        jlog(
            "datakazina_response",
            ref=incoming_api_ref,
            ok=ok,
            http_status=resp.status_code,
            provider_reference=provider_ref,
            payload=payload,
        )

        return {
            "ok": ok,
            "http_status": resp.status_code,
            "provider": "datakazina",
            "provider_reference": provider_ref,
            "response": payload,
            "message": message or ("Success" if ok else "Request failed"),
        }
    except requests.RequestException as e:
        jlog("datakazina_error", ref=incoming_api_ref, error=str(e))
        return {
            "ok": False,
            "http_status": 599,
            "provider": "datakazina",
            "provider_reference": None,
            "response": {"success": False, "error": str(e), "type": "NETWORK_ERROR"},
            "message": str(e),
        }


def _datakazina_submit_many_as_single_orders(jobs: list[dict]):
    """
    Sequentially submit multiple DataKazina jobs as single requests.
    """
    results = []
    success_count = 0
    failed_count = 0
    for job in jobs or []:
        res = _datakazina_submit_single(
            recipient_msisdn=job.get("phone"),
            shared_bundle=job.get("shared_bundle"),
            incoming_api_ref=job.get("incoming_api_ref") or job.get("provider_request_order_id") or "",
            meta={"order_id": job.get("order_id"), "line_ref": job.get("provider_request_order_id")},
        )
        results.append(res)
        if res.get("ok"):
            success_count += 1
        else:
            failed_count += 1
    return {
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def _extract_reference_from_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    candidates = [
        payload.get("transaction_code"),
        payload.get("reference"),
        payload.get("order_reference"),
        payload.get("order_id"),
        payload.get("orderId"),
        payload.get("id"),
    ]

    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend(
            [
                data.get("transaction_code"),
                data.get("reference"),
                data.get("order_reference"),
                data.get("order_id"),
                data.get("orderId"),
                data.get("id"),
            ]
        )

    for val in candidates:
        if val not in (None, "", [], {}):
            return str(val)
    return None


def _skplug_submit_single(
    recipient: str,
    network: str,
    gb_size: int,
    incoming_api_ref: str,
    meta: dict | None = None,
):
    """
    Sends a single SKPlug order to /order/.
    Success = HTTP 2xx and no explicit {"success": false} marker.
    """
    cleaned_key = _clean_api_key(SKPLUG_API_KEY or "")
    if not cleaned_key:
        jlog("skplug_error", stage="config", error="SKPLUG_API_KEY not configured")
        return {
            "ok": False,
            "http_status": 500,
            "provider": "skplug",
            "provider_reference": None,
            "response": {"success": False, "error": "SKPLUG API key not configured"},
            "message": "SKPLUG API key not configured",
        }

    recipient = str(recipient or "").strip()
    network = str(network or "").strip().upper()
    try:
        gb_size = int(gb_size)
    except Exception:
        gb_size = 0

    if not recipient:
        jlog("skplug_error", stage="validation", error="recipient missing", ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 400,
            "provider": "skplug",
            "provider_reference": None,
            "response": {"success": False, "error": "recipient missing"},
            "message": "recipient missing",
        }

    if not network:
        jlog("skplug_error", stage="validation", error="network missing", ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 400,
            "provider": "skplug",
            "provider_reference": None,
            "response": {"success": False, "error": "network missing"},
            "message": "network missing",
        }

    if gb_size <= 0:
        jlog("skplug_error", stage="validation", error="gb_size invalid", ref=incoming_api_ref)
        return {
            "ok": False,
            "http_status": 400,
            "provider": "skplug",
            "provider_reference": None,
            "response": {"success": False, "error": "gb_size invalid"},
            "message": "gb_size invalid",
        }

    url = f"{SKPLUG_BASE_URL.rstrip('/')}/order/"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cleaned_key}",
    }
    body = {
        "recipient": recipient,
        "network": network,
        "gb_size": str(gb_size),
    }

    masked = recipient[:3] + "***" + recipient[-2:] if len(recipient) >= 5 else "***"
    jlog(
        "skplug_request",
        ref=incoming_api_ref,
        phone=masked,
        network=network,
        gb_size=gb_size,
        url=url,
        meta=meta or {},
    )

    try:
        resp = requests.post(url, headers=headers, json=body, timeout=SKPLUG_TIMEOUT)
        text = resp.text or ""
        try:
            payload = resp.json()
        except Exception:
            payload = {"raw": text} if text else {}

        if isinstance(payload, dict):
            payload.setdefault("http_status", resp.status_code)

        explicit_fail = isinstance(payload, dict) and payload.get("success") is False
        ok = bool(resp.ok) and not explicit_fail
        provider_ref = _extract_reference_from_payload(payload)
        message = None
        if isinstance(payload, dict):
            message = payload.get("message") or payload.get("error")

        jlog(
            "skplug_response",
            ref=incoming_api_ref,
            ok=ok,
            http_status=resp.status_code,
            provider_reference=provider_ref,
            payload=payload,
        )

        return {
            "ok": ok,
            "http_status": resp.status_code,
            "provider": "skplug",
            "provider_reference": provider_ref,
            "response": payload,
            "message": message or ("Success" if ok else "Request failed"),
        }
    except requests.RequestException as e:
        jlog("skplug_error", ref=incoming_api_ref, error=str(e))
        return {
            "ok": False,
            "http_status": 599,
            "provider": "skplug",
            "provider_reference": None,
            "response": {"success": False, "error": str(e), "type": "NETWORK_ERROR"},
            "message": str(e),
        }


# ===== Unavailability checker ================================================
def _service_unavailability_reason(svc_doc: dict):
    """
    Returns (is_unavailable, reason_text)
    """
    if not svc_doc:
        return True, "Closed"

    status = (svc_doc.get("status") or "").strip().upper()
    availability = (svc_doc.get("availability") or "").strip().upper()

    if availability in {"OUT_OF_STOCK", "OUT OF STOCK", "OUTOFSTOCK"}:
        return True, "Out of stock"

    if status == "CLOSED":
        return True, "Closed"

    return False, ""


# ===== Duplicate-in-processing guard =========================================
DUP_WINDOW_MINUTES = 30


def _normalize_amount_key(v):
    try:
        return float(f"{float(v):.2f}")
    except Exception:
        return 0.0


def _has_processing_conflict_strict(
    phone: str,
    service_id_raw: str | None,
    svc_name: str | None,
    network_id: int | None,
    bundle_key: tuple | None,
    amount_key: float,
) -> bool:
    if not phone or network_id is None or bundle_key is None:
        return False

    window_start = datetime.utcnow() - timedelta(minutes=DUP_WINDOW_MINUTES)
    kind, bval = bundle_key

    elem = {
        "phone": phone,
        "network_id": network_id,
        "bundle_key.kind": kind,
        "bundle_key.value": bval,
        "amount": amount_key,
    }
    if service_id_raw:
        elem["serviceId"] = service_id_raw

    q = {
        "status": {"$in": ["pending", "processing"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": elem},
    }
    if orders_col.find_one(q, {"_id": 1}):
        return True

    alt = {
        "phone": phone,
        "network_id": network_id,
        "amount": amount_key,
    }
    if kind == "offer":
        alt["value_obj.id"] = bval
    else:
        alt["value_obj.volume"] = bval
    if service_id_raw:
        alt["serviceId"] = service_id_raw

    q2 = {
        "status": {"$in": ["pending", "processing"]},
        "created_at": {"$gte": window_start},
        "items": {"$elemMatch": alt},
    }
    return bool(orders_col.find_one(q2, {"_id": 1}))


def _bundleportal_submit_single(recipient: str, package_size, network: str, order_id: str) -> dict:
    token = _clean_api_key(BUNDLEPORTAL_API_KEY)
    if not token:
        return {"ok": False, "response": {"success": False, "message": "BUNDLEPORTAL API key not configured"}, "provider_reference": None, "provider_order_id": None}
    network_slug = str(network or "").strip().lower()
    if network_slug == "ishare":
        network_slug = "airteltigo"
    body = {
        "action": "place_order", "network": network_slug,
        "recipient": str(recipient or "").strip(), "package_size": package_size,
        "order_id": str(order_id or "")[:80],
    }
    try:
        response = requests.post(
            BUNDLEPORTAL_BASE_URL.rstrip("/"),
            headers={"Accept": "application/json", "Content-Type": "application/json", "x-api-key": token},
            json=body, timeout=BUNDLEPORTAL_TIMEOUT,
        )
        try:
            payload = response.json()
        except Exception:
            payload = {"success": False, "message": response.text[:1000]}
        data = payload.get("data") if isinstance(payload, dict) else None
        data = data if isinstance(data, dict) else {}
        ok = bool(response.ok and isinstance(payload, dict) and payload.get("success") is True)
        provider_ref = data.get("reference") or data.get("order_id")
        return {"ok": ok, "response": payload, "provider_reference": provider_ref, "provider_order_id": data.get("order_id") or provider_ref}
    except Exception as exc:
        return {"ok": False, "response": {"success": False, "message": str(exc), "type": "BUNDLEPORTAL_EXCEPTION"}, "provider_reference": None, "provider_order_id": None}


# ===== BACKGROUND WORKER =====================================================
def _background_process_providers(order_id: str, api_jobs: list[dict]):
    """
    Runs in a separate thread AFTER the HTTP response is sent.
    It picks queued lines and calls DataConnect/CodeCraft, then updates the order doc.
    """
    jlog("checkout_bg_worker_start", order_id=order_id, jobs=len(api_jobs))
    local_debug = []

    for job in api_jobs:
        line_ref = job.get("provider_request_order_id")
        try:
            phone = job.get("phone")
            provider = job.get("provider")
            job_order_id = job.get("order_id") or order_id
            if not job_order_id:
                continue

            if provider == "bundleportal":
                provider_network = (job.get("provider_network") or "").strip().lower()
                provider_gig = job.get("provider_gig")
                result = _bundleportal_submit_single(phone, provider_gig, provider_network, line_ref)
                ok = bool(result.get("ok"))
                orders_col.update_one(
                    {"order_id": job_order_id, "items.provider_request_order_id": line_ref},
                    {"$set": {
                        "items.$.api_status": "success" if ok else "failed",
                        "items.$.line_status": "processing" if ok else "failed",
                        "items.$.api_response": result.get("response"),
                        "items.$.provider_reference": result.get("provider_reference"),
                        "items.$.provider_order_id": result.get("provider_order_id"),
                        "items.$.provider": "bundleportal", "items.$.provider_network": provider_network,
                        "items.$.provider_gig": provider_gig, "status": "processing" if ok else "failed",
                        "updated_at": datetime.utcnow(),
                    }},
                )
                continue

            if provider == "dataconnect":
                dataconnect_network_id = job.get("network_id")
                dataconnect_shared_bundle = job.get("shared_bundle")

                ok, payload = _send_dataconnect_order(
                    phone=phone,
                    network_id=dataconnect_network_id,
                    shared_bundle=dataconnect_shared_bundle,
                    external_ref=line_ref,
                    order_id=job_order_id,
                    debug_events=local_debug,
                )

                provider_ref = None
                provider_order_id = None
                if isinstance(payload, dict):
                    provider_ref = (
                        payload.get("transaction_code")
                        or payload.get("reference")
                        or payload.get("order_reference")
                    )
                    provider_order_id = (
                        payload.get("orderId")
                        or payload.get("order_id")
                        or payload.get("transaction_code")
                    )

                # Update this specific line inside the order items
                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_order_id,
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "datakazina":
                datakazina_shared_bundle = job.get("shared_bundle")
                incoming_api_ref = job.get("incoming_api_ref") or line_ref or ""
                result = _datakazina_submit_single(
                    recipient_msisdn=phone,
                    shared_bundle=datakazina_shared_bundle,
                    incoming_api_ref=incoming_api_ref,
                    meta={"order_id": job_order_id, "line_ref": line_ref},
                )

                ok = bool(result.get("ok"))
                payload = result.get("response") if isinstance(result, dict) else {}
                provider_ref = result.get("provider_reference") if isinstance(result, dict) else None

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_ref,
                            "items.$.provider": "datakazina",
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "codecraft":
                provider_network = job.get("provider_network")
                provider_gig = job.get("provider_gig")
                provider_mode = job.get("provider_mode")
                provider_amount = job.get("provider_amount")

                if provider_network == "TELECEL" and provider_mode == "bigtime":
                    provider_mode = "regular"

                if provider_mode == "bigtime":
                    ok, payload, reference_id = _codecraft_submit_bigtime(
                        phone=phone,
                        gig=provider_gig,
                        network=provider_network,
                    )
                else:
                    ok, payload, reference_id = _codecraft_submit_regular(
                        phone=phone,
                        gig=provider_gig,
                        network=provider_network,
                    )

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "processing",
                            "items.$.line_status": "processing",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": reference_id,
                            "items.$.provider_order_id": reference_id,
                            "items.$.provider_mode": provider_mode,
                            "items.$.provider_network": provider_network,
                            "items.$.provider_gig": provider_gig,
                            "items.$.provider_package_amount": provider_amount,
                            "items.$.provider": "codecraft",
                            "status": "processing",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "skplug":
                provider_network = job.get("provider_network")
                provider_gb_size = job.get("provider_gb_size")
                incoming_api_ref = job.get("incoming_api_ref") or line_ref or ""
                result = _skplug_submit_single(
                    recipient=phone,
                    network=provider_network,
                    gb_size=provider_gb_size,
                    incoming_api_ref=incoming_api_ref,
                    meta={"order_id": job_order_id, "line_ref": line_ref},
                )

                ok = bool(result.get("ok"))
                payload = result.get("response") if isinstance(result, dict) else {}
                provider_ref = result.get("provider_reference") if isinstance(result, dict) else None

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "failed",
                            "items.$.line_status": "processing" if ok else "failed",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_ref,
                            "items.$.provider": "skplug",
                            "items.$.provider_network": provider_network,
                            "items.$.provider_gb_size": provider_gb_size,
                            "status": "processing" if ok else "failed",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue

            if provider == "portal02":
                if not PORTAL02_API_KEY:
                    ok = False
                    payload = {"success": False, "error": "PORTAL02 API key not configured", "http_status": 500}
                else:
                    network_slug = (job.get("portal02_network_slug") or "mtn").strip().lower()
                    offer_slug = job.get("portal02_offer_slug") or PORTAL02_OFFER_SLUG_MTN_NORMAL
                    package_size_gb = job.get("package_size_gb")
                    norm_phone = _normalize_portal02_phone(phone)

                    url = f"{PORTAL02_BASE_URL.rstrip('/')}/order/{network_slug}"
                    headers = {
                        "x-api-key": PORTAL02_API_KEY,
                        "Content-Type": "application/json",
                    }
                    body = {
                        "type": "single",
                        "volume": int(package_size_gb) if package_size_gb is not None else None,
                        "phone": norm_phone,
                        "offerSlug": offer_slug,
                        "webhookUrl": PORTAL02_WEBHOOK_URL,
                    }

                    try:
                        resp = requests.post(url, headers=headers, json=body, timeout=45)
                        text = resp.text or ""
                        try:
                            payload = resp.json()
                        except Exception:
                            payload = {"raw": text} if text else {}
                        if isinstance(payload, dict):
                            payload.setdefault("http_status", resp.status_code)
                        ok = bool(resp.ok)
                    except requests.RequestException as e:
                        ok = False
                        payload = {"success": False, "error": str(e), "type": "NETWORK_ERROR", "http_status": 599}

                provider_ref = None
                provider_order_id = None
                if isinstance(payload, dict):
                    provider_ref = payload.get("reference") or payload.get("transaction_code")
                    provider_order_id = (
                        payload.get("orderId")
                        or payload.get("order_id")
                        or payload.get("transaction_code")
                        or payload.get("reference")
                    )

                orders_col.update_one(
                    {
                        "order_id": job_order_id,
                        "items.provider_request_order_id": line_ref,
                    },
                    {
                        "$set": {
                            "items.$.api_status": "success" if ok else "failed",
                            "items.$.line_status": "processing" if ok else "failed",
                            "items.$.api_response": payload,
                            "items.$.provider_reference": provider_ref,
                            "items.$.provider_order_id": provider_order_id,
                            "items.$.provider": "portal02",
                            "status": "processing" if ok else "failed",
                            "updated_at": datetime.utcnow(),
                        }
                    },
                )
                continue
            else:
                jlog("provider_skipped", order_id=job_order_id, ref=line_ref, provider=provider)
                api_status = "not_applicable_unknown_provider"
                api_note = "Unknown provider; queued for manual processing."

            orders_col.update_one(
                {
                    "order_id": job_order_id,
                    "items.provider_request_order_id": line_ref,
                },
                {
                    "$set": {
                        "items.$.api_status": api_status,
                        "items.$.line_status": "processing",
                        "items.$.api_response": {"note": api_note},
                        "status": "processing",
                        "updated_at": datetime.utcnow(),
                    }
                },
            )
        except Exception as e:
            jlog("checkout_bg_worker_line_error", order_id=job_order_id, error=str(e))
            if line_ref:
                try:
                    provider = job.get("provider")
                    err_type = "CODECRAFT_EXCEPTION" if provider == "codecraft" else "PROVIDER_EXCEPTION"
                    orders_col.update_one(
                        {
                            "order_id": job_order_id,
                            "items.provider_request_order_id": line_ref,
                        },
                        {
                            "$set": {
                                "items.$.api_status": "failed",
                                "items.$.line_status": "failed",
                                "items.$.api_response": {"error": str(e), "type": err_type},
                                "status": "failed",
                                "updated_at": datetime.utcnow(),
                            }
                        },
                    )
                except Exception:
                    pass

    if local_debug:
        # append debug entries
        try:
            orders_col.update_one(
                {"order_id": order_id},
                {"$push": {"debug.events": {"$each": local_debug}}},
            )
        except Exception:
            pass

    jlog("checkout_bg_worker_end", order_id=order_id, jobs=len(api_jobs))


# ===== Route (FAST RESPONSE, PROVIDERS IN BACKGROUND) ========================
@checkout_bp.route("/checkout", methods=["POST"])
def process_checkout():
    try:
        # Auth
        if "user_id" not in session or session.get("role") != "customer":
            jlog("checkout_auth_fail", session_keys=list(session.keys()))
            return jsonify({"success": False, "message": "Not authorized"}), 401

        try:
            user_id = ObjectId(session["user_id"])
        except Exception:
            return jsonify({"success": False, "message": "Invalid user ID"}), 400

        data = request.get_json(silent=True) or {}
        cart = data.get("cart", [])
        method = data.get("method", "wallet")
        jlog("checkout_incoming", payload=data)

        if not cart or not isinstance(cart, list):
            return jsonify({"success": False, "message": "Cart is empty or invalid"}), 400
        blocked = first_blocked_phone([it.get("phone") for it in cart if isinstance(it, dict)])
        if blocked:
            return jsonify({"success": False, "message": blocked["message"] or BLOCK_NEW_NUMBERS_MESSAGE, "phone": blocked["phone"]}), 400

        # Total requested (customer-facing)
        total_requested = sum(_money(item.get("amount")) for item in cart)
        if total_requested <= 0:
            return jsonify({"success": False, "message": "Total amount must be greater than zero"}), 400

        client_request_id = (data.get("client_request_id") or "").strip()
        if client_request_id:
            existing = orders_col.find_one(
                {"user_id": user_id, "client_request_id": client_request_id},
                {"order_id": 1, "batch_id": 1, "status": 1, "charged_amount": 1, "profit_amount_total": 1, "items": 1},
            )
            if existing:
                existing_status = existing.get("status") or "pending"
                existing_batch_id = existing.get("batch_id") or existing.get("order_id")
                existing_orders = list(orders_col.find(
                    {"user_id": user_id, "batch_id": existing_batch_id},
                    {"order_id": 1, "charged_amount": 1, "profit_amount_total": 1, "items": 1},
                ).sort("batch_position", 1)) if existing.get("batch_id") else [existing]
                existing_order_ids = [doc.get("order_id") for doc in existing_orders if doc.get("order_id")]
                return (
                    jsonify(
                        {
                            "success": True,
                            "message": "Order already received.",
                            "order_id": existing.get("order_id"),
                            "order_ids": existing_order_ids,
                            "batch_id": existing_batch_id,
                            "redirect_url": f"/invoice-batch/{existing_batch_id}",
                            "status": existing_status,
                            "charged_amount": sum(_money(doc.get("charged_amount")) for doc in existing_orders),
                            "profit_amount_total": sum(_money(doc.get("profit_amount_total")) for doc in existing_orders),
                            "items": [item for doc in existing_orders for item in (doc.get("items") or [])],
                        }
                    ),
                    200,
                )

        order_id = generate_order_id()

        # Balance check
        bal_doc = balances_col.find_one({"user_id": user_id}) or {}
        current_balance = _money(bal_doc.get("amount", 0))
        jlog("checkout_balance", order_id=order_id, balance=current_balance, total=total_requested)
        if current_balance < total_requested:
            return jsonify({"success": False, "message": "❌ Insufficient wallet balance"}), 400

        results = []
        debug_events = []

        total_delivered_api_amount = 0.0  # stays 0.0 (we don't mark delivered immediately)
        total_processing_amount = 0.0
        api_requested_total = 0.0
        has_processing = False
        profit_amount_total = 0.0

        seen_keys = set()
        api_jobs = []  # lines to be sent to providers in the background worker
        codecraft_regular_map = None
        codecraft_bigtime_map = None
        successful_provider_debits = []

        for idx, item in enumerate(cart, start=1):
            phone = (item.get("phone") or "").strip()
            value_obj = _coerce_value_obj(item.get("value_obj") or item.get("value"))
            amt_total = _money(item.get("amount"))
            amount_key = _normalize_amount_key(amt_total)
            ported_fields = _extract_ported_fields(item)

            service_id_raw = item.get("serviceId")
            svc_doc = None
            svc_type = None
            svc_name = item.get("serviceName") or None
            svc_provider = ""

            if service_id_raw:
                try:
                    svc_doc = services_col.find_one(
                        {"_id": ObjectId(service_id_raw)},
                        {
                            "type": 1,
                            "network_id": 1,
                            "name": 1,
                            "network": 1,
                            "offers": 1,
                            "provider": 1,
                            "default_profit_percent": 1,
                            "service_category": 1,
                            "status": 1,
                            "availability": 1,
                            "service_network": 1,
                        },
                    )
                    if svc_doc:
                        st = svc_doc.get("type")
                        svc_type = (st.strip().upper() if isinstance(st, str) else st)
                        svc_name = svc_doc.get("name") or svc_doc.get("network") or svc_name
                except Exception:
                    svc_doc = None
                    svc_type = None

            if svc_doc and svc_doc.get("provider"):
                svc_provider = str(svc_doc.get("provider") or "").strip().lower()
            elif item.get("provider"):
                svc_provider = str(item.get("provider") or "").strip().lower()

            # HARD GATE: availability
            is_unavail, reason_text = _service_unavailability_reason(svc_doc)
            if is_unavail:
                return jsonify(
                    {
                        "success": False,
                        "message": reason_text,
                        "unavailable": {
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "reason": reason_text,
                        },
                    }
                ), 400

            # Duplicate guards
            network_id = _resolve_network_id(item, value_obj, svc_doc)
            bundle_key = _build_bundle_key(value_obj, item)

            if phone and (network_id is not None) and (bundle_key is not None):
                cart_key = (phone, int(network_id), bundle_key[1], bundle_key[0], amount_key)
                if cart_key in seen_keys:
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        **ported_fields,
                        "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                            "network_id": network_id,
                            "bundle_key": {"kind": bundle_key[0], "value": bundle_key[1]},
                            "line_amount_key": amount_key,
                            "line_status": "skipped_duplicate_in_cart",
                            "api_status": "skipped",
                            "api_response": {
                                "note": "Duplicate line in this cart (same number, network, bundle, amount)"
                            },
                        }
                    )
                    continue
                seen_keys.add(cart_key)

            is_dup_strict = _has_processing_conflict_strict(
                phone, service_id_raw, svc_name, network_id, bundle_key, amount_key
            )
            if is_dup_strict:
                results.append(
                    {
                        "phone": phone,
                        "base_amount": 0.0,
                        "amount": 0.0,
                        "originally_requested_amount": amt_total,
                        "profit_amount": 0.0,
                        "profit_percent_used": 0.0,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else ("unknown" if not svc_doc else None),
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "skipped_duplicate_processing",
                        "api_status": "skipped",
                        "api_response": {
                            "note": "Same number + same network + same bundle + same amount already processing; skipping."
                        },
                    }
                )
                continue

            # Provider wallet debit (based on service.provider + system offers)
            provider_name = ""
            if svc_doc and svc_doc.get("provider"):
                provider_name = str(svc_doc.get("provider") or "").strip().lower()

            if provider_name in ("codecraft", "portal02", "dataconnect", "datakazina", "skplug", "bundleportal"):
                provider_cost = compute_provider_cost(svc_doc or {}, item.get("value") or value_obj)
                if provider_cost is not None and provider_cost > 0:
                    ok, _msg, _bal = debit_provider(
                        provider_name,
                        provider_cost,
                        order_id,
                        idx,
                        meta={
                            "service_id": str(service_id_raw or ""),
                            "service_name": svc_name,
                            "value": item.get("value"),
                        },
                    )
                    if ok:
                        successful_provider_debits.append((provider_name, provider_cost, idx))
                    else:
                        for p, amt, line_idx in successful_provider_debits:
                            credit_provider(
                                p,
                                amt,
                                order_id=order_id,
                                line_index=line_idx,
                                reason="ROLLBACK",
                                meta={"failed_provider": provider_name, "failed_line_index": idx},
                            )
                        return (
                            jsonify(
                                {
                                    "success": False,
                                    "message": "Insufficient Provider Wallet balance. Please fund Provider Wallet.",
                                }
                            ),
                            400,
                        )

            # base & profit (requested): profit = amount - base_amount
            base_hint = _to_float(item.get("base_amount"))
            base_amount = round(float(base_hint if base_hint is not None else 0.0), 2)
            profit_amount = max(0.0, round(amt_total - base_amount, 2))
            profit_percent_used = round((profit_amount / base_amount) * 100.0, 2) if base_amount > 0 else 0.0
            profit_amount_total += profit_amount

            svc_name_norm = (svc_name or "").strip().lower()
            is_mtn_normal = (svc_name_norm == "mtn normal") or _is_mtn_normal_service(service_id_raw, svc_doc)
            is_mtn_express = (svc_name_norm == "mtn express")
            is_mtn_portal_service = bool(is_mtn_normal or is_mtn_express)

            if str(item.get("provider") or "").strip().lower() == "portal02" and not is_mtn_portal_service:
                jlog(
                    "portal02" + "_blocked",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else "unknown",
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_portal_blocked",
                        "api_response": {"note": "Portal provider disabled; queued for manual processing."},
                    }
                )
                continue

            # No service doc → manual processing
            if not svc_doc:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type if svc_type else "unknown",
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable",
                        "api_response": {"note": "Service not found; queued for processing"},
                    }
                )
                continue

            # Provider selection
            resolved_network = _resolve_dataconnect_network(svc_doc, item)

            svc_type_flag = (svc_type or "").strip().upper() if isinstance(svc_type, str) else ""
            type_allows_api = svc_type_flag in ("ON", "API")
            api_allowed = type_allows_api
            if svc_type_flag == "OFF":
                api_allowed = False

            # MTN provider selection (prefer provider field for MTN services)
            is_mtn_service = is_mtn_portal_service
            mtn_provider = None
            if is_mtn_service:
                mtn_provider = (svc_provider or "").strip().lower()
                if mtn_provider not in {"portal02", "dataconnect", "codecraft", "datakazina", "skplug", "bundleportal"}:
                    mtn_provider = None
                # Preserve existing MTN NORMAL default
                if is_mtn_normal and mtn_provider is None:
                    mtn_provider = "portal02"

            use_portal02 = bool(is_mtn_portal_service and mtn_provider == "portal02" and api_allowed)
            use_skplug = bool(
                api_allowed
                and (
                    (is_mtn_service and mtn_provider == "skplug")
                    or (not is_mtn_service and svc_provider == "skplug")
                )
            )
            skplug_network = _resolve_skplug_network_name(svc_doc, item) if use_skplug else None

            use_codecraft = bool(
                api_allowed
                and (
                    (is_mtn_service and mtn_provider == "codecraft")
                    or (not is_mtn_service and svc_provider == "codecraft")
                )
            )
            codecraft_network = _resolve_codecraft_network_name(svc_doc, item) if use_codecraft else None

            use_datakazina = bool(api_allowed and is_mtn_service and mtn_provider == "datakazina")
            use_bundleportal = bool(api_allowed and ((is_mtn_service and mtn_provider == "bundleportal") or (not is_mtn_service and svc_provider == "bundleportal")))

            # DataConnect: MTN Express rule unchanged + MTN NORMAL override
            use_dataconnect_express = (
                resolved_network == "mtn"
                and is_mtn_express
                and api_allowed
                and not use_codecraft
                and not use_datakazina
                and not use_skplug
                and not use_bundleportal
            )
            use_dataconnect_mtn_normal = (is_mtn_normal and mtn_provider == "dataconnect" and api_allowed)
            use_dataconnect = (use_dataconnect_express or use_dataconnect_mtn_normal) and not use_codecraft and not use_skplug and not use_bundleportal

            jlog(
                "checkout_line_routing",
                order_id=order_id,
                idx=idx,
                serviceId=service_id_raw,
                svc_name=svc_name,
                resolved_network=resolved_network,
                svc_type_flag=svc_type_flag,
                is_mtn_express=is_mtn_express,
                is_mtn_normal=is_mtn_normal,
                is_mtn_portal_service=is_mtn_portal_service,
                is_mtn_service=is_mtn_service,
                mtn_provider=mtn_provider,
                api_allowed=api_allowed,
                use_portal02=use_portal02,
                use_skplug=use_skplug,
                use_dataconnect=use_dataconnect,
                use_datakazina=use_datakazina,
                use_bundleportal=use_bundleportal,
                svc_provider=svc_provider,
                use_codecraft=use_codecraft,
                codecraft_network=codecraft_network,
                skplug_network=skplug_network,
            )

            if use_datakazina:
                jlog(
                    "datakazina_routing_selected",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider="datakazina",
                    resolved_network=resolved_network,
                )

            # HARD GATE: never call any provider if service type is OFF
            if not api_allowed:
                jlog(
                    "api_gate_blocked_type_off",
                    order_id=order_id,
                    idx=idx,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                    provider=svc_provider,
                    svc_type_flag=svc_type_flag,
                )
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "not_applicable_type_off",
                        "api_response": {
                            "note": "API calls disabled for this service (type OFF); queued for manual processing."
                        },
                    }
                )
                continue

            if not use_dataconnect and not use_datakazina and not use_codecraft and not use_portal02 and not use_skplug and not use_bundleportal:
                has_processing = True
                total_processing_amount += amt_total

                if not api_allowed:
                    note = (
                        "API calls disabled for this service (type OFF); queued for manual processing."
                    )
                    api_status = "not_applicable_type_off"
                else:
                    note = (
                        "API is only used for MTN Portal (Portal-02), MTN EXPRESS (DataConnect/DataKazina), and CodeCraft services; queued for manual processing."
                    )
                    api_status = "not_applicable_network"

                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": api_status,
                        "api_response": {
                            "note": note,
                            "resolved_network": resolved_network,
                            "serviceName": svc_name,
                            "service_type_flag": svc_type_flag,
                        },
                    }
                )
                continue

            if use_portal02:
                api_requested_total += amt_total

                package_size_gb = _resolve_package_size_gb(value_obj, item)

                if not phone or package_size_gb is None:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "package_size_gb": package_size_gb,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "portal02",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "portal02",
                    "portal02_network_slug": "mtn",
                    "package_size_gb": package_size_gb,
                    "portal02_offer_slug": PORTAL02_OFFER_SLUG_MTN_NORMAL,
                    "service_id": svc_doc["_id"],
                    "raw_item": item,
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_bundleportal:
                api_requested_total += amt_total
                package_size_gb = _resolve_package_size_gb(value_obj, item)
                portal_network = (_resolve_bundleportal_network(svc_doc, item) or "").strip().lower()
                if portal_network == "vodafone": portal_network = "telecel"
                if portal_network in {"airtel", "tigo", "at"}: portal_network = "airteltigo"
                if not phone or package_size_gb is None or portal_network not in {"mtn", "telecel", "airteltigo", "ishare"}:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append({"phone": phone, "base_amount": base_amount, "amount": amt_total, "profit_amount": profit_amount, "profit_percent_used": profit_percent_used, **ported_fields, "value": item.get("value"), "value_obj": value_obj, "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type, "network_id": network_id, "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None), "line_amount_key": amount_key, "line_status": "processing", "api_status": "skipped_missing_fields", "api_response": {"note": "BundlePortal fields missing or network unsupported; queued for processing"}})
                    continue
                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"[:80]
                has_processing = True
                total_processing_amount += amt_total
                results.append({"phone": phone, "base_amount": base_amount, "amount": amt_total, "profit_amount": profit_amount, "profit_percent_used": profit_percent_used, **ported_fields, "value": item.get("value"), "value_obj": value_obj, "serviceId": service_id_raw, "serviceName": svc_name, "service_type": svc_type, "provider": "bundleportal", "provider_reference": None, "provider_order_id": None, "provider_request_order_id": external_ref, "provider_network": portal_network, "provider_gig": package_size_gb, "network_id": network_id, "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None), "line_amount_key": amount_key, "line_status": "pending", "api_status": "queued", "api_response": {"note": "Queued for background BundlePortal API call"}})
                api_jobs.append({"provider_request_order_id": external_ref, "phone": phone, "provider": "bundleportal", "provider_network": portal_network, "provider_gig": package_size_gb, "service_id": svc_doc["_id"], "line_index": idx})
                continue

            if use_skplug:
                api_requested_total += amt_total

                provider_gb_size = _resolve_package_size_gb(value_obj, item)

                if not phone or provider_gb_size is None or not skplug_network:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing for SKPlug; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "provider_network": skplug_network,
                                    "provider_gb_size": provider_gb_size,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "skplug",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_network": skplug_network,
                    "provider_gb_size": provider_gb_size,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                jlog(
                    "skplug_request_prepared",
                    order_id=order_id,
                    idx=idx,
                    ref=external_ref,
                    network=skplug_network,
                    provider_gb_size=provider_gb_size,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "incoming_api_ref": external_ref,
                    "phone": phone,
                    "provider": "skplug",
                    "provider_network": skplug_network,
                    "provider_gb_size": provider_gb_size,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_codecraft:
                api_requested_total += amt_total

                volume_mb = None
                provider_gig = None
                if isinstance(value_obj, dict):
                    vol_raw = value_obj.get("volume")
                    if vol_raw not in (None, "", []):
                        try:
                            vol_str = str(vol_raw).strip().lower()
                            vol_num = float(re.findall(r"(\d+(?:\.\d+)?)", vol_str)[0])
                            if "gb" in vol_str:
                                provider_gig = max(1, int(round(vol_num)))
                                volume_mb = int(round(vol_num * 1000.0))
                            elif "mb" in vol_str:
                                volume_mb = int(round(vol_num))
                                provider_gig = max(1, int(round(vol_num / 1000.0)))
                            else:
                                if vol_num >= 100:
                                    volume_mb = int(round(vol_num))
                                    provider_gig = max(1, int(round(vol_num / 1000.0)))
                                else:
                                    provider_gig = max(1, int(round(vol_num)))
                                    volume_mb = int(round(vol_num * 1000.0))
                        except Exception:
                            volume_mb = None
                            provider_gig = None
                if volume_mb is None or provider_gig is None:
                    gb_fallback = _resolve_package_size_gb(value_obj, item)
                    if gb_fallback is not None:
                        volume_mb = int(gb_fallback * 1000)
                        provider_gig = max(1, int(round(float(gb_fallback))))

                if not phone or not provider_gig or not codecraft_network:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "provider_network": codecraft_network,
                                    "provider_gig": provider_gig,
                                },
                            },
                        }
                    )
                    continue

                if codecraft_regular_map is None or codecraft_bigtime_map is None:
                    codecraft_regular_map, codecraft_bigtime_map = _codecraft_get_packages_cached()

                key = (codecraft_network, provider_gig)
                alt_keys = []
                if volume_mb:
                    try:
                        alt_keys.append((codecraft_network, max(1, int(round(volume_mb / 1000.0)))))
                        alt_keys.append((codecraft_network, max(1, int(round(volume_mb / 1024.0)))))
                    except Exception:
                        pass
                # de-dup while preserving order
                seen = set([key])
                alt_keys = [k for k in alt_keys if k not in seen and not seen.add(k)]

                jlog(
                    "codecraft_package_lookup",
                    order_id=order_id,
                    idx=idx,
                    codecraft_network=codecraft_network,
                    provider_gig=provider_gig,
                    volume_mb=volume_mb,
                    lookup_key=key,
                    alt_keys=alt_keys,
                )

                provider_mode = None
                provider_amount = None
                used_key = None
                fallback_used = False

                def _select_for_key(k):
                    nonlocal provider_mode, provider_amount, used_key
                    if codecraft_network == "TELECEL":
                        if codecraft_regular_map and k in codecraft_regular_map:
                            provider_mode = "regular"
                            provider_amount = codecraft_regular_map.get(k)
                            used_key = k
                            return True
                    elif codecraft_network == "MTN":
                        if codecraft_regular_map and k in codecraft_regular_map:
                            provider_mode = "regular"
                            provider_amount = codecraft_regular_map.get(k)
                            used_key = k
                            return True
                        if codecraft_bigtime_map and k in codecraft_bigtime_map:
                            provider_mode = "bigtime"
                            provider_amount = codecraft_bigtime_map.get(k)
                            used_key = k
                            return True
                    else:
                        if codecraft_bigtime_map and k in codecraft_bigtime_map:
                            provider_mode = "bigtime"
                            provider_amount = codecraft_bigtime_map.get(k)
                            used_key = k
                            return True
                        if codecraft_regular_map and k in codecraft_regular_map:
                            provider_mode = "regular"
                            provider_amount = codecraft_regular_map.get(k)
                            used_key = k
                            return True
                    return False

                _select_for_key(key)
                if not provider_mode:
                    for k in alt_keys:
                        if _select_for_key(k):
                            break

                mtn_prefer_regular = bool(is_mtn_service and codecraft_network == "MTN")
                if not provider_mode and mtn_prefer_regular:
                    provider_mode = "regular"
                    fallback_used = True

                jlog(
                    "codecraft_mode_selected",
                    order_id=order_id,
                    idx=idx,
                    codecraft_network=codecraft_network,
                    provider_mode=provider_mode,
                    provider_amount=provider_amount,
                    lookup_key=key,
                    used_key=used_key,
                    alt_keys=alt_keys,
                    provider_gig=provider_gig,
                    volume_mb=volume_mb,
                    fallback_used=fallback_used,
                    mtn_prefer_regular=mtn_prefer_regular,
                )

                if not provider_mode:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_package_not_found",
                            "api_response": {
                                "note": "Package not found in CodeCraft; queued for processing",
                                "provider_network": codecraft_network,
                                "provider_gig": provider_gig,
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "codecraft",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "provider_mode": provider_mode,
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_package_amount": provider_amount,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                }

                results.append(line_record)

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "phone": phone,
                    "provider": "codecraft",
                    "provider_network": codecraft_network,
                    "provider_gig": provider_gig,
                    "provider_mode": provider_mode,
                    "provider_amount": provider_amount,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if use_datakazina:
                api_requested_total += amt_total

                shared_bundle = _resolve_datakazina_shared_bundle(value_obj, item)

                if not phone or not shared_bundle:
                    has_processing = True
                    total_processing_amount += amt_total
                    results.append(
                        {
                            "phone": phone,
                            "base_amount": base_amount,
                            "amount": amt_total,
                            "profit_amount": profit_amount,
                            "profit_percent_used": profit_percent_used,
                            **ported_fields,
                            "value": item.get("value"),
                            "value_obj": value_obj,
                            "serviceId": service_id_raw,
                            "serviceName": svc_name,
                            "service_type": svc_type,
                            "network_id": network_id,
                            "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                            "line_amount_key": amount_key,
                            "line_status": "processing",
                            "api_status": "skipped_missing_fields",
                            "api_response": {
                                "note": "API fields missing for DataKazina; queued for processing",
                                "got": {
                                    "phone": bool(phone),
                                    "shared_bundle": shared_bundle,
                                },
                            },
                        }
                    )
                    continue

                external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

                has_processing = True
                total_processing_amount += amt_total

                line_record = {
                    "phone": phone,
                    "base_amount": base_amount,
                    "amount": amt_total,
                    "profit_amount": profit_amount,
                    "profit_percent_used": profit_percent_used,
                    **ported_fields,
                    "value": item.get("value"),
                    "value_obj": value_obj,
                    "serviceId": service_id_raw,
                    "serviceName": svc_name,
                    "service_type": svc_type,
                    "provider": "datakazina",
                    "provider_reference": None,
                    "provider_order_id": None,
                    "provider_request_order_id": external_ref,
                    "network_id": network_id,
                    "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                    "line_amount_key": amount_key,
                    "line_status": "pending",
                    "api_status": "queued",
                    "api_response": {"note": "Queued for background API call"},
                    "shared_bundle": shared_bundle,
                }

                results.append(line_record)

                masked = phone[:3] + "***" + phone[-2:] if phone and len(phone) >= 5 else "***"
                jlog(
                    "datakazina_request_prepared",
                    order_id=order_id,
                    idx=idx,
                    ref=external_ref,
                    phone=masked,
                    shared_bundle=shared_bundle,
                    serviceId=service_id_raw,
                    serviceName=svc_name,
                )

                job_payload = {
                    "provider_request_order_id": external_ref,
                    "incoming_api_ref": external_ref,
                    "phone": phone,
                    "provider": "datakazina",
                    "network_id": 3,
                    "shared_bundle": shared_bundle,
                    "service_id": svc_doc["_id"],
                    "line_index": idx,
                }

                api_jobs.append(job_payload)
                continue

            if not use_dataconnect:
                continue

            # From here: API-eligible line → we will send it via BACKGROUND worker
            api_requested_total += amt_total

            package_size_gb = _resolve_package_size_gb(value_obj, item)

            # Resolve shared_bundle for DataConnect from your stored offer structure
            shared_bundle = None
            if isinstance(value_obj, dict):
                sb = value_obj.get("volume") or value_obj.get("shared_bundle") or value_obj.get("mb")
                if sb not in (None, "", []):
                    try:
                        shared_bundle = int(float(sb))
                    except Exception:
                        shared_bundle = None
            if shared_bundle is None and package_size_gb is not None:
                shared_bundle = int(package_size_gb * 1000)

            if not phone or package_size_gb is None:
                has_processing = True
                total_processing_amount += amt_total
                results.append(
                    {
                        "phone": phone,
                        "base_amount": base_amount,
                        "amount": amt_total,
                        "profit_amount": profit_amount,
                        "profit_percent_used": profit_percent_used,
                        **ported_fields,
                        "value": item.get("value"),
                        "value_obj": value_obj,
                        "serviceId": service_id_raw,
                        "serviceName": svc_name,
                        "service_type": svc_type,
                        "network_id": network_id,
                        "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                        "line_amount_key": amount_key,
                        "line_status": "processing",
                        "api_status": "skipped_missing_fields",
                        "api_response": {
                            "note": "API fields missing; queued for processing",
                            "got": {
                                "phone": bool(phone),
                                "resolved_network": resolved_network,
                                "package_size_gb": package_size_gb,
                            },
                        },
                    }
                )
                continue

            # Prepare background job meta
            external_ref = f"{order_id}_{idx}_{uuid.uuid4().hex[:6]}"

            provider_name = "dataconnect"

            has_processing = True
            total_processing_amount += amt_total

            # store line with "queued" status; background worker will update
            line_record = {
                "phone": phone,
                "base_amount": base_amount,
                "amount": amt_total,
                "profit_amount": profit_amount,
                "profit_percent_used": profit_percent_used,
                **ported_fields,
                "value": item.get("value"),
                "value_obj": value_obj,
                "serviceId": service_id_raw,
                "serviceName": svc_name,
                "service_type": svc_type,
                "provider": provider_name,
                "provider_reference": None,
                "provider_order_id": None,
                "provider_request_order_id": external_ref,
                "network_id": network_id,
                "bundle_key": ({"kind": bundle_key[0], "value": bundle_key[1]} if bundle_key else None),
                "line_amount_key": amount_key,
                "line_status": "pending",
                "api_status": "queued",      # <--- queued for background call
                "api_response": {"note": "Queued for background API call"},
            }

            # For transparency/debug you can store shared_bundle on the line as well
            if use_dataconnect:
                line_record["shared_bundle"] = shared_bundle

            results.append(line_record)

            job_payload = {
                "provider_request_order_id": external_ref,
                "phone": phone,
                "provider": provider_name,
                "service_id": svc_doc["_id"],
                "line_index": idx,
            }

            if provider_name == "dataconnect":
                job_payload["network_id"] = network_id
                job_payload["shared_bundle"] = shared_bundle

            api_jobs.append(job_payload)

        if len(debug_events) > 10:
            debug_events = debug_events[-10:]

        total_to_charge_now = round(total_delivered_api_amount + total_processing_amount, 2)

        # If nothing to charge (all skipped)
        if total_to_charge_now <= 0:
            created_now = datetime.utcnow()

            order_doc = {
                "user_id": user_id,
                "order_id": order_id,
                "items": results,
                "total_amount": 0.0,
                "charged_amount": 0.0,
                "profit_amount_total": 0.0,
                "status": "skipped",
                "paid_from": method,
                "created_at": created_now,
                "updated_at": created_now,
                "debug": {"events": debug_events},
            }
            if client_request_id:
                order_doc["client_request_id"] = client_request_id

            order_docs, order_ids = _split_order_documents(order_doc, results, order_id)
            for line_doc in order_docs:
                _insert_order_doc_like_checkout(orders_col, line_doc)
            skipped_count = sum(
                1
                for it in results
                if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
            )
            return (
                jsonify(
                    {
                        "success": True,
                        "message": (
                            "No charge taken. {n} item(s) were skipped because the same phone, network, bundle, "
                            "and amount already has an order in processing or duplicated in cart."
                        ).format(n=skipped_count),
                        "order_id": order_ids[0],
                        "order_ids": order_ids,
                        "batch_id": order_id,
                        "redirect_url": f"/invoice-batch/{order_id}",
                        "status": "skipped",
                        "charged_amount": 0.0,
                        "profit_amount_total": 0.0,
                        "skipped_count": skipped_count,
                        "items": results,
                    }
                ),
                200,
            )

        # Deduct balance NOW
        balances_col.update_one(
            {"user_id": user_id},
            {"$inc": {"amount": -total_to_charge_now}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True,
        )

        status = "pending"
        created_now = datetime.utcnow()

        order_doc = {
            "user_id": user_id,
            "order_id": order_id,
            "items": results,
            "total_amount": total_requested,
            "charged_amount": total_to_charge_now,
            "profit_amount_total": round(profit_amount_total, 2),
            "status": status,
            "paid_from": method,
            "created_at": created_now,
            "updated_at": created_now,
            "debug": {"events": debug_events},
        }
        if client_request_id:
            order_doc["client_request_id"] = client_request_id

        order_docs, order_ids = _split_order_documents(order_doc, results, order_id)
        for line_doc in order_docs:
            _insert_order_doc_like_checkout(orders_col, line_doc)

        # Provider-wallet reservations happened before persistence using the
        # temporary batch ID. Relink them to the final independent order IDs.
        for line_doc in order_docs:
            try:
                provider_transactions_col.update_many(
                    {"order_id": order_id, "line_index": line_doc.get("batch_position")},
                    {"$set": {"order_id": line_doc["order_id"], "reference": line_doc["order_id"], "batch_id": order_id}},
                )
            except Exception as exc:
                jlog("provider_wallet_order_relink_error", batch_id=order_id, line_order_id=line_doc["order_id"], error=str(exc))

        # Record transaction
        providers_used = sorted(
            {it.get("provider") for it in results if it.get("provider")}
        )
        provider_request_ids = [
            it.get("provider_request_order_id")
            for it in results
            if it.get("provider_request_order_id")
        ]
        transactions_col.insert_one(
            {
                "user_id": user_id,
                "amount": total_to_charge_now,
                "reference": order_id,
                "status": "success",
                "type": "purchase",
                "gateway": "Wallet",
                "currency": "GHS",
                "created_at": datetime.utcnow(),
                "verified_at": datetime.utcnow(),
                "meta": {
                    "order_status": status,
                    "api_delivered_amount": round(total_delivered_api_amount, 2),
                    "processing_amount": round(total_processing_amount, 2),
                    "profit_amount_total": round(profit_amount_total, 2),
                    "providers_used": providers_used,
                    "provider_request_ids": provider_request_ids,
                },
            }
        )

        skipped_count = sum(
            1
            for it in results
            if it.get("line_status") in ("skipped_duplicate_processing", "skipped_duplicate_in_cart")
        )
        processing_count = sum(1 for it in results if it.get("line_status") == "processing")

        order_id_by_provider_ref = {
            doc["items"][0].get("provider_request_order_id"): doc["order_id"]
            for doc in order_docs if doc["items"][0].get("provider_request_order_id")
        }
        order_id_by_position = {doc.get("batch_position"): doc["order_id"] for doc in order_docs}
        for job in api_jobs:
            job["order_id"] = order_id_by_provider_ref.get(
                job.get("provider_request_order_id"),
                order_id_by_position.get(job.get("line_index"), order_ids[0]),
            )

        # 🔥 Spawn background worker for provider calls (does not block response)
        if api_jobs:
            try:
                t = threading.Thread(
                    target=_background_process_providers,
                    args=(order_id, api_jobs),
                    daemon=True,
                )
                t.start()
            except Exception as e:
                jlog("checkout_bg_spawn_error", order_id=order_id, error=str(e))

        msg = (
            "📝 Order received and is processing. "
            "We’ve charged your wallet. Order ID: {oid}"
        ).format(oid=", ".join(order_ids))

        return (
            jsonify(
                {
                    "success": True,
                    "message": msg,
                    "order_id": order_ids[0],
                    "order_ids": order_ids,
                    "batch_id": order_id,
                    "redirect_url": f"/invoice-batch/{order_id}",
                    "status": status,
                    "charged_amount": total_to_charge_now,
                    "profit_amount_total": round(profit_amount_total, 2),
                    "processing_count": processing_count,
                    "skipped_count": skipped_count,
                    "items": results,
                }
            ),
            200,
        )

    except Exception:
        jlog("checkout_uncaught", error=traceback.format_exc())
        return jsonify({"success": False, "message": "Server error"}), 500


# ===== Invoice view (same blueprint) =========================================
@checkout_bp.route("/invoice-batch/<batch_id>")
def invoice_batch_view(batch_id):
    orders = list(orders_col.find({"batch_id": batch_id}).sort("batch_position", 1))
    if not orders:
        single = orders_col.find_one({"order_id": batch_id})
        if not single:
            abort(404)
        orders = [single]
    return render_template(
        "invoice_batch.html",
        orders=orders,
        batch_id=batch_id,
        batch_total=sum(_money(order.get("charged_amount")) for order in orders),
        first_order=orders[0],
    )


@checkout_bp.route("/invoice/<order_id>")
def invoice_view(order_id):
    """
    Render a single invoice by CAMPUS DATA Order ID (e.g. CAMP578343)
    Uses invoice.html template you already created.
    """
    order = orders_col.find_one({"order_id": order_id})
    if not order:
        abort(404)

    user = {}
    try:
        uid = order.get("user_id")
        if uid:
            user = users_col.find_one({"_id": uid}) or {}
    except Exception:
        user = {}

    customer_name = (
        user.get("name")
        or user.get("full_name")
        or user.get("username")
        or "Customer"
    )

    return render_template(
        "invoice.html",
        order=order,
        user=user,
        customer=customer_name,
    )
