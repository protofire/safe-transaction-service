from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from eth_account import Account
from safe_eth.eth import EthereumClient

from ..indexers import ProxyFactoryIndexer
from ..services import IndexServiceProvider


class TestEventsIndexer(SimpleTestCase):
    """
    Address-filtered `eth_getLogs` queries, using `ProxyFactoryIndexer` as the simplest
    `EventsIndexer` (`IGNORE_ADDRESSES_ON_LOG_FILTER = False`)
    """

    def setUp(self):
        # `EthereumIndexer.__init__` replaces the client of the `IndexService` singleton
        index_service = IndexServiceProvider()
        self.addCleanup(
            setattr, index_service, "ethereum_client", index_service.ethereum_client
        )
        self.indexer = ProxyFactoryIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))
        self.addresses = [Account.create().address for _ in range(3)]
        get_logs_patcher = mock.patch.object(
            self.indexer.ethereum_client.slow_w3.eth, "get_logs", return_value=[]
        )
        self.get_logs_mock = get_logs_patcher.start()
        self.addCleanup(get_logs_patcher.stop)

    def get_logs_parameters(self) -> list[dict]:
        return [call.args[0] for call in self.get_logs_mock.call_args_list]

    def test_get_logs_for_addresses_chunks(self):
        self.indexer.query_chunk_size = 2
        self.indexer._get_logs_for_addresses(set(self.addresses), 10, 20)

        parameters = self.get_logs_parameters()
        self.assertEqual(len(parameters), 2)
        self.assertCountEqual(
            [address for p in parameters for address in p["address"]],
            self.addresses,
        )
        expected_topics = [list(self.indexer.events_to_listen.keys())]
        for p in parameters:
            self.assertEqual(p["fromBlock"], 10)
            self.assertEqual(p["toBlock"], 20)
            self.assertEqual(p["topics"], expected_topics)

    def test_get_logs_for_addresses_no_chunking(self):
        self.indexer.query_chunk_size = 0
        self.indexer._get_logs_for_addresses(set(self.addresses), 10, 20)

        parameters = self.get_logs_parameters()
        self.assertEqual(len(parameters), 1)
        self.assertCountEqual(parameters[0]["address"], self.addresses)

    def test_get_logs_for_addresses_joins_results(self):
        self.indexer.query_chunk_size = 1
        self.get_logs_mock.side_effect = lambda p: [f"log-{p['address'][0]}"]

        self.assertCountEqual(
            self.indexer._get_logs_for_addresses(set(self.addresses), 10, 20),
            [f"log-{address}" for address in self.addresses],
        )

    def test_do_node_query_uses_addresses(self):
        with mock.patch.object(
            self.indexer, "_get_logs_for_addresses", return_value=["log"]
        ) as get_logs_for_addresses_mock:
            self.assertEqual(
                self.indexer._do_node_query(set(self.addresses), 10, 20), ["log"]
            )
        get_logs_for_addresses_mock.assert_called_once_with(set(self.addresses), 10, 20)

    def test_do_node_query_ignoring_addresses(self):
        self.indexer.IGNORE_ADDRESSES_ON_LOG_FILTER = True
        with mock.patch.object(
            self.indexer, "_get_logs_for_addresses"
        ) as get_logs_for_addresses_mock:
            self.indexer._do_node_query(set(self.addresses), 10, 20)
        get_logs_for_addresses_mock.assert_not_called()

        parameters = self.get_logs_parameters()
        self.assertEqual(len(parameters), 1)
        self.assertNotIn("address", parameters[0])
        self.assertEqual(
            parameters[0]["topics"], [list(self.indexer.events_to_listen.keys())]
        )
