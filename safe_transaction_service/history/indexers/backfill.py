"""
Indexers for backfilling the history of whitelisted Safes over a block range, reusing the
stock `EthereumIndexer.process_addresses` driver (block limit auto-adjust and the reset
to 1 on provider errors) with an in-memory cursor instead of the global progress markers.
"""

from collections.abc import Callable
from logging import getLogger

from django.conf import settings

from celery.exceptions import SoftTimeLimitExceeded
from eth_typing import ChecksumAddress
from requests import Timeout
from safe_eth.eth import EthereumClient
from web3.exceptions import Web3RPCError

from .erc20_events_indexer import Erc20EventsIndexer
from .ethereum_indexer import EthereumIndexer, FindRelevantElementsException
from .safe_events_indexer import SafeEventsIndexer

logger = getLogger(__name__)

# Errors `EthereumIndexer.process_addresses` handles: `block_process_limit` is set to 1
# and the cursor is not moved, so the window can be retried
BACKFILL_RETRYABLE_ERRORS = (
    FindRelevantElementsException,
    SoftTimeLimitExceeded,
    Timeout,
    ValueError,
    Web3RPCError,
)
PROGRESS_LOG_EVERY = 10


class BackfillGaveUp(Exception):
    def __init__(self, cursor: int, error: Exception):
        self.cursor = cursor
        super().__init__(
            f"Too many provider errors with block_process_limit=1 at block {cursor}: {error}"
        )


class BackfillCursorMixin:
    """
    Keep the indexing progress in memory. Must come first in the MRO, as
    `Erc20EventsIndexer` overrides these methods to use `IndexingStatus`
    """

    _cursor: int

    def set_cursor(self, block_number: int) -> None:
        self._cursor = block_number

    @property
    def cursor(self) -> int:
        return self._cursor

    def get_from_block_number(
        self, addresses: set[ChecksumAddress] | None = None
    ) -> int:
        return self._cursor

    def update_monitored_addresses(
        self,
        addresses: set[str],
        from_block_number: int,
        to_block_number: int,
    ) -> bool:
        assert from_block_number == self._cursor, (
            f"Unexpected from-block-number={from_block_number}, cursor={self._cursor}"
        )
        self._cursor = to_block_number + 1
        return True


class BackfillSafeEventsIndexer(BackfillCursorMixin, SafeEventsIndexer):
    pass


class BackfillErc20EventsIndexer(BackfillCursorMixin, Erc20EventsIndexer):
    pass


def build_backfill_indexers(
    max_block_process_limit: int,
) -> tuple[BackfillSafeEventsIndexer, BackfillErc20EventsIndexer]:
    """
    :param max_block_process_limit: Maximum number of blocks per `eth_getLogs`
    :return: Safe events and ERC20 indexers for the backfill
    """
    kwargs = {
        "confirmations": 0,
        "blocks_to_reindex_again": 0,
        "block_process_limit": min(1_000, max_block_process_limit),
        "block_process_limit_max": max_block_process_limit,
    }
    safe_events_indexer = BackfillSafeEventsIndexer(
        EthereumClient(settings.ETHEREUM_NODE_URL), **kwargs
    )
    # Explicit addresses, like `reindex_master_copies --addresses`
    safe_events_indexer.IGNORE_ADDRESSES_ON_LOG_FILTER = False
    erc20_events_indexer = BackfillErc20EventsIndexer(
        EthereumClient(settings.ETHEREUM_NODE_URL), **kwargs
    )
    return safe_events_indexer, erc20_events_indexer


def run_backfill(
    indexer: EthereumIndexer,
    addresses: set[ChecksumAddress],
    start: int,
    end: int,
    max_failures_at_min: int = 3,
    on_progress: Callable[[int, int], None] | None = None,
) -> int:
    """
    Index `addresses` from block `start` to `end` (both included), retrying provider errors

    :param indexer: A `BackfillCursorMixin` indexer
    :param addresses:
    :param start:
    :param end:
    :param max_failures_at_min: Consecutive errors with `block_process_limit=1` before
        giving up
    :param on_progress: Called with `(cursor, block_process_limit)` after every window
    :return: Number of elements processed
    :raises BackfillGaveUp: with the cursor to resume from
    """
    assert isinstance(indexer, BackfillCursorMixin)
    indexer.set_cursor(start)
    elements_number = 0
    failures_at_min = 0
    iterations = 0
    while True:
        block_process_limit = indexer.block_process_limit
        try:
            elements, _, _, updated = indexer.process_addresses(
                addresses, current_block_number=end
            )
        except BACKFILL_RETRYABLE_ERRORS as e:
            failures_at_min = failures_at_min + 1 if block_process_limit == 1 else 0
            logger.warning(
                "%s: Provider error at cursor=%d block-process-limit=%d: %s",
                indexer.__class__.__name__,
                indexer.cursor,
                block_process_limit,
                e,
            )
            if failures_at_min >= max_failures_at_min:
                raise BackfillGaveUp(indexer.cursor, e) from e
            continue

        failures_at_min = 0
        elements_number += len(elements)
        iterations += 1
        if on_progress:
            on_progress(indexer.cursor, indexer.block_process_limit)
        if updated or iterations % PROGRESS_LOG_EVERY == 0:
            logger.info(
                "%s: Backfill cursor=%d end=%d block-process-limit=%d elements=%d",
                indexer.__class__.__name__,
                indexer.cursor,
                end,
                indexer.block_process_limit,
                elements_number,
            )
        if updated:
            return elements_number
