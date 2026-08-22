from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from db import db
from datetime import datetime, timedelta

admin_transactions_bp = Blueprint("admin_transactions", __name__)

transactions_col = db["transactions"]
users_col = db["users"]


@admin_transactions_bp.route("/admin/transactions")
def admin_view_transactions():
    # Auth
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    customer_id = (request.args.get("customer") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    range_preset = (request.args.get("range") or "").strip().lower()
    gateway = (request.args.get("gateway") or "").strip().lower()

    # pagination
    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)

    per_page = 20

    query = {}

    # Filter by customer
    if customer_id:
        try:
            query["user_id"] = ObjectId(customer_id)
        except Exception:
            flash("Invalid customer selected.", "warning")

    # Date range filter (verified_at)
    verified_filter = {}
    now = datetime.utcnow()
    start_dt = None
    end_dt = None

    if range_preset in ("today", "yesterday", "last7"):
        today = datetime(now.year, now.month, now.day)
        if range_preset == "today":
            start_dt = today
            end_dt = today + timedelta(days=1)
        elif range_preset == "yesterday":
            start_dt = today - timedelta(days=1)
            end_dt = today
        else:
            start_dt = today - timedelta(days=6)
            end_dt = today + timedelta(days=1)
    else:
        if start_date:
            try:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            except Exception:
                flash("Invalid start date.", "warning")
        if end_date:
            try:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            except Exception:
                flash("Invalid end date.", "warning")

    if start_dt:
        verified_filter["$gte"] = start_dt
    if end_dt:
        verified_filter["$lt"] = end_dt
    if verified_filter:
        query["verified_at"] = verified_filter

    if gateway:
        query["$or"] = [
            {"gateway": gateway},
            {"source": gateway},
        ]

    # Count
    total_txns = transactions_col.count_documents(query)
    total_pages = max((total_txns + per_page - 1) // per_page, 1)

    # Clamp page to range (prevents dead pages when filters reduce results)
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    # Fetch transactions
    transactions = list(
        transactions_col.find(query)
        .sort("verified_at", -1)
        .skip(skip)
        .limit(per_page)
    )

    # Load customers for dropdown
    customers = list(users_col.find({"role": "customer"}).sort("first_name", 1))
    gateways_raw = transactions_col.distinct("gateway")
    sources_raw = transactions_col.distinct("source")
    gateways = sorted({g for g in (gateways_raw + sources_raw) if g})

    # Attach user info efficiently
    user_ids = [t.get("user_id") for t in transactions if t.get("user_id")]
    users_map = {}
    if user_ids:
        for u in users_col.find({"_id": {"$in": list(set(user_ids))}}):
            users_map[u["_id"]] = u

    for txn in transactions:
        txn["user"] = users_map.get(txn.get("user_id"), {}) or {}

    return render_template(
        "admin_transactions.html",
        transactions=transactions,
        customers=customers,
        selected_customer=customer_id,
        start_date=start_date,
        end_date=end_date,
        selected_gateway=gateway,
        range_preset=range_preset,
        gateways=gateways,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
    )
