# routes/customer_profile.py
from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime  # ✅ FIXED: this import was missing
from db import db

customer_profile_bp = Blueprint("customer_profile", __name__)
users_col = db["users"]

@customer_profile_bp.route("/customer/profile", methods=["GET", "POST"])
def customer_profile():
    if session.get("role") != "customer":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    customer = users_col.find_one({"_id": ObjectId(user_id)})
    if not customer:
        flash("❌ Customer not found.", "danger")
        return redirect(url_for("login.login"))

    # Handle password update
    if request.method == "POST":
        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(customer["password"], current_password):
            flash("❌ Current password is incorrect.", "danger")
            return redirect(url_for("customer_profile.customer_profile"))

        if new_password != confirm_password:
            flash("❌ New passwords do not match.", "danger")
            return redirect(url_for("customer_profile.customer_profile"))

        # Update password
        users_col.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": {
                "password": generate_password_hash(new_password),
                "updated_at": datetime.utcnow()
            }}
        )
        flash("✅ Password updated successfully.", "success")
        return redirect(url_for("customer_profile.customer_profile"))

    return render_template("customer_profile.html", customer=customer)
