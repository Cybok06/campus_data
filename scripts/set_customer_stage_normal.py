from datetime import datetime

from db import db


def main():
    users_col = db["users"]
    now = datetime.utcnow()

    query = {
        "role": "customer",
        "$or": [
            {"customer_stage": {"$exists": False}},
            {"customer_stage": None},
            {"customer_stage": ""},
        ],
    }

    update = {
        "$set": {
            "customer_stage": "Normal",
            "customer_stage_updated_at": now,
        }
    }

    res = users_col.update_many(query, update)
    print(f"Updated {res.modified_count} customer(s) with customer_stage='Normal'.")


if __name__ == "__main__":
    main()
