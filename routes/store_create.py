from __future__ import annotations

import traceback
import requests
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from bson import ObjectId
from werkzeug.utils import secure_filename
from flask import (
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
    send_file,
    abort,
)

from db import db

# Import the SAME blueprint + shared helpers/collections from store_page.py
from .store_page import (
    stores_bp,
    fs,
    services_col,
    stores_col,
    _load_all_services_for_store_edit,
    _store_to_client,
    _find_user_store,
    _upsert_store_from_payload,
)

# ============================
# Store products (isolated)
# ============================
store_products_col = db["store_products"]
images_col = db["images"]
users_col = db["users"]  # ✅ needed to pull owner email/firstname/lastname

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif"}

# ===== Cloudflare Images (hardcoded as requested) =====
CF_ACCOUNT_ID = "63e6f91eec9591f77699c4b434ab44c6"
CF_IMAGES_TOKEN = "Brz0BEfl_GqEUjEghS2UEmLZhK39EUmMbZgu_hIo"
CF_HASH = "h9fmMoa1o2c2P55TcWJGOg"
DEFAULT_VARIANT = "public"  # ensure this variant exists in Cloudflare Images


# -----------------------------
# Helpers
# -----------------------------
def _allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _to_float(val: Any, default: float = 0.0) -> float:
    try:
        return float(str(val).replace(",", "").strip())
    except Exception:
        return float(default)


def _to_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except Exception:
        return int(default)


def _require_customer_login() -> bool:
    return bool(session.get("role") == "customer" and session.get("user_id"))


def _owner_id() -> ObjectId:
    return ObjectId(session["user_id"])


def _ensure_store_owned(slug: str) -> Optional[Dict[str, Any]]:
    """
    Ensure the store exists and belongs to the logged-in customer (not deleted).
    """
    try:
        doc = stores_col.find_one(
            {"slug": slug, "owner_id": _owner_id(), "status": {"$ne": "deleted"}},
            {"_id": 1, "slug": 1, "owner_id": 1, "status": 1},
        )
        return doc
    except Exception:
        return None


def _product_to_client(p: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(p or {})
    if out.get("_id"):
        out["id"] = str(out["_id"])
    out.pop("_id", None)
    if isinstance(out.get("created_at"), datetime):
        out["created_at"] = out["created_at"].isoformat()
    if isinstance(out.get("updated_at"), datetime):
        out["updated_at"] = out["updated_at"].isoformat()
    if isinstance(out.get("owner_id"), ObjectId):
        out["owner_id"] = str(out["owner_id"])
    return out


def _extract_selected_service_ids(payload: Dict[str, Any]) -> Tuple[str, List[str]]:
    candidates = [
        "selected_service_ids",
        "service_ids",
        "services",
        "enabled_service_ids",
        "enabled_services",
        "selected_services",
    ]
    for k in candidates:
        v = payload.get(k)
        if isinstance(v, list):
            ids = [str(x).strip() for x in v if str(x).strip()]
            return k, ids
    return "", []


def _enforce_mtn_exclusive_selection(payload: Dict[str, Any]) -> Tuple[bool, str]:
    _, id_strings = _extract_selected_service_ids(payload)
    if not id_strings or len(id_strings) < 2:
        return True, ""

    oids: List[ObjectId] = []
    for s in id_strings:
        try:
            oids.append(ObjectId(s))
        except Exception:
            pass

    if not oids:
        return True, ""

    docs = list(
        services_col.find(
            {"_id": {"$in": oids}},
            {"name": 1, "network": 1, "service_network": 1},
        )
    )

    def norm(x: Any) -> str:
        return str(x or "").strip().lower()

    has_mtn_normal = False
    has_mtn_express = False

    for d in docs:
        name = norm(d.get("name"))
        net = norm(d.get("service_network") or d.get("network"))
        if net == "mtn" or "mtn" in net:
            if name == "mtn normal":
                has_mtn_normal = True
            if name == "mtn express":
                has_mtn_express = True

    if has_mtn_normal and has_mtn_express:
        return (
            False,
            "You cannot select both MTN NORMAL and MTN EXPRESS for the same store. Please choose only one.",
        )

    return True, ""


def _attach_owner_identity_to_store(slug: str, owner_id: ObjectId) -> None:
    """
    ✅ When saving store, attach owner email, first name, last name (pulled from users collection).
    Only sets fields if values exist (won't overwrite with empty strings).
    """
    try:
        u = users_col.find_one(
            {"_id": owner_id},
            {"email": 1, "first_name": 1, "last_name": 1},
        ) or {}

        owner_email = (u.get("email") or "").strip()
        owner_first = (u.get("first_name") or "").strip()
        owner_last = (u.get("last_name") or "").strip()

        sets: Dict[str, Any] = {"updated_at": datetime.utcnow()}
        if owner_email:
            sets["owner_email"] = owner_email
        if owner_first:
            sets["owner_first_name"] = owner_first
        if owner_last:
            sets["owner_last_name"] = owner_last

        # only write if there's something besides updated_at
        if len(sets) > 1:
            stores_col.update_one(
                {"slug": slug, "owner_id": owner_id, "status": {"$ne": "deleted"}},
                {"$set": sets},
            )
    except Exception:
        pass


# ============================================================================
# PAGES (CREATE / EDIT)
# ============================================================================
@stores_bp.route("/create-store", methods=["GET"])
def create_store_page():
    if not _require_customer_login():
        return redirect(url_for("login.login"))

    services_min = _load_all_services_for_store_edit()
    user_id = _owner_id()
    slug = (request.args.get("slug") or "").strip() or None
    store_doc = _find_user_store(user_id, slug)

    store_client = _store_to_client(store_doc)

    return render_template(
        "store_create.html",
        services=services_min,
        store=store_client,
    )


# ============================================================================
# API: media (GridFS)
# ============================================================================
@stores_bp.route("/api/media", methods=["POST"])
def api_upload_media():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    f = (request.files or {}).get("file")
    if not f:
        return jsonify({"success": False, "message": "No file"}), 400

    data = f.read()
    if not data:
        return jsonify({"success": False, "message": "Empty file"}), 400

    fid = fs.put(
        data,
        filename=f.filename or "upload",
        content_type=f.mimetype or "application/octet-stream",
        uploaded_by=str(session.get("user_id")),
        created_at=datetime.utcnow(),
    )
    return jsonify(
        {
            "success": True,
            "id": str(fid),
            "url": url_for("stores.get_media", file_id=str(fid)),
        }
    )


@stores_bp.route("/media/<file_id>", methods=["GET"])
def get_media(file_id: str):
    try:
        oid = ObjectId(file_id)
    except Exception:
        abort(404)

    try:
        gfile = fs.get(oid)
    except Exception:
        abort(404)

    return send_file(
        gfile,
        mimetype=(getattr(gfile, "content_type", None) or "application/octet-stream"),
        as_attachment=False,
        download_name=getattr(gfile, "filename", None) or "file",
    )


# ============================================================================
# API (store CRUD)
# ============================================================================
@stores_bp.route("/api/stores/mine", methods=["GET"])
def api_get_my_store():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    user_id = _owner_id()
    slug = (request.args.get("slug") or "").strip() or None
    store = _find_user_store(user_id, slug)

    client = _store_to_client(store) if store else None
    return jsonify({"success": True, "store": client})


@stores_bp.route("/api/stores", methods=["POST"])
def api_upsert_store():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = _owner_id()
    data = request.get_json(silent=True) or {}

    ok_ex, msg_ex = _enforce_mtn_exclusive_selection(data)
    if not ok_ex:
        return jsonify({"success": False, "message": msg_ex}), 400

    contact = data.get("contact") or {}
    whatsapp_number = (contact.get("whatsapp_number") or "").strip()
    whatsapp_group_link = (contact.get("whatsapp_group_link") or "").strip()

    ok, payload = _upsert_store_from_payload(owner_id, data)
    if not ok:
        return jsonify({"success": False, **payload}), 400

    # ✅ Remove old AFA data if any store has it + ensure contact saved
    try:
        slug = payload.get("slug")
        stores_col.update_one(
            {"slug": slug, "owner_id": owner_id, "status": {"$ne": "deleted"}},
            {
                "$set": {
                    "contact": {
                        "whatsapp_number": whatsapp_number,
                        "whatsapp_group_link": whatsapp_group_link,
                    },
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {"afa": ""},  # ✅ purge old field if it exists
            },
        )
        if slug:
            _attach_owner_identity_to_store(slug, owner_id)  # ✅ add owner email/first/last
    except Exception:
        pass

    return jsonify({"success": True, **payload})


@stores_bp.route("/api/stores/preview", methods=["POST"])
def api_save_draft_for_preview():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    owner_id = _owner_id()
    data = request.get_json(silent=True) or {}
    data["status"] = "draft"

    ok_ex, msg_ex = _enforce_mtn_exclusive_selection(data)
    if not ok_ex:
        return jsonify({"success": False, "message": msg_ex}), 400

    contact = data.get("contact") or {}
    whatsapp_number = (contact.get("whatsapp_number") or "").strip()
    whatsapp_group_link = (contact.get("whatsapp_group_link") or "").strip()

    ok, payload = _upsert_store_from_payload(owner_id, data)
    if not ok:
        return jsonify({"success": False, **payload}), 400

    # Ensure only one active draft per owner
    try:
        slug = payload.get("slug")
        if slug:
            stores_col.update_many(
                {"owner_id": owner_id, "status": "draft", "slug": {"$ne": slug}},
                {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}},
            )
    except Exception:
        pass

    # ✅ Remove old AFA data if any store has it + ensure contact saved
    try:
        slug = payload.get("slug")
        stores_col.update_one(
            {"slug": slug, "owner_id": owner_id, "status": {"$ne": "deleted"}},
            {
                "$set": {
                    "contact": {
                        "whatsapp_number": whatsapp_number,
                        "whatsapp_group_link": whatsapp_group_link,
                    },
                    "updated_at": datetime.utcnow(),
                },
                "$unset": {"afa": ""},  # ✅ purge old field if it exists
            },
        )
        if slug:
            _attach_owner_identity_to_store(slug, owner_id)  # ✅ add owner email/first/last
    except Exception:
        pass

    return jsonify({"success": True, **payload})


@stores_bp.route("/api/stores/<slug>/status", methods=["POST"])
def api_update_status(slug: str):
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    body = request.get_json(silent=True) or {}
    status = (body.get("status") or "").strip()
    if status not in {"published", "suspended", "draft"}:
        return jsonify({"success": False, "message": "Invalid status"}), 400

    res = stores_col.update_one(
        {"slug": slug, "owner_id": _owner_id(), "status": {"$ne": "deleted"}},
        {"$set": {"status": status, "updated_at": datetime.utcnow()}},
    )
    if res.matched_count == 0:
        return jsonify({"success": False, "message": "Store not found"}), 404

    return jsonify({"success": True, "slug": slug, "status": status})


@stores_bp.route("/api/stores/<slug>", methods=["DELETE"])
def api_delete_store(slug: str):
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    res = stores_col.update_one(
        {"slug": slug, "owner_id": _owner_id()},
        {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}},
    )
    if res.matched_count == 0:
        return jsonify({"success": False, "message": "Store not found"}), 404

    try:
        store_products_col.update_many(
            {"store_slug": slug, "owner_id": _owner_id(), "status": {"$ne": "deleted"}},
            {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}},
        )
    except Exception:
        pass

    return jsonify({"success": True})


# ============================================================================
# Cloudflare product image upload (isolated)
# ============================================================================
@stores_bp.route("/api/store-products/upload_image", methods=["POST"])
def api_store_products_upload_image():
    if not _require_customer_login():
        return jsonify({"success": False, "error": "Login required"}), 401

    try:
        if "image" not in request.files:
            return jsonify({"success": False, "error": "No file part in request"}), 400

        image = request.files["image"]
        if not image or image.filename == "":
            return jsonify({"success": False, "error": "No selected file"}), 400

        if not _allowed_file(image.filename):
            return jsonify({"success": False, "error": "File type not allowed"}), 400

        direct_url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/images/v2/direct_upload"
        headers = {"Authorization": f"Bearer {CF_IMAGES_TOKEN}"}

        res = requests.post(direct_url, headers=headers, data={}, timeout=20)
        try:
            j = res.json()
        except Exception:
            return jsonify({"success": False, "error": "Cloudflare direct_upload returned non-JSON"}), 502

        if not j.get("success"):
            return jsonify({"success": False, "error": "Cloudflare direct_upload failed", "details": j}), 400

        upload_url = j["result"]["uploadURL"]
        image_id = j["result"]["id"]

        up = requests.post(
            upload_url,
            files={
                "file": (
                    secure_filename(image.filename),
                    image.stream,
                    image.mimetype or "application/octet-stream",
                )
            },
            timeout=60,
        )
        try:
            uj = up.json()
        except Exception:
            return jsonify({"success": False, "error": "Cloudflare upload returned non-JSON"}), 502

        if not uj.get("success"):
            return jsonify({"success": False, "error": "Cloudflare upload failed", "details": uj}), 400

        variant = (request.args.get("variant") or DEFAULT_VARIANT).strip() or DEFAULT_VARIANT
        image_url = f"https://imagedelivery.net/{CF_HASH}/{image_id}/{variant}"

        images_col.insert_one(
            {
                "provider": "cloudflare_images",
                "image_id": image_id,
                "variant": variant,
                "url": image_url,
                "original_filename": secure_filename(image.filename),
                "mimetype": image.mimetype,
                "size_bytes": request.content_length,
                "created_at": datetime.utcnow(),
                "meta": {"module": "store_products", "owner_id": str(session.get("user_id"))},
            }
        )

        return jsonify({"success": True, "image_url": image_url, "image_id": image_id, "variant": variant})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"success": False, "error": str(e)}), 500


# ============================================================================
# Store products CRUD (isolated)
# ============================================================================
@stores_bp.route("/api/store-products/mine", methods=["GET"])
def api_store_products_mine():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    slug = (request.args.get("slug") or "").strip()
    if not slug:
        return jsonify({"success": True, "products": []})

    store_doc = _ensure_store_owned(slug)
    if not store_doc:
        return jsonify({"success": False, "message": "Save the store first (or invalid slug)."}), 400

    rows = list(
        store_products_col.find(
            {"store_slug": slug, "owner_id": _owner_id(), "status": {"$ne": "deleted"}},
            sort=[("created_at", -1)],
        )
    )
    return jsonify({"success": True, "products": [_product_to_client(x) for x in rows]})


@stores_bp.route("/api/store-products", methods=["POST"])
def api_store_products_create():
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    body = request.get_json(silent=True) or {}
    slug = (body.get("store_slug") or "").strip()
    name = (body.get("name") or "").strip()
    image_url = (body.get("image_url") or "").strip()
    image_id = (body.get("image_id") or "").strip() or None
    price = _to_float(body.get("price"), 0.0)
    quantity = _to_int(body.get("quantity"), 0)

    if not slug:
        return jsonify({"success": False, "message": "Store slug is required."}), 400
    if not name:
        return jsonify({"success": False, "message": "Product name is required."}), 400
    if not image_url:
        return jsonify({"success": False, "message": "Product image is required."}), 400
    if price <= 0:
        return jsonify({"success": False, "message": "Price must be greater than 0."}), 400
    if quantity < 0:
        return jsonify({"success": False, "message": "Quantity cannot be negative."}), 400

    store_doc = _ensure_store_owned(slug)
    if not store_doc:
        return jsonify({"success": False, "message": "Save the store first (or invalid slug)."}), 400

    doc = {
        "store_slug": slug,
        "owner_id": _owner_id(),
        "name": name,
        "image_url": image_url,
        "cf_image_id": image_id,
        "price": round(float(price), 2),
        "quantity": int(quantity),
        "status": "active",
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
    }
    res = store_products_col.insert_one(doc)
    return jsonify({"success": True, "product": _product_to_client({**doc, "_id": res.inserted_id})})


@stores_bp.route("/api/store-products/<product_id>", methods=["DELETE"])
def api_store_products_delete(product_id: str):
    if not _require_customer_login():
        return jsonify({"success": False, "message": "Login required"}), 401

    try:
        oid = ObjectId(product_id)
    except Exception:
        return jsonify({"success": False, "message": "Invalid product id"}), 400

    slug = (request.args.get("slug") or "").strip()  # optional
    q: Dict[str, Any] = {"_id": oid, "owner_id": _owner_id(), "status": {"$ne": "deleted"}}
    if slug:
        q["store_slug"] = slug

    res = store_products_col.update_one(q, {"$set": {"status": "deleted", "updated_at": datetime.utcnow()}})
    if res.matched_count == 0:
        return jsonify({"success": False, "message": "Product not found"}), 404

    return jsonify({"success": True})
