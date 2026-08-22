from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
import os
import uuid
import requests
import json

from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for
from db import db
from routes.provider_wallet import (
    ensure_provider_account,
    credit_provider,
    PROVIDER_WALLET_KEY,
    PROVIDER_WALLET_LABEL,
)

admin_provider_balances_bp = Blueprint("admin_provider_balances", __name__)

provider_accounts_col = db["provider_accounts"]
provider_transactions_col = db["provider_transactions"]



def _clean_key(v: Any) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _is_pk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("pk_")


def _is_sk(v: str) -> bool:
    return isinstance(v, str) and v.strip().lower().startswith("sk_")


def _load_paystack_keys() -> Tuple[str, str]:
    # Hardcoded test keys (as requested)
    pk = "pk_live_4c909336372002195e900f36649a37c56d0b8cdb"
    sk = "sk_live_4316292a9beb8d5e619f6f97864bed7ed7f19fb7"

    # auto-fix swap if misconfigured
    if _is_sk(pk) and _is_pk(sk):
        pk, sk = sk, pk

    # defensive recovery
    if not _is_pk(pk) and _is_pk(sk):
        pk = sk
    if not _is_sk(sk) and _is_sk(pk):
        sk = pk

    return pk, sk


def _is_admin() -> bool:
    return bool(session.get("admin_logged_in") or session.get("role") in ("admin", "superadmin"))


def _ensure_provider_accounts() -> None:
    try:
        ensure_provider_account(PROVIDER_WALLET_KEY)
    except Exception:
        pass


def _provider_card_list() -> List[Dict[str, Any]]:
    _ensure_provider_accounts()
    doc = provider_accounts_col.find_one(
        {"provider": PROVIDER_WALLET_KEY},
        {"provider": 1, "balance": 1, "currency": 1},
    ) or {}

    return [
        {
            "provider_key": PROVIDER_WALLET_KEY,
            "provider_label": PROVIDER_WALLET_LABEL,
            "balance": float(doc.get("balance", 0.0) or 0.0),
            "currency": doc.get("currency") or "GHS",
        }
    ]


def _valid_email(raw: str) -> bool:
    s = (raw or "").strip()
    return "@" in s and "." in s


def _admin_email_fallback() -> str:
    raw_email = (session.get("email") or "").strip()
    if not _valid_email(raw_email):
        raw_email = ""
    return raw_email or "nagosenu4@gmail.com"


@admin_provider_balances_bp.route("/admin/provider-balances")
def admin_provider_balances():
    if not _is_admin():
        return redirect(url_for("login.login"))

    cards = _provider_card_list()
    admin_email = _admin_email_fallback()

    return render_template(
        "admin_provider_balances.html",
        providers=cards,
        admin_email=admin_email,
    )


@admin_provider_balances_bp.route("/admin/provider-fund/init", methods=["POST"])
def admin_provider_fund_init():
    if not _is_admin():
        return jsonify({"success": False, "message": "Not authorized"}), 403

    data = request.get_json(silent=True) or {}
    provider = (data.get("provider") or "").strip().lower() or PROVIDER_WALLET_KEY
    amount_raw = data.get("amount")

    if provider != PROVIDER_WALLET_KEY:
        provider = PROVIDER_WALLET_KEY

    try:
        amount = float(str(amount_raw).replace(",", "").strip())
    except Exception:
        amount = 0.0

    if amount <= 0:
        return jsonify({"success": False, "message": "Amount must be greater than zero"}), 400

    pk, sk = _load_paystack_keys()
    if not _is_pk(pk) or not _is_sk(sk):
        return jsonify({"success": False, "message": "Paystack not configured"}), 400

    reference = f"PROV-{provider.upper()}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    email = (data.get("email") or "").strip()
    if not _valid_email(email):
        email = _admin_email_fallback()

    amount_pes = int(round(amount * 100))
    payload = {
        "email": email,
        "amount": amount_pes,
        "currency": "GHS",
        "reference": reference,
        "metadata": {"provider": provider, "purpose": "PROVIDER_TOPUP", "credit_amount": amount},
    }

    try:
        headers = {"Authorization": f"Bearer {sk}", "Content-Type": "application/json"}
        r = requests.post("https://api.paystack.co/transaction/initialize", headers=headers, json=payload, timeout=25)
        init_data = r.json()
    except Exception as e:
        return jsonify({"success": False, "message": f"Initialize error: {str(e)}"}), 400

    try:
        print(json.dumps({"evt": "provider_paystack_init", "payload": payload, "response": init_data}))
    except Exception:
        pass

    if not isinstance(init_data, dict) or not init_data.get("status"):
        msg = (init_data or {}).get("message") or "Paystack initialize failed"
        return jsonify({"success": False, "message": msg}), 400

    data_obj = init_data.get("data") or {}
    return jsonify(
        {
            "success": True,
            "public_key": pk,
            "reference": reference,
            "access_code": data_obj.get("access_code"),
            "authorization_url": data_obj.get("authorization_url"),
        }
    ), 200


@admin_provider_balances_bp.route("/admin/provider-fund/verify", methods=["POST"])
def admin_provider_fund_verify():
    if not _is_admin():
        return jsonify({"success": False, "message": "Not authorized"}), 403

    data = request.get_json(silent=True) or {}
    provider = (data.get("provider") or "").strip().lower() or PROVIDER_WALLET_KEY
    reference = (data.get("reference") or "").strip()

    if provider != PROVIDER_WALLET_KEY:
        provider = PROVIDER_WALLET_KEY
    if not reference:
        return jsonify({"success": False, "message": "Reference required"}), 400

    _ensure_provider_accounts()

    # idempotency handled by credit_provider via dedupe_key

    _pk, sk = _load_paystack_keys()
    if not _is_sk(sk):
        return jsonify({"success": False, "message": "Paystack not configured"}), 400

    try:
        headers = {"Authorization": f"Bearer {sk}"}
        url = f"https://api.paystack.co/transaction/verify/{reference}"
        r = requests.get(url, headers=headers, timeout=25)
        result = r.json()
    except Exception as e:
        return jsonify({"success": False, "message": f"Verify error: {str(e)}"}), 400

    if not isinstance(result, dict) or not result.get("status"):
        return jsonify({"success": False, "message": (result or {}).get("message") or "Verification failed"}), 400

    data = result.get("data") or {}
    if not isinstance(data, dict) or data.get("status") != "success":
        return jsonify({"success": False, "message": data.get("gateway_response") or "Payment not successful"}), 400

    amount_pes = int(data.get("amount") or 0)
    currency = str(data.get("currency") or "GHS").upper()
    if amount_pes <= 0 or currency != "GHS":
        return jsonify({"success": False, "message": "Invalid payment amount/currency"}), 400

    amount_ghs = round(amount_pes / 100.0, 2)
    credit_amount = amount_ghs
    try:
        meta = data.get("metadata") or {}
        ca = meta.get("credit_amount")
        if ca not in (None, ""):
            credit_amount = round(float(ca), 2)
    except Exception:
        credit_amount = amount_ghs

    if credit_amount <= 0:
        return jsonify({"success": False, "message": "Invalid credit amount"}), 400

    dedupe_key = f"TOPUP:{reference}:{provider}:{credit_amount:.2f}"
    ok, _msg, _bal = credit_provider(
        provider,
        credit_amount,
        order_id=None,
        line_index=None,
        reason="MANUAL_TOPUP",
        meta={"paystack": result, "reference": reference, "paid_amount": amount_ghs, "credit_amount": credit_amount},
        dedupe_key=dedupe_key,
    )
    if not ok:
        return jsonify({"success": False, "message": "Failed to credit provider wallet"}), 400

    acc = provider_accounts_col.find_one({"provider": provider}, {"balance": 1, "currency": 1}) or {}

    return jsonify(
        {
            "success": True,
            "balance": float(acc.get("balance", 0.0) or 0.0),
            "currency": acc.get("currency") or "GHS",
                "amount": amount_ghs,
            }
        ), 200


@admin_provider_balances_bp.route("/admin/provider-transactions", methods=["GET"])
def admin_provider_transactions():
    if not _is_admin():
        return jsonify({"success": False, "message": "Not authorized"}), 403

    provider = (request.args.get("provider") or "").strip().lower() or PROVIDER_WALLET_KEY
    page_raw = (request.args.get("page") or "1").strip()
    limit_raw = (request.args.get("limit") or "10").strip()

    try:
        page = max(1, int(page_raw))
    except Exception:
        page = 1
    try:
        limit = max(1, min(100, int(limit_raw)))
    except Exception:
        limit = 10

    q: Dict[str, Any] = {}
    if provider != PROVIDER_WALLET_KEY:
        provider = PROVIDER_WALLET_KEY
    q["provider"] = provider

    try:
        total_count = int(provider_transactions_col.count_documents(q))
    except Exception:
        total_count = 0

    pages = max(1, (total_count + limit - 1) // limit) if total_count else 1
    if page > pages:
        page = pages

    skip = (page - 1) * limit
    try:
        rows = list(
            provider_transactions_col.find(q, sort=[("created_at", -1)], skip=skip, limit=limit)
        )
    except Exception:
        rows = []

    txs: List[Dict[str, Any]] = []
    for r in rows:
        created_at = r.get("created_at")
        txs.append(
            {
                "_id": str(r.get("_id")),
                "provider": r.get("provider"),
                "amount": float(r.get("amount", 0.0) or 0.0),
                "direction": r.get("direction") or r.get("type") or "",
                "reason": r.get("reason") or r.get("source") or "",
                "reference": r.get("reference") or "",
                "status": r.get("status") or "success",
                "order_id": r.get("order_id") or "",
                "created_at": created_at.isoformat() if isinstance(created_at, datetime) else "",
            }
        )

    return jsonify(
        {
            "success": True,
            "total_count": total_count,
            "page": page,
            "pages": pages,
            "transactions": txs,
        }
    ), 200
