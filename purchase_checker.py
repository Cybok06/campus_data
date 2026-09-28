from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from bson.objectid import ObjectId
from db import db
from datetime import datetime
import random

purchase_checker_bp = Blueprint("purchase_checker", __name__)

# Collections
wassce_col = db["wassce_checker"]
balances_col = db["balances"]
users_col = db["users"]
purchase_history_col = db["purchase_history"]

@purchase_checker_bp.route("/purchase_checker", methods=["GET", "POST"])
def purchase_checker():
    if "user_id" not in session:
        return redirect(url_for("login.login"))

    user_id = ObjectId(session["user_id"])
    balance_doc = balances_col.find_one({"user_id": user_id})
    balance = float(balance_doc["amount"]) if balance_doc else 0.0

    if request.method == "POST":
        checker_id = request.form.get("checker_id")
        checker = wassce_col.find_one({"_id": ObjectId(checker_id), "status": "not_sold"})
        if not checker:
            flash("Checker not available or already sold.", "danger")
            return redirect(url_for("purchase_checker.purchase_checker"))

        price = float(checker["amount"])

        # Check balance
        if balance < price:
            flash("Insufficient balance. Please top up.", "danger")
            return redirect("http://127.0.0.1:5000/deposit")  # Direct link to deposit page

        # Deduct from balance
        new_balance = balance - price
        balances_col.update_one(
            {"user_id": user_id},
            {"$set": {"amount": new_balance, "updated_at": datetime.utcnow()}}
        )

        # Mark checker as sold
        wassce_col.update_one(
            {"_id": ObjectId(checker_id)},
            {"$set": {
                "status": "sold",
                "sold_to": str(user_id),
                "sold_at": datetime.utcnow()
            }}
        )

        # Save to purchase history
        purchase_history_col.insert_one({
            "user_id": str(user_id),
            "checker_id": str(checker["_id"]),
            "type": checker.get("type", ""),
            "amount": price,
            "message": checker.get("message", ""),
            "purchased_at": datetime.utcnow()
        })

        flash("Purchase successful!", "success")
        return redirect(url_for("purchases.view_purchases"))  # Redirect to purchases page

    # On GET: show only ONE available checker for selected type
    selected_type = request.args.get("type")
    checkers = []
    if selected_type in ["wassce", "bece"]:
        unsold = list(wassce_col.find({"type": selected_type, "status": "not_sold"}))
        if unsold:
            checkers = [random.choice(unsold)]  # Pick 1 random unsold checker

    return render_template(
        "purchase_checker.html",
        balance=balance,
        checkers=checkers,
        selected_type=selected_type
    )
