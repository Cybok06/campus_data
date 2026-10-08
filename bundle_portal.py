"""Bundle Portal v2 client. Never poll status or retry an uncertain purchase."""
import os
import re
from decimal import Decimal, InvalidOperation

import requests

PROVIDERS = {
    "bundleportal_mtn": "mtn",
    "bundleportal_mtn2": "mtn_2",
    "bundleportal_mtn3": "mtn_3",
    "bundleportal_ishare": "airteltigo",
    "bundleportal_telecel": "telecel",
}
LABELS = {
    "bundleportal_mtn": "Bundle Portal MTN",
    "bundleportal_mtn2": "Bundle Portal MTN2",
    "bundleportal_mtn3": "Bundle Portal MTN3",
    "bundleportal_ishare": "Bundle Portal AT iShare",
    "bundleportal_telecel": "Bundle Portal Telecel",
}
API_URL = "https://api.bundleportal.com/v2"


def at_service_kind(service):
    name = re.sub(r"[^a-z0-9]", "", str((service or {}).get("name") or "").lower())
    if name in {"atishare", "airteltigoishare", "ishare"}:
        return "ishare"
    if name in {"atbigtime", "airteltigobigtime"}:
        return "bigtime"
    return None


def supports_service(provider, service):
    if provider == "bundleportal_telecel":
        names = " ".join(str((service or {}).get(key) or "") for key in ("name", "service_network", "network")).lower()
        return "telecel" in names or "vodafone" in names
    if provider == "bundleportal_ishare":
        return at_service_kind(service) == "ishare"
    return provider in PROVIDERS and str((service or {}).get("name") or "").strip().lower() in {"mtn normal", "mtn express"}


def canonical_network(network):
    return {"ishare": "airteltigo", "mtn_1": "mtn"}.get(network, network)


def configured():
    return bool((os.getenv("BUNDLE_PORTAL_KEY") or os.getenv("BUNDLEPORTAL_API_KEY", "")).strip())


def call(action, **fields):
    key = (os.getenv("BUNDLE_PORTAL_KEY") or os.getenv("BUNDLEPORTAL_API_KEY", "")).strip()
    if not key:
        return {"success": False, "code": "not_configured", "message": "Bundle Portal API key is not configured."}
    try:
        response = requests.post(
            API_URL, headers={"x-api-key": key, "Content-Type": "application/json"},
            json={"action": action, **fields}, timeout=(5, 25),
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Invalid response")
        payload["http_status"] = response.status_code
        if not response.ok:
            payload["success"] = False
        return payload
    except (requests.RequestException, ValueError):
        return {"success": False, "code": "unknown_outcome", "message": "Bundle Portal response unavailable; review before retrying."}


def package_size(value_obj, item):
    """Preserve fractional GB; existing service volume fields use decimal MB."""
    obj = value_obj if isinstance(value_obj, dict) else {}
    raw, divisor = None, 1
    for field in ("size_gb", "package_size", "gb_size", "gb", "volume_gb"):
        if obj.get(field) not in (None, ""):
            raw = obj[field]
            break
    if raw is None:
        raw = obj.get("volume") or obj.get("mb")
        divisor = 1000
        if obj.get("volume") and not obj.get("mb"):
            try:
                if Decimal(str(raw)) < 100:
                    divisor = 1
            except InvalidOperation:
                pass
    if raw is None:
        match = re.search(r"(\d+(?:\.\d+)?)\s*(GB|MB)\b", str(item.get("value") or item.get("label") or ""), re.I)
        if match:
            raw, divisor = match[1], (1000 if match[2].upper() == "MB" else 1)
    try:
        size = Decimal(str(raw)) / divisor
        return float(size) if size.is_finite() and size > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def submit(provider, recipient, size, reference, retry_purchase=False):
    network = PROVIDERS[provider]
    if not re.fullmatch(r"0\d{9}", str(recipient or "")) or not size:
        return {"success": False, "code": "validation", "message": "Invalid recipient or bundle size."}
    def purchase():
        result = call("place_order", network=network, recipient=recipient, package_size=size, order_id=reference)
        result["order_attempted"] = True
        return result
    if retry_purchase:
        # Reuse the exact reference; re-verification could block our own pending order.
        return purchase()
    # Verify against this route's own catalogue, never silently switch routes.
    bundles = call("get_bundles", network=network)
    if bundles.get("success") is not True:
        return bundles
    bundle_data = bundles.get("data")
    if not isinstance(bundle_data, dict) or not isinstance(bundle_data.get("bundles"), list):
        return {"success": False, "code": "unknown_outcome", "message": "Invalid Bundle Portal catalogue response."}
    available = [bundle for bundle in bundle_data["bundles"] if isinstance(bundle, dict)]
    if not any(canonical_network(b.get("network", network)) == network and _same_size(b.get("size_gb"), size) for b in available):
        return {"success": False, "code": "bundle_unavailable", "message": "Bundle size is unavailable on the selected Bundle Portal route."}
    verification = call("verify_number", network=network, recipient=recipient)
    if verification.get("success") is not True:
        return verification
    verification_data = verification.get("data")
    if not isinstance(verification_data, dict):
        return {"success": False, "code": "unknown_outcome", "message": "Invalid Bundle Portal verification response."}
    if verification_data.get("can_order") is not True:
        return {"success": False, "code": "recipient_blocked", "message": "Recipient is not approved or has an unfinished order."}
    return purchase()


def _same_size(left, right):
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except InvalidOperation:
        return False
