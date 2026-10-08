"""Purchase-time store margins and atomic, payment-deduplicated profit credits."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def currency(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        if amount.is_finite() and amount >= 0:
            return amount
    except (InvalidOperation, TypeError, ValueError):
        pass
    raise ValueError('Store price is missing or invalid.')


def store_margin(selling, base):
    selling, base = currency(selling), currency(base)
    if selling < base:
        raise ValueError('Store selling price cannot be below the price given to the store.')
    return selling - base


def prepare_store_profit_items(items, *, paid):
    """Normalize every provider path, including skipped and zero-margin lines."""
    revised = []
    total = Decimal(0)
    for item in items:
        item = dict(item)
        skipped = str(item.get('line_status') or '').startswith('skipped')
        margin = store_margin(item.get('amount'), item.get('base_amount')) if paid and not skipped else Decimal(0)
        item['store_profit_amount'] = float(margin)
        item['store_profit_basis'] = 'selling_price_less_store_base' if paid and not skipped else 'not_earned'
        total += margin
        revised.append(item)
    return revised, float(total)


def order_store_profit(order):
    """Recorded agent profit; system margin is never an agent-profit fallback."""
    return float(sum((currency(item.get('store_profit_amount') or 0)
                      for item in order.get('items') or []
                      if not str(item.get('line_status') or '').startswith('skipped')), Decimal(0)))


def store_profit_expression():
    """The same recorded-profit definition for Mongo dashboard aggregates."""
    profits = {'$map': {'input': {'$ifNull': ['$items', []]}, 'as': 'it',
        'in': {'$cond': [
            {'$regexMatch': {'input': {'$ifNull': ['$$it.line_status', '']}, 'regex': '^skipped'}},
            0, {'$ifNull': ['$$it.store_profit_amount', 0]},
        ]}}}
    return {'$let': {'vars': {'profits': profits}, 'in': {'$sum': '$$profits'}}}


def persist_store_purchase(database, documents, *, paid, reference):
    """Store orders, profit receipt and account increment succeed together."""
    if not documents:
        raise ValueError('No store order items to save.')
    slug = documents[0]['store_slug']
    if any(document.get('store_slug') != slug for document in documents):
        raise ValueError('Store order items belong to different stores.')
    if paid and not reference:
        raise ValueError('Verified payment reference is required for store profit credit.')
    receipt_id = f'STORE:{slug}:PAYMENT:{reference}'
    # _id also ensures only one account can be created during concurrent sales.
    account_id = f'STORE:{slug}'

    def commit(session):
        orders = database['orders']
        if paid:
            receipt = database['store_profit_credits'].find_one({'_id': receipt_id}, session=session)
            if receipt:
                existing = list(orders.find({'_id': {'$in': receipt['order_db_ids']}}, session=session))
                return sorted(existing, key=lambda doc: doc.get('batch_position', 0)), False
            # Do not retroactively repair historical purchases through retries.
            existing = list(orders.find({'store_slug': slug, 'paystack_reference': reference}, session=session))
            if existing:
                return sorted(existing, key=lambda doc: doc.get('batch_position', 0)), False
        total = Decimal(0)
        for document in documents:
            document['items'], line_total = prepare_store_profit_items(document['items'], paid=paid)
            document['store_profit_amount_total'] = line_total
            total += currency(line_total)
            orders.insert_one(document, session=session)
        if paid:
            now = datetime.utcnow()
            database['store_profit_credits'].insert_one({
                '_id': receipt_id, 'store_slug': slug, 'paystack_reference': reference,
                'order_db_ids': [document['_id'] for document in documents],
                'order_ids': [document['order_id'] for document in documents],
                'amount': float(total), 'status': 'success', 'created_at': now,
                'basis': 'selling_price_less_store_base',
            }, session=session)
            if total:
                accounts = database['store_accounts']
                account = accounts.find_one({'store_slug': slug}, session=session)
                query = {'_id': account['_id']} if account else {'_id': account_id}
                accounts.update_one(query, {
                    '$inc': {'total_profit_balance': float(total)},
                    '$set': {'last_updated_profit': float(total), 'updated_at': now},
                    '$setOnInsert': {'store_slug': slug, 'created_at': now},
                }, upsert=True, session=session)
        return documents, True

    with database.client.start_session() as session:
        return session.with_transaction(commit)
