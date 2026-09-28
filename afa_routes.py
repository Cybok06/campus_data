# afa_routes.py
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from db import db
from datetime import datetime, timedelta
from bson import ObjectId
import re

afa_bp = Blueprint("afa", __name__)
afa_col = db["afa_registrations"]

PHONE_RE = re.compile(r"^0\d{9}$")  # 0xxxxxxxxx

def _current_customer_ids():
    """Return (raw_id, [both string id and ObjectId (if valid)]) for querying."""
    raw = session.get("user_id") or session.get("customer_id")
    if not raw:
        return None, []
    ids = [raw]
    try:
        ids.append(ObjectId(raw))
    except Exception:
        pass
    return raw, ids

@afa_bp.route("/api/afa/register", methods=["POST"])
def afa_register():
    # require logged-in customer
    raw, ids = _current_customer_ids()
    if not raw:
        return jsonify(success=False, error="Unauthorized"), 401

    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    phone = re.sub(r"\D+", "", (data.get("phone") or ""))
    dob = (data.get("dob") or "").strip() or None
    location = (data.get("location") or "").strip() or None
    ghana_card = (data.get("ghana_card") or data.get("ghanaCard") or "").strip() or None

    if not name:
        return jsonify(success=False, error="Name is required"), 400
    if not PHONE_RE.match(phone):
        return jsonify(success=False, error="Phone must be 0xxxxxxxxx"), 400

    doc = {
        "customer_id": ids[-1] if ids else raw,   # prefer ObjectId if available
        "name": name,
        "phone": phone,                            # digits only
        "dob": dob,
        "location": location,
        "ghana_card": ghana_card,
        "amount": 2.00,                            # AFA Registration @ GHS 2.00
        "status": "pending",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    res = afa_col.insert_one(doc)
    return jsonify(success=True, id=str(res.inserted_id), message="AFA registration submitted. Status: pending.")

@afa_bp.route("/api/afa/list", methods=["GET"])
def afa_list_api():
    raw, ids = _current_customer_ids()
    if not raw:
        return jsonify(success=False, error="Unauthorized"), 401

    q = (request.args.get("q") or "").strip()
    status = (request.args.get("status") or "").strip().lower()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()

    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    try:
        page_size = int(request.args.get("page_size", 10))
    except Exception:
        page_size = 10
    page_size = max(1, min(page_size, 100))

    query = {"customer_id": {"$in": ids}} if ids else {"customer_id": raw}
    if status:
        query["status"] = status

    if q:
        rx = re.compile(re.escape(q), re.I)
        query["$or"] = [
            {"name": rx},
            {"phone": rx},
            {"ghana_card": rx},
            {"location": rx},
        ]

    if date_from or date_to:
        rng = {}
        try:
            if date_from:
                rng["$gte"] = datetime.strptime(date_from, "%Y-%m-%d")
            if date_to:
                rng["$lt"] = datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1)
        except Exception:
            pass
        if rng:
            query["created_at"] = rng

    total = afa_col.count_documents(query)
    cursor = (afa_col.find(query)
              .sort([("created_at", -1)])
              .skip((page - 1) * page_size)
              .limit(page_size))

    items = []
    for d in cursor:
        created = d.get("created_at")
        items.append({
            "id": str(d.get("_id")),
            "name": d.get("name"),
            "phone": d.get("phone"),
            "ghana_card": d.get("ghana_card"),
            "dob": d.get("dob"),
            "location": d.get("location"),
            "amount": float(d.get("amount", 0)),
            "status": (d.get("status") or "pending"),
            "created_at": created.isoformat() if created else None,
            "created_at_display": created.strftime("%d %b %Y, %I:%M %p") if created else ""
        })

    return jsonify(success=True, items=items, total=total, page=page, page_size=page_size)

@afa_bp.route("/customer/afa", methods=["GET"])
def afa_list_page():
    # gate with your login if needed
    if not (session.get("user_id") or session.get("customer_id")):
        return redirect(url_for("login.login"))
    return render_template("afa_list.html")
