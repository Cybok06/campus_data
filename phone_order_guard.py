from datetime import datetime

from db import db


orders_col = db["orders"]
settings_col = db["settings"]

BLOCK_NEW_NUMBERS_KEY = "block_new_numbers"
BLOCK_NEW_NUMBERS_MESSAGE = "@ can't Place Order Now"


def normalize_phone(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits


def is_block_new_numbers_enabled():
    doc = settings_col.find_one({"key": BLOCK_NEW_NUMBERS_KEY}, {"enabled": 1})
    return bool((doc or {}).get("enabled"))


def set_block_new_numbers(enabled, actor_id=None):
    now = datetime.utcnow()
    settings_col.update_one(
        {"key": BLOCK_NEW_NUMBERS_KEY},
        {
            "$set": {
                "key": BLOCK_NEW_NUMBERS_KEY,
                "enabled": bool(enabled),
                "updated_at": now,
                "updated_by": actor_id,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )


def phone_has_prior_order(phone):
    digits = normalize_phone(phone)
    if not digits:
        return False
    return orders_col.find_one({"items.phone": digits}, {"_id": 1}) is not None


def is_phone_allowed(phone):
    digits = normalize_phone(phone)
    if not digits:
        return False, BLOCK_NEW_NUMBERS_MESSAGE, False
    if not is_block_new_numbers_enabled():
        return True, "", True
    has_history = phone_has_prior_order(digits)
    if has_history:
        return True, "", True
    return False, BLOCK_NEW_NUMBERS_MESSAGE, False


def first_blocked_phone(phones):
    seen = set()
    for raw in phones or []:
        phone = normalize_phone(raw)
        if not phone or phone in seen:
            continue
        seen.add(phone)
        allowed, message, has_history = is_phone_allowed(phone)
        if not allowed:
            return {
                "phone": phone,
                "message": message or BLOCK_NEW_NUMBERS_MESSAGE,
                "has_history": has_history,
            }
    return None
