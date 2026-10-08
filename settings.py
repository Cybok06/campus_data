from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from db import db
from datetime import datetime

settings_bp = Blueprint("settings", __name__)
api_col = db["API"]

@settings_bp.route("/admin/settings", methods=["GET", "POST"])
def manage_api():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    # Fetch current API key document (assuming only one stored)
    api_doc = api_col.find_one({"name": "paystack_secret"})

    if request.method == "POST":
        new_key = request.form.get("api_key", "").strip()
        if not new_key:
            flash("❌ API key cannot be empty.", "danger")
            return redirect(url_for("settings.manage_api"))

        if api_doc:
            # Update existing key
            api_col.update_one(
                {"_id": api_doc["_id"]},
                {"$set": {"key": new_key, "updated_at": datetime.utcnow()}}
            )
        else:
            # Create new key document
            api_col.insert_one({
                "name": "paystack_secret",
                "key": new_key,
                "created_at": datetime.utcnow(),
                "updated_at": datetime.utcnow()
            })

        flash("✅ API key updated successfully.", "success")
        return redirect(url_for("settings.manage_api"))

    return render_template("settings.html", api_key=api_doc["key"] if api_doc else "")
