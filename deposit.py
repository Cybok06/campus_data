from flask import Blueprint, render_template, session, redirect, url_for, request, flash
from bson import ObjectId
from datetime import datetime
import requests, json

from db import db

deposit_bp = Blueprint("deposit", __name__)
balances_col = db["balances"]
transactions_col = db["transactions"]
users_col = db["users"]

# ==========================
# ✅ HARDCODED PAYSTACK KEYS
# ==========================
PAYSTACK_PUBLIC_KEY = "pk_live_85fcc37236d5be344124733a4452e9733e059ded"
PAYSTACK_SECRET_KEY = "sk_live_69375953a7230f5694a898d9864b5d3e13e2b19a"

# ✅ Hardcode fee rate too (0.5%)
DEPOSIT_FEE_RATE = 0.005
MIN_DEPOSIT_GHS = 10.0


def _r2(x: float) -> float:
    return round(float(x or 0), 2)


@deposit_bp.route("/deposit")
def deposit_page():
    if session.get("role") != "customer" or "user_id" not in session:
        return redirect(url_for("login.login"))

    email = session.get("email")
    if not email:
        user = users_col.find_one({"_id": ObjectId(session["user_id"])})
        email = user.get("email", "") if user else ""

    return render_template(
        "deposit.html",
        user_id=session["user_id"],
        email=email,
        paystack_pk=PAYSTACK_PUBLIC_KEY,     # ✅ send hardcoded PK to UI
        deposit_fee_rate=DEPOSIT_FEE_RATE,   # 0.5% sent to UI
        min_deposit=MIN_DEPOSIT_GHS,
    )


@deposit_bp.route("/verify_transaction")
def verify_transaction():
    reference = request.args.get("reference", type=str)
    user_id = session.get("user_id")

    if not reference or not user_id:
        flash("❌ Invalid deposit request", "danger")
        return redirect(url_for("customer_dashboard.customer_dashboard"))

    headers = {"Authorization": f"Bearer {PAYSTACK_SECRET_KEY}"}
    url = f"https://api.paystack.co/transaction/verify/{reference}"

    try:
        r = requests.get(url, headers=headers, timeout=20)
        result = r.json()
        print("🧾 Paystack Verification Response:", json.dumps(result, indent=2))

        ok = result.get("status") and result.get("data", {}).get("status") == "success"
        if not ok:
            fail_msg = result.get("message") or result.get("data", {}).get("gateway_response") or "Verification failed."
            flash(f"❌ Payment verification failed: {fail_msg}", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        data = result["data"]

        # Amount from Paystack is in pesewas → GHS
        paid_gross_ghs = _r2((data.get("amount", 0) or 0) / 100.0)
        currency = data.get("currency", "GHS")
        channel = data.get("channel", "")
        paid_ref = data.get("reference")
        metadata = data.get("metadata") or {}

        if paid_gross_ghs <= 0 or currency != "GHS":
            flash("❌ Invalid payment amount/currency.", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        # Idempotency
        if transactions_col.find_one({"reference": paid_ref, "status": "success"}):
            flash("✅ Deposit already verified earlier.", "success")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        # STRICT RULE: credit EXACTLY what the user entered (net_amount_ghs)
        fee_rate = float(metadata.get("fee_rate", DEPOSIT_FEE_RATE) or 0.0)

        meta_net = metadata.get("net_amount_ghs")
        try:
            net_credit_ghs = _r2(float(meta_net)) if meta_net is not None else None
        except Exception:
            net_credit_ghs = None

        if net_credit_ghs is None:
            # Fallback only if metadata is missing (old clients)
            net_credit_ghs = _r2(paid_gross_ghs / (1.0 + fee_rate))

        # Enforce minimum deposit (GHS)
        if net_credit_ghs < MIN_DEPOSIT_GHS:
            flash(f"❌ Minimum deposit is GHS {MIN_DEPOSIT_GHS:.2f}.", "danger")
            return redirect(url_for("customer_dashboard.customer_dashboard"))

        # Guardrails — never over-credit
        if net_credit_ghs < 0:
            net_credit_ghs = 0.0
        if net_credit_ghs > paid_gross_ghs:
            net_credit_ghs = paid_gross_ghs

        # For records: compute fee for audit
        fee_ghs = _r2(paid_gross_ghs - net_credit_ghs)

        # Credit NET to wallet
        balances_col.update_one(
            {"user_id": ObjectId(user_id)},
            {"$inc": {"amount": net_credit_ghs}, "$set": {"updated_at": datetime.utcnow()}},
            upsert=True,
        )

        # Store full audit details
        transactions_col.insert_one({
            "user_id": ObjectId(user_id),
            "amount": net_credit_ghs,          # NET credited (exact user entry)
            "reference": paid_ref,
            "status": "success",
            "type": "deposit",
            "gateway": "Paystack",
            "currency": currency,
            "channel": channel,
            "raw": data,
            "verified_at": datetime.utcnow(),
            "created_at": datetime.utcnow(),
            "meta": {
                "paid_gross_ghs": paid_gross_ghs,
                "net_credit_ghs": net_credit_ghs,
                "fee_ghs": fee_ghs,
                "fee_rate": fee_rate,
                "source": "deposit_fee_0p5_strict_net_credit"
            }
        })

        flash(f"✅ Deposit successful! Credited ₵{net_credit_ghs:.2f}.", "success")

    except Exception as e:
        print("❌ Paystack Exception:", str(e))
        flash("❌ Could not verify payment. Please try again.", "danger")

    return redirect(url_for("customer_dashboard.customer_dashboard"))
