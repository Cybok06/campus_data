# complaints.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from bson.objectid import ObjectId
from datetime import datetime
from werkzeug.utils import secure_filename
import os, time, uuid

from db import db

complaints_bp = Blueprint("complaints", __name__)
orders_col = db["orders"]
complaints_col = db["complaints"]

# === Uploads ===
UPLOAD_FOLDER = os.path.join(os.getcwd(), "uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
MAX_IMAGE_MB = 8  # hard cap per file

def _allowed_image(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS

def _filesize_ok(f) -> bool:
    # Some servers won’t populate content_length reliably for in‑memory streams; this guards typical cases.
    try:
        f.stream.seek(0, os.SEEK_END)
        size = f.stream.tell()
        f.stream.seek(0)
        return size <= MAX_IMAGE_MB * 1024 * 1024
    except Exception:
        # If we can’t measure, allow and rely on server’s MAX_CONTENT_LENGTH if set
        return True

def _save_image(file_storage, prefix: str) -> str:
    """Save image to uploads/ with a unique name; returns web path like /uploads/xxx.jpg"""
    original = secure_filename(file_storage.filename or "")
    ext = original.rsplit(".", 1)[1].lower() if "." in original else "jpg"
    unique_name = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:10]}.{ext}"
    fullpath = os.path.join(UPLOAD_FOLDER, unique_name)
    file_storage.save(fullpath)
    return f"/uploads/{unique_name}"

def _try_objectid(s: str):
    try:
        return ObjectId(s)
    except Exception:
        return None

def _find_order_for_user(user_id: ObjectId, order_number: str):
    """
    Attempts to find an order for this user by common identifiers:
    - order_no
    - order_id
    - _id (ObjectId string)
    """
    oid = _try_objectid(order_number)
    query = {
        "user_id": ObjectId(user_id),
        "$or": [{"order_no": order_number}, {"order_id": order_number}] + ([{"_id": oid}] if oid else [])
    }
    return orders_col.find_one(query)

@complaints_bp.route("/complaints", methods=["GET", "POST"])
def submit_complaint():
    """
    Form fields (POST):
      - order_number: str (required)
      - screenshot_balance: File (required)  -> proof of data balance
      - screenshot_msisdn:  File (required)  -> proof of phone number (MSISDN)
    """
    user_id = session.get("user_id")
    if not user_id:
        flash("You must be logged in to submit a complaint.", "danger")
        return redirect(url_for("login.login"))

    if request.method == "POST":
        order_number = (request.form.get("order_number") or "").strip()
        file_balance = request.files.get("screenshot_balance")
        file_msisdn = request.files.get("screenshot_msisdn")

        # --- Basic validation ---
        if not order_number:
            flash("Order number is required.", "danger")
            return redirect(url_for("complaints.submit_complaint"))

        if not file_balance or file_balance.filename == "":
            flash("Screenshot of data balance is required.", "danger")
            return redirect(url_for("complaints.submit_complaint"))

        if not file_msisdn or file_msisdn.filename == "":
            flash("Screenshot of phone number is required.", "danger")
            return redirect(url_for("complaints.submit_complaint"))

        # --- File validation ---
        for field_name, f in [("data balance", file_balance), ("phone number", file_msisdn)]:
            if not _allowed_image(f.filename):
                flash(f"Invalid {field_name} image type. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}.", "danger")
                return redirect(url_for("complaints.submit_complaint"))
            if not _filesize_ok(f):
                flash(f"The {field_name} screenshot is too large (>{MAX_IMAGE_MB}MB).", "danger")
                return redirect(url_for("complaints.submit_complaint"))

        # --- Find order for this user ---
        order = _find_order_for_user(ObjectId(user_id), order_number)
        if not order or not order.get("items"):
            flash("We couldn't find that order for your account.", "danger")
            return redirect(url_for("complaints.submit_complaint"))

        # Extract a few useful fields for quick admin triage
        item = order["items"][0]
        service_name = item.get("serviceName")
        offer = item.get("value")
        created_at = order.get("created_at")

        # --- Save images ---
        balance_path = _save_image(file_balance, "balance")
        msisdn_path = _save_image(file_msisdn, "msisdn")

        complaint_doc = {
            "user_id": ObjectId(user_id),
            "order_ref": {
                # Keep flexible keys to match whatever you store on orders
                "_id": order.get("_id"),
                "order_no": order.get("order_no"),
                "order_id": order.get("order_id"),
            },
            "service_name": service_name,
            "offer": offer,
            "order_date": created_at,
            "order_number_provided": order_number,  # exactly what user typed
            "screenshots": {
                "data_balance": balance_path,
                "phone_msisdn": msisdn_path,
            },
            "submitted_at": datetime.utcnow(),
            "status": "pending",
        }

        complaints_col.insert_one(complaint_doc)
        flash("✅ Complaint submitted successfully!", "success")
        return redirect(url_for("complaints.submit_complaint"))

    # GET – if you still want to pre-fill anything, you can render a minimal page.
    # (Template will now just show fields for order number + two uploads.)
    return render_template("complaints.html")

@complaints_bp.route("/view_complaints")
def view_complaints():
    user_id = session.get("user_id")
    if not user_id:
        flash("You must be logged in to view complaints.", "danger")
        return redirect(url_for("login.login"))

    status_filter = (request.args.get("status") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()

    query = {"user_id": ObjectId(user_id)}

    if status_filter:
        query["status"] = status_filter

    # Date filtering (submitted_at)
    date_cond = {}
    if start_date:
        try:
            date_cond["$gte"] = datetime.strptime(start_date, "%Y-%m-%d")
        except Exception:
            flash("Invalid start date format (use YYYY-MM-DD).", "warning")
    if end_date:
        try:
            # include the whole end day
            dt = datetime.strptime(end_date, "%Y-%m-%d")
            date_cond["$lte"] = dt
        except Exception:
            flash("Invalid end date format (use YYYY-MM-DD).", "warning")
    if date_cond:
        query["submitted_at"] = date_cond

    complaints = list(complaints_col.find(query).sort("submitted_at", -1))
    return render_template(
        "view_complaints.html",
        complaints=complaints,
        status_filter=status_filter,
        start_date=start_date,
        end_date=end_date,
    )
