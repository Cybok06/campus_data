from __future__ import annotations

from datetime import datetime, date, timedelta
from typing import Any, Dict, List, Tuple, Optional

from bson import ObjectId
from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from db import db

# IMPORTANT: used for safe one-time wallet credit
from pymongo import ReturnDocument

admin_store_bp = Blueprint("admin_store", __name__, url_prefix="/admin")

# Collections
stores_col        = db["stores"]
orders_col        = db["orders"]
users_col         = db["users"]
balances_col      = db["balances"]
transactions_col  = db["transactions"]
store_accounts_col = db["store_accounts"]

# payout settings & logs (read-only for admin)
store_payouts_col = db["store_payouts"]       # { owner_id, store_slug, recipient_name, msisdn, network, created_at, updated_at }
store_payout_logs = db["store_payout_logs"]   # { owner_id, store_slug, changes, created_at }


# ---------------- helpers ----------------
def _require_admin() -> bool:
    return bool(session.get("role") == "admin" and session.get("user_id"))


def _day_range(d: date) -> Tuple[datetime, datetime]:
    start = datetime.combine(d, datetime.min.time())
    end = start + timedelta(days=1)
    return start, end


def _fmt_money(x: Any) -> float:
    try:
        return round(float(x or 0), 2)
    except Exception:
        return 0.0


def _display_name(user_doc: Dict[str, Any] | None) -> str:
    if not user_doc:
        return "User"
    for key in ("full_name", "name"):
        if user_doc.get(key):
            return str(user_doc[key]).strip()
    if user_doc.get("username"):
        return str(user_doc["username"]).strip()
    if user_doc.get("email"):
        return str(user_doc["email"]).split("@", 1)[0]
    return "User"


def _withdrawn_reserved_so_far(store_slug: str) -> float:
    """
    Sum of withdrawals that should reduce withdrawable profit:
      - requested (owner asked; not yet processed)  -> reserved
      - pending momo payouts (reserved)
      - paid/success withdrawals
    Stored in transactions:
      type='store_withdrawal', meta.store_slug=<slug>, status in {'requested','pending','paid','success'}
    """
    pipeline = [
        {"$match": {
            "type": "store_withdrawal",
            "meta.store_slug": store_slug,
            "status": {"$in": ["requested", "pending", "paid", "success"]}
        }},
        {"$group": {"_id": None, "amt": {"$sum": {"$toDouble": {"$ifNull": ["$amount", 0]}}}}}
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return _fmt_money(agg[0]["amt"]) if agg else 0.0


def _withdrawn_paid_so_far(store_slug: str) -> float:
    """Sum of completed withdrawals (paid/success) for display."""
    pipeline = [
        {"$match": {
            "type": "store_withdrawal",
            "meta.store_slug": store_slug,
            "status": {"$in": ["paid", "success"]}
        }},
        {"$group": {"_id": None, "amt": {"$sum": {"$toDouble": {"$ifNull": ["$amount", 0]}}}}}
    ]
    agg = list(transactions_col.aggregate(pipeline))
    return _fmt_money(agg[0]["amt"]) if agg else 0.0


def _profit_all_time(store_slug: str) -> float:
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
        {"$match": {"store_slug": store_slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {"_id": None, "p": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}}}}
    ]
    agg = list(orders_col.aggregate(pipeline))
    return _fmt_money(agg[0]["p"]) if agg else 0.0


def _store_account_balance(store_slug: str) -> float:
    acct = store_accounts_col.find_one({"store_slug": store_slug}, {"total_profit_balance": 1}) or {}
    return _fmt_money(acct.get("total_profit_balance"))


def _owner_wallet_balance(user_id: Optional[ObjectId]) -> float:
    if not user_id:
        return 0.0
    bal_doc = balances_col.find_one({"user_id": user_id}) or {}
    return _fmt_money(bal_doc.get("amount"))


def _payout_payload(owner_id: Optional[ObjectId], slug: str) -> Dict[str, Any]:
    payout = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    history_count = store_payout_logs.count_documents({"owner_id": owner_id, "store_slug": slug}) if owner_id else 0
    return {
        "recipient_name": payout.get("recipient_name"),
        "msisdn": payout.get("msisdn"),
        "network": payout.get("network"),
        "created_at": payout.get("created_at").isoformat() if isinstance(payout.get("created_at"), datetime) else payout.get("created_at"),
        "updated_at": payout.get("updated_at").isoformat() if isinstance(payout.get("updated_at"), datetime) else payout.get("updated_at"),
        "history_count": history_count,
        # convenience
        "name": payout.get("recipient_name"),
        "phone": payout.get("msisdn"),
        "provider": payout.get("network"),
    }


def _require_payout_ready(p: Dict[str, Any]) -> Tuple[bool, str]:
    name = (p.get("recipient_name") or p.get("name") or "").strip()
    msisdn = (p.get("msisdn") or p.get("phone") or "").strip()
    network = (p.get("network") or p.get("provider") or "").strip()
    if not name:
        return False, "Payout name not set for this store"
    if not msisdn:
        return False, "Payout phone number not set for this store"
    if not network:
        return False, "Payout network not set for this store"
    return True, ""


def _pending_withdraw_requests_count(store_slug: str) -> int:
    """
    Count withdrawals that still need admin action.
    We consider:
      - status 'requested' (owner asked)
      - status 'pending'   (momo initiated but not marked paid)
    """
    return int(transactions_col.count_documents({
        "type": "store_withdrawal",
        "meta.store_slug": store_slug,
        "status": {"$in": ["requested", "pending"]}
    }))


def _pending_requests_map_for_slugs(slugs: List[str]) -> Dict[str, int]:
    if not slugs:
        return {}
    pipeline = [
        {"$match": {
            "type": "store_withdrawal",
            "meta.store_slug": {"$in": slugs},
            "status": {"$in": ["requested", "pending"]}
        }},
        {"$group": {"_id": "$meta.store_slug", "c": {"$sum": 1}}}
    ]
    out = {}
    for x in transactions_col.aggregate(pipeline):
        out[str(x["_id"])] = int(x.get("c", 0))
    return out


def _tx_destination(meta: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a friendly destination block for UI history.
    - wallet -> {"kind":"wallet","label":"Wallet"}
    - momo   -> {"kind":"momo","label":"MoMo • <net> • <phone> • <name>", "name","phone","network"}
    Uses payout snapshot if present.
    """
    method = (meta.get("method") or meta.get("payout_method") or "wallet").strip().lower()
    if method != "momo":
        return {"kind": "wallet", "label": "Wallet"}

    snap = meta.get("payout_snapshot") or {}
    name = (snap.get("recipient_name") or snap.get("name") or "").strip()
    phone = (snap.get("msisdn") or snap.get("phone") or "").strip()
    net = (snap.get("network") or snap.get("provider") or "").strip()

    bits = [b for b in ["MoMo", net, phone, name] if b]
    return {
        "kind": "momo",
        "name": name or None,
        "phone": phone or None,
        "network": net or None,
        "label": " • ".join(bits) if bits else "MoMo"
    }


def _get_tx_method(tx: Dict[str, Any]) -> str:
    meta = tx.get("meta") or {}
    return (meta.get("method") or meta.get("payout_method") or "wallet").strip().lower()


def _safe_objectid(x: Any) -> Optional[ObjectId]:
    try:
        return ObjectId(str(x))
    except Exception:
        return None


def _iso(dt: Any) -> Any:
    return dt.isoformat() if isinstance(dt, datetime) else dt


# ---------------- pages ----------------
@admin_store_bp.route("/stores", methods=["GET"])
def admin_stores_page():
    if not _require_admin():
        return redirect(url_for("login.login"))
    return render_template("admin_stores.html")


# ---------------- APIs ----------------
@admin_store_bp.route("/api/stores", methods=["GET"])
def api_admin_list_stores():
    """
    Returns a row per store with owner info, KPIs (today + all-time), status, withdrawable profit,
    plus pending withdrawal requests count (requested/pending).
    Optional params:
      - q: search by store/owner/slug (case-insensitive substring)
      - status: published|suspended|draft
      - page, limit
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    q = (request.args.get("q") or "").strip().lower()
    status_filter = (request.args.get("status") or "").strip().lower()

    stats = {
        "total_stores": 0,
        "published": 0,
        "draft": 0,
        "suspended": 0,
        "outstanding_payouts": 0.0,
        "sales_today_total": 0.0,
        "sales_yesterday_total": 0.0,
    }
    try:
        stats_pipeline = [
            {"$match": {"status": {"$ne": "deleted"}}},
            {"$lookup": {
                "from": "store_accounts",
                "localField": "slug",
                "foreignField": "store_slug",
                "as": "acct",
            }},
            {"$unwind": {"path": "$acct", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": None,
                "total": {"$sum": 1},
                "published": {"$sum": {"$cond": [{"$eq": ["$status", "published"]}, 1, 0]}},
                "draft": {"$sum": {"$cond": [{"$eq": ["$status", "draft"]}, 1, 0]}},
                "suspended": {"$sum": {"$cond": [{"$eq": ["$status", "suspended"]}, 1, 0]}},
                "outstanding": {"$sum": {"$toDouble": {"$ifNull": ["$acct.total_profit_balance", 0]}}},
            }},
        ]
        agg_stats = list(stores_col.aggregate(stats_pipeline))
        if agg_stats:
            s0 = agg_stats[0]
            stats = {
                "total_stores": int(s0.get("total", 0) or 0),
                "published": int(s0.get("published", 0) or 0),
                "draft": int(s0.get("draft", 0) or 0),
                "suspended": int(s0.get("suspended", 0) or 0),
                "outstanding_payouts": _fmt_money(s0.get("outstanding")),
                "sales_today_total": 0.0,
                "sales_yesterday_total": 0.0,
            }
    except Exception:
        stats = stats

    try:
        limit = max(1, min(200, int(request.args.get("limit", 50))))
    except Exception:
        limit = 50
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    skip = (page - 1) * limit

    stores_query: Dict[str, Any] = {"status": {"$ne": "deleted"}}
    if status_filter in {"published", "suspended", "draft"}:
        stores_query["status"] = status_filter

    cur = (
        stores_col.find(stores_query)
        .sort([("updated_at", -1), ("created_at", -1)])
        .skip(skip)
        .limit(limit)
    )
    store_docs = list(cur)

    owner_ids = list({s.get("owner_id") for s in store_docs if s.get("owner_id")})
    owners: Dict[ObjectId, Dict[str, Any]] = {}
    if owner_ids:
        owners = {
            o["_id"]: o
            for o in users_col.find(
                {"_id": {"$in": owner_ids}},
                {"full_name": 1, "name": 1, "username": 1, "email": 1}
            )
        }

    today = datetime.utcnow().date()
    d0, d1 = _day_range(today)
    yday = today - timedelta(days=1)
    y0, y1 = _day_range(yday)
    slugs = [s.get("slug") for s in store_docs if s.get("slug")]

    def _sum_sales_range(start_dt: datetime, end_dt: datetime) -> float:
        pipe = [
            {"$match": {"created_at": {"$gte": start_dt, "$lt": end_dt}, "store_slug": {"$exists": True, "$ne": None}}},
            {"$group": {"_id": None, "sales": {"$sum": {"$toDouble": {"$ifNull": ["$total_amount", 0]}}}}},
        ]
        agg = list(orders_col.aggregate(pipe))
        return _fmt_money(agg[0]["sales"]) if agg else 0.0

    stats["sales_today_total"] = _sum_sales_range(d0, d1)
    stats["sales_yesterday_total"] = _sum_sales_range(y0, y1)

    acct_map: Dict[str, Dict[str, Any]] = {}
    if slugs:
        acct_map = {
            a.get("store_slug"): a
            for a in store_accounts_col.find(
                {"store_slug": {"$in": slugs}},
                {"store_slug": 1, "total_profit_balance": 1},
            )
        }

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
    pipeline_all = [
        {"$match": {"store_slug": {"$in": slugs}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": "$store_slug",
            "total_sales": {"$sum": {"$toDouble": {"$ifNull": ["$total_amount", 0]}}},
            "total_profit": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}},
            "orders_count": {"$sum": 1}
        }},
    ]
    all_time_map = {x["_id"]: x for x in orders_col.aggregate(pipeline_all)} if slugs else {}

    pipeline_today = [
        {"$match": {"store_slug": {"$in": slugs}, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": "$store_slug",
            "sales":  {"$sum": {"$toDouble": {"$ifNull": ["$total_amount", 0]}}},
            "profit": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}},
            "orders": {"$sum": 1}
        }},
    ]
    today_map = {x["_id"]: x for x in orders_col.aggregate(pipeline_today)} if slugs else {}

    pending_map = _pending_requests_map_for_slugs(slugs)

    rows: List[Dict[str, Any]] = []
    for s in store_docs:
        slug = s.get("slug")
        if not slug:
            continue

        owner_id = s.get("owner_id")
        owner = owners.get(owner_id)
        owner_name = _display_name(owner)

        # search filter
        if q:
            blob = f"{(s.get('name') or '').lower()} {slug.lower()} {owner_name.lower()}"
            if q not in blob:
                continue

        a = all_time_map.get(slug, {})
        t = today_map.get(slug, {})

        total_sales = _fmt_money(a.get("total_sales"))
        total_profit = _fmt_money(a.get("total_profit"))
        orders_count = int(a.get("orders_count", 0))

        sales_today = _fmt_money(t.get("sales"))
        profit_today = _fmt_money(t.get("profit"))
        orders_today = int(t.get("orders", 0))

        withdrawn_reserved = _withdrawn_reserved_so_far(slug)
        acct = acct_map.get(slug) or {}
        withdrawable = _fmt_money(acct.get("total_profit_balance"))

        pending_requests = int(pending_map.get(slug, 0))

        rows.append({
            "slug": slug,
            "name": s.get("name"),
            "status": s.get("status") or "draft",
            "owner_id": str(owner_id) if owner_id else None,
            "owner_name": owner_name,

            "sales_today": sales_today,
            "profit_today": profit_today,
            "orders_today": orders_today,

            "total_sales": total_sales,
            "total_profit": total_profit,
            "orders_count": orders_count,

            "withdrawn_reserved": withdrawn_reserved,
            "withdrawable": withdrawable,

            "pending_requests": pending_requests,

            "updated_at": _iso(s.get("updated_at")),
        })

    return jsonify({"success": True, "rows": rows, "page": page, "limit": limit, "stats": stats})


@admin_store_bp.route("/api/stores/<slug>", methods=["GET"])
def api_admin_store_summary(slug: str):
    """
    Single store summary (for modal).
    Returns: store basics, owner, wallet, KPIs (today & all-time), withdrawable,
             + payout details, + pending withdrawal requests count.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    store = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if not store:
        return jsonify({"success": False, "message": "Store not found"}), 404

    owner_id: Optional[ObjectId] = store.get("owner_id")

    owner = users_col.find_one(
        {"_id": owner_id},
        {"full_name": 1, "name": 1, "username": 1, "email": 1}
    ) if owner_id else None

    owner_name = _display_name(owner)
    owner_wallet = _owner_wallet_balance(owner_id)

    # KPIs
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
    pipeline_all = [
        {"$match": {"store_slug": slug}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": None,
            "total_sales": {"$sum": {"$toDouble": {"$ifNull": ["$total_amount", 0]}}},
            "total_profit": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}},
            "orders_count": {"$sum": 1}
        }},
    ]
    agg_all = list(orders_col.aggregate(pipeline_all))
    total_sales = _fmt_money(agg_all[0].get("total_sales") if agg_all else 0)
    total_profit = _fmt_money(agg_all[0].get("total_profit") if agg_all else 0)
    orders_count = int(agg_all[0].get("orders_count") if agg_all else 0)

    pipeline_today = [
        {"$match": {"store_slug": slug, "created_at": {"$gte": d0, "$lt": d1}}},
        {"$addFields": {"store_profit_sum": order_profit_expr}},
        {"$group": {
            "_id": None,
            "sales":  {"$sum": {"$toDouble": {"$ifNull": ["$total_amount", 0]}}},
            "profit": {"$sum": {"$toDouble": {"$ifNull": ["$store_profit_sum", 0]}}},
            "orders": {"$sum": 1}
        }},
    ]
    agg_today = list(orders_col.aggregate(pipeline_today))
    sales_today = _fmt_money(agg_today[0].get("sales") if agg_today else 0)
    profit_today = _fmt_money(agg_today[0].get("profit") if agg_today else 0)
    orders_today = int(agg_today[0].get("orders") if agg_today else 0)

    withdrawn_reserved = _withdrawn_reserved_so_far(slug)
    withdrawn_paid = _withdrawn_paid_so_far(slug)
    withdrawable = _store_account_balance(slug)

    payout_payload = _payout_payload(owner_id, slug)
    pending_requests = _pending_withdraw_requests_count(slug)

    return jsonify({
        "success": True,
        "store": {
            "slug": slug,
            "name": store.get("name"),
            "status": store.get("status") or "draft",
            "owner_id": str(owner_id) if owner_id else None,
            "owner_name": owner_name,
            "owner_wallet": owner_wallet,

            "today": {"sales": sales_today, "profit": profit_today, "orders": orders_today},
            "all_time": {"sales": total_sales, "profit": total_profit, "orders": orders_count},

            "withdrawn_reserved": withdrawn_reserved,
            "withdrawn_paid": withdrawn_paid,
            "withdrawable": withdrawable,

            "pending_requests": pending_requests,

            "updated_at": _iso(store.get("updated_at")),
            "payout": payout_payload,
        }
    })


@admin_store_bp.route("/api/stores/<slug>/withdrawals", methods=["GET"])
def api_admin_store_withdrawals(slug: str):
    """
    Paginated list of withdrawals for a store (most recent first).
    Query params: page (default 1), limit (default 20)
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    store = stores_col.find_one({"slug": slug})
    if not store:
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
        {"type": "store_withdrawal", "meta.store_slug": slug}
    ).sort("created_at", -1).skip(skip).limit(limit)

    items: List[Dict[str, Any]] = []
    for t in cur:
        meta = t.get("meta") or {}
        method = (meta.get("method") or meta.get("payout_method") or "wallet").strip().lower()
        dest = _tx_destination(meta)
        snap = (meta.get("payout_snapshot") or {}) if isinstance(meta.get("payout_snapshot"), dict) else {}

        momo = None
        if method == "momo":
            momo = {
                "name": (snap.get("recipient_name") or snap.get("name") or "").strip() or None,
                "phone": (snap.get("msisdn") or snap.get("phone") or "").strip() or None,
                "network": (snap.get("network") or snap.get("provider") or "").strip() or None,
            }

        items.append({
            "id": str(t.get("_id")),
            "reference": t.get("reference"),
            "amount": _fmt_money(t.get("amount")),
            "status": (t.get("status") or "").strip().lower(),
            "method": method,
            "destination": dest,
            "momo": momo,  # explicit momo details (easy for UI)
            "created_at": _iso(t.get("created_at")),
            "verified_at": _iso(t.get("verified_at")),
            "note": meta.get("note"),
            "admin_note": meta.get("admin_note"),
            "gateway": t.get("gateway") or ("MoMo" if (method == "momo") else "Internal"),
            "currency": t.get("currency") or "GHS",
        })

    total_count = transactions_col.count_documents({"type": "store_withdrawal", "meta.store_slug": slug})

    return jsonify({
        "success": True,
        "slug": slug,
        "page": page,
        "limit": limit,
        "total": total_count,
        "items": items
    })


@admin_store_bp.route("/api/withdrawals/requests", methods=["GET"])
def api_admin_withdrawal_requests_global():
    """
    NEW: Global list of withdrawals that need admin attention across ALL stores.

    Query params:
      - status: requested|pending|all   (default all => requested+pending)
      - q: free text search in store slug/name, owner name/email, reference
      - page, limit

    Returns items with: store info, owner, momo details (if any), status, reference, timestamps.
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    q = (request.args.get("q") or "").strip().lower()
    st = (request.args.get("status") or "all").strip().lower()

    if st == "requested":
        st_set = ["requested"]
    elif st == "pending":
        st_set = ["pending"]
    else:
        st_set = ["requested", "pending"]

    try:
        limit = max(1, min(200, int(request.args.get("limit", 20))))
    except Exception:
        limit = 20
    try:
        page = max(1, int(request.args.get("page", 1)))
    except Exception:
        page = 1
    skip = (page - 1) * limit

    base_q: Dict[str, Any] = {
        "type": "store_withdrawal",
        "status": {"$in": st_set},
    }

    cur = transactions_col.find(base_q).sort("created_at", -1).skip(skip).limit(limit)
    txs = list(cur)

    # Collect slugs + owner ids
    slugs = []
    owner_ids: List[ObjectId] = []
    for t in txs:
        meta = t.get("meta") or {}
        slug = meta.get("store_slug")
        if slug:
            slugs.append(str(slug))
        uid = t.get("user_id")
        if isinstance(uid, ObjectId):
            owner_ids.append(uid)

    store_map: Dict[str, Dict[str, Any]] = {}
    if slugs:
        for s in stores_col.find({"slug": {"$in": list(set(slugs))}}, {"slug": 1, "name": 1, "owner_id": 1}):
            store_map[str(s.get("slug"))] = s

    owner_map: Dict[ObjectId, Dict[str, Any]] = {}
    if owner_ids:
        for u in users_col.find({"_id": {"$in": list(set(owner_ids))}}, {"full_name": 1, "name": 1, "username": 1, "email": 1}):
            owner_map[u["_id"]] = u

    items: List[Dict[str, Any]] = []
    for t in txs:
        meta = t.get("meta") or {}
        slug = str(meta.get("store_slug") or "")
        store = store_map.get(slug) or {}
        owner_id = t.get("user_id")
        owner = owner_map.get(owner_id) if isinstance(owner_id, ObjectId) else None

        store_name = store.get("name") or slug or "Store"
        owner_name = _display_name(owner)
        ref = t.get("reference") or ""

        # Search filter
        if q:
            blob = f"{slug} {store_name} {owner_name} {ref}".lower()
            if owner and owner.get("email"):
                blob += " " + str(owner["email"]).lower()
            if q not in blob:
                continue

        method = _get_tx_method(t)
        snap = (meta.get("payout_snapshot") or {}) if isinstance(meta.get("payout_snapshot"), dict) else {}

        momo = None
        if method == "momo":
            momo = {
                "name": (snap.get("recipient_name") or snap.get("name") or "").strip() or None,
                "phone": (snap.get("msisdn") or snap.get("phone") or "").strip() or None,
                "network": (snap.get("network") or snap.get("provider") or "").strip() or None,
            }

        items.append({
            "id": str(t.get("_id")),
            "reference": ref,
            "status": (t.get("status") or "").strip().lower(),
            "amount": _fmt_money(t.get("amount")),
            "method": method,
            "momo": momo,
            "store_slug": slug,
            "store_name": store_name,
            "owner_name": owner_name,
            "created_at": _iso(t.get("created_at")),
            "verified_at": _iso(t.get("verified_at")),
            "note": meta.get("note"),
            "admin_note": meta.get("admin_note"),
        })

    total = transactions_col.count_documents(base_q)
    return jsonify({
        "success": True,
        "page": page,
        "limit": limit,
        "total": total,
        "items": items,
    })


@admin_store_bp.route("/api/stores/<slug>/status", methods=["POST"])
def api_admin_update_store_status(slug: str):
    """
    body: { "action": "suspend"|"resume"|"delete" }
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    body = request.get_json(silent=True) or {}
    action = (body.get("action") or "").strip().lower()

    if action == "suspend":
        res = stores_col.update_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"$set": {"status": "suspended", "updated_at": datetime.utcnow()}}
        )
    elif action == "resume":
        res = stores_col.update_one(
            {"slug": slug, "status": {"$ne": "deleted"}},
            {"$set": {"status": "published", "updated_at": datetime.utcnow()}}
        )
    elif action == "delete":
        res = stores_col.update_one(
            {"slug": slug},
            {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}}
        )
    else:
        return jsonify({"success": False, "message": "Invalid action"}), 400

    if res.matched_count == 0:
        return jsonify({"success": False, "message": "Store not found"}), 404

    return jsonify({"success": True, "slug": slug, "action": action})


@admin_store_bp.route("/api/stores/suspend_all", methods=["POST"])
def api_admin_suspend_all():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    res = stores_col.update_many(
        {"status": {"$ne": "deleted"}},
        {"$set": {"status": "suspended", "updated_at": datetime.utcnow()}}
    )
    return jsonify({"success": True, "affected": res.modified_count})


@admin_store_bp.route("/api/stores/resume_all", methods=["POST"])
def api_admin_resume_all():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    res = stores_col.update_many(
        {"status": "suspended"},
        {"$set": {"status": "published", "updated_at": datetime.utcnow()}}
    )
    return jsonify({"success": True, "affected": res.modified_count})


@admin_store_bp.route("/api/stores/<slug>/deposit", methods=["POST"])
def api_admin_store_deposit(slug: str):
    """
    Admin deposit to store profit account.
    body: { "amount": <number>, "note": "optional admin note" }
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    try:
        store = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
        if not store:
            return jsonify({"success": False, "message": "Store not found"}), 404

        body = request.get_json(silent=True) or {}
        amount = _fmt_money(body.get("amount"))
        if amount <= 0:
            return jsonify({"success": False, "message": "Amount must be greater than 0"}), 400

        note = (body.get("note") or "").strip()
        now = datetime.utcnow()
        admin_oid = _safe_objectid(session.get("user_id"))
        ref = f"DEP-{slug}-{int(now.timestamp())}"

        history_entry = {
            "event": "deposit",
            "amount": amount,
            "reference": ref,
            "status": "success",
            "created_at": now,
            "processed_by": admin_oid,
            "note": note,
        }

        acct = store_accounts_col.find_one_and_update(
            {"store_slug": slug},
            {
                "$set": {"updated_at": now},
                "$setOnInsert": {
                    "store_slug": slug,
                    "created_at": now,
                },
                "$inc": {"total_profit_balance": amount},
                "$push": {"history": history_entry},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

        new_balance = _fmt_money((acct or {}).get("total_profit_balance", 0))

        return jsonify({
            "success": True,
            "slug": slug,
            "reference": ref,
            "deposited": amount,
            "withdrawable": new_balance,
        })
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"success": False, "message": "Deposit failed", "error": str(e)}), 500


@admin_store_bp.route("/api/stores/<slug>/withdraw", methods=["POST"])
def api_admin_store_withdraw(slug: str):
    """
    Admin withdraw profit using either:
      - method=wallet: credits owner wallet (balances) immediately and logs success
      - method=momo: creates a PENDING MoMo payout tx, admin later MARKS PAID
    body:
      { "amount": <number|null|'all'>, "method": "wallet"|"momo" }

    withdrawable = store_accounts.total_profit_balance (current store profit balance)
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    store = stores_col.find_one({"slug": slug, "status": {"$ne": "deleted"}})
    if not store:
        return jsonify({"success": False, "message": "Store not found"}), 404

    owner_id: Optional[ObjectId] = store.get("owner_id")
    if not owner_id:
        return jsonify({"success": False, "message": "Store missing owner"}), 400

    body = request.get_json(silent=True) or {}
    method = (body.get("method") or "wallet").strip().lower()
    if method not in {"wallet", "momo"}:
        return jsonify({"success": False, "message": "Invalid method. Use 'wallet' or 'momo'."}), 400

    withdrawable = _store_account_balance(slug)

    amt_req = body.get("amount", "all")
    if isinstance(amt_req, str) and amt_req.strip().lower() == "all":
        amount = withdrawable
    else:
        amount = _fmt_money(amt_req)

    if amount <= 0:
        return jsonify({"success": False, "message": "Nothing to withdraw"}), 400
    if amount - withdrawable > 1e-9:
        return jsonify({"success": False, "message": "Amount exceeds withdrawable"}), 400

    payout = store_payouts_col.find_one({"owner_id": owner_id, "store_slug": slug}) or {}
    payout_snapshot = {
        "recipient_name": payout.get("recipient_name"),
        "msisdn": payout.get("msisdn"),
        "network": payout.get("network"),
    }

    # if momo, payout details must exist
    if method == "momo":
        ok, msg = _require_payout_ready({
            "recipient_name": payout_snapshot.get("recipient_name"),
            "msisdn": payout_snapshot.get("msisdn"),
            "network": payout_snapshot.get("network"),
        })
        if not ok:
            return jsonify({"success": False, "message": msg}), 400

    ref = f"WDR-{slug}-{int(datetime.utcnow().timestamp())}"
    now = datetime.utcnow()
    admin_oid = _safe_objectid(session.get("user_id"))
    history_entry = {
        "event": "withdrawal",
        "amount": amount,
        "method": method,
        "reference": ref,
        "status": ("success" if method == "wallet" else "pending"),
        "created_at": now,
        "processed_by": admin_oid,
    }

    updated = store_accounts_col.update_one(
        {"store_slug": slug, "total_profit_balance": {"$gte": amount}},
        {
            "$inc": {"total_profit_balance": -amount},
            "$set": {"updated_at": now},
            "$push": {"history": history_entry},
        },
    )
    if updated.matched_count == 0:
        return jsonify({"success": False, "message": "Insufficient store profit balance"}), 400

    if method == "wallet":
        # credit owner's wallet
        balances_col.update_one(
            {"user_id": owner_id},
            {"$inc": {"amount": amount}, "$set": {"updated_at": now}},
            upsert=True
        )

        transactions_col.insert_one({
            "user_id": owner_id,
            "amount": amount,
            "reference": ref,
            "status": "success",
            "type": "store_withdrawal",
            "gateway": "Internal",
            "currency": "GHS",
            "created_at": now,
            "verified_at": now,
            "meta": {
                "store_slug": slug,
                "method": "wallet",
                "note": "Admin credited store profit to owner wallet",
                "processed_by": admin_oid,
                "wallet_credited": True,  # safety flag
                "payout_snapshot": payout_snapshot,
            }
        })

        new_wallet = _owner_wallet_balance(owner_id)
        new_withdrawable = max(0.0, round(withdrawable - amount, 2))

        copy_line = "Name: {name} | Number: {num} | Network: {net}".format(
            name=payout_snapshot.get("recipient_name") or "-",
            num=payout_snapshot.get("msisdn") or "-",
            net=payout_snapshot.get("network") or "-",
        )

        return jsonify({
            "success": True,
            "slug": slug,
            "method": "wallet",
            "reference": ref,
            "credited": amount,
            "withdrawable_left": new_withdrawable,
            "owner_wallet": new_wallet,
            "payout": {
                "recipient_name": payout_snapshot.get("recipient_name"),
                "msisdn": payout_snapshot.get("msisdn"),
                "network": payout_snapshot.get("network"),
                "copy_line": copy_line,
            }
        })

    # method == momo: create a pending payout (reserve funds), NO wallet credit
    transactions_col.insert_one({
        "user_id": owner_id,
        "amount": amount,
        "reference": ref,
        "status": "pending",
        "type": "store_withdrawal",
        "gateway": "MoMo",
        "currency": "GHS",
        "created_at": now,
        "verified_at": None,
        "meta": {
            "store_slug": slug,
            "method": "momo",
            "note": "Admin initiated MoMo payout (pending).",
            "processed_by": admin_oid,
            "payout_snapshot": payout_snapshot,
        }
    })

    new_withdrawable = max(0.0, round(withdrawable - amount, 2))

    copy_line = "Send MoMo: {amt} GHS | Name: {name} | Number: {num} | Network: {net}".format(
        amt=f"{amount:.2f}",
        name=payout_snapshot.get("recipient_name") or "-",
        num=payout_snapshot.get("msisdn") or "-",
        net=payout_snapshot.get("network") or "-"
    )

    return jsonify({
        "success": True,
        "slug": slug,
        "method": "momo",
        "reference": ref,
        "credited": 0.0,
        "momo_amount": amount,
        "withdrawable_left": new_withdrawable,
        "owner_wallet": _owner_wallet_balance(owner_id),
        "payout": {
            "recipient_name": payout_snapshot.get("recipient_name"),
            "msisdn": payout_snapshot.get("msisdn"),
            "network": payout_snapshot.get("network"),
            "copy_line": copy_line,
        },
        "message": "MoMo withdrawal created as PENDING. Send the MoMo using the payout details, then mark as PAID."
    })


# ---------------- Admin actions on withdrawals ----------------
@admin_store_bp.route("/api/withdrawals/<tx_id>/mark_paid", methods=["POST"])
def api_admin_mark_withdrawal_paid(tx_id: str):
    """
    Mark a withdrawal as PAID (or SUCCESS for wallet).
    - If method == wallet and tx is requested/pending -> credits wallet now (ONLY ONCE), marks success
    - If method == momo  and tx is requested/pending -> marks paid
    body: { "admin_note": "optional note" }
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    oid = _safe_objectid(tx_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid tx id"}), 400

    tx = transactions_col.find_one({"_id": oid, "type": "store_withdrawal"})
    if not tx:
        return jsonify({"success": False, "message": "Withdrawal not found"}), 404

    status = (tx.get("status") or "").strip().lower()
    if status in {"paid", "success"}:
        return jsonify({"success": True, "message": "Already completed"}), 200

    if status not in {"requested", "pending"}:
        return jsonify({"success": False, "message": f"Cannot mark paid from status '{status}'"}), 400

    meta = tx.get("meta") or {}
    method = (meta.get("method") or meta.get("payout_method") or "wallet").strip().lower()

    body = request.get_json(silent=True) or {}
    admin_note = (body.get("admin_note") or "").strip()

    admin_oid = _safe_objectid(session.get("user_id"))
    user_id = tx.get("user_id")
    amount = _fmt_money(tx.get("amount"))

    # wallet: credit now and mark success (race-safe via credit_lock)
    if method != "momo":
        if not user_id:
            return jsonify({"success": False, "message": "Missing withdrawal user_id"}), 400

        # If already credited, just finalize status
        already = bool((meta or {}).get("wallet_credited") is True)
        if not already:
            locked = transactions_col.find_one_and_update(
                {
                    "_id": oid,
                    "type": "store_withdrawal",
                    "status": {"$in": ["requested", "pending"]},
                    "meta.wallet_credited": {"$ne": True},
                    "meta.credit_lock": {"$ne": True},
                },
                {"$set": {"meta.credit_lock": True}},
                return_document=ReturnDocument.AFTER
            )
            if locked:
                balances_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"amount": amount}, "$set": {"updated_at": datetime.utcnow()}},
                    upsert=True
                )
                already = True

        upd = {
            "$set": {
                "status": "success",
                "verified_at": datetime.utcnow(),
                "gateway": tx.get("gateway") or "Internal",
                "meta.processed_by": admin_oid,
                "meta.wallet_credited": True,
            },
            "$unset": {"meta.credit_lock": ""}  # remove lock
        }
        if admin_note:
            upd["$set"]["meta.admin_note"] = admin_note

        transactions_col.update_one({"_id": oid}, upd)
        return jsonify({"success": True, "method": "wallet", "status": "success"})

    # momo: just mark paid
    upd = {
        "$set": {
            "status": "paid",
            "verified_at": datetime.utcnow(),
            "gateway": tx.get("gateway") or "MoMo",
            "meta.processed_by": admin_oid,
        }
    }
    if admin_note:
        upd["$set"]["meta.admin_note"] = admin_note

    transactions_col.update_one({"_id": oid}, upd)
    return jsonify({"success": True, "method": "momo", "status": "paid"})


@admin_store_bp.route("/api/withdrawals/<tx_id>/reject", methods=["POST"])
def api_admin_reject_withdrawal(tx_id: str):
    """
    Reject a withdrawal request/pending payout.
    This removes it from reserved sums because withdrawable calc ignores 'rejected'.
    body: { "admin_note": "reason" }
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    oid = _safe_objectid(tx_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid tx id"}), 400

    tx = transactions_col.find_one({"_id": oid, "type": "store_withdrawal"})
    if not tx:
        return jsonify({"success": False, "message": "Withdrawal not found"}), 404

    status = (tx.get("status") or "").strip().lower()
    if status in {"paid", "success"}:
        return jsonify({"success": False, "message": "Cannot reject a completed withdrawal"}), 400

    if status not in {"requested", "pending"}:
        return jsonify({"success": False, "message": f"Cannot reject from status '{status}'"}), 400

    body = request.get_json(silent=True) or {}
    admin_note = (body.get("admin_note") or "").strip()

    admin_oid = _safe_objectid(session.get("user_id"))
    amount = _fmt_money(tx.get("amount"))
    store_slug = (tx.get("meta") or {}).get("store_slug")
    already_refunded = bool((tx.get("meta") or {}).get("refunded") is True)

    upd = {
        "$set": {
            "status": "rejected",
            "verified_at": datetime.utcnow(),
            "meta.processed_by": admin_oid,
            "meta.refunded": True if (store_slug and amount > 0 and not already_refunded) else (tx.get("meta") or {}).get("refunded"),
        },
        "$unset": {"meta.credit_lock": ""}  # clear lock if any
    }
    if admin_note:
        upd["$set"]["meta.admin_note"] = admin_note

    transactions_col.update_one({"_id": oid}, upd)

    if store_slug and amount > 0 and not already_refunded:
        store_accounts_col.update_one(
            {"store_slug": store_slug},
            {
                "$inc": {"total_profit_balance": amount},
                "$set": {"updated_at": datetime.utcnow()},
                "$push": {
                    "history": {
                        "event": "withdrawal_refund",
                        "amount": amount,
                        "method": (tx.get("meta") or {}).get("method") or "wallet",
                        "reference": tx.get("reference"),
                        "status": "rejected",
                        "created_at": datetime.utcnow(),
                        "processed_by": admin_oid,
                    }
                },
            },
        )

    return jsonify({"success": True, "status": "rejected"})


@admin_store_bp.route("/api/withdrawals/<tx_id>/set_status", methods=["POST"])
def api_admin_set_withdrawal_status(tx_id: str):
    """
    Set withdrawal status manually (admin control).
    body: { "status": "requested|pending|paid|success|rejected|failed", "admin_note": "" }

    Safety:
      - If method=wallet and status becomes 'success': wallet credit happens ONLY ONCE (credit_lock + wallet_credited)
      - If method=momo: status updates only, no wallet credit
    """
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 401

    oid = _safe_objectid(tx_id)
    if not oid:
        return jsonify({"success": False, "message": "Invalid tx id"}), 400

    body = request.get_json(silent=True) or {}
    new_status = (body.get("status") or "").strip().lower()
    admin_note = (body.get("admin_note") or "").strip()

    allowed = {"requested", "pending", "paid", "success", "rejected", "failed"}
    if new_status not in allowed:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    tx = transactions_col.find_one({"_id": oid, "type": "store_withdrawal"})
    if not tx:
        return jsonify({"success": False, "message": "Withdrawal not found"}), 404

    meta = tx.get("meta") or {}
    method = _get_tx_method(tx)
    amount = _fmt_money(tx.get("amount"))
    user_id = tx.get("user_id")
    admin_oid = _safe_objectid(session.get("user_id"))

    # If completed, don't allow downgrading (avoid tampering)
    cur_status = (tx.get("status") or "").strip().lower()
    if cur_status in {"paid", "success"} and new_status not in {"paid", "success"}:
        return jsonify({"success": False, "message": "Cannot downgrade a completed withdrawal"}), 400

    store_slug = (tx.get("meta") or {}).get("store_slug")
    already_refunded = bool((tx.get("meta") or {}).get("refunded") is True)

    upd_set: Dict[str, Any] = {
        "status": new_status,
        "meta.processed_by": admin_oid,
    }

    # verified_at logic
    if new_status in {"paid", "success", "rejected", "failed"}:
        upd_set["verified_at"] = datetime.utcnow()
    else:
        upd_set["verified_at"] = None

    if admin_note:
        upd_set["meta.admin_note"] = admin_note

    # wallet credit only if wallet + success and not already credited (use credit_lock)
    if method != "momo" and new_status == "success":
        if not user_id:
            return jsonify({"success": False, "message": "Missing withdrawal user_id"}), 400

        already = bool(meta.get("wallet_credited") is True)
        if not already:
            locked = transactions_col.find_one_and_update(
                {
                    "_id": oid,
                    "type": "store_withdrawal",
                    "meta.wallet_credited": {"$ne": True},
                    "meta.credit_lock": {"$ne": True},
                },
                {"$set": {"meta.credit_lock": True}},
                return_document=ReturnDocument.AFTER
            )
            if locked:
                balances_col.update_one(
                    {"user_id": user_id},
                    {"$inc": {"amount": amount}, "$set": {"updated_at": datetime.utcnow()}},
                    upsert=True
                )
                upd_set["meta.wallet_credited"] = True

        upd_set["meta.wallet_credited"] = True
        upd_set["gateway"] = tx.get("gateway") or "Internal"

    if method == "momo":
        upd_set["gateway"] = tx.get("gateway") or "MoMo"

    # clear lock when leaving non-success states too
    upd = {"$set": upd_set}
    if new_status in {"rejected", "failed", "paid", "success"}:
        upd["$unset"] = {"meta.credit_lock": ""}

    transactions_col.update_one({"_id": oid}, upd)

    if (
        store_slug
        and amount > 0
        and new_status in {"rejected", "failed"}
        and cur_status in {"requested", "pending"}
        and not already_refunded
    ):
        store_accounts_col.update_one(
            {"store_slug": store_slug},
            {
                "$inc": {"total_profit_balance": amount},
                "$set": {"updated_at": datetime.utcnow()},
                "$push": {
                    "history": {
                        "event": "withdrawal_refund",
                        "amount": amount,
                        "method": method,
                        "reference": tx.get("reference"),
                        "status": new_status,
                        "created_at": datetime.utcnow(),
                        "processed_by": admin_oid,
                    }
                },
            },
        )
        transactions_col.update_one({"_id": oid}, {"$set": {"meta.refunded": True}})

    return jsonify({"success": True, "status": new_status, "method": method})
