"""
Characterize how stock indexers react when the RPC provider rejects `eth_getLogs`
requests, using the fault-injection proxy in `scripts/rpc_logging_proxy.py` in front
of the test node. No production behaviour is changed here.
"""

import os
import sys
import threading
from unittest import mock

from django.conf import settings
from django.test import TestCase

import requests
from safe_eth.eth import EthereumClient
from web3.exceptions import Web3RPCError
from web3.types import RPCEndpoint

from ..indexers import ProxyFactoryIndexer
from ..indexers.ethereum_indexer import FindRelevantElementsException
from ..models import ProxyFactory
from ..services import IndexingException, IndexService, IndexServiceProvider
from .factories import ProxyFactoryFactory

# `scripts/` is not a package
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")
    ),
)

from rpc_logging_proxy import ProxyConfig, RpcLoggingProxy


class TestProviderRejection(TestCase):
    BLOCKS_BEHIND = 20

    def setUp(self):
        self.ethereum_client = EthereumClient(settings.ETHEREUM_NODE_URL)
        current_block_number = self.ethereum_client.current_block_number
        if current_block_number < self.BLOCKS_BEHIND + 10:
            self.ethereum_client.w3.provider.make_request(
                RPCEndpoint("evm_mine"),
                [{"blocks": self.BLOCKS_BEHIND + 10 - current_block_number}],
            )
        # Pinned, so the target block doesn't move during the test
        self.current_block_number = self.ethereum_client.current_block_number
        self.start_block_number = self.current_block_number - self.BLOCKS_BEHIND

        # `EthereumIndexer.__init__` replaces the client of the `IndexService` singleton
        index_service: IndexService = IndexServiceProvider()
        original_ethereum_client = index_service.ethereum_client
        self.addCleanup(
            setattr, index_service, "ethereum_client", original_ethereum_client
        )

    def start_proxy(self, **limits) -> RpcLoggingProxy:
        proxy = RpcLoggingProxy(
            ("127.0.0.1", 0),
            ProxyConfig(upstream=settings.ETHEREUM_NODE_URL, **limits),
        )
        threading.Thread(target=proxy.serve_forever, daemon=True).start()
        self.addCleanup(proxy.server_close)
        self.addCleanup(proxy.shutdown)
        return proxy

    def build_indexer(self, proxy: RpcLoggingProxy, **kwargs) -> ProxyFactoryIndexer:
        kwargs.setdefault("block_process_limit", 10)
        kwargs.setdefault("block_process_limit_max", 0)
        kwargs.setdefault("query_chunk_size", 1_000)
        return ProxyFactoryIndexer(
            EthereumClient(proxy.url),
            confirmations=0,
            blocks_to_reindex_again=0,
            **kwargs,
        )

    def create_proxy_factory(self) -> ProxyFactory:
        return ProxyFactoryFactory.create(tx_block_number=self.start_block_number)

    def process(self, indexer: ProxyFactoryIndexer, *proxy_factories: ProxyFactory):
        return indexer.process_addresses(
            {proxy_factory.address for proxy_factory in proxy_factories},
            current_block_number=self.current_block_number,
        )

    def marker(self, proxy_factory: ProxyFactory) -> int:
        proxy_factory.refresh_from_db()
        return proxy_factory.tx_block_number

    def fast_runs(self):
        """
        Every `auto_adjust_block_limit` measurement takes 0 seconds, so the limit doubles
        """
        return mock.patch(
            "safe_transaction_service.history.indexers.ethereum_indexer.time.time",
            return_value=0,
        )

    def assert_jsonrpc_rejection(
        self, indexer: ProxyFactoryIndexer, proxy_factory: ProxyFactory
    ) -> None:
        """
        Case 1: a JSON-RPC error response escapes as `Web3RPCError`, not wrapped as
        `FindRelevantElementsException`, and the block process limit drops to 1
        """
        with self.assertRaises(Web3RPCError) as context:
            self.process(indexer, proxy_factory)
        self.assertNotIsInstance(context.exception, FindRelevantElementsException)
        self.assertNotIsInstance(context.exception, IndexingException)
        self.assertIn("max-block-range exceeded", str(context.exception))
        self.assertEqual(indexer.block_process_limit, 1)
        self.assertEqual(self.marker(proxy_factory), self.start_block_number)

    def test_jsonrpc_rejection(self):
        proxy = self.start_proxy(max_block_range=5)
        indexer = self.build_indexer(proxy, block_process_limit=10)
        proxy_factory = self.create_proxy_factory()

        self.assert_jsonrpc_rejection(indexer, proxy_factory)
        self.assertEqual(
            proxy.stats.summary()["rejected"], {"eth_getLogs": {"max-block-range": 1}}
        )

    def test_http_rejection(self):
        for reject_style in ("http413", "http429"):
            with self.subTest(reject_style=reject_style):
                proxy = self.start_proxy(max_block_range=5, reject_style=reject_style)
                indexer = self.build_indexer(proxy, block_process_limit=10)
                proxy_factory = self.create_proxy_factory()

                with self.assertRaises(FindRelevantElementsException) as context:
                    self.process(indexer, proxy_factory)
                cause = context.exception.__cause__
                self.assertIsInstance(cause, OSError)
                self.assertIs(type(cause), requests.HTTPError)
                self.assertEqual(indexer.block_process_limit, 1)
                self.assertEqual(self.marker(proxy_factory), self.start_block_number)
                # web3 `HTTPProvider` default `ExceptionRetryConfiguration` retries
                # `requests.HTTPError` for `eth_getLogs`: 5 attempts in total
                self.assertEqual(
                    proxy.stats.summary()["methods"]["eth_getLogs"]["requests"], 5
                )

    def test_regrow_oscillates(self):
        proxy = self.start_proxy(max_block_range=5)
        indexer = self.build_indexer(proxy, block_process_limit=10)
        proxy_factory = self.create_proxy_factory()
        self.assert_jsonrpc_rejection(indexer, proxy_factory)

        with self.fast_runs():
            for expected_limit in (2, 4, 8):
                self.process(indexer, proxy_factory)
                self.assertEqual(indexer.block_process_limit, expected_limit)

            # 1 + 2 + 4 blocks were processed
            self.assertEqual(self.marker(proxy_factory), self.start_block_number + 7)

            # The 8-block range is rejected again, back to 1
            with self.assertRaises(Web3RPCError):
                self.process(indexer, proxy_factory)
            self.assertEqual(indexer.block_process_limit, 1)
            self.assertEqual(self.marker(proxy_factory), self.start_block_number + 7)

        self.assertEqual(
            proxy.stats.summary()["rejected"], {"eth_getLogs": {"max-block-range": 2}}
        )

    def test_block_process_limit_max_prevents_oscillation(self):
        proxy = self.start_proxy(max_block_range=5)
        indexer = self.build_indexer(
            proxy, block_process_limit=1, block_process_limit_max=5
        )
        proxy_factory = self.create_proxy_factory()

        updated = False
        with self.fast_runs():
            for _ in range(self.BLOCKS_BEHIND + 1):
                *_, updated = self.process(indexer, proxy_factory)
                self.assertLessEqual(indexer.block_process_limit, 5)
                if updated:
                    break

        self.assertTrue(updated)
        self.assertEqual(self.marker(proxy_factory), self.current_block_number + 1)
        summary = proxy.stats.summary()
        self.assertEqual(summary["rejected"], {})
        self.assertLessEqual(
            max(int(span) for span in summary["eth_getLogs"]["block_span"]), 5
        )

    def test_address_count_rejection(self):
        proxy = self.start_proxy(max_addresses=1)
        proxy_factories = [self.create_proxy_factory() for _ in range(2)]

        # Shrinking the block range doesn't help with too many addresses
        indexer = self.build_indexer(proxy, block_process_limit=1, query_chunk_size=10)
        for _ in range(3):
            with self.assertRaises(Web3RPCError):
                self.process(indexer, *proxy_factories)
            self.assertEqual(indexer.block_process_limit, 1)
        for proxy_factory in proxy_factories:
            self.assertEqual(self.marker(proxy_factory), self.start_block_number)
        self.assertEqual(
            proxy.stats.summary()["rejected"], {"eth_getLogs": {"max-addresses": 3}}
        )

        # One address per request works
        indexer = self.build_indexer(proxy, block_process_limit=1, query_chunk_size=1)
        self.process(indexer, *proxy_factories)
        for proxy_factory in proxy_factories:
            self.assertEqual(self.marker(proxy_factory), self.start_block_number + 1)
