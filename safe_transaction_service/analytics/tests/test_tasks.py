import json

from django.test import TestCase

from safe_transaction_service.analytics.services.analytics_service import (
    AnalyticsService,
)
from safe_transaction_service.analytics.tasks import (
    _calculate_native_balances_from_db,
    get_transactions_per_safe_app_task,
)
from safe_transaction_service.history.models import (
    EthereumTxCallType,
    MultisigTransaction,
)
from safe_transaction_service.history.tests.factories import (
    InternalTxFactory,
    MultisigTransactionFactory,
    SafeContractFactory,
)
from safe_transaction_service.utils.redis import get_redis


class TestCalculateNativeBalancesFromDb(TestCase):
    def test_empty_safe_contracts(self):
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 0)
        self.assertEqual(safes_with_balance, 0)

    def test_safe_with_only_incoming(self):
        safe = SafeContractFactory()
        InternalTxFactory(
            to=safe.address,
            value=1000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 1000)
        self.assertEqual(safes_with_balance, 1)

    def test_safe_with_only_outgoing(self):
        safe = SafeContractFactory()
        InternalTxFactory(
            _from=safe.address,
            value=500,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 0)
        self.assertEqual(safes_with_balance, 0)

    def test_safe_with_positive_net_balance(self):
        safe = SafeContractFactory()
        InternalTxFactory(
            to=safe.address,
            value=1000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        InternalTxFactory(
            _from=safe.address,
            value=300,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 700)
        self.assertEqual(safes_with_balance, 1)

    def test_safe_with_zero_net_balance(self):
        safe = SafeContractFactory()
        InternalTxFactory(
            to=safe.address,
            value=500,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        InternalTxFactory(
            _from=safe.address,
            value=500,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 0)
        self.assertEqual(safes_with_balance, 0)

    def test_error_transactions_excluded(self):
        safe = SafeContractFactory()
        # Successful incoming
        InternalTxFactory(
            to=safe.address,
            value=1000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        # Failed incoming — should be excluded
        InternalTxFactory(
            to=safe.address,
            value=5000,
            call_type=EthereumTxCallType.CALL.value,
            error="Reverted",
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 1000)
        self.assertEqual(safes_with_balance, 1)

    def test_non_safe_address_excluded(self):
        """InternalTx for addresses not in SafeContract should not be counted."""
        safe = SafeContractFactory()
        # Incoming to the Safe
        InternalTxFactory(
            to=safe.address,
            value=100,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        # Incoming to a non-Safe address (no SafeContract record)
        InternalTxFactory(
            value=9999,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        self.assertEqual(total_balance, 100)
        self.assertEqual(safes_with_balance, 1)

    def test_multiple_safes(self):
        safe1 = SafeContractFactory()
        safe2 = SafeContractFactory()
        InternalTxFactory(
            to=safe1.address,
            value=2000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        InternalTxFactory(
            to=safe2.address,
            value=3000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        InternalTxFactory(
            _from=safe2.address,
            value=1000,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
        )
        total_balance, safes_with_balance = _calculate_native_balances_from_db()
        # safe1: 2000, safe2: 3000-1000=2000
        self.assertEqual(total_balance, 4000)
        self.assertEqual(safes_with_balance, 2)


class TestTasks(TestCase):
    def test_get_transactions_per_safe_apps(self):
        redis = get_redis()
        redis.flushall()
        redis_key = AnalyticsService.REDIS_TRANSACTIONS_PER_SAFE_APP
        origin_1 = {"url": "https://example1.com", "name": "SafeApp1"}
        origin_2 = {"url": "https://example2.com", "name": "SafeApp2"}
        string_origin = "test"
        expected = [
            {
                "name": "SafeApp2",
                "url": "https://example2.com",
                "total_tx": 7,
                "tx_last_week": 7,
                "tx_last_month": 7,
                "tx_last_year": 7,
            },
            {
                "name": "SafeApp1",
                "url": "https://example1.com",
                "total_tx": 3,
                "tx_last_week": 3,
                "tx_last_month": 3,
                "tx_last_year": 3,
            },
        ]
        for _ in range(3):
            MultisigTransactionFactory(origin=origin_1)
        for _ in range(7):
            MultisigTransactionFactory(origin=origin_2)
        MultisigTransactionFactory(origin=string_origin)

        self.assertEqual(MultisigTransaction.objects.count(), 11)
        value = redis.get(redis_key)
        self.assertIsNone(value)
        # Execute the task to get data from database
        get_transactions_per_safe_app_task()
        # Get the result from redis
        value = redis.get(redis_key)
        analytic = json.loads(value)

        self.assertEqual(analytic, expected)
