# routes/referral.py
from flask import Blueprint, render_template, session, redirect, url_for, flash, request
from bson.objectid import ObjectId
from datetime import datetime
import random, string
from db import db

referral_bp = Blueprint("referral", __name__)
referrals_col = db["referrals"]
users_col = db["users"]

def generate_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))

@referral_bp.route("/referral/invite")
def generate_invite():
    user_id = session.get("user_id")
    if not user_id:
        flash("Please login to access referral.", "warning")
        return redirect(url_for("login.login"))

    user = users_col.find_one({"_id": ObjectId(user_id)})
    if not user:
        flash("User not found", "danger")
        return redirect(url_for("login.login"))

    # Check if referral already exists
    existing = referrals_col.find_one({"user_id": ObjectId(user_id)})
    if existing:
        code = existing["ref_code"]
    else:
        code = generate_code()
        referrals_col.insert_one({
            "user_id": ObjectId(user_id),
            "ref_code": code,
            "created_at": datetime.utcnow()
        })

    # Build full link
    invite_link = url_for('signup.signup', ref=code, _external=True)

    return render_template("invite.html", invite_link=invite_link, code=code, user=user)
