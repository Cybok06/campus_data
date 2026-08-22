# shares.py — Admin insights for public (house wallet) orders + Wallet withdrawals
from __future__ import annotations

from flask import Blueprint, render_template, request, jsonify
from bson import ObjectId
from datetime import datetime, timezone
from typing import Any, Dict, List
import os, math, random

from db import db

orders_col = db["orders"]
transactions_col = db["transactions"]

shares_bp = Blueprint("shares", __name__)

HOUSE_USER_ID = ObjectId(os.getenv("HOUSE_USER_ID", "6892c12eaecf4fd8d6fce9e6"))

def _to_money(x: Any) -> float:
    try:
        return round(float(x or 0.0), 2)
    except Exception:
        return 0.0

def _nearest_whole(x: float) -> int:
    xf = float(x or 0.0)
    return int(xf + 0.5)

def _asked_from_items(items: List[Dict[str, Any]]) -> float:
    total = 0.0
    for it in items or []:
        amt = _to_money(it.get("amount"))
        total += float(_nearest_whole(amt) + 1)
    return round(total, 2)

def _asked_for_order(order: Dict[str, Any]) -> float:
    dbg = (order.get("debug") or {})
    exp = dbg.get("paystack_expected_ghs")
    if exp is not None:
        return _to_money(exp)
    return _asked_from_items(order.get("items") or [])

def _profit_for_order(order: Dict[str, Any]) -> float:
    system_total = _to_money(order.get("total_amount"))
    asked = _asked_for_order(order)
    return round(asked - system_total, 2)

def _fmt_dt(dt: Any) -> str:
    if isinstance(dt, datetime):
        return dt.strftime("%Y-%m-%d %H:%M")
    return str(dt or "")

def _midnight_today_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(year=now.year, month=now.month, day=now.day, tzinfo=timezone.utc)

def _gen_withdraw_ref() -> str:
    return f"WD-{int(datetime.now().timestamp())}-{random.randint(10000,99999)}"

@shares_bp.route("/shares", methods=["GET"])
def shares_dashboard():
    """
    Insights for public checkout orders (house wallet):
    - Cards: System Spend, User Pay (Asked), Profit (All Time), Profit (Today), Total Orders, Wallet
    - Wallet: Available = Profit (All Time) - Withdrawals
    - Table with pagination and paystack ref per order
    - Withdrawal history (paginated separately)
    """
    base_query = {"user_id": HOUSE_USER_ID, "debug.public_checkout": True}

    # ---- pagination for orders ----
    try:
        page = max(int(request.args.get("page", 1)), 1)
    except Exception:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 20))
        if per_page not in (10, 20, 30, 50, 100):
            per_page = 20
    except Exception:
        per_page = 20
    skip = (page - 1) * per_page

    # ---- totals (all time) ----
    total_orders = orders_col.count_documents(base_query)
    system_sum = 0.0
    asked_sum = 0.0
    profit_sum = 0.0

    for od in orders_col.find(base_query, {"total_amount": 1, "items": 1, "debug": 1}):
        sys_t = _to_money(od.get("total_amount"))
        ask_t = _asked_for_order(od)
        prof = round(ask_t - sys_t, 2)
        system_sum += sys_t
        asked_sum += ask_t
        profit_sum += prof

    system_sum = round(system_sum, 2)
    asked_sum = round(asked_sum, 2)
    profit_sum = round(profit_sum, 2)

    # ---- today's profit (UTC) ----
    today0 = _midnight_today_utc()
    todays_profit_sum = 0.0
    for od in orders_col.find(
        {"created_at": {"$gte": today0}, **base_query},
        {"total_amount": 1, "items": 1, "debug": 1}
    ):
        todays_profit_sum += _profit_for_order(od)
    todays_profit_sum = round(todays_profit_sum, 2)

    # ---- withdrawals info ----
    wd_query = {
        "user_id": HOUSE_USER_ID,
        "type": "profit_withdrawal",
        "status": "success",
    }
    withdrawals_sum = 0.0
    for tx in transactions_col.find(wd_query, {"amount": 1}):
        withdrawals_sum += _to_money(tx.get("amount"))
    withdrawals_sum = round(withdrawals_sum, 2)

    available_balance = round(profit_sum - withdrawals_sum, 2)
    if available_balance < 0:
        available_balance = 0.0

    # ---- order rows page ----
    cursor = orders_col.find(
        base_query,
        {
            "order_id": 1, "status": 1, "created_at": 1, "updated_at": 1,
            "items": 1, "total_amount": 1, "paid_from": 1,
            "paystack_reference": 1, "charged_amount": 1, "debug": 1
        }
    ).sort([("created_at", -1)]).skip(skip).limit(per_page)

    rows: List[Dict[str, Any]] = []
    for od in cursor:
        sys_t = _to_money(od.get("total_amount"))
        ask_t = _asked_for_order(od)
        prof = round(ask_t - sys_t, 2)
        rows.append({
            "order_id": od.get("order_id"),
            "paystack_reference": od.get("paystack_reference") or "-",
            "created_at": _fmt_dt(od.get("created_at")),
            "updated_at": _fmt_dt(od.get("updated_at")),
            "status": (od.get("status") or "").title(),
            "paid_from": od.get("paid_from") or "-",
            "items_count": len(od.get("items") or []),
            "system_total": sys_t,
            "asked_total": ask_t,
            "profit": prof,
        })

    total_pages = max(1, math.ceil(total_orders / float(per_page)))

    # ---- withdrawal history (paginated separately) ----
    try:
        wpage = max(int(request.args.get("wpage", 1)), 1)
    except Exception:
        wpage = 1
    try:
        wper = int(request.args.get("wper", 10))
        if wper not in (5, 10, 20, 50):
            wper = 10
    except Exception:
        wper = 10
    wskip = (wpage - 1) * wper

    wtotal = transactions_col.count_documents(wd_query)
    wcursor = transactions_col.find(
        wd_query,
        {"amount": 1, "created_at": 1, "reference": 1, "meta": 1}
    ).sort([("created_at", -1)]).skip(wskip).limit(wper)

    withdrawals: List[Dict[str, Any]] = []
    for tx in wcursor:
        withdrawals.append({
            "reference": tx.get("reference") or "-",
            "created_at": _fmt_dt(tx.get("created_at")),
            "amount": _to_money(tx.get("amount")),
            "note": ((tx.get("meta") or {}).get("note") or "-"),
        })
    wpages = max(1, math.ceil(wtotal / float(wper)))

    return render_template(
        "shares.html",
        page=page, per_page=per_page, total_pages=total_pages,
        total_orders=total_orders,
        rows=rows,
        cards={
            "system_sum": system_sum,
            "asked_sum": asked_sum,
            "profit_sum": profit_sum,
            "todays_profit_sum": todays_profit_sum,
            "withdrawals_sum": withdrawals_sum,
            "available_balance": available_balance,
        },
        # wallet history
        wpage=wpage, wper=wper, wpages=wpages, wtotal=wtotal, withdrawals=withdrawals
    )

@shares_bp.route("/shares/withdraw", methods=["POST"])
def shares_withdraw():
    """
    Create a withdrawal against profit wallet.
    Body: JSON { "amount": number, "note": string }
    """
    try:
        data = request.get_json(silent=True) or {}
        amt = _to_money(data.get("amount"))
        note = (data.get("note") or "").strip()

        if amt <= 0:
            return jsonify({"success": False, "message": "Amount must be greater than zero."}), 400

        # recompute available (authoritative, same logic as dashboard)
        base_query = {"user_id": HOUSE_USER_ID, "debug.public_checkout": True}
        profit_sum = 0.0
        for od in orders_col.find(base_query, {"total_amount": 1, "items": 1, "debug": 1}):
            profit_sum += _profit_for_order(od)
        profit_sum = round(profit_sum, 2)

        wd_query = {"user_id": HOUSE_USER_ID, "type": "profit_withdrawal", "status": "success"}
        withdrawals_sum = 0.0
        for tx in transactions_col.find(wd_query, {"amount": 1}):
            withdrawals_sum += _to_money(tx.get("amount"))
        withdrawals_sum = round(withdrawals_sum, 2)

        available = round(profit_sum - withdrawals_sum, 2)
        if available < amt - 1e-9:
            return jsonify({"success": False, "message": "Insufficient wallet balance."}), 400

        ref = _gen_withdraw_ref()
        now = datetime.utcnow()
        transactions_col.insert_one({
            "user_id": HOUSE_USER_ID,
            "amount": amt,                     # positive number for audit
            "reference": ref,
            "status": "success",
            "type": "profit_withdrawal",
            "gateway": "Wallet",
            "currency": "GHS",
            "created_at": now,
            "verified_at": now,
            "meta": {"note": note}
        })

        new_withdrawals_sum = round(withdrawals_sum + amt, 2)
        new_available = round(profit_sum - new_withdrawals_sum, 2)
        return jsonify({
            "success": True,
            "message": "Withdrawal recorded.",
            "reference": ref,
            "wallet": {
                "profit_sum": profit_sum,
                "withdrawals_sum": new_withdrawals_sum,
                "available_balance": max(0.0, new_available)
            },
            "tx": {
                "reference": ref,
                "amount": amt,
                "created_at": _fmt_dt(now),
                "note": note or "-"
            }
        }), 200

    except Exception as e:
        return jsonify({"success": False, "message": f"Error: {e}"}), 500
