from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple
import json
import re
import ast

from db import db
from pymongo import ReturnDocument

provider_accounts_col = db["provider_accounts"]
provider_transactions_col = db["provider_transactions"]

PROVIDER_WALLET_KEY = "provider_wallet"
PROVIDER_WALLET_LABEL = "Provider Wallet"

_INDEXES_READY = False

_MB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*MB\s*$", re.I)
_GB_RE = re.compile(r"^\s*([\d,]+(?:\.\d+)?)\s*G(?:B|IG)?\s*$", re.I)
_INT_RE = re.compile(r"^\s*[\d,]+\s*$")


def jlog(event: str, **kv):
    rec = {"evt": event, **kv}
    try:
        print(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    except Exception:
        print(f"[LOG_FALLBACK] {event} {kv}")


def _ensure_indexes() -> None:
    global _INDEXES_READY
    if _INDEXES_READY:
        return
    try:
        provider_accounts_col.create_index("provider", unique=True)
    except Exception:
        pass
    try:
        provider_transactions_col.create_index("dedupe_key", unique=True, sparse=True)
    except Exception:
        pass
    try:
        provider_transactions_col.create_index([("provider", 1), ("created_at", -1)])
    except Exception:
        pass
    _INDEXES_READY = True


def _wallet_provider_key(provider: str) -> tuple[str, Optional[str]]:
    raw = (provider or "").strip().lower()
    if not raw or raw == PROVIDER_WALLET_KEY:
        return PROVIDER_WALLET_KEY, None
    return PROVIDER_WALLET_KEY, raw


def ensure_provider_account(provider: str) -> None:
    _ensure_indexes()
    provider, _orig = _wallet_provider_key(provider)
    now = datetime.utcnow()
    try:
        provider_accounts_col.update_one(
            {"provider": provider},
            {
                "$setOnInsert": {
                    "provider": provider,
                    "balance": 0.0,
                    "currency": "GHS",
                    "created_at": now,
                },
                "$set": {"updated_at": now, "currency": "GHS"},
            },
            upsert=True,
        )
    except Exception:
        pass


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return float(default)


def _to_int(v: Any) -> Optional[int]:
    try:
        if isinstance(v, str):
            v = v.replace(",", "").strip()
        return int(float(v))
    except Exception:
        return None


def _coerce_value_obj(v: Any) -> Dict[str, Any]:
    if isinstance(v, dict):
        return v
    if v is None:
        return {}
    s = str(v).strip()
    if s.startswith("{") and s.endswith("}"):
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                return d
        except Exception:
            try:
                d = ast.literal_eval(s)
                if isinstance(d, dict):
                    return d
            except Exception:
                pass
    return {}


def _parse_volume_to_mb(v: Any) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(round(float(v)))
    txt = str(v).strip()

    m = _MB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val))

    m = _GB_RE.match(txt)
    if m:
        val = float(m.group(1).replace(",", ""))
        return int(round(val * 1000))

    if _INT_RE.match(txt):
        return int(txt.replace(",", ""))

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "volume" in as_json:
                return _to_int(as_json["volume"])
    except Exception:
        pass

    try:
        d = ast.literal_eval(txt)
        if isinstance(d, dict) and "volume" in d:
            return _to_int(d["volume"])
    except Exception:
        pass

    return None


def _extract_pkg_id(value_raw: Any) -> Optional[int]:
    if value_raw is None:
        return None
    if isinstance(value_raw, (int, float)):
        return _to_int(value_raw)

    txt = str(value_raw).strip()
    if _INT_RE.match(txt):
        return _to_int(txt)

    try:
        if txt.startswith("{") and txt.endswith("}"):
            as_json = json.loads(txt)
            if isinstance(as_json, dict) and "id" in as_json:
                return _to_int(as_json["id"])
    except Exception:
        pass

    try:
        d = ast.literal_eval(txt)
        if isinstance(d, dict) and "id" in d:
            return _to_int(d["id"])
    except Exception:
        pass

    return None


def _offer_id_and_volume(value_raw: Any) -> Tuple[Optional[int], Optional[int]]:
    if isinstance(value_raw, dict):
        return _to_int(value_raw.get("id")), _to_int(value_raw.get("volume"))

    if isinstance(value_raw, str):
        val = value_raw.strip()
        if val.startswith("{") and val.endswith("}"):
            try:
                d = json.loads(val)
                if isinstance(d, dict):
                    return _to_int(d.get("id")), _to_int(d.get("volume"))
            except Exception:
                try:
                    d = ast.literal_eval(val)
                    if isinstance(d, dict):
                        return _to_int(d.get("id")), _to_int(d.get("volume"))
                except Exception:
                    pass

    return _extract_pkg_id(value_raw), _parse_volume_to_mb(value_raw)


def compute_provider_cost(service_doc: Dict[str, Any], selected_value: Any) -> Optional[float]:
    offers = service_doc.get("offers") or []
    if not isinstance(offers, list) or not offers:
        return None

    value_obj = _coerce_value_obj(selected_value)
    sel_id = _to_int(value_obj.get("id")) if value_obj else None
    sel_vol = _to_int(value_obj.get("volume")) if value_obj else None

    if sel_id is None:
        sel_id = _extract_pkg_id(selected_value)
    if sel_vol is None:
        sel_vol = _parse_volume_to_mb(selected_value)

    best_idx = None
    best_diff = None

    # First pass: match id + volume
    for idx, of in enumerate(offers):
        of_val = of.get("value")
        of_id, of_vol = _offer_id_and_volume(of_val)
        if sel_id is not None and sel_vol is not None:
            if of_id == sel_id and of_vol == sel_vol:
                return _to_float(of.get("amount"), None)

    # Second: match id
    if sel_id is not None:
        for idx, of in enumerate(offers):
            of_id, _of_vol = _offer_id_and_volume(of.get("value"))
            if of_id == sel_id:
                return _to_float(of.get("amount"), None)

    # Third: match exact volume
    if sel_vol is not None:
        for idx, of in enumerate(offers):
            _of_id, of_vol = _offer_id_and_volume(of.get("value"))
            if of_vol == sel_vol:
                return _to_float(of.get("amount"), None)

    # Fourth: match raw value equality
    for idx, of in enumerate(offers):
        if selected_value is not None and of.get("value") == selected_value:
            return _to_float(of.get("amount"), None)

    # Fallback: closest volume
    if sel_vol is not None:
        for idx, of in enumerate(offers):
            _of_id, of_vol = _offer_id_and_volume(of.get("value"))
            if of_vol is None:
                continue
            diff = abs(int(of_vol) - int(sel_vol))
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_idx = idx

    if best_idx is not None:
        return _to_float((offers[best_idx] or {}).get("amount"), None)

    # If only one offer, use it
    if len(offers) == 1:
        return _to_float((offers[0] or {}).get("amount"), None)

    return None


def debit_provider(
    provider: str,
    amount: float,
    order_id: str,
    line_index: int,
    meta: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str, Optional[float]]:
    _ensure_indexes()
    provider, original_provider = _wallet_provider_key(provider)
    amount_f = round(_to_float(amount, 0.0), 2)
    if not provider or amount_f <= 0:
        return False, "Invalid provider/amount", None

    ensure_provider_account(provider)

    dedupe_key = f"{order_id}:{line_index}:{original_provider or provider}:{amount_f:.2f}"
    existing = provider_transactions_col.find_one({"dedupe_key": dedupe_key}, {"_id": 1})
    if existing:
        acc = provider_accounts_col.find_one({"provider": provider}, {"balance": 1}) or {}
        return True, "idempotent", _to_float(acc.get("balance"), 0.0)

    jlog("provider_debit_attempt", provider=provider, amount=amount_f, order_id=order_id, line_index=line_index)

    now = datetime.utcnow()
    doc = provider_accounts_col.find_one_and_update(
        {"provider": provider, "balance": {"$gte": amount_f}},
        {"$inc": {"balance": -amount_f}, "$set": {"updated_at": now}},
        return_document=ReturnDocument.AFTER,
    )

    if not doc:
        jlog("provider_debit_failed_insufficient", provider=provider, amount=amount_f, order_id=order_id, line_index=line_index)
        return False, "Insufficient provider balance", None

    meta_doc = dict(meta or {})
    if original_provider:
        meta_doc.setdefault("service_provider", original_provider)

    try:
        provider_transactions_col.insert_one(
            {
                "provider": provider,
                "amount": amount_f,
                "direction": "DEBIT",
                "reason": "ORDER_RESERVE",
                "order_id": order_id,
                "reference": order_id,
                "line_index": line_index,
                "dedupe_key": dedupe_key,
                "created_at": now,
                "meta": meta_doc,
            }
        )
    except Exception:
        # rollback if insert fails for any reason
        provider_accounts_col.update_one(
            {"provider": provider},
            {"$inc": {"balance": amount_f}, "$set": {"updated_at": datetime.utcnow()}},
        )
        existing2 = provider_transactions_col.find_one({"dedupe_key": dedupe_key}, {"_id": 1})
        if existing2:
            acc = provider_accounts_col.find_one({"provider": provider}, {"balance": 1}) or {}
            return True, "idempotent", _to_float(acc.get("balance"), 0.0)
        return False, "Failed to record provider debit", None

    jlog("provider_debit_success", provider=provider, amount=amount_f, order_id=order_id, line_index=line_index)
    return True, "ok", _to_float((doc or {}).get("balance"), 0.0)


def credit_provider(
    provider: str,
    amount: float,
    order_id: Optional[str],
    line_index: Optional[int],
    reason: str = "ROLLBACK",
    meta: Optional[Dict[str, Any]] = None,
    dedupe_key: Optional[str] = None,
) -> Tuple[bool, str, Optional[float]]:
    _ensure_indexes()
    provider, original_provider = _wallet_provider_key(provider)
    amount_f = round(_to_float(amount, 0.0), 2)
    if not provider or amount_f <= 0:
        return False, "Invalid provider/amount", None

    ensure_provider_account(provider)

    dk = dedupe_key or f"{order_id}:{line_index}:{original_provider or provider}:{reason}:{amount_f:.2f}"
    existing = provider_transactions_col.find_one({"dedupe_key": dk}, {"_id": 1})
    if existing:
        acc = provider_accounts_col.find_one({"provider": provider}, {"balance": 1}) or {}
        return True, "idempotent", _to_float(acc.get("balance"), 0.0)

    now = datetime.utcnow()
    provider_accounts_col.update_one(
        {"provider": provider},
        {"$inc": {"balance": amount_f}, "$set": {"updated_at": now}, "$setOnInsert": {"created_at": now}},
        upsert=True,
    )

    meta_doc = dict(meta or {})
    if original_provider:
        meta_doc.setdefault("service_provider", original_provider)

    try:
        ref = None
        if isinstance(meta, dict) and meta.get("reference"):
            ref = str(meta.get("reference"))
        provider_transactions_col.insert_one(
            {
                "provider": provider,
                "amount": amount_f,
                "direction": "CREDIT",
                "reason": reason,
                "order_id": order_id,
                "reference": ref or (order_id if order_id else None),
                "line_index": line_index,
                "dedupe_key": dk,
                "created_at": now,
                "meta": meta_doc,
            }
        )
    except Exception:
        pass

    jlog("provider_debit_rollback_success" if reason == "ROLLBACK" else "provider_credit_success", provider=provider, amount=amount_f, order_id=order_id, line_index=line_index, reason=reason)
    acc = provider_accounts_col.find_one({"provider": provider}, {"balance": 1}) or {}
    return True, "ok", _to_float(acc.get("balance"), 0.0)
