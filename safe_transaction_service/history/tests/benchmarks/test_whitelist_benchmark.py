"""
Opt-in Ganache benchmark: RPC calls, response bytes and stored rows of stock vs
whitelist indexing as the number of Safes on chain grows. Relative scaling only, see
`docs/whitelist-benchmark.md`.

    WHITELIST_BENCHMARK=1 WHITELIST_BENCHMARK_N=20,100,500 WHITELIST_BENCHMARK_OUT=/tmp/bench \\
        pytest safe_transaction_service/history/tests/benchmarks/test_whitelist_benchmark.py -s
"""

import csv
import os
import sys
import threading
import time
from unittest import skipUnless

from django.conf import settings
from django.test import TestCase, override_settings

from eth_account import Account
from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.safe import Safe
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from web3.types import RPCEndpoint

from ...indexers import Erc20EventsIndexer, SafeEventsIndexer
from ...models import (
    ERC20Transfer,
    ERC721Transfer,
    EthereumBlock,
    EthereumTx,
    IndexingStatus,
    InternalTx,
    InternalTxDecoded,
    ModuleTransaction,
    MultisigTransaction,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
)
from ...services import IndexServiceProvider
from ..differential import reset_indexing_state
from ..factories import ProxyFactoryFactory, SafeMasterCopyFactory

# `scripts/` is not a package
sys.path.insert(
    0,
    os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "scripts")
    ),
)
from rpc_logging_proxy import ProxyConfig, RpcLoggingProxy

SAFES_NUMBERS = [
    int(n) for n in os.environ.get("WHITELIST_BENCHMARK_N", "20,100,500").split(",")
]
WHITELISTED_SAFES_NUMBER = int(os.environ.get("WHITELIST_BENCHMARK_W", "5"))
OUTPUT_PATH = os.environ.get("WHITELIST_BENCHMARK_OUT")
# Same span for every N, so the number of block windows doesn't depend on N
BLOCK_SPAN = 4 * max(SAFES_NUMBERS) + 50
BLOCK_PROCESS_LIMIT = 50  # Fixed, no auto adjust
QUERY_CHUNK_SIZE = 10  # Below N: stock ERC20 indexing uses broad mode
MAX_INDEXER_RUNS = 1_000
VERSIONS = ("1.3.0", "1.4.1", "1.5.0")
TABLES = (
    EthereumBlock,
    EthereumTx,
    InternalTx,
    InternalTxDecoded,
    ERC20Transfer,
    ERC721Transfer,
    SafeRelevantTransaction,
    SafeContract,
    SafeStatus,
    SafeLastStatus,
    MultisigTransaction,
    ModuleTransaction,
)
TX_FETCH_METHODS = ("eth_getTransactionByHash", "eth_getTransactionReceipt")


@skipUnless(
    os.environ.get("WHITELIST_BENCHMARK") == "1", "Set WHITELIST_BENCHMARK=1 to run"
)
class TestWhitelistBenchmark(SafeTestCaseMixin, TestCase):
    def setUp(self):
        self.proxy = RpcLoggingProxy(
            ("127.0.0.1", 0), ProxyConfig(upstream=settings.ETHEREUM_NODE_URL)
        )
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        self.addCleanup(self.proxy.server_close)
        self.addCleanup(self.proxy.shutdown)
        self.addCleanup(IndexServiceProvider.del_singleton)
        self.singletons = {
            "1.3.0": self.safe_contract_V1_3_0.address,
            "1.4.1": self.safe_contract_V1_4_1.address,
            "1.5.0": self.safe_contract_V1_5_0.address,
        }
        for version, singleton in self.singletons.items():
            SafeMasterCopyFactory(address=singleton, version=version, l2=True)
        ProxyFactoryFactory(address=self.proxy_factory.address)

    def build_chain(self, safes_number: int) -> tuple[int, int, list[str]]:
        """
        Deploy `safes_number` Safes, run a Safe tx on each and send an ERC20 to every
        second one

        :return: Block range and the Safes
        """
        owner = self.ethereum_test_account
        range_start = self.ethereum_client.current_block_number + 1
        erc20_contract = self.deploy_example_erc20(10**6, owner.address)
        safes = []
        for i in range(safes_number):
            singleton = self.singletons[VERSIONS[i % len(VERSIONS)]]
            initializer = self.safe_contract_V1_4_1.functions.setup(
                [owner.address],
                1,
                NULL_ADDRESS,
                b"",
                NULL_ADDRESS,
                NULL_ADDRESS,
                0,
                NULL_ADDRESS,
            ).build_transaction({"gas": 1, "gasPrice": 1})["data"]
            ethereum_tx_sent = self.proxy_factory.deploy_proxy_contract_with_nonce(
                owner, singleton, initializer=initializer
            )
            self.w3.eth.wait_for_transaction_receipt(ethereum_tx_sent.tx_hash)
            safe_address = ethereum_tx_sent.contract_address
            safes.append(safe_address)

            multisig_tx = Safe(safe_address, self.ethereum_client).build_multisig_tx(
                Account.create().address, 0, b""
            )
            multisig_tx.sign(owner.key)
            tx_hash, _ = multisig_tx.execute(owner.key)
            self.w3.eth.wait_for_transaction_receipt(tx_hash)
            if i % 2 == 0:
                self.w3.eth.wait_for_transaction_receipt(
                    self.ethereum_client.erc20.send_tokens(
                        safe_address, 10, erc20_contract.address, owner.key
                    )
                )

        range_end = range_start + BLOCK_SPAN - 1
        blocks_to_mine = range_end - self.ethereum_client.current_block_number
        self.assertGreaterEqual(blocks_to_mine, 0, "Increase BLOCK_SPAN")
        if blocks_to_mine:
            response = self.w3.provider.make_request(
                RPCEndpoint("evm_mine"), [{"blocks": blocks_to_mine}]
            )
            self.assertNotIn("error", response)
        return range_start, range_end, safes

    def run_indexers(
        self, range_start: int, range_end: int, whitelisted_safes: frozenset[str]
    ) -> dict[str, float | int]:
        """
        Index `[range_start, range_end]` through the proxy and process the decoded txs

        :return: RPC calls, response bytes, rows per table and wall time
        """
        reset_indexing_state(range_start)
        self.proxy.stats.reset()
        start_time = time.monotonic()
        with override_settings(
            WHITELISTED_SAFES=whitelisted_safes, ETH_L2_NETWORK=True
        ):
            kwargs = {
                "confirmations": 0,
                "blocks_to_reindex_again": 0,
                "block_process_limit": BLOCK_PROCESS_LIMIT,
                "block_auto_process_limit": False,
                "query_chunk_size": QUERY_CHUNK_SIZE,
            }
            for indexer, get_next_block_number in (
                (
                    SafeEventsIndexer(EthereumClient(self.proxy.url), **kwargs),
                    lambda: min(
                        SafeMasterCopy.objects.values_list("tx_block_number", flat=True)
                    ),
                ),
                (
                    Erc20EventsIndexer(EthereumClient(self.proxy.url), **kwargs),
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

        methods = self.proxy.stats.summary()["methods"]
        result: dict[str, float | int] = {
            "wall_seconds": round(time.monotonic() - start_time, 2),
            "rpc_calls": sum(method["requests"] for method in methods.values()),
            "response_bytes": sum(
                method["response_bytes"] for method in methods.values()
            ),
            "eth_getLogs": methods.get("eth_getLogs", {}).get("requests", 0),
            "tx_fetches": sum(
                methods.get(method, {}).get("requests", 0)
                for method in TX_FETCH_METHODS
            ),
        }
        for model in TABLES:
            result[f"rows_{model.__name__}"] = model.objects.count()
        result["rows_total"] = sum(
            value for key, value in result.items() if key.startswith("rows_")
        )
        return result

    def write_results(self, results: list[dict]) -> None:
        summary_columns = [
            "N",
            "mode",
            "rpc_calls",
            "eth_getLogs",
            "tx_fetches",
            "response_bytes",
            "rows_total",
            "wall_seconds",
        ]
        table_columns = ["N", "mode"] + [f"rows_{model.__name__}" for model in TABLES]
        lines = [
            (
                f"Block span per N: {BLOCK_SPAN}, block process limit: "
                f"{BLOCK_PROCESS_LIMIT}, query chunk size: {QUERY_CHUNK_SIZE}, "
                f"whitelisted Safes: {WHITELISTED_SAFES_NUMBER}"
            ),
            "",
        ]
        for columns in (summary_columns, table_columns):
            lines.append("| " + " | ".join(columns) + " |")
            lines.append("|" + "---|" * len(columns))
            for result in results:
                lines.append(
                    "| " + " | ".join(str(result[column]) for column in columns) + " |"
                )
            lines.append("")

        # Growth from the smallest to the largest N, per mode
        smallest, largest = min(SAFES_NUMBERS), max(SAFES_NUMBERS)
        lines.append(
            f"Growth N={smallest} -> N={largest} (x{largest // smallest} Safes):"
        )
        for metric in (
            "rpc_calls",
            "eth_getLogs",
            "tx_fetches",
            "response_bytes",
            "rows_total",
        ):
            growth = {}
            for mode in ("stock", "whitelist"):
                by_n = {r["N"]: r[metric] for r in results if r["mode"] == mode}
                growth[mode] = (
                    f"x{by_n[largest] / by_n[smallest]:.2f}"
                    if by_n[smallest]
                    else "n/a"
                )
            lines.append(
                f"- {metric}: stock {growth['stock']}, whitelist {growth['whitelist']}"
            )
        report = "\n".join(lines)
        print(f"\n{report}")

        if OUTPUT_PATH:
            with open(f"{OUTPUT_PATH}.md", "w") as f:
                f.write(report + "\n")
            with open(f"{OUTPUT_PATH}.csv", "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=list(results[0]))
                writer.writeheader()
                writer.writerows(results)
            print(f"Results written to {OUTPUT_PATH}.md and {OUTPUT_PATH}.csv")

    def test_whitelist_benchmark(self):
        results = []
        for safes_number in sorted(SAFES_NUMBERS):
            range_start, range_end, safes = self.build_chain(safes_number)
            whitelisted_safes = frozenset(safes[:WHITELISTED_SAFES_NUMBER])
            for mode, whitelist in (
                ("stock", frozenset()),
                ("whitelist", whitelisted_safes),
            ):
                result = self.run_indexers(range_start, range_end, whitelist)
                results.append({"N": safes_number, "mode": mode, **result})
                print(f"N={safes_number} {mode}: {result}")
            # Sanity: the whitelist run indexes only the whitelisted Safes
            self.assertEqual(
                set(SafeContract.objects.values_list("address", flat=True)),
                whitelisted_safes,
            )
        self.write_results(results)
