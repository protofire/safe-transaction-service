import json
import os
import sys
import tempfile
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from unittest import TestCase

# `scripts/` is not a package
SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SCRIPTS_DIR)

from compare_with_safe_service import (  # noqa: E402
    ApiClient,
    compare_safe,
    main,
    normalize,
)

SAFE = "0x5aFE3855358E112B5647B952709E6165e1c1eEEe"
INDEXED_BLOCK = 1_000


class FakeServiceHandler(BaseHTTPRequestHandler):
    server: "FakeService"

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.server.requests.append(self.path)
        if self.server.fail_first.get(self.path, 0):
            self.server.fail_first[self.path] -= 1
            status, data = 429, {"detail": "Throttled"}
        else:
            status, data = self.server.routes.get(
                self.path, (404, {"detail": "Not found"})
            )
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class FakeService(ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeServiceHandler)
        self.routes: dict[str, tuple[int, object]] = {}
        self.requests: list[str] = []
        self.fail_first: dict[str, int] = {}
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def stop(self):
        self.shutdown()
        self.server_close()

    def set_safe(
        self,
        transactions: list[dict],
        nonce: int = 1,
        page_size: int = 100,
        indexed_block: int = INDEXED_BLOCK,
    ):
        self.routes["/api/v1/about/indexing/"] = (
            200,
            {
                "masterCopiesBlockNumber": indexed_block,
                "erc20BlockNumber": indexed_block,
            },
        )
        self.routes[f"/api/v1/safes/{SAFE}/"] = (
            200,
            {"address": SAFE, "nonce": str(nonce), "modified": "ignored"},
        )
        self.routes[f"/api/v1/safes/{SAFE}/creation/"] = (
            200,
            {"created": "ignored", "transactionHash": "0x01"},
        )
        base = f"/api/v1/safes/{SAFE}/all-transactions/?limit={page_size}"
        for offset in range(0, max(len(transactions), 1), page_size):
            path = base if not offset else f"{base}&offset={offset}"
            next_offset = offset + page_size
            self.routes[path] = (
                200,
                {
                    "count": len(transactions),
                    "next": f"{self.url}{base}&offset={next_offset}"
                    if next_offset < len(transactions)
                    else None,
                    "previous": None,
                    "results": transactions[offset:next_offset],
                },
            )


def multisig(safe_tx_hash: str, block_number: int | None = 10, **kwargs) -> dict:
    return {
        "txType": "MULTISIG_TRANSACTION",
        "safeTxHash": safe_tx_hash,
        "blockNumber": block_number,
        "isExecuted": block_number is not None,
        "nonce": 0,
        "confirmations": [],
        "modified": "ignored",
        "transfers": [],
        **kwargs,
    }


def ethereum_tx(tx_hash: str, block_number: int = 10, transfers=()) -> dict:
    return {
        "txType": "ETHEREUM_TRANSACTION",
        "txHash": tx_hash,
        "blockNumber": block_number,
        "transfers": list(transfers),
    }


class TestCompareWithSafeService(TestCase):
    def setUp(self):
        self.ours = FakeService()
        self.reference = FakeService()
        self.addCleanup(self.ours.stop)
        self.addCleanup(self.reference.stop)

    def compare(self, page_size: int = 100, max_items: int | None = None):
        return compare_safe(
            ApiClient(self.ours.url, delay=0),
            ApiClient(self.reference.url, delay=0),
            SAFE,
            INDEXED_BLOCK,
            page_size=page_size,
            max_items=max_items,
        )

    def test_normalize(self):
        self.assertEqual(
            normalize(
                {
                    "modified": 1,
                    "nested": [{"created": 1, "value": 2}],
                    "transfers": [{"transferId": "b"}, {"transferId": "a"}],
                }
            ),
            {
                "nested": [{"value": 2}],
                "transfers": [{"transferId": "a"}, {"transferId": "b"}],
            },
        )

    def test_identical_with_pagination(self):
        transactions = [ethereum_tx(f"0x{i:02x}") for i in range(5)]
        self.ours.set_safe(transactions, page_size=2)
        self.reference.set_safe(transactions, page_size=2)
        report = self.compare(page_size=2)
        self.assertEqual(report.differences, [])
        self.assertEqual(report.compared_transactions, 5)
        # 3 pages
        self.assertEqual(
            len([path for path in self.ours.requests if "all-transactions" in path]), 3
        )
        # Only the latest items
        self.assertEqual(
            self.compare(page_size=2, max_items=3).compared_transactions, 3
        )

    def test_unexpected_differences(self):
        self.ours.set_safe(
            [ethereum_tx("0x01", transfers=[{"transferId": "a", "value": "1"}])],
            nonce=2,
        )
        self.reference.set_safe(
            [
                ethereum_tx("0x01", transfers=[{"transferId": "a", "value": "2"}]),
                multisig("0x02"),  # Executed, missing in ours
            ]
        )
        report = self.compare()
        self.assertCountEqual(
            [(d.endpoint, d.path, d.ours, d.reference) for d in report.unexpected],
            [
                ("safe", ".data.nonce", "2", "1"),
                ("all-transactions", "[ethereum:0x01].transfers[0].value", "1", "2"),
                ("all-transactions", "[multisig:0x02]", "<missing>", "<present>"),
            ],
        )
        self.assertEqual(report.expected, [])

    def test_expected_off_chain_differences(self):
        self.ours.set_safe([multisig("0x01")])
        self.reference.set_safe(
            [
                # Confirmations and origin of an API proposal
                multisig(
                    "0x01",
                    confirmations=[{"owner": "0xA"}, {"owner": "0xB"}],
                    origin='{"url": "https://app.safe.global"}',
                ),
                # Proposed, not executed
                multisig("0x02", block_number=None),
            ]
        )
        report = self.compare()
        self.assertEqual(report.unexpected, [])
        self.assertEqual(
            {d.category for d in report.expected},
            {"off-chain"},
        )
        self.assertEqual(
            {d.path.split(".")[0].split("]")[0] + "]" for d in report.expected},
            {"[multisig:0x01]", "[multisig:0x02]"},
        )

    def test_transactions_after_cutoff_ignored(self):
        self.ours.set_safe([ethereum_tx("0x01")])
        self.reference.set_safe(
            [ethereum_tx("0x01"), ethereum_tx("0x02", block_number=INDEXED_BLOCK + 1)]
        )
        report = self.compare()
        self.assertEqual(report.differences, [])
        self.assertEqual(report.compared_transactions, 1)

    def test_retry_throttled(self):
        self.reference.set_safe([])
        self.reference.fail_first["/api/v1/about/indexing/"] = 2
        sleeps = []
        client = ApiClient(self.reference.url, delay=0, sleep=sleeps.append)
        self.assertEqual(client.indexed_block_number(), INDEXED_BLOCK)
        self.assertEqual(self.reference.requests.count("/api/v1/about/indexing/"), 3)
        self.assertIn(1, sleeps)
        self.assertIn(2, sleeps)

    def test_main(self):
        self.ours.set_safe([ethereum_tx("0x01")], indexed_block=INDEXED_BLOCK + 5)
        self.reference.set_safe([ethereum_tx("0x01")])
        args = [
            "--ours",
            self.ours.url,
            "--reference",
            self.reference.url,
            "--safes",
            SAFE,
            "--delay",
            "0",
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_path = os.path.join(tmp_dir, "report.json")
            stdout = StringIO()
            with redirect_stdout(stdout):
                self.assertEqual(main(args + ["--json", json_path]), 0)
            self.assertIn(
                f"Comparing transactions up to block {INDEXED_BLOCK}", stdout.getvalue()
            )
            self.assertIn(f"{SAFE}: OK", stdout.getvalue())
            with open(json_path) as f:
                self.assertEqual(json.load(f)["safes"][0]["differences"], [])

        self.ours.set_safe([], nonce=5)
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(main(args), 1)
        self.assertIn("differences", stdout.getvalue())

        # Unknown Safe in both services
        with redirect_stdout(StringIO()) as stdout:
            self.assertEqual(
                main(
                    args[:-3]
                    + ["0x0000000000000000000000000000000000000001", "--delay", "0"]
                ),
                1,
            )
        self.assertIn("ERROR", stdout.getvalue())
