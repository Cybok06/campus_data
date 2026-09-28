import hashlib
import hmac
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta
import importlib.util
from pathlib import Path
from unittest.mock import patch

import mongomock
import requests
from flask import Flask
from bson import ObjectId

import bundle_portal as api
import bundle_portal_orders as orders


class BundlePortalClientTests(unittest.TestCase):
    def test_sizes_preserve_fractional_gb_and_convert_mb(self):
        for value, item, expected in [({'volume': 1500}, {}, 1.5),
                                      ({'size_gb': 0.5}, {}, 0.5),
                                      ({'volume': 5}, {}, 5),
                                      ({}, {'value': '500MB'}, 0.5),
                                      ({}, {'value': '5 GB'}, 5),
                                      ({'size_gb': 'NaN'}, {}, None), ({}, {}, None)]:
            with self.subTest(value=value, item=item):
                self.assertEqual(api.package_size(value, item), expected)

    @patch.object(api, 'call')
    def test_all_routes_verify_catalog_and_recipient_before_purchase(self, call):
        for provider, network in api.PROVIDERS.items():
            with self.subTest(provider=provider):
                call.reset_mock()
                call.side_effect = [
                    {'success': True, 'data': {'bundles': [{'size_gb': 1.5, 'network': network}]}},
                    {'success': True, 'data': {'can_order': True}},
                    {'success': True, 'data': {'status': 'cached'}},
                ]
                result = api.submit(provider, '0241234567', 1.5, 'BP_reference')
                self.assertTrue(result['success'])
                self.assertEqual([c.args[0] for c in call.call_args_list], ['get_bundles', 'verify_number', 'place_order'])
                self.assertTrue(all(c.kwargs['network'] == network for c in call.call_args_list))
                self.assertEqual(call.call_args.kwargs['order_id'], 'BP_reference')

    @patch.object(api, 'call')
    def test_unavailable_size_and_blocked_number_do_not_purchase(self, call):
        call.return_value = {'success': True, 'data': {'bundles': []}}
        self.assertEqual(api.submit('bundleportal_mtn', '0241234567', 5, 'ref')['code'], 'bundle_unavailable')
        self.assertEqual(call.call_count, 1)
        call.reset_mock()
        call.side_effect = [
            {'success': True, 'data': {'bundles': [{'size_gb': 5}]}},
            {'success': True, 'data': {'can_order': False}},
        ]
        self.assertEqual(api.submit('bundleportal_mtn', '0241234567', 5, 'ref')['code'], 'recipient_blocked')
        self.assertEqual(call.call_count, 2)

    @patch.object(api, 'call', return_value={'success': True})
    def test_uncertain_purchase_retry_reuses_reference_without_blocking_on_own_order(self, call):
        api.submit('bundleportal_mtn2', '0241234567', 5, 'original_ref', retry_purchase=True)
        call.assert_called_once_with('place_order', network='mtn_2', recipient='0241234567', package_size=5, order_id='original_ref')

    @patch.dict(os.environ, {'BUNDLE_PORTAL_KEY': ''})
    @patch.object(api.requests, 'post')
    def test_missing_credentials_never_calls_network(self, post):
        self.assertEqual(api.call('get_bundles')['code'], 'not_configured')
        post.assert_not_called()

    @patch.dict(os.environ, {'BUNDLE_PORTAL_KEY': 'test-key'})
    @patch.object(api.requests, 'post', side_effect=requests.Timeout())
    def test_timeout_is_uncertain_and_not_automatically_retried(self, post):
        self.assertEqual(api.call('place_order', order_id='same-ref')['code'], 'unknown_outcome')
        self.assertEqual(post.call_count, 1)


class BundlePortalOrderTests(unittest.TestCase):
    def setUp(self):
        self.db = mongomock.MongoClient().test
        fake = types.ModuleType('db')
        fake.db = self.db
        fake.campus_db = mongomock.MongoClient().campus
        for p in (patch.dict(sys.modules, {'db': fake}),
                  patch.dict(os.environ, {'BUNDLE_PORTAL_WEBHOOK_SECRET': 'test-secret'}),
                  patch.object(orders, 'invalidate_orders')):
            p.start()
            self.addCleanup(p.stop)
        app = Flask(__name__)
        app.secret_key = 'test'
        app.register_blueprint(orders.bundle_portal_bp)
        self.client = app.test_client()
        self.db.orders.insert_one({'order_id': 'ORDER1', 'status': 'processing', 'items': [{
            'provider': 'bundleportal_mtn2', 'provider_request_order_id': 'BP_ref',
            'phone': '0241234567', 'provider_network': 'mtn_2', 'provider_gb_size': 5,
            'line_status': 'pending', 'api_status': 'queued',
        }]})
        self.job = {'provider': 'bundleportal_mtn2', 'provider_request_order_id': 'BP_ref',
                    'phone': '0241234567', 'bundle_portal_gb_size': 5}

    def document(self):
        return self.db.orders.find_one({'order_id': 'ORDER1'})

    def callback(self, status='completed', **overrides):
        data = {'event': f'order.{status}', 'status': status, 'order_id': 'BP_ref',
                'network': 'mtn_2', 'recipient': '0241234567', 'reference': 'provider-ref', **overrides}
        raw = json.dumps(data).encode()
        signature = 'sha256=' + hmac.new(b'test-secret', raw, hashlib.sha256).hexdigest()
        return self.client.post('/webhooks/bundleportal', data=raw, content_type='application/json',
                                headers={'X-BundlePortal-Signature': signature})

    def test_invalid_signature_rejected_without_writes(self):
        response = self.client.post('/webhooks/bundleportal', json={'status': 'completed'})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.document()['items'][0]['line_status'], 'pending')

    def test_completed_callback_updates_line_and_parent_and_is_idempotent(self):
        self.assertEqual(self.callback().status_code, 200)
        self.assertEqual(self.callback().status_code, 200)
        self.assertEqual(self.document()['status'], 'delivered')
        self.assertEqual(self.document()['items'][0]['line_status'], 'delivered')
        self.assertEqual(self.db.bundleportal_events.count_documents({}), 1)

    def test_wrong_route_or_recipient_rejected(self):
        self.assertEqual(self.callback(network='mtn').status_code, 400)
        self.assertEqual(self.callback(recipient='0240000000').status_code, 400)
        self.assertEqual(self.document()['status'], 'processing')

    def test_ishare_webhook_alias_updates_the_correct_line(self):
        self.db.orders.update_one({'order_id': 'ORDER1'}, {'$set': {
            'items.0.provider': 'bundleportal_ishare', 'items.0.provider_network': 'airteltigo',
            'items.0.phone': '0271234567',
        }})
        self.assertEqual(self.callback(network='ishare', recipient='0271234567').status_code, 200)
        self.assertEqual(self.document()['status'], 'delivered')

    def test_telecel_webhook_updates_the_correct_line(self):
        self.db.orders.update_one({'order_id': 'ORDER1'}, {'$set': {
            'items.0.provider': 'bundleportal_telecel', 'items.0.provider_network': 'telecel',
            'items.0.phone': '0201234567',
        }})
        self.assertEqual(self.callback(network='telecel', recipient='0201234567').status_code, 200)
        self.assertEqual(self.document()['status'], 'delivered')

    def test_provider_refund_after_delivery_does_not_claim_local_wallet_refund(self):
        self.callback()
        self.assertEqual(self.callback('refunded').status_code, 200)
        self.assertEqual(self.document()['items'][0]['line_status'], 'failed')
        self.assertEqual(self.document()['items'][0]['provider_status'], 'refunded')

    def test_local_refund_is_not_reverted_by_late_callback(self):
        self.db.orders.update_one({'order_id': 'ORDER1'}, {'$set': {'items.0.line_status': 'refunded'}})
        self.callback()
        self.assertEqual(self.document()['items'][0]['line_status'], 'refunded')

    def test_failure_flags_local_refund_without_claiming_wallet_was_credited(self):
        self.assertEqual(self.callback('failed').status_code, 200)
        self.assertEqual(self.document()['status'], 'failed')
        self.assertTrue(self.document()['items'][0]['refund_required'])
        self.assertEqual(self.db.transactions.count_documents({}), 0)

    @patch.object(orders, 'submit', return_value={'success': True, 'data': {'status': 'cached'}})
    def test_cached_is_processing_and_duplicate_worker_does_not_respend(self, submit):
        orders.process_job(self.db.orders, 'ORDER1', self.job)
        orders.process_job(self.db.orders, 'ORDER1', self.job)
        self.assertEqual(self.document()['items'][0]['line_status'], 'processing')
        self.assertEqual(submit.call_count, 1)

    def test_callback_arriving_before_submit_response_is_not_overwritten(self):
        def early_callback(*args, **kwargs):
            self.assertEqual(self.callback().status_code, 200)
            return {'success': True, 'data': {'status': 'processing'}}
        with patch.object(orders, 'submit', side_effect=early_callback):
            orders.process_job(self.db.orders, 'ORDER1', self.job)
        self.assertEqual(self.document()['status'], 'delivered')
        self.assertEqual(self.document()['items'][0]['line_status'], 'delivered')

    def test_transient_errors_stay_processing_for_review(self):
        for payload in ({'success': False, 'http_status': 429},
                        {'success': False, 'http_status': 409},
                        {'success': False, 'code': 'unknown_outcome'}):
            self.db.orders.update_one({'order_id': 'ORDER1'}, {'$set': {'items.0.api_status': 'queued'}})
            with patch.object(orders, 'submit', return_value=payload):
                orders.process_job(self.db.orders, 'ORDER1', self.job)
            item = self.document()['items'][0]
            self.assertEqual(item['api_status'], 'review_required')
            self.assertEqual(item['line_status'], 'processing')
            self.assertFalse(item['refund_required'])

    def test_catalog_and_retry_require_admin(self):
        self.assertEqual(self.client.get('/admin/services/bundleportal/catalog').status_code, 401)
        self.assertEqual(self.client.post('/admin/orders/abc/items/0/bundleportal-retry').status_code, 401)



if __name__ == "__main__":
    unittest.main()
