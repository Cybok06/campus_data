from flask import Blueprint, render_template, session, redirect, url_for
from bson.objectid import ObjectId
from db import db

admin_purchases_bp = Blueprint("admin_purchases", __name__)

purchase_history_col = db["purchase_history"]
users_col = db["users"]

@admin_purchases_bp.route("/admin/purchases")
def view_all_purchases():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    # Fetch all purchases
    purchases = list(purchase_history_col.find().sort("purchased_at", -1))

    # Attach customer details
    for p in purchases:
        user = users_col.find_one({"_id": ObjectId(p["user_id"])})
        p["customer_name"] = user.get("username", "Unknown") if user else "Unknown"
        p["customer_email"] = user.get("email", "N/A") if user else "N/A"
        p["customer_phone"] = user.get("phone", "N/A") if user else "N/A"

    return render_template("admin_purchases.html", purchases=purchases)
