from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.test import APITestCase

from safe_transaction_service.history.tests.factories import (
    ERC20TransferFactory,
    ERC721TransferFactory,
    InternalTxFactory,
    ModuleTransactionFactory,
    MultisigConfirmationFactory,
    MultisigTransactionFactory,
    SafeContractFactory,
    SafeStatusFactory,
)

from safe_transaction_service.utils.redis import get_redis
from safe_transaction_service.analytics.tasks import (
    compute_active_owners_task,
    compute_active_safes_task,
    compute_safe_segments_task,
    compute_tvl_task,
    get_transactions_per_safe_app_task,
)


class AnalyticsTestMixin:
    """Common setup for analytics test classes."""

    def setUp(self):
        super().setUp()
        self.redis = get_redis()
        self.redis.flushall()
        self.user, _ = User.objects.get_or_create(username="test", password="12345")
        self.token, _ = Token.objects.get_or_create(user=self.user)
        self.auth_header = {"HTTP_AUTHORIZATION": "Token " + self.token.key}


class TestViewsV2(AnalyticsTestMixin, APITestCase):
    def test_analytics_multisig_txs_by_origin_view(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-multisig-txs-by-origin")
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        response = self.client.get(
            reverse("v2:analytics:analytics-multisig-txs-by-origin"),
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

        origin_1 = {"url": "https://example1.com", "name": "SafeApp1"}
        origin_2 = {"url": "https://example2.com", "name": "SafeApp2"}

        MultisigTransactionFactory(origin=origin_1)
        get_transactions_per_safe_app_task()
        response = self.client.get(
            reverse("v2:analytics:analytics-multisig-txs-by-origin"),
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected = [
            {
                "name": origin_1["name"],
                "url": origin_1["url"],
                "total_tx": 1,
                "tx_last_month": 1,
                "tx_last_week": 1,
                "tx_last_year": 1,
            },
        ]
        self.assertEqual(response.data, expected)

        for _ in range(3):
            MultisigTransactionFactory(origin=origin_2)

        get_transactions_per_safe_app_task()

        response = self.client.get(
            reverse("v2:analytics:analytics-multisig-txs-by-origin"),
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected = [
            {
                "name": origin_2["name"],
                "url": origin_2["url"],
                "total_tx": 3,
                "tx_last_month": 3,
                "tx_last_week": 3,
                "tx_last_year": 3,
            },
            {
                "name": origin_1["name"],
                "url": origin_1["url"],
                "total_tx": 1,
                "tx_last_month": 1,
                "tx_last_week": 1,
                "tx_last_year": 1,
            },
        ]
        self.assertEqual(response.data, expected)

        for _ in range(3):
            MultisigTransactionFactory(origin=origin_1)

        get_transactions_per_safe_app_task()
        response = self.client.get(
            reverse("v2:analytics:analytics-multisig-txs-by-origin"),
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        expected = [
            {
                "name": origin_1["name"],
                "url": origin_1["url"],
                "total_tx": 4,
                "tx_last_month": 4,
                "tx_last_week": 4,
                "tx_last_year": 4,
            },
            {
                "name": origin_2["name"],
                "url": origin_2["url"],
                "total_tx": 3,
                "tx_last_month": 3,
                "tx_last_week": 3,
                "tx_last_year": 3,
            },
        ]
        self.assertEqual(response.data, expected)


class TestSummaryView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-summary"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch(
        "safe_transaction_service.utils.ethereum.get_chain_id",
        return_value=84532,
    )
    def test_summary_empty(self, mock_chain_id):
        response = self.client.get(
            reverse("v2:analytics:analytics-summary"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_safes"], 0)
        self.assertEqual(data["total_multisig_txs"], 0)
        self.assertEqual(data["total_module_txs"], 0)
        self.assertEqual(data["total_erc20_transfers"], 0)
        self.assertEqual(data["total_erc721_transfers"], 0)
        self.assertIsNone(data["first_safe_created"])
        self.assertIsNone(data["last_safe_created"])
        self.assertEqual(data["chain_id"], 84532)

    @patch(
        "safe_transaction_service.utils.ethereum.get_chain_id",
        return_value=84532,
    )
    def test_summary_with_data(self, mock_chain_id):
        SafeContractFactory()
        SafeContractFactory()
        MultisigTransactionFactory()
        ModuleTransactionFactory()
        ERC20TransferFactory()
        ERC721TransferFactory()

        response = self.client.get(
            reverse("v2:analytics:analytics-summary"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_safes"], 2)
        self.assertEqual(data["total_multisig_txs"], 1)
        self.assertEqual(data["total_module_txs"], 1)
        self.assertEqual(data["total_erc20_transfers"], 1)
        self.assertEqual(data["total_erc721_transfers"], 1)
        self.assertIsNotNone(data["first_safe_created"])
        self.assertIsNotNone(data["last_safe_created"])


class TestActiveSafesView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-active-safes"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_window(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-active-safes"),
            {"window": "5d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_cache(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-active-safes"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["active_safes"], 0)

    def test_with_cached_data(self):
        safe = SafeContractFactory()
        MultisigTransactionFactory(safe=safe.address)

        compute_active_safes_task()

        response = self.client.get(
            reverse("v2:analytics:analytics-active-safes"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["active_safes"], 1)
        self.assertIsNotNone(response.data["computed_at"])


class TestSafeCreationsView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-safe-creations"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_interval(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-safe-creations"),
            {"interval": "hour"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-safe-creations"),
            {"interval": "day"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_with_data(self):
        SafeContractFactory()
        SafeContractFactory()

        response = self.client.get(
            reverse("v2:analytics:analytics-safe-creations"),
            {"interval": "day"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)
        for entry in response.data:
            self.assertIn("period", entry)
            self.assertIn("count", entry)


class TestActiveOwnersView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-active-owners"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_invalid_window(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-active-owners"),
            {"window": "1y"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_with_cached_data(self):
        MultisigConfirmationFactory()

        compute_active_owners_task()

        response = self.client.get(
            reverse("v2:analytics:analytics-active-owners"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["active_owners"], 1)
        self.assertIsNotNone(response.data["computed_at"])


class TestTxVolumeView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-tx-volume"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-tx-volume"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_multisig_txs"], 0)
        self.assertEqual(data["executed_multisig_txs"], 0)
        self.assertEqual(data["module_txs"], 0)
        self.assertEqual(data["total_value_wei"], "0")

    def test_with_data(self):
        MultisigTransactionFactory(value=1000)
        MultisigTransactionFactory(value=2000)
        ModuleTransactionFactory()

        response = self.client.get(
            reverse("v2:analytics:analytics-tx-volume"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_multisig_txs"], 2)
        self.assertEqual(data["module_txs"], 1)
        self.assertEqual(data["total_value_wei"], "3000")


class TestSafeSegmentsView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-safe-segments"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_cache(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-safe-segments"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["personal"], 0)
        self.assertEqual(data["team"], 0)
        self.assertEqual(data["enterprise"], 0)

    def test_with_cached_data(self):
        from eth_account import Account

        # Personal Safe (1 owner)
        SafeStatusFactory(owners=[Account.create().address], threshold=1)
        # Team Safe (3 owners)
        SafeStatusFactory(
            owners=[Account.create().address for _ in range(3)], threshold=2
        )

        compute_safe_segments_task()

        response = self.client.get(
            reverse("v2:analytics:analytics-safe-segments"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["personal"], 1)
        self.assertEqual(data["team"], 1)
        self.assertEqual(data["enterprise"], 0)
        self.assertIsNotNone(data["computed_at"])


class TestTvlView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-tvl"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty_cache(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-tvl"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_safes_with_balance"], 0)
        self.assertEqual(data["native_balance_wei"], "0")
        self.assertEqual(data["erc20_token_count"], 0)
        self.assertEqual(data["top_tokens"], [])

    def test_with_cached_data(self):
        safe = SafeContractFactory()
        InternalTxFactory(to=safe.address, value=1000000)

        compute_tvl_task()

        response = self.client.get(
            reverse("v2:analytics:analytics-tvl"), **self.auth_header
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIsNotNone(data["computed_at"])


class TestTokenVolumeView(AnalyticsTestMixin, APITestCase):
    def test_auth_required(self):
        response = self.client.get(reverse("v2:analytics:analytics-token-volume"))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_empty(self):
        response = self.client.get(
            reverse("v2:analytics:analytics-token-volume"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_erc20_transfers"], 0)
        self.assertEqual(data["unique_tokens"], 0)
        self.assertEqual(data["top_tokens"], [])

    def test_with_data(self):
        from eth_account import Account

        token_address = Account.create().address
        ERC20TransferFactory(address=token_address, value=100)
        ERC20TransferFactory(address=token_address, value=200)

        response = self.client.get(
            reverse("v2:analytics:analytics-token-volume"),
            {"window": "30d"},
            **self.auth_header,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_erc20_transfers"], 2)
        self.assertEqual(data["unique_tokens"], 1)
        self.assertEqual(len(data["top_tokens"]), 1)
        self.assertEqual(data["top_tokens"][0]["transfer_count"], 2)
        self.assertEqual(data["top_tokens"][0]["total_value"], "300")
