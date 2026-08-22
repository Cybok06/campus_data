# admin_referals.py
from flask import Blueprint, render_template, session, redirect, url_for, request
from bson import ObjectId
from db import db

admin_referrals_bp = Blueprint("admin_referrals", __name__)
users_col = db["users"]
referrals_col = db["referrals"]

def _require_admin():
    return session.get("role") == "admin"

@admin_referrals_bp.route("/admin/referrals")
def admin_referrals():
    """
    Referral Overview (authoritative on `referrals` collection):
      - Each document in `referrals` has: { user_id, ref_code, created_at }
      - Show the owner (user_id), the code, total referred users (from users.referral),
        and list of referred users (username & phone, plus optional name/email).
      - Case-insensitive match between users.referral and ref_code.
      - Optional ?q= filter over owner & ref_code.
    """
    if not _require_admin():
        return redirect(url_for("login.login"))

    q = (request.args.get("q") or "").strip().lower()

    # 1) Load all referral codes with their owners
    ref_rows = list(referrals_col.find({}, {"user_id": 1, "ref_code": 1, "created_at": 1}))
    if not ref_rows:
        return render_template(
            "admin_referrals.html",
            referrals=[],
            total_referred=0,
            q=q
        )

    owner_ids = []
    code_lowers = []
    code_by_lower = {}      # lower(code) -> original code (preserve display case)
    rows_by_lower = {}      # lower(code) -> list of rows (usually 1, but support multiple)
    for r in ref_rows:
        code = (r.get("ref_code") or "").strip()
        if not code:
            continue
        cl = code.lower()
        code_lowers.append(cl)
        code_by_lower[cl] = code_by_lower.get(cl) or code
        rows_by_lower.setdefault(cl, []).append(r)
        uid = r.get("user_id")
        if isinstance(uid, ObjectId):
            owner_ids.append(uid)
        else:
            # best-effort: try cast
            try:
                owner_ids.append(ObjectId(uid))
            except Exception:
                pass

    # 2) Load all owners at once
    owners = list(users_col.find(
        {"_id": {"$in": list(set(owner_ids))}},
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "email": 1}
    ))
    owner_map = {o["_id"]: o for o in owners}

    # 3) Load all customers that have any referral filled and group by lower(referral)
    referred_cursor = users_col.find(
        {"role": "customer", "referral": {"$exists": True, "$ne": ""}},
        {"first_name": 1, "last_name": 1, "username": 1, "phone": 1, "email": 1, "referral": 1, "created_at": 1}
    )

    # Build lower(referral) -> list of users who used that code
    referred_map = {}
    for u in referred_cursor:
        code_used = (u.get("referral") or "").strip()
        if not code_used:
            continue
        referred_map.setdefault(code_used.lower(), []).append(u)

    # 4) Build results from referral rows (only codes present in referrals collection)
    results = []
    total_referred = 0

    # If there are duplicate rows with the same code (rare), we show one card per row/owner pair.
    for cl in rows_by_lower.keys():
        code_display = code_by_lower.get(cl, cl)
        for r in rows_by_lower[cl]:
            owner = owner_map.get(r.get("user_id"))
            referred_list = referred_map.get(cl, []) or []
            total_referred += len(referred_list)

            # Simplify referred users for template (username + phone highlighted; name/email optional)
            simplified = []
            for ru in referred_list:
                simplified.append({
                    "_id": str(ru.get("_id")),
                    "username": ru.get("username") or "",
                    "phone": ru.get("phone") or "",
                    "first_name": ru.get("first_name") or "",
                    "last_name": ru.get("last_name") or "",
                    "email": ru.get("email") or "",
                    "created_at": ru.get("created_at"),
                })

            results.append({
                "referrer": owner,               # dict or None
                "ref_code": code_display,        # preserve original case
                "created_at": r.get("created_at"),
                "referred": simplified,          # list of dicts
                "count": len(simplified),
            })

    # 5) Optional filter ?q=
    if q:
        filtered = []
        for row in results:
            ref = row.get("referrer") or {}
            haystack = [
                row.get("ref_code", ""),
                ref.get("username", ""),
                ref.get("phone", ""),
                ref.get("email", ""),
                ref.get("first_name", ""),
                ref.get("last_name", ""),
            ]
            if any((s or "").lower().find(q) >= 0 for s in haystack):
                filtered.append(row)
        results = filtered

    # 6) Sort: by count desc, then by ref_code asc
    results.sort(key=lambda r: (-r["count"], str(r["ref_code"]).lower()))

    return render_template(
        "admin_referrals.html",
        referrals=results,
        total_referred=total_referred,
        q=(request.args.get("q") or "")
    )
