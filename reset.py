# reset.py
from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, flash
from bson import ObjectId, errors as bson_errors
from datetime import datetime, timedelta
from db import db
from werkzeug.security import generate_password_hash
import os, hashlib, base64

reset_bp = Blueprint("reset", __name__, template_folder="templates")

users_col = db["users"]
password_resets_col = db["password_resets"]

# ---- Optional indexes (run once on import) ----
try:
    password_resets_col.create_index("token_hash", unique=True)
    password_resets_col.create_index([("user_id", 1), ("used_at", 1)])
    password_resets_col.create_index("expires_at")
except Exception:
    # Index creation failures shouldn't crash the app
    pass

# ===== Helpers =====
def _require_admin():
    return session.get("role") == "admin"

def _now():
    return datetime.utcnow()

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

def _make_token(nbytes: int = 32) -> str:
    # URL-safe token (no padding)
    return base64.urlsafe_b64encode(os.urandom(nbytes)).decode("utf-8").rstrip("=")

def _parse_user_id() -> ObjectId:
    """
    Accepts user_id from:
      - form (multipart/form-data or x-www-form-urlencoded)
      - querystring
      - JSON body
    Returns a valid ObjectId or raises ValueError.
    """
    payload = request.get_json(silent=True) or {}
    raw = (request.values.get("user_id")
           or payload.get("user_id")
           or "").strip()
    if not raw:
        raise ValueError("user_id is required")
    try:
        return ObjectId(raw)
    except bson_errors.InvalidId:
        raise ValueError("user_id is not a valid ObjectId")

def _parse_ttl_hours(default=24) -> int:
    payload = request.get_json(silent=True) or {}
    raw = (request.values.get("ttl_hours") or payload.get("ttl_hours") or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return max(1, val)  # minimum 1 hour
    except Exception:
        return default

# ===== Admin: Generate a one-time reset link =====
@reset_bp.route("/admin/reset/generate", methods=["POST"])
def admin_generate_reset():
    """
    Body (form/json):
      - user_id: Mongo _id of the customer (required)
      - ttl_hours (optional): validity window (default 24)
    Returns:
      JSON { status, reset_url, expires_at }
    """
    if not _require_admin():
        return jsonify({"status": "error", "message": "Unauthorized"}), 403

    try:
        user_oid = _parse_user_id()
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400

    user = users_col.find_one({"_id": user_oid, "role": "customer"})
    if not user:
        return jsonify({"status": "error", "message": "Customer not found"}), 404

    ttl_hours = _parse_ttl_hours(default=24)

    # Invalidate any previous unused tokens for this user (optional hardening)
    password_resets_col.update_many(
        {"user_id": user_oid, "used_at": None},
        {"$set": {"used_at": _now()}}
    )

    raw_token = _make_token()
    token_hash = _hash_token(raw_token)
    expires_at = _now() + timedelta(hours=ttl_hours)

    # Store only hash
    password_resets_col.insert_one({
        "user_id": user_oid,
        "token_hash": token_hash,
        "created_at": _now(),
        "expires_at": expires_at,
        "used_at": None
    })

    # Absolute URL to the reset page for the customer
    reset_url = url_for("reset.reset_form", token=raw_token, _external=True)
    return jsonify({
        "status": "success",
        "reset_url": reset_url,
        "expires_at": expires_at.isoformat()
    }), 200

# ===== Customer: View the reset page via token =====
@reset_bp.route("/reset/<token>", methods=["GET"])
def reset_form(token):
    token_hash = _hash_token(token)
    rec = password_resets_col.find_one({"token_hash": token_hash})
    if not rec:
        flash("This reset link is invalid.", "danger")
        return render_template("reset.html", invalid=True)

    if rec.get("used_at"):
        flash("This reset link has already been used.", "warning")
        return render_template("reset.html", invalid=True)

    if rec.get("expires_at") and rec["expires_at"] < _now():
        flash("This reset link has expired.", "warning")
        return render_template("reset.html", invalid=True)

    return render_template("reset.html", token=token, invalid=False)

# ===== Customer: Submit new password =====
@reset_bp.route("/reset/<token>", methods=["POST"])
def reset_apply(token):
    password = (request.form.get("password") or "").strip()
    confirm  = (request.form.get("confirm") or "").strip()

    if not password or not confirm:
        flash("Please fill all fields.", "danger")
        return redirect(url_for("reset.reset_form", token=token))

    if password != confirm:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("reset.reset_form", token=token))

    token_hash = _hash_token(token)
    rec = password_resets_col.find_one({"token_hash": token_hash})
    if not rec:
        flash("This reset link is invalid.", "danger")
        return render_template("reset.html", invalid=True)

    if rec.get("used_at"):
        flash("This reset link has already been used.", "warning")
        return render_template("reset.html", invalid=True)

    if rec.get("expires_at") and rec["expires_at"] < _now():
        flash("This reset link has expired.", "warning")
        return render_template("reset.html", invalid=True)

    users_col.update_one(
        {"_id": rec["user_id"]},
        {"$set": {"password": generate_password_hash(password)}}
    )

    password_resets_col.update_one(
        {"_id": rec["_id"]},
        {"$set": {"used_at": _now()}}
    )

    flash("Password updated successfully. You can now log in.", "success")
    return render_template("reset.html", completed=True, invalid=False)
