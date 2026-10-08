"""Verify production split-order snapshots without importing live DB connections."""
import ast
from copy import deepcopy
from pathlib import Path
import unittest


class WalletDebitSnapshotTests(unittest.TestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'checkout.py').read_text(encoding='utf-8'))
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in ('_money', '_split_order_documents')]
        namespace = {'generate_order_id': lambda: 'LINE'}
        exec(compile(ast.Module(body=functions, type_ignores=[]), 'checkout.py', 'exec'), namespace)
        self.split = namespace['_split_order_documents']

    def test_each_order_keeps_only_its_share_of_the_wallet_deduction(self):
        order = {'user_id': 'payer', 'wallet_user_id': 'payer', 'wallet_debit_amount': 30,
                 'paid_from': 'wallet'}
        items = [{'amount': 20, 'base_amount': 15, 'line_status': 'processing'},
                 {'amount': 10, 'base_amount': 7, 'line_status': 'processing'},
                 {'amount': 99, 'line_status': 'skipped_duplicate_processing'}]
        original = deepcopy(items)
        docs, _ = self.split(order, items, 'BATCH')
        self.assertEqual([doc['wallet_debit_amount'] for doc in docs], [20, 10, 0])
        self.assertEqual([doc['items'][0]['wallet_debit_amount'] for doc in docs], [20, 10, 0])
        self.assertEqual([doc['wallet_user_id'] for doc in docs], ['payer'] * 3)
        self.assertEqual([doc['batch_position'] for doc in docs], [1, 2, 3])
        self.assertEqual(items, original)

    def test_paystack_orders_do_not_gain_wallet_debit_snapshots(self):
        docs, _ = self.split({'paid_from': 'paystack_inline', 'user_id': 'store-owner'},
                            [{'amount': 20, 'line_status': 'processing'}], 'BATCH')
        self.assertNotIn('wallet_debit_amount', docs[0])
        self.assertNotIn('wallet_debit_amount', docs[0]['items'][0])


if __name__ == '__main__':
    unittest.main()
