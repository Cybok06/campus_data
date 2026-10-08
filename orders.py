# routes/orders.py
from flask import Blueprint, render_template, session, redirect, url_for, request
from bson import ObjectId, Regex
from db import db
from datetime import datetime, timedelta
import math

orders_bp = Blueprint("orders", __name__)
orders_col = db["orders"]

def _parse_ymd(s: str):
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")

@orders_bp.route("/customer/orders")
def view_orders():
    # --- auth ---
    if session.get("role") != "customer":
        return redirect(url_for("login.login"))
    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))

    # ----- Filters -----
    status       = (request.args.get("status") or "all").strip().lower()
    start_date_s = (request.args.get("start_date") or "").strip()
    end_date_s   = (request.args.get("end_date") or "").strip()
    order_id_q   = (request.args.get("order_id") or "").strip()
    phone_q      = (request.args.get("phone") or "").strip()

    # pagination
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except ValueError:
        page = 1
    PER_PAGE = 10

    # --- build query ---
    base_query = {"user_id": ObjectId(user_id)}
    if status and status != "all":
        base_query["status"] = status

    # Date range
    date_filter = {}
    try:
        if start_date_s:
            date_filter["$gte"] = _parse_ymd(start_date_s)
        if end_date_s:
            date_filter["$lt"] = _parse_ymd(end_date_s) + timedelta(days=1)
    except Exception:
        date_filter = {}
    if date_filter:
        base_query["created_at"] = date_filter

    # Order ID search (partial, case-insensitive)
    if order_id_q:
        base_query["order_id"] = Regex(order_id_q, "i")

    # Phone search (within items[].phone)
    if phone_q:
        base_query["items.phone"] = Regex(phone_q, "i")

    query = base_query

    # --- counts + page data ---
    total_count = orders_col.count_documents(query)
    total_pages = max(math.ceil(total_count / PER_PAGE), 1)
    if page > total_pages:
        page = total_pages

    # fetch current page
    cursor = (
        orders_col.find(query)
        .sort("created_at", -1)
        .skip((page - 1) * PER_PAGE)
        .limit(PER_PAGE)
    )
    orders = list(cursor)

    # status list for dropdown (prioritized order)
    available_statuses = orders_col.distinct("status", {"user_id": ObjectId(user_id)}) or []
    preferred = ["processing", "delivered", "failed", "refunded", "pending", "completed"]
    ordered_statuses = [s for s in preferred if s in available_statuses]
    for s in available_statuses:
        if s not in ordered_statuses:
            ordered_statuses.append(s)

    # Pagination window for template
    window = 2
    start = max(page - window, 1)
    end = min(page + window, total_pages)
    page_numbers = list(range(start, end + 1))

    return render_template(
        "orders.html",
        orders=orders,
        page=page,
        per_page=PER_PAGE,
        total_count=total_count,
        total_pages=total_pages,
        page_numbers=page_numbers,
        # echo filters
        status=status,
        start_date=start_date_s,
        end_date=end_date_s,
        order_id_q=order_id_q,
        phone_q=phone_q,
        statuses=ordered_statuses
    )
