from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Tuple, Optional
import os, re

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from db import db
from withdraw_requests import update_withdraw_request_status

customer_store_bp = Blueprint("customer_store", __name__)

# Collections
stores_col               = db["stores"]
orders_col               = db["orders"]
users_col                = db["users"]
balances_col             = db["balances"]
transactions_col         = db["transactions"]
store_payouts_col        = db["store_payouts"]
store_payout_logs        = db["store_payout_logs"]
store_withdraw_requests  = db["store_withdraw_requests"]
store_accounts_col       = db["store_accounts"]

MIN_WITHDRAW_AMOUNT = 20.0
STORE_PUBLIC_HOST = (os.getenv("STORE_PUBLIC_HOST") or "").strip()

def _store_public_host(req_host: str) -> str:
    return STORE_PUBLIC_HOST or (req_host or "").strip()

def _store_public_scheme(req) -> str:
    # Respect proxies/load balancers when set, otherwise use request.scheme.
    return (req.headers.get("X-Forwarded-Proto") or req.scheme or "http").split(",")[0].strip()


# ---------- helpers ----------
def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end

def _fmt_money(x: Any) -> float:
    try:
        return round(float(x or 0), 2)
    except Exception:
        return 0.0

def _ensure_owner_store(user_id: ObjectId, slug: str) -> Optional[Dict[str, Any]]:
    return stores_col.find_one({"owner_id": user_id, "slug": slug, "status": {"$ne": "deleted"}})

def _latest_owner_store(user_id: ObjectId) -> Optional[Dict[str, Any]]:
    return stores_col.find_one(
        {"owner_id": user_id, "status": {"$ne": "deleted"}},
        sort=[("updated_at", -1), ("created_at", -1)]
    )

def _owner_display_name(user_doc: Optional[Dict[str, Any]]) -> str:
    if not user_doc:
        return "Customer"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "Customer"

def _owner_wallet_balance(user_id: ObjectId) -> float:
    bal = balances_col.find_one({"user_id": user_id}) or {}
    return _fmt_money(bal.get("amount"))

def _profit_all_time(slug: str) -> float:
    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }
    pipeline = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "p": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}}}},
    ]
    agg = list(orders_col.aggregate(pipeline))
    return _fmt_money(agg[0]["p"]) if agg else 0.0

def _withdrawn_so_far(slug: str) -> float:
    pipeline = [
        {"$match": {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}},
        {"$group": {"_id": None, "amt": {"$sum": {"$ifNull": ["$amount", 0]}}}},
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return _fmt_money(agg[0]["amt"]) if agg else 0.0

def _withdrawable(slug: str) -> float:
    acct = store_accounts_col.find_one({"store_slug": slug}, {"total_profit_balance": 1}) or {}
    return _fmt_money(acct.get("total_profit_balance"))

def _get_auto_withdraw_settings(slug: str) -> Dict[str, Any]:
    acct = store_accounts_col.find_one(
        {"store_slug": slug},
        {"auto_withdraw_enabled": 1, "auto_withdraw_amount": 1, "auto_withdraw_method": 1},
    ) or {}
    return {
        "enabled": bool(acct.get("auto_withdraw_enabled")),
        "amount": _fmt_money(acct.get("auto_withdraw_amount")),
        "method": (acct.get("auto_withdraw_method") or "momo").strip().lower(),
    }

def _maybe_auto_withdraw(owner_id: ObjectId, slug: str) -> Optional[Dict[str, Any]]:
    settings = _get_auto_withdraw_settings(slug)
    if not settings.get("enabled"):
        return None

    amount = _fmt_money(settings.get("amount"))
    method = str(settings.get("method") or "momo").strip().lower()
    if amount < MIN_WITHDRAW_AMOUNT:
        return None
    if method not in ("momo", "wallet"):
        return None

    max_allowed = _withdrawable(slug)
    if max_allowed < amount - 1e-9:
        return None
    if max_allowed < MIN_WITHDRAW_AMOUNT - 1e-9:
        return None

    pending = store_withdraw_requests.find_one({
        "owner_id": owner_id,
        "store_slug": slug,
        "status": "pending",
    })
    if pending:
        return None

    payout_snapshot = None
    if method == "momo":
        payout = store_payouts_col.find_one(
            {"owner_id": owner_id, "store_slug": slug},
            {"recipient_name": 1, "msisdn": 1, "network": 1}
        ) or {}
        if not (payout.get("msisdn") or payout.get("recipient_name")):
            return None
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }

    doc_id = ObjectId()
    doc = {
        "_id": doc_id,
        "reference": _make_reference("WDR", doc_id),
        "owner_id": owner_id,
        "store_slug": slug,
        "amount": amount,
        "method": method,
        "payout_snapshot": payout_snapshot,
        "status": "pending",
        "note": "auto_withdraw",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    store_withdraw_requests.insert_one(doc)
    return {"id": str(doc_id), "reference": doc["reference"]}

def _iso(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    if isinstance(dt, str):
        return dt[:16]
    return ""

def _make_reference(prefix: str, oid: ObjectId) -> str:
    d = datetime.utcnow().strftime("%Y%m%d")
    tail = str(oid)[-6:].upper()
    return f"{prefix}-{d}-{tail}"

def _admin_guard() -> bool:
    return bool(session.get("user_id")) and (session.get("role") in ("admin", "superadmin"))

def _clean_phone(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()

def _extract_order_phones(order_doc: Dict[str, Any]) -> Dict[str, Any]:
    """
    Orders created by store_page.py store phone at items[].phone
    We derive:
      - phone_primary
      - phone_count
      - phone_summary
    """
    phones: List[str] = []

    top_phone = _clean_phone(order_doc.get("phone") or order_doc.get("customer_phone"))
    if top_phone:
        phones.append(top_phone)

    items = order_doc.get("items") or []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            p = _clean_phone(it.get("phone"))
            if p:
                phones.append(p)

    uniq: List[str] = []
    seen = set()
    for p in phones:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)

    phone_primary = uniq[0] if uniq else ""
    phone_count = len(uniq)

    if not uniq:
        summary = ""
    else:
        show = uniq[:4]
        summary = ", ".join(show)
        if len(uniq) > 4:
            summary += f" (+{len(uniq)-4} more)"

    return {
        "phone_primary": phone_primary or None,
        "phone_count": phone_count,
        "phone_summary": summary or None,
    }

def _gather_dashboard(slug: str) -> Dict[str, Any]:
    today = datetime.utcnow().date()
    d0, d1 = _day_range(today)

    order_profit_expr = {
        "$let": {
            "vars": {
                "items_profit": {
                    "$sum": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                        }
                    }
                },
                "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
            },
            "in": {
                "$cond": [
                    {"$gt": ["$$items_profit", 0]},
                    "$$items_profit",
                    "$$legacy_profit",
                ]
            },
        }
    }

    pipeline_totals = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": None,
            "total_sales": {"$sum": {"$ifNull": ["$total_amount", 0]}},
            "total_profit": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}},
            "orders_count": {"$sum": 1},
        }},
    ]
    agg_tot = list(orders_col.aggregate(pipeline_totals))
    all_time_sales  = _fmt_money(agg_tot[0].get("total_sales") if agg_tot else 0)
    all_time_profit = _fmt_money(agg_tot[0].get("total_profit") if agg_tot else 0)
    orders_count    = int(agg_tot[0].get("orders_count") if agg_tot else 0)

    pipeline_today_profit = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "profit_today": {"$sum": {"$ifNull": ["$store_profit_sum", 0]}}}},
    ]
    agg_today = list(orders_col.aggregate(pipeline_today_profit))
    profit_today = _fmt_money(agg_today[0].get("profit_today") if agg_today else 0)

    pipeline_top_offers = [
        {"$match": {"store_slug": slug}},
        {"$unwind": {"path": "$items", "preserveNullAndEmptyArrays": False}},
        {"$group": {
            "_id": {
                "service": {"$ifNull": ["$items.serviceName", "Unknown Service"]},
                "label":   {"$ifNull": ["$items.value", "-"]},
            },
            "count":   {"$sum": 1},
            "revenue": {"$sum": {"$ifNull": ["$items.amount", 0]}},
        }},
        {"$sort": {"count": -1, "revenue": -1}},
        {"$limit": 8},
    ]
    top_offers_raw = list(orders_col.aggregate(pipeline_top_offers))
    top_offers = [{
        "service": x["_id"]["service"],
        "label":   x["_id"]["label"],
        "count":   int(x.get("count", 0)),
        "revenue": _fmt_money(x.get("revenue", 0)),
    } for x in top_offers_raw]

    recent_orders_cur = (
        orders_col.find({"store_slug": slug})
        .sort("created_at", -1)
        .limit(10)
    )

    recent_orders: List[Dict[str, Any]] = []
    for o in recent_orders_cur:
        items = o.get("items") or []
        store_profit_total = 0.0
        found_store_profit = False
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                sp = it.get("store_profit_amount")
                if sp is not None:
                    found_store_profit = True
                    store_profit_total += _fmt_money(sp)
        if not found_store_profit:
            store_profit_total = _fmt_money(o.get("profit_amount_total", 0))

        phone_info = _extract_order_phones(o)
        recent_orders.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "total_amount":        _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": store_profit_total,
            "charged_amount":      _fmt_money(o.get("charged_amount", 0)),
            "items_count": len(o.get("items") or []),
            "created_at": o.get("created_at"),
            **phone_info,
        })

    return {
        "today": today,
        "all_time_sales": all_time_sales,
        "profit_today": profit_today,
        "all_time_profit": all_time_profit,
        "orders_count": orders_count,
        "top_offers": top_offers,
        "recent_orders": recent_orders,
        "withdrawable": _withdrawable(slug),
    }


# ---------- Pages ----------
@customer_store_bp.route("/customer/store", methods=["GET"])
def customer_store_home():
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    owner = users_col.find_one({"_id": owner_id}, {"full_name": 1, "name": 1, "username": 1, "email": 1})
    store_doc = _latest_owner_store(owner_id)

    if not store_doc:
        today = datetime.utcnow().date()
        return render_template(
            "customer_store.html",
            store=None,
            owner_name=_owner_display_name(owner),
            all_time_sales=0.00,
            profit_today=0.00,
            all_time_profit=0.00,
            orders_count=0,
            top_offers=[],
            recent_orders=[],
            withdrawable=0.00,
            wallet_balance=_owner_wallet_balance(owner_id),
            today_str=today.strftime("%b %d, %Y"),
            slug=None,
            store_host=_store_public_host(request.host),
            store_scheme=_store_public_scheme(request),
        )

    slug = store_doc.get("slug")
    _maybe_auto_withdraw(owner_id, slug)
    k = _gather_dashboard(slug)

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        slug=slug,
        store_host=_store_public_host(request.host),
        store_scheme=_store_public_scheme(request),
    )

@customer_store_bp.route("/customer/store/<slug>", methods=["GET"])
def customer_store_dashboard(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))

    owner_id = ObjectId(session["user_id"])
    store_doc = _ensure_owner_store(owner_id, slug)
    if not store_doc:
        return redirect(url_for("customer_store.customer_store_home"))

    _maybe_auto_withdraw(owner_id, slug)
    k = _gather_dashboard(slug)
    owner = users_col.find_one({"_id": owner_id}, {"full_name": 1, "name": 1, "username": 1, "email": 1})

    return render_template(
        "customer_store.html",
        store=store_doc,
        owner_name=_owner_display_name(owner),
        all_time_sales=k["all_time_sales"],
        profit_today=k["profit_today"],
        all_time_profit=k["all_time_profit"],
        orders_count=k["orders_count"],
        top_offers=k["top_offers"],
        recent_orders=k["recent_orders"],
        withdrawable=k["withdrawable"],
        wallet_balance=_owner_wallet_balance(owner_id),
        today_str=k["today"].strftime("%b %d, %Y"),
        slug=slug,
        store_host=_store_public_host(request.host),
        store_scheme=_store_public_scheme(request),
    )


# ---------- Customer APIs ----------
@customer_store_bp.route("/api/customer/store/<slug>/payout_snapshot", methods=["GET"])
def api_customer_store_payout_snapshot(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    payout = store_payouts_col.find_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"recipient_name": 1, "msisdn": 1, "network": 1}
    ) or {}
    return jsonify({"success": True, "payout": {
        "recipient_name": payout.get("recipient_name"),
        "msisdn": payout.get("msisdn"),
        "network": payout.get("network"),
    }})

@customer_store_bp.route("/api/customer/store/<slug>/withdrawals", methods=["GET"])
def api_customer_store_withdrawals(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401
    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(100, int(request.args.get("limit", 20))))
    except Exception:
        limit = 20
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    skip = (page - 1) * limit

    cur = transactions_col.find(
        {"type": "store_withdrawal", "status": "success", "meta.store_slug": slug}
    ).sort("created_at", -1).skip(skip).limit(limit)

    items = []
    for t in cur:
        meta = (t.get("meta") or {})
        note = meta.get("note") or ""
        method = meta.get("method")
        if method == "momo":
            note = note or "Paid to MoMo"
        elif method == "wallet":
            note = note or "Credited to wallet"

        items.append({
            "reference": t.get("reference"),
            "amount": _fmt_money(t.get("amount")),
            "created_at": _iso(t.get("created_at")),
            "verified_at": _iso(t.get("verified_at")),
            "note": note or "Paid",
        })

    total = transactions_col.count_documents({"type": "store_withdrawal", "status": "success", "meta.store_slug": slug})
    return jsonify({"success": True, "items": items, "page": page, "limit": limit, "total": total})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/requests", methods=["GET"])
def api_customer_store_withdraw_requests(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    cur = store_withdraw_requests.find(
        {"owner_id": owner_id, "store_slug": slug},
        sort=[("created_at", -1)]
    ).limit(limit)

    items = []
    for r in cur:
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "amount": _fmt_money(r.get("amount")),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/customer/store/<slug>/auto-withdraw", methods=["GET", "POST"])
def api_customer_store_auto_withdraw(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    if request.method == "GET":
        settings = _get_auto_withdraw_settings(slug)
        return jsonify({"success": True, "settings": settings})

    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    amount = _fmt_money(payload.get("amount"))
    method = str(payload.get("method") or "momo").strip().lower()

    if method not in ("momo", "wallet"):
        return jsonify({"success": False, "message": "Invalid method"}), 400
    if enabled and amount < MIN_WITHDRAW_AMOUNT:
        return jsonify({"success": False, "message": f"Minimum auto-withdraw amount is GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400

    store_accounts_col.update_one(
        {"store_slug": slug},
        {"$set": {
            "store_slug": slug,
            "auto_withdraw_enabled": enabled,
            "auto_withdraw_amount": amount,
            "auto_withdraw_method": method,
            "updated_at": datetime.utcnow(),
        }},
        upsert=True,
    )

    auto_result = _maybe_auto_withdraw(owner_id, slug)
    return jsonify({"success": True, "settings": _get_auto_withdraw_settings(slug), "auto_request": auto_result})

@customer_store_bp.route("/api/customer/store/<slug>/auto-withdraw/run", methods=["POST"])
def api_customer_store_auto_withdraw_run(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    auto_result = _maybe_auto_withdraw(owner_id, slug)
    return jsonify({"success": True, "auto_request": auto_result})

@customer_store_bp.route("/api/customer/store/<slug>/withdraw/request", methods=["POST"])
def api_customer_store_request_withdraw(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    payload = request.get_json(silent=True) or {}
    amount = _fmt_money(payload.get("amount"))
    method = str(payload.get("method") or "momo").strip().lower()

    if method not in ("momo", "wallet"):
        return jsonify({"success": False, "message": "Invalid method"}), 400
    if amount <= 0:
        return jsonify({"success": False, "message": "Enter a valid amount"}), 400
    if amount < MIN_WITHDRAW_AMOUNT:
        return jsonify({"success": False, "message": f"Minimum withdrawal amount is GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400

    max_allowed = _withdrawable(slug)
    if max_allowed < MIN_WITHDRAW_AMOUNT - 1e-9:
        return jsonify({"success": False, "message": f"Profit balance must be at least GHS {MIN_WITHDRAW_AMOUNT:.2f}"}), 400
    if amount - max_allowed > 1e-9:
        return jsonify({"success": False, "message": f"Amount exceeds profit balance (GHS {max_allowed:.2f})"}), 400

    payout_snapshot = None
    if method == "momo":
        payout = store_payouts_col.find_one(
            {"owner_id": owner_id, "store_slug": slug},
            {"recipient_name": 1, "msisdn": 1, "network": 1}
        ) or {}
        payout_snapshot = {
            "recipient_name": payout.get("recipient_name"),
            "msisdn": payout.get("msisdn"),
            "network": payout.get("network"),
        }

    recent_pending = store_withdraw_requests.find_one({
        "owner_id": owner_id,
        "store_slug": slug,
        "status": "pending",
        "amount": amount,
        "created_at": {"$gte": datetime.utcnow() - timedelta(minutes=2)}
    })
    if recent_pending:
        return jsonify({"success": True, "message": "Request already submitted", "id": str(recent_pending["_id"])})

    doc_id = ObjectId()
    doc = {
        "_id": doc_id,
        "reference": _make_reference("WDR", doc_id),
        "owner_id": owner_id,
        "store_slug": slug,
        "amount": amount,
        "method": method,
        "payout_snapshot": payout_snapshot,
        "status": "pending",
        "note": "",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    store_withdraw_requests.insert_one(doc)

    return jsonify({"success": True, "id": str(doc_id), "reference": doc["reference"]})


@customer_store_bp.route("/api/customer/store/<slug>/orders/search", methods=["GET"])
def api_customer_store_orders_search(slug: str):
    """
    Default returns latest 10.
    If q provided, searches:
      - order_id (regex)
      - items.phone (regex)
      - phone / customer_phone (regex)

    Critical Fix:
      - No projection collision (do NOT project 'items' and 'items.phone' together)
      - Use aggregation to return a LIGHT items array with only {phone}
    """
    if session.get("role") != "customer" or not session.get("user_id"):
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = ObjectId(session["user_id"])
    if not _ensure_owner_store(owner_id, slug):
        return jsonify({"success": False, "message": "Store not found"}), 404

    q_raw = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(50, int(request.args.get("limit", 10))))
    except Exception:
        limit = 10

    q_digits = re.sub(r"\D+", "", q_raw or "").strip()

    match: Dict[str, Any] = {"store_slug": slug}

    if q_raw:
        rx_any = {"$regex": re.escape(q_raw), "$options": "i"}

        or_terms: List[Dict[str, Any]] = [
            {"order_id": rx_any},
            {"items.phone": rx_any},
            {"phone": rx_any},
            {"customer_phone": rx_any},
        ]

        if q_digits and len(q_digits) >= 6:
            rx_d = {"$regex": re.escape(q_digits), "$options": "i"}
            or_terms.extend([
                {"items.phone": rx_d},
                {"phone": rx_d},
                {"customer_phone": rx_d},
            ])

        match["$or"] = or_terms

    try:
        order_profit_expr = {
            "$let": {
                "vars": {
                    "items_profit": {
                        "$sum": {
                            "$map": {
                                "input": {"$ifNull": ["$items", []]},
                                "as": "it",
                                "in": {"$toDouble": {"$ifNull": ["$$it.store_profit_amount", 0]}},
                            }
                        }
                    },
                    "legacy_profit": {"$toDouble": {"$ifNull": ["$profit_amount_total", 0]}},
                },
                "in": {
                    "$cond": [
                        {"$gt": ["$$items_profit", 0]},
                        "$$items_profit",
                        "$$legacy_profit",
                    ]
                },
            }
        }
        pipeline = [
            {"$match": match},
            {"$sort": {"created_at": -1}},
            {"$limit": int(limit)},
            {"$addFields": {"store_profit_sum": order_profit_expr}},
            {
                "$project": {
                    "_id": 0,
                    "order_id": 1,
                    "status": 1,
                    "total_amount": 1,
                    "profit_amount_total": "$store_profit_sum",
                    "charged_amount": 1,
                    "created_at": 1,
                    "phone": 1,
                    "customer_phone": 1,

                    "items_count": {"$size": {"$ifNull": ["$items", []]}},

                    # Light items for phone extraction
                    "items": {
                        "$map": {
                            "input": {"$ifNull": ["$items", []]},
                            "as": "it",
                            "in": {"phone": {"$ifNull": ["$$it.phone", None]}}
                        }
                    },
                }
            },
        ]
        docs = list(orders_col.aggregate(pipeline))
    except Exception as e:
        return jsonify({"success": False, "message": f"Search failed: {str(e)}"}), 500

    items: List[Dict[str, Any]] = []
    for o in docs:
        phone_info = _extract_order_phones(o)
        items.append({
            "order_id": o.get("order_id"),
            "status": o.get("status"),
            "total_amount": _fmt_money(o.get("total_amount", 0)),
            "profit_amount_total": _fmt_money(o.get("profit_amount_total", 0)),
            "charged_amount": _fmt_money(o.get("charged_amount", 0)),
            "items_count": int(o.get("items_count") or 0),
            "created_at": _iso(o.get("created_at")),
            **phone_info,
        })

    return jsonify({"success": True, "items": items})


# ---------- Admin APIs ----------
@customer_store_bp.route("/api/admin/store/withdraw/requests", methods=["GET"])
def api_admin_store_withdraw_requests():
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    status = (request.args.get("status") or "").strip().lower()
    slug = (request.args.get("slug") or "").strip()
    owner_id_raw = (request.args.get("owner_id") or "").strip()
    q = (request.args.get("q") or "").strip()

    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50

    query: Dict[str, Any] = {}
    if status in ("pending", "paid", "rejected"):
        query["status"] = status
    if slug:
        query["store_slug"] = slug
    if owner_id_raw:
        try:
            query["owner_id"] = ObjectId(owner_id_raw)
        except Exception:
            return jsonify({"success": False, "message": "Invalid owner_id"}), 400
    if q:
        query["reference"] = {"$regex": re.escape(q), "$options": "i"}

    cur = store_withdraw_requests.find(query).sort("created_at", -1).limit(limit)

    items = []
    for r in cur:
        payout = r.get("payout_snapshot") or {}
        items.append({
            "id": str(r.get("_id")),
            "reference": r.get("reference"),
            "store_slug": r.get("store_slug"),
            "owner_id": str(r.get("owner_id")),
            "amount": _fmt_money(r.get("amount")),
            "method": r.get("method"),
            "status": r.get("status"),
            "created_at": _iso(r.get("created_at")),
            "updated_at": _iso(r.get("updated_at")),
            "payout_snapshot": payout,
        })

    return jsonify({"success": True, "items": items})

@customer_store_bp.route("/api/admin/store/withdraw/<request_id>/status", methods=["POST"])
def api_admin_store_withdraw_update_status(request_id: str):
    if not _admin_guard():
        return jsonify({"success": False, "message": "Admin login required"}), 401

    payload = request.get_json(silent=True) or {}
    new_status = str(payload.get("status") or "").strip().lower()
    note = str(payload.get("note") or "").strip()
    ok, payload, code = update_withdraw_request_status(
        req_id=request_id,
        new_status=new_status,
        actor_id=session.get("user_id") or "admin",
        note=note,
    )
    if ok:
        return jsonify({"success": True, **payload}), code
    return jsonify({"success": False, "message": payload.get("message")}), code


# ---------- Payout settings ----------
@customer_store_bp.route("/customer/store/<slug>/payout", methods=["GET"])
def customer_store_payout_page(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    payout = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    hist = list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1).limit(100))

    return render_template(
        "customer_store_payout.html",
        store=store,
        current=payout,
        history=hist,
        withdrawable=_withdrawable(slug),
        wallet_balance=_owner_wallet_balance(owner_id),
    )

@customer_store_bp.route("/customer/store/<slug>/payout", methods=["POST"])
def customer_store_payout_save(slug: str):
    if session.get("role") != "customer" or not session.get("user_id"):
        return redirect(url_for("login.login"))
    owner_id = ObjectId(session["user_id"])
    store = _ensure_owner_store(owner_id, slug)
    if not store:
        return redirect(url_for("customer_store.customer_store_home"))

    name = (request.form.get("recipient_name") or "").strip()
    phone = (request.form.get("msisdn") or "").strip()
    network = (request.form.get("network") or "").strip().upper()

    valid_nets = {"MTN", "VODAFONE", "AIRTELTIGO"}
    if network not in valid_nets:
        return render_template(
            "customer_store_payout.html",
            store=store,
            current={"recipient_name": name, "msisdn": phone, "network": network},
            history=list(store_payout_logs.find({"owner_id": owner_id, "store_slug": slug}).sort("created_at", -1)),
            error="Select a valid network.",
            withdrawable=_withdrawable(slug),
            wallet_balance=_owner_wallet_balance(owner_id),
        )

    def _normalize_phone(raw: str) -> str:
        p = raw.replace(" ", "").replace("-", "").replace("+", "")
        if p.startswith("0") and len(p) == 10:
            p = "233" + p[1:]
        if p.startswith("233") and len(p) == 12:
            return p
        return raw.strip()

    phone_norm = _normalize_phone(phone)

    prev = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    doc = {
        "owner_id": owner_id,
        "store_slug": slug,
        "recipient_name": name,
        "msisdn": phone_norm,
        "network": network,
        "updated_at": datetime.utcnow(),
        "created_at": prev.get("created_at") or datetime.utcnow(),
    }
    store_payouts_col.update_one(
        {"owner_id": owner_id, "store_slug": slug},
        {"$set": doc},
        upsert=True
    )

    changes: Dict[str, Dict[str, Any]] = {}
    for k in ("recipient_name", "msisdn", "network"):
        old_v = prev.get(k)
        new_v = doc.get(k)
        if old_v != new_v:
            changes[k] = {"from": old_v, "to": new_v}
    if changes:
        store_payout_logs.insert_one({
            "owner_id": owner_id,
            "store_slug": slug,
            "changes": changes,
            "created_at": datetime.utcnow(),
        })

    return redirect(url_for("customer_store.customer_store_payout_page", slug=slug))
