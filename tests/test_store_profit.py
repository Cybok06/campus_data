import ast
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import re
import traceback
import types
import unittest
import uuid
from unittest.mock import Mock, patch

from bson import ObjectId
from flask import Flask, jsonify, session
import mongomock
import bundle_portal
import store_profit as profit


class Session:
    def __init__(self, database):
        self.database = database
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def with_transaction(self, callback):
        snapshot = {name: deepcopy(list(self.database.raw[name].find({})))
                    for name in self.database.raw.list_collection_names()}
        try:
            return callback(self)
        except Exception:
            for name in self.database.raw.list_collection_names():
                self.database.raw[name].delete_many({})
                if snapshot.get(name):
                    self.database.raw[name].insert_many(snapshot[name])
            raise


class Collection:
    def __init__(self, raw):
        self.raw = raw
    def __getattr__(self, name):
        def call(*args, **kwargs):
            kwargs.pop('session', None)
            return getattr(self.raw, name)(*args, **kwargs)
        return call


class Database:
    def __init__(self):
        self.raw = mongomock.MongoClient().campus
        self.client = types.SimpleNamespace(start_session=lambda: Session(self))
        self.collections = {}
    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection(self.raw[name]))


class ProfitPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.db['store_accounts'].insert_one({'store_slug': 'shop', 'total_profit_balance': 50})
    def documents(self, amounts=((12, 10),), reference='PS1', slug='shop'):
        return [{'order_id': f'ORDER{i}', 'store_slug': slug, 'paystack_reference': reference,
                 'batch_position': i, 'items': [{'amount': selling, 'base_amount': base,
                                               'line_status': 'processing'}]}
                for i, (selling, base) in enumerate(amounts, 1)]
    def save(self, documents=None, reference='PS1', paid=True):
        return profit.persist_store_purchase(self.db, documents or self.documents(), paid=paid, reference=reference)
    def balance(self):
        return self.db['store_accounts'].find_one({'store_slug': 'shop'})['total_profit_balance']

    def test_credit_multiple_lines_and_receipt(self):
        docs, saved = self.save(self.documents(((12, 10), (20, 15))))
        self.assertTrue(saved)
        self.assertEqual(self.balance(), 57)
        self.assertEqual([d['store_profit_amount_total'] for d in docs], [2, 5])
        self.assertEqual(self.db['store_profit_credits'].find_one({})['amount'], 7)
        self.assertEqual(self.db['orders'].count_documents({}), 2)

    def test_repeat_payment_returns_original_orders_without_extra_credit(self):
        first, _ = self.save()
        second, saved = self.save(self.documents(((30, 10),)))
        self.assertFalse(saved)
        self.assertEqual(second[0]['_id'], first[0]['_id'])
        self.assertEqual(self.balance(), 52)
        self.assertEqual(self.db['orders'].count_documents({}), 1)
        self.assertEqual(self.db['store_profit_credits'].count_documents({}), 1)

    def test_historical_orders_are_not_recredited(self):
        self.db['orders'].insert_one(self.documents()[0])
        _, saved = self.save()
        self.assertFalse(saved)
        self.assertEqual(self.balance(), 50)

    def test_skipped_line_has_zero_profit_even_if_branch_retains_it(self):
        docs = self.documents(((12, 10), (0, 0)))
        docs[1]['items'][0].update(line_status='skipped_duplicate_in_cart', store_profit_amount=2)
        saved, _ = self.save(docs)
        self.assertEqual(saved[1]['items'][0]['store_profit_amount'], 0)
        self.assertEqual(self.balance(), 52)

    def test_zero_margin_deduplicates_without_credit(self):
        self.save(self.documents(((10, 10),)))
        self.save(self.documents(((12, 10),)))
        self.assertEqual(self.balance(), 50)
        self.assertEqual(self.db['store_profit_credits'].count_documents({}), 1)

    def test_unpaid_override_creates_no_profit_credit(self):
        docs, _ = self.save(paid=False)
        self.assertEqual(docs[0]['items'][0]['store_profit_amount'], 0)
        self.assertEqual(self.balance(), 50)
        self.assertEqual(self.db['store_profit_credits'].count_documents({}), 0)

    def test_account_failure_rolls_back_orders_and_receipt_then_retry_succeeds(self):
        with patch.object(self.db['store_accounts'], 'update_one', side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertEqual(self.db['orders'].count_documents({}), 0)
        self.assertEqual(self.db['store_profit_credits'].count_documents({}), 0)
        self.assertEqual(self.balance(), 50)
        self.save()
        self.assertEqual(self.balance(), 52)

    def test_receipt_failure_rolls_back_orders_and_balance(self):
        with patch.object(self.db['store_profit_credits'], 'insert_one', side_effect=RuntimeError('Unavailable')):
            with self.assertRaises(RuntimeError):
                self.save()
        self.assertEqual(self.db['orders'].count_documents({}), 0)
        self.assertEqual(self.balance(), 50)

    def test_later_order_failure_rolls_back_earlier_line(self):
        docs = self.documents(((12, 10), (20, 15)))
        docs[0]['_id'] = docs[1]['_id'] = ObjectId()
        with self.assertRaises(Exception):
            self.save(docs)
        self.assertEqual(self.db['orders'].count_documents({}), 0)
        self.assertEqual(self.balance(), 50)

    def test_new_store_account_uses_stable_identity(self):
        self.db['store_accounts'].delete_many({})
        self.save()
        self.save(self.documents(reference='PS2'), reference='PS2')
        self.assertEqual(self.balance(), 4)
        self.assertEqual(self.db['store_accounts'].count_documents({}), 1)

    def test_fractional_currency_is_accurate(self):
        self.save(self.documents((('4.96', '4.50'), ('9.91', '9.00'))))
        self.assertEqual(self.balance(), 51.37)

    def test_invalid_price_or_negative_margin_never_creates_credit(self):
        for selling, base in ((9, 10), ('nan', 10), (12, None), (12, 'inf')):
            with self.subTest(selling=selling, base=base):
                with self.assertRaises(ValueError):
                    self.save(self.documents(((selling, base),)))
                self.assertEqual(self.balance(), 50)
                self.assertEqual(self.db['orders'].count_documents({}), 0)

    def test_zero_and_missing_profit_never_fall_back_to_platform_margin(self):
        self.assertEqual(profit.order_store_profit({'profit_amount_total': 5,
            'items': [{'store_profit_amount': 0}, {'amount': 12, 'base_amount': 10},
                      {'store_profit_amount': 2, 'line_status': 'skipped_duplicate_processing'}]}), 0)

    def test_dashboard_aggregate_matches_recorded_item_profit(self):
        samples = [
            {'profit_amount_total': 99, 'items': [{'store_profit_amount': 0}]},
            {'profit_amount_total': 99, 'items': [{'amount': 12, 'base_amount': 10}]},
            {'items': [{'store_profit_amount': 2, 'line_status': 'delivered'},
                       {'store_profit_amount': 9, 'line_status': 'skipped_duplicate_in_cart'}]},
            {'items': [{'store_profit_amount': .46}, {'store_profit_amount': .91}]},
        ]
        self.db.raw.orders.insert_many(samples)
        results = list(self.db.raw.orders.aggregate([{'$project': {'profit': profit.store_profit_expression()}}]))
        self.assertEqual([round(r['profit'], 2) for r in results], [0, 0, 2, 1.37])
        self.assertEqual([round(r['profit'], 2) for r in results], [profit.order_store_profit(s) for s in samples])


class StoreCheckoutTests(unittest.TestCase):
    """Execute production checkout and pricing functions with isolated database/payment doubles."""
    def setUp(self):
        root = Path(__file__).resolve().parents[1]
        self.db = Database()
        env = dict(ast=ast, json=json, re=re, traceback=traceback, datetime=datetime, uuid=uuid,
            ObjectId=ObjectId, jsonify=jsonify, session=session, db=self.db,
            store_margin=profit.store_margin, prepare_store_profit_items=profit.prepare_store_profit_items,
            persist_store_purchase=profit.persist_store_purchase,
            BP_PROVIDERS=bundle_portal.PROVIDERS, bp_supports=bundle_portal.supports_service,
            bp_size=bundle_portal.package_size, _checkout_helpers={},
            threading=types.SimpleNamespace(Thread=Mock()), url_for=lambda *a, **k: '/invoice',
            jlog=Mock(), first_blocked_phone=lambda phones: None, BLOCK_NEW_NUMBERS_MESSAGE='Blocked',
            _service_unavailability_reason=lambda service: (False, ''),
            _is_mtn_normal_service=lambda sid, service: (service or {}).get('name') == 'MTN NORMAL',
            _extract_ported_fields=lambda item: {}, _build_bundle_key=lambda value, item: ('volume', 1000),
            _has_processing_conflict_strict=Mock(return_value=False), _resolve_network_id=lambda *a: 1,
            _resolve_network_group=lambda *a: 'mtn', _extract_gh_prefix=lambda phone: '024',
            PORTED_PREFIXES={}, _resolve_provider_network=lambda *a: 'mtn',
            _resolve_skplug_network_name=lambda *a: 'mtn',
            _resolve_dataconnect_network=lambda *a: 'mtn',
            _resolve_bundleportal_network=lambda *a: 'mtn',
            _resolve_package_size_gb=lambda value, item: bundle_portal.package_size(value, item),
            _resolve_codecraft_network_name=lambda *a: 'mtn',
            _resolve_datakazina_shared_bundle=lambda *a: False,
            compute_provider_cost=lambda *a: 8, debit_provider=Mock(return_value=(True, 'ok', 100)),
            credit_provider=Mock(return_value=(True, 'ok', 100)),
            _background_process_providers=Mock(), _utc_day_key=lambda: '2026-10-08',
            _insert_paystack_audit=Mock(), _paid_enough=lambda actual, expected: actual >= expected,
            _verify_paystack=Mock(return_value=(True, {'amount': 1200, 'currency': 'GHS'}, 'OK', {})))
        for name in ('stores', 'services', 'orders', 'transactions', 'provider_transactions', 'store_accounts'):
            env[name + '_col'] = self.db[name]
        for filename, names in (('checkout.py', {'_money', '_to_float', '_coerce_value_obj', '_split_order_documents'}),
                                ('routes/store_page.py', None)):
            tree = ast.parse((root / filename).read_text(encoding='utf-8'))
            nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and (names is None or node.name in names)]
            for node in nodes:
                node.decorator_list = []
            prefix = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
            module = ast.Module(body=[prefix] + nodes, type_ignores=[])
            ast.fix_missing_locations(module)
            exec(compile(module, filename, 'exec'), env)
            # Avoid replacing the test doubles with unrelated network helpers.
        env.update(_resolve_network_group=lambda *a: 'mtn', _extract_gh_prefix=lambda p: '024',
                   _normalize_gh_phone=lambda p: str(p), _is_valid_gh_phone=lambda p: bool(re.fullmatch(r'0\d{9}', p)),
                   _utc_day_key=lambda: '2026-10-08', _insert_paystack_audit=Mock(),
                   _verify_paystack=Mock(return_value=(True, {'amount': 1200, 'currency': 'GHS'}, 'OK', {})),
                   _paid_enough=lambda actual, expected: actual >= expected)
        for name, pattern in (('_NUM', r'^\s*-?\d+(\.\d+)?\s*$'), ('_GB', r'(\d+(?:\.\d+)?)\s*GB\b'),
                              ('_MB', r'(\d+(?:\.\d+)?)\s*MB\b'), ('_MIN', r'(\d+(?:\.\d+)?)\s*MIN\b')):
            env[name] = re.compile(pattern, re.I)
        env['_PKG_TAIL'] = re.compile(r'\s*\(Pkg\s*\d+\)\s*$', re.I)
        env['generate_order_id'] = lambda: uuid.uuid4().hex[:12]
        self.env = env
        self.sid = ObjectId()
        self.db['stores'].insert_one({'slug': 'shop', 'owner_id': ObjectId(), 'pricing': {'percent_default': 20}})
        self.db['services'].insert_one({'_id': self.sid, 'name': 'MTN NORMAL', 'type': 'OFF',
            'offers': [{'value': {'volume': 1000}, 'amount': 8}],
            'store_offers': [{'value': {'volume': 1000}, 'amount': 10}]})
        self.app = Flask(__name__)
        self.app.secret_key = 'test'
        # Store creation page and checkout must read the same price source.

    def call(self, cart=None, reference='PS1', method='paystack_inline'):
        body = {'cart': cart or [{'serviceId': str(self.sid), 'serviceName': 'MTN NORMAL',
                 'value': '1GB', 'phone': '0241234567', 'amount': 999, 'base_amount': 1}],
                'paystack': {'reference': reference}, 'method': method}
        with self.app.test_request_context('/api/store/shop/checkout'):
            if method == 'admin_override':
                session['role'] = 'admin'
            response, status = self.env['_store_checkout_handler']('shop', body)
            return response.get_json(), status

    def test_checkout_reprices_tampered_cart_and_credits_store_base_margin(self):
        response, status = self.call()
        self.assertEqual((status, response.get('success')), (200, True), response)
        item = self.db['orders'].find_one({})['items'][0]
        self.assertEqual((item['amount'], item['base_amount'], item['store_profit_amount']), (12, 10, 2))
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)

    def test_duplicate_cart_line_does_not_credit_profit(self):
        item = {'serviceId': str(self.sid), 'value': '1GB', 'phone': '0241234567'}
        self.env['_verify_paystack'].return_value = (True, {'amount': 2400, 'currency': 'GHS'}, 'OK', {})
        response, status = self.call([dict(item), dict(item)])
        self.assertEqual(status, 200, response)
        items = [d['items'][0] for d in self.db['orders'].find({}).sort('batch_position', 1)]
        self.assertEqual([i['store_profit_amount'] for i in items], [2, 0])
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)

    def test_existing_processing_duplicate_has_zero_profit(self):
        self.env['_has_processing_conflict_strict'].return_value = True
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['orders'].find_one({})['items'][0]['store_profit_amount'], 0)
        self.assertIsNone(self.db['store_accounts'].find_one({}))

    def test_repeat_route_submission_does_not_credit_again(self):
        self.call()
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertTrue(response['idempotent'])
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)

    def test_below_base_override_is_rejected_before_payment(self):
        self.db['stores'].update_one({}, {'$set': {'pricing': {'percent_default': -10}}})
        response, status = self.call()
        self.assertEqual(status, 400, response)
        self.env['_verify_paystack'].assert_not_called()
        self.assertEqual(self.db['orders'].count_documents({}), 0)

    def test_unknown_offer_and_service_are_rejected(self):
        for item in ({'serviceId': str(self.sid), 'value': '2GB', 'phone': '0241234567'},
                     {'serviceId': str(self.sid), 'value': 'not-an-offer', 'phone': '0241234567'},
                     {'serviceId': str(ObjectId()), 'value': '1GB', 'phone': '0241234567'}):
            response, status = self.call([item])
            self.assertEqual(status, 400, response)
        self.assertEqual(self.db['orders'].count_documents({}), 0)

    def test_admin_override_without_payment_does_not_credit_profit(self):
        response, status = self.call(method='admin_override')
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['orders'].find_one({})['items'][0]['store_profit_amount'], 0)
        self.assertIsNone(self.db['store_accounts'].find_one({}))

    def test_all_bundleportal_provider_paths_credit_profit(self):
        for provider in bundle_portal.PROVIDERS:
            with self.subTest(provider=provider):
                self.db['services'].update_one({}, {'$set': {'type': 'API', 'provider': provider,
                    'name': 'Telecel' if provider.endswith('telecel') else ('AT iShare' if provider.endswith('ishare') else 'MTN NORMAL')}})
                response, status = self.call(reference=provider)
                self.assertEqual(status, 200, response)
                order = self.db['orders'].find_one({'paystack_reference': provider})
                self.assertEqual(order['items'][0]['store_profit_amount'], 2)
                self.assertEqual(order['store_profit_amount_total'], 2)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2 * len(bundle_portal.PROVIDERS))

    def test_legacy_bundleportal_and_skplug_paths_credit_profit(self):
        for provider in ('bundleportal', 'skplug'):
            with self.subTest(provider=provider):
                self.db['services'].update_one({}, {'$set': {'type': 'API', 'provider': provider}})
                response, status = self.call(reference=provider)
                self.assertEqual(status, 200, response)
                self.assertEqual(self.db['orders'].find_one({'paystack_reference': provider})['items'][0]['store_profit_amount'], 2)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 4)

    def test_failed_account_write_returns_failure_and_route_retry_credits_once(self):
        with patch.object(self.db['store_accounts'], 'update_one', side_effect=RuntimeError('Unavailable')):
            response, status = self.call()
        self.assertEqual(status, 500, response)
        self.assertEqual(self.db['orders'].count_documents({}), 0)
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)

    def test_store_creation_display_and_purchase_use_same_base(self):
        editor = self.env['_load_all_services_for_store_edit']()
        self.assertEqual(editor[0]['offers'][0]['amount'], 10)
        self.call()
        self.assertEqual(self.db['orders'].find_one({})['items'][0]['base_amount'], editor[0]['offers'][0]['amount'])

    def test_manual_selling_price_and_store_amount_base(self):
        self.db['services'].update_one({}, {'$set': {'store_offers': [
            {'value': {'volume': 1000}, 'amount': 8, 'store_amount': 10}]}})
        self.db['stores'].update_one({}, {'$set': {'pricing': {'per_service': [
            {'service_id': str(self.sid), 'offers': [{'index': 0, 'total': 13.5}]}]}}})
        self.env['_verify_paystack'].return_value = (True, {'amount': 1350, 'currency': 'GHS'}, 'OK', {})
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 3.5)

    def test_offer_fallback_without_store_specific_offers(self):
        self.db['services'].update_one({}, {'$unset': {'store_offers': ''}})
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['orders'].find_one({})['items'][0]['base_amount'], 8)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 1.6)

    def test_multiple_bundles_select_exact_offer_not_first_offer(self):
        self.db['services'].update_one({}, {'$push': {'store_offers': {'value': {'volume': 2000}, 'amount': 18}}})
        self.env['_verify_paystack'].return_value = (True, {'amount': 2160, 'currency': 'GHS'}, 'OK', {})
        response, status = self.call([{'serviceId': str(self.sid), 'value': '2GB', 'phone': '0241234567'}])
        self.assertEqual(status, 200, response)
        item = self.db['orders'].find_one({})['items'][0]
        self.assertEqual((item['amount'], item['base_amount'], item['store_profit_amount']), (21.6, 18, 3.6))

    def test_fee_overpayment_is_not_added_to_agent_profit(self):
        self.env['_verify_paystack'].return_value = (True, {'amount': 1230, 'currency': 'GHS'}, 'OK', {})
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)

    def test_unverified_or_underpaid_payment_never_credits(self):
        for ok, amount in ((False, 1200), (True, 1000)):
            self.env['_verify_paystack'].return_value = (ok, {'amount': amount, 'currency': 'GHS'}, 'Failed', {})
            response, status = self.call()
            self.assertEqual(status, 400, response)
            self.assertEqual(self.db['store_profit_credits'].count_documents({}), 0)

    def test_simultaneous_payment_loser_does_not_credit_or_submit_jobs(self):
        self.db['services'].update_one({}, {'$set': {'type': 'API', 'provider': 'bundleportal_mtn'}})
        persist = profit.persist_store_purchase
        def competing_commit(database, documents, **kwargs):
            winner = deepcopy(documents)
            for doc in winner:
                doc['order_id'] = 'WINNER'
                doc['batch_id'] = 'WINNER_BATCH'
            persist(database, winner, **kwargs)
            return persist(database, documents, **kwargs)
        self.env['persist_store_purchase'] = competing_commit
        response, status = self.call()
        self.assertEqual(status, 200, response)
        self.assertEqual(response['order_ids'], ['WINNER'])
        self.assertEqual(self.db['orders'].count_documents({}), 1)
        self.assertEqual(self.db['store_accounts'].find_one({})['total_profit_balance'], 2)
        self.env['credit_provider'].assert_called_once()
        self.env['threading'].Thread.assert_not_called()

    def test_paid_order_alert_runs_only_after_successful_commit(self):
        alert = Mock()
        self.env['_checkout_helpers']['alert_fn'] = alert
        response, status = self.call()
        self.assertEqual(status, 200, response)
        alert.assert_called_once()
        self.call()
        alert.assert_called_once()


if __name__ == '__main__':
    unittest.main()
