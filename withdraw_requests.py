from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Tuple

from bson import ObjectId

from db import db

store_withdraw_requests_col = db["store_withdraw_requests"]

ALLOWED_WITHDRAW_STATUSES = {"pending", "processing", "paid", "rejected", "canceled"}


def _normalize_status(status: str) -> str:
    s = (status or "").strip().lower()
    if s == "cancelled":
        s = "canceled"
    return s


def update_withdraw_request_status(
    req_id: str,
    new_status: str,
    actor_id: Any = None,
    note: str | None = None,
) -> Tuple[bool, Dict[str, Any], int]:
    if not req_id:
        return False, {"message": "Invalid request id"}, 400

    try:
        oid = ObjectId(req_id)
    except Exception:
        return False, {"message": "Invalid request id"}, 400

    status_norm = _normalize_status(new_status)
    if status_norm not in ALLOWED_WITHDRAW_STATUSES:
        return False, {"message": "Invalid status"}, 400

    req_doc = store_withdraw_requests_col.find_one({"_id": oid})
    if not req_doc:
        return False, {"message": "Request not found"}, 404

    old_status = (req_doc.get("status") or "pending").strip().lower()
    note_in = (note or "").strip()
    if old_status == status_norm and note_in == (req_doc.get("note") or ""):
        return True, {"message": "No changes", "no_change": True}, 200

    now = datetime.utcnow()
    update_fields: Dict[str, Any] = {
        "status": status_norm,
        "updated_at": now,
        "updated_by": actor_id or "admin",
    }
    if note_in:
        update_fields["note"] = note_in
    if status_norm == "paid":
        update_fields["paid_at"] = now
        update_fields["paid_by"] = actor_id or "admin"

    history_event = {
        "when": now,
        "actor_id": actor_id or "admin",
        "from_status": old_status,
        "to_status": status_norm,
    }
    if note_in:
        history_event["note"] = note_in

    store_withdraw_requests_col.update_one(
        {"_id": oid},
        {"$set": update_fields, "$push": {"history": history_event}},
    )
    return True, {"message": f"Marked {status_norm}"}, 200
