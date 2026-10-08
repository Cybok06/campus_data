# admin_sidebar.py
from flask import Blueprint, session
from db import db

admin_sidebar_bp = Blueprint("admin_sidebar", __name__)

orders_col = db["orders"]
complaints_col = db["complaints"]   # change if your collection name differs


def _is_admin() -> bool:
    return session.get("role") == "admin"


@admin_sidebar_bp.app_context_processor
def inject_admin_counts():
    """
    Inject counts into all templates. If not an admin, expose zeros so templates stay safe.
    """
    if not _is_admin():
        return {
            "pending_orders_count": 0,
            "undelivered_orders_count": 0,
            "pending_complaints_count": 0,
        }

    # Pending orders (if you use it anywhere else)
    try:
        pending_orders = orders_col.count_documents({"status": "pending"})
    except Exception:
        pending_orders = 0

    # Orders that are pending or processing
    try:
        undelivered_orders = orders_col.count_documents({"status": {"$in": ["pending", "processing"]}})
    except Exception:
        undelivered_orders = 0

    # Complaints that are pending
    try:
        pending_complaints = complaints_col.count_documents({"status": "pending"})
    except Exception:
        pending_complaints = 0

    return {
        "pending_orders_count": pending_orders,
        "undelivered_orders_count": undelivered_orders,
        "pending_complaints_count": pending_complaints,
    }
