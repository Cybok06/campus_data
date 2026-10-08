from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from bson.objectid import ObjectId
from db import db
from datetime import datetime

admin_wassce_checker_bp = Blueprint("admin_wassce_checker", __name__)
wassce_col = db["wassce_checker"]

@admin_wassce_checker_bp.route("/admin/wassce_checker", methods=["GET", "POST"])
def admin_wassce_checker():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    # Handle new checker creation
    if request.method == "POST" and request.form.get("action") == "add":
        message = request.form.get("message", "").strip()
        amount = request.form.get("amount")
        profit = request.form.get("profit")
        checker_type = request.form.get("type", "wassce").lower()

        if not message or not amount or not profit:
            flash("All fields are required.", "warning")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

        try:
            amount = float(amount)
            profit = float(profit)
        except ValueError:
            flash("Amount and Profit must be numeric.", "danger")
            return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

        wassce_col.insert_one({
            "message": message,
            "amount": amount,
            "profit": profit,
            "status": "not_sold",
            "type": checker_type,
            "created_at": datetime.utcnow()
        })

        flash(f"{checker_type.upper()} checker added successfully!", "success")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    # Handle update
    if request.method == "POST" and request.form.get("action") == "update":
        checker_id = request.form.get("checker_id")
        if checker_id:
            try:
                wassce_col.update_one(
                    {"_id": ObjectId(checker_id)},
                    {
                        "$set": {
                            "message": request.form.get("message", "").strip(),
                            "amount": float(request.form.get("amount")),
                            "profit": float(request.form.get("profit")),
                            "type": request.form.get("type", "").lower()
                        }
                    }
                )
                flash("Checker updated successfully!", "success")
            except Exception as e:
                flash(f"Error updating checker: {str(e)}", "danger")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    # Handle delete single
    if request.args.get("delete_id"):
        try:
            wassce_col.delete_one({"_id": ObjectId(request.args.get("delete_id"))})
            flash("Checker deleted successfully!", "success")
        except Exception as e:
            flash(f"Error deleting checker: {str(e)}", "danger")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    # Handle delete all sold
    if request.args.get("delete_sold") == "1":
        result = wassce_col.delete_many({"status": "sold"})
        flash(f"Deleted {result.deleted_count} sold checkers.", "info")
        return redirect(url_for("admin_wassce_checker.admin_wassce_checker"))

    # Filters from GET params
    filter_status = request.args.get("status")
    filter_type = request.args.get("type")

    query = {}
    if filter_status in ["sold", "not_sold"]:
        query["status"] = filter_status
    if filter_type in ["wassce", "bece"]:
        query["type"] = filter_type

    messages = list(wassce_col.find(query).sort("created_at", -1))

    return render_template(
        "admin_wassce_checker.html",
        messages=messages,
        selected_status=filter_status,
        selected_type=filter_type
    )
