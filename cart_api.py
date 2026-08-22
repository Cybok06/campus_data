from flask import Blueprint, request, jsonify, session
from bson import ObjectId
from datetime import datetime
import re

from db import db
from phone_order_guard import BLOCK_NEW_NUMBERS_MESSAGE, first_blocked_phone, is_phone_allowed

cart_api_bp = Blueprint("cart_api", __name__)

carts_col = db["carts"]  # { user_id, items: [ { _id, ... } ], updated_at }

PHONE_RE = re.compile(r"^0\d{9}$")

def _oid(x):
    return ObjectId(x) if isinstance(x, str) else x

def _ensure_customer():
    # Require an authenticated customer
    if not session.get("user_id") or session.get("role") != "customer":
        return None
    try:
        return ObjectId(session["user_id"])
    except Exception:
        return None

def _normalize_item(raw):
    """
    Accept and normalize a cart item from client.
    Required: serviceId, serviceName, phone, amount/total
    Optional: value, value_obj, base_amount
    """
    if not isinstance(raw, dict):
        return None, "Invalid item"
    service_id = raw.get("serviceId") or raw.get("service_id")
    service_name = (raw.get("serviceName") or raw.get("service_name") or "").strip()
    phone = (raw.get("phone") or "").strip()
    amount = raw.get("total", raw.get("amount"))

    if not service_id or not service_name:
        return None, "Missing service info"
    if not PHONE_RE.match(phone):
        return None, "Invalid phone (must be 0xxxxxxxxx)"
    allowed, message, _has_history = is_phone_allowed(phone)
    if not allowed:
        return None, message or BLOCK_NEW_NUMBERS_MESSAGE
    try:
        amount = float(amount)
        if amount <= 0:
            return None, "Invalid amount"
    except Exception:
        return None, "Invalid amount"

    item = {
        "_id": ObjectId(),
        "serviceId": str(service_id),
        "serviceName": service_name,
        "phone": phone,
        "value": raw.get("value"),          # label like '1GB'
        "value_obj": raw.get("value_obj"),  # original offer value object (if any)
        "base_amount": float(raw.get("base_amount") or 0.0),
        "amount": amount,
        "total": amount,                    # keep both for compatibility with your JS
        "created_at": datetime.utcnow(),
    }
    return item, None

def _get_cart_doc(user_oid):
    doc = carts_col.find_one({"user_id": user_oid}, {"items": 1, "updated_at": 1})
    if not doc:
        doc = {"user_id": user_oid, "items": [], "updated_at": datetime.utcnow()}
        carts_col.insert_one(doc)
    return doc

@cart_api_bp.route("/api/cart", methods=["GET"])
def get_cart():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    # stringify _id for JSON
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/phone-order-eligibility", methods=["POST"])
def phone_order_eligibility():
    payload = request.get_json(silent=True) or {}
    phone = (payload.get("phone") or "").strip()
    allowed, message, has_history = is_phone_allowed(phone)
    return jsonify({
        "success": True,
        "allowed": allowed,
        "message": message if not allowed else "",
        "has_history": has_history,
    })

@cart_api_bp.route("/api/cart/add_bulk", methods=["POST"])
def add_bulk():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list) or not raw_items:
        return jsonify({"success": False, "error": "No items"}), 400

    normalized = []
    for r in raw_items:
        item, err = _normalize_item(r)
        if err:
            return jsonify({"success": False, "error": err}), 400
        normalized.append(item)

    carts_col.update_one(
        {"user_id": user_oid},
        {"$push": {"items": {"$each": normalized}}, "$set": {"updated_at": datetime.utcnow()}},
        upsert=True,
    )

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/replace", methods=["POST"])
def replace_cart():
    """
    Replace entire cart with given items array (used by client sync).
    """
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    raw_items = payload.get("items") or []
    if not isinstance(raw_items, list):
        return jsonify({"success": False, "error": "Invalid items"}), 400

    normalized = []
    for r in raw_items:
        item, err = _normalize_item(r)
        if err:
            return jsonify({"success": False, "error": err}), 400
        normalized.append(item)

    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": normalized, "updated_at": datetime.utcnow()}},
        upsert=True,
    )

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/remove", methods=["POST"])
def remove_item():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    item_id = payload.get("item_id")
    if not item_id:
        return jsonify({"success": False, "error": "item_id required"}), 400

    try:
        carts_col.update_one(
            {"user_id": user_oid},
            {"$pull": {"items": {"_id": ObjectId(item_id)}}, "$set": {"updated_at": datetime.utcnow()}},
        )
    except Exception:
        return jsonify({"success": False, "error": "Invalid item_id"}), 400

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    for it in items:
        it["_id"] = str(it["_id"])
    return jsonify({"success": True, "items": items, "count": len(items)})

@cart_api_bp.route("/api/cart/clear", methods=["POST"])
def clear_cart():
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": [], "updated_at": datetime.utcnow()}},
        upsert=True,
    )
    return jsonify({"success": True, "items": [], "count": 0})

@cart_api_bp.route("/api/cart/checkout_start", methods=["POST"])
def checkout_start():
    """
    Atomically snapshot cart items and clear them, returning the snapshot.
    Use this response as the immutable 'lockedCart' for payment.
    """
    user_oid = _ensure_customer()
    if not user_oid:
        return jsonify({"success": False, "error": "Unauthorized"}), 401

    doc = _get_cart_doc(user_oid)
    items = doc.get("items", [])
    blocked = first_blocked_phone([it.get("phone") for it in items if isinstance(it, dict)])
    if blocked:
        return jsonify({"success": False, "error": blocked["message"], "phone": blocked["phone"]}), 400
    total = 0.0
    out = []
    for it in items:
        amt = float(it.get("total", it.get("amount", 0)) or 0)
        total += amt
        out.append({
            "_id": str(it["_id"]),
            "serviceId": it.get("serviceId"),
            "serviceName": it.get("serviceName"),
            "phone": it.get("phone"),
            "value": it.get("value"),
            "value_obj": it.get("value_obj"),
            "base_amount": float(it.get("base_amount", 0) or 0),
            "amount": float(it.get("amount", amt) or amt),
            "total": float(it.get("total", amt) or amt),
        })

    # Clear the cart immediately
    carts_col.update_one(
        {"user_id": user_oid},
        {"$set": {"items": [], "updated_at": datetime.utcnow()}},
        upsert=True,
    )

    return jsonify({
        "success": True,
        "locked": out,
        "count": len(out),
        "total": round(total, 2)
    })
