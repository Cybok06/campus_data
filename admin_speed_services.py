from __future__ import annotations

from datetime import datetime
from ast import literal_eval

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, session, url_for

from Speedlink_db import db as speedlink_db


admin_speed_services_bp = Blueprint("admin_speed_services", __name__)
speed_services_col = speedlink_db["services"]


def _require_admin() -> bool:
    return session.get("role") == "admin"


def _to_float(value):
    try:
        if isinstance(value, str):
            value = value.replace(",", "").strip()
        return float(value)
    except Exception:
        return None


def _format_volume_label(volume, service_name: str | None = None) -> str | None:
    try:
        volume_num = float(volume)
    except Exception:
        return None

    service_name_normalized = (service_name or "").strip().lower()
    if service_name_normalized == "afa talktime":
        if abs(volume_num - round(volume_num)) < 1e-9:
            return f"{int(round(volume_num))} mins"
        return f"{volume_num:.2f} mins"

    if volume_num >= 1000:
        gb = volume_num / 1000
        if abs(gb - round(gb)) < 1e-9:
            return f"{int(round(gb))}GB"
        return f"{gb:.2f}GB"

    if abs(volume_num - round(volume_num)) < 1e-9:
        return f"{int(round(volume_num))}MB"
    return f"{volume_num:.2f}MB"


def _offer_label(offer: dict, fallback_index: int, service_name: str | None = None) -> str:
    value = offer.get("value")
    if isinstance(value, str):
        try:
            parsed = literal_eval(value)
            if isinstance(parsed, dict):
                volume = parsed.get("volume")
                pkg_id = parsed.get("id")
                volume_label = _format_volume_label(volume, service_name)
                if volume_label and pkg_id is not None:
                    return f"{volume_label} (Pkg {pkg_id})"
                if volume_label:
                    return volume_label
        except Exception:
            pass
        if value.strip():
            return value
    return f"Offer {fallback_index}"


def _norm_status_flag(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"open", "1", "true", "on", "yes"}:
        return "OPEN"
    if normalized in {"closed", "0", "false", "off", "no"}:
        return "CLOSED"
    return None


def _norm_availability_flag(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"available", "in_stock", "instock", "1", "true", "on", "yes"}:
        return "AVAILABLE"
    if normalized in {"out_of_stock", "outofstock", "oos", "unavailable", "0", "false", "off", "no"}:
        return "OUT_OF_STOCK"
    return None


@admin_speed_services_bp.route("/admin/speed-services", methods=["GET"])
def manage_speed_services():
    if not _require_admin():
        return redirect(url_for("login.login"))

    services = list(
        speed_services_col.find(
            {},
            {
                "name": 1,
                "image_url": 1,
                "offers": 1,
                "store_offers": 1,
                "provider": 1,
                "status": 1,
                "availability": 1,
                "updated_at": 1,
            },
        ).sort([("_id", -1)])
    )

    for service in services:
        service["_id_str"] = str(service["_id"])
        for key in ("offers", "store_offers"):
            rows = service.get(key) or []
            for index, offer in enumerate(rows, start=1):
                offer["label"] = _offer_label(offer, index, service.get("name"))

    return render_template("admin_speed_services.html", services=services)


@admin_speed_services_bp.route("/admin/speed-services/<service_id>/update-prices", methods=["POST"])
def update_speed_service_prices(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        object_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    service = speed_services_col.find_one({"_id": object_id}, {"offers": 1, "store_offers": 1, "name": 1})
    if not service:
        flash("Speed service not found.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    offers = list(service.get("offers") or [])

    offer_amounts = request.form.getlist("offers_amount[]")

    updated_default = 0

    for index, offer in enumerate(offers):
        if index >= len(offer_amounts):
            continue
        new_amount = _to_float(offer_amounts[index])
        if new_amount is None:
            continue
        offer["amount"] = new_amount
        updated_default += 1

    speed_services_col.update_one(
        {"_id": object_id},
        {
            "$set": {
                "offers": offers,
                "updated_at": datetime.utcnow(),
            }
        },
    )

    flash(
        f"Updated {service.get('name', 'service')} offer prices ({updated_default} offers).",
        "success",
    )
    return redirect(url_for("admin_speed_services.manage_speed_services"))


@admin_speed_services_bp.route("/admin/speed-services/<service_id>/status", methods=["POST"])
def update_speed_service_status(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        object_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    status_value = _norm_status_flag(request.form.get("status"))
    if not status_value:
        flash("Invalid status value.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    result = speed_services_col.update_one(
        {"_id": object_id},
        {"$set": {"status": status_value, "updated_at": datetime.utcnow()}},
    )
    if not result.matched_count:
        flash("Speed service not found.", "danger")
    else:
        flash(f"Service status updated to {status_value}.", "success")
    return redirect(url_for("admin_speed_services.manage_speed_services"))


@admin_speed_services_bp.route("/admin/speed-services/<service_id>/availability", methods=["POST"])
def update_speed_service_availability(service_id):
    if not _require_admin():
        return redirect(url_for("login.login"))

    try:
        object_id = ObjectId(service_id)
    except Exception:
        flash("Invalid service id.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    availability_value = _norm_availability_flag(request.form.get("availability"))
    if not availability_value:
        flash("Invalid availability value.", "danger")
        return redirect(url_for("admin_speed_services.manage_speed_services"))

    result = speed_services_col.update_one(
        {"_id": object_id},
        {"$set": {"availability": availability_value, "updated_at": datetime.utcnow()}},
    )
    if not result.matched_count:
        flash("Speed service not found.", "danger")
    else:
        flash(f"Service availability updated to {availability_value}.", "success")
    return redirect(url_for("admin_speed_services.manage_speed_services"))
