import itertools
import os
from collections import OrderedDict
from unittest import mock
from unittest.mock import MagicMock, PropertyMock

from django.test import TestCase

from eth_typing import HexStr
from hexbytes import HexBytes
from safe_eth.eth import EthereumClient
from safe_eth.eth.ethereum_client import TracingManager
from safe_eth.eth.utils import fast_to_checksum_address
from safe_eth.util.util import to_0x_hex_str

from ..indexers import InternalTxIndexer, InternalTxIndexerProvider
from ..indexers.internal_tx_indexer import InternalTxIndexerWithTraceBlock
from ..indexers.tx_processor import SafeTxProcessorProvider
from ..models import (
    EthereumBlock,
    EthereumTx,
    IndexingStatus,
    InternalTx,
    InternalTxDecoded,
    MultisigTransaction,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
)
from ..services import IndexServiceProvider
from .factories import EthereumTxFactory, SafeMasterCopyFactory
from .mocks import mocks_mainnet_traces
from .mocks.mocks_internal_tx_indexer import (
    block_result,
    trace_blocks_filtered_0x5aC2_result,
    trace_blocks_result,
    trace_filter_result,
    trace_transactions_result,
    transaction_receipts_result,
    transactions_result,
)


class TestInternalTxIndexer(TestCase):
    def setUp(self) -> None:
        InternalTxIndexerProvider.del_singleton()
        self.internal_tx_indexer = InternalTxIndexerProvider()
        self.internal_tx_indexer.blocks_to_reindex_again = 0

    def tearDown(self) -> None:
        InternalTxIndexerProvider.del_singleton()

    def test_internal_tx_indexer_provider(self):
        internal_tx_indexer = InternalTxIndexerProvider()
        self.assertIsInstance(internal_tx_indexer, InternalTxIndexer)
        self.assertNotIsInstance(internal_tx_indexer, InternalTxIndexerWithTraceBlock)
        InternalTxIndexerProvider.del_singleton()
        with self.settings(ETH_INTERNAL_NO_FILTER=True):
            internal_tx_indexer = InternalTxIndexerProvider()
            self.assertIsInstance(
                internal_tx_indexer,
                (InternalTxIndexer, InternalTxIndexerWithTraceBlock),
            )

    def test_is_relevant_trace(self):
        indexer = self.internal_tx_indexer

        delegate_call_trace = {
            "type": "call",
            "action": {"callType": "delegatecall", "input": HexBytes("0x1234")},
        }
        ether_transfer_trace = {
            "type": "call",
            "action": {"callType": "call", "value": 1},
        }
        create_trace = {
            "type": "create",
            "action": {"init": HexBytes("0x1234")},
        }
        not_relevant_trace = {
            "type": "call",
            "action": {"callType": "call", "value": 0, "input": HexBytes("0x")},
        }

        self.assertTrue(indexer.is_relevant_trace(delegate_call_trace))
        self.assertTrue(indexer.is_relevant_trace(ether_transfer_trace))
        self.assertTrue(indexer.is_relevant_trace(create_trace))
        self.assertFalse(indexer.is_relevant_trace(not_relevant_trace))

    def return_sorted_blocks(self, hashes: HexStr):
        """
        Mock function helper

        :param hashes:
        :return:
        """
        block_dict = {to_0x_hex_str(block["hash"]): block for block in block_result}
        return [block_dict[provided_hash] for provided_hash in hashes]

    @mock.patch.object(
        TracingManager, "trace_blocks", autospec=True, return_value=trace_blocks_result
    )
    @mock.patch.object(
        TracingManager, "trace_filter", autospec=True, return_value=trace_filter_result
    )
    @mock.patch.object(
        TracingManager,
        "trace_transactions",
        autospec=True,
        return_value=trace_transactions_result,
    )
    @mock.patch.object(
        EthereumClient, "get_blocks", autospec=True, side_effect=return_sorted_blocks
    )
    @mock.patch.object(
        EthereumClient,
        "get_transaction_receipts",
        autospec=True,
        return_value=transaction_receipts_result,
    )
    @mock.patch.object(
        EthereumClient,
        "get_transactions",
        autospec=True,
        return_value=transactions_result,
    )
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def _test_internal_tx_indexer(
        self,
        current_block_number_mock: MagicMock,
        transactions_mock: MagicMock,
        transaction_receipts_mock: MagicMock,
        blocks_mock: MagicMock,
        trace_transactions_mock: MagicMock,
        trace_filter_mock: MagicMock,
        trace_block_mock: MagicMock,
    ):
        current_block_number = current_block_number_mock.return_value

        internal_tx_indexer = self.internal_tx_indexer
        self.assertEqual(
            internal_tx_indexer.ethereum_client.current_block_number,
            current_block_number,
        )
        self.assertIsNone(current_block_number_mock.assert_called_with())

        self.assertEqual(
            internal_tx_indexer.start(), (0, 0)
        )  # No SafeMasterCopy to index

        safe_master_copy: SafeMasterCopy = SafeMasterCopyFactory(
            address="0x5aC255889882aCd3da2aA939679E3f3d4cea221e"
        )
        self.assertEqual(safe_master_copy.tx_block_number, 0)
        self.assertEqual(internal_tx_indexer.start(), (3, 2001))

        self.assertEqual(EthereumTx.objects.count(), len(transactions_result))
        self.assertEqual(EthereumBlock.objects.count(), len(block_result))
        # Just store useful traces 2 decoded + 1 contract creation + 2 ether transfers
        self.assertEqual(InternalTx.objects.count(), 5)
        self.assertEqual(InternalTxDecoded.objects.count(), 2)
        create_internal_tx = InternalTx.objects.get(
            contract_address="0x673Fd582FED2CD8201d58552B912F0D1DaA37bB2"
        )
        self.assertFalse(create_internal_tx.is_call)
        self.assertFalse(create_internal_tx.is_delegate_call)
        self.assertTrue(create_internal_tx.is_create)
        self.assertEqual(
            SafeRelevantTransaction.objects.count(), 2
        )  # 2 Ether Transfers

        ethereum_tx = EthereumTx.objects.get(
            tx_hash="0x18f8eb25336203d4e561229c08a3a0ef88db1dd9767b641301d9ea3121dfeaea"
        )
        self.assertEqual(ethereum_tx.block.number, 6045252)
        self.assertEqual(
            ethereum_tx.block.block_hash,
            "0x39ba45ad930dece3aec537c8c5cd615daf7ee39a2513475e7680ec226e90b923",
        )
        self.assertEqual(len(ethereum_tx.logs), 1)

        ethereum_tx = EthereumTx.objects.get(
            tx_hash="0xf554b52dcb336b83bf31e7e2e7aa94853a456f01a139a6b7dec71329460dfb61"
        )
        self.assertEqual(ethereum_tx.block.number, 6045275)
        self.assertEqual(
            ethereum_tx.block.block_hash,
            "0x08df561efd3d242263d8a117e32c1beb08454c87df0a287cf93fa39f0675cf04",
        )
        self.assertEqual(ethereum_tx.logs, [])

        trace_filter_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing,
            from_block=0,
            to_block=current_block_number - internal_tx_indexer.number_trace_blocks,
            to_address=[safe_master_copy.address],
        )
        trace_block_mock.assert_called_with(
            internal_tx_indexer.ethereum_client.tracing,
            list(
                range(
                    current_block_number - internal_tx_indexer.number_trace_blocks + 1,
                    current_block_number + 1 - internal_tx_indexer.confirmations,
                )
            ),
        )
        self.assertEqual(
            SafeRelevantTransaction.objects.count(), 2
        )  # 2 Ether Transfers

    def test_internal_tx_indexer(self):
        self._test_internal_tx_indexer()

    @mock.patch.object(
        TracingManager,
        "trace_blocks",
        autospec=True,
        return_value=trace_blocks_filtered_0x5aC2_result,
    )
    @mock.patch.object(
        TracingManager, "trace_filter", autospec=True, return_value=trace_filter_result
    )
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def test_find_relevant_elements(
        self,
        current_block_number_mock: MagicMock,
        trace_filter_mock: MagicMock,
        trace_block_mock: MagicMock,
    ):
        current_block_number = current_block_number_mock.return_value
        internal_tx_indexer = self.internal_tx_indexer
        addresses = {"0x5aC255889882aCd3da2aA939679E3f3d4cea221e"}
        trace_filter_transactions = OrderedDict(
            (trace["transactionHash"], None) for trace in trace_filter_mock.return_value
        )
        trace_block_transactions = OrderedDict(
            (
                (k, list(v))
                for k, v in itertools.groupby(
                    itertools.chain(*trace_block_mock.return_value),
                    lambda x: x["transactionHash"],
                )
            )
        )

        # Just trace filter
        elements = internal_tx_indexer.find_relevant_elements(
            addresses, 1, current_block_number - 50
        )
        self.assertDictEqual(trace_filter_transactions, elements)
        trace_filter_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing,
            from_block=1,
            to_block=current_block_number - 50,
            to_address=list(addresses),
        )
        trace_block_mock.assert_not_called()
        trace_filter_mock.reset_mock()

        # Mixed trace_block and trace_filter
        elements = internal_tx_indexer.find_relevant_elements(
            addresses, current_block_number - 50, current_block_number
        )
        self.assertDictEqual(
            trace_filter_transactions | trace_block_transactions, elements
        )
        trace_filter_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing,
            from_block=current_block_number - 50,
            to_block=current_block_number - internal_tx_indexer.number_trace_blocks,
            to_address=list(addresses),
        )

        trace_block_mock.assert_called_with(
            internal_tx_indexer.ethereum_client.tracing,
            list(
                range(
                    current_block_number - internal_tx_indexer.number_trace_blocks + 1,
                    current_block_number + 1,
                )
            ),
        )

        trace_filter_mock.reset_mock()
        trace_block_mock.reset_mock()

        # Just trace block
        elements = internal_tx_indexer.find_relevant_elements(
            addresses, current_block_number - 3, current_block_number
        )
        self.assertEqual(trace_block_transactions, elements)
        trace_filter_mock.assert_not_called()

        trace_block_mock.assert_called_with(
            internal_tx_indexer.ethereum_client.tracing,
            list(range(current_block_number - 3, current_block_number + 1)),
        )

    @mock.patch.object(TracingManager, "trace_blocks", autospec=True, return_value=[])
    @mock.patch.object(TracingManager, "trace_filter", autospec=True, return_value=[])
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def test_trace_filter_queries_all_master_copies(
        self,
        current_block_number_mock: MagicMock,
        trace_filter_mock: MagicMock,
        trace_block_mock: MagicMock,
    ):
        """
        Stock `trace_filter` searches traces `to` every master copy, so it finds the
        transactions of every Safe on the network
        """
        master_copy_addresses = {
            SafeMasterCopyFactory(tx_block_number=0).address for _ in range(3)
        }
        self.internal_tx_indexer.start()

        trace_filter_mock.assert_called()
        for call in trace_filter_mock.call_args_list:
            self.assertEqual(set(call.kwargs["to_address"]), master_copy_addresses)
            self.assertNotIn("from_address", call.kwargs)

    def test_trace_block_matches_action_from_or_to(self):
        """
        `trace_block` keeps every trace of the transactions with a trace `from` or `to`
        one of the addresses
        """
        block_traces = (
            mocks_mainnet_traces.exec_trace_transaction_result
            + mocks_mainnet_traces.incoming_ether_trace_transaction_result
        )
        exec_tx_hash = HexBytes(mocks_mainnet_traces.exec_tx_hash)
        incoming_ether_tx_hash = HexBytes(mocks_mainnet_traces.incoming_ether_tx_hash)
        safe_v1_3_0_singleton = "0xd9Db270c1B5E3Bd161E8c8503c55cEABeE709552"

        for addresses, expected_tx_hashes in (
            # Safe is the `from` of the delegatecall to its singleton
            ({mocks_mainnet_traces.exec_safe_address}, [exec_tx_hash]),
            # Both Safes delegatecall `to` the same singleton
            ({safe_v1_3_0_singleton}, [exec_tx_hash, incoming_ether_tx_hash]),
            ({"0x5aFE3855358E112B5647B952709E6165e1c1eEEe"}, []),
        ):
            with (
                self.subTest(addresses=addresses),
                mock.patch.object(
                    TracingManager,
                    "trace_blocks",
                    autospec=True,
                    return_value=[block_traces],
                ),
            ):
                elements = (
                    self.internal_tx_indexer._find_relevant_elements_using_trace_block(
                        addresses, 1, 1
                    )
                )
                self.assertEqual(list(elements.keys()), expected_tx_hashes)
                for tx_hash, traces in elements.items():
                    self.assertEqual(
                        traces,
                        [
                            trace
                            for trace in block_traces
                            if trace["transactionHash"] == tx_hash
                        ],
                    )

    @mock.patch.object(TracingManager, "trace_blocks", autospec=True, return_value=[])
    @mock.patch.object(TracingManager, "trace_filter", autospec=True, return_value=[])
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def test_find_relevant_elements_number_trace_blocks_zero(
        self,
        current_block_number_mock: MagicMock,
        trace_filter_mock: MagicMock,
        trace_block_mock: MagicMock,
    ):
        """
        `ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS=0` (no `trace_block` near the tip)
        """
        current_block_number = current_block_number_mock.return_value
        internal_tx_indexer = self.internal_tx_indexer
        internal_tx_indexer.number_trace_blocks = 0
        addresses = {"0x5aC255889882aCd3da2aA939679E3f3d4cea221e"}

        internal_tx_indexer.find_relevant_elements(
            addresses, 1, current_block_number - 1
        )
        trace_filter_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing,
            from_block=1,
            to_block=current_block_number - 1,
            to_address=list(addresses),
        )
        trace_block_mock.assert_not_called()
        trace_filter_mock.reset_mock()

        # Up to the current block: `trace_filter` to the current block, then
        # `trace_blocks` is still called, with no blocks
        internal_tx_indexer.find_relevant_elements(addresses, 1, current_block_number)
        trace_filter_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing,
            from_block=1,
            to_block=current_block_number,
            to_address=list(addresses),
        )
        trace_block_mock.assert_called_once_with(
            internal_tx_indexer.ethereum_client.tracing, []
        )

    @mock.patch.object(TracingManager, "trace_blocks", autospec=True, return_value=[])
    @mock.patch.object(TracingManager, "trace_filter", autospec=True, return_value=[])
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def test_trace_filter_whitelist_chunks(
        self,
        current_block_number_mock: MagicMock,
        trace_filter_mock: MagicMock,
        trace_block_mock: MagicMock,
    ):
        """
        With a whitelist, `trace_filter` searches the traces `from` the whitelisted
        Safes in chunks of `query_chunk_size`, instead of `to` every master copy
        """
        SafeMasterCopyFactory(tx_block_number=0)
        whitelisted_safes = frozenset(
            fast_to_checksum_address(os.urandom(20)) for _ in range(5)
        )
        internal_tx_indexer = self.internal_tx_indexer
        internal_tx_indexer.whitelisted_safes = whitelisted_safes

        for query_chunk_size, expected_calls in ((2, 3), (0, 1)):
            with self.subTest(query_chunk_size=query_chunk_size):
                internal_tx_indexer.query_chunk_size = query_chunk_size
                SafeMasterCopy.objects.update(tx_block_number=0)
                trace_filter_mock.reset_mock()
                internal_tx_indexer.start()

                self.assertEqual(trace_filter_mock.call_count, expected_calls)
                queried_addresses = []
                for call in trace_filter_mock.call_args_list:
                    self.assertNotIn("to_address", call.kwargs)
                    queried_addresses.extend(call.kwargs["from_address"])
                    if query_chunk_size:
                        self.assertLessEqual(
                            len(call.kwargs["from_address"]), query_chunk_size
                        )
                self.assertCountEqual(queried_addresses, whitelisted_safes)

    def test_trace_filter_whitelist_chunks_merged(self):
        """
        Chunk results are merged in block order, without duplicated transactions
        """

        def trace(block_number: int, transaction_position: int, tx_hash: str):
            return {
                "blockNumber": block_number,
                "transactionPosition": transaction_position,
                "transactionHash": HexBytes(tx_hash),
            }

        internal_tx_indexer = self.internal_tx_indexer
        internal_tx_indexer.whitelisted_safes = frozenset(
            fast_to_checksum_address(os.urandom(20)) for _ in range(2)
        )
        internal_tx_indexer.query_chunk_size = 1
        with mock.patch.object(
            TracingManager,
            "trace_filter",
            autospec=True,
            side_effect=[
                [trace(10, 3, "0x03"), trace(12, 0, "0x04")],
                # `0x03` involves both Safes
                [trace(9, 0, "0x01"), trace(10, 1, "0x02"), trace(10, 3, "0x03")],
            ],
        ) as trace_filter_mock:
            elements = internal_tx_indexer._find_relevant_elements_using_trace_filter(
                {"0x5aC255889882aCd3da2aA939679E3f3d4cea221e"}, 1, 100
            )
        self.assertEqual(trace_filter_mock.call_count, 2)
        self.assertEqual(
            list(elements.items()),
            [(HexBytes(tx_hash), None) for tx_hash in ("0x01", "0x02", "0x03", "0x04")],
        )

    @mock.patch.object(TracingManager, "trace_filter", autospec=True, return_value=[])
    @mock.patch.object(
        EthereumClient,
        "current_block_number",
        new_callable=PropertyMock,
        return_value=2000,
    )
    def test_reindex_master_copies_whitelist(
        self,
        current_block_number_mock: MagicMock,
        trace_filter_mock: MagicMock,
    ):
        """
        L1 reindex: the default path searches the whitelisted Safes, explicitly
        provided addresses are queried as in stock
        """
        master_copy = SafeMasterCopyFactory()
        whitelisted_safe = fast_to_checksum_address(os.urandom(20))
        index_service = IndexServiceProvider()
        with (
            self.settings(WHITELISTED_SAFES=frozenset({whitelisted_safe})),
            mock.patch.object(index_service, "eth_l2_network", False),
        ):
            index_service.reindex_master_copies(0, 10, block_process_limit=100)
            trace_filter_mock.assert_called_once_with(
                mock.ANY, from_block=0, to_block=10, from_address=[whitelisted_safe]
            )
            trace_filter_mock.reset_mock()

            for addresses in ([master_copy.address], [whitelisted_safe]):
                with self.subTest(addresses=addresses):
                    index_service.reindex_master_copies(
                        0, 10, block_process_limit=100, addresses=addresses
                    )
                    trace_filter_mock.assert_called_once_with(
                        mock.ANY, from_block=0, to_block=10, to_address=addresses
                    )
                    trace_filter_mock.reset_mock()

    def test_process_elements_whitelist_mainnet_txs(self):
        """
        Real Mainnet txs with traces of other contracts: only the traces of the
        whitelisted Safe are stored
        """
        for case in ("incoming_ether", "module"):
            safe_address = getattr(mocks_mainnet_traces, f"{case}_safe_address")
            tx_hash = HexBytes(getattr(mocks_mainnet_traces, f"{case}_tx_hash"))
            traces = getattr(mocks_mainnet_traces, f"{case}_trace_transaction_result")
            EthereumTxFactory(tx_hash=tx_hash, status=1)

            for whitelisted_safes, expected_internal_txs in (
                (frozenset(), 5),  # Stock
                (frozenset({safe_address}), 1),
            ):
                with self.subTest(case=case, whitelisted_safes=whitelisted_safes):
                    InternalTx.objects.all().delete()
                    self.internal_tx_indexer.element_already_processed_checker.clear()
                    self.internal_tx_indexer.whitelisted_safes = whitelisted_safes
                    self.internal_tx_indexer.process_elements({tx_hash: traces})
                    self.assertEqual(InternalTx.objects.count(), expected_internal_txs)
                    if whitelisted_safes:
                        internal_tx = InternalTx.objects.get()
                        self.assertIn(safe_address, (internal_tx._from, internal_tx.to))

    def test_process_elements_whitelist_other_safe(self):
        """
        A tx with a whitelisted Safe and a non-whitelisted Safe: nothing is stored or
        decoded for the non-whitelisted one
        """
        whitelisted_safe = mocks_mainnet_traces.exec_safe_address
        other_safe = fast_to_checksum_address(os.urandom(20))
        tx_hash = HexBytes(mocks_mainnet_traces.exec_tx_hash)
        exec_traces = mocks_mainnet_traces.exec_trace_transaction_result
        safe_delegate_call = next(
            trace
            for trace in exec_traces
            if trace["action"].get("callType") == "delegatecall"
        )
        other_safe_delegate_call = {
            **safe_delegate_call,
            "action": {**safe_delegate_call["action"], "from": other_safe},
            "traceAddress": [9],
        }
        ether_to_other_safe = {
            **safe_delegate_call,
            "action": {
                "from": fast_to_checksum_address(os.urandom(20)),
                "callType": "call",
                "gas": 2300,
                "input": HexBytes("0x"),
                "to": other_safe,
                "value": 5,
            },
            "subtraces": 0,
            "traceAddress": [10],
        }
        traces = exec_traces + [other_safe_delegate_call, ether_to_other_safe]
        EthereumTxFactory(tx_hash=tx_hash, status=1)

        self.internal_tx_indexer.whitelisted_safes = frozenset({whitelisted_safe})
        self.internal_tx_indexer.process_elements({tx_hash: traces})

        self.assertFalse(InternalTx.objects.filter(_from=other_safe).exists())
        self.assertFalse(InternalTx.objects.filter(to=other_safe).exists())
        self.assertTrue(InternalTx.objects.filter(_from=whitelisted_safe).exists())
        self.assertEqual(
            list(InternalTxDecoded.objects.values_list("safe_address", flat=True)),
            [whitelisted_safe],
        )
        self.assertFalse(
            SafeRelevantTransaction.objects.filter(safe=other_safe).exists()
        )

    def test_process_elements_whitelist_no_traces_left(self):
        """
        A tx with no traces of whitelisted Safes is not fetched. As in stock, it is
        marked as processed only if another tx of the batch is processed
        """
        m = mocks_mainnet_traces
        other_tx_hash = HexBytes(m.exec_tx_hash)
        other_traces = m.exec_trace_transaction_result
        tx_hash = HexBytes(m.incoming_ether_tx_hash)
        traces = m.incoming_ether_trace_transaction_result
        EthereumTxFactory(tx_hash=tx_hash, status=1)
        internal_tx_indexer = self.internal_tx_indexer
        internal_tx_indexer.whitelisted_safes = frozenset(
            {m.incoming_ether_safe_address}
        )

        with mock.patch.object(
            internal_tx_indexer,
            "_prefetch_ethereum_txs",
            wraps=internal_tx_indexer._prefetch_ethereum_txs,
        ) as prefetch_ethereum_txs_mock:
            # Only the not whitelisted tx: nothing to process, nothing marked
            self.assertEqual(
                internal_tx_indexer.process_elements({other_tx_hash: other_traces}), []
            )
            prefetch_ethereum_txs_mock.assert_not_called()
            self.assertFalse(
                internal_tx_indexer._is_processed(
                    other_tx_hash, other_traces[0]["blockHash"]
                )
            )

            self.assertEqual(
                internal_tx_indexer.process_elements(
                    {other_tx_hash: other_traces, tx_hash: traces}
                ),
                [tx_hash],
            )
            prefetch_ethereum_txs_mock.assert_called_once_with([tx_hash])

        self.assertEqual(InternalTx.objects.count(), 1)
        self.assertTrue(
            internal_tx_indexer._is_processed(
                other_tx_hash, other_traces[0]["blockHash"]
            )
        )

    def test_trace_block_whitelist(self):
        """
        With a whitelist, `trace_block` keeps the txs of the whitelisted Safes instead of
        the txs of every Safe using the provided master copies
        """
        m = mocks_mainnet_traces
        block_traces = (
            m.exec_trace_transaction_result + m.incoming_ether_trace_transaction_result
        )
        exec_tx_hash = HexBytes(m.exec_tx_hash)
        incoming_ether_tx_hash = HexBytes(m.incoming_ether_tx_hash)
        master_copies = {"0xd9Db270c1B5E3Bd161E8c8503c55cEABeE709552"}

        InternalTxIndexerProvider.del_singleton()
        with self.settings(ETH_INTERNAL_NO_FILTER=True):
            trace_block_indexer = InternalTxIndexerProvider()
        self.assertIsInstance(trace_block_indexer, InternalTxIndexerWithTraceBlock)

        for internal_tx_indexer, ignore_addresses, expected_tx_hashes in (
            (self.internal_tx_indexer, True, [exec_tx_hash]),
            (trace_block_indexer, True, [exec_tx_hash]),
            # Explicitly provided addresses (`reindex --addresses`): stock
            (
                self.internal_tx_indexer,
                False,
                [exec_tx_hash, incoming_ether_tx_hash],
            ),
        ):
            with (
                self.subTest(
                    indexer=internal_tx_indexer.__class__.__name__,
                    ignore_addresses=ignore_addresses,
                ),
                mock.patch.object(
                    TracingManager,
                    "trace_blocks",
                    autospec=True,
                    return_value=[block_traces],
                ),
            ):
                internal_tx_indexer.whitelisted_safes = frozenset({m.exec_safe_address})
                internal_tx_indexer.IGNORE_ADDRESSES_ON_LOG_FILTER = ignore_addresses
                elements = internal_tx_indexer.find_relevant_elements(
                    master_copies, 1, 1, current_block_number=1
                )
                self.assertEqual(list(elements.keys()), expected_tx_hashes)

    def test_tx_processor_using_internal_tx_indexer(self):
        self._test_internal_tx_indexer()
        tx_processor = SafeTxProcessorProvider()
        self.assertEqual(InternalTxDecoded.objects.count(), 2)  # Setup and execute tx
        internal_txs_decoded = list(InternalTxDecoded.objects.pending_for_safes())
        self.assertEqual(len(internal_txs_decoded), 2)
        self.assertEqual(MultisigTransaction.objects.count(), 0)
        number_processed = tx_processor.process_decoded_transactions(
            internal_txs_decoded
        )  # Index using `setup` trace
        self.assertEqual(MultisigTransaction.objects.count(), 1)
        self.assertEqual(len(number_processed), 2)  # Setup and execute trace
        self.assertEqual(SafeContract.objects.count(), 1)
        self.assertEqual(SafeStatus.objects.count(), 2)

        safe_status = SafeStatus.objects.first()
        self.assertEqual(len(safe_status.owners), 1)
        self.assertEqual(safe_status.nonce, 0)
        self.assertEqual(safe_status.threshold, 1)

        safe_status = SafeStatus.objects.last()
        self.assertEqual(len(safe_status.owners), 1)
        self.assertEqual(safe_status.nonce, 1)
        self.assertEqual(safe_status.threshold, 1)

        # Multisig Transaction was indexed
        self.assertEqual(SafeRelevantTransaction.objects.count(), 3)

        # Try to decode again without new traces, nothing should be decoded
        internal_txs_decoded = list(InternalTxDecoded.objects.pending_for_safes())
        self.assertEqual(
            len(internal_txs_decoded), 0
        )  # Safe indexed, execute tx can be decoded now
        number_processed = tx_processor.process_decoded_transactions(
            list(internal_txs_decoded)
        )
        self.assertEqual(len(number_processed), 0)  # Setup trace

        safe_last_status = SafeLastStatus.objects.get()
        self.assertEqual(
            safe_last_status, SafeLastStatus.from_status_instance(safe_status)
        )

    def test_tx_processor_using_internal_tx_indexer_with_existing_safe(self):
        self._test_internal_tx_indexer()
        tx_processor = SafeTxProcessorProvider()
        tx_processor.process_decoded_transactions(
            list(InternalTxDecoded.objects.pending_for_safes())
        )
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number, 0
        )

        SafeStatus.objects.all().delete()
        InternalTxDecoded.objects.update(processed=False)
        internal_txs_decoded = list(InternalTxDecoded.objects.pending_for_safes())
        self.assertEqual(len(internal_txs_decoded), 2)
        self.assertEqual(internal_txs_decoded[0].function_name, "setup")
        results = tx_processor.process_decoded_transactions(internal_txs_decoded)
        self.assertEqual(results, [True, True])

    def test_element_already_processed_checker(self):
        """
        Test not reprocessing of processed events
        """

        # Transform mock to dictionary tx_hash -> traces
        tx_hash_with_traces = {}
        for trace_transaction_result in trace_transactions_result:
            tx_hash = trace_transaction_result[0]["transactionHash"]
            tx_hash_with_traces[tx_hash] = trace_transaction_result
            # Create transaction in db so not fetching of transaction is needed
            EthereumTxFactory(tx_hash=tx_hash)

        # After the first processing transactions will be cached to prevent reprocessing
        processed_element_cache = self.internal_tx_indexer.element_already_processed_checker._processed_element_cache
        self.assertEqual(len(processed_element_cache), 0)
        self.assertEqual(
            len(self.internal_tx_indexer.process_elements(tx_hash_with_traces)), 2
        )
        self.assertEqual(len(processed_element_cache), 2)

        # Transactions are cached and will not be reprocessed
        self.assertEqual(
            len(self.internal_tx_indexer.process_elements(tx_hash_with_traces)), 0
        )
        self.assertEqual(
            len(self.internal_tx_indexer.process_elements(tx_hash_with_traces)), 0
        )

        # Cleaning the cache will reprocess the transactions again
        self.internal_tx_indexer.element_already_processed_checker.clear()
        self.assertEqual(
            len(self.internal_tx_indexer.process_elements(tx_hash_with_traces)), 2
        )
