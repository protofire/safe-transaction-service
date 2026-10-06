from unittest import mock

from django.test import TestCase, override_settings

from eth_abi import encode as encode_abi
from eth_account import Account
from hexbytes import HexBytes
from safe_eth.eth.tests.ethereum_test_case import EthereumTestCaseMixin

from ..indexers import Erc20EventsIndexer, Erc20EventsIndexerProvider
from ..models import (
    ERC20Transfer,
    ERC721Transfer,
    EthereumTx,
    IndexingStatus,
    SafeRelevantTransaction,
)
from .factories import EthereumTxFactory, SafeContractFactory
from .mocks.mocks_erc20_events_indexer import log_receipt_mock


class TestErc20EventsIndexer(EthereumTestCaseMixin, TestCase):
    def setUp(self) -> None:
        Erc20EventsIndexerProvider.del_singleton()
        self.erc20_events_indexer = Erc20EventsIndexerProvider()

    def tearDown(self) -> None:
        Erc20EventsIndexerProvider.del_singleton()

    def test_erc20_events_indexer(self):
        erc20_events_indexer = self.erc20_events_indexer
        erc20_events_indexer.confirmations = 0
        self.assertEqual(erc20_events_indexer.start(), (0, 0))

        account = self.ethereum_test_account
        amount = 10
        erc20_contract = self.deploy_example_erc20(amount, account.address)

        safe_contract = SafeContractFactory()
        IndexingStatus.objects.set_erc20_721_indexing_status(0)
        tx_hash = self.ethereum_client.erc20.send_tokens(
            safe_contract.address, amount, erc20_contract.address, account.key
        )

        self.assertFalse(EthereumTx.objects.filter(tx_hash=tx_hash).exists())
        self.assertFalse(
            ERC20Transfer.objects.tokens_used_by_address(safe_contract.address)
        )
        self.assertEqual(SafeRelevantTransaction.objects.count(), 0)
        self.assertEqual(
            erc20_events_indexer.start(),
            (1, self.ethereum_client.current_block_number + 1),
        )

        # Store one entry for the sender and other for the receiver
        self.assertEqual(SafeRelevantTransaction.objects.count(), 2)
        self.assertEqual(
            SafeRelevantTransaction.objects.filter(
                safe=safe_contract.address, ethereum_tx_id=tx_hash
            ).count(),
            1,
        )

        # Erc20/721 last indexed block number is stored on IndexingStatus
        self.assertGreater(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number, 0
        )

        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number,
            self.ethereum_client.current_block_number
            - erc20_events_indexer.confirmations
            + 1,
        )
        self.assertTrue(EthereumTx.objects.filter(tx_hash=tx_hash).exists())
        self.assertTrue(
            ERC20Transfer.objects.tokens_used_by_address(safe_contract.address)
        )

        self.assertEqual(
            ERC20Transfer.objects.to_or_from(safe_contract.address).count(), 1
        )

        block_number = self.ethereum_client.get_transaction(tx_hash)["blockNumber"]
        event = self.ethereum_client.erc20.get_total_transfer_history(
            from_block=block_number, to_block=block_number
        )[0]
        self.assertIn("value", event["args"])

    def test_element_already_processed_checker(self):
        # Create transaction in db so not fetching of transaction is needed
        for log_receipt in log_receipt_mock:
            tx_hash = log_receipt["transactionHash"]
            block_hash = log_receipt["blockHash"]
            EthereumTxFactory(tx_hash=tx_hash, block__block_hash=block_hash)

        # After the first processing transactions will be cached to prevent reprocessing
        processed_element_cache = self.erc20_events_indexer.element_already_processed_checker._processed_element_cache
        self.assertEqual(len(processed_element_cache), 0)
        self.assertEqual(
            len(self.erc20_events_indexer.process_elements(log_receipt_mock)), 1
        )
        self.assertEqual(len(processed_element_cache), 1)

        # Transactions are cached and will not be reprocessed
        self.assertEqual(
            len(self.erc20_events_indexer.process_elements(log_receipt_mock)), 0
        )
        self.assertEqual(
            len(self.erc20_events_indexer.process_elements(log_receipt_mock)), 0
        )

        # Cleaning the cache will reprocess the transactions again
        self.erc20_events_indexer.element_already_processed_checker.clear()
        self.assertEqual(
            len(self.erc20_events_indexer.process_elements(log_receipt_mock)), 1
        )

    def test_get_almost_updated_addresses(self):
        self.assertIsNone(self.erc20_events_indexer.addresses_cache)
        self.assertEqual(
            self.erc20_events_indexer.get_almost_updated_addresses(0), set()
        )
        self.assertIsNone(self.erc20_events_indexer.addresses_cache)

        safe_contract_1 = SafeContractFactory()
        safe_contract_2 = SafeContractFactory()
        self.assertGreaterEqual(safe_contract_2.created, safe_contract_1.created)

        expected_addresses = {safe_contract_1.address, safe_contract_2.address}
        self.assertEqual(
            self.erc20_events_indexer.get_almost_updated_addresses(0),
            expected_addresses,
        )
        self.assertIsNotNone(self.erc20_events_indexer.addresses_cache)
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.last_checked,
            safe_contract_2.created,
        )
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.addresses, expected_addresses
        )

        # Add a new address to the database
        safe_contract_3 = SafeContractFactory()
        self.assertGreater(safe_contract_3.created, safe_contract_2.created)

        expected_addresses.add(safe_contract_3.address)
        self.assertEqual(
            self.erc20_events_indexer.get_almost_updated_addresses(0),
            expected_addresses,
        )
        self.assertIsNotNone(self.erc20_events_indexer.addresses_cache)
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.last_checked,
            safe_contract_3.created,
        )
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.addresses, expected_addresses
        )

        # Calling the function again, without adding a new address to the DB, should yield the same results
        self.assertEqual(
            self.erc20_events_indexer.get_almost_updated_addresses(0),
            expected_addresses,
        )
        self.assertIsNotNone(self.erc20_events_indexer.addresses_cache)
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.last_checked,
            safe_contract_3.created,
        )
        self.assertEqual(
            self.erc20_events_indexer.addresses_cache.addresses, expected_addresses
        )

    def _build_whitelisted_indexer(self, whitelisted_safes, **kwargs):
        with override_settings(WHITELISTED_SAFES=frozenset(whitelisted_safes)):
            return Erc20EventsIndexer(self.ethereum_client, confirmations=0, **kwargs)

    def _spy_transfer_history(self, indexer: Erc20EventsIndexer, **kwargs):
        erc20_manager = indexer.ethereum_client.erc20
        kwargs.setdefault("wraps", erc20_manager.get_total_transfer_history)
        return mock.patch.object(erc20_manager, "get_total_transfer_history", **kwargs)

    def test_erc20_events_indexer_whitelist(self):
        account = self.ethereum_test_account
        erc20_contract = self.deploy_example_erc20(100, account.address)
        whitelisted_safe = SafeContractFactory()
        other_safe = SafeContractFactory()
        IndexingStatus.objects.set_erc20_721_indexing_status(
            self.ethereum_client.current_block_number + 1
        )
        for safe_contract in (whitelisted_safe, other_safe):
            self.ethereum_client.erc20.send_tokens(
                safe_contract.address, 10, erc20_contract.address, account.key
            )

        indexer = self._build_whitelisted_indexer({whitelisted_safe.address})
        with self._spy_transfer_history(indexer) as get_transfer_history_mock:
            indexer.start()

        self.assertEqual(
            ERC20Transfer.objects.to_or_from(whitelisted_safe.address).count(), 1
        )
        self.assertEqual(
            ERC20Transfer.objects.to_or_from(other_safe.address).count(), 0
        )
        get_transfer_history_mock.assert_called()
        for call in get_transfer_history_mock.call_args_list:
            self.assertEqual(list(call.args[0]), [whitelisted_safe.address])

    def test_erc20_events_indexer_whitelist_transfer_between_chunks(self):
        sender = self.ethereum_test_account
        receiver = Account.create()
        erc20_contract = self.deploy_example_erc20(100, sender.address)
        SafeContractFactory(address=sender.address)
        SafeContractFactory(address=receiver.address)
        IndexingStatus.objects.set_erc20_721_indexing_status(
            self.ethereum_client.current_block_number + 1
        )
        tx_hash = self.ethereum_client.erc20.send_tokens(
            receiver.address, 10, erc20_contract.address, sender.key
        )

        indexer = self._build_whitelisted_indexer(
            {sender.address, receiver.address}, query_chunk_size=1
        )
        with self._spy_transfer_history(indexer) as get_transfer_history_mock:
            indexer.start()

        # Found by the `from` query of one chunk and the `to` query of the other
        self.assertEqual(get_transfer_history_mock.call_count, 2)
        self.assertEqual(ERC20Transfer.objects.filter(ethereum_tx=tx_hash).count(), 1)

    def test_whitelist_do_node_query_chunks(self):
        addresses = [Account.create().address for _ in range(5)]
        for whitelist, query_chunk_size, query_addresses, expected_calls in (
            (addresses, 2, addresses, 3),
            (addresses, 0, addresses, 1),
            (addresses, 2, [], 0),
        ):
            with self.subTest(
                query_chunk_size=query_chunk_size, query_addresses=query_addresses
            ):
                indexer = self._build_whitelisted_indexer(
                    whitelist, query_chunk_size=query_chunk_size
                )
                with self._spy_transfer_history(
                    indexer, wraps=None, return_value=[]
                ) as get_transfer_history_mock:
                    self.assertEqual(
                        indexer._do_node_query(set(query_addresses), 10, 20), []
                    )
                self.assertEqual(get_transfer_history_mock.call_count, expected_calls)
                chunks = [
                    call.args[0] for call in get_transfer_history_mock.call_args_list
                ]
                for chunk in chunks:
                    self.assertTrue(chunk)
                    self.assertLessEqual(len(chunk), query_chunk_size or len(addresses))
                self.assertCountEqual(
                    [address for chunk in chunks for address in chunk],
                    query_addresses,
                )

        # Disabled whitelist keeps stock behaviour: every transfer above chunk size
        indexer = self._build_whitelisted_indexer(set(), query_chunk_size=2)
        with self._spy_transfer_history(
            indexer, wraps=None, return_value=[]
        ) as get_transfer_history_mock:
            indexer._do_node_query(set(addresses), 10, 20)
        get_transfer_history_mock.assert_called_once_with(
            None, from_block=10, to_block=20
        )

    def test_erc721_events_indexer_whitelist(self):
        whitelisted_safe = SafeContractFactory()
        other_safe = SafeContractFactory()
        ethereum_tx = EthereumTxFactory()
        sender = Account.create().address
        token_id = 7
        erc721_event = {
            "address": Account.create().address,
            "blockHash": HexBytes(ethereum_tx.block.block_hash),
            "blockNumber": ethereum_tx.block.number,
            "data": "0x",
            "logIndex": 0,
            "removed": False,
            "topics": [
                HexBytes(
                    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
                ),
                HexBytes(encode_abi(["address"], [sender])),
                HexBytes(encode_abi(["address"], [whitelisted_safe.address])),
                HexBytes(encode_abi(["uint256"], [token_id])),
            ],
            "transactionHash": HexBytes(ethereum_tx.tx_hash),
            "transactionIndex": 0,
            "args": {
                "from": sender,
                "to": whitelisted_safe.address,
                "tokenId": token_id,
            },
        }
        current_block_number = self.ethereum_client.current_block_number
        IndexingStatus.objects.set_erc20_721_indexing_status(current_block_number)

        indexer = self._build_whitelisted_indexer({whitelisted_safe.address})
        with self._spy_transfer_history(
            indexer,
            wraps=None,
            side_effect=lambda addresses, **kwargs: (
                [erc721_event] if whitelisted_safe.address in addresses else []
            ),
        ) as get_transfer_history_mock:
            indexer.start()

        erc721_transfer = ERC721Transfer.objects.get()
        self.assertEqual(erc721_transfer.to, whitelisted_safe.address)
        self.assertEqual(erc721_transfer.token_id, token_id)
        self.assertEqual(ERC20Transfer.objects.count(), 0)
        get_transfer_history_mock.assert_called()
        for call in get_transfer_history_mock.call_args_list:
            self.assertNotIn(other_safe.address, call.args[0])

    def test_get_almost_updated_addresses_whitelist(self):
        whitelisted_safe = SafeContractFactory()
        SafeContractFactory()
        not_indexed_safe = Account.create().address
        indexer = self._build_whitelisted_indexer(
            {whitelisted_safe.address, not_indexed_safe}
        )
        self.assertEqual(
            indexer.get_almost_updated_addresses(
                self.ethereum_client.current_block_number
            ),
            {whitelisted_safe.address},
        )
        self.assertIsNone(indexer.addresses_cache)
