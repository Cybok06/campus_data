# index.py — Public landing page ONLY (no checkout / no orders)
from __future__ import annotations

from flask import Blueprint, render_template, request, session, redirect, url_for
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import json, ast, re, os

from db import db

index_bp = Blueprint("index", __name__)

# --- DB collections ---
services_col = db["services"]

# (Optional) still load Paystack public key if your index.html references it in JS
PAYSTACK_PUBLIC_KEY = os.getenv("PAYSTACK_PUBLIC_KEY", "")
STORE_PUBLIC_HOST = os.getenv("STORE_PUBLIC_HOST", "nagmart.store").strip().lower()

# ---------------- small helpers (local, no checkout imports) ----------------

def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        return float(v)
    except Exception:
        return None

def _money(v: Any) -> float:
    """Simple money normalizer -> 2dp float."""
    try:
        return round(float(v or 0.0), 2)
    except Exception:
        return 0.0

_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)
_mapping_like = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)

def _service_unit(svc: Dict[str, Any]) -> str:
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes"):
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"

def _parse_value_field(value: Any) -> Any:
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                try:
                    if _mapping_like.match(vt):
                        data = ast.literal_eval(vt)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    """
    For unit == 'data' -> we treat volume as MB.
    For unit == 'minutes' -> we treat volume as minutes.
    """
    if isinstance(value, dict):
        vol = value.get("volume")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            return float(vol)
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m: return float(m.group(1))
            if _NUM.match(vol_s): return float(vol_s)
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m: return float(m.group(1))
            if _NUM.match(s): return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m: return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m: return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m: return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m: return float(m.group(1))
            if _NUM.match(s2): return float(s2)
            return None

    return None

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _host_is_store_domain(host: str) -> bool:
    host_only = (host or "").split(":", 1)[0].strip().lower()
    if not STORE_PUBLIC_HOST:
        return False
    return host_only in (STORE_PUBLIC_HOST, f"www.{STORE_PUBLIC_HOST}")

PREFERRED_ORDER: List[str] = ["MTN", "AT - iShare", "AT - BigTime", "AFA TALKTIME"]

def _name_rank(name: str) -> Optional[int]:
    n = _norm(name)
    for i, want in enumerate(PREFERRED_ORDER):
        if _norm(want) == n:
            return i
    n2 = " ".join(n.split())
    for i, want in enumerate(PREFERRED_ORDER):
        if " ".join(_norm(want).split()) == n2:
            return i
    return None

def _created_ts(service_doc: Dict[str, Any]) -> float:
    ca = service_doc.get("created_at")
    if isinstance(ca, datetime):
        return ca.timestamp()
    try:
        val = float(ca)
        if val > 1e12:
            return val / 1000.0
        return val
    except Exception:
        return 0.0

def _service_priority_tuple(svc: Dict[str, Any]):
    prio = _to_float(svc.get("priority"))
    prio = prio if prio is not None else float("inf")
    name = svc.get("name") or ""
    nrank = _name_rank(name)
    nrank = nrank if nrank is not None else 10_000
    display_order = _to_float(svc.get("display_order"))
    display_order = display_order if display_order is not None else float("inf")
    ts = -_created_ts(svc)
    alpha = _norm(name)
    return (prio, nrank, display_order, ts, alpha)

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()
    availability = (svc.get("availability") or "AVAILABLE").upper()
    closed_msg = (svc.get("closed_message") or "This service is temporarily closed.")
    oos_msg = (svc.get("out_of_stock_message") or "This service is currently out of stock.")
    can_order = (status == "OPEN" and availability == "AVAILABLE")
    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        else:
            disabled_reason = "This service is currently unavailable."
    return {
        "type": t,
        "status": status,
        "availability": availability,
        "closed_message": closed_msg,
        "out_of_stock_message": oos_msg,
        "can_order": can_order,
        "disabled_reason": disabled_reason,
    }

def _is_express(svc: Dict[str, Any]) -> bool:
    cat = (svc.get("service_category") or "").strip().lower()
    cat2 = (svc.get("category") or "").strip().lower()
    return cat == "express services" or cat2 == "express"

# ------------------ data prep for landing page ------------------

def load_services_for_landing() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Load all services, normalize offers for display only.
    No wallet, no orders, no external providers.
    """
    raw = list(services_col.find({}))
    raw.sort(key=_service_priority_tuple)

    services: List[Dict[str, Any]] = []
    for s in raw:
        s = dict(s)
        s["_id_str"] = str(s["_id"])
        st = _service_state(s)
        s.update(st)

        # Simple display profit: use service.default_profit_percent if present, else 0
        eff_profit = _to_float(s.get("default_profit_percent")) or 0.0
        unit = _service_unit(s)
        offers = s.get("offers") or []

        normalized_offers: List[Dict[str, Any]] = []
        for of in offers:
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)
            value_text = _value_text_for_display(parsed_value, unit)

            amount = _to_float(of.get("amount"))
            # total = amount + markup (for display), you can change to just amount if you prefer
            total = (
                round((amount or 0.0) + ((amount or 0.0) * eff_profit / 100.0), 2)
                if amount is not None
                else None
            )

            normalized_offers.append(
                {
                    "amount": amount,
                    "value": parsed_value,
                    "value_text": value_text,
                    "profit_percent_used": eff_profit,
                    "total": total,
                    "_sort_vol": vol_num if vol_num is not None else float("inf"),
                    "_sort_amt": amount if amount is not None else float("inf"),
                }
            )

        normalized_offers.sort(key=lambda x: (x["_sort_vol"], x["_sort_amt"]))
        s["offers"] = [
            {k: v for k, v in o.items() if not k.startswith("_sort_")}
            for o in normalized_offers
        ]
        s["effective_profit_percent"] = eff_profit
        s["unit"] = unit

        services.append(s)

    express_services = [s for s in services if _is_express(s)]
    regular_services = [s for s in services if not _is_express(s)]
    return regular_services, express_services

# ------------------ routes ------------------

@index_bp.route("/", methods=["GET"])
def landing():
    """
    Simple public landing:
    - Loads services and express_services for display.
    - No posting orders, no Paystack verification, no checkout logic.
    """
    if session.get("user_id"):
        role = session.get("role", "customer")
        if role == "admin":
            return redirect(url_for("admin_dashboard.admin_dashboard"))
        return redirect(url_for("customer_dashboard.customer_dashboard"))
    try:
        regular, express = load_services_for_landing()
    except Exception:
        regular, express = [], []
    store_notice = None
    if _host_is_store_domain(request.host if request else ""):
        store_notice = "No store was found. Add the store slug to visit."

    return render_template(
        "index.html",
        services=regular,
        express_services=express,
        paystack_pk=PAYSTACK_PUBLIC_KEY,  # safe to leave; template can ignore if not used
        store_notice=store_notice,
    )
