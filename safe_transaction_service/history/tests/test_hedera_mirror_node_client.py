from unittest.mock import MagicMock

from django.test import TestCase

from safe_transaction_service.history.clients.exceptions import (
    HederaMirrorNodeNotFoundException,
    HederaMirrorNodeRateLimitException,
)
from safe_transaction_service.history.clients.hedera_mirror_node_client import (
    HederaMirrorNodeClient,
    hedera_account_id_to_long_zero_address,
)


class TestHederaAccountIdToLongZeroAddress(TestCase):
    def test_converts_shard_realm_num_to_padded_evm_address(self):
        self.assertEqual(
            hedera_account_id_to_long_zero_address("0.0.10127045"),
            "0x00000000000000000000000000000000009a86c5",
        )

    def test_converts_low_account_number(self):
        self.assertEqual(
            hedera_account_id_to_long_zero_address("0.0.98"),
            "0x0000000000000000000000000000000000000062",
        )


class TestHederaMirrorNodeClient(TestCase):
    def setUp(self):
        self.client = HederaMirrorNodeClient(
            base_url="https://testnet.mirrornode.hedera.com/",
            rate_limit_rps=0,  # disable throttling sleep in tests
        )

    def _mock_response(
        self, status_code: int, json_data: dict | None = None, headers=None
    ):
        response = MagicMock()
        response.status_code = status_code
        response.ok = 200 <= status_code < 300
        response.json.return_value = json_data or {}
        response.headers = headers or {}
        response.text = str(json_data)
        return response

    def test_resolve_account_id_returns_account_on_success(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(200, {"account": "0.0.10127045"})
        )
        self.assertEqual(
            self.client.resolve_account_id("0x0000...0099899705"), "0.0.10127045"
        )

    def test_resolve_account_id_returns_none_on_404(self):
        self.client.http_session.get = MagicMock(return_value=self._mock_response(404))
        self.assertIsNone(self.client.resolve_account_id("0xnotfound"))

    def test_resolve_evm_address_uses_alias_when_present(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(
                200,
                {
                    "account": "0.0.1111111",
                    "evm_address": "0xaaaa00000000000000000000000000000000aaaa",
                },
            )
        )
        self.assertEqual(
            self.client.resolve_evm_address("0.0.1111111"),
            "0xaaaa00000000000000000000000000000000aaaa",
        )

    def test_resolve_evm_address_falls_back_to_long_zero(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(
                200, {"account": "0.0.1111111", "evm_address": None}
            )
        )
        self.assertEqual(
            self.client.resolve_evm_address("0.0.1111111"),
            hedera_account_id_to_long_zero_address("0.0.1111111"),
        )

    def test_resolve_evm_address_falls_back_to_long_zero_when_account_not_found(self):
        # The account isn't found at all (Mirror Node 404s the account
        # lookup) — get_account returns None — resolve_evm_address must
        # still fall back to the deterministic long-zero address rather
        # than erroring out.
        self.client.http_session.get = MagicMock(return_value=self._mock_response(404))

        with self.assertRaises(HederaMirrorNodeNotFoundException):
            self.client._get(
                "https://testnet.mirrornode.hedera.com/api/v1/accounts/0.0.1111111"
            )
        self.assertIsNone(self.client.get_account("0.0.1111111"))
        self.assertEqual(
            self.client.resolve_evm_address("0.0.1111111"),
            hedera_account_id_to_long_zero_address("0.0.1111111"),
        )

    def test_get_crypto_transfers_follows_pagination(self):
        page_1 = self._mock_response(
            200,
            {
                "transactions": [{"transaction_id": "tx-1"}],
                "links": {"next": "/api/v1/transactions?page=2"},
            },
        )
        page_2 = self._mock_response(
            200, {"transactions": [{"transaction_id": "tx-2"}], "links": {"next": None}}
        )
        self.client.http_session.get = MagicMock(side_effect=[page_1, page_2])
        results = list(self.client.get_crypto_transfers("0.0.10127045"))
        self.assertEqual([r["transaction_id"] for r in results], ["tx-1", "tx-2"])
        self.assertEqual(self.client.http_session.get.call_count, 2)

    def test_resolve_block_number_returns_number(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(200, {"blocks": [{"number": 12345}]})
        )
        self.assertEqual(
            self.client.resolve_block_number("1787164650.039440869"), 12345
        )

    def test_resolve_block_number_returns_none_when_no_block_found(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(200, {"blocks": []})
        )
        self.assertIsNone(self.client.resolve_block_number("1787164650.039440869"))

    def test_get_retries_on_429_then_succeeds(self):
        rate_limited = self._mock_response(429, headers={"Retry-After": "0"})
        success = self._mock_response(200, {"account": "0.0.10127045"})
        self.client.http_session.get = MagicMock(side_effect=[rate_limited, success])
        self.assertEqual(self.client.resolve_account_id("0xabc"), "0.0.10127045")

    def test_get_raises_after_exhausting_429_retries(self):
        self.client.http_session.get = MagicMock(
            return_value=self._mock_response(429, headers={"Retry-After": "0"})
        )
        with self.assertRaises(HederaMirrorNodeRateLimitException):
            self.client.resolve_account_id("0xabc")
