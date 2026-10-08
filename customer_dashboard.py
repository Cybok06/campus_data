from flask import Blueprint, render_template, session, redirect, url_for, request, jsonify
from bson import ObjectId
from db import db
import json, ast, re
from datetime import datetime, date, timedelta
from typing import Optional, Any, Dict, List, Tuple  # add Tuple for 3.8/3.9

customer_dashboard_bp = Blueprint("customer_dashboard", __name__)

# --- Collections ---
services_col         = db["services"]
balances_col         = db["balances"]
orders_col           = db["orders"]
users_col            = db["users"]             # for display name
stores_col           = db["stores"]
store_accounts_col   = db["store_accounts"]
settings_col         = db["settings"]          # legacy AFA settings (price/open/stock)
afa_settings_col     = db["afa_settings"]      # primary AFA settings (price/open/stock)
afa_col              = db["afa_registrations"] # AFA registrations
balance_logs_col     = db["balance_logs"]      # wallet logs
transactions_col     = db["transactions"]

# ---------- helpers ----------
_NUM = re.compile(r"^\s*-?\d+(\.\d+)?\s*$", re.IGNORECASE)
_GB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*G(?:B|IG)?\b", re.IGNORECASE)
_MB  = re.compile(r"(\d+(?:\.\d+)?)[\s]*MB\b", re.IGNORECASE)
_MIN = re.compile(r"(\d+(?:\.\d+)?)[\s]*(?:MIN|MINS|MINUTE|MINUTES)\b", re.IGNORECASE)
_PKG_TAIL = re.compile(r"\s*\(Pkg\s*\d+\)\s*$", re.IGNORECASE)
_mapping_like = re.compile(r"^\s*\{.*\}\s*$", re.DOTALL)

def _now() -> datetime:
    return datetime.utcnow()

def _to_float(x: Any) -> Optional[float]:
    """
    Safely convert numbers, Mongo Extended JSON (e.g. {'$numberDouble':'15.0'}),
    strings like '15', etc. to float.
    """
    try:
        # Handle {"$numberDouble": "..."} or {"$numberInt": "..."}
        if isinstance(x, dict):
            for k in ("$numberDouble", "$numberInt", "$numberDecimal", "$numberLong"):
                if k in x:
                    return float(x[k])
        return float(x)
    except Exception:
        return None

# ---- unit helpers ------------------------------------------------------------

def _service_unit(svc: Dict[str, Any]) -> str:
    """
    Returns the unit for a service:
      - 'minutes' for AFA talktime (by name or optional svc['unit']=='minutes')
      - 'data' (MB/GB) for everything else
    """
    unit = (svc.get("unit") or "").strip().lower()
    name = (svc.get("name") or "").strip().lower()
    if unit in ("min", "mins", "minute", "minutes"):
        return "minutes"
    if name == "afa talktime":
        return "minutes"
    return "data"

def _format_volume_unit(value: Optional[float], unit: str) -> str:
    if value is None:
        return "-"
    try:
        v = float(value)
    except Exception:
        return "-"
    if unit == "minutes":
        return f"{int(round(v))} mins"
    # default 'data': MB
    if v >= 1000:
        gb = v / 1000.0
        return f"{int(gb)}GB" if abs(gb - int(gb)) < 1e-9 else f"{gb:.2f}GB"
    return f"{int(v)}MB"

def _parse_value_field(value: Any) -> Any:
    """
    Accepts:
      - dict like {"id": 50, "volume": 20000}
      - Python-like string "{'id': 50, 'volume': 20000}"
      - raw string like "1GB" or "1000MB" or "250 MIN"
      - display string like "GHS 160 — 1GB (Pkg 2)"
    Returns either dict (preferred) or the original string.
    """
    if isinstance(value, dict) or value is None:
        return value
    if isinstance(value, str):
        vt = value.strip()
        if vt.startswith("{") and vt.endswith("}"):
            # try JSON first
            try:
                data = json.loads(vt)
                if isinstance(data, dict):
                    return data
            except Exception:
                # then tolerant Python-literal
                try:
                    if _mapping_like.match(vt):
                        data = ast.literal_eval(vt)
                        if isinstance(data, dict):
                            return data
                except Exception:
                    pass
        return vt
    return value

def _extract_volume(value: Any, unit: str) -> Optional[float]:
    """Return numeric volume for sorting (MB for data, minutes for talktime)."""
    if isinstance(value, dict):
        vol = value.get("volume")
        if vol is None:
            return None
        if isinstance(vol, (int, float)) or (_NUM.match(str(vol))):
            return float(vol)
        # textual volume
        vol_s = str(vol)
        if unit == "minutes":
            m = _MIN.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)
            return None
        else:
            m = _GB.search(vol_s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(vol_s)
            if m:
                return float(m.group(1))
            if _NUM.match(vol_s):
                return float(vol_s)  # assume MB
            return None

    if isinstance(value, str):
        s = value
        if unit == "minutes":
            m = _MIN.search(s)
            if m:
                return float(m.group(1))
            if _NUM.match(s):
                return float(s)
            s2 = _PKG_TAIL.sub("", s)
            m = _MIN.search(s2)
            if m:
                return float(m.group(1))
            return None
        else:
            m = _GB.search(s)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s)
            if m:
                return float(m.group(1))
            s2 = _PKG_TAIL.sub("", s)
            m = _GB.search(s2)
            if m:
                return float(m.group(1)) * 1000.0
            m = _MB.search(s2)
            if m:
                return float(m.group(1))
            if _NUM.match(s2):
                return float(s2)  # assume MB
            return None
    return None

def _value_text_for_display(value: Any, unit: str) -> str:
    if isinstance(value, dict):
        vol = _extract_volume(value, unit)
        return _format_volume_unit(vol, unit) if vol is not None else "-"
    if isinstance(value, str):
        cleaned = _PKG_TAIL.sub("", value).strip()
        vol = _extract_volume(cleaned, unit)
        return _format_volume_unit(vol, unit) if vol is not None else (cleaned or "-")
    return value or "-"

# ---- stage pricing helpers --------------------------------------------------

def _norm_stage_key(stage: Optional[str]) -> str:
    s = (stage or "").strip().lower()
    if s in {"normal", "normal_agent", "normal agent"}:
        return "normal_agent"
    if s in {"elite", "elite_agent", "elite agent"}:
        return "elite_agent"
    if s in {"premium", "premium_agent"}:
        return "premium"
    return "normal_agent"

def _stage_price_from_offer(offer: Dict[str, Any], stage_key: str) -> Optional[float]:
    prices = offer.get("stage_prices")
    if not isinstance(prices, dict):
        return None

    if stage_key in prices:
        return _to_float(prices.get(stage_key))

    aliases = {
        "normal_agent": ("normal", "normal_agent", "normal agent"),
        "elite_agent": ("elite", "elite_agent", "elite agent"),
        "premium": ("premium", "premium_agent"),
    }.get(stage_key, ())

    lowered = {str(k).strip().lower(): v for k, v in prices.items()}
    for a in aliases:
        if a in lowered:
            return _to_float(lowered.get(a))
    return None

# ---- service ordering ----
PREFERRED_ORDER: List[str] = [
    "MTN",
    "AT - iShare",
    "AT - BigTime",
    "AFA TALKTIME",
]

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _name_rank(name: str) -> Optional[int]:
    n = _norm(name)
    for i, want in enumerate(PREFERRED_ORDER):
        if _norm(want) == n:
            return i
    n2 = " ".join(n.split())
    for i, want in enumerate(PREFERRED_ORDER):
        if " ".join(_norm(want).split()) == n2:
            return i
    return None

def _created_ts(service_doc: Dict[str, Any]) -> float:
    ca = service_doc.get("created_at")
    if isinstance(ca, datetime):
        return ca.timestamp()
    try:
        val = float(ca)
        if val > 1e12:
            return val / 1000.0
        return val
    except Exception:
        return 0.0

def _service_priority_tuple(svc: Dict[str, Any]):
    prio = _to_float(svc.get("priority"))
    prio = prio if prio is not None else float("inf")
    name = svc.get("name") or ""
    nrank = _name_rank(name)
    nrank = nrank if nrank is not None else 10_000
    display_order = _to_float(svc.get("display_order"))
    display_order = display_order if display_order is not None else float("inf")
    ts = -_created_ts(svc)
    alpha = _norm(name)
    return (prio, nrank, display_order, ts, alpha)

def _display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Customer"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    first = (user_doc.get("first_name") or "").strip()
    last  = (user_doc.get("last_name") or "").strip()
    if first or last:
        return (first + " " + last).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "Customer"

# ---- service-state helper ----------------------------------------------------

def _service_state(svc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize flags + derive if the service can be ordered.
    """
    t = (svc.get("type") or "API").upper()
    status = (svc.get("status") or "OPEN").upper()               # OPEN | CLOSED
    availability = (svc.get("availability") or "AVAILABLE").upper()  # AVAILABLE | OUT_OF_STOCK

    # optional custom messages stored on the service doc
    closed_msg = (svc.get("closed_message") or "This service is temporarily closed.")
    oos_msg = (svc.get("out_of_stock_message") or "This service is currently out of stock.")

    can_order = (t == "API" and status == "OPEN" and availability == "AVAILABLE")

    disabled_reason = None
    if not can_order:
        if status != "OPEN":
            disabled_reason = closed_msg
        elif availability != "AVAILABLE":
            disabled_reason = oos_msg
        elif t != "API":
            disabled_reason = "This service is currently unavailable."

    return {
        "type": t,
        "status": status,
        "availability": availability,
        "closed_message": closed_msg,
        "out_of_stock_message": oos_msg,
        "can_order": can_order,
        "disabled_reason": disabled_reason
    }

# ---------- AFA settings loader (price / open / stock) ----------

def _load_afa_settings() -> Dict[str, Any]:
    """
    Reads configurable AFA price/open/stock from afa_settings (primary),
    with a legacy fallback to db.settings. Does NOT mirror from any service.
    """
    defaults: Dict[str, Any] = {
        "price": 2.00,
        "is_open": True,
        "in_stock": True,
        "status": "OPEN",
        "availability": "AVAILABLE",
        "disabled_reason": "This service is currently unavailable."
    }

    # Preferred single-record document created/managed by admin_afa.py
    doc = afa_settings_col.find_one({"_id": "AFA_SETTINGS"})

    # Legacy fallback for older deployments
    if not doc:
        doc = settings_col.find_one({"key": "afa_settings"}) or settings_col.find_one({"key": "afa"})

    if doc:
        price = _to_float(doc.get("price"))
        if price is not None:
            defaults["price"] = price

        is_open = bool(doc.get("is_open", True))
        in_stock = bool(doc.get("in_stock", True))
        defaults["is_open"] = is_open
        defaults["in_stock"] = in_stock
        defaults["status"] = "OPEN" if is_open else "CLOSED"
        defaults["availability"] = "AVAILABLE" if in_stock else "OUT_OF_STOCK"

        if doc.get("disabled_reason"):
            defaults["disabled_reason"] = str(doc["disabled_reason"])

    # IMPORTANT: No reflection from 'AFA TALKTIME' or any other service.
    return defaults

# ---------- NEW: customer daily sales (today + last 5) ----------

def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end

def compute_user_daily_sales(user_oid: ObjectId, days_back: int = 6) -> Dict[str, Any]:
    """
    Customer 'sales' = sum of their order totals (customer-facing price).
    Uses orders.total_amount per day.
    If you prefer charged-only, switch "$total_amount" to "$charged_amount" below.
    Returns labels, values, today_sales, yesterday_sales, change_pct, trend, statement.
    """
    today = datetime.utcnow().date()
    # previous 5 days + today, in chronological order
    days = [today - timedelta(days=i) for i in range(days_back)][::-1]

    window_start, _ = _day_range(days[0])
    _, window_end = _day_range(days[-1])  # end-of-today

    pipeline = [
        {"$match": {
            "user_id": user_oid,
            "created_at": {"$gte": window_start, "$lt": window_end},
            # If needed: "status": "completed",
        }},
        {"$project": {
            "d": {"$dateTrunc": {"date": "$created_at", "unit": "day"}},
            "amt": {"$ifNull": ["$total_amount", 0]},
        }},
        {"$group": {"_id": "$d", "sales": {"$sum": "$amt"}}},
    ]

    try:
        agg = list(orders_col.aggregate(pipeline))
    except Exception:
        agg = []

    by_day: Dict[date, float] = {}
    for row in agg:
        dt = row.get("_id")
        if isinstance(dt, datetime):
            by_day[dt.date()] = float(row.get("sales", 0) or 0)

    labels: List[str] = []
    values: List[float] = []
    for d in days:
        labels.append("Today" if d == today else d.strftime("%b %d"))
        values.append(round(by_day.get(d, 0.0), 2))

    today_sales = values[-1] if values else 0.0
    yesterday_sales = values[-2] if len(values) >= 2 else 0.0

    if yesterday_sales == 0:
        change_pct = 100.0 if today_sales > 0 else 0.0
    else:
        change_pct = ((today_sales - yesterday_sales) / abs(yesterday_sales)) * 100.0

    if abs(today_sales - yesterday_sales) < 1e-9:
        trend = "flat"
        statement = "Today’s purchases are the same as yesterday."
    elif today_sales > yesterday_sales:
        trend = "up"
        diff = round(today_sales - yesterday_sales, 2)
        pct = round(change_pct, 2)
        statement = f"Today’s purchases have risen by {pct}% compared to yesterday (up GHS {diff:,.2f})."
    else:
        trend = "down"
        diff = round(yesterday_sales - today_sales, 2)
        pct = round(abs(change_pct), 2)
        statement = f"Today’s purchases have fallen by {pct}% compared to yesterday (down GHS {diff:,.2f})."

    return {
        "labels": labels,
        "values": values,
        "today_sales": round(today_sales, 2),
        "yesterday_sales": round(yesterday_sales, 2),
        "change_pct": round(change_pct, 2),
        "trend": trend,
        "statement": statement,
    }

# ---------- globals ----------
@customer_dashboard_bp.app_context_processor
def inject_customer_globals():
    bal = 0.0
    uname = session.get("username")
    try:
        if session.get("role") == "customer" and session.get("user_id"):
            uid = ObjectId(session["user_id"])
            bal_doc = balances_col.find_one({"user_id": uid})
            if bal_doc and bal_doc.get("amount") is not None:
                bal = float(bal_doc["amount"])
            user_doc = users_col.find_one({"_id": uid}, {
                "full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1
            })
            uname = _display_name(user_doc)
    except Exception:
        pass
    return {"customer_balance": bal, "customer_username": uname or "Customer"}

# ---------- API: Customer AFA Registration (charge immediately) ----------
@customer_dashboard_bp.route("/api/afa/register", methods=["POST"])
def api_afa_register():
    # Auth: customers only
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify(success=False, error="Unauthorized"), 401

    user_oid = ObjectId(session["user_id"])

    payload = request.get_json(silent=True) or {}
    name       = (payload.get("name") or "").strip()
    phone      = (payload.get("phone") or "").strip()
    dob        = (payload.get("dob") or None)
    location   = (payload.get("location") or None)
    ghana_card = (payload.get("ghana_card") or None)

    # Basic validation
    if not name:
        return jsonify(success=False, error="Name is required"), 400
    if not re.match(r"^0\d{9}$", phone):
        return jsonify(success=False, error="Phone must be 0xxxxxxxxx"), 400

    # Load AFA settings (single source of truth)
    afa = _load_afa_settings()
    if not afa["is_open"]:
        return jsonify(success=False, error="Service closed"), 400
    if not afa["in_stock"]:
        return jsonify(success=False, error="Out of stock"), 400

    price = _to_float(afa.get("price")) or 0.0
    if price < 0:
        price = 0.0

    now = _now()

    # Atomic charge: guard against insufficient funds
    upd = balances_col.update_one(
        {"user_id": user_oid, "amount": {"$gte": price}},
        {"$inc": {"amount": -price}, "$set": {"updated_at": now}},
        upsert=False
    )
    if upd.matched_count == 0:
        return jsonify(success=False, error="Insufficient funds"), 400

    # Fetch new balance (best effort)
    bal_doc = balances_col.find_one({"user_id": user_oid}) or {}
    new_balance = float(bal_doc.get("amount", 0.0) or 0.0)

    # Log balance change
    actor_name = session.get("username") or session.get("email") or "customer"
    log_doc = {
        "user_id": user_oid,
        "action": "withdraw",
        "delta": -price,
        "amount_before": None,  # Optional: keep None or compute preimage with find_one_and_update if required
        "amount_after": new_balance,
        "currency": bal_doc.get("currency", "GHS"),
        "note": "AFA registration (customer self-charge)",
        "actor_id": user_oid,
        "actor_name": actor_name,
        "created_at": now,
    }
    log_res = balance_logs_col.insert_one(log_doc)

    # Create registration (already charged)
    reg_doc = {
        "customer_id": user_oid,
        "name": name,
        "phone": phone,
        "dob": dob or None,
        "location": location or None,
        "ghana_card": ghana_card or None,

        "status": "pending",
        "charged": True,
        "amount": price,                 # normalize UI amount to settings price used
        "charged_amount": price,
        "charged_at": now,
        "charged_by": actor_name,
        "charge_log_id": log_res.inserted_id,

        "created_at": now,
        "updated_at": now,
    }
    reg_id = afa_col.insert_one(reg_doc).inserted_id

    return jsonify(
        success=True,
        message="Registration submitted and charged.",
        registration_id=str(reg_id),
        balance=new_balance,
        price=price
    ), 200


@customer_dashboard_bp.route("/api/store-notifications", methods=["GET"])
def api_store_notifications():
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify(success=False, error="Unauthorized"), 401

    user_oid = ObjectId(session["user_id"])
    store_slugs: List[str] = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1}
            )
            if s.get("slug")
        ]
    except Exception:
        store_slugs = []

    if not store_slugs:
        return jsonify(success=True, orders=[], payments=[])

    orders_view: List[Dict[str, Any]] = []
    try:
        cur = (
            orders_col.find(
                {"store_slug": {"$in": store_slugs}},
                {
                    "order_id": 1,
                    "store_slug": 1,
                    "items": 1,
                    "total_amount": 1,
                    "status": 1,
                    "created_at": 1,
                    "paystack_reference": 1,
                }
            )
            .sort("created_at", -1)
            .limit(30)
        )
        for od in cur:
            created_at = od.get("created_at")
            created_iso = created_at.isoformat() if isinstance(created_at, datetime) else ""
            created_fmt = created_at.strftime("%d %b %Y, %I:%M %p") if isinstance(created_at, datetime) else ""
            items = od.get("items") or []
            phone = ""
            if items and isinstance(items[0], dict):
                phone = items[0].get("phone") or ""
            orders_view.append({
                "order_id": od.get("order_id"),
                "store_slug": od.get("store_slug"),
                "phone": phone,
                "total_amount": _to_float(od.get("total_amount")) or 0.0,
                "status": od.get("status") or "",
                "paystack_reference": (od.get("paystack_reference") or "").strip(),
                "created_at_iso": created_iso,
                "created_at_fmt": created_fmt,
            })
    except Exception:
        orders_view = []

    payments_view: List[Dict[str, Any]] = []
    try:
        q = {
            "status": "success",
            "meta.store_slug": {"$in": store_slugs},
            "$or": [
                {"source": {"$in": ["paystack_inline", "paystack_transfer", "paystack"]}},
                {"gateway": {"$regex": "paystack", "$options": "i"}},
            ],
        }
        cur = (
            transactions_col.find(
                q,
                {
                    "amount": 1,
                    "reference": 1,
                    "paystack_reference": 1,
                    "verified_at": 1,
                    "created_at": 1,
                    "meta.store_slug": 1,
                }
            )
            .sort([("verified_at", -1), ("created_at", -1)])
            .limit(30)
        )
        for tx in cur:
            ts = tx.get("verified_at") or tx.get("created_at")
            created_iso = ts.isoformat() if isinstance(ts, datetime) else ""
            created_fmt = ts.strftime("%d %b %Y, %I:%M %p") if isinstance(ts, datetime) else ""
            payments_view.append({
                "amount": _to_float(tx.get("amount")) or 0.0,
                "reference": (tx.get("reference") or "").strip(),
                "paystack_reference": (tx.get("paystack_reference") or "").strip(),
                "created_at_iso": created_iso,
                "created_at_fmt": created_fmt,
            })
    except Exception:
        payments_view = []

    return jsonify(success=True, orders=orders_view, payments=payments_view)

# ---------- route ----------
@customer_dashboard_bp.route("/customer/dashboard")
def customer_dashboard():
    if session.get("role") != "customer":
        return redirect(url_for("login.login"))

    user_id = session.get("user_id")
    if not user_id:
        return redirect(url_for("login.login"))
    user_oid = ObjectId(user_id)

    # user doc
    user_doc = users_col.find_one({"_id": user_oid}, {
        "full_name": 1, "name": 1, "first_name": 1, "last_name": 1, "username": 1, "email": 1,
        "customer_stage": 1,
    })
    customer_name = _display_name(user_doc)
    customer_stage = _norm_stage_key(user_doc.get("customer_stage") if user_doc else None)

    # services (sorted)
    raw_services = list(services_col.find({}))
    raw_services.sort(key=_service_priority_tuple)

    services: List[Dict[str, Any]] = []
    for s in raw_services:
        s["_id_str"] = str(s["_id"])
        # attach flags/state for UI
        st = _service_state(s)
        s.update(st)  # type, status, availability, can_order, disabled_reason, messages

        unit = _service_unit(s)  # minutes for AFA TALKTIME, data otherwise
        offers = s.get("offers") or []

        normalized_offers: List[Dict[str, Any]] = []
        for of in offers:
            parsed_value = _parse_value_field(of.get("value"))
            vol_num = _extract_volume(parsed_value, unit)  # for sorting
            value_text = _value_text_for_display(parsed_value, unit)

            amount = _to_float(of.get("amount"))
            stage_price = _stage_price_from_offer(of, customer_stage)
            default_price = _to_float(of.get("customer_price"))
            if stage_price is not None:
                total = stage_price
                price_source = "stage"
            elif default_price is not None:
                total = default_price
                price_source = "default"
            else:
                total = amount
                price_source = "base"

            normalized_offers.append({
                "amount": amount,
                "value": parsed_value,
                "value_text": value_text,
                "legacy_profit": _to_float(of.get("profit")),
                "profit_percent_used": None,
                "total": total,
                "price_source": price_source,
                "_sort_vol": vol_num if vol_num is not None else float("inf"),
                "_sort_amt": amount if amount is not None else float("inf"),
            })

        # sort by volume asc, then amount asc
        normalized_offers.sort(key=lambda x: (x["_sort_vol"], x["_sort_amt"]))
        s["offers"] = [{k: v for k, v in o.items() if not k.startswith("_sort_")} for o in normalized_offers]
        s["effective_profit_percent"] = None
        s["unit"] = unit
        s["customer_stage"] = customer_stage

        services.append(s)

    # Balance
    balance_doc = balances_col.find_one({"user_id": user_oid})
    balance = float(balance_doc["amount"]) if (balance_doc and balance_doc.get("amount") is not None) else 0.00

    # Recent orders
    recent_orders = list(
        orders_col.find({"user_id": user_oid})
        .sort("created_at", -1)
        .limit(5)
    )

    # Outstanding payouts + store recent orders
    outstanding_payouts = 0.0
    store_slugs: List[str] = []
    store_recent_orders: List[Dict[str, Any]] = []
    store_recent_orders_view: List[Dict[str, Any]] = []
    try:
        store_slugs = [
            s.get("slug")
            for s in stores_col.find(
                {"owner_id": user_oid, "status": {"$ne": "deleted"}},
                {"slug": 1}
            )
            if s.get("slug")
        ]
        if store_slugs:
            pipeline = [
                {"$match": {"store_slug": {"$in": store_slugs}}},
                {"$group": {
                    "_id": None,
                    "total": {"$sum": {"$toDouble": {"$ifNull": ["$total_profit_balance", 0]}}},
                }},
            ]
            agg = list(store_accounts_col.aggregate(pipeline))
            if agg:
                outstanding_payouts = _to_float(agg[0].get("total")) or 0.0

            store_recent_orders = list(
                orders_col.find(
                    {"store_slug": {"$in": store_slugs}},
                    {
                        "order_id": 1,
                        "store_slug": 1,
                        "items": 1,
                        "total_amount": 1,
                        "status": 1,
                        "created_at": 1,
                        "paystack_reference": 1,
                    }
                )
                .sort("created_at", -1)
            )
            for od in store_recent_orders:
                created_at = od.get("created_at")
                created_iso = created_at.isoformat() if isinstance(created_at, datetime) else ""
                created_fmt = created_at.strftime("%d %b %Y, %I:%M %p") if isinstance(created_at, datetime) else ""
                items = od.get("items") or []
                phone = ""
                if items and isinstance(items[0], dict):
                    phone = items[0].get("phone") or ""
                store_recent_orders_view.append({
                    "order_id": od.get("order_id"),
                    "store_slug": od.get("store_slug"),
                    "phone": phone,
                    "total_amount": _to_float(od.get("total_amount")) or 0.0,
                    "status": od.get("status") or "",
                    "paystack_reference": (od.get("paystack_reference") or "").strip(),
                    "created_at_iso": created_iso,
                    "created_at_fmt": created_fmt,
                })
    except Exception:
        pass

    # ---- split into categories (Express vs others) ----
    def _is_express(svc: Dict[str, Any]) -> bool:
        cat = (svc.get("service_category") or "").strip().lower()
        cat2 = (svc.get("category") or "").strip().lower()
        return cat == "express services" or cat2 == "express"

    express_services = [s for s in services if _is_express(s)]
    regular_services = [s for s in services if not _is_express(s)]

    # AFA settings (price / open / stock) — decoupled from services
    afa = _load_afa_settings()

    # Affordability for AFA button state on the page
    can_buy_afa = bool(afa["is_open"] and afa["in_stock"] and balance >= float(afa["price"] or 0.0))

    # NEW: the customer’s own sales trend (today + last 5 days)
    ds = compute_user_daily_sales(user_oid, days_back=6)
    store_notify_key = ",".join(sorted(store_slugs)) if store_slugs else "none"

    return render_template(
        "customer_dashboard.html",
        services=regular_services,         # keep old variable working for existing section
        express_services=express_services, # NEW
        balance=balance,
        recent_orders=recent_orders,
        customer_name=customer_name,
        afa=afa,                           # pass settings for the AFA block in your HTML
        can_buy_afa=can_buy_afa,           # NEW: enable/disable Buy button for AFA

        # sales KPIs for the hero section
        today_sales=ds["today_sales"],
        yesterday_sales=ds["yesterday_sales"],
        sales_change_pct=ds["change_pct"],
        sales_trend=ds["trend"],
        sales_statement=ds["statement"],
        daily_sales_labels=ds["labels"],
        daily_sales_values=ds["values"],
        outstanding_payouts=outstanding_payouts,
        store_recent_orders=store_recent_orders,
        store_recent_orders_view=store_recent_orders_view,
        has_store=bool(store_slugs),
        store_notify_key=store_notify_key,
    )
