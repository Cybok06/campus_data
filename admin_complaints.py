from flask import Blueprint, render_template, session, redirect, url_for, request, flash, send_file, jsonify
from bson import ObjectId
from datetime import datetime
from urllib.parse import urlencode
from io import BytesIO
import pandas as pd
import requests
from urllib.parse import quote

# PDF export deps
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet

from db import db

admin_complaints_bp = Blueprint("admin_complaints", __name__)
complaints_col = db["complaints"]
users_col = db["users"]
orders_col = db["orders"]

# ---- SMS config (same style as balances/payments) ----
ARKESEL_API_KEY = "b3dheEVqUWNyeVBuUGxDVWFxZ0E"  # move to env var in production
SENDER_ID = "CAMPUS DATA"  # requested sender name

# ---------------- Helpers ----------------
def _fmt_dt(dt):
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""

def _safe(c, *path, default=None):
    """Safely traverse nested keys/attrs in dict-like objects."""
    cur = c
    for p in path:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            return default
    return cur

def _normalize_phone(raw: str) -> str | None:
    """Normalize to 233XXXXXXXXX for Ghana MSISDN."""
    if not raw:
        return None
    p = raw.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None

def _send_sms(msisdn: str, message: str) -> str:
    """Send SMS via Arkesel; returns 'sent'|'failed'|'error'."""
    try:
        url = (
            "https://sms.arkesel.com/sms/api?action=send-sms"
            f"&api_key={ARKESEL_API_KEY}"
            f"&to={msisdn}"
            f"&from={quote(SENDER_ID)}"
            f"&sms={quote(message)}"
        )
        resp = requests.get(url, timeout=12)
        if resp.status_code == 200 and '"code":"ok"' in resp.text:
            return "sent"
        return "failed"
    except Exception:
        return "error"

def _service_offer_text(c: dict) -> str:
    """
    Build the label used in SMS like 'MTN 2GB' from service_name + offer.
    Fallbacks: offer only, service only, or 'service'.
    """
    service = (c.get("service_name") or "").strip()
    offer = (c.get("offer") or "").strip()
    if service and offer:
        return f"{service} {offer}"
    if offer:
        return offer
    if service:
        return service
    return "service"

def _resolve_store_slug(c: dict) -> str:
    slug = c.get("store_slug") or ""
    if slug:
        return slug
    order_ref = c.get("order_ref") or {}
    order_id = order_ref.get("order_id")
    order_oid = order_ref.get("_id")
    order_doc = None
    if order_id:
        order_doc = orders_col.find_one({"order_id": order_id}, {"store_slug": 1})
    if not order_doc and order_oid:
        order_doc = orders_col.find_one({"_id": order_oid}, {"store_slug": 1})
    return (order_doc or {}).get("store_slug") or ""

# ---------------- Routes ----------------
@admin_complaints_bp.route("/admin/complaints", methods=["GET"])
def admin_view_complaints():
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    status = (request.args.get("status") or "").strip()
    start_date = (request.args.get("start_date") or "").strip()
    end_date = (request.args.get("end_date") or "").strip()
    export_type = (request.args.get("export") or "").lower()
    try:
        page = int(request.args.get("page", 1))
        page = max(1, page)
    except Exception:
        page = 1
    per_page = 7

    query = {}
    if status:
        query["status"] = status

    # Date filtering: submitted_at
    date_cond = {}
    if start_date:
        try:
            date_cond["$gte"] = datetime.strptime(start_date, "%Y-%m-%d")
        except Exception:
            flash("Invalid start date format", "warning")
    if end_date:
        try:
            dt = datetime.strptime(end_date, "%Y-%m-%d")
            date_cond["$lte"] = dt
        except Exception:
            flash("Invalid end date format", "warning")
    if date_cond:
        query["submitted_at"] = date_cond

    if export_type in {"excel", "pdf"}:
        complaints = list(complaints_col.find(query).sort("submitted_at", -1))
    else:
        skip = (page - 1) * per_page
        complaints = list(
            complaints_col.find(query)
            .sort("submitted_at", -1)
            .skip(skip)
            .limit(per_page)
        )

    # Fetch users in bulk
    user_ids = list({c.get("user_id") for c in complaints if c.get("user_id")})
    users = {u["_id"]: u for u in users_col.find({"_id": {"$in": user_ids}})} if user_ids else {}

    # Normalize fields for template & export
    for c in complaints:
        u = users.get(c.get("user_id"), {})
        c["user"] = u
        if u:
            c["customer_name"] = f"{u.get('first_name', '')} {u.get('last_name', '')}".strip() or u.get("username", "")
            c["customer_phone"] = u.get("phone", "")
        else:
            c["customer_name"] = c.get("customer_name") or ""
            c["customer_phone"] = c.get("customer_phone") or ""
        c["submitted_at_str"] = _fmt_dt(c.get("submitted_at"))
        c["order_date_str"] = _fmt_dt(c.get("order_date"))
        c["payment_date_str"] = (
            c.get("payment_date_str")
            or _fmt_dt(c.get("payment_date_dt"))
            or _fmt_dt(c.get("payment_date"))
        )
        c["order_ref"] = c.get("order_ref", {}) or {}
        c["order_no_display"] = c.get("order_number_provided", "")
        c["store_slug"] = _resolve_store_slug(c)
        c["store_name"] = c.get("store_name") or ""
        ref = (c.get("paystack_reference") or "").strip()
        flagged_exists = bool(c.get("flagged_ref_exists"))
        flagged_order_id = c.get("flagged_ref_order_id") or ""
        if ref:
            order_doc = orders_col.find_one(
                {"store_slug": c["store_slug"], "paystack_reference": ref},
                {"order_id": 1},
            )
            if order_doc:
                flagged_exists = True
                flagged_order_id = order_doc.get("order_id") or flagged_order_id
        c["flagged_ref_exists"] = flagged_exists
        c["flagged_ref_order_id"] = flagged_order_id
        shots = c.get("screenshots") or {}
        c["screenshots"] = {
            "data_balance": shots.get("data_balance", ""),
            "phone_msisdn": shots.get("phone_msisdn", ""),
        }

    if export_type == "excel":
        return _export_complaints_to_excel(complaints)
    elif export_type == "pdf":
        return _export_complaints_to_pdf(complaints)

    total_count = complaints_col.count_documents(query)
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    base_query = {}
    if status:
        base_query["status"] = status
    if start_date:
        base_query["start_date"] = start_date
    if end_date:
        base_query["end_date"] = end_date
    filters_query = urlencode(base_query)

    return render_template(
        "admin_complaints.html",
        complaints=complaints,
        status_filter=status,
        start_date=start_date,
        end_date=end_date,
        page=page,
        per_page=per_page,
        total_pages=total_pages,
        total_count=total_count,
        filters_query=filters_query,
    )

def _export_complaints_to_excel(complaints):
    data = []
    for c in complaints:
        row = {
            "Customer": c.get("customer_name", ""),
            "Phone": c.get("customer_phone", ""),
            "Service": c.get("service_name", ""),
            "Offer": c.get("offer", ""),
            "Order Entered": c.get("order_no_display", ""),
            "Order _id": _safe(c, "order_ref", "_id", default=""),
            "Order order_no": _safe(c, "order_ref", "order_no", default=""),
            "Order order_id": _safe(c, "order_ref", "order_id", default=""),
            "Order Date": c.get("order_date_str", ""),
            "Payment Date": c.get("payment_date_str", ""),
            "Proof: Data Balance": _safe(c, "screenshots", "data_balance", default=c.get("image_path","")),
            "Proof: Phone MSISDN": _safe(c, "screenshots", "phone_msisdn", default=""),
            "Description (legacy)": c.get("description", ""),
            "WhatsApp (legacy)": c.get("whatsapp", ""),
            "Status": c.get("status", ""),
            "Submitted At": c.get("submitted_at_str", ""),
        }
        data.append(row)

    df = pd.DataFrame(data)
    output = BytesIO()
    with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
        df.to_excel(writer, index=False, sheet_name="Complaints")
    output.seek(0)
    return send_file(output, download_name="complaints.xlsx", as_attachment=True)

def _export_complaints_to_pdf(complaints):
    output = BytesIO()
    doc = SimpleDocTemplate(output, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=42, bottomMargin=36)
    elements = []
    styles = getSampleStyleSheet()
    elements.append(Paragraph("Customer Complaints Report", styles["Title"]))

    data = [["Customer", "Phone", "Service", "Offer", "Payment Date", "Status", "Submitted"]]
    for c in complaints:
        data.append([
            c.get("customer_name", ""),
            c.get("customer_phone", ""),
            c.get("service_name", ""),
            c.get("offer", ""),
            c.get("payment_date_str", ""),
            (c.get("status","") or "").capitalize(),
            c.get("submitted_at_str",""),
        ])

    table = Table(data, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.darkblue),
        ("TEXTCOLOR", (0,0), (-1,0), colors.whitesmoke),
        ("FONTSIZE", (0,0), (-1,-1), 9),
        ("ALIGN", (0,0), (-1,-1), "LEFT"),
        ("GRID", (0,0), (-1,-1), 0.25, colors.black),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.whitesmoke, colors.lightgrey]),
    ]))

    elements.append(table)
    doc.build(elements)
    output.seek(0)
    return send_file(output, download_name="complaints.pdf", as_attachment=True)

@admin_complaints_bp.route("/admin/complaints/<complaint_id>/open", methods=["GET"])
def open_complaint_store(complaint_id):
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    try:
        _id = ObjectId(complaint_id)
    except Exception:
        flash("Invalid complaint id.", "danger")
        return redirect(url_for("admin_complaints.admin_view_complaints"))

    c = complaints_col.find_one({"_id": _id})
    if not c:
        flash("Complaint not found.", "warning")
        return redirect(url_for("admin_complaints.admin_view_complaints"))

    store_slug = _resolve_store_slug(c)
    if not store_slug:
        flash("Missing store slug for this complaint.", "warning")
        return redirect(url_for("admin_complaints.admin_view_complaints"))

    return redirect(url_for(
        "stores.store_public_page",
        slug=store_slug,
        admin_override="1",
        complaint_id=str(_id),
    ))


@admin_complaints_bp.route("/api/admin/complaints/<complaint_id>/snapshot", methods=["GET"])
def admin_complaint_snapshot(complaint_id):
    if session.get("role") != "admin":
        return jsonify({"success": False, "message": "Admin login required"}), 401

    try:
        _id = ObjectId(complaint_id)
    except Exception:
        return jsonify({"success": False, "message": "Invalid complaint id"}), 400

    c = complaints_col.find_one({"_id": _id})
    if not c:
        return jsonify({"success": False, "message": "Complaint not found"}), 404

    store_slug = _resolve_store_slug(c)
    if not store_slug:
        return jsonify({"success": False, "message": "Store not found for complaint"}), 404

    return jsonify({
        "success": True,
        "store_slug": store_slug,
        "paystack_reference": c.get("paystack_reference"),
        "customer_phone": c.get("customer_phone") or "",
        "customer_name": c.get("customer_name") or "",
        "cart_snapshot": c.get("cart_snapshot") or [],
    })

@admin_complaints_bp.route("/admin/complaints/<complaint_id>/update", methods=["POST"])
def update_complaint_status(complaint_id):
    if session.get("role") != "admin":
        return redirect(url_for("login.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    # Added 'false' and 'rejected'
    allowed = {"pending", "resolved", "refund", "false", "rejected"}
    if new_status not in allowed:
        flash("Invalid status selected.", "warning")
        return redirect(url_for(
            "admin_complaints.admin_view_complaints",
            status=request.args.get("status") or request.form.get("status_filter") or "",
            start_date=request.args.get("start_date") or request.form.get("start_date") or "",
            end_date=request.args.get("end_date") or request.form.get("end_date") or ""
        ))

    # Validate id
    try:
        _id = ObjectId(complaint_id)
    except Exception:
        flash("Invalid complaint id.", "danger")
        return redirect(url_for(
            "admin_complaints.admin_view_complaints",
            status=request.args.get("status") or request.form.get("status_filter") or "",
            start_date=request.args.get("start_date") or request.form.get("start_date") or "",
            end_date=request.args.get("end_date") or request.form.get("end_date") or ""
        ))

    c = complaints_col.find_one({"_id": _id})
    if not c:
        flash("Complaint not found.", "warning")
        return redirect(url_for(
            "admin_complaints.admin_view_complaints",
            status=request.args.get("status") or request.form.get("status_filter") or "",
            start_date=request.args.get("start_date") or request.form.get("start_date") or "",
            end_date=request.args.get("end_date") or request.form.get("end_date") or ""
        ))

    # Update document
    update_doc = {
        "status": new_status,
        "updated_at": datetime.utcnow(),
        "updated_by": {
            "user_id": session.get("user_id"),
            "username": session.get("username") or session.get("email") or "admin"
        }
    }
    complaints_col.update_one({"_id": _id}, {"$set": update_doc})

    # ---- SMS on status transitions (resolved/refund only) ----
    sms_status = None
    if new_status in {"resolved", "refund"}:
        # fetch user phone
        user = users_col.find_one({"_id": c.get("user_id")}, {"phone": 1})
        msisdn = _normalize_phone((user or {}).get("phone", ""))
        if msisdn:
            service_text = _service_offer_text(c)
            if new_status == "resolved":
                message = f"your {service_text} complaint has been resolved"
            else:
                message = f"your {service_text} complaint has been approved for refund"
            sms_status = _send_sms(msisdn, message)
        else:
            sms_status = "invalid_phone"

    # Flash result
    ok_msg = f"Complaint status updated to {new_status}."
    if sms_status == "sent":
        ok_msg += " SMS sent."
    elif sms_status in ("failed", "error"):
        ok_msg += " (SMS delivery failed)"
    elif sms_status == "invalid_phone":
        ok_msg += " (Phone not valid for SMS)"
    flash(ok_msg, "success")

    # Redirect back to the list (preserve current filters if present)
    return redirect(url_for(
        "admin_complaints.admin_view_complaints",
        status=request.args.get("status") or request.form.get("status_filter") or "",
        start_date=request.args.get("start_date") or request.form.get("start_date") or "",
        end_date=request.args.get("end_date") or request.form.get("end_date") or ""
    ))
