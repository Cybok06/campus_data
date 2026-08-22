from io import BytesIO
from math import ceil
from datetime import datetime, timedelta
from urllib.parse import urlencode

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for
from pymongo import ASCENDING, DESCENDING
from bson.regex import Regex
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from db import db
from phone_order_guard import is_block_new_numbers_enabled, set_block_new_numbers


admin_phone_numbers_bp = Blueprint("admin_phone_numbers", __name__)

orders_col = db["orders"]
services_col = db["services"]

PER_PAGE = 20


def _require_admin():
    return session.get("role") == "admin"


def _normalize_phone(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits or None


def _normalize_service(raw):
    return str(raw or "").strip()


def _normalize_network(raw):
    value = str(raw or "").strip().upper()
    if not value:
        return ""
    aliases = {
        "VODAFONE": "TELECEL",
        "AT": "AIRTELTIGO",
        "AIRTELTIGO": "AIRTELTIGO",
        "AIRTEL TIGO": "AIRTELTIGO",
        "TIGO": "AIRTELTIGO",
    }
    return aliases.get(value, value)


def _infer_network_from_service(service_name):
    name = str(service_name or "").strip().upper()
    if "MTN" in name:
        return "MTN"
    if "TELECEL" in name or "VODAFONE" in name:
        return "TELECEL"
    if "AIRTELTIGO" in name or "AIRTEL TIGO" in name or "AT" == name:
        return "AIRTELTIGO"
    return ""


def _extract_network(item):
    candidates = (
        item.get("network_name"),
        item.get("network"),
        item.get("provider_network"),
        item.get("ported_expected_network"),
        item.get("ported_detected_network"),
    )
    for candidate in candidates:
        normalized = _normalize_network(candidate)
        if normalized:
            return normalized
    return _infer_network_from_service(item.get("serviceName"))


def _ensure_indexes():
    try:
        orders_col.create_index([("created_at", DESCENDING)])
        orders_col.create_index([("items.phone", ASCENDING)])
        orders_col.create_index([("items.serviceName", ASCENDING)])
        orders_col.create_index([("items.network", ASCENDING)])
        orders_col.create_index([("items.network_name", ASCENDING)])
        orders_col.create_index([("items.provider_network", ASCENDING)])
    except Exception:
        pass


def _dt_label(value):
    if not value:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    return str(value)


def _parse_ymd(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except Exception:
        return None


def _network_expr():
    return {
        "$let": {
            "vars": {
                "raw": {
                    "$ifNull": [
                        "$items.network_name",
                        {
                            "$ifNull": [
                                "$items.network",
                                {
                                    "$ifNull": [
                                        "$items.provider_network",
                                        {
                                            "$ifNull": [
                                                "$items.ported_expected_network",
                                                "$items.ported_detected_network",
                                            ]
                                        },
                                    ]
                                },
                            ]
                        },
                    ]
                }
            },
            "in": {
                "$switch": {
                    "branches": [
                        {"case": {"$eq": [{"$toUpper": {"$ifNull": ["$$raw", ""]}}, "VODAFONE"]}, "then": "TELECEL"},
                        {"case": {"$eq": [{"$toUpper": {"$ifNull": ["$$raw", ""]}}, "AIRTEL TIGO"]}, "then": "AIRTELTIGO"},
                        {"case": {"$eq": [{"$toUpper": {"$ifNull": ["$$raw", ""]}}, "AT"]}, "then": "AIRTELTIGO"},
                        {"case": {"$eq": [{"$toUpper": {"$ifNull": ["$$raw", ""]}}, "TIGO"]}, "then": "AIRTELTIGO"},
                    ],
                    "default": {"$toUpper": {"$ifNull": ["$$raw", ""]}},
                }
            },
        }
    }


def _base_line_pipeline(service_filter="", network_filter="", search=""):
    service_filter = _normalize_service(service_filter)
    network_filter = _normalize_network(network_filter)
    search = (search or "").strip()

    pipeline = [
        {"$match": {"items.0": {"$exists": True}}},
        {"$unwind": "$items"},
        {
            "$project": {
                "_id": 0,
                "phone": {"$trim": {"input": {"$ifNull": ["$items.phone", ""]}}},
                "service_name": {"$trim": {"input": {"$ifNull": ["$items.serviceName", "Unknown"]}}},
                "network_name": _network_expr(),
                "order_id": {"$ifNull": ["$order_id", ""]},
                "created_at": "$created_at",
            }
        },
        {"$match": {"phone": {"$ne": ""}}},
    ]

    filters = []
    if service_filter:
        filters.append({"service_name": service_filter})
    if network_filter:
        filters.append({"network_name": network_filter})
    if search:
        regex = {"$regex": search, "$options": "i"}
        filters.append({
            "$or": [
                {"phone": regex},
                {"service_name": regex},
                {"network_name": regex},
                {"order_id": regex},
            ]
        })
    if filters:
        pipeline.append({"$match": {"$and": filters}})

    return pipeline


def _normalize_grouped_row(row):
    services = sorted([s for s in (row.get("services") or []) if s], key=lambda x: x.lower())
    networks = sorted([n for n in (row.get("networks") or []) if n])
    return {
        "phone": row.get("phone") or row.get("_id") or "",
        "services": services,
        "services_label": ", ".join(services),
        "services_count": len(services),
        "networks": networks,
        "networks_label": ", ".join(networks),
        "networks_count": len(networks),
        "order_count": int(row.get("order_count") or 0),
        "line_count": int(row.get("line_count") or 0),
        "first_order_at": row.get("first_order_at"),
        "last_order_at": row.get("last_order_at"),
        "first_order_at_label": _dt_label(row.get("first_order_at")),
        "last_order_at_label": _dt_label(row.get("last_order_at")),
        "latest_order_id": row.get("latest_order_id") or "-",
    }


def _fetch_phone_page(service_filter="", network_filter="", search="", page=1, per_page=PER_PAGE):
    skip = max(page - 1, 0) * per_page
    pipeline = _base_line_pipeline(service_filter, network_filter, search) + [
        {"$sort": {"created_at": -1, "order_id": -1}},
        {
            "$group": {
                "_id": "$phone",
                "phone": {"$first": "$phone"},
                "latest_order_id": {"$first": "$order_id"},
                "last_order_at": {"$first": "$created_at"},
                "first_order_at": {"$min": "$created_at"},
                "services": {"$addToSet": "$service_name"},
                "networks": {"$addToSet": "$network_name"},
                "order_ids": {"$addToSet": "$order_id"},
                "line_count": {"$sum": 1},
            }
        },
        {"$addFields": {"order_count": {"$size": "$order_ids"}}},
        {"$sort": {"last_order_at": -1, "phone": 1}},
        {
            "$facet": {
                "data": [{"$skip": skip}, {"$limit": per_page}],
                "meta": [{"$count": "total_rows"}],
            }
        },
    ]

    result = list(orders_col.aggregate(pipeline, allowDiskUse=True))
    bucket = result[0] if result else {"data": [], "meta": []}
    total_rows = int(((bucket.get("meta") or [{}])[0]).get("total_rows") or 0)
    rows = [_normalize_grouped_row(row) for row in (bucket.get("data") or [])]
    return rows, total_rows


def _fetch_phone_summary(service_filter="", network_filter="", search=""):
    pipeline = _base_line_pipeline(service_filter, network_filter, search) + [
        {
            "$group": {
                "_id": None,
                "phones": {"$addToSet": "$phone"},
                "order_ids": {"$addToSet": "$order_id"},
                "total_matching_lines": {"$sum": 1},
            }
        },
        {
            "$project": {
                "_id": 0,
                "total_phone_numbers": {"$size": "$phones"},
                "total_matching_orders": {"$size": "$order_ids"},
                "total_matching_lines": 1,
            }
        },
    ]
    result = list(orders_col.aggregate(pipeline, allowDiskUse=True))
    if result:
        return result[0]
    return {
        "total_phone_numbers": 0,
        "total_matching_orders": 0,
        "total_matching_lines": 0,
    }


def _fetch_all_phone_rows(service_filter="", network_filter="", search=""):
    pipeline = _base_line_pipeline(service_filter, network_filter, search) + [
        {"$sort": {"created_at": -1, "order_id": -1}},
        {
            "$group": {
                "_id": "$phone",
                "phone": {"$first": "$phone"},
                "latest_order_id": {"$first": "$order_id"},
                "last_order_at": {"$first": "$created_at"},
                "first_order_at": {"$min": "$created_at"},
                "services": {"$addToSet": "$service_name"},
                "networks": {"$addToSet": "$network_name"},
                "order_ids": {"$addToSet": "$order_id"},
                "line_count": {"$sum": 1},
            }
        },
        {"$addFields": {"order_count": {"$size": "$order_ids"}}},
        {"$sort": {"last_order_at": -1, "phone": 1}},
    ]
    return [_normalize_grouped_row(row) for row in orders_col.aggregate(pipeline, allowDiskUse=True)]


def _new_phone_group_pipeline(search="", start_date=None, end_date=None):
    search = (search or "").strip()
    pipeline = [
        {"$match": {"items.0": {"$exists": True}}},
        {"$unwind": "$items"},
        {
            "$project": {
                "_id": 0,
                "phone": {"$trim": {"input": {"$ifNull": ["$items.phone", ""]}}},
                "service_name": {"$trim": {"input": {"$ifNull": ["$items.serviceName", "Unknown"]}}},
                "network_name": _network_expr(),
                "order_id": {"$ifNull": ["$order_id", ""]},
                "created_at": "$created_at",
            }
        },
        {"$match": {"phone": {"$ne": ""}}},
        {"$sort": {"created_at": 1, "order_id": 1}},
        {
            "$group": {
                "_id": "$phone",
                "phone": {"$first": "$phone"},
                "first_order_at": {"$first": "$created_at"},
                "first_order_id": {"$first": "$order_id"},
                "first_service": {"$first": "$service_name"},
                "first_network": {"$first": "$network_name"},
                "services": {"$addToSet": "$service_name"},
                "networks": {"$addToSet": "$network_name"},
                "order_ids": {"$addToSet": "$order_id"},
                "line_count": {"$sum": 1},
                "last_order_at": {"$max": "$created_at"},
            }
        },
        {"$addFields": {"order_count": {"$size": "$order_ids"}}},
    ]

    filters = []
    if start_date or end_date:
        dt_filter = {}
        if start_date:
            dt_filter["$gte"] = start_date
        if end_date:
            dt_filter["$lt"] = end_date
        filters.append({"first_order_at": dt_filter})
    if search:
        rx = Regex(search, "i")
        filters.append({
            "$or": [
                {"phone": rx},
                {"first_service": rx},
                {"first_network": rx},
                {"first_order_id": rx},
            ]
        })
    if filters:
        pipeline.append({"$match": {"$and": filters}})
    return pipeline


def _normalize_new_phone_row(row):
    services = sorted([s for s in (row.get("services") or []) if s], key=lambda x: x.lower())
    networks = sorted([n for n in (row.get("networks") or []) if n])
    return {
        "phone": row.get("phone") or row.get("_id") or "",
        "first_order_at": row.get("first_order_at"),
        "first_order_at_label": _dt_label(row.get("first_order_at")),
        "first_order_id": row.get("first_order_id") or "-",
        "first_service": row.get("first_service") or "Unknown",
        "first_network": row.get("first_network") or "Unknown",
        "services": services,
        "services_label": ", ".join(services),
        "networks": networks,
        "networks_label": ", ".join(networks),
        "order_count": int(row.get("order_count") or 0),
        "line_count": int(row.get("line_count") or 0),
        "last_order_at": row.get("last_order_at"),
        "last_order_at_label": _dt_label(row.get("last_order_at")),
    }


def _fetch_new_phone_page(search="", start_date=None, end_date=None, page=1, per_page=PER_PAGE):
    skip = max(page - 1, 0) * per_page
    pipeline = _new_phone_group_pipeline(search=search, start_date=start_date, end_date=end_date) + [
        {"$sort": {"first_order_at": -1, "phone": 1}},
        {
            "$facet": {
                "data": [{"$skip": skip}, {"$limit": per_page}],
                "meta": [{"$count": "total_rows"}],
            }
        },
    ]
    result = list(orders_col.aggregate(pipeline, allowDiskUse=True))
    bucket = result[0] if result else {"data": [], "meta": []}
    total_rows = int(((bucket.get("meta") or [{}])[0]).get("total_rows") or 0)
    rows = [_normalize_new_phone_row(row) for row in (bucket.get("data") or [])]
    return rows, total_rows


def _fetch_new_phone_summary(search="", start_date=None, end_date=None):
    pipeline = _new_phone_group_pipeline(search=search, start_date=start_date, end_date=end_date) + [
        {
            "$group": {
                "_id": None,
                "new_numbers_count": {"$sum": 1},
                "total_orders": {"$sum": "$order_count"},
                "total_lines": {"$sum": "$line_count"},
            }
        },
        {"$project": {"_id": 0, "new_numbers_count": 1, "total_orders": 1, "total_lines": 1}},
    ]
    result = list(orders_col.aggregate(pipeline, allowDiskUse=True))
    if result:
        return result[0]
    return {"new_numbers_count": 0, "total_orders": 0, "total_lines": 0}


def _fetch_all_new_phone_rows(search="", start_date=None, end_date=None):
    pipeline = _new_phone_group_pipeline(search=search, start_date=start_date, end_date=end_date) + [
        {"$sort": {"first_order_at": -1, "phone": 1}},
    ]
    return [_normalize_new_phone_row(row) for row in orders_col.aggregate(pipeline, allowDiskUse=True)]


def _service_options():
    names = set()
    try:
        for doc in services_col.find({}, {"name": 1}):
            name = _normalize_service(doc.get("name"))
            if name:
                names.add(name)
    except Exception:
        pass
    return sorted(names, key=lambda x: x.lower())


def _network_options():
    names = set()
    cursor = orders_col.find(
        {"items": {"$exists": True, "$ne": []}},
        {"items.network_name": 1, "items.network": 1, "items.provider_network": 1, "items.ported_expected_network": 1, "items.ported_detected_network": 1, "items.serviceName": 1},
    )
    for order in cursor:
        for item in (order.get("items") or []):
            network_name = _extract_network(item)
            if network_name:
                names.add(network_name)
    return sorted(names)


def _render_excel(rows, title, is_new_view=False):

    html = ["<html><head><meta charset='utf-8'></head><body>", f"<h2>{title}</h2>", "<table border='1'>"]
    if is_new_view:
        html.append(
            "<tr><th>Phone Number</th><th>First Network</th><th>First Service</th><th>First Order Date</th><th>First Order ID</th><th>Total Orders</th><th>Total Lines</th><th>Last Order</th></tr>"
        )
        for row in rows:
            html.append(
                "<tr>"
                f"<td>{row['phone']}</td>"
                f"<td>{row['first_network']}</td>"
                f"<td>{row['first_service']}</td>"
                f"<td>{row['first_order_at_label']}</td>"
                f"<td>{row['first_order_id']}</td>"
                f"<td>{row['order_count']}</td>"
                f"<td>{row['line_count']}</td>"
                f"<td>{row['last_order_at_label']}</td>"
                "</tr>"
            )
    else:
        html.append(
            "<tr><th>Phone Number</th><th>Networks</th><th>Services</th><th>Service Count</th><th>Orders</th><th>Order Lines</th><th>First Order</th><th>Last Order</th><th>Latest Order ID</th></tr>"
        )
        for row in rows:
            html.append(
                "<tr>"
                f"<td>{row['phone']}</td>"
                f"<td>{row['networks_label']}</td>"
                f"<td>{row['services_label']}</td>"
                f"<td>{row['services_count']}</td>"
                f"<td>{row['order_count']}</td>"
                f"<td>{row['line_count']}</td>"
                f"<td>{row['first_order_at_label']}</td>"
                f"<td>{row['last_order_at_label']}</td>"
                f"<td>{row['latest_order_id']}</td>"
                "</tr>"
            )
    html.append("</table></body></html>")
    return "".join(html)


def _render_pdf(rows, title, subtitle_lines, is_new_view=False):
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=20,
        rightMargin=20,
        topMargin=20,
        bottomMargin=20,
    )

    styles = getSampleStyleSheet()
    story = [Paragraph(title, styles["Title"])]
    for line in subtitle_lines:
        story.append(Paragraph(line, styles["Normal"]))
    story.append(Spacer(1, 12))
    if is_new_view:
        table_data = [[
            "Phone",
            "First Network",
            "First Service",
            "First Order Date",
            "First Order ID",
            "Orders",
            "Lines",
        ]]
        for row in rows:
            table_data.append([
                row["phone"],
                row["first_network"] or "-",
                row["first_service"] or "-",
                row["first_order_at_label"],
                row["first_order_id"],
                str(row["order_count"]),
                str(row["line_count"]),
            ])
        table = Table(
            table_data,
            repeatRows=1,
            colWidths=[90, 85, 160, 100, 110, 55, 55],
        )
    else:
        table_data = [[
            "Phone",
            "Networks",
            "Services",
            "Svc Cnt",
            "Orders",
            "Lines",
            "First Order",
            "Last Order",
        ]]
        for row in rows:
            table_data.append([
                row["phone"],
                row["networks_label"] or "-",
                row["services_label"] or "-",
                str(row["services_count"]),
                str(row["order_count"]),
                str(row["line_count"]),
                row["first_order_at_label"],
                row["last_order_at_label"],
            ])
        table = Table(
            table_data,
            repeatRows=1,
            colWidths=[80, 85, 150, 55, 50, 50, 95, 95],
        )
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0d6efd")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d0d7de")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f6f8fa")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(table)
    doc.build(story)
    buffer.seek(0)
    return buffer


@admin_phone_numbers_bp.route("/admin/phone-numbers")
def phone_numbers_page():
    if not _require_admin():
        return redirect(url_for("login.login"))

    view = (request.args.get("view") or "unique").strip().lower()
    if view not in {"unique", "new"}:
        view = "unique"

    service_filter = (request.args.get("service") or "").strip()
    network_filter = (request.args.get("network") or "").strip()
    search = (request.args.get("q") or "").strip()
    page = max(int(request.args.get("page", 1) or 1), 1)
    date_mode = (request.args.get("date_mode") or "all").strip().lower()
    if date_mode not in {"all", "custom"}:
        date_mode = "all"
    start_date_str = (request.args.get("start_date") or "").strip()
    end_date_str = (request.args.get("end_date") or "").strip()
    start_date = _parse_ymd(start_date_str) if date_mode == "custom" else None
    end_date = _parse_ymd(end_date_str) if date_mode == "custom" else None
    if end_date:
        end_date = end_date.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    if view == "new":
        rows, total_rows = _fetch_new_phone_page(
            search=search,
            start_date=start_date,
            end_date=end_date,
            page=page,
            per_page=PER_PAGE,
        )
        summary = _fetch_new_phone_summary(
            search=search,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        rows, total_rows = _fetch_phone_page(
            service_filter=service_filter,
            network_filter=network_filter,
            search=search,
            page=page,
            per_page=PER_PAGE,
        )
        summary = _fetch_phone_summary(
            service_filter=service_filter,
            network_filter=network_filter,
            search=search,
        )

    total_pages = max(ceil(total_rows / PER_PAGE), 1)
    page = min(page, total_pages)

    qs = request.args.to_dict(flat=True)
    qs.pop("page", None)

    return render_template(
        "admin_phone_numbers.html",
        view=view,
        phone_rows=rows,
        total_rows=total_rows,
        total_pages=total_pages,
        page=page,
        per_page=PER_PAGE,
        service_filter=service_filter,
        network_filter=network_filter,
        search_q=search,
        service_options=_service_options(),
        network_options=_network_options(),
        summary=summary,
        block_new_numbers_enabled=is_block_new_numbers_enabled(),
        date_mode=date_mode,
        start_date=start_date_str,
        end_date=end_date_str,
        base_qs=urlencode({k: v for k, v in qs.items() if v}),
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/export")
def export_phone_numbers():
    if not _require_admin():
        return redirect(url_for("login.login"))

    export_format = (request.args.get("format") or "excel").strip().lower()
    view = (request.args.get("view") or "unique").strip().lower()
    if view not in {"unique", "new"}:
        view = "unique"
    export_scope = (request.args.get("scope") or "all").strip().lower()
    scope_value = (request.args.get("scope_value") or "").strip()
    service_filter = (request.args.get("service") or "").strip()
    network_filter = (request.args.get("network") or "").strip()
    search = (request.args.get("q") or "").strip()
    date_mode = (request.args.get("date_mode") or "all").strip().lower()
    start_date_str = (request.args.get("start_date") or "").strip()
    end_date_str = (request.args.get("end_date") or "").strip()
    start_date = _parse_ymd(start_date_str) if date_mode == "custom" else None
    end_date = _parse_ymd(end_date_str) if date_mode == "custom" else None
    if end_date:
        end_date = end_date + timedelta(days=1)

    if view == "unique":
        if export_scope == "service":
            service_filter = scope_value or service_filter
            network_filter = ""
        elif export_scope == "network":
            network_filter = scope_value or network_filter
            service_filter = ""
        else:
            service_filter = ""
            network_filter = ""

        rows = _fetch_all_phone_rows(
            service_filter=service_filter,
            network_filter=network_filter,
            search=search,
        )
        title = "Unique Phone Numbers Report"
        subtitles = []
        if service_filter:
            subtitles.append(f"Service Filter: {service_filter}")
        if network_filter:
            subtitles.append(f"Network Filter: {network_filter}")
    else:
        rows = _fetch_all_new_phone_rows(
            search=search,
            start_date=start_date,
            end_date=end_date,
        )
        title = "New Phone Numbers Report"
        subtitles = []
        if date_mode == "custom" and start_date_str and end_date_str:
            subtitles.append(f"Date Range: {start_date_str} to {end_date_str}")
        else:
            subtitles.append("Date Range: All time")

    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    if export_format == "pdf":
        pdf_buffer = _render_pdf(rows, title, subtitles, is_new_view=(view == "new"))
        return Response(
            pdf_buffer.getvalue(),
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f"attachment; filename=phone_numbers_{stamp}.pdf",
            },
        )

    excel_html = _render_excel(rows, title, is_new_view=(view == "new"))
    return Response(
        excel_html,
        mimetype="application/vnd.ms-excel",
        headers={
            "Content-Disposition": f"attachment; filename=phone_numbers_{stamp}.xls",
        },
    )


@admin_phone_numbers_bp.route("/admin/phone-numbers/block-new-numbers", methods=["POST"])
def toggle_block_new_numbers():
    if not _require_admin():
        return jsonify({"success": False, "message": "Unauthorized"}), 403

    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get("enabled"))
    set_block_new_numbers(enabled, actor_id=session.get("user_id"))
    return jsonify({"success": True, "enabled": enabled})


_ensure_indexes()
