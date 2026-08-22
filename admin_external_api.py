from __future__ import annotations

from datetime import datetime
from ast import literal_eval
import hashlib
import json
import secrets
import re
from urllib.parse import urlencode

from bson import ObjectId
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for, flash

from db import db
from checkout import process_checkout


admin_external_api_bp = Blueprint("admin_external_api", __name__)

api_clients_col = db["api_clients"]
api_balance_logs_col = db["api_client_balance_logs"]
api_request_logs_col = db["api_request_logs"]
orders_col = db["orders"]
transactions_col = db["transactions"]
users_col = db["users"]
balances_col = db["balances"]
services_col = db["services"]
afa_col = db["afa_registrations"]
afa_settings_col = db["afa_settings"]

AFA_REGISTRATION_SERVICE_ID = "AFA_REGISTRATION"
AFA_REGISTRATION_SERVICE_NAME = "AFA Registration"


def _now():
    return datetime.utcnow()


def _is_admin() -> bool:
    return session.get("role") == "admin"


def _require_admin():
    if not _is_admin():
        return redirect(url_for("login.login"))
    return None


def _key_hash(raw: str) -> str:
    return hashlib.sha256((raw or "").encode("utf-8")).hexdigest()


def _make_api_key() -> tuple[str, str]:
    raw = "campapi_" + secrets.token_urlsafe(32)
    return raw, _key_hash(raw)


def _money(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _parse_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        return None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except Exception:
            pass
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt)
        except Exception:
            pass
    return None


def _safe_object_id(raw: str | None):
    try:
        return ObjectId(str(raw))
    except Exception:
        return None


def _extract_key_from_request() -> str:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("X-API-Key") or "").strip()


def _normalize_api_client(client: dict) -> dict:
    linked_user_id = client.get("linked_user_id")
    bal_doc = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1, "updated_at": 1}) or {}
    return {
        **client,
        "balance": _money(bal_doc.get("amount")),
        "balance_updated_at": bal_doc.get("updated_at"),
    }


def _create_hidden_wallet_user(client_name: str) -> ObjectId:
    suffix = secrets.token_hex(4)
    now = _now()
    user_doc = {
        "first_name": "API",
        "last_name": client_name[:48] if client_name else "Client",
        "username": f"api_client_{suffix}",
        "email": f"api_client_{suffix}@internal.local",
        "phone": "",
        "role": "customer",
        "is_api_client_user": True,
        "created_at": now,
        "updated_at": now,
    }
    res = users_col.insert_one(user_doc)
    balances_col.update_one(
        {"user_id": res.inserted_id},
        {"$setOnInsert": {"amount": 0.0, "currency": "GHS", "created_at": now}, "$set": {"updated_at": now}},
        upsert=True,
    )
    return res.inserted_id


def _set_wallet_amount(linked_user_id: ObjectId, amount: float) -> tuple[float, float]:
    bal = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1}) or {}
    before = _money(bal.get("amount"))
    balances_col.update_one(
        {"user_id": linked_user_id},
        {
            "$set": {"amount": float(amount), "currency": "GHS", "updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )
    return before, float(amount)


def _change_wallet_amount(linked_user_id: ObjectId, delta: float) -> tuple[float, float]:
    bal = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1}) or {}
    before = _money(bal.get("amount"))
    after = round(before + float(delta), 2)
    balances_col.update_one(
        {"user_id": linked_user_id},
        {
            "$set": {"amount": after, "currency": "GHS", "updated_at": _now()},
            "$setOnInsert": {"created_at": _now()},
        },
        upsert=True,
    )
    return before, after


def _log_balance_event(client: dict, action: str, before: float, after: float, delta: float, note: str = "", reference: str = ""):
    api_balance_logs_col.insert_one(
        {
            "api_client_id": client["_id"],
            "client_name": client.get("name") or "API Client",
            "linked_user_id": client.get("linked_user_id"),
            "action": action,
            "delta": float(delta),
            "amount_before": float(before),
            "amount_after": float(after),
            "reference": reference or "",
            "note": (note or "")[:240],
            "actor_admin_id": _safe_object_id(session.get("user_id")),
            "actor_name": session.get("username") or session.get("email") or "admin",
            "created_at": _now(),
        }
    )


_MB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*MB\s*$", re.I)
_GB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*G(?:B|IG)?\s*$", re.I)
_MIN_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*(?:MIN|MINS|MINUTE|MINUTES)?\s*$", re.I)


def _parse_value_obj(raw):
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    txt = str(raw).strip()
    if not txt or not txt.startswith("{") or not txt.endswith("}"):
        return {}
    try:
        parsed = json.loads(txt)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        pass
    try:
        parsed = literal_eval(txt)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _service_unit(service_doc: dict) -> str:
    unit = str(service_doc.get("unit") or "").strip().lower()
    name = str(service_doc.get("name") or "").strip().lower()
    if unit in {"min", "mins", "minute", "minutes"}:
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"


def _format_offer_size(value_raw) -> str:
    value_obj = _parse_value_obj(value_raw)
    volume = value_obj.get("volume")
    if volume not in (None, ""):
        try:
            vol = float(volume)
            return f"{int(round(vol))}"
        except Exception:
            pass

    txt = str(value_raw or "").strip()
    if not txt:
        return ""

    mb = _MB_RE.match(txt)
    if mb:
        return f"{int(round(float(mb.group(1).replace(',', ''))))}MB"
    gb = _GB_RE.match(txt)
    if gb:
        val = float(gb.group(1).replace(",", ""))
        return f"{int(val)}GB" if abs(val - round(val)) < 1e-9 else f"{val:.2f}GB"
    return txt


def _value_text_for_offer(service_doc: dict, value_raw) -> str:
    unit = _service_unit(service_doc)
    value_obj = _parse_value_obj(value_raw)
    volume = value_obj.get("volume")
    if volume not in (None, ""):
        try:
            vol = float(volume)
            if unit == "minutes":
                return f"{int(round(vol))} mins"
            if vol >= 1000:
                gb = vol / 1000.0
                return f"{int(gb)}GB" if abs(gb - round(gb)) < 1e-9 else f"{gb:.2f}GB"
            return f"{int(round(vol))}MB"
        except Exception:
            pass
    base = _format_offer_size(value_raw)
    if unit == "minutes" and base:
        m = _MIN_RE.match(base)
        if m:
            val = float(m.group(1).replace(",", ""))
            return f"{int(round(val))} mins"
    return base


def _normalize_size_key(value: str, unit: str = "data") -> str:
    raw = str(value or "").strip().lower()
    raw = re.sub(r"\s+", "", raw)
    if unit == "minutes":
        m = _MIN_RE.match(str(value or ""))
        if m:
            return f"{float(m.group(1).replace(',', '')):.0f}mins"
        return raw.replace("minutes", "mins").replace("minute", "mins").replace("min", "mins")
    return raw


def _find_offer_for_size(service_doc: dict, requested_size: str):
    unit = _service_unit(service_doc)
    target = _normalize_size_key(requested_size, unit)
    offers = service_doc.get("offers") or []
    for offer in offers:
        display = _value_text_for_offer(service_doc, offer.get("value"))
        if _normalize_size_key(display, unit) == target:
            return offer, display
        raw_val = str(offer.get("value") or "").strip()
        if raw_val and _normalize_size_key(raw_val, unit) == target:
            return offer, display or raw_val
    return None, None


def _details_to_object(details):
    if isinstance(details, dict):
        return details
    if isinstance(details, list):
        out = {}
        for item in details:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or item.get("name") or "").strip()
            if not key:
                continue
            out[key] = item.get("value")
        return out
    return {}


def _load_afa_settings():
    doc = afa_settings_col.find_one({"_id": "AFA_SETTINGS"}) or {}
    price = _money(doc.get("price"), 0.0)
    return {
        "price": max(0.0, price),
        "is_open": bool(doc.get("is_open", True)),
        "in_stock": bool(doc.get("in_stock", True)),
    }


def _build_external_order_response(order: dict):
    items = []
    for item in (order.get("items") or []):
        items.append(
            {
                "service_id": str(item.get("serviceId") or ""),
                "service_name": item.get("serviceName") or "",
                "phone": item.get("phone") or "",
                "size": _format_offer_size(item.get("value")),
                "line_status": item.get("line_status") or "",
                "api_status": item.get("api_status") or "",
                "amount": _money(item.get("amount")),
            }
        )

    return {
        "success": True,
        "order_id": order.get("order_id"),
        "client_reference": ((order.get("external_request") or {}).get("client_reference") or ""),
        "status_url": f"{request.url_root.rstrip('/')}/api/external/orders/{order.get('order_id')}",
        "status": order.get("status") or "",
        "charged_amount": _money(order.get("charged_amount")),
        "api_client_charged_amount": _money(order.get("api_client_charged_amount"), _money(order.get("charged_amount"))),
        "source": order.get("source") or "external_api",
        "paid_from": order.get("paid_from") or "",
        "api_client": order.get("api_client_name") or "API Client",
        "balance_remaining": _money((balances_col.find_one({"user_id": order.get("user_id")}, {"amount": 1}) or {}).get("amount")),
        "items": items,
        "created_at": order.get("created_at").isoformat() if order.get("created_at") else None,
        "updated_at": order.get("updated_at").isoformat() if order.get("updated_at") else None,
    }


def _build_external_balance_response(client: dict):
    linked_user_id = client.get("linked_user_id")
    bal_doc = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1, "currency": 1, "updated_at": 1}) or {}
    return {
        "success": True,
        "client_id": str(client.get("_id")),
        "client_name": client.get("name") or "API Client",
        "balance": _money(bal_doc.get("amount")),
        "currency": bal_doc.get("currency") or "GHS",
        "updated_at": bal_doc.get("updated_at").isoformat() if bal_doc.get("updated_at") else None,
    }


def _build_external_transaction_response(txn: dict):
    meta = txn.get("meta") or {}
    return {
        "id": str(txn.get("_id")),
        "amount": _money(txn.get("amount")),
        "reference": txn.get("reference") or "",
        "status": txn.get("status") or "",
        "type": txn.get("type") or "",
        "gateway": txn.get("gateway") or "",
        "currency": txn.get("currency") or "GHS",
        "source": meta.get("source") or txn.get("source") or "",
        "created_at": txn.get("created_at").isoformat() if txn.get("created_at") else None,
        "verified_at": txn.get("verified_at").isoformat() if txn.get("verified_at") else None,
        "meta": meta,
    }


def _build_external_afa_registration_status_response(reg: dict):
    external_request = reg.get("external_request") or {}
    return {
        "success": True,
        "client_reference": external_request.get("client_reference") or "",
        "status": reg.get("status") or "",
    }


def _log_request(client: dict | None, endpoint: str, status_code: int, client_reference: str = "", order_id: str = "", detail: str = ""):
    api_request_logs_col.insert_one(
        {
            "api_client_id": client.get("_id") if client else None,
            "client_name": (client or {}).get("name") or "",
            "endpoint": endpoint,
            "status_code": int(status_code),
            "client_reference": client_reference or "",
            "order_id": order_id or "",
            "detail": (detail or "")[:400],
            "ip": request.headers.get("X-Forwarded-For", request.remote_addr or ""),
            "created_at": _now(),
        }
    )


def _authenticate_api_client():
    raw = _extract_key_from_request()
    if not raw:
        return None
    hashed = _key_hash(raw)
    client = api_clients_col.find_one({"key_hash": hashed, "status": "active"})
    if not client:
        return None
    api_clients_col.update_one({"_id": client["_id"]}, {"$set": {"last_used_at": _now()}})
    return client


def _get_page_arg(name: str, default: int = 1) -> int:
    try:
        return max(1, int(request.args.get(name, default)))
    except Exception:
        return default


def _page_url(param_name: str, page: int) -> str:
    args = request.args.to_dict(flat=True)
    args[param_name] = str(max(1, int(page)))
    return f"{url_for('admin_external_api.admin_api_page')}?{urlencode(args)}"


def _render_admin_page(generated_key: str | None = None):
    per_page = 5
    orders_page = _get_page_arg("orders_page")
    logs_page = _get_page_arg("logs_page")
    requests_page = _get_page_arg("requests_page")

    clients = [
        _normalize_api_client(doc)
        for doc in api_clients_col.find({}).sort("created_at", -1)
    ]
    total_recent_orders = orders_col.count_documents({"source": "external_api"})
    recent_orders = list(
        orders_col.find({"source": "external_api"})
        .sort("created_at", -1)
        .skip((orders_page - 1) * per_page)
        .limit(per_page)
    )
    total_recent_logs = api_balance_logs_col.count_documents({})
    recent_logs = list(
        api_balance_logs_col.find({})
        .sort("created_at", -1)
        .skip((logs_page - 1) * per_page)
        .limit(per_page)
    )
    total_recent_requests = api_request_logs_col.count_documents({})
    recent_requests = list(
        api_request_logs_col.find({})
        .sort("created_at", -1)
        .skip((requests_page - 1) * per_page)
        .limit(per_page)
    )
    services = list(
        services_col.find(
            {},
            {"name": 1, "provider": 1, "type": 1, "status": 1, "offers": 1},
        ).sort("name", 1).limit(80)
    )
    return render_template(
        "admin_external_api.html",
        clients=clients,
        recent_orders=recent_orders,
        recent_logs=recent_logs,
        recent_requests=recent_requests,
        orders_page=orders_page,
        logs_page=logs_page,
        requests_page=requests_page,
        orders_total_pages=max(1, (total_recent_orders + per_page - 1) // per_page),
        logs_total_pages=max(1, (total_recent_logs + per_page - 1) // per_page),
        requests_total_pages=max(1, (total_recent_requests + per_page - 1) // per_page),
        orders_prev_url=_page_url("orders_page", max(1, orders_page - 1)),
        orders_next_url=_page_url("orders_page", orders_page + 1),
        logs_prev_url=_page_url("logs_page", max(1, logs_page - 1)),
        logs_next_url=_page_url("logs_page", logs_page + 1),
        requests_prev_url=_page_url("requests_page", max(1, requests_page - 1)),
        requests_next_url=_page_url("requests_page", requests_page + 1),
        services=services,
        generated_key=generated_key,
    )


@admin_external_api_bp.route("/admin/api", methods=["GET"])
def admin_api_page():
    gate = _require_admin()
    if gate:
        return gate
    return _render_admin_page()


@admin_external_api_bp.route("/admin/api/clients/create", methods=["POST"])
def create_api_client():
    gate = _require_admin()
    if gate:
        return gate

    name = (request.form.get("name") or "").strip()
    notes = (request.form.get("notes") or "").strip()
    initial_balance = _money(request.form.get("initial_balance"), 0.0)
    if not name:
        flash("Client name is required.", "warning")
        return redirect(url_for("admin_external_api.admin_api_page"))

    linked_user_id = _create_hidden_wallet_user(name)
    raw_key, key_hash = _make_api_key()
    now = _now()
    doc = {
        "name": name[:80],
        "notes": notes[:400],
        "status": "active",
        "key_hash": key_hash,
        "key_prefix": raw_key[:14],
        "linked_user_id": linked_user_id,
        "created_at": now,
        "updated_at": now,
        "last_used_at": None,
    }
    res = api_clients_col.insert_one(doc)
    if initial_balance > 0:
        before, after = _set_wallet_amount(linked_user_id, initial_balance)
        _log_balance_event({**doc, "_id": res.inserted_id}, "set", before, after, after - before, note="Initial API balance")
    flash("API client created.", "success")
    return _render_admin_page(generated_key=raw_key)


@admin_external_api_bp.route("/admin/api/clients/<client_id>/regenerate", methods=["POST"])
def regenerate_api_key(client_id):
    gate = _require_admin()
    if gate:
        return gate
    oid = _safe_object_id(client_id)
    if not oid:
        flash("Invalid client.", "danger")
        return redirect(url_for("admin_external_api.admin_api_page"))
    raw_key, key_hash = _make_api_key()
    api_clients_col.update_one(
        {"_id": oid},
        {"$set": {"key_hash": key_hash, "key_prefix": raw_key[:14], "updated_at": _now()}},
    )
    flash("API key regenerated.", "success")
    return _render_admin_page(generated_key=raw_key)


@admin_external_api_bp.route("/admin/api/clients/<client_id>/status", methods=["POST"])
def update_api_client_status(client_id):
    gate = _require_admin()
    if gate:
        return gate
    oid = _safe_object_id(client_id)
    status = (request.form.get("status") or "").strip().lower()
    if not oid or status not in {"active", "suspended", "revoked"}:
        flash("Invalid client update.", "danger")
        return redirect(url_for("admin_external_api.admin_api_page"))
    api_clients_col.update_one({"_id": oid}, {"$set": {"status": status, "updated_at": _now()}})
    flash(f"Client marked {status}.", "success")
    return redirect(url_for("admin_external_api.admin_api_page"))


@admin_external_api_bp.route("/admin/api/clients/<client_id>/wallet", methods=["POST"])
def adjust_api_client_wallet(client_id):
    gate = _require_admin()
    if gate:
        return gate
    oid = _safe_object_id(client_id)
    client = api_clients_col.find_one({"_id": oid})
    if not client:
        flash("Client not found.", "danger")
        return redirect(url_for("admin_external_api.admin_api_page"))

    action = (request.form.get("action") or "").strip().lower()
    amount = _money(request.form.get("amount"), 0.0)
    note = (request.form.get("note") or "").strip()
    linked_user_id = client.get("linked_user_id")
    if not linked_user_id:
        flash("Client wallet is not linked.", "danger")
        return redirect(url_for("admin_external_api.admin_api_page"))

    if action == "set":
        before, after = _set_wallet_amount(linked_user_id, max(0.0, amount))
        _log_balance_event(client, "set", before, after, after - before, note=note)
    elif action == "fund":
        if amount <= 0:
            flash("Fund amount must be greater than zero.", "warning")
            return redirect(url_for("admin_external_api.admin_api_page"))
        before, after = _change_wallet_amount(linked_user_id, amount)
        _log_balance_event(client, "fund", before, after, amount, note=note)
    elif action == "debit":
        if amount <= 0:
            flash("Debit amount must be greater than zero.", "warning")
            return redirect(url_for("admin_external_api.admin_api_page"))
        bal = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1}) or {}
        if _money(bal.get("amount")) < amount:
            flash("Insufficient API balance for manual debit.", "danger")
            return redirect(url_for("admin_external_api.admin_api_page"))
        before, after = _change_wallet_amount(linked_user_id, -amount)
        _log_balance_event(client, "debit", before, after, -amount, note=note)
    else:
        flash("Unknown wallet action.", "danger")
        return redirect(url_for("admin_external_api.admin_api_page"))

    flash("API wallet updated.", "success")
    return redirect(url_for("admin_external_api.admin_api_page"))


@admin_external_api_bp.route("/api/external/orders", methods=["POST"])
def create_external_order():
    client = _authenticate_api_client()
    if not client:
        _log_request(None, "/api/external/orders", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    payload = request.get_json(silent=True) or {}
    service_id = str(payload.get("service_id") or "").strip()
    phone = str(payload.get("phone") or "").strip()
    size = str(payload.get("size") or "").strip()
    client_reference = str(payload.get("client_reference") or "").strip()
    requested_base_amount = payload.get("base_amount")

    if not service_id or not phone or not size:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="missing_required_fields")
        return jsonify({"success": False, "message": "service_id, phone, and size are required"}), 400

    service_oid = _safe_object_id(service_id)
    if not service_oid:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="invalid_service_id")
        return jsonify({"success": False, "message": "Invalid service_id"}), 400

    service_doc = services_col.find_one({"_id": service_oid})
    if not service_doc:
        _log_request(client, "/api/external/orders", 404, client_reference=client_reference, detail="service_not_found")
        return jsonify({"success": False, "message": "Service not found"}), 404

    offer, resolved_size = _find_offer_for_size(service_doc, size)
    if not offer:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="offer_size_not_found")
        return jsonify({"success": False, "message": "Requested size does not match this service"}), 400

    amount = _money(offer.get("customer_price"), None)
    if amount is None:
        amount = _money(offer.get("amount"), 0.0)
    if amount <= 0:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="invalid_offer_amount")
        return jsonify({"success": False, "message": "Resolved offer amount is invalid"}), 400

    api_client_charge_amount = _money(requested_base_amount, None)
    if api_client_charge_amount is None:
        api_client_charge_amount = amount
    if api_client_charge_amount <= 0:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="invalid_base_amount")
        return jsonify({"success": False, "message": "base_amount must be greater than zero"}), 400

    linked_user_id = client.get("linked_user_id")
    if not linked_user_id:
        _log_request(client, "/api/external/orders", 500, client_reference=client_reference, detail="missing_linked_wallet")
        return jsonify({"success": False, "message": "API client wallet is not configured"}), 500

    if client_reference:
        existing_order = orders_col.find_one(
            {"user_id": linked_user_id, "client_request_id": client_reference},
            {"order_id": 1},
        )
        if existing_order and existing_order.get("order_id"):
            saved = orders_col.find_one({"order_id": existing_order.get("order_id")}) or existing_order
            _log_request(client, "/api/external/orders", 200, client_reference=client_reference, order_id=existing_order.get("order_id"), detail="idempotent_hit")
            return jsonify(_build_external_order_response(saved)), 200

    bal_doc = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1}) or {}
    original_api_balance = _money(bal_doc.get("amount"))
    if original_api_balance < api_client_charge_amount:
        _log_request(client, "/api/external/orders", 400, client_reference=client_reference, detail="insufficient_api_balance")
        return jsonify({"success": False, "message": "Insufficient API balance"}), 400

    temp_wallet_boosted = False
    if original_api_balance < amount:
        _set_wallet_amount(linked_user_id, amount)
        temp_wallet_boosted = True

    internal_payload = {
        "cart": [
            {
                "serviceId": service_id,
                "serviceName": service_doc.get("name") or "",
                "phone": phone,
                "value": resolved_size or size,
                "value_obj": offer.get("value"),
                "amount": amount,
            }
        ],
        "method": "external_api_balance",
    }
    if client_reference:
        internal_payload["client_request_id"] = client_reference

    with current_app.test_request_context("/checkout", method="POST", json=internal_payload):
        session["user_id"] = str(linked_user_id)
        session["role"] = "customer"
        session["username"] = client.get("name") or "API Client"
        checkout_resp = process_checkout()

    if isinstance(checkout_resp, tuple):
        response_obj, status_code = checkout_resp
    else:
        response_obj, status_code = checkout_resp, getattr(checkout_resp, "status_code", 200)

    body = response_obj.get_json(silent=True) or {}
    order_id = body.get("order_id") or ""

    if status_code == 200 and order_id:
        current_bal_doc = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1}) or {}
        wallet_after_checkout = _money(current_bal_doc.get("amount"))
        target_api_balance = round(original_api_balance - api_client_charge_amount, 2)
        adjustment = round(target_api_balance - wallet_after_checkout, 2)
        if abs(adjustment) >= 0.0001:
            _change_wallet_amount(linked_user_id, adjustment)

        order_updates = {
            "source": "external_api",
            "api_client_id": client["_id"],
            "api_client_name": client.get("name") or "API Client",
            "paid_from": "external_api_balance",
            "api_client_charged_amount": api_client_charge_amount,
            "external_request": {
                "service_id": service_id,
                "phone": phone,
                "size": resolved_size or size,
                "client_reference": client_reference,
                "base_amount": api_client_charge_amount,
            },
            "updated_at": _now(),
        }
        orders_col.update_one({"order_id": order_id}, {"$set": order_updates})
        transactions_col.update_many(
            {"reference": order_id},
            {
                "$set": {
                    "gateway": "External API Balance",
                    "type": "external_api_purchase",
                    "api_client_id": client["_id"],
                    "amount": api_client_charge_amount,
                    "updated_at": _now(),
                    "meta.source": "external_api",
                    "meta.api_client_id": str(client["_id"]),
                    "meta.client_reference": client_reference,
                    "meta.api_client_charged_amount": api_client_charge_amount,
                    "meta.order_saved_amount": _money(body.get("charged_amount")),
                }
            },
        )

        if api_client_charge_amount > 0:
            _log_balance_event(
                client,
                "debit",
                original_api_balance,
                target_api_balance,
                -api_client_charge_amount,
                note="External API order debit" if api_client_charge_amount == amount else f"External API order debit override (saved order amount {amount:.2f})",
                reference=order_id,
            )

        saved = orders_col.find_one({"order_id": order_id}) or {}
        _log_request(client, "/api/external/orders", 200, client_reference=client_reference, order_id=order_id, detail="ok")
        return jsonify(_build_external_order_response(saved)), 200

    if temp_wallet_boosted:
        _set_wallet_amount(linked_user_id, original_api_balance)

    _log_request(
        client,
        "/api/external/orders",
        int(status_code),
        client_reference=client_reference,
        order_id=order_id,
        detail=body.get("message") or "checkout_failed",
    )
    return jsonify(body or {"success": False, "message": "Order creation failed"}), status_code


@admin_external_api_bp.route("/api/external/afa/registrations", methods=["POST"])
def create_external_afa_registration():
    client = _authenticate_api_client()
    if not client:
        _log_request(None, "/api/external/afa/registrations", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    payload = request.get_json(silent=True) or {}
    service_id = str(payload.get("service_id") or "").strip()
    client_reference = str(payload.get("client_reference") or "").strip()
    details = _details_to_object(payload.get("details"))

    if service_id != AFA_REGISTRATION_SERVICE_ID:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="invalid_service_id")
        return jsonify({"success": False, "message": f"service_id must be {AFA_REGISTRATION_SERVICE_ID}"}), 400

    name = str(details.get("name") or "").strip()
    phone = str(details.get("phone") or "").strip()
    dob = str(details.get("dob") or "").strip() or None
    location = str(details.get("location") or "").strip() or None
    ghana_card = str(details.get("ghana_card") or details.get("ghanaCard") or "").strip() or None

    if not name:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="missing_name")
        return jsonify({"success": False, "message": "details.name is required"}), 400
    if not re.match(r"^0\d{9}$", phone):
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="invalid_phone")
        return jsonify({"success": False, "message": "details.phone must be 0xxxxxxxxx"}), 400

    settings = _load_afa_settings()
    if not settings["is_open"]:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="service_closed")
        return jsonify({"success": False, "message": "AFA registration is closed"}), 400
    if not settings["in_stock"]:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="out_of_stock")
        return jsonify({"success": False, "message": "AFA registration is out of stock"}), 400

    linked_user_id = client.get("linked_user_id")
    if not linked_user_id:
        _log_request(client, "/api/external/afa/registrations", 500, client_reference=client_reference, detail="missing_linked_wallet")
        return jsonify({"success": False, "message": "API client wallet is not configured"}), 500

    if client_reference:
        existing = afa_col.find_one(
            {"api_client_id": client["_id"], "external_request.client_reference": client_reference},
            {"_id": 1, "status": 1, "charged_amount": 1, "created_at": 1},
        )
        if existing:
            _log_request(client, "/api/external/afa/registrations", 200, client_reference=client_reference, detail="idempotent_hit")
            return jsonify(
                {
                    "success": True,
                    "registration_id": str(existing["_id"]),
                    "status": existing.get("status") or "pending",
                    "charged_amount": _money(existing.get("charged_amount")),
                    "source": "external_api",
                    "service_id": AFA_REGISTRATION_SERVICE_ID,
                    "service_name": AFA_REGISTRATION_SERVICE_NAME,
                    "created_at": existing.get("created_at").isoformat() if existing.get("created_at") else None,
                }
            ), 200

    bal = balances_col.find_one({"user_id": linked_user_id}, {"amount": 1, "currency": 1}) or {}
    price = _money(settings.get("price"), 0.0)
    before = _money(bal.get("amount"))
    if before < price:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="insufficient_balance")
        return jsonify({"success": False, "message": "Insufficient API balance"}), 400

    now = _now()
    after = round(before - price, 2)
    upd = balances_col.update_one(
        {"user_id": linked_user_id, "amount": {"$gte": price}},
        {"$inc": {"amount": -price}, "$set": {"updated_at": now}},
        upsert=False,
    )
    if upd.matched_count == 0:
        _log_request(client, "/api/external/afa/registrations", 400, client_reference=client_reference, detail="insufficient_balance_race")
        return jsonify({"success": False, "message": "Insufficient API balance"}), 400

    log_res = api_balance_logs_col.insert_one(
        {
            "api_client_id": client["_id"],
            "client_name": client.get("name") or "API Client",
            "linked_user_id": linked_user_id,
            "action": "debit",
            "delta": -price,
            "amount_before": before,
            "amount_after": after,
            "reference": client_reference or "",
            "note": "External API AFA registration debit",
            "actor_admin_id": None,
            "actor_name": "external_api",
            "created_at": now,
        }
    )

    reg_doc = {
        "customer_id": linked_user_id,
        "name": name,
        "phone": phone,
        "dob": dob,
        "location": location,
        "ghana_card": ghana_card,
        "status": "pending",
        "charged": True,
        "amount": price,
        "charged_amount": price,
        "charged_at": now,
        "charged_by": client.get("name") or "external_api",
        "charge_log_id": log_res.inserted_id,
        "source": "external_api",
        "api_client_id": client["_id"],
        "api_client_name": client.get("name") or "API Client",
        "service_id": AFA_REGISTRATION_SERVICE_ID,
        "service_name": AFA_REGISTRATION_SERVICE_NAME,
        "external_request": {
            "client_reference": client_reference,
            "details": {
                "name": name,
                "phone": phone,
                "dob": dob,
                "location": location,
                "ghana_card": ghana_card,
            },
        },
        "created_at": now,
        "updated_at": now,
    }
    reg_id = afa_col.insert_one(reg_doc).inserted_id

    _log_request(client, "/api/external/afa/registrations", 200, client_reference=client_reference, detail="ok")
    return jsonify(
        {
            "success": True,
            "registration_id": str(reg_id),
            "status": "pending",
            "charged_amount": price,
            "balance_remaining": after,
            "source": "external_api",
            "service_id": AFA_REGISTRATION_SERVICE_ID,
            "service_name": AFA_REGISTRATION_SERVICE_NAME,
            "created_at": now.isoformat(),
        }
    ), 200


@admin_external_api_bp.route("/api/external/balance", methods=["GET"])
def get_external_balance():
    client = _authenticate_api_client()
    if not client:
        _log_request(None, "/api/external/balance", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    _log_request(client, "/api/external/balance", 200, detail="ok")
    return jsonify(_build_external_balance_response(client)), 200


@admin_external_api_bp.route("/api/external/balance/topup", methods=["POST"])
def topup_external_balance():
    client = _authenticate_api_client()
    if not client:
        _log_request(None, "/api/external/balance/topup", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    payload = request.get_json(silent=True) or {}
    amount = _money(payload.get("amount"), 0.0)
    raw_date = str(payload.get("date") or "").strip()
    paystack_reference = str(payload.get("paystack_reference") or "").strip()
    paid_at = _parse_datetime(raw_date)

    if amount <= 0:
        _log_request(client, "/api/external/balance/topup", 400, detail="invalid_amount")
        return jsonify({"success": False, "message": "amount must be greater than zero"}), 400
    if not paid_at:
        _log_request(client, "/api/external/balance/topup", 400, detail="invalid_date")
        return jsonify({"success": False, "message": "date is required and must be valid"}), 400
    if not paystack_reference:
        _log_request(client, "/api/external/balance/topup", 400, detail="missing_paystack_reference")
        return jsonify({"success": False, "message": "paystack_reference is required"}), 400

    linked_user_id = client.get("linked_user_id")
    if not linked_user_id:
        _log_request(client, "/api/external/balance/topup", 500, detail="missing_linked_wallet")
        return jsonify({"success": False, "message": "API client wallet is not configured"}), 500

    existing_log = api_balance_logs_col.find_one(
        {
            "api_client_id": client["_id"],
            "action": "fund",
            "reference": paystack_reference,
        },
        {"amount_after": 1, "created_at": 1},
    )
    if existing_log:
        _log_request(client, "/api/external/balance/topup", 200, detail="idempotent_hit")
        return jsonify(
            {
                **_build_external_balance_response(client),
                "message": "Balance top-up already recorded",
                "paystack_reference": paystack_reference,
                "amount": amount,
                "date": paid_at.isoformat(),
            }
        ), 200

    before, after = _change_wallet_amount(linked_user_id, amount)
    _log_balance_event(
        client,
        "fund",
        before,
        after,
        amount,
        note=f"External API balance top-up on {paid_at.isoformat()}",
        reference=paystack_reference,
    )
    api_balance_logs_col.update_one(
        {
            "api_client_id": client["_id"],
            "action": "fund",
            "reference": paystack_reference,
            "amount_after": after,
        },
        {
            "$set": {
                "payment_date": paid_at,
                "payment_gateway": "Paystack",
                "updated_at": _now(),
            }
        },
    )
    transactions_col.insert_one(
        {
            "user_id": linked_user_id,
            "amount": amount,
            "reference": paystack_reference,
            "status": "success",
            "type": "external_api_funding",
            "gateway": "Paystack",
            "currency": "GHS",
            "created_at": _now(),
            "verified_at": paid_at,
            "meta": {
                "source": "external_api",
                "api_client_id": str(client["_id"]),
                "api_client_name": client.get("name") or "API Client",
                "payment_date": paid_at.isoformat(),
            },
        }
    )

    _log_request(client, "/api/external/balance/topup", 200, detail="ok")
    return jsonify(
        {
            **_build_external_balance_response(client),
            "message": "Balance updated successfully",
            "paystack_reference": paystack_reference,
            "amount": amount,
            "date": paid_at.isoformat(),
        }
    ), 200


@admin_external_api_bp.route("/api/external/transactions", methods=["GET"])
def get_external_transactions():
    client = _authenticate_api_client()
    if not client:
        _log_request(None, "/api/external/transactions", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    linked_user_id = client.get("linked_user_id")
    if not linked_user_id:
        _log_request(client, "/api/external/transactions", 500, detail="missing_linked_wallet")
        return jsonify({"success": False, "message": "API client wallet is not configured"}), 500

    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        limit = int(request.args.get("limit", 20))
    except Exception:
        limit = 20
    limit = max(1, min(limit, 100))

    type_filter = str(request.args.get("type") or "").strip()
    status_filter = str(request.args.get("status") or "").strip()

    query = {"user_id": linked_user_id}
    if type_filter:
        query["type"] = type_filter
    if status_filter:
        query["status"] = status_filter

    total = transactions_col.count_documents(query)
    items = list(
        transactions_col.find(query)
        .sort([("verified_at", -1), ("created_at", -1)])
        .skip((page - 1) * limit)
        .limit(limit)
    )

    _log_request(client, "/api/external/transactions", 200, detail="ok")
    return jsonify(
        {
            "success": True,
            "client_id": str(client["_id"]),
            "client_name": client.get("name") or "API Client",
            "page": page,
            "limit": limit,
            "total": total,
            "items": [_build_external_transaction_response(txn) for txn in items],
        }
    ), 200


@admin_external_api_bp.route("/api/external/afa/registrations/<client_reference>", methods=["GET"])
def get_external_afa_registration_status(client_reference):
    client = _authenticate_api_client()
    if not client:
        _log_request(None, f"/api/external/afa/registrations/{client_reference}", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    ref = str(client_reference or "").strip()
    reg = afa_col.find_one(
        {
            "api_client_id": client["_id"],
            "external_request.client_reference": ref,
        }
    )
    if not reg:
        _log_request(client, f"/api/external/afa/registrations/{ref}", 404, detail="registration_not_found")
        return jsonify({"success": False, "message": "AFA registration not found"}), 404

    _log_request(client, f"/api/external/afa/registrations/{ref}", 200, client_reference=ref, detail="ok")
    return jsonify(_build_external_afa_registration_status_response(reg)), 200


@admin_external_api_bp.route("/api/external/orders/<order_id>", methods=["GET"])
def get_external_order(order_id):
    client = _authenticate_api_client()
    if not client:
        _log_request(None, f"/api/external/orders/{order_id}", 401, detail="invalid_api_key")
        return jsonify({"success": False, "message": "Invalid API key"}), 401

    order = orders_col.find_one({"order_id": order_id, "api_client_id": client["_id"]})
    if not order:
        _log_request(client, f"/api/external/orders/{order_id}", 404, detail="order_not_found")
        return jsonify({"success": False, "message": "Order not found"}), 404

    _log_request(client, f"/api/external/orders/{order_id}", 200, order_id=order_id, detail="ok")
    return jsonify(_build_external_order_response(order)), 200
