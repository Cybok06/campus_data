# admin_orders.py  — Admin Orders + DB-Backed Scheduler (Render-safe) + Bulk Deliver (Selected)
from flask import Blueprint, render_template, session, redirect, url_for, request, flash, jsonify
from bson import ObjectId, Regex
from db import db
from datetime import datetime, timedelta
import json
from urllib.parse import urlencode
import uuid
from typing import List, Tuple
from order_status import _compute_order_status_from_items, _normalize_status

admin_orders_bp = Blueprint("admin_orders", __name__)

orders_col        = db["orders"]
users_col         = db["users"]
balances_col      = db["balances"]         # for refunds
transactions_col  = db["transactions"]     # for refund ledger
schedules_col     = db["order_schedules"]  # NEW: persistent job queue

# Keep legacy; primary set includes refunded
ALLOWED_STATUSES   = {"pending", "processing", "delivered", "failed", "completed", "refunded"}
ALLOWED_SORTS      = {"newest", "oldest", "amount_desc", "amount_asc"}
DEFAULT_PER_PAGE   = 10
FINAL_STATUS       = "completed"
API_PROVIDER_LABELS = {
    "codecraft": "CodeCraft",
    "dataconnect": "DataConnect",
    "datakazina": "DataKazina",
    "portal02": "Portal-02",
    "skplug": "SKPlug",
    "bundleportal": "BundlePortal",
}
API_PROVIDER_KEYS = tuple(API_PROVIDER_LABELS.keys())
API_PROVIDERS = set(API_PROVIDER_KEYS)
ALLOWED_TRANSITIONS = {
    "pending": {"processing"},
    "processing": {"delivered", "failed", "refunded"},
    "delivered": {"completed"},
    "failed": set(),
    "refunded": set(),
    "completed": set(),
}

def _jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")

def _can_transition(old_status: str, new_status: str) -> bool:
    if old_status == new_status:
        return True
    return new_status in ALLOWED_TRANSITIONS.get(old_status, set())

def _log_status_blocked(order, attempted_status: str, reason: str, source: str, actor_admin_id=None):
    _jlog(
        "order_status_blocked",
        order_id=order.get("order_id"),
        mongo_id=str(order.get("_id")),
        attempted_status=attempted_status,
        current_status=(order.get("status") or ""),
        reason=reason,
        source=source,
        actor_admin_id=actor_admin_id,
    )

# --------- HELPERS ----------
def _parse_date(dstr):
    if not dstr:
        return None
    try:
        s = dstr.strip()
        if len(s) <= 10:
            return datetime.strptime(s, "%Y-%m-%d")
        return datetime.strptime(s, "%Y-%m-%d %H:%M")
    except Exception:
        return None

def _build_preserved_query(args, exclude=("page",)):
    kept = {k: v for k, v in args.items() if k not in exclude and v not in (None, "", "None")}
    return urlencode(kept)

def _append_and(query: dict, clause: dict) -> dict:
    query["$and"] = (query.get("$and") or []) + [clause]
    return query

def _build_query_from_params(args):
    """Central builder so list + bulk share identical filters."""
    status_filter = (args.get("status") or "").strip().lower()
    source_filter = (args.get("source_filter") or "").strip().lower()
    order_id_q    = (args.get("order_id") or "").strip()
    customer_q    = (args.get("customer") or "").strip()
    paid_from     = (args.get("paid_from") or "").strip().lower()
    min_total     = (args.get("min_total") or "").strip()
    max_total     = (args.get("max_total") or "").strip()
    date_from     = _parse_date((args.get("date_from") or "").strip())
    date_to_raw   = _parse_date((args.get("date_to") or "").strip())
    date_to       = datetime(date_to_raw.year, date_to_raw.month, date_to_raw.day) + timedelta(days=1) if date_to_raw else None

    item_service  = (args.get("item_service") or "").strip()
    item_offer    = (args.get("item_offer") or "").strip()
    item_phone    = (args.get("item_phone") or "").strip()

    query = {}

    if status_filter and status_filter in ALLOWED_STATUSES:
        query["status"] = status_filter
    if source_filter == "external_api":
        query["source"] = "external_api"
    elif source_filter == "regular":
        query["$or"] = [{"source": {"$exists": False}}, {"source": {"$ne": "external_api"}}]
    if paid_from:
        if paid_from == "paystack":
            query["paid_from"] = {"$in": ["paystack", "paystack_inline"]}
        else:
            query["paid_from"] = paid_from
    if order_id_q:
        query["order_id"] = Regex(order_id_q, "i")

    if date_from or date_to:
        dt = {}
        if date_from: dt["$gte"] = date_from
        if date_to:   dt["$lt"]  = date_to
        query["created_at"] = dt

    amt = {}
    try:
        if min_total != "": amt["$gte"] = float(min_total)
    except Exception:
        pass
    try:
        if max_total != "": amt["$lte"] = float(max_total)
    except Exception:
        pass
    if amt:
        query["total_amount"] = amt

    if customer_q:
        rx = Regex(customer_q, "i")
        user_ids = [u["_id"] for u in users_col.find(
            {"$or": [
                {"first_name": rx}, {"last_name": rx}, {"email": rx},
                {"phone": rx}, {"username": rx},
            ]},
            {"_id": 1},
        )]
        query["user_id"] = {"$in": user_ids or []}

    item_and = []
    if item_service: item_and.append({"items.serviceName": Regex(item_service, "i")})
    if item_offer:   item_and.append({"items.value": Regex(item_offer, "i")})
    if item_phone:   item_and.append({"items.phone": Regex(item_phone, "i")})
    if item_and:
        query["$and"] = (query.get("$and") or []) + item_and

    return query

def _require_admin():
    return session.get("role") == "admin"

def _money(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default

def _compute_api_fields(order: dict) -> None:
    items = order.get("items") or []
    providers = set()
    for item in items:
        prov = (item.get("provider") or "").strip().lower()
        if prov not in API_PROVIDERS:
            continue
        if (item.get("api_status") or "").strip().lower() == "skipped":
            continue
        line_status = (item.get("line_status") or "").strip().lower()
        if line_status in ("skipped_duplicate_processing", "skipped_duplicate_in_cart"):
            continue
        providers.add(prov)

    order["api_passed"] = bool(providers)
    order["api_providers"] = sorted(providers)
    labels = [API_PROVIDER_LABELS[p] for p in order["api_providers"] if p in API_PROVIDER_LABELS]
    order["api_providers_label"] = ", ".join(labels)

def _normalize_line_status(s: str | None) -> str:
    try:
        return _normalize_status(s)
    except Exception:
        return (s or "").strip().lower()

def _is_final_line_status(s: str | None) -> bool:
    return _normalize_line_status(s) == "delivered"

def _parse_line_id(line_id: str):
    if not line_id or ":" not in line_id:
        return None
    left, right = line_id.split(":", 1)
    try:
        oid = ObjectId(left.strip())
        idx = int(right.strip())
        if idx < 0:
            return None
        return oid, idx
    except Exception:
        return None

def _apply_line_status_change(line_ids: List[str], new_status: str, api_status: str | None = None, reason: str = "manual", actor_admin_id=None) -> Tuple[int, List[str]]:
    updated_lines = 0
    errors = []
    now = datetime.utcnow()
    if new_status not in {"pending", "processing", "delivered", "failed", "refunded"}:
        return 0, [f"invalid line status: {new_status}"]

    grouped = {}
    for lid in line_ids:
        parsed = _parse_line_id(lid)
        if not parsed:
            errors.append(f"{lid}: invalid line id")
            continue
        oid, idx = parsed
        grouped.setdefault(oid, set()).add(idx)

    for oid, idxs in grouped.items():
        try:
            order = orders_col.find_one({"_id": oid})
            if not order:
                errors.append(f"{oid}: not found")
                continue

            items = order.get("items") or []
            set_doc = {"updated_at": now}
            any_changed = False

            for idx in sorted(idxs):
                if idx < 0 or idx >= len(items):
                    errors.append(f"{oid}:{idx}: item not found")
                    continue
                item = items[idx]
                current_line = _normalize_line_status(item.get("line_status"))
                if _is_final_line_status(current_line) and _normalize_line_status(new_status) != "delivered":
                    errors.append(f"{oid}:{idx}: line is delivered and cannot be changed")
                    continue

                item["line_status"] = new_status
                set_doc[f"items.{idx}.line_status"] = new_status
                if api_status:
                    item["api_status"] = api_status
                    set_doc[f"items.{idx}.api_status"] = api_status
                set_doc[f"items.{idx}.provider_status_checked_at"] = now
                any_changed = True
                updated_lines += 1

            if not any_changed:
                continue

            current_status = (order.get("status") or "").lower()
            if current_status != "completed":
                new_order_status = _compute_order_status_from_items(items, current_status=current_status)
                if new_order_status and new_order_status != current_status:
                    set_doc["status"] = new_order_status
                    if new_order_status == "delivered" and not order.get("delivered_at"):
                        set_doc["delivered_at"] = now

            orders_col.update_one({"_id": oid}, {"$set": set_doc})
        except Exception as e:
            errors.append(f"{oid}: {e}")

    return updated_lines, errors

def _extract_duplicate_delivered_updates(item: dict) -> dict | None:
    api_resp = item.get("api_response")
    if not isinstance(api_resp, dict):
        return None
    if api_resp.get("http_status") != 409:
        return None
    dup = api_resp.get("duplicate_order") or {}
    dup_status = (dup.get("status") or "").strip().upper()
    if dup_status != "DELIVERED":
        return None

    updates = {}
    if _normalize_line_status(item.get("line_status")) != "delivered":
        updates["line_status"] = "delivered"
    if (item.get("api_status") or "").strip().lower() not in ("success", "duplicate_delivered"):
        updates["api_status"] = "success"
    if not item.get("provider_reference"):
        ref = dup.get("transaction_code") or dup.get("provider_reference") or dup.get("reference") or dup.get("id")
        if ref:
            updates["provider_reference"] = ref
    return updates or None

# ---------- CORE: apply status change (used by manual, bulk, scheduled) ----------
def _apply_status_change(order_ids: List[ObjectId], new_status: str, reason: str = "manual", actor_admin_id=None) -> Tuple[int, List[str]]:
    """
    Idempotent per-order updates, including wallet credit for refunds.
    Returns (updated_count, errors)
    """
    updated = 0
    errors  = []

    now = datetime.utcnow()
    for oid in order_ids:
        try:
            order = orders_col.find_one({"_id": oid})
            if not order:
                errors.append(f"{oid}: not found")
                continue

            old_status = (order.get("status") or "").lower()
            if old_status == FINAL_STATUS and new_status != FINAL_STATUS:
                _log_status_blocked(order, new_status, "final_status", reason, actor_admin_id)
                errors.append(f"{oid}: order is completed and cannot be changed")
                continue
            if not _can_transition(old_status, new_status):
                _log_status_blocked(order, new_status, "invalid_transition", reason, actor_admin_id)
                errors.append(f"{oid}: invalid transition {old_status} -> {new_status}")
                continue
            update_doc = {"status": new_status, "updated_at": now}
            # Delivered → set delivered_at if missing
            if new_status == "delivered" and not order.get("delivered_at"):
                update_doc["delivered_at"] = now

            # Refunded → single wallet credit based on charged_amount
            if new_status == "refunded":
                charged_amount = _money(order.get("charged_amount"), 0.0)
                user_id = order.get("user_id")
                already_refunded = bool(order.get("refunded_at")) or (old_status == "refunded")

                if charged_amount > 0 and user_id and not already_refunded:
                    try:
                        balances_col.update_one(
                            {"user_id": user_id},
                            {"$inc": {"amount": charged_amount}, "$set": {"updated_at": now}},
                            upsert=True
                        )
                        transactions_col.insert_one({
                            "user_id": user_id,
                            "amount": charged_amount,
                            "reference": order.get("order_id"),
                            "status": "success",
                            "type": "refund",
                            "gateway": "Wallet",
                            "currency": "GHS",
                            "created_at": now,
                            "verified_at": now,
                            "meta": {
                                "note": f"{reason.capitalize()} refund",
                                "order_db_id": oid,
                                "actor_admin_id": actor_admin_id,
                            }
                        })
                    except Exception as e:
                        errors.append(f"{oid}: refund ledger err: {e}")
                update_doc["refunded_at"] = now

            update_filter = {"_id": oid}
            if new_status != FINAL_STATUS:
                update_filter["status"] = {"$ne": FINAL_STATUS}
            res = orders_col.update_one(update_filter, {"$set": update_doc})
            if res.modified_count:
                # Flip line_status in items from processing -> delivered when marking delivered
                if new_status == "delivered":
                    try:
                        orders_col.update_one(
                            {"_id": oid, "status": {"$ne": FINAL_STATUS}},
                            {"$set": {"items.$[it].line_status": "delivered"}},
                            array_filters=[{"it.line_status": "processing"}]
                        )
                    except Exception:
                        pass
                updated += 1
            else:
                if new_status != FINAL_STATUS:
                    _log_status_blocked(order, new_status, "db_guard", reason, actor_admin_id)

        except Exception as e:
            errors.append(f"{oid}: {e}")

    return updated, errors

# ---------- DB-backed scheduler utilities ----------
def _enqueue_status_job(order_id_strs: List[str], new_status: str, run_time: datetime, admin_id: str | None, note: str | None, line_ids: List[str] | None = None):
    """
    Persist a job document that can be executed later (Render-safe).
    """
    now = datetime.utcnow()
    doc = {
        "job_key": str(uuid.uuid4()),
        "order_ids": order_id_strs,     # strings
        "line_ids": line_ids or [],     # strings "orderId:itemIndex"
        "status": new_status,
        "note": note or "",
        "admin_id": admin_id,
        "state": "scheduled",           # scheduled | running | done | error | cancelled
        "attempts": 0,
        "max_attempts": 3,
        "created_at": now,
        "run_at": run_time,             # UTC datetime
        "started_at": None,
        "finished_at": None,
        "result": None,                 # {updated, errors:[], ...}
        "lock_token": None,             # for cooperative locking
        "locked_at": None
    }
    schedules_col.insert_one(doc)
    return doc

def _process_due_jobs(max_batch: int = 25):
    """
    Cooperatively process due jobs. Safe to call at the top of admin routes
    and/or from a Render Cron ping.
    """
    now = datetime.utcnow()
    # pick up to max_batch jobs that are due and not locked/running/cancelled
    cursor = schedules_col.find({
        "state": {"$in": ["scheduled", "error"]},
        "run_at": {"$lte": now},
        "$or": [{"lock_token": None}, {"locked_at": {"$lt": now - timedelta(minutes=5)}}]
    }).sort([("run_at", 1)]).limit(max_batch)

    for job in cursor:
        lock_token = str(uuid.uuid4())
        # try to acquire lock
        claimed = schedules_col.update_one(
            {"_id": job["_id"], "lock_token": job.get("lock_token")},
            {"$set": {"lock_token": lock_token, "locked_at": now, "state": "running", "started_at": now}}
        )
        if not claimed.modified_count:
            continue

        # Execute
        try:
            updated = 0
            errors = []

            line_ids = [s for s in (job.get("line_ids") or []) if s]
            if line_ids:
                line_updated, line_errors = _apply_line_status_change(line_ids, job.get("status"), reason="scheduled", actor_admin_id=job.get("admin_id"))
                updated += line_updated
                errors += line_errors

            ids = []
            for s in (job.get("order_ids") or []):
                try:
                    ids.append(ObjectId(s))
                except Exception:
                    pass
            if ids:
                order_updated, order_errors = _apply_status_change(ids, job.get("status"), reason="scheduled", actor_admin_id=job.get("admin_id"))
                updated += order_updated
                errors += order_errors
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "done" if not errors else "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": updated, "error_count": len(errors), "errors": errors}
                }}
            )
        except Exception as e:
            schedules_col.update_one(
                {"_id": job["_id"], "lock_token": lock_token},
                {"$set": {
                    "state": "error",
                    "finished_at": datetime.utcnow(),
                    "attempts": (job.get("attempts", 0) + 1),
                    "result": {"updated": 0, "error_count": 1, "errors": [str(e)]}
                }}
            )

# =========================================================
#                       ROUTES
# =========================================================
@admin_orders_bp.route("/admin/orders")
def admin_view_orders():
    if not _require_admin():
        return redirect(url_for("login.login"))

    # Opportunistically run any due jobs (cheap)
    try:
        _process_due_jobs(max_batch=10)
    except Exception:
        pass

    sort = (request.args.get("sort") or "newest").strip().lower()
    if sort not in ALLOWED_SORTS:
        sort = "newest"

    try:
        per_page = int(request.args.get("per_page", DEFAULT_PER_PAGE))
        per_page = max(1, min(per_page, 100))
    except Exception:
        per_page = DEFAULT_PER_PAGE

    try:
        page = int(request.args.get("page", 1))
        page = max(1, page)
    except Exception:
        page = 1

    skip = (page - 1) * per_page
    query = _build_query_from_params(request.args)
    api_filter = (request.args.get("api_filter") or "").strip().lower()
    if api_filter in {"passed", "not_passed"}:
        api_elem = {
            "items": {
                "$elemMatch": {
                    "provider": {"$in": list(API_PROVIDER_KEYS)},
                    "api_status": {"$ne": "skipped"},
                    "line_status": {"$nin": ["skipped_duplicate_processing", "skipped_duplicate_in_cart"]},
                }
            }
        }
        if api_filter == "passed":
            _append_and(query, api_elem)
        else:
            _append_and(query, {"$nor": [api_elem]})

    sort_spec = [("created_at", -1)]
    if sort == "oldest":
        sort_spec = [("created_at", 1)]
    elif sort == "amount_desc":
        sort_spec = [("total_amount", -1), ("created_at", -1)]
    elif sort == "amount_asc":
        sort_spec = [("total_amount", 1), ("created_at", -1)]

    try:
        total_orders = orders_col.count_documents(query)
        total_pages  = max(1, (total_orders + per_page - 1) // per_page)
        orders       = list(orders_col.find(query).sort(sort_spec).skip(skip).limit(per_page))
        order_lines  = []

        for o in orders:
            uid = o.get("user_id")
            if isinstance(uid, str):
                try:
                    uid = ObjectId(uid)
                except Exception:
                    uid = None
            o["user"] = users_col.find_one({"_id": uid}) if uid else {}
            items = o.get("items") or []
            now = datetime.utcnow()
            changed_indexes = []
            for idx, item in enumerate(items):
                updates = _extract_duplicate_delivered_updates(item)
                if updates:
                    item.update(updates)
                    changed_indexes.append((idx, updates))

            if changed_indexes:
                for idx, updates in changed_indexes:
                    set_doc = {f"items.{idx}.{k}": v for k, v in updates.items()}
                    set_doc["updated_at"] = now
                    orders_col.update_one({"_id": o["_id"]}, {"$set": set_doc})

                current_status = (o.get("status") or "").lower()
                if current_status != "completed":
                    new_status = _compute_order_status_from_items(items, current_status=current_status)
                    if new_status and new_status != current_status:
                        set_doc = {"status": new_status, "updated_at": now}
                        if new_status == "delivered" and not o.get("delivered_at"):
                            set_doc["delivered_at"] = now
                        orders_col.update_one({"_id": o["_id"]}, {"$set": set_doc})
                        o["status"] = new_status

            _compute_api_fields(o)
            for idx, item in enumerate(items):
                order_lines.append({
                    "order_mongo_id": str(o.get("_id")),
                    "order_id": o.get("order_id"),
                    "user": o.get("user") or {},
                    "source": o.get("source"),
                    "api_client_name": o.get("api_client_name"),
                    "external_request": o.get("external_request") or {},
                    "paid_from": o.get("paid_from"),
                    "created_at": o.get("created_at"),
                    "status": o.get("status"),
                    "order_total_amount": o.get("total_amount"),
                    "profit_amount_total": o.get("profit_amount"),
                    "item_index": idx,
                    "item": item,
                    "line_id": f"{o.get('_id')}:{idx}",
                })
    except Exception:
        flash("Error loading orders.", "danger")
        orders, total_pages, total_orders, order_lines = [], 1, 0, []

    view_mode = (request.args.get("view") or "lines").strip().lower()
    if view_mode not in {"lines", "orders"}:
        view_mode = "lines"
    view_line_url = url_for("admin_orders.admin_view_orders")
    view_order_url = url_for("admin_orders.admin_view_orders")
    try:
        view_line_query = _build_preserved_query({**request.args.to_dict(flat=True), "view": "lines"})
        view_order_query = _build_preserved_query({**request.args.to_dict(flat=True), "view": "orders"})
        if view_line_query:
            view_line_url = f"{view_line_url}?{view_line_query}"
        if view_order_query:
            view_order_url = f"{view_order_url}?{view_order_query}"
    except Exception:
        pass

    return render_template(
        "admin_orders.html",
        orders=orders,
        order_lines=order_lines,
        order_lines_count=len(order_lines),
        page=page, total_pages=total_pages, total_orders=total_orders,
        status_filter=(request.args.get("status") or "").strip().lower(),
        source_filter=(request.args.get("source_filter") or "").strip().lower(),
        order_id_q=(request.args.get("order_id") or "").strip(),
        customer_q=(request.args.get("customer") or "").strip(),
        paid_from=(request.args.get("paid_from") or "").strip().lower(),
        min_total=(request.args.get("min_total") or "").strip(),
        max_total=(request.args.get("max_total") or "").strip(),
        date_from=(request.args.get("date_from") or "").strip(),
        date_to=(request.args.get("date_to") or "").strip(),
        sort=sort, per_page=per_page,
        item_service=(request.args.get("item_service") or "").strip(),
        item_offer=(request.args.get("item_offer") or "").strip(),
        item_phone=(request.args.get("item_phone") or "").strip(),
        api_filter=(request.args.get("api_filter") or "").strip().lower(),
        filters_query=_build_preserved_query(request.args),
        view_mode=view_mode,
        view_line_url=view_line_url,
        view_order_url=view_order_url,
    )

@admin_orders_bp.route("/admin/orders/<order_id>/items/<int:item_index>/status", methods=["POST"])
def update_order_line_status(order_id, item_index):
    if not _require_admin():
        return redirect(url_for("login.login"))

    payload = {}
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    new_status = (request.form.get("line_status") or payload.get("line_status") or "").strip().lower()
    api_status = (request.form.get("api_status") or payload.get("api_status") or "").strip().lower()

    if new_status not in {"delivered", "processing", "failed", "pending", "refunded"}:
        flash("Invalid line status.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        oid = ObjectId(order_id)
    except Exception:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    order = orders_col.find_one({"_id": oid})
    if not order:
        flash("Order not found.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    items = order.get("items") or []
    if item_index < 0 or item_index >= len(items):
        flash("Line item not found.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    item = items[item_index]
    current_line = _normalize_line_status(item.get("line_status"))
    if _is_final_line_status(current_line) and _normalize_line_status(new_status) != "delivered":
        flash("This line is delivered and cannot be downgraded.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    now = datetime.utcnow()
    item["line_status"] = new_status
    if api_status:
        item["api_status"] = api_status

    update_doc = {
        f"items.{item_index}.line_status": new_status,
        f"items.{item_index}.provider_status_checked_at": now,
        "updated_at": now,
    }
    if api_status:
        update_doc[f"items.{item_index}.api_status"] = api_status

    current_status = (order.get("status") or "").lower()
    if current_status != "completed":
        new_order_status = _compute_order_status_from_items(items, current_status=current_status)
        if new_order_status and new_order_status != current_status:
            update_doc["status"] = new_order_status
            if new_order_status == "delivered" and not order.get("delivered_at"):
                update_doc["delivered_at"] = now

    orders_col.update_one({"_id": oid}, {"$set": update_doc})

    flash("✅ Line status updated.", "success")
    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/<order_id>/update", methods=["POST"])
def update_order_status(order_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    new_status = (request.form.get("status") or "").strip().lower()
    if new_status not in ALLOWED_STATUSES:
        flash("Invalid status.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        oid = ObjectId(order_id)
    except Exception:
        flash("Invalid order id.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    updated, errors = _apply_status_change([oid], new_status, reason="manual", actor_admin_id=session.get("user_id"))
    if updated:
        msg = {
            "processing": "✅ Order marked as Processing.",
            "delivered": "✅ Order marked as Delivered.",
            "failed": "✅ Order marked as Failed.",
            "refunded": "✅ Order marked as Refunded (wallet credited if not already).",
            "pending": "✅ Order marked as Pending.",
            "completed": "✅ Order marked as Completed.",
        }.get(new_status, "✅ Order updated.")
        flash(msg, "success")
    else:
        if errors:
            flash(" | ".join(errors[:3]), "warning")
        else:
            flash("ℹ️ No change to order.", "warning")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/bulk-deliver", methods=["POST"])
def bulk_deliver_orders():
    """
    Existing behavior: mark all orders that match CURRENT FILTERS and are processing -> delivered.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))
    args = request.args.to_dict(flat=True)
    args["status"] = "processing"
    query = _build_query_from_params(args)

    try:
        # Find ids first (so we can also update line_status)
        ids = [o["_id"] for o in orders_col.find(query, {"_id": 1})]
        updated, errors = _apply_status_change(ids, "delivered", reason="bulk_deliver", actor_admin_id=session.get("user_id"))
        if updated:
            flash(f"✅ Marked {updated} processing order(s) as Delivered.", "success")
        else:
            flash("ℹ️ No eligible processing orders to deliver.", "warning")
        if errors:
            flash(" | ".join(errors[:3]), "warning")
    except Exception:
        flash("❌ Bulk update failed.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# NEW: mark SELECTED ids as delivered (from checkboxes / floating bar)
@admin_orders_bp.route("/admin/orders/bulk-deliver-selected", methods=["POST"])
def bulk_deliver_selected():
    if not _require_admin():
        return redirect(url_for("login.login"))

    line_ids = []
    if "line_ids" in request.form:
        line_ids += [request.form.get("line_ids") or ""]
    line_ids += request.form.getlist("line_ids[]")
    line_ids = ",".join([s for s in line_ids if s]).split(",")
    line_ids = [s.strip() for s in line_ids if s.strip()]

    if line_ids:
        try:
            updated, errors = _apply_line_status_change(line_ids, "delivered", reason="bulk_deliver_selected", actor_admin_id=session.get("user_id"))
            if updated:
                flash(f"✅ Marked {updated} selected line(s) as Delivered.", "success")
            else:
                flash("ℹ️ No eligible lines to deliver.", "warning")
            if errors:
                flash(" | ".join(errors[:3]), "warning")
        except Exception:
            flash("❌ Failed to bulk deliver selected lines.", "danger")

        back_to = url_for("admin_orders.admin_view_orders")
        qs = _build_preserved_query(request.args)
        return redirect(f"{back_to}?{qs}" if qs else back_to)

    # Accept: order_ids (comma string) OR order_ids[] OR order_id[]
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    ids = []
    for s in raw_list:
        try:
            ids.append(ObjectId((s or "").strip()))
        except Exception:
            pass

    if not ids:
        flash("Please select at least one order.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    try:
        updated, errors = _apply_status_change(ids, "delivered", reason="bulk_deliver_selected", actor_admin_id=session.get("user_id"))
        if updated:
            flash(f"✅ Marked {updated} selected order(s) as Delivered.", "success")
        else:
            flash("ℹ️ No eligible orders to deliver.", "warning")
        if errors:
            flash(" | ".join(errors[:3]), "warning")
    except Exception:
        flash("❌ Failed to bulk deliver selected.", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# =========================================================
#            DB-BACKED SCHEDULING ENDPOINTS (Admin)
# =========================================================
@admin_orders_bp.route("/admin/orders/schedule-status", methods=["POST"])
def schedule_status():
    """
    Form fields:
      - order_ids: comma-separated string OR multiple order_ids[] fields OR order_id[]
      - status: one of ALLOWED_STATUSES
      - delay_minutes: int (optional)
      - run_at: "YYYY-MM-DD HH:MM" (UTC, optional)
      - note: optional
    One of delay_minutes or run_at is required.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))

    status = (request.form.get("status") or "").strip().lower()
    if status not in ALLOWED_STATUSES:
        flash("Invalid status for scheduling.", "danger")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # collect order ids
    raw_list = []
    if "order_ids" in request.form:
        raw_list += [request.form.get("order_ids") or ""]
    raw_list += request.form.getlist("order_ids[]")
    raw_list += request.form.getlist("order_id[]")
    raw_list = ",".join([s for s in raw_list if s]).split(",")

    order_id_strs = []
    bad_ids = []
    for s in raw_list:
        s2 = (s or "").strip()
        if not s2:
            continue
        try:
            ObjectId(s2)
            order_id_strs.append(s2)
        except Exception:
            bad_ids.append(s2)

    # collect line ids
    line_ids = []
    if "line_ids" in request.form:
        line_ids += [request.form.get("line_ids") or ""]
    line_ids += request.form.getlist("line_ids[]")
    line_ids = ",".join([s for s in line_ids if s]).split(",")
    line_ids = [s.strip() for s in line_ids if s.strip()]

    valid_line_ids = []
    for lid in line_ids:
        parsed = _parse_line_id(lid)
        if parsed:
            valid_line_ids.append(lid)

    if not order_id_strs and not valid_line_ids:
        flash("Please select at least one valid order or line.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))
    if valid_line_ids and status == "completed" and not order_id_strs:
        flash("Completed is an order-only status. Choose another status for lines.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    # compute run time
    delay_str  = (request.form.get("delay_minutes") or "").strip()
    run_at_str = (request.form.get("run_at") or "").strip()
    run_time   = None

    if delay_str:
        try:
            mins = int(delay_str)
            run_time = datetime.utcnow() + timedelta(minutes=max(0, mins))
        except Exception:
            flash("Invalid delay minutes.", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
    elif run_at_str:
        dt = _parse_date(run_at_str)
        if not dt:
            flash("Invalid run_at datetime. Use 'YYYY-MM-DD HH:MM' (UTC).", "danger")
            return redirect(url_for("admin_orders.admin_view_orders"))
        run_time = dt
        if run_time < datetime.utcnow():
            flash("Run time must be in the future.", "warning")
            return redirect(url_for("admin_orders.admin_view_orders"))
    else:
        flash("Provide either delay_minutes or run_at.", "warning")
        return redirect(url_for("admin_orders.admin_view_orders"))

    note = (request.form.get("note") or "").strip()
    admin_id = (session.get("user_id") or None)
    job = _enqueue_status_job(order_id_strs, status, run_time, str(admin_id) if admin_id else None, note, line_ids=valid_line_ids)

    target_count = len(order_id_strs) or len(valid_line_ids)
    target_label = "order(s)" if order_id_strs else "line(s)"
    flash(f"⏱️ Scheduled {target_count} {target_label} → {status} at {run_time.strftime('%Y-%m-%d %H:%M')} UTC.", "success")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

@admin_orders_bp.route("/admin/orders/schedules", methods=["GET"])
def list_schedules():
    """Returns JSON of recent schedules (for the offcanvas in the UI)."""
    if not _require_admin():
        return redirect(url_for("login.login"))
    # Also opportunistically process due jobs when viewing the list
    try:
        _process_due_jobs(max_batch=25)
    except Exception:
        pass

    jobs = []
    for j in schedules_col.find({}).sort([("created_at", -1)]).limit(100):
        jobs.append({
            "id": str(j.get("_id")),
            "job_key": j.get("job_key"),
            "next_run_time": j.get("run_at").strftime("%Y-%m-%d %H:%M:%S UTC") if j.get("run_at") else None,
            "state": j.get("state"),
            "status": j.get("status"),
            "args": [j.get("order_ids"), j.get("status")],
            "result": j.get("result"),
            "attempts": j.get("attempts", 0),
        })
    return jsonify({"jobs": jobs})

@admin_orders_bp.route("/admin/orders/schedules/<job_id>/cancel", methods=["POST"])
def cancel_schedule(job_id):
    if not _require_admin():
        return redirect(url_for("login.login"))
    try:
        res = schedules_col.update_one({"_id": ObjectId(job_id)}, {"$set": {"state": "cancelled"}})
        if res.modified_count:
            flash("🗑️ Schedule cancelled.", "success")
        else:
            flash("Schedule not found.", "warning")
    except Exception as e:
        flash(f"Failed to cancel schedule: {e}", "danger")

    back_to = url_for("admin_orders.admin_view_orders")
    qs = _build_preserved_query(request.args)
    return redirect(f"{back_to}?{qs}" if qs else back_to)

# Optional: endpoint you can ping from Render Cron every minute
@admin_orders_bp.route("/admin/orders/schedules/run-due", methods=["POST", "GET"])
def run_due_schedules():
    if not _require_admin():
        # If you want cron w/o session, you can protect via secret token instead
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    try:
        _process_due_jobs(max_batch=50)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
