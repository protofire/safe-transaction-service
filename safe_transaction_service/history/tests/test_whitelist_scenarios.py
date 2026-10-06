"""
Opt-in whitelist scenarios for the human-run checkpoints. Skipped by default.

Indexers use `WHITELIST_SCENARIO_RPC_URL` (e.g. `scripts/rpc_logging_proxy.py`), while
contracts are deployed directly against `ETHEREUM_NODE_URL`:

    WHITELIST_SCENARIOS=1 WHITELIST_SCENARIO_RPC_URL=http://127.0.0.1:8546 \\
        pytest safe_transaction_service/history/tests/test_whitelist_scenarios.py \\
        -k checkpoint_a -s
"""

import os
from unittest import mock, skipUnless

from django.conf import settings
from django.test import TestCase, override_settings

from hexbytes import HexBytes
from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.safe import Safe
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from safe_eth.util.util import to_0x_hex_str

from ..indexers import SafeEventsIndexer
from ..indexers.tx_processor import SafeTxProcessor
from ..models import (
    EthereumTx,
    InternalTx,
    InternalTxDecoded,
    InternalTxType,
    SafeContract,
    SafeLastStatus,
)
from ..services import IndexServiceProvider, SafeServiceProvider
from .factories import ProxyFactoryFactory, SafeMasterCopyFactory

SAFES_PER_VERSION = 3


@skipUnless(
    os.environ.get("WHITELIST_SCENARIOS") == "1", "Set WHITELIST_SCENARIOS=1 to run"
)
class TestWhitelistScenarios(SafeTestCaseMixin, TestCase):
    def setUp(self):
        self.scenario_rpc_url = os.environ.get(
            "WHITELIST_SCENARIO_RPC_URL", settings.ETHEREUM_NODE_URL
        )
        # `EthereumIndexer.__init__` replaces the client of the `IndexService` singleton
        index_service = IndexServiceProvider()
        self.addCleanup(
            setattr, index_service, "ethereum_client", index_service.ethereum_client
        )

    def deploy_safe(self, safe_contract) -> tuple[str, HexBytes]:
        initializer = HexBytes(
            safe_contract.functions.setup(
                [self.ethereum_test_account.address],
                1,
                NULL_ADDRESS,
                b"",
                NULL_ADDRESS,
                NULL_ADDRESS,
                0,
                NULL_ADDRESS,
            ).build_transaction({"gas": 1, "gasPrice": 1})["data"]
        )
        ethereum_tx_sent = self.proxy_factory.deploy_proxy_contract_with_nonce(
            self.ethereum_test_account, safe_contract.address, initializer=initializer
        )
        self.w3.eth.wait_for_transaction_receipt(ethereum_tx_sent.tx_hash)
        return ethereum_tx_sent.contract_address, HexBytes(ethereum_tx_sent.tx_hash)

    def execute_safe_tx(self, safe_address: str) -> HexBytes:
        multisig_tx = Safe(safe_address, self.ethereum_client).build_multisig_tx(
            safe_address, 0, b""
        )
        multisig_tx.sign(self.ethereum_test_account.key)
        tx_hash, _ = multisig_tx.execute(self.ethereum_test_account.key)
        self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return HexBytes(tx_hash)

    def test_checkpoint_a_l2_live_indexing(self):
        singletons = {
            "1.3.0": self.safe_contract_V1_3_0,
            "1.4.1": self.safe_contract_V1_4_1,
            "1.5.0": self.safe_contract_V1_5_0,
        }
        initial_block_number = self.ethereum_client.current_block_number + 1
        for version, singleton in singletons.items():
            SafeMasterCopyFactory(
                address=singleton.address,
                initial_block_number=initial_block_number,
                tx_block_number=initial_block_number,
                version=version,
                l2=True,
            )
        ProxyFactoryFactory(
            address=self.proxy_factory.address,
            initial_block_number=initial_block_number,
            tx_block_number=initial_block_number,
        )

        # Deploy Safes, whitelist the first one of every version, run a Safe tx on each
        safe_singleton: dict[str, str] = {}
        whitelisted_safes: set[str] = set()
        other_tx_hashes: set[HexBytes] = set()
        for singleton in singletons.values():
            for i in range(SAFES_PER_VERSION):
                safe_address, deploy_tx_hash = self.deploy_safe(singleton)
                safe_tx_hash = self.execute_safe_tx(safe_address)
                safe_singleton[safe_address] = singleton.address
                if i == 0:
                    whitelisted_safes.add(safe_address)
                else:
                    other_tx_hashes |= {deploy_tx_hash, safe_tx_hash}
        target_block_number = self.ethereum_client.current_block_number

        with override_settings(WHITELISTED_SAFES=frozenset(whitelisted_safes)):
            indexer = SafeEventsIndexer(
                EthereumClient(self.scenario_rpc_url),
                confirmations=0,
                blocks_to_reindex_again=0,
            )
            with mock.patch.object(
                indexer.index_service,
                "txs_create_or_update_from_tx_hashes",
                wraps=indexer.index_service.txs_create_or_update_from_tx_hashes,
            ) as fetch_txs_mock:
                for _ in range(100):
                    indexer.start()
                    if (
                        min(
                            indexer.database_queryset.values_list(
                                "tx_block_number", flat=True
                            )
                        )
                        > target_block_number
                    ):
                        break
                else:
                    self.fail("Indexer did not reach the target block")
            SafeTxProcessor(
                self.ethereum_client, None, None
            ).process_decoded_transactions(
                list(InternalTxDecoded.objects.pending_for_safes())
            )

        requested_tx_hashes = {
            HexBytes(tx_hash)
            for call in fetch_txs_mock.call_args_list
            for tx_hash in call.args[0]
        }
        other_safes = set(safe_singleton) - whitelisted_safes
        print(f"\nWhitelisted Safes: {sorted(whitelisted_safes)}")
        print(f"Other Safes: {sorted(other_safes)}")
        print(
            "Non-whitelisted tx hashes: "
            f"{sorted(to_0x_hex_str(tx_hash) for tx_hash in other_tx_hashes)}"
        )
        print(f"Tx hashes fetched by the indexer: {len(requested_tx_hashes)}")

        self.assertEqual(
            set(SafeContract.objects.values_list("address", flat=True)),
            whitelisted_safes,
        )
        for safe_address in whitelisted_safes:
            with self.subTest(safe_address=safe_address):
                self.assertTrue(
                    InternalTx.objects.filter(
                        contract_address=safe_address,
                        tx_type=InternalTxType.CREATE.value,
                    ).exists()
                )
                self.assertEqual(
                    SafeLastStatus.objects.get(address=safe_address).master_copy,
                    safe_singleton[safe_address],
                )
                self.assertIsNotNone(
                    SafeServiceProvider().get_safe_creation_info(safe_address)
                )

        self.assertFalse(requested_tx_hashes & other_tx_hashes)
        self.assertFalse(
            EthereumTx.objects.filter(tx_hash__in=other_tx_hashes).exists()
        )
        self.assertFalse(
            InternalTx.objects.filter(ethereum_tx_id__in=other_tx_hashes).exists()
        )
