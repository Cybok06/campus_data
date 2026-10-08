from flask import Blueprint, render_template, session, redirect, url_for, request
from bson import ObjectId
from datetime import datetime, timedelta
from db import db

transactions_bp   = Blueprint("transactions", __name__)
transactions_col  = db["transactions"]

def _sum_amount(match):
    """Aggregate helper to sum 'amount' with a $match stage."""
    pipeline = [
        {"$match": match},
        {"$group": {"_id": None, "total": {"$sum": {"$toDouble": "$amount"}}}}
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return float(agg[0]["total"]) if agg else 0.0

@transactions_bp.route("/customer/transactions")
def view_transactions():
    if session.get("role") != "customer":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    uid = ObjectId(user_id)

    range_preset = (request.args.get("range") or "").strip().lower()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    gateway = (request.args.get("gateway") or "").strip().lower()

    # Time windows (UTC, aligning with how verified_at/created_at are stored)
    now = datetime.utcnow()
    start_today = datetime(now.year, now.month, now.day)
    end_today = start_today + timedelta(days=1)

    range_start = None
    range_end = None
    if range_preset in ("today", "yesterday", "last7"):
        if range_preset == "today":
            range_start = start_today
            range_end = end_today
        elif range_preset == "yesterday":
            range_start = start_today - timedelta(days=1)
            range_end = start_today
        else:
            range_start = start_today - timedelta(days=6)
            range_end = end_today
    else:
        if start_date:
            try:
                range_start = datetime.strptime(start_date, "%Y-%m-%d")
            except Exception:
                range_start = None
        if end_date:
            try:
                range_end = datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)
            except Exception:
                range_end = None

    # --- Totals ---
    kpi_range = {"$gte": start_today, "$lt": end_today}
    if range_start or range_end:
        kpi_range = {}
        if range_start:
            kpi_range["$gte"] = range_start
        if range_end:
            kpi_range["$lt"] = range_end

    # Total Topups: successful deposits verified in range
    k_total_topups_today = _sum_amount({
        "user_id": uid,
        "type": "deposit",
        "status": "success",
        "verified_at": kpi_range
    })

    # Total Sales: successful purchases verified in range
    k_total_sales_today = _sum_amount({
        "user_id": uid,
        "type": "purchase",
        "status": "success",
        "verified_at": kpi_range
    })

    # Lifetime Sales: successful purchases (all time)
    k_lifetime_sales = _sum_amount({
        "user_id": uid,
        "type": "purchase",
        "status": "success"
    })

    # Average Daily Sales (range or last 30 days)
    if range_start or range_end:
        avg_start = range_start or start_today
        avg_end = range_end or end_today
        days = max(1, (avg_end - avg_start).days)
        sales_range = _sum_amount({
            "user_id": uid,
            "type": "purchase",
            "status": "success",
            "verified_at": {"$gte": avg_start, "$lt": avg_end}
        })
        k_avg_daily_sales = round(sales_range / float(days), 2)
    else:
        start_30 = start_today - timedelta(days=29)  # 30-day window
        sales_last_30 = _sum_amount({
            "user_id": uid,
            "type": "purchase",
            "status": "success",
            "verified_at": {"$gte": start_30, "$lt": end_today}
        })
        k_avg_daily_sales = round(sales_last_30 / 30.0, 2)

    # Refunds: successful refunds verified in range (if you record refunds)
    k_refunds_today = _sum_amount({
        "user_id": uid,
        "type": "refund",
        "status": "success",
        "verified_at": kpi_range
    })

    query = {"user_id": uid}
    if range_start or range_end:
        date_filter = {}
        if range_start:
            date_filter["$gte"] = range_start
        if range_end:
            date_filter["$lt"] = range_end
        if date_filter:
            query["verified_at"] = date_filter
    if gateway:
        query["$or"] = [
            {"gateway": gateway},
            {"source": gateway},
        ]

    try:
        page = int(request.args.get("page", 1))
    except Exception:
        page = 1
    page = max(page, 1)
    per_page = 5
    total_txns = transactions_col.count_documents(query)
    total_pages = max((total_txns + per_page - 1) // per_page, 1)
    if page > total_pages:
        page = total_pages
    skip = (page - 1) * per_page

    # Transactions list (newest first). Use verified_at, then created_at as fallback in template.
    transactions = list(
        transactions_col.find(query).sort([("verified_at", -1), ("created_at", -1)]).skip(skip).limit(per_page)
    )

    gateways_raw = transactions_col.distinct("gateway", {"user_id": uid})
    sources_raw = transactions_col.distinct("source", {"user_id": uid})
    gateways = sorted({g for g in (gateways_raw + sources_raw) if g})

    return render_template(
        "transactions.html",
        transactions=transactions,
        gateways=gateways,
        selected_gateway=gateway,
        range_preset=range_preset,
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        k_total_topups_today=round(k_total_topups_today, 2),
        k_total_sales_today=round(k_total_sales_today, 2),
        k_lifetime_sales=round(k_lifetime_sales, 2),
        k_avg_daily_sales=round(k_avg_daily_sales, 2),
        k_refunds_today=round(k_refunds_today, 2),
    )
