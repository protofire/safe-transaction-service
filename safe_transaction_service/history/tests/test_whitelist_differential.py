"""
Differential correctness: index the same chain as stock, with a whitelist (live) and with
a whitelist (backfill), and require the same rows and API responses for the whitelisted
Safes, and nothing for the other Safes.

`WHITELIST_DIFF_N` sets the number of Safes (default 6, 3 of them whitelisted).
"""

import os
from copy import deepcopy
from dataclasses import dataclass
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from django.db.models import Q
from django.test import TestCase, override_settings

from eth_account import Account
from hexbytes import HexBytes
from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS, SENTINEL_ADDRESS
from safe_eth.safe import Safe
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from web3.types import RPCEndpoint

from ...tokens.models import Token
from ..indexers import Erc20EventsIndexer, SafeEventsIndexer
from ..models import (
    ERC20Transfer,
    IndexingStatus,
    InternalTx,
    SafeContract,
    SafeMasterCopy,
    SafeRelevantTransaction,
)
from ..services import IndexServiceProvider
from .differential import (
    COMPARED_MODELS,
    DiffLine,
    Snapshot,
    diff_snapshots,
    format_diff,
    normalize,
    reset_indexing_state,
    snapshot_api,
    table_filters,
    take_snapshot,
)
from .factories import ProxyFactoryFactory, SafeMasterCopyFactory

SAFES_NUMBER = int(os.environ.get("WHITELIST_DIFF_N", "6"))
VERSIONS = ("1.3.0", "1.4.1", "1.5.0")
BLOCKS_BETWEEN_GROUPS = 5
QUERY_CHUNK_SIZE = 2  # Below the number of Safes, stock ERC20 indexing uses broad mode
BACKFILL_BLOCK_PROCESS_LIMIT = 7  # Several windows per Safe
MAX_INDEXER_RUNS = 100


@dataclass
class DifferentialChain:
    range_start: int
    range_end: int
    singletons: dict[str, str]
    safes: list[str]
    creation_tx_hashes: dict[str, str]
    whitelisted_safes: frozenset[str]
    erc20_address: str
    # ETH sent by a non-whitelisted Safe to a whitelisted Safe
    eth_to_whitelisted_tx_hash: str
    eth_to_whitelisted_recipient: str


class TestWhitelistDifferential(SafeTestCaseMixin, TestCase):
    chain: DifferentialChain | None = None
    stock: tuple[Snapshot, Snapshot] | None = None

    def setUp(self):
        cls = TestWhitelistDifferential
        if cls.chain is None:
            cls.chain = self.build_chain()
        for version, singleton in self.chain.singletons.items():
            SafeMasterCopyFactory(
                address=singleton,
                initial_block_number=self.chain.range_start,
                tx_block_number=self.chain.range_start,
                version=version,
                l2=True,
            )
        ProxyFactoryFactory(
            address=self.proxy_factory.address,
            initial_block_number=self.chain.range_start,
            tx_block_number=self.chain.range_start,
        )
        self.addCleanup(IndexServiceProvider.del_singleton)
        # Token info is not indexing state. The API creates it lazily (and caches it),
        # so create it upfront for every variant to see the same
        self.assertIsNotNone(
            Token.objects.create_from_blockchain(self.chain.erc20_address)
        )

    # Chain -------------------------------------------------------------------------
    def mine_empty_blocks(self):
        response = self.w3.provider.make_request(
            RPCEndpoint("evm_mine"), [{"blocks": BLOCKS_BETWEEN_GROUPS}]
        )
        self.assertNotIn("error", response)

    def wait(self, tx_hash) -> str:
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash)
        self.assertEqual(receipt["status"], 1)
        return HexBytes(tx_hash).to_0x_hex()

    def deploy_safe(self, singleton: str) -> tuple[str, str]:
        initializer = self.safe_contract_V1_4_1.functions.setup(
            [self.ethereum_test_account.address],
            1,
            NULL_ADDRESS,
            b"",
            NULL_ADDRESS,
            NULL_ADDRESS,
            0,
            NULL_ADDRESS,
        ).build_transaction({"gas": 1, "gasPrice": 1})["data"]
        ethereum_tx_sent = self.proxy_factory.deploy_proxy_contract_with_nonce(
            self.ethereum_test_account, singleton, initializer=initializer
        )
        return ethereum_tx_sent.contract_address, self.wait(ethereum_tx_sent.tx_hash)

    def execute_safe_tx(
        self, safe_address: str, to: str, value: int = 0, data: bytes = b""
    ) -> str:
        multisig_tx = Safe(safe_address, self.ethereum_client).build_multisig_tx(
            to, value, data
        )
        multisig_tx.sign(self.ethereum_test_account.key)
        tx_hash, _ = multisig_tx.execute(self.ethereum_test_account.key)
        return self.wait(tx_hash)

    def safe_call_data(self, safe_address: str, function_name: str, *args) -> bytes:
        contract = Safe(safe_address, self.ethereum_client).contract
        return HexBytes(
            getattr(contract.functions, function_name)(*args).build_transaction(
                {"gas": 1, "gasPrice": 1}
            )["data"]
        )

    def execute_from_module(self, safe_address: str, to: str) -> str:
        module = self.ethereum_test_account
        contract = Safe(safe_address, self.ethereum_client).contract
        tx = contract.functions.execTransactionFromModule(
            to, 0, b"", 0
        ).build_transaction({"from": module.address, "gas": 500_000})
        return self.wait(
            self.ethereum_client.send_unsigned_transaction(tx, private_key=module.key)
        )

    def build_chain(self) -> DifferentialChain:
        singletons = {
            "1.3.0": self.safe_contract_V1_3_0.address,
            "1.4.1": self.safe_contract_V1_4_1.address,
            "1.5.0": self.safe_contract_V1_5_0.address,
        }
        range_start = self.ethereum_client.current_block_number + 1
        owner = self.ethereum_test_account

        safes: list[str] = []
        creation_tx_hashes: dict[str, str] = {}
        for i in range(SAFES_NUMBER):
            safe_address, creation_tx_hash = self.deploy_safe(
                singletons[VERSIONS[i % len(VERSIONS)]]
            )
            safes.append(safe_address)
            creation_tx_hashes[safe_address] = creation_tx_hash
        # First Safe of every version
        whitelisted_safes = frozenset(safes[: len(VERSIONS)])
        other_safe = safes[len(VERSIONS)]
        whitelisted_1, whitelisted_2 = safes[0], safes[1]
        guard = self.deploy_example_transaction_guard()
        self.mine_empty_blocks()

        for safe_address in safes:
            self.wait(self.send_ether(safe_address, 10**16))
            self.execute_safe_tx(safe_address, Account.create().address, value=10**14)
            new_owner = Account.create().address
            for function_name, args in (
                ("addOwnerWithThreshold", (new_owner, 1)),
                ("changeThreshold", (1,)),
                ("removeOwner", (SENTINEL_ADDRESS, new_owner, 1)),
            ):
                self.execute_safe_tx(
                    safe_address,
                    safe_address,
                    data=self.safe_call_data(safe_address, function_name, *args),
                )
        self.mine_empty_blocks()

        for safe_address in safes:
            for function_name, args in (
                ("setGuard", (guard,)),
                ("enableModule", (owner.address,)),
            ):
                self.execute_safe_tx(
                    safe_address,
                    safe_address,
                    data=self.safe_call_data(safe_address, function_name, *args),
                )
            self.execute_from_module(safe_address, Account.create().address)
        self.mine_empty_blocks()

        erc20_contract = self.deploy_example_erc20(10**6, owner.address)
        for safe_address in safes:
            self.wait(
                self.ethereum_client.erc20.send_tokens(
                    safe_address, 100, erc20_contract.address, owner.key
                )
            )
        for sender, receiver in (
            (whitelisted_1, whitelisted_2),
            (whitelisted_1, other_safe),
            (other_safe, whitelisted_1),
        ):
            self.execute_safe_tx(
                sender,
                erc20_contract.address,
                data=HexBytes(
                    erc20_contract.functions.transfer(receiver, 10).build_transaction(
                        {"gas": 1, "gasPrice": 1}
                    )["data"]
                ),
            )
        self.mine_empty_blocks()

        eth_to_whitelisted_tx_hash = self.execute_safe_tx(
            other_safe, whitelisted_1, value=10**14
        )
        return DifferentialChain(
            range_start=range_start,
            range_end=self.ethereum_client.current_block_number,
            singletons=singletons,
            safes=safes,
            creation_tx_hashes=creation_tx_hashes,
            whitelisted_safes=whitelisted_safes,
            erc20_address=erc20_contract.address,
            eth_to_whitelisted_tx_hash=eth_to_whitelisted_tx_hash,
            eth_to_whitelisted_recipient=whitelisted_1,
        )

    # Variants ------------------------------------------------------------------------
    def take_snapshots(self) -> tuple[Snapshot, Snapshot]:
        whitelisted_safes = self.chain.whitelisted_safes
        return take_snapshot(whitelisted_safes), snapshot_api(
            self.client, whitelisted_safes
        )

    def run_live(self, whitelisted_safes: frozenset[str]) -> tuple[Snapshot, Snapshot]:
        """
        Index the chain range with the live indexers and process the decoded txs
        """
        range_end = self.chain.range_end
        reset_indexing_state(self.chain.range_start)
        with override_settings(
            WHITELISTED_SAFES=whitelisted_safes, ETH_L2_NETWORK=True
        ):
            kwargs = {
                "confirmations": 0,
                "blocks_to_reindex_again": 0,
                "query_chunk_size": QUERY_CHUNK_SIZE,
            }
            safe_events_indexer = SafeEventsIndexer(
                EthereumClient(settings.ETHEREUM_NODE_URL), **kwargs
            )
            erc20_events_indexer = Erc20EventsIndexer(
                EthereumClient(settings.ETHEREUM_NODE_URL), **kwargs
            )
            for indexer, get_next_block_number in (
                (
                    safe_events_indexer,
                    lambda: min(
                        SafeMasterCopy.objects.values_list("tx_block_number", flat=True)
                    ),
                ),
                (
                    erc20_events_indexer,
                    lambda: (
                        IndexingStatus.objects.get_erc20_721_indexing_status().block_number
                    ),
                ),
            ):
                for _ in range(MAX_INDEXER_RUNS):
                    if get_next_block_number() > range_end:
                        break
                    indexer.start()
                else:
                    self.fail(f"{indexer.__class__.__name__} did not reach {range_end}")
            IndexServiceProvider().process_all_decoded_txs()
            return self.take_snapshots()

    def run_backfill(self) -> tuple[Snapshot, Snapshot]:
        """
        Live indexers are already past the range, so only the backfill sees it
        """
        range_end = self.chain.range_end
        reset_indexing_state(self.chain.range_start)
        SafeMasterCopy.objects.update(tx_block_number=range_end + 1)
        IndexingStatus.objects.set_erc20_721_indexing_status(range_end + 1)
        with override_settings(
            WHITELISTED_SAFES=self.chain.whitelisted_safes, ETH_L2_NETWORK=True
        ):
            for safe_address in sorted(self.chain.whitelisted_safes):
                call_command(
                    "backfill_whitelisted_safe",
                    address=safe_address,
                    creation_tx_hash=self.chain.creation_tx_hashes[safe_address],
                    to_block_number=range_end,
                    block_process_limit=BACKFILL_BLOCK_PROCESS_LIMIT,
                    stdout=StringIO(),
                    stderr=StringIO(),
                )
            return self.take_snapshots()

    def get_stock(self) -> tuple[Snapshot, Snapshot]:
        # The chain doesn't change, so the stock result can be reused between tests
        cls = TestWhitelistDifferential
        if cls.stock is None:
            cls.stock = self.run_live(frozenset())
        return deepcopy(cls.stock)

    # Assertions ----------------------------------------------------------------------
    def assert_no_diff(self, name_a, snapshot_a, name_b, snapshot_b, expected=()):
        diff_lines = [
            line
            for line in diff_snapshots(name_a, snapshot_a, name_b, snapshot_b)
            if line not in expected
        ]
        self.assertEqual(
            diff_lines,
            [],
            f"\n{format_diff(name_a, name_b, diff_lines)}",
        )

    def expected_whitelist_diff(self, stock_snapshot: Snapshot) -> list[DiffLine]:
        """
        Expected difference 1: stock stores the ether transfer from the sender Safe's
        `SafeMultiSigTransaction` event, which is dropped when the sender is not
        whitelisted. The recipient's `SafeReceived` row is in every variant
        """
        tx_hash = normalize(HexBytes(self.chain.eth_to_whitelisted_tx_hash))
        keys = [
            key
            for key, row in stock_snapshot["InternalTx"].items()
            if key[0] == tx_hash and row["trace_address"].endswith(",0")
        ]
        self.assertEqual(len(keys), 1, stock_snapshot["InternalTx"])
        self.assertEqual(
            stock_snapshot["InternalTx"][keys[0]]["to"],
            self.chain.eth_to_whitelisted_recipient,
        )
        return [DiffLine("InternalTx", keys[0], None, "<present>", "<missing>")]

    def expected_whitelist_api(
        self, stock_api: Snapshot, child_internal_tx_key: tuple
    ) -> Snapshot:
        """
        Expected difference 1 in the API (Q10, accepted): stock lists the ether transfer
        twice in the recipient's `all-transactions` (synthetic child and `SafeReceived`),
        whitelist mode only once (`SafeReceived`)

        :return: `stock_api` without the transfer of the synthetic child `InternalTx`
        """
        tx_hash, trace_address = child_internal_tx_key
        child_transfer_id = f"i{tx_hash[2:]}{trace_address}"
        expected_api = deepcopy(stock_api)
        recipient_txs = expected_api["all-transactions"][
            self.chain.eth_to_whitelisted_recipient
        ]["data"]
        removed = 0
        for tx in recipient_txs:
            if tx.get("txHash") == tx_hash:
                transfers = tx["transfers"]
                tx["transfers"] = [
                    transfer
                    for transfer in transfers
                    if transfer["transferId"] != child_transfer_id
                ]
                removed += len(transfers) - len(tx["transfers"])
        self.assertEqual(removed, 1, "Synthetic ether transfer not found in stock API")
        return expected_api

    def assert_differential(self, name: str, variant: tuple[Snapshot, Snapshot]):
        stock_tables, stock_api = self.get_stock()
        variant_tables, variant_api = variant
        expected = self.expected_whitelist_diff(stock_tables)
        diff_lines = diff_snapshots("stock", stock_tables, name, variant_tables)
        for line in expected:
            self.assertIn(line, diff_lines)
        self.assert_no_diff("stock", stock_tables, name, variant_tables, expected)
        self.assertNotEqual(stock_api, variant_api)
        self.assert_no_diff(
            "stock",
            self.expected_whitelist_api(stock_api, expected[0].key),
            name,
            variant_api,
        )

    def assert_non_whitelisted_absent(self):
        whitelisted_safes = self.chain.whitelisted_safes
        whitelisted_filters = table_filters(whitelisted_safes)
        self.assertFalse(
            SafeContract.objects.exclude(address__in=whitelisted_safes).exists()
        )
        # Txs touching a whitelisted Safe
        whitelisted_tx_ids = set(
            ERC20Transfer.objects.filter(
                whitelisted_filters[ERC20Transfer]
            ).values_list("ethereum_tx_id", flat=True)
        ) | set(
            InternalTx.objects.filter(whitelisted_filters[InternalTx]).values_list(
                "ethereum_tx_id", flat=True
            )
        )
        other_safes = set(self.chain.safes) - whitelisted_safes
        other_filters = table_filters(other_safes)
        for model in COMPARED_MODELS:
            with self.subTest(model=model.__name__):
                queryset = model.objects.filter(other_filters[model])
                if model in (InternalTx, ERC20Transfer):
                    # Rows with a whitelisted counterpart are expected
                    queryset = queryset.exclude(whitelisted_filters[model])
                elif model is SafeRelevantTransaction:
                    # Expected difference 6: a relevant tx is stored for both sides of an
                    # indexed transfer, Safe or not
                    queryset = queryset.exclude(
                        Q(ethereum_tx_id__in=whitelisted_tx_ids)
                    )
                self.assertFalse(queryset.exists(), list(queryset.values()))

    # Tests -----------------------------------------------------------------------------
    def test_differential_live(self):
        self.assert_differential(
            "whitelist", self.run_live(self.chain.whitelisted_safes)
        )

    def test_differential_backfill(self):
        self.assert_differential("backfill", self.run_backfill())

    def test_non_whitelisted_absent(self):
        for name, run in (
            ("whitelist", lambda: self.run_live(self.chain.whitelisted_safes)),
            ("backfill", self.run_backfill),
        ):
            with self.subTest(variant=name):
                run()
                self.assert_non_whitelisted_absent()

    def test_comparator_detects_differences(self):
        stock_tables, _ = self.get_stock()
        self.assertTrue(stock_tables["MultisigTransaction"])
        self.assertTrue(stock_tables["ERC20Transfer"])
        altered = deepcopy(stock_tables)
        deleted_key = next(iter(altered["MultisigTransaction"]))
        del altered["MultisigTransaction"][deleted_key]
        altered_key = next(iter(altered["ERC20Transfer"]))
        altered["ERC20Transfer"][altered_key]["value"] = "123456789"

        diff_lines = diff_snapshots("stock", stock_tables, "altered", altered)
        self.assertCountEqual(
            diff_lines,
            [
                DiffLine(
                    "MultisigTransaction", deleted_key, None, "<present>", "<missing>"
                ),
                DiffLine(
                    "ERC20Transfer",
                    altered_key,
                    "value",
                    stock_tables["ERC20Transfer"][altered_key]["value"],
                    "123456789",
                ),
            ],
        )
        self.assertIn("2 differences", format_diff("stock", "altered", diff_lines))
