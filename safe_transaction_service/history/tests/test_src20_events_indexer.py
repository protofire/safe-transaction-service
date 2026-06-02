from unittest import mock
from unittest.mock import MagicMock

from django.test import TestCase

from safe_eth.eth.ethereum_client import Erc20Info, Erc20Manager

from safe_transaction_service.tokens.models import Token, TokenManager

from ..indexers import Src20EventsIndexerProvider
from ..models import SafeRelevantTransaction, SRC20Transfer
from .factories import EthereumTxFactory
from .mocks.mocks_src20_events_indexer import log_receipt_mock


class TestSrc20EventsIndexer(TestCase):
    def setUp(self) -> None:
        Src20EventsIndexerProvider.del_singleton()
        self.src20_events_indexer = Src20EventsIndexerProvider()

    def tearDown(self) -> None:
        Src20EventsIndexerProvider.del_singleton()

    def _store_ethereum_tx(self):
        # `process_elements` prefetches the tx and reads the block timestamp by hash
        for log_receipt in log_receipt_mock:
            EthereumTxFactory(
                tx_hash=log_receipt["transactionHash"],
                block__block_hash=log_receipt["blockHash"],
            )

    @mock.patch.object(
        Erc20Manager,
        "get_info",
        autospec=True,
        # Attacker-controlled, over the `Token.name`/`symbol` 60-char limit. Without
        # truncation this raises `DataError` and halts the indexer permanently.
        return_value=Erc20Info(name="N" * 100, symbol="S" * 100, decimals=18),
    )
    def test_process_elements_long_token_name_does_not_halt(
        self, get_info: MagicMock
    ):
        self._store_ethereum_tx()
        token_address = log_receipt_mock[0]["address"]

        # Must not raise
        processed = self.src20_events_indexer.process_elements(log_receipt_mock)

        self.assertEqual(len(processed), 1)
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        # Both `from` and `to` get a SafeRelevantTransaction row
        self.assertEqual(SafeRelevantTransaction.objects.count(), 2)

        token = Token.objects.get(address=token_address)
        self.assertTrue(token.src20)
        self.assertEqual(token.decimals, 0)
        self.assertEqual(len(token.name), 60)
        self.assertEqual(token.name, "N" * 60)

    @mock.patch.object(
        TokenManager,
        "create_src20_from_blockchain",
        side_effect=Exception("boom"),
    )
    def test_process_elements_token_registration_failure_does_not_halt(
        self, create_src20: MagicMock
    ):
        self._store_ethereum_tx()

        # Token registration blows up, but transfers are already stored and the call returns
        # (so the indexer's block cursor still advances).
        processed = self.src20_events_indexer.process_elements(log_receipt_mock)

        self.assertEqual(len(processed), 1)
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        create_src20.assert_called_once()
        # No token row created because registration failed
        self.assertEqual(Token.objects.count(), 0)

    def test_topic_to_address(self):
        # Sanity check on the indexed-topic -> address helper used for node-side filtering
        from_topic = log_receipt_mock[0]["topics"][1]
        self.assertEqual(
            self.src20_events_indexer._topic_to_address(from_topic),
            "0x22d491Bde2303f2f43325b2108D26f1eAbA1e32b",
        )
