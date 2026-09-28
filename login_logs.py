# login_logs.py
from flask import Blueprint, render_template, request, session, redirect, url_for
from db import db
from datetime import datetime, timedelta
from typing import Dict, Any, List
import math

login_logs_bp = Blueprint("login_logs", __name__)
login_logs_col = db["login_logs"]


def _parse_ymd(s: str):
    """Parse 'YYYY-MM-DD' -> datetime (naive UTC). Returns None on failure."""
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d")
    except Exception:
        return None


def _build_date_filter(start_date: str | None, end_date: str | None) -> Dict[str, Any]:
    """
    Build a Mongo filter on created_at using inclusive start (>=) and exclusive end (< next day).
    """
    filt: Dict[str, Any] = {}
    start_dt = _parse_ymd(start_date) if start_date else None
    end_dt = _parse_ymd(end_date) if end_date else None

    # If both provided and out of order, swap
    if start_dt and end_dt and start_dt > end_dt:
        start_dt, end_dt = end_dt, start_dt

    if start_dt and end_dt:
        filt["created_at"] = {"$gte": start_dt, "$lt": end_dt + timedelta(days=1)}
    elif start_dt:
        filt["created_at"] = {"$gte": start_dt}
    elif end_dt:
        filt["created_at"] = {"$lt": end_dt + timedelta(days=1)}

    return filt


@login_logs_bp.route("/admin/login-logs")
def view_login_logs():
    # Admin-only
    if not session.get("admin_logged_in"):
        return redirect(url_for("login.login"))

    # Query params
    start_date = (request.args.get("start_date") or "").strip() or None
    end_date = (request.args.get("end_date") or "").strip() or None
    page = request.args.get("page", "1")
    per_page = request.args.get("per_page", "20")

    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    try:
        per_page = min(100, max(5, int(per_page)))  # clamp 5..100
    except Exception:
        per_page = 20

    filt = _build_date_filter(start_date, end_date)

    # Count then fetch
    try:
        total_count = login_logs_col.count_documents(filt)
    except Exception:
        total_count = 0

    total_pages = max(1, math.ceil(total_count / per_page))
    if page > total_pages:
        page = total_pages

    skip = (page - 1) * per_page

    try:
        logs = list(
            login_logs_col.find(filt)
            .sort("created_at", -1)
            .skip(skip)
            .limit(per_page)
        )
    except Exception:
        logs = []

    # Build a small window of page numbers around current (do it in Python to avoid Jinja max/min)
    start_win = 1 if page <= 3 else page - 2
    end_win = min(total_pages, start_win + 4)
    start_win = max(1, end_win - 4)
    page_numbers: List[int] = list(range(start_win, end_win + 1))

    return render_template(
        "login_logs.html",
        logs=logs,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        page_numbers=page_numbers,
        start_date=start_date or "",
        end_date=end_date or "",
    )
