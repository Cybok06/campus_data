"""Order persistence and authenticated Bundle Portal delivery callbacks."""
from datetime import datetime
import hashlib
import hmac
import os

from flask import Blueprint, jsonify, request, session, flash, redirect, url_for
from bson import ObjectId

from bundle_portal import PROVIDERS, call, submit, canonical_network

bundle_portal_bp = Blueprint("bundle_portal", __name__)
FINAL_LINES = {"delivered", "completed", "refunded"}


def refresh_order(collection, order_id):
    # Compare the snapshot to avoid overwriting another line's concurrent result.
    for _ in range(5):
        order = collection.find_one({"order_id": order_id})
        if not order:
            return
        items = order.get("items") or []
        statuses = [i.get("line_status") for i in items if not str(i.get("line_status", "")).startswith("skipped")]
        if not statuses:
            return
        if all(s in {"delivered", "completed"} for s in statuses):
            status = "delivered"
        elif all(s == "refunded" for s in statuses):
            status = "refunded"
        elif all(s in {"failed", "refunded"} for s in statuses):
            status = "failed"
        else:
            status = "processing"
        if order.get("status") == "completed":
            return
        fields = {"status": status, "updated_at": datetime.utcnow()}
        if status == "delivered" and not order.get("delivered_at"):
            fields["delivered_at"] = datetime.utcnow()
        result = collection.update_one(
            {"_id": order["_id"], "items": items, "status": order.get("status")},
            {"$set": fields},
        )
        if result.matched_count:
            return


def invalidate_orders():
    # Campus renders orders directly without the Nagonu orders cache.
    pass


def process_job(collection, order_id, job):
    reference = job["provider_request_order_id"]
    provider = job["provider"]
    selector = {"order_id": order_id, "items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": provider,
        "api_status": {"$in": ["queued", "review_required"]}, "line_status": {"$in": ["pending", "processing"]},
    }}}
    claimed = collection.update_one(selector, {"$set": {
        "items.$.api_status": "submitting", "updated_at": datetime.utcnow(),
    }})
    if not claimed.modified_count:
        return
    payload = submit(provider, job.get("phone"), job.get("bundle_portal_gb_size"), reference,
                     retry_purchase=bool(job.get("retry_purchase")))
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    accepted = payload.get("success") is True
    # A timeout/5xx may follow a charged purchase. Keep it for webhook/manual review.
    uncertain = (payload.get("code") in {"unknown_outcome", "recipient_blocked", "not_allowlisted"}
                 or payload.get("http_status", 0) in {409, 429}
                 or payload.get("http_status", 0) >= 500)
    raw_status = data.get("status")
    line_status = "delivered" if accepted and raw_status == "completed" else "processing"
    if (not accepted and not uncertain) or (accepted and raw_status in {"failed", "cancelled", "refunded"}):
        line_status = "failed"
    fields = {
        "items.$.api_status": "success" if accepted else ("review_required" if uncertain else "failed"),
        "items.$.line_status": line_status,
        "items.$.api_response": payload,
        "items.$.provider_reference": data.get("reference"),
        "items.$.provider_order_id": reference,
        "items.$.provider_status": raw_status,
        "items.$.provider_submission_attempted": bool(payload.get("order_attempted")),
        "items.$.refund_required": line_status == "failed",
        "updated_at": datetime.utcnow(),
    }
    # A fast webhook must never be overwritten by the initial HTTP response.
    collection.update_one({"order_id": order_id, "items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": provider,
        "api_status": "submitting", "line_status": {"$nin": list(FINAL_LINES | {"failed"})},
    }}}, {"$set": fields})
    refresh_order(collection, order_id)
    invalidate_orders()


@bundle_portal_bp.route("/webhooks/bundleportal", methods=["POST"])
def webhook():
    secret = os.getenv("BUNDLE_PORTAL_WEBHOOK_SECRET", "").strip()
    if not secret:
        return jsonify(success=False, error="Webhook is not configured"), 503
    raw = request.get_data()
    signature = request.headers.get("X-BundlePortal-Signature", "")
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected.encode(), signature.encode()):
        return jsonify(success=False, error="Invalid signature"), 401
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(success=False, error="Invalid payload"), 400
    events = {"order.completed": "completed", "order.failed": "failed",
              "order.cancelled": "cancelled", "order.refunded": "refunded"}
    event = payload.get("event")
    if not isinstance(event, str) or event not in events or payload.get("status") != events[event] or not isinstance(payload.get("order_id"), str):
        return jsonify(success=False, error="Invalid order event"), 400
    from db import db
    collection = db["orders"]
    reference = payload["order_id"]
    order = collection.find_one({"items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": {"$in": list(PROVIDERS)},
    }}})
    if not order:
        return jsonify(success=False, error="Order not found"), 404
    item = next(i for i in order["items"] if i.get("provider_request_order_id") == reference and i.get("provider") in PROVIDERS)
    if canonical_network(payload.get("network")) != PROVIDERS[item["provider"]] or payload.get("recipient") != item.get("phone"):
        return jsonify(success=False, error="Order details do not match"), 400
    # Persist authenticated callbacks for support/reconciliation (v2 does not retry).
    db["bundleportal_events"].update_one(
        {"_id": hashlib.sha256(raw).hexdigest()},
        {"$setOnInsert": {"payload": payload, "received_at": datetime.utcnow()}}, upsert=True,
    )
    target = "delivered" if event == "order.completed" else "failed"
    blocked = list(FINAL_LINES | {"failed"})
    # A provider refund can follow delivery; it still requires local wallet review.
    if event == "order.refunded":
        blocked = ["refunded", "completed"]
    collection.update_one({"_id": order["_id"], "items": {"$elemMatch": {
        "provider_request_order_id": reference, "provider": item["provider"],
        "line_status": {"$nin": blocked},
    }}}, {"$set": {
        "items.$.line_status": target,
        "items.$.api_status": "success" if target == "delivered" else "failed",
        "items.$.provider_status": payload["status"],
        "items.$.provider_reference": payload.get("reference"),
        "items.$.provider_status_payload": payload,
        "items.$.provider_status_checked_at": datetime.utcnow(),
        "items.$.refund_required": target == "failed",
        "updated_at": datetime.utcnow(),
    }})
    refresh_order(collection, order["order_id"])
    invalidate_orders()
    return jsonify(success=True)


@bundle_portal_bp.route("/admin/services/bundleportal/catalog", methods=["GET"])
def catalog():
    if session.get("role") != "admin":
        return jsonify(success=False, error="Unauthorized"), 401
    network = request.args.get("network", "mtn")
    if network not in PROVIDERS.values():
        return jsonify(success=False, error="Invalid Bundle Portal network"), 400
    payload = call("get_bundles", network=network)
    return jsonify(payload), 200 if payload.get("success") else 502


@bundle_portal_bp.route("/admin/orders/<order_id>/items/<int:item_index>/bundleportal-retry", methods=["POST"])
def retry_line(order_id, item_index):
    if session.get("role") != "admin":
        return jsonify(success=False, error="Unauthorized"), 401
    from db import db
    collection = db["orders"]
    try:
        order = collection.find_one({"_id": ObjectId(order_id)})
    except Exception:
        order = None
    items = (order or {}).get("items") or []
    if not order or item_index >= len(items):
        return jsonify(success=False, error="Line not found"), 404
    item = items[item_index]
    if item.get("provider") not in PROVIDERS or item.get("api_status") not in {"queued", "review_required"} or item.get("line_status") not in {"pending", "processing"}:
        return jsonify(success=False, error="Line is not eligible for retry"), 409
    process_job(collection, order["order_id"], {
        "provider": item["provider"], "phone": item.get("phone"),
        "provider_request_order_id": item["provider_request_order_id"],
        "bundle_portal_gb_size": item.get("provider_gb_size"),
        "retry_purchase": item.get("provider_submission_attempted", False),
    })
    flash("Bundle Portal retry processed. Check the line's current status.", "info")
    return redirect(url_for("admin_orders.admin_view_orders", view="lines"))
