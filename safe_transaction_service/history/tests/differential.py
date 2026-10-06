"""
Helpers to compare the indexing result of different variants (stock, whitelist, backfill)
on the same chain: reset the indexing state, take normalised snapshots of the database and
the API, and diff them.
"""

import datetime
import json
from collections.abc import Callable, Collection
from decimal import Decimal
from typing import Any, NamedTuple

from django.core.cache import cache
from django.db.models import Model, Q
from django.test import Client
from django.urls import reverse

from eth_utils import is_checksum_address, to_checksum_address
from hexbytes import HexBytes

from ..indexers import Erc20EventsIndexerProvider, SafeEventsIndexerProvider
from ..indexers.tx_processor import SafeTxProcessorProvider
from ..models import (
    ERC20Transfer,
    EthereumBlock,
    EthereumTx,
    IndexingStatus,
    InternalTx,
    InternalTxDecoded,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
)
from ..services import BalanceServiceProvider, IndexServiceProvider

Snapshot = dict[str, dict[Any, dict[str, Any]]]

API_IGNORED_KEYS = {
    "modified",
    "submissionDate",
    "created",
    "next",
    "previous",
    "count",
}


class DiffLine(NamedTuple):
    table: str
    key: Any
    column: str | None  # `None` for a missing/extra key
    value_a: Any
    value_b: Any


def normalize(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return HexBytes(bytes(value)).to_0x_hex().lower()
    if isinstance(value, str) and len(value) == 42 and value.startswith("0x"):
        try:
            return to_checksum_address(value)
        except ValueError:
            return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(
            {key: normalize(item) for key, item in value.items()}, sort_keys=True
        )
    if isinstance(value, list | tuple):
        return tuple(normalize(item) for item in value)
    return value


def reset_indexing_state(range_start: int) -> None:
    """
    Remove every indexed row, rewind the indexers to `range_start` and drop the cached
    singletons, so the next variant starts from scratch
    """
    MultisigConfirmation.objects.all().delete()
    MultisigTransaction.objects.all().delete()
    SafeContract.objects.all().delete()
    EthereumBlock.objects.all().delete()  # Cascades to txs and indexed rows
    for model in (
        SafeStatus,
        SafeLastStatus,
        ModuleTransaction,
        SafeRelevantTransaction,
        InternalTxDecoded,
        InternalTx,
        ERC20Transfer,
        EthereumTx,
    ):
        model.objects.all().delete()
    SafeMasterCopy.objects.update(tx_block_number=range_start)
    IndexingStatus.objects.set_erc20_721_indexing_status(range_start)
    for provider in (
        SafeEventsIndexerProvider,
        Erc20EventsIndexerProvider,
        IndexServiceProvider,
        SafeTxProcessorProvider,
        BalanceServiceProvider,  # In-memory token info cache
    ):
        provider.del_singleton()
    cache.clear()

    # Leftovers would make the comparison vacuous
    for model in COMPARED_MODELS:
        assert not model.objects.exists(), f"{model.__name__} not empty after reset"


def _internal_tx_keys(internal_tx_ids: Collection[int]) -> dict[int, tuple]:
    return {
        internal_tx_id: (normalize(ethereum_tx_id), trace_address)
        for internal_tx_id, ethereum_tx_id, trace_address in InternalTx.objects.filter(
            id__in=internal_tx_ids
        ).values_list("id", "ethereum_tx_id", "trace_address")
    }


def snapshot_table(
    model: type[Model],
    filter_q: Q,
    key_fn: Callable[[dict[str, Any]], Any],
    ignore: set[str],
) -> dict[Any, dict[str, Any]]:
    """
    :return: `{natural key: {column: normalised value}}`. `internal_tx_id` foreign keys
        are replaced by the natural key of the `InternalTx`
    """
    rows = list(model.objects.filter(filter_q).values())
    internal_tx_keys = _internal_tx_keys(
        [row["internal_tx_id"] for row in rows if "internal_tx_id" in row]
    )
    snapshot = {}
    for row in rows:
        if "internal_tx_id" in row:
            row["internal_tx_id"] = internal_tx_keys[row["internal_tx_id"]]
        normalized = {
            column: normalize(value)
            for column, value in row.items()
            if column not in ignore
        }
        key = key_fn(normalized)
        assert key not in snapshot, f"Duplicated key {key} in {model.__name__}"
        snapshot[key] = normalized
    return snapshot


def _in(field: str, addresses: Collection[str]) -> Q:
    return Q(**{f"{field}__in": list(addresses)})


def table_filters(addresses: Collection[str]) -> dict[type[Model], Q]:
    """
    :return: Filter of the rows related to `addresses` for every compared table
    """
    return {
        SafeContract: _in("address", addresses),
        SafeLastStatus: _in("address", addresses),
        SafeStatus: _in("address", addresses),
        MultisigTransaction: _in("safe", addresses),
        MultisigConfirmation: _in("multisig_transaction__safe", addresses),
        ModuleTransaction: _in("safe", addresses),
        InternalTx: _in("_from", addresses)
        | _in("to", addresses)
        | _in("contract_address", addresses),
        InternalTxDecoded: _in("safe_address", addresses),
        ERC20Transfer: _in("_from", addresses) | _in("to", addresses),
        SafeRelevantTransaction: _in("safe", addresses),
    }


# model: (natural key, ignored columns)
TABLES: dict[type[Model], tuple[Callable[[dict], Any], set[str]]] = {
    SafeContract: (lambda row: row["address"], {"created"}),
    SafeLastStatus: (lambda row: row["address"], set()),
    SafeStatus: (lambda row: row["internal_tx_id"], set()),
    MultisigTransaction: (lambda row: row["safe_tx_hash"], {"created", "modified"}),
    MultisigConfirmation: (
        lambda row: (row["multisig_transaction_id"], row["owner"]),
        {"id", "created", "modified"},
    ),
    ModuleTransaction: (lambda row: row["internal_tx_id"], {"created", "modified"}),
    InternalTx: (lambda row: (row["ethereum_tx_id"], row["trace_address"]), {"id"}),
    InternalTxDecoded: (lambda row: row["internal_tx_id"], set()),
    ERC20Transfer: (lambda row: (row["ethereum_tx_id"], row["log_index"]), {"id"}),
    SafeRelevantTransaction: (lambda row: (row["ethereum_tx_id"], row["safe"]), {"id"}),
}
COMPARED_MODELS = list(TABLES)


def take_snapshot(addresses: Collection[str]) -> Snapshot:
    filters = table_filters(addresses)
    return {
        model.__name__: snapshot_table(model, filters[model], key_fn, ignore)
        for model, (key_fn, ignore) in TABLES.items()
    }


def normalize_api(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: normalize_api(item)
            for key, item in value.items()
            if key not in API_IGNORED_KEYS
        }
    if isinstance(value, list):
        return [normalize_api(item) for item in value]
    if isinstance(value, str) and is_checksum_address(value):
        return value
    return value


def snapshot_api(client: Client, addresses: Collection[str]) -> Snapshot:
    """
    :return: `{endpoint: {address: normalised response}}`. Every page of
        `all-transactions` is fetched
    """
    snapshot: Snapshot = {}
    for address in sorted(addresses):
        for name in ("safe-info", "safe-creation", "safe-balances"):
            response = client.get(reverse(f"v1:history:{name}", args=(address,)))
            snapshot.setdefault(name, {})[address] = {
                "status": response.status_code,
                "data": normalize_api(response.json()),
            }
        results = []
        url = reverse("v1:history:all-transactions", args=(address,))
        while url:
            response = client.get(url)
            assert response.status_code == 200, response.content
            results.extend(response.json()["results"])
            url = response.json()["next"]
        snapshot.setdefault("all-transactions", {})[address] = {
            "status": 200,
            "data": normalize_api(results),
        }
    return snapshot


def diff_snapshots(
    name_a: str, snapshot_a: Snapshot, name_b: str, snapshot_b: Snapshot
) -> list[DiffLine]:
    diff_lines: list[DiffLine] = []
    for table in sorted(set(snapshot_a) | set(snapshot_b)):
        rows_a, rows_b = snapshot_a.get(table, {}), snapshot_b.get(table, {})
        for key in sorted(set(rows_a) | set(rows_b), key=repr):
            if key not in rows_b:
                diff_lines.append(DiffLine(table, key, None, "<present>", "<missing>"))
            elif key not in rows_a:
                diff_lines.append(DiffLine(table, key, None, "<missing>", "<present>"))
            else:
                row_a, row_b = rows_a[key], rows_b[key]
                for column in sorted(set(row_a) | set(row_b)):
                    if row_a.get(column) != row_b.get(column):
                        diff_lines.append(
                            DiffLine(
                                table, key, column, row_a.get(column), row_b.get(column)
                            )
                        )
    return diff_lines


def nested_differences(value_a: Any, value_b: Any, path: str = "") -> list[str]:
    """
    :return: Path and values of every difference in nested lists/dicts
    """
    if isinstance(value_a, dict) and isinstance(value_b, dict):
        return [
            difference
            for key in sorted(set(value_a) | set(value_b), key=repr)
            for difference in nested_differences(
                value_a.get(key, "<missing>"),
                value_b.get(key, "<missing>"),
                f"{path}.{key}",
            )
        ]
    if isinstance(value_a, list) and isinstance(value_b, list):
        differences = [
            difference
            for i, (item_a, item_b) in enumerate(zip(value_a, value_b, strict=False))
            for difference in nested_differences(item_a, item_b, f"{path}[{i}]")
        ]
        if len(value_a) != len(value_b):
            differences.append(f"{path}: length {len(value_a)} != {len(value_b)}")
        return differences
    if value_a != value_b:
        return [f"{path}: {value_a!r} != {value_b!r}"]
    return []


def format_diff(
    name_a: str, name_b: str, diff_lines: list[DiffLine], max_lines: int = 100
) -> str:
    def short(value: Any) -> str:
        text = repr(value)
        return text if len(text) <= 80 else text[:77] + "..."

    lines = []
    for line in diff_lines[:max_lines]:
        detail = (
            "; ".join(nested_differences(line.value_a, line.value_b)[:10])
            if isinstance(line.value_a, list | dict)
            else None
        )
        lines.append(
            f"{line.table} {short(line.key)} {line.column or '<row>'}: "
            + (
                detail
                or f"{name_a}={short(line.value_a)} {name_b}={short(line.value_b)}"
            )
        )
    counts: dict[str, int] = {}
    for line in diff_lines:
        counts[line.table] = counts.get(line.table, 0) + 1
    lines.append(f"{len(diff_lines)} differences, per table: {counts}")
    return "\n".join(lines)
