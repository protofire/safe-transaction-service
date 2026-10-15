"""
Differential correctness on L1 (trace indexing): the C27 scenario indexed as stock
(`trace_filter` to the master copies), with a whitelist (live, `trace_filter` from the
whitelisted Safes, `trace_block` near the end of the range) and with a whitelist
(backfill). Whitelisted Safes must get the same rows and API responses as stock.

Needs a node with `trace_*` support for both URLs, so it runs on its own (the Safe test
contracts are deployed once per process)::

    docker compose --profile develop up -d anvil
    WHITELIST_DIFF_L1=1 \\
    ETHEREUM_NODE_URL=http://localhost:8546 \\
    ETHEREUM_TRACING_NODE_URL=http://localhost:8546 \\
      pytest safe_transaction_service/history/tests/test_whitelist_differential_l1.py
"""

import os
import unittest
from unittest import mock

from django.conf import settings

from safe_eth.eth import EthereumClient
from safe_eth.eth.ethereum_client import TracingManager

from ..indexers import InternalTxIndexer
from ..services import IndexServiceProvider
from . import test_whitelist_differential
from .differential import Snapshot

# `trace_block` for the last blocks of the range, so it's indexed by both `trace_filter`
# and `trace_block`
NUMBER_TRACE_BLOCKS = 10
# anvil rejects `trace_filter` ranges over 300 blocks (block range too large)
TRACE_FILTER_MAX_BLOCKS = 300


@unittest.skipUnless(
    os.environ.get("WHITELIST_DIFF_L1") == "1",
    "Set WHITELIST_DIFF_L1=1 and point the node URLs to a tracing node (anvil)",
)
class TestWhitelistDifferentialL1(
    test_whitelist_differential.TestWhitelistDifferential
):
    L2_NETWORK = False

    def build_safe_indexer(self, **kwargs) -> InternalTxIndexer:
        internal_tx_indexer = InternalTxIndexer(
            EthereumClient(settings.ETHEREUM_TRACING_NODE_URL),
            block_process_limit=TRACE_FILTER_MAX_BLOCKS,
            block_process_limit_max=TRACE_FILTER_MAX_BLOCKS,
            **kwargs,
        )
        internal_tx_indexer.number_trace_blocks = NUMBER_TRACE_BLOCKS
        return internal_tx_indexer

    def after_safe_indexing(self) -> None:
        """
        On L1, `SafeContract` rows are created when processing the decoded `setup`
        traces (the processing task runs continuously in production)
        """
        IndexServiceProvider().process_all_decoded_txs()

    def assert_differential(self, name: str, variant: tuple[Snapshot, Snapshot]):
        """
        No expected differences on L1: the ether sent by a non-whitelisted Safe is a
        trace `to` the whitelisted Safe, stored in every variant
        """
        stock_tables, stock_api = self.get_stock()
        variant_tables, variant_api = variant
        self.assertTrue(stock_tables["InternalTx"])
        self.assertTrue(stock_tables["MultisigTransaction"])
        self.assertTrue(stock_tables["ModuleTransaction"])
        self.assert_no_diff("stock", stock_tables, name, variant_tables)
        self.assert_no_diff("stock", stock_api, name, variant_api)

    def test_whitelist_queries(self):
        """
        Stock searches the traces `to` the master copies, whitelist mode `from` the
        whitelisted Safes. `trace_block` is used near the end of the range in both
        """
        for whitelisted_safes in (frozenset(), self.chain.whitelisted_safes):
            with (
                self.subTest(whitelist=bool(whitelisted_safes)),
                mock.patch.object(
                    TracingManager,
                    "trace_filter",
                    autospec=True,
                    side_effect=TracingManager.trace_filter,
                ) as trace_filter_mock,
                mock.patch.object(
                    TracingManager,
                    "trace_blocks",
                    autospec=True,
                    side_effect=TracingManager.trace_blocks,
                ) as trace_blocks_mock,
            ):
                self.run_live(whitelisted_safes)
                self.assertTrue(trace_filter_mock.call_args_list)
                for call in trace_filter_mock.call_args_list:
                    if whitelisted_safes:
                        self.assertNotIn("to_address", call.kwargs)
                        self.assertLessEqual(
                            set(call.kwargs["from_address"]), whitelisted_safes
                        )
                    else:
                        self.assertNotIn("from_address", call.kwargs)
                        self.assertEqual(
                            set(call.kwargs["to_address"]),
                            set(self.chain.singletons.values()),
                        )
                self.assertTrue(
                    any(call.args[1] for call in trace_blocks_mock.call_args_list)
                )
