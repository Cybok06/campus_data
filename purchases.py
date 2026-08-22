from flask import Blueprint, render_template, session, redirect, url_for
from bson.objectid import ObjectId
from db import db

purchases_bp = Blueprint("purchases", __name__)

purchase_history_col = db["purchase_history"]

@purchases_bp.route("/purchases")
def view_purchases():
    if "user_id" not in session:
        return redirect(url_for("login.login"))

    user_id = str(session["user_id"])  # stored as string in purchase_history
    purchases = list(
        purchase_history_col.find({"user_id": user_id}).sort("purchased_at", -1)
    )

    return render_template("purchases.html", purchases=purchases)
