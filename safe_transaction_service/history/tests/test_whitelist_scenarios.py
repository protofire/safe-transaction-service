"""
Opt-in whitelist scenarios for the human-run checkpoints. Skipped by default.

Indexers use `WHITELIST_SCENARIO_RPC_URL` (e.g. `scripts/rpc_logging_proxy.py`), while
contracts are deployed directly against `ETHEREUM_NODE_URL`:

    WHITELIST_SCENARIOS=1 WHITELIST_SCENARIO_RPC_URL=http://127.0.0.1:8546 \\
        pytest safe_transaction_service/history/tests/test_whitelist_scenarios.py \\
        -k checkpoint_a -s

`-k checkpoint_b` runs the ERC20 scenario. `-k checkpoint_c` runs the backfill command
through the RPC URL, with `--block-process-limit` from
`WHITELIST_SCENARIO_BLOCK_PROCESS_LIMIT`.
"""

import logging
import os
from collections.abc import Callable
from io import StringIO
from unittest import mock, skipUnless

from django.conf import settings
from django.core.management import call_command
from django.test import TestCase, override_settings

from celery.exceptions import SoftTimeLimitExceeded
from hexbytes import HexBytes
from requests import Timeout
from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.safe import Safe
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from safe_eth.util.util import to_0x_hex_str
from web3.exceptions import Web3RPCError
from web3.types import RPCEndpoint

from ..indexers import Erc20EventsIndexer, SafeEventsIndexer
from ..indexers.ethereum_indexer import EthereumIndexer, FindRelevantElementsException
from ..indexers.tx_processor import SafeTxProcessor
from ..models import (
    ERC20Transfer,
    EthereumTx,
    IndexingStatus,
    InternalTx,
    InternalTxDecoded,
    InternalTxType,
    MultisigTransaction,
    ProxyFactory,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
)
from ..services import IndexServiceProvider, SafeServiceProvider
from .factories import (
    ProxyFactoryFactory,
    SafeContractFactory,
    SafeMasterCopyFactory,
)

SAFES_PER_VERSION = 3
MAX_INDEXER_RUNS = 200
# Errors `EthereumIndexer.process_addresses` handles (block process limit set to 1).
# The periodic indexing task runs again after them, so the scenario does the same
INDEXER_RETRYABLE_ERRORS = (
    FindRelevantElementsException,
    SoftTimeLimitExceeded,
    Timeout,
    ValueError,
    Web3RPCError,
)


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

    def execute_safe_tx(
        self, safe_address: str, to: str | None = None, data: bytes = b""
    ) -> HexBytes:
        multisig_tx = Safe(safe_address, self.ethereum_client).build_multisig_tx(
            to or safe_address, 0, data
        )
        multisig_tx.sign(self.ethereum_test_account.key)
        tx_hash, _ = multisig_tx.execute(self.ethereum_test_account.key)
        self.w3.eth.wait_for_transaction_receipt(tx_hash)
        return HexBytes(tx_hash)

    def run_indexer(
        self,
        indexer: EthereumIndexer,
        target_block_number: int,
        get_next_block_number: Callable[[], int],
    ) -> None:
        """
        Run `indexer.start()` like the periodic task until `get_next_block_number()`
        (next block to index) is past `target_block_number`, retrying after provider
        errors. Prints the block process limit used on every run and the errors
        """
        block_process_limits: list[int] = []
        errors: list[str] = []
        try:
            for _ in range(MAX_INDEXER_RUNS):
                if get_next_block_number() > target_block_number:
                    return
                block_process_limits.append(indexer.block_process_limit)
                try:
                    indexer.start()
                except INDEXER_RETRYABLE_ERRORS as e:
                    errors.append(f"{e.__class__.__name__}: {str(e)[:120]}")
            self.fail(
                f"Indexer did not reach the target block in {MAX_INDEXER_RUNS} runs"
            )
        finally:
            print(f"\nIndexer runs: {len(block_process_limits)}")
            print(f"Block process limit per run: {block_process_limits}")
            print(f"Provider errors ({len(errors)}): {errors[:5]}")

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
                self.run_indexer(
                    indexer,
                    target_block_number,
                    lambda: min(
                        indexer.database_queryset.values_list(
                            "tx_block_number", flat=True
                        )
                    ),
                )
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

    def test_checkpoint_b_erc20(self):
        account = self.ethereum_test_account
        erc20_contract = self.deploy_example_erc20(1_000, account.address)
        safes = [self.deploy_safe(self.safe_contract_V1_4_1)[0] for _ in range(4)]
        whitelisted_safes = set(safes[:3])
        other_safe = safes[3]
        # Like an existing database: every Safe has a SafeContract
        for safe_address in safes:
            SafeContractFactory(address=safe_address)
        IndexingStatus.objects.set_erc20_721_indexing_status(
            self.ethereum_client.current_block_number + 1
        )

        def transfer_data(to: str, amount: int) -> bytes:
            return HexBytes(
                erc20_contract.functions.transfer(to, amount).build_transaction(
                    {"gas": 1, "gasPrice": 1}
                )["data"]
            )

        def send_tokens(to: str, amount: int) -> HexBytes:
            tx_hash = self.ethereum_client.erc20.send_tokens(
                to, amount, erc20_contract.address, account.key
            )
            self.w3.eth.wait_for_transaction_receipt(tx_hash)
            return HexBytes(tx_hash)

        for safe_address in safes:
            send_tokens(safe_address, 100)
        whitelisted_1, whitelisted_2 = safes[0], safes[1]
        # Between whitelisted Safes, possibly in different chunks
        whitelisted_to_whitelisted_tx_hash = self.execute_safe_tx(
            whitelisted_1, erc20_contract.address, transfer_data(whitelisted_2, 10)
        )
        # From a whitelisted Safe to the other Safe
        self.execute_safe_tx(
            whitelisted_2, erc20_contract.address, transfer_data(other_safe, 10)
        )
        # Only touching the other Safe
        other_tx_hashes = {
            self.execute_safe_tx(
                other_safe, erc20_contract.address, transfer_data(account.address, 5)
            )
        }
        target_block_number = self.ethereum_client.current_block_number

        with override_settings(WHITELISTED_SAFES=frozenset(whitelisted_safes)):
            indexer = Erc20EventsIndexer(
                EthereumClient(self.scenario_rpc_url), confirmations=0
            )
        erc20_manager = indexer.ethereum_client.erc20
        with mock.patch.object(
            erc20_manager,
            "get_total_transfer_history",
            wraps=erc20_manager.get_total_transfer_history,
        ) as get_transfer_history_mock:
            self.run_indexer(
                indexer,
                target_block_number,
                lambda: (
                    IndexingStatus.objects.get_erc20_721_indexing_status().block_number
                ),
            )

        erc20_transfers = list(
            ERC20Transfer.objects.values_list(
                "ethereum_tx_id", "log_index", "_from", "to"
            )
        )
        address_chunks = [
            call.args[0] for call in get_transfer_history_mock.call_args_list
        ]
        print(f"Token: {erc20_contract.address}")
        print(f"Whitelisted Safes: {sorted(whitelisted_safes)}")
        print(f"Other Safe: {other_safe}")
        print(f"ERC20Transfer rows: {len(erc20_transfers)}")
        print(
            f"get_total_transfer_history calls: {len(address_chunks)}, "
            f"chunk sizes: {sorted({len(chunk) for chunk in address_chunks})}"
        )

        # Every query is filtered by whitelisted Safes
        self.assertTrue(address_chunks)
        for chunk in address_chunks:
            self.assertTrue(chunk)
            self.assertTrue(set(chunk) <= whitelisted_safes)
        # Only transfers touching whitelisted Safes, no duplicates
        for _, _, _from, to in erc20_transfers:
            self.assertTrue(_from in whitelisted_safes or to in whitelisted_safes)
        self.assertEqual(
            len(erc20_transfers), len({transfer[:2] for transfer in erc20_transfers})
        )
        self.assertEqual(
            ERC20Transfer.objects.filter(
                ethereum_tx_id=whitelisted_to_whitelisted_tx_hash
            ).count(),
            1,
        )
        self.assertFalse(
            ERC20Transfer.objects.filter(ethereum_tx_id__in=other_tx_hashes).exists()
        )
        for safe_address in whitelisted_safes:
            self.assertTrue(ERC20Transfer.objects.filter(to=safe_address).exists())
        # 3 funding transfers + whitelisted -> whitelisted + whitelisted -> other
        self.assertEqual(len(erc20_transfers), 5)

    def test_checkpoint_c_backfill(self):
        """
        "Old" Safes: their history is in blocks the live indexers never see
        """
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
        erc20_contract = self.deploy_example_erc20(
            1_000, self.ethereum_test_account.address
        )

        safe_creation_tx_hashes: dict[str, HexBytes] = {}
        safe_singleton: dict[str, str] = {}
        for singleton in singletons.values():
            safe_address, creation_tx_hash = self.deploy_safe(singleton)
            safe_creation_tx_hashes[safe_address] = creation_tx_hash
            safe_singleton[safe_address] = singleton.address
            for _ in range(2):
                self.execute_safe_tx(safe_address)
            self.w3.eth.wait_for_transaction_receipt(
                self.ethereum_client.erc20.send_tokens(
                    safe_address,
                    10,
                    erc20_contract.address,
                    self.ethereum_test_account.key,
                )
            )

        blocks_to_mine = 2_000
        response = self.w3.provider.make_request(
            RPCEndpoint("evm_mine"), [{"blocks": blocks_to_mine}]
        )
        self.assertNotIn("error", response)
        # Live indexers are already at the tip
        next_block_number = self.ethereum_client.current_block_number + 1
        SafeMasterCopy.objects.update(tx_block_number=next_block_number)
        ProxyFactory.objects.update(tx_block_number=next_block_number)
        IndexingStatus.objects.set_erc20_721_indexing_status(next_block_number)

        block_process_limit = os.environ.get(
            "WHITELIST_SCENARIO_BLOCK_PROCESS_LIMIT", "5000"
        )
        provider_errors = []

        class ProviderErrorsHandler(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    provider_errors.append(record.getMessage())

        backfill_logger = logging.getLogger(
            "safe_transaction_service.history.indexers.backfill"
        )
        handler = ProviderErrorsHandler()
        backfill_logger.addHandler(handler)
        self.addCleanup(backfill_logger.removeHandler, handler)

        def backfill_all() -> dict[str, int]:
            for safe_address, creation_tx_hash in safe_creation_tx_hashes.items():
                stdout, stderr = StringIO(), StringIO()
                call_command(
                    "backfill_whitelisted_safe",
                    f"--address={safe_address}",
                    f"--creation-tx-hash={to_0x_hex_str(creation_tx_hash)}",
                    f"--block-process-limit={block_process_limit}",
                    stdout=stdout,
                    stderr=stderr,
                )
                print(stdout.getvalue().strip().splitlines()[-1], stderr.getvalue())
            return {
                model.__name__: model.objects.count()
                for model in (
                    SafeContract,
                    InternalTx,
                    InternalTxDecoded,
                    MultisigTransaction,
                    ERC20Transfer,
                    SafeLastStatus,
                )
            }

        with override_settings(
            WHITELISTED_SAFES=frozenset(safe_creation_tx_hashes),
            ETH_L2_NETWORK=True,
            ETHEREUM_NODE_URL=self.scenario_rpc_url,
        ):
            IndexServiceProvider.del_singleton()
            self.addCleanup(IndexServiceProvider.del_singleton)
            print(
                f"\nBackfilling {len(safe_creation_tx_hashes)} Safes, "
                f"{blocks_to_mine} empty blocks, block-process-limit={block_process_limit}"
            )
            row_counts = backfill_all()
            first_run_provider_errors = len(provider_errors)
            print(f"Row counts: {row_counts}")
            print(f"Provider errors (first run): {first_run_provider_errors}")
            # A second run changes nothing
            self.assertEqual(backfill_all(), row_counts)

        for safe_address in safe_creation_tx_hashes:
            with self.subTest(safe_address=safe_address):
                safe = Safe(safe_address, self.ethereum_client)
                self.assertTrue(
                    InternalTx.objects.filter(
                        contract_address=safe_address,
                        tx_type=InternalTxType.CREATE.value,
                    ).exists()
                )
                safe_last_status = SafeLastStatus.objects.get(address=safe_address)
                self.assertEqual(
                    safe_last_status.master_copy, safe_singleton[safe_address]
                )
                self.assertEqual(safe_last_status.nonce, safe.retrieve_nonce())
                self.assertEqual(safe_last_status.owners, safe.retrieve_owners())
                self.assertEqual(safe_last_status.threshold, safe.retrieve_threshold())
                self.assertEqual(
                    MultisigTransaction.objects.filter(safe=safe_address).count(), 2
                )
                self.assertEqual(
                    ERC20Transfer.objects.to_or_from(safe_address).count(), 1
                )
                self.assertIsNotNone(
                    SafeServiceProvider().get_safe_creation_info(safe_address)
                )
        self.assertEqual(
            set(SafeContract.objects.values_list("address", flat=True)),
            set(safe_creation_tx_hashes),
        )
