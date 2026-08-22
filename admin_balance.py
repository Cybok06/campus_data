from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from bson.objectid import ObjectId
from db import db
from datetime import datetime
import uuid
import requests
from urllib.parse import quote  # for URL-encoding SMS

admin_balance_bp = Blueprint("admin_balance", __name__)

balances_col = db["balances"]
users_col = db["users"]
balance_logs_col = db["balance_logs"]  # audit trail
transactions_col = db["transactions"]

# ---- SMS config (Arkesel-style) ----
ARKESEL_API_KEY = "b3dheEVqUWNyeVBuUGxDVWFxZ0E"  # replace with env var in production
SENDER_ID = "CAMPUS DATA"  # requested sender name


# ------------ Helpers ------------
def _now():
    return datetime.utcnow()


def _is_ajax(req) -> bool:
    return req.headers.get("X-Requested-With", "").lower() == "xmlhttprequest"


def _normalize_phone(raw: str) -> str | None:
    """Return MSISDN like 233XXXXXXXXX for Ghana numbers, else None."""
    if not raw:
        return None
    p = raw.strip().replace(" ", "").replace("-", "").replace("+", "")
    if p.startswith("0") and len(p) == 10:
        p = "233" + p[1:]
    if p.startswith("233") and len(p) == 12:
        return p
    return None


def _get_actor():
    """Return (actor_id, actor_name) for the admin doing the change."""
    actor_id = session.get("user_id")
    actor_name = session.get("username") or session.get("email") or "admin"
    if actor_id:
        try:
            u = users_col.find_one({"_id": ObjectId(actor_id)}, {"username": 1, "first_name": 1, "last_name": 1})
            if u:
                actor_name = (
                    u.get("username")
                    or f"{u.get('first_name', '')} {u.get('last_name', '')}".strip()
                    or actor_name
                )
        except Exception:
            pass
    return actor_id, actor_name


def _make_admin_wallet_reference(balance_id: ObjectId) -> str:
    ts = _now().strftime("%Y%m%d%H%M%S")
    return f"ADM-WAL-{balance_id}-{ts}-{uuid.uuid4().hex[:6].upper()}"


def _send_sms(msisdn: str, message: str) -> str:
    """Best-effort SMS send via Arkesel HTTP API. Returns 'sent'/'failed'/error code."""
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


def _fmt_money(x: float, style: str = "GHS") -> str:
    """
    Format money for SMS.
    Default uses ASCII-safe 'GHS' (avoids Unicode SMS quirks).
    To switch to the cedi symbol, call _fmt_money(x, style="GH₵").
    """
    amt = f"{float(x):.0f}" if float(x).is_integer() else f"{float(x):.2f}"
    if style == "GH₵":
        return f"GH₵{amt}"
    return f"GHS{amt}"


def _user_snapshot(u: dict) -> dict:
    return {
        "_id": u["_id"],
        "first_name": u.get("first_name", ""),
        "last_name": u.get("last_name", ""),
        "phone": u.get("phone", ""),
    }


def _to_float_safe(v, default: float = 0.0) -> float:
    """
    Safely convert to float.
    Handles None, "", and bad types without throwing.
    """
    try:
        if v is None:
            return float(default)
        return float(v)
    except Exception:
        return float(default)


# ------------ Routes ------------
@admin_balance_bp.route("/admin/balances")
def view_balances():
    """
    List balances with fast batch lookups and server-side search.
    Optional query params:
      q=  (filters by name or phone)
      limit= (default 200)
    """
    q = (request.args.get("q") or "").strip().lower()
    try:
        limit = max(1, min(int(request.args.get("limit", "200")), 1000))
    except ValueError:
        limit = 200

    # Fetch balances first (small projection)
    bal_cursor = balances_col.find({}, {"user_id": 1, "amount": 1, "currency": 1, "updated_at": 1}).limit(limit)
    balances_raw = list(bal_cursor)
    user_ids = [b["user_id"] for b in balances_raw if b.get("user_id")]

    # Batch-fetch users
    users_map = {}
    if user_ids:
        for u in users_col.find({"_id": {"$in": user_ids}}, {"first_name": 1, "last_name": 1, "phone": 1}):
            users_map[u["_id"]] = _user_snapshot(u)

    balances = []
    for bal in balances_raw:
        user = users_map.get(bal.get("user_id"))
        if not user:
            continue  # skip orphans

        full_name = f"{user['first_name']} {user['last_name']}".strip()
        phone = user.get("phone", "")
        # If server-side filter requested
        if q:
            if q not in full_name.lower() and q not in (phone or "").lower():
                continue

        balances.append({
            "_id": str(bal["_id"]),  # ensure string for HTML attributes
            "user": {
                "_id": str(user["_id"]),  # string so history URL works
                "first_name": user["first_name"],
                "last_name": user["last_name"],
                "phone": phone,
            },
            "amount": _to_float_safe(bal.get("amount")),  # safe float
            "currency": bal.get("currency", "GHS"),
            "updated_at": bal.get("updated_at"),
        })

    return render_template("admin_balance.html", balances=balances)


# NOTE: "SET NEW AMOUNT" route REMOVED — only deposit & withdraw remain.


@admin_balance_bp.route("/admin/balances/deposit/<balance_id>", methods=["POST"])
def deposit_balance(balance_id):
    """
    Increment by a positive amount (deposit).
    Sends SMS: "Your account has been credited with GHSX, balance: GHSY"
    """
    delta = request.form.get("amount")
    note = (request.form.get("note") or "").strip()

    if not delta:
        msg = "Enter an amount to deposit."
        if _is_ajax(request):
            return jsonify(success=False, message=msg), 400
        flash(msg, "warning")
        return redirect(url_for("admin_balance.view_balances"))

    try:
        delta_f = float(delta)
        if delta_f <= 0:
            msg = "Deposit amount must be greater than zero."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "warning")
            return redirect(url_for("admin_balance.view_balances"))

        bal = balances_col.find_one({"_id": ObjectId(balance_id)})
        if not bal:
            msg = "Balance not found."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 404
            flash(msg, "danger")
            return redirect(url_for("admin_balance.view_balances"))

        old_amount = _to_float_safe(bal.get("amount"))
        new_amount = old_amount + delta_f
        currency = bal.get("currency", "GHS")

        balances_col.update_one(
            {"_id": bal["_id"]},
            {"$set": {"amount": new_amount, "updated_at": _now()}}
        )

        actor_id, actor_name = _get_actor()
        log_res = balance_logs_col.insert_one({
            "balance_id": bal["_id"],
            "user_id": bal["user_id"],
            "action": "deposit",
            "delta": float(delta_f),
            "amount_before": float(old_amount),
            "amount_after": float(new_amount),
            "currency": currency,
            "note": note[:240],
            "actor_id": ObjectId(actor_id) if actor_id else None,
            "actor_name": actor_name,
            "created_at": _now(),
        })

        gateway = (request.form.get("gateway") or "admin_wallet").strip().lower()
        if log_res and not transactions_col.find_one({"balance_log_id": log_res.inserted_id}):
            transactions_col.insert_one({
                "reference": _make_admin_wallet_reference(bal["_id"]),
                "user_id": bal["user_id"],
                "amount": float(delta_f),
                "currency": currency,
                "type": "deposit",
                "status": "success",
                "source": "admin_wallet",
                "gateway": gateway or "admin_wallet",
                "created_at": _now(),
                "verified_at": _now(),
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "note": note[:240],
                "balance_log_id": log_res.inserted_id,
            })

        # SMS
        user = users_col.find_one({"_id": bal["user_id"]}, {"phone": 1})
        sms_status = None
        if user:
            msisdn = _normalize_phone(user.get("phone", ""))
            if msisdn:
                message = f"Your account has been credited with {_fmt_money(delta_f)}, balance: {_fmt_money(new_amount)}"
                sms_status = _send_sms(msisdn, message)
            else:
                sms_status = "invalid_phone"

        ok_msg = "Deposit successful."
        if sms_status == "sent":
            ok_msg += " SMS sent."
        elif sms_status in ("failed", "error"):
            ok_msg += " (SMS delivery failed)"
        elif sms_status == "invalid_phone":
            ok_msg += " (Phone not valid for SMS)"

        if _is_ajax(request):
            return jsonify(success=True, message=ok_msg, new_balance=new_amount)
        flash(ok_msg, "success")
    except Exception as e:
        print("Deposit Error:", e)
        if _is_ajax(request):
            return jsonify(success=False, message="Error processing deposit."), 500
        flash("Error processing deposit.", "danger")

    return redirect(url_for("admin_balance.view_balances"))


@admin_balance_bp.route("/admin/balances/withdraw/<balance_id>", methods=["POST"])
def withdraw_balance(balance_id):
    """
    Decrement by a positive amount (withdraw).
    Prevents negative balance (no overdraft).
    Sends SMS: "Your account has been debited with GHSX, balance: GHSY"
    """
    delta = request.form.get("amount")
    note = (request.form.get("note") or "").strip()

    if not delta:
        msg = "Enter an amount to withdraw."
        if _is_ajax(request):
            return jsonify(success=False, message=msg), 400
        flash(msg, "warning")
        return redirect(url_for("admin_balance.view_balances"))

    try:
        delta_f = float(delta)
        if delta_f <= 0:
            msg = "Withdrawal amount must be greater than zero."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "warning")
            return redirect(url_for("admin_balance.view_balances"))

        bal = balances_col.find_one({"_id": ObjectId(balance_id)})
        if not bal:
            msg = "Balance not found."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 404
            flash(msg, "danger")
            return redirect(url_for("admin_balance.view_balances"))

        old_amount = _to_float_safe(bal.get("amount"))
        new_amount = old_amount - delta_f
        if new_amount < 0:
            msg = "Insufficient funds: cannot withdraw more than the current balance."
            if _is_ajax(request):
                return jsonify(success=False, message=msg), 400
            flash(msg, "danger")
            return redirect(url_for("admin_balance.view_balances"))

        currency = bal.get("currency", "GHS")

        balances_col.update_one(
            {"_id": bal["_id"]},
            {"$set": {"amount": new_amount, "updated_at": _now()}}
        )

        actor_id, actor_name = _get_actor()
        log_res = balance_logs_col.insert_one({
            "balance_id": bal["_id"],
            "user_id": bal["user_id"],
            "action": "withdraw",
            "delta": float(-delta_f),
            "amount_before": float(old_amount),
            "amount_after": float(new_amount),
            "currency": currency,
            "note": note[:240],
            "actor_id": ObjectId(actor_id) if actor_id else None,
            "actor_name": actor_name,
            "created_at": _now(),
        })

        gateway = (request.form.get("gateway") or "admin_wallet").strip().lower()
        if log_res and not transactions_col.find_one({"balance_log_id": log_res.inserted_id}):
            transactions_col.insert_one({
                "reference": _make_admin_wallet_reference(bal["_id"]),
                "user_id": bal["user_id"],
                "amount": float(delta_f),
                "currency": currency,
                "type": "withdraw",
                "status": "success",
                "source": "admin_wallet",
                "gateway": gateway or "admin_wallet",
                "created_at": _now(),
                "verified_at": _now(),
                "actor_id": ObjectId(actor_id) if actor_id else None,
                "note": note[:240],
                "balance_log_id": log_res.inserted_id,
            })

        # SMS
        user = users_col.find_one({"_id": bal["user_id"]}, {"phone": 1})
        sms_status = None
        if user:
            msisdn = _normalize_phone(user.get("phone", ""))
            if msisdn:
                message = f"Your account has been debited with {_fmt_money(delta_f)}, balance: {_fmt_money(new_amount)}"
                sms_status = _send_sms(msisdn, message)
            else:
                sms_status = "invalid_phone"

        ok_msg = "Withdrawal successful."
        if sms_status == "sent":
            ok_msg += " SMS sent."
        elif sms_status in ("failed", "error"):
            ok_msg += " (SMS delivery failed)"
        elif sms_status == "invalid_phone":
            ok_msg += " (Phone not valid for SMS)"

        if _is_ajax(request):
            return jsonify(success=True, message=ok_msg, new_balance=new_amount)
        flash(ok_msg, "success")
    except Exception as e:
        print("Withdraw Error:", e)
        if _is_ajax(request):
            return jsonify(success=False, message="Error processing withdrawal."), 500
        flash("Error processing withdrawal.", "danger")

    return redirect(url_for("admin_balance.view_balances"))


@admin_balance_bp.route("/admin/balances/history/<user_id>")
def balance_history(user_id):
    """
    Return JSON history for a given user (sorted newest first).
    Consumed by the modal in the UI.
    """
    try:
        uid = ObjectId(user_id)
    except Exception:
        return jsonify({"success": False, "error": "Invalid user id"}), 400

    try:
        logs = []
        cursor = (
            balance_logs_col.find(
                {"user_id": uid},
                {
                    "action": 1,
                    "delta": 1,
                    "amount_before": 1,
                    "amount_after": 1,
                    "currency": 1,
                    "note": 1,
                    "actor_name": 1,
                    "created_at": 1,
                },
            )
            .sort("created_at", -1)
            .limit(200)
        )
        for lg in cursor:
            logs.append({
                "id": str(lg["_id"]),
                "action": lg.get("action"),
                "delta": _to_float_safe(lg.get("delta")),               # SAFE
                "amount_before": _to_float_safe(lg.get("amount_before")),  # SAFE
                "amount_after": _to_float_safe(lg.get("amount_after")),    # SAFE
                "currency": lg.get("currency", "GHS"),
                "note": lg.get("note", ""),
                "actor_name": lg.get("actor_name", "admin"),
                "created_at": lg.get("created_at").strftime("%Y-%m-%d %H:%M") if lg.get("created_at") else "",
            })
        return jsonify({"success": True, "logs": logs})
    except Exception as e:
        print("History Error:", e)
        return jsonify({"success": False, "error": "Server error loading history"}), 500
