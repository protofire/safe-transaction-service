from django.test import TestCase

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from safe_eth.eth.tests.ethereum_test_case import EthereumTestCaseMixin
from safe_eth.eth.utils import fast_keccak, fast_keccak_text

from ..indexers import DelayModuleEventsIndexerProvider
from ..models import DelayModuleTransaction, IndexingStatus
from ..services import IndexServiceProvider

TRANSACTION_ADDED_TOPIC = fast_keccak_text(
    "TransactionAdded(uint256,bytes32,address,uint256,bytes,uint8)"
)


def get_transaction_added_emitter_init_code() -> bytes:
    """
    Minimal contract emitting `LOG3(calldata[64:], TransactionAdded topic, calldata[0:32], calldata[32:64])`,
    the same log a Zodiac Delay Modifier emits when a module queues a transaction
    """
    runtime = (
        bytes.fromhex("604036038060406000376020356000357f")
        + TRANSACTION_ADDED_TOPIC
        + bytes.fromhex("836000a300")
    )
    # CODECOPY the runtime (placed right after this 11 bytes init code) and RETURN it
    return bytes.fromhex(f"60{len(runtime):02x}80600b6000396000f3") + runtime


class TestDelayModuleEventsIndexer(EthereumTestCaseMixin, TestCase):
    def setUp(self) -> None:
        self.delay_module_events_indexer = DelayModuleEventsIndexerProvider()
        self.delay_module_events_indexer.confirmations = 0
        self.delay_module_events_indexer.blocks_to_reindex_again = 0
        # Ignore events from previous tests
        IndexingStatus.objects.set_delay_module_indexing_status(
            self.ethereum_client.current_block_number + 1
        )

    def tearDown(self) -> None:
        DelayModuleEventsIndexerProvider.del_singleton()
        # Indexers replace the `ethereum_client` of the shared `IndexService`
        IndexServiceProvider.del_singleton()

    def deploy_delay_module(self) -> ChecksumAddress:
        tx_hash = self.send_tx(
            {"data": get_transaction_added_emitter_init_code()},
            self.ethereum_test_account,
        )
        return self.w3.eth.wait_for_transaction_receipt(tx_hash)["contractAddress"]

    def queue_transaction(
        self,
        delay_module_address: ChecksumAddress,
        queue_nonce: int,
        to: ChecksumAddress,
        value: int,
        data: bytes,
        operation: int,
    ) -> bytes:
        module_tx_hash = fast_keccak(
            abi_encode(
                ["address", "uint256", "bytes", "uint8"], [to, value, data, operation]
            )
        )
        calldata = (
            queue_nonce.to_bytes(32, "big")
            + module_tx_hash
            + abi_encode(
                ["address", "uint256", "bytes", "uint8"], [to, value, data, operation]
            )
        )
        tx_hash = self.send_tx(
            {"to": delay_module_address, "data": calldata}, self.ethereum_test_account
        )
        self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return module_tx_hash

    def test_index_transaction_added(self):
        delay_module_address = self.deploy_delay_module()
        to = Account.create().address
        module_tx_hash = self.queue_transaction(
            delay_module_address, 3, to, 5, b"\x12\x34", 1
        )

        number_events, _ = self.delay_module_events_indexer.start()

        self.assertEqual(number_events, 1)
        delay_module_transaction = DelayModuleTransaction.objects.get()
        self.assertEqual(delay_module_transaction.module, delay_module_address)
        self.assertEqual(delay_module_transaction.queue_nonce, 3)
        self.assertEqual(
            HexBytes(delay_module_transaction.module_tx_hash), module_tx_hash
        )
        self.assertEqual(delay_module_transaction.to, to)
        self.assertEqual(delay_module_transaction.value, 5)
        self.assertEqual(bytes(delay_module_transaction.data), b"\x12\x34")
        self.assertEqual(delay_module_transaction.operation, 1)
        self.assertEqual(
            delay_module_transaction.ethereum_tx._from,
            self.ethereum_test_account.address,
        )
        self.assertEqual(
            delay_module_transaction.block_number,
            delay_module_transaction.ethereum_tx.block_id,
        )

    def test_index_events_from_every_delay_module(self):
        first_delay_module_address = self.deploy_delay_module()
        second_delay_module_address = self.deploy_delay_module()
        to = Account.create().address
        self.queue_transaction(first_delay_module_address, 0, to, 0, b"", 0)
        self.queue_transaction(second_delay_module_address, 0, to, 0, b"", 0)

        number_events, _ = self.delay_module_events_indexer.start()

        self.assertEqual(number_events, 2)
        self.assertEqual(
            set(DelayModuleTransaction.objects.values_list("module", flat=True)),
            {first_delay_module_address, second_delay_module_address},
        )

    def test_reindexing_does_not_duplicate_events(self):
        from_block_number = self.ethereum_client.current_block_number + 1
        delay_module_address = self.deploy_delay_module()
        self.queue_transaction(
            delay_module_address, 0, Account.create().address, 0, b"", 0
        )
        self.delay_module_events_indexer.start()

        IndexingStatus.objects.set_delay_module_indexing_status(from_block_number)
        self.delay_module_events_indexer.element_already_processed_checker.clear()
        self.delay_module_events_indexer.start()

        self.assertEqual(DelayModuleTransaction.objects.count(), 1)

    def test_indexing_status_is_updated(self):
        self.delay_module_events_indexer.start()

        self.assertEqual(
            IndexingStatus.objects.get_delay_module_indexing_status().block_number,
            self.ethereum_client.current_block_number + 1,
        )
