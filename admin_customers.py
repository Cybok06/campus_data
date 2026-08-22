# admin_customers.py
from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from db import db
from urllib.parse import urlencode
from bson import ObjectId
import math
import re
from werkzeug.security import generate_password_hash
from datetime import datetime

admin_customers_bp = Blueprint("admin_customers", __name__)
users_col = db["users"]

def _require_admin_json():
    if session.get("role") != "admin":
        return False, (jsonify({"status": "error", "message": "Unauthorized"}), 403)
    return True, None

def _require_admin_redirect():
    return session.get("role") == "admin"

def _to_object_id(hex_id: str):
    try:
        return ObjectId(hex_id)
    except Exception:
        return None

# View Customers Page
@admin_customers_bp.route("/admin/customers")
def view_customers():
    if not _require_admin_redirect():
        return redirect(url_for("login.login"))

    # --- Filters ---
    q = (request.args.get("q") or "").strip()
    referral = (request.args.get("referral") or "").strip()
    has_whatsapp = request.args.get("has_whatsapp")
    has_email = request.args.get("has_email")
    status = (request.args.get("status") or "").strip().lower()  # 'active' | 'blocked' | ''

    page = max(int(request.args.get("page", 1) or 1), 1)
    per_page = 15

    # Build query
    conditions = [{"role": "customer"}]

    # Exclude deleted by default
    conditions.append({"$or": [{"deleted": {"$exists": False}}, {"deleted": False}]})

    if q:
        regex = {"$regex": re.escape(q), "$options": "i"}
        conditions.append({
            "$or": [
                {"first_name": regex},
                {"last_name": regex},
                {"username": regex},
                {"email": regex},
                {"phone": regex},
                {"business_name": regex},
                {"whatsapp": regex},
                {"referral": regex},
            ]
        })

    if referral:
        conditions.append({"referral": {"$regex": re.escape(referral), "$options": "i"}})

    if has_whatsapp == "1":
        conditions.append({"whatsapp": {"$exists": True, "$ne": ""}})
    elif has_whatsapp == "0":
        conditions.append({"$or": [
            {"whatsapp": {"$exists": False}},
            {"whatsapp": ""},
            {"whatsapp": None},
        ]})

    if has_email == "1":
        conditions.append({"email": {"$exists": True, "$ne": ""}})
    elif has_email == "0":
        conditions.append({"$or": [
            {"email": {"$exists": False}},
            {"email": ""},
            {"email": None},
        ]})

    # IMPORTANT: treat missing status as "active" for filtering
    if status == "blocked":
        conditions.append({"status": "blocked"})
    elif status == "active":
        conditions.append({"$or": [
            {"status": "active"},
            {"status": {"$exists": False}}
        ]})

    query = {"$and": conditions} if len(conditions) > 1 else conditions[0]

    total = users_col.count_documents(query)
    total_pages = max(math.ceil(total / per_page), 1)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    customers = list(
        users_col.find(query)
        .sort([("_id", -1)])
        .skip(skip)
        .limit(per_page)
    )

    # Base query string for pagination
    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)
    base_qs = urlencode(qs)

    return render_template(
        "admin_customers.html",
        customers=customers,
        q=q,
        referral=referral,
        has_whatsapp=has_whatsapp,
        has_email=has_email,
        status=status,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
        base_qs=base_qs
    )

# Update Customer API (AJAX)
@admin_customers_bp.route("/admin/customers/update/<customer_id>", methods=["POST"])
def update_customer(customer_id):
    if session.get("role") != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    data = {k: v.strip() for k, v in request.form.items() if isinstance(v, str) and v.strip()}

    customer_stage = (request.form.get("customer_stage") or "").strip()
    if customer_stage:
        if customer_stage not in {"Normal", "Elite", "Premium"}:
            return jsonify({"status": "error", "message": "Invalid customer stage"}), 400
        data["customer_stage"] = customer_stage

    # Handle password hashing if provided
    if "password" in data and data["password"]:
        data["password"] = generate_password_hash(data["password"])
    else:
        data.pop("password", None)

    # Protect fields
    for key in ("_id", "role", "deleted", "deleted_at"):
        data.pop(key, None)

    res = users_col.update_one({"_id": oid, "role": "customer"}, {"$set": data})
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Customer updated successfully" if res.modified_count else "No changes detected"
    })

# === Block / Unblock with audit & boolean flag ===
@admin_customers_bp.route("/admin/customers/toggle_block/<customer_id>", methods=["POST"])
def toggle_block(customer_id):
    ok, resp = _require_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    payload = request.get_json(silent=True) or {}
    block = bool(payload.get("block", False))

    user = users_col.find_one({"_id": oid, "role": "customer"})
    if not user:
        return jsonify({"status": "error", "message": "Customer not found"}), 404

    now = datetime.utcnow()
    new_status = "blocked" if block else "active"

    update_doc = {
        "$set": {
            "status": new_status,
            "is_blocked": bool(block),
            "status_updated_at": now
        },
        "$push": {
            "status_history": {
                "at": now,
                "by": session.get("admin_id") or session.get("user_id"),
                "action": "toggle_block",
                "to": new_status
            }
        }
    }

    res = users_col.update_one({"_id": oid}, update_doc)

    return jsonify({
        "status": "success",
        "message": "Customer blocked" if block else "Customer unblocked",
        "new_status": new_status,
        "modified": int(bool(res.modified_count))
    })

# === Delete (Soft by default, optional hard delete) ===
@admin_customers_bp.route("/admin/customers/delete/<customer_id>", methods=["POST"])
def delete_customer(customer_id):
    ok, resp = _require_admin_json()
    if not ok:
        return resp

    oid = _to_object_id(customer_id)
    if not oid:
        return jsonify({"status": "error", "message": "Invalid customer id"}), 400

    payload = request.get_json(silent=True) or {}
    hard = bool(payload.get("hard", False))

    user = users_col.find_one({"_id": oid, "role": "customer"})
    if not user:
        return jsonify({"status": "error", "message": "Customer not found"}), 404

    if hard:
        # Permanent delete (use with care; consider cascading in your app if needed)
        res = users_col.delete_one({"_id": oid})
        return jsonify({
            "status": "success" if res.deleted_count else "noop",
            "message": "Customer permanently deleted" if res.deleted_count else "No action taken",
            "hard": True
        })

    # Soft delete (recommended)
    now = datetime.utcnow()
    res = users_col.update_one(
        {"_id": oid},
        {
            "$set": {
                "deleted": True,
                "deleted_at": now,
                "status": "deleted"
            },
            "$push": {
                "status_history": {
                    "at": now,
                    "by": session.get("admin_id") or session.get("user_id"),
                    "action": "delete",
                    "to": "deleted"
                }
            }
        }
    )
    return jsonify({
        "status": "success" if res.modified_count else "noop",
        "message": "Customer deleted (soft)" if res.modified_count else "No action taken",
        "hard": False
    })
