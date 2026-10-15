"""
Compare the API of a whitelist deployment with a reference Safe Transaction Service
(stock, indexing every Safe) for the whitelisted Safes:

- `/api/v1/safes/{address}/`
- `/api/v1/safes/{address}/creation/`
- `/api/v1/safes/{address}/all-transactions/` (every page, matched by tx id)

Read-only GET requests. Volatile keys (`modified`, `created`...) are ignored, and only
transactions mined up to the lowest block indexed by both services are compared.

Some differences are expected and reported apart from the real ones:

- `off-chain`: data only known by the service receiving API requests, like transactions
  proposed but not executed and confirmations or metadata of proposals (`origin`,
  `proposer`...). A whitelist deployment only indexes the chain.

Usage::

    python scripts/compare_with_safe_service.py --ours http://localhost:8000 \\
        --safes 0x...,0x...

Exit code: 0 if no unexpected differences, 1 otherwise.
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DEFAULT_REFERENCE = "https://transaction-ethereum.safe.protofire.io"
IGNORED_KEYS = {"modified", "created", "submissionDate", "next", "previous", "count"}
# Multisig transaction data coming from API proposals, not from the chain
OFF_CHAIN_KEYS = {
    "confirmations",
    "origin",
    "proposer",
    "proposedByDelegate",
    "trusted",
}
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_SHOWN_DIFFERENCES = 10


@dataclass
class Difference:
    endpoint: str
    path: str
    ours: Any
    reference: Any
    category: str | None = None  # `None` for unexpected differences

    def __str__(self) -> str:
        return f"{self.endpoint}{self.path}: ours={short(self.ours)} reference={short(self.reference)}"


@dataclass
class SafeReport:
    address: str
    differences: list[Difference] = field(default_factory=list)
    compared_transactions: int = 0

    @property
    def unexpected(self) -> list[Difference]:
        return [
            difference for difference in self.differences if difference.category is None
        ]

    @property
    def expected(self) -> list[Difference]:
        return [
            difference
            for difference in self.differences
            if difference.category is not None
        ]


def short(value: Any, length: int = 80) -> str:
    text = json.dumps(value, sort_keys=True)
    return text if len(text) <= length else text[: length - 3] + "..."


class ApiClient:
    def __init__(
        self,
        base_url: str,
        delay: float = 0.2,
        retries: int = 5,
        timeout: int = 60,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.sleep = sleep

    def get(self, url: str) -> tuple[int, Any]:
        """
        :param url: Absolute or relative to the base url
        :return: Status code and decoded JSON (`None` if not JSON)
        """
        if not url.startswith("http"):
            url = self.base_url + url
        for attempt in range(self.retries + 1):
            self.sleep(self.delay)
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_STATUS or attempt == self.retries:
                    body = e.read()
                    try:
                        return e.code, json.loads(body)
                    except ValueError:
                        return e.code, None
            except urllib.error.URLError:
                if attempt == self.retries:
                    raise
            self.sleep(2**attempt)
        raise AssertionError("Unreachable")

    def get_ok(self, url: str) -> Any:
        status, data = self.get(url)
        if status != 200:
            raise RuntimeError(f"GET {url} returned {status}: {data}")
        return data

    def indexed_block_number(self) -> int:
        indexing = self.get_ok("/api/v1/about/indexing/")
        return min(indexing["masterCopiesBlockNumber"], indexing["erc20BlockNumber"])

    def all_transactions(
        self, address: str, page_size: int, max_items: int | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        url: str | None = f"/api/v1/safes/{address}/all-transactions/?limit={page_size}"
        while url and (max_items is None or len(results) < max_items):
            page = self.get_ok(url)
            results.extend(page["results"])
            url = page["next"]
        return results if max_items is None else results[:max_items]


def normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize(item)
            for key, item in value.items()
            if key not in IGNORED_KEYS
        }
    if isinstance(value, list):
        items = [normalize(item) for item in value]
        if items and all(
            isinstance(item, dict) and "transferId" in item for item in items
        ):
            return sorted(items, key=lambda item: item["transferId"])
        return items
    return value


def differences(value_a: Any, value_b: Any, path: str = "") -> list[tuple]:
    """
    :return: `(path, value_a, value_b)` for every differing leaf
    """
    if isinstance(value_a, dict) and isinstance(value_b, dict):
        return [
            difference
            for key in sorted(value_a.keys() | value_b.keys())
            for difference in differences(
                value_a.get(key, "<missing>"),
                value_b.get(key, "<missing>"),
                f"{path}.{key}",
            )
        ]
    if (
        isinstance(value_a, list)
        and isinstance(value_b, list)
        and len(value_a) == len(value_b)
    ):
        return [
            difference
            for i, (item_a, item_b) in enumerate(zip(value_a, value_b, strict=True))
            for difference in differences(item_a, item_b, f"{path}[{i}]")
        ]
    return [] if value_a == value_b else [(path, value_a, value_b)]


def transaction_id(transaction: dict[str, Any]) -> str:
    tx_type = transaction.get("txType")
    if tx_type == "MULTISIG_TRANSACTION":
        return f"multisig:{transaction['safeTxHash']}"
    if tx_type == "MODULE_TRANSACTION":
        return f"module:{transaction['moduleTransactionId']}"
    return f"ethereum:{transaction['txHash']}"


def is_off_chain(transaction: dict[str, Any]) -> bool:
    """
    :return: `True` for multisig transactions proposed through the API, but not executed
    """
    return transaction.get("txType") == "MULTISIG_TRANSACTION" and not transaction.get(
        "isExecuted"
    )


def category_for_transaction_path(path: str, tx_id: str) -> str | None:
    """
    :param path: `[tx_id].key...`
    """
    key = path[len(tx_id) + 3 :].split(".")[0].split("[")[0]
    return "off-chain" if key in OFF_CHAIN_KEYS else None


def compare_transactions(
    ours: list[dict[str, Any]],
    reference: list[dict[str, Any]],
    cutoff_block_number: int,
) -> tuple[list[Difference], int]:
    """
    :return: differences and number of compared transactions
    """

    def by_id(transactions):
        return {
            transaction_id(transaction): normalize(transaction)
            for transaction in transactions
            if transaction.get("blockNumber") is None
            or transaction["blockNumber"] <= cutoff_block_number
        }

    ours_by_id, reference_by_id = by_id(ours), by_id(reference)
    found: list[Difference] = []
    for tx_id in sorted(ours_by_id.keys() | reference_by_id.keys()):
        ours_tx = ours_by_id.get(tx_id)
        reference_tx = reference_by_id.get(tx_id)
        endpoint = "all-transactions"
        if ours_tx is None or reference_tx is None:
            present = ours_tx or reference_tx
            found.append(
                Difference(
                    endpoint,
                    f"[{tx_id}]",
                    "<present>" if ours_tx else "<missing>",
                    "<present>" if reference_tx else "<missing>",
                    "off-chain" if is_off_chain(present) else None,
                )
            )
            continue
        for path, ours_value, reference_value in differences(
            ours_tx, reference_tx, f"[{tx_id}]"
        ):
            found.append(
                Difference(
                    endpoint,
                    path,
                    ours_value,
                    reference_value,
                    category_for_transaction_path(path, tx_id),
                )
            )
    return found, len(ours_by_id.keys() & reference_by_id.keys())


def compare_safe(
    ours: ApiClient,
    reference: ApiClient,
    address: str,
    cutoff_block_number: int,
    page_size: int = 100,
    max_items: int | None = None,
) -> SafeReport:
    report = SafeReport(address)
    for endpoint, path in (
        ("safe", f"/api/v1/safes/{address}/"),
        ("creation", f"/api/v1/safes/{address}/creation/"),
    ):
        ours_response = ours.get(path)
        reference_response = reference.get(path)
        for diff_path, ours_value, reference_value in differences(
            normalize(
                {"status": ours_response[0], "data": ours_response[1]},
            ),
            normalize(
                {"status": reference_response[0], "data": reference_response[1]},
            ),
        ):
            report.differences.append(
                Difference(endpoint, diff_path, ours_value, reference_value)
            )

    transaction_differences, compared = compare_transactions(
        ours.all_transactions(address, page_size, max_items),
        reference.all_transactions(address, page_size, max_items),
        cutoff_block_number,
    )
    report.differences.extend(transaction_differences)
    report.compared_transactions = compared
    return report


def format_report(report: SafeReport) -> str:
    unexpected, expected = report.unexpected, report.expected
    status = "OK" if not unexpected else f"{len(unexpected)} differences"
    lines = [
        (
            f"{report.address}: {status} ({report.compared_transactions} transactions "
            f"compared, {len(expected)} expected differences)"
        )
    ]
    lines += [f"  - {difference}" for difference in unexpected[:MAX_SHOWN_DIFFERENCES]]
    if len(unexpected) > MAX_SHOWN_DIFFERENCES:
        lines.append(f"  ... {len(unexpected) - MAX_SHOWN_DIFFERENCES} more")
    categories: dict[str, int] = {}
    for difference in expected:
        categories[difference.category] = categories.get(difference.category, 0) + 1
    lines += [
        f"  expected {name}: {number}" for name, number in sorted(categories.items())
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ours", required=True, help="Whitelist deployment base URL")
    parser.add_argument(
        "--reference",
        default=DEFAULT_REFERENCE,
        help=f"Reference service base URL (default {DEFAULT_REFERENCE})",
    )
    parser.add_argument(
        "--safes", required=True, help="Comma separated checksummed Safe addresses"
    )
    parser.add_argument(
        "--delay", type=float, default=0.2, help="Seconds between requests"
    )
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--max-items",
        type=int,
        help="Compare only the latest N transactions of every Safe",
    )
    parser.add_argument("--json", help="Write the full report to this file")
    args = parser.parse_args(argv)

    ours = ApiClient(args.ours, delay=args.delay)
    reference = ApiClient(args.reference, delay=args.delay)
    cutoff_block_number = min(
        ours.indexed_block_number(), reference.indexed_block_number()
    )
    print(f"Comparing transactions up to block {cutoff_block_number}")

    reports = []
    for address in [safe.strip() for safe in args.safes.split(",") if safe.strip()]:
        try:
            report = compare_safe(
                ours,
                reference,
                address,
                cutoff_block_number,
                page_size=args.page_size,
                max_items=args.max_items,
            )
        except (RuntimeError, OSError) as e:
            report = SafeReport(address)
            report.differences.append(Difference("error", "", str(e), None))
            print(f"{address}: ERROR {e}")
        else:
            print(format_report(report))
        reports.append(report)

    if args.json:
        with open(args.json, "w") as f:
            json.dump(
                {
                    "cutoffBlockNumber": cutoff_block_number,
                    "safes": [
                        {
                            "address": report.address,
                            "comparedTransactions": report.compared_transactions,
                            "differences": [
                                {
                                    "endpoint": difference.endpoint,
                                    "path": difference.path,
                                    "ours": difference.ours,
                                    "reference": difference.reference,
                                    "category": difference.category,
                                }
                                for difference in report.differences
                            ],
                        }
                        for report in reports
                    ],
                },
                f,
                indent=2,
            )
    return 1 if any(report.unexpected for report in reports) else 0


if __name__ == "__main__":
    sys.exit(main())
