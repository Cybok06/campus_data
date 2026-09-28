from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from db import db
from werkzeug.security import generate_password_hash
from datetime import datetime
from pymongo.errors import DuplicateKeyError
from bson.objectid import ObjectId
import re

signup_bp = Blueprint("signup", __name__)
users_col     = db["users"]
balances_col  = db["balances"]
referrals_col = db["referrals"]  # <-- for referral validation

# NOTE: No users_col.create_index(...) calls here to avoid deploy crashes.

def normalize_phone(raw: str) -> str:
    """Normalize Ghana numbers to '0XXXXXXXXX' where possible."""
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("233") and len(digits) >= 12:
        return "0" + digits[-9:]
    if len(digits) == 9:
        return "0" + digits
    if len(digits) == 10 and digits.startswith("0"):
        return digits
    return digits  # fallback

@signup_bp.route("/signup", methods=["GET", "POST"])
def signup():
    referral_code = (request.args.get("ref") or "").strip()

    if request.method == "POST":
        # ---- Collect & trim ----
        first_name   = (request.form.get("first_name") or "").strip()
        last_name    = (request.form.get("last_name") or "").strip()
        username     = (request.form.get("username") or "").strip()
        email        = (request.form.get("email") or "").strip().lower()
        phone        = (request.form.get("phone") or "").strip()
        business     = (request.form.get("business_name") or "").strip()
        whatsapp     = (request.form.get("whatsapp") or "").strip()
        referral     = ((request.form.get("referral") or "").strip()).upper()
        password     = request.form.get("password") or ""
        confirm_pw   = request.form.get("confirm_password") or ""

        # ---- Server-side 'all compulsory' checks ----
        missing = []
        for key, val, label in [
            ("first_name", first_name, "First name"),
            ("last_name", last_name, "Last name"),
            ("username", username, "Username"),
            ("email", email, "Email"),
            ("phone", phone, "Phone"),
            ("business_name", business, "Business name"),
            ("whatsapp", whatsapp, "WhatsApp"),
            ("referral", referral, "Referral code"),
            ("password", password, "Password"),
            ("confirm_password", confirm_pw, "Confirm password"),
        ]:
            if not val:
                missing.append(label)
        if missing:
            flash(f"❌ Missing required fields: {', '.join(missing)}", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        if password != confirm_pw:
            flash("❌ Passwords do not match", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        # Basic shape checks similar to your client rules
        if not re.fullmatch(r"^[a-zA-Z0-9._-]{3,}$", username):
            flash("❌ Invalid username format.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))
        if not re.fullmatch(r"^0\d{9}$", phone):
            flash("❌ Invalid Ghana phone number.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))
        if not re.fullmatch(r"^0\d{9}$", whatsapp):
            flash("❌ Invalid WhatsApp number.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        # ---- Referral must exist ----
        ref_doc = referrals_col.find_one({"ref_code": referral})
        if not ref_doc:
            flash("❌ Invalid referral code.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        phone_normalized = normalize_phone(phone)

        # ---- Pre-insert duplicate checks ----
        if users_col.find_one({"username": username}):
            flash("❌ Username already exists.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        if users_col.find_one({"email": email}):
            flash("❌ Email already exists.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        if phone_normalized and users_col.find_one({"phone_normalized": phone_normalized}):
            flash("❌ Phone number already exists.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        now = datetime.utcnow()
        new_user = {
            "first_name": first_name,
            "last_name": last_name,
            "username": username,
            "email": email,
            "phone": phone,                 # keep raw for display
            "phone_normalized": phone_normalized,
            "business_name": business,
            "whatsapp": whatsapp,
            "referral": referral,           # validated, uppercase
            "password": generate_password_hash(password),
            "role": "customer",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        }

        try:
            res = users_col.insert_one(new_user)
            user_id = res.inserted_id

            balances_col.insert_one({
                "user_id": user_id,
                "amount": 0.00,
                "currency": "GHS",
                "created_at": now,
                "updated_at": now,
            })

            # (Optional) increment signups on the referrer
            try:
                referrals_col.update_one({"_id": ref_doc["_id"]}, {"$inc": {"signups": 1}})
            except Exception:
                pass

        except DuplicateKeyError as e:
            try:
                kv = (e.details or {}).get("keyValue") or {}
                if "username" in kv:
                    msg = "❌ Username already exists."
                elif "email" in kv:
                    msg = "❌ Email already exists."
                elif "phone_normalized" in kv:
                    msg = "❌ Phone number already exists."
                else:
                    msg = "❌ That credential is already registered."
            except Exception:
                msg = "❌ That credential is already registered."
            users_col.delete_one({"username": username})
            flash(msg, "danger")
            return redirect(url_for("signup.signup", ref=referral_code))
        except Exception:
            flash("❌ Could not complete signup. Please try again.", "danger")
            return redirect(url_for("signup.signup", ref=referral_code))

        flash("✅ Account created successfully! You can now log in.", "success")
        return redirect(url_for("login.login"))

    # GET
    return render_template("signup.html", referral_code=referral_code)

# ---------- Lightweight API for live referral validation ----------
@signup_bp.route("/signup/api/referral/validate")
def api_validate_referral():
    code = ((request.args.get("code") or "").strip()).upper()
    if not code:
        return jsonify({"ok": False, "reason": "empty"})
    ok = referrals_col.find_one({"ref_code": code}, {"_id": 1}) is not None
    return jsonify({"ok": ok})
