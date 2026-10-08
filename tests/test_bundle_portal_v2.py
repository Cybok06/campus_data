"""Run from campus_data-main: python -m unittest discover -s tests -p test_bundle_portal_v2.py."""
import ast
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock
from bundle_portal import PROVIDERS, supports_service, package_size

class CampusRoutingTests(unittest.TestCase):
    def test_customer_and_store_build_correct_v2_jobs(self):
        for filename in ('checkout.py', 'routes/store_page.py'):
            tree = ast.parse((Path(__file__).resolve().parents[1] / filename).read_text(encoding='utf-8'))
            block = next(n for n in ast.walk(tree) if isinstance(n, ast.If) and isinstance(n.test, ast.Name) and n.test.id == 'use_bp_v2')
            # Run the production routing branch in a one-item loop.
            module = ast.Module(body=[ast.For(target=ast.Name(id='_once', ctx=ast.Store()), iter=ast.List(elts=[ast.Constant(1)], ctx=ast.Load()), body=[block], orelse=[])], type_ignores=[])
            ast.fix_missing_locations(module)
            for provider, network in PROVIDERS.items():
                with self.subTest(path=filename, provider=provider):
                    env = dict(use_bp_v2=True, api_requested_total=0, amt_total=10, value_obj={'volume': 5000}, item={'value': '5GB'}, bp_size=package_size, BP_PROVIDERS=PROVIDERS, bp_provider=provider, phone='0271234567', has_processing=False, total_processing_amount=0, results=[], api_jobs=[], uuid=uuid, base_amount=9, profit_amount=1, profit_percent_used=10, ported_fields={}, service_id_raw='service', svc_name='Test', svc_type='API', network_id=1, bundle_key=None, amount_key=10, svc_doc={'_id':'service'}, idx=1, order_id='ORDER')
                    exec(compile(module, filename, 'exec'), env)
                    self.assertEqual(len(env['api_jobs']), 1)
                    job = env['api_jobs'][0]
                    self.assertEqual(job['provider'], provider)
                    self.assertEqual(job['provider_network'], network)
                    self.assertEqual(job['bundle_portal_gb_size'], 5)
                    self.assertTrue(job['provider_request_order_id'].startswith('BPC_'))
                    self.assertEqual(env['results'][0]['provider'], provider)

    def test_worker_dispatches_v2_job_to_its_own_order(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'checkout.py').read_text(encoding='utf-8'))
        worker = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_background_process_providers')
        namespace = {'BP_PROVIDERS': PROVIDERS, 'orders_col': Mock(), 'jlog': Mock(), 'datetime': __import__('datetime').datetime}
        exec(compile(ast.Module(body=[worker], type_ignores=[]), 'checkout.py', 'exec'), namespace)
        from unittest.mock import patch
        for provider in PROVIDERS:
            job = {'provider': provider, 'phone': '0241234567', 'provider_request_order_id': 'BPC_test', 'order_id': 'LINE_ORDER'}
            with patch('bundle_portal_orders.process_job') as process:
                namespace['_background_process_providers']('PARENT_ORDER', [job])
                process.assert_called_once_with(namespace['orders_col'], 'LINE_ORDER', job)

    def test_provider_service_validation(self):
        for provider in PROVIDERS:
            name = 'Telecel' if provider.endswith('telecel') else ('AT iShare' if provider.endswith('ishare') else 'MTN NORMAL')
            self.assertTrue(supports_service(provider, {'name': name}))
            self.assertFalse(supports_service(provider, {'name': 'AT Bigtime'}))

if __name__ == '__main__':
    unittest.main()
