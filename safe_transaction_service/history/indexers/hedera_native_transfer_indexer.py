import contextlib
import datetime
import itertools
import logging
from collections.abc import Iterator
from decimal import Decimal

from django.db import connection, transaction
from django.utils import timezone

from hexbytes import HexBytes
from safe_eth.eth.utils import fast_keccak_text
from safe_eth.util.util import to_0x_hex_str

from ..clients.hedera_mirror_node_client import (
    MIRROR_NODE_PAGE_SIZE,
    HederaMirrorNodeClient,
)
from ..models import (
    EthereumTx,
    EthereumTxCallType,
    HederaSafeTransferCursor,
    HederaTransferFeedCursor,
    InternalTx,
    InternalTxType,
    SafeContract,
    SafeRelevantTransaction,
)

logger = logging.getLogger(__name__)

TINYBAR_TO_WEIBAR = 10**10
# Postgres advisory lock id, any constant unique in the database
INDEXING_LOCK_ID = 0x48424152  # "HBAR"


def consensus_timestamp_to_datetime(consensus_timestamp: str) -> datetime.datetime:
    """
    :param consensus_timestamp: Mirror Node's ``"<seconds>.<nanos>"`` string.
    :return: Timezone-aware UTC datetime (nanosecond precision is truncated
        to the microsecond precision Django's DateTimeField stores).
    """
    return datetime.datetime.fromtimestamp(float(consensus_timestamp), tz=datetime.UTC)


def datetime_to_consensus_timestamp(dt: datetime.datetime) -> str:
    """
    Inverse of :func:`consensus_timestamp_to_datetime`. Used to seed a newly
    tracked Safe's sync cursor at its creation time, so the first sync only
    looks forward from when the Safe started existing instead of scanning
    that Hedera account's entire transfer history.

    :param dt: Timezone-aware datetime.
    :return: Mirror Node ``"<seconds>.<nanos>"`` formatted string.
    """
    timestamp = dt.timestamp()
    seconds = int(timestamp)
    nanos = round((timestamp - seconds) * 1_000_000_000)
    return f"{seconds}.{nanos:09d}"


def consensus_timestamp_is_before(timestamp: str, other_timestamp: str) -> bool:
    """
    :param timestamp: Mirror Node ``"<seconds>.<nanos>"`` string.
    :param other_timestamp: Mirror Node ``"<seconds>.<nanos>"`` string.
    :return: Whether ``timestamp`` is strictly older than ``other_timestamp``.
    """
    return Decimal(timestamp) < Decimal(other_timestamp)


def build_synthetic_tx_hash(mirror_tx: dict, safe_address: str) -> str:
    """
    Build the placeholder ``EthereumTx.tx_hash`` for a native Hedera transfer.

    :param mirror_tx: One entry from Mirror Node's ``/api/v1/transactions``.
    :param safe_address: Checksummed address of the Safe being indexed.
    :return: ``0x``-prefixed keccak hash, stable across re-runs.
    """
    return to_0x_hex_str(
        fast_keccak_text(
            f"hedera-native:{mirror_tx['transaction_id']}:"
            f"{mirror_tx.get('nonce', 0)}:{safe_address}"
        )
    )


def extract_transfer_legs(mirror_tx: dict, safe_account_id: str) -> list[dict]:
    """
    Turn a Mirror Node ``CRYPTOTRANSFER`` transaction into the list of HBAR
    movements from each real sender to ``safe_account_id``.

    :param mirror_tx: One entry from Mirror Node's
        ``/api/v1/transactions?transactiontype=CRYPTOTRANSFER`` response.
    :param safe_account_id: The Safe's own Hedera account id, e.g. ``"0.0.X"``.
    :return: ``[{"counterparty_account_id": str, "amount_tinybar": int}, ...]``,
        where ``amount_tinybar`` is always positive (HBAR received by the Safe).
    """
    transfers = mirror_tx.get("transfers") or []
    safe_leg = next((t for t in transfers if t["account"] == safe_account_id), None)
    if not safe_leg or safe_leg["amount"] <= 0:
        return []

    safe_net = safe_leg["amount"]
    payer_account_id = mirror_tx["transaction_id"].split("-")[0]
    charged_tx_fee = mirror_tx.get("charged_tx_fee") or 0

    sender_legs = []
    for t in transfers:
        if t["account"] == safe_account_id or t["amount"] >= 0:
            # Not a real sender: either a fee-collector/reward account
            # or another simultaneous receiver in the same transaction.
            continue
        amount = t["amount"]
        if t["account"] == payer_account_id:
            # This account absorbed the transaction fee as its payer; remove
            # it so the leg reflects only the HBAR actually sent.
            amount += charged_tx_fee
        if amount >= 0:
            # The payer only paid the fee and sent nothing themselves.
            continue
        sender_legs.append({"account": t["account"], "amount": amount})

    if not sender_legs:
        return []

    if len(sender_legs) == 1:
        return [
            {
                "counterparty_account_id": sender_legs[0]["account"],
                "amount_tinybar": safe_net,
            }
        ]

    logger.info(
        "Multi-party Hedera crypto transfer %s to safe=%s: apportioning "
        "the Safe's net amount across %d senders",
        mirror_tx["transaction_id"],
        safe_account_id,
        len(sender_legs),
    )

    total_sender_magnitude = sum(abs(leg["amount"]) for leg in sender_legs)
    result = []
    allocated = 0
    for index, leg in enumerate(sender_legs):
        if index == len(sender_legs) - 1:
            share = safe_net - allocated
        else:
            share = safe_net * abs(leg["amount"]) // total_sender_magnitude
            allocated += share
        result.append(
            {
                "counterparty_account_id": leg["account"],
                "amount_tinybar": share,
            }
        )
    return result


class HederaNativeTransferIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = cls.get_new_instance()
        return cls.instance

    @classmethod
    def get_new_instance(cls) -> "HederaNativeTransferIndexer":
        from django.conf import settings

        return HederaNativeTransferIndexer(
            client=HederaMirrorNodeClient(
                base_url=settings.HEDERA_MIRROR_NODE_URL,
                api_key=settings.HEDERA_MIRROR_NODE_API_KEY,
                rate_limit_rps=settings.HEDERA_MIRROR_NODE_RATE_LIMIT_RPS,
                request_timeout=settings.HEDERA_MIRROR_NODE_REQUEST_TIMEOUT,
                max_retries=settings.HEDERA_MIRROR_NODE_MAX_RETRIES,
            ),
            max_txs_per_safe_per_run=settings.HEDERA_NATIVE_TRANSFER_MAX_TXS_PER_SAFE_PER_RUN,
            feed_max_pages_per_run=settings.HEDERA_NATIVE_TRANSFER_FEED_MAX_PAGES_PER_RUN,
            feed_lag_seconds=settings.HEDERA_NATIVE_TRANSFER_FEED_LAG_SECONDS,
        )

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


@contextlib.contextmanager
def indexing_lock() -> Iterator[bool]:
    """
    Database-level guard so two indexing runs never overlap, even if the
    Celery task lock expires or a run is started by hand. Released when the
    run ends, or with the connection if the worker dies.

    :return: Whether the lock was acquired
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [INDEXING_LOCK_ID])
        acquired = cursor.fetchone()[0]
    try:
        yield acquired
    finally:
        if acquired:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [INDEXING_LOCK_ID])


class UnresolvedBlockNumber(Exception):
    """
    Mirror Node returned no block for a transfer's consensus timestamp. The
    transfer (and everything after it) is retried on the next run.
    """


class CachedMirrorNodeLookups:
    """
    Block number and EVM address lookups, cached for the duration of one
    indexing run so a block or counterparty shared by several transfers is
    only requested once.
    """

    def __init__(self, client: HederaMirrorNodeClient):
        self.client = client
        self.block_numbers: dict[str, int | None] = {}
        self.evm_addresses: dict[str, str] = {}

    def prefetch_block_numbers(self, consensus_timestamps: list[str]) -> None:
        """
        Resolve the block numbers of several close consensus timestamps (e.g.
        one feed page) with ``/blocks`` range requests instead of one request
        each. Timestamps left unresolved are looked up one by one later.
        """
        pending = sorted(
            {
                consensus_timestamp
                for consensus_timestamp in consensus_timestamps
                if consensus_timestamp not in self.block_numbers
            },
            key=Decimal,
        )
        while pending:
            number_pending = len(pending)
            for block in self.client.get_blocks(pending[0]):
                # Same match as `resolve_block_number`: the first block
                # ending at or after the timestamp
                block_end = Decimal(block["timestamp"]["to"])
                while pending and Decimal(pending[0]) <= block_end:
                    self.block_numbers[pending.pop(0)] = block["number"]
            if len(pending) == number_pending:
                # Not ingested yet
                return

    def get_block_number(self, consensus_timestamp: str) -> int:
        """
        :raises UnresolvedBlockNumber:
        """
        if consensus_timestamp not in self.block_numbers:
            self.block_numbers[consensus_timestamp] = self.client.resolve_block_number(
                consensus_timestamp
            )
        block_number = self.block_numbers[consensus_timestamp]
        if block_number is None:
            raise UnresolvedBlockNumber(consensus_timestamp)
        return block_number

    def get_evm_address(self, account_id: str) -> str:
        if account_id not in self.evm_addresses:
            self.evm_addresses[account_id] = self.client.resolve_evm_address(account_id)
        return self.evm_addresses[account_id]


class HederaNativeTransferIndexer:
    """
    Indexes native (non-EVM) HBAR transfers to Safes from the Hedera Mirror
    Node. Mirror Node has no multi-account filter, so instead of querying
    every Safe's account on every run, the indexer reads the network-wide
    ``CRYPTOTRANSFER`` feed once per run (see ``HederaTransferFeedCursor``)
    and matches it locally. A Safe only gets its own per-account query to
    catch up to that feed: when it's first seen, after being unbanned, or
    while a previous catch-up hasn't finished. From then on it follows the
    feed (``HederaSafeTransferCursor.following_feed``) until it's banned.
    """

    def __init__(
        self,
        client: HederaMirrorNodeClient,
        max_txs_per_safe_per_run: int = 5000,
        feed_max_pages_per_run: int = 1000,
        feed_lag_seconds: int = 30,
    ):
        self.client = client
        self.max_txs_per_safe_per_run = max_txs_per_safe_per_run
        self.feed_max_pages_per_run = feed_max_pages_per_run
        self.feed_lag_seconds = feed_lag_seconds

    def get_feed_until_timestamp(self) -> str:
        """
        :return: Newest consensus timestamp the feed may read up to. Kept
            ``feed_lag_seconds`` behind now so Mirror Node has finished
            ingesting everything before it.
        """
        return datetime_to_consensus_timestamp(
            timezone.now() - datetime.timedelta(seconds=self.feed_lag_seconds)
        )

    def process_all_safes(self) -> tuple[int, int] | None:
        """
        1. Catch up every Safe not following the feed yet with its own
           per-account query, up to the feed cursor. Safes already following
           it need no request.
        2. Read the network-wide feed from the feed cursor on and index the
           transfers to every Safe following it.

        :return: (number of Safes following the feed, number of InternalTx
            rows created), or ``None`` if skipped because another run is in
            progress
        """
        with indexing_lock() as acquired:
            if not acquired:
                logger.warning(
                    "Another Hedera native transfer indexing run is in progress, "
                    "skipping this one"
                )
                return None
            return self._process_all_safes()

    def _process_all_safes(self) -> tuple[int, int]:
        # Overlapping runs could move the feed cursor back, or let a Safe
        # join the feed after another run read past its join point
        feed_cursor, _ = HederaTransferFeedCursor.objects.get_or_create(
            id=HederaTransferFeedCursor.SINGLETON_ID,
            defaults={"last_consensus_timestamp": self.get_feed_until_timestamp()},
        )
        # Banned Safes leave the feed. They're indexed up to the feed cursor,
        # so if unbanned they are caught up from there
        HederaSafeTransferCursor.objects.filter(
            following_feed=True, safe_contract__banned=True
        ).update(
            following_feed=False,
            last_consensus_timestamp=feed_cursor.last_consensus_timestamp,
            updated=timezone.now(),
        )

        lookups = CachedMirrorNodeLookups(self.client)
        safe_cursors = {
            cursor.safe_contract_id: cursor
            for cursor in HederaSafeTransferCursor.objects.all()
        }

        following_cursors: list[HederaSafeTransferCursor] = []
        number_internal_txs_created = 0
        for safe_contract in SafeContract.objects.filter(banned=False).iterator():
            cursor = safe_cursors.get(safe_contract.address)
            if cursor is None:
                cursor = HederaSafeTransferCursor.objects.create(
                    safe_contract=safe_contract
                )
            if not cursor.following_feed:
                try:
                    caught_up, number_created = self.catch_up_safe(
                        safe_contract,
                        cursor,
                        feed_cursor.last_consensus_timestamp,
                        lookups,
                    )
                except Exception:
                    logger.exception(
                        "Error indexing Hedera native transfers for safe=%s",
                        safe_contract.address,
                    )
                    continue
                number_internal_txs_created += number_created
                if not caught_up:
                    continue
                cursor.following_feed = True
                cursor.save(update_fields=["following_feed", "updated"])
            following_cursors.append(cursor)

        number_internal_txs_created += self.process_feed(
            feed_cursor, following_cursors, lookups
        )
        return len(following_cursors), number_internal_txs_created

    def process_safe(
        self, safe_contract: SafeContract, until_timestamp: str | None = None
    ) -> int:
        """
        Index a single Safe with its own per-account query.

        :param safe_contract:
        :param until_timestamp: Stop at this consensus timestamp. ``None``
            reads up to the newest transfer.
        :return: Number of InternalTx rows created
        """
        cursor, _ = HederaSafeTransferCursor.objects.get_or_create(
            safe_contract=safe_contract
        )
        _, number_created = self.catch_up_safe(
            safe_contract,
            cursor,
            until_timestamp,
            CachedMirrorNodeLookups(self.client),
        )
        return number_created

    def catch_up_safe(
        self,
        safe_contract: SafeContract,
        cursor: HederaSafeTransferCursor,
        until_timestamp: str | None,
        lookups: CachedMirrorNodeLookups,
    ) -> tuple[bool, int]:
        """
        :return: (whether the Safe is now indexed up to ``until_timestamp``,
            number of InternalTx rows created)
        """
        if not cursor.hedera_account_id:
            account_id = self.client.resolve_account_id(safe_contract.address)
            if not account_id:
                return False, 0
            cursor.hedera_account_id = account_id
            # Seed the cursor at the Safe's REAL on-chain creation time —
            # not `safe_contract.created` (a DateField with auto_now_add,
            # which is when *our own EVM indexer* happened to notice the
            # Safe, lagging the real creation by however long block
            # confirmation takes). A HashPack/SDK deposit made in that gap
            # would otherwise fall before the cursor's seed point and be
            # silently, permanently skipped, since the cursor never looks
            # backward. `ethereum_tx.execution_date` is the creation
            # transaction's actual block timestamp; fall back to
            # `safe_contract.created` defensively in case it's ever unset.
            creation_timestamp = (
                safe_contract.ethereum_tx.execution_date or safe_contract.created
            )
            cursor.last_consensus_timestamp = datetime_to_consensus_timestamp(
                creation_timestamp
            )
            cursor.save(update_fields=["hedera_account_id", "last_consensus_timestamp"])

        if until_timestamp and not consensus_timestamp_is_before(
            cursor.last_consensus_timestamp, until_timestamp
        ):
            # Created after the feed cursor, or already caught up
            return True, 0

        ethereum_txs: list[EthereumTx] = []
        internal_txs: list[InternalTx] = []
        last_committed_timestamp = cursor.last_consensus_timestamp
        mirror_txs = self.client.get_crypto_transfers(
            cursor.hedera_account_id,
            after_timestamp=cursor.last_consensus_timestamp,
            until_timestamp=until_timestamp,
        )
        try:
            for mirror_txs_batch in itertools.batched(
                itertools.islice(mirror_txs, self.max_txs_per_safe_per_run),
                min(MIRROR_NODE_PAGE_SIZE, self.max_txs_per_safe_per_run),
                strict=False,
            ):
                lookups.prefetch_block_numbers(
                    [
                        mirror_tx["consensus_timestamp"]
                        for mirror_tx in mirror_txs_batch
                        if any(
                            transfer["account"] == cursor.hedera_account_id
                            and transfer["amount"] > 0
                            for transfer in mirror_tx.get("transfers") or []
                        )
                    ]
                )
                for mirror_tx in mirror_txs_batch:
                    ethereum_tx, new_internal_txs = self._build_transfer_rows(
                        mirror_tx,
                        safe_contract.address,
                        cursor.hedera_account_id,
                        lookups,
                    )
                    if ethereum_tx:
                        ethereum_txs.append(ethereum_tx)
                        internal_txs.extend(new_internal_txs)
                    last_committed_timestamp = mirror_tx["consensus_timestamp"]
            # Stopped by `max_txs_per_safe_per_run` if there's more to read
            caught_up = next(mirror_txs, None) is None
        except UnresolvedBlockNumber as exc:
            logger.warning(
                "Could not resolve block number for Hedera consensus_timestamp=%s, "
                "stopping sync for safe=%s until next run",
                exc,
                safe_contract.address,
            )
            caught_up = False
        except Exception:
            # Keep what was read so far, the rest is retried on the next run
            logger.exception(
                "Error reading Hedera native transfers for safe=%s after "
                "consensus_timestamp=%s",
                safe_contract.address,
                last_committed_timestamp,
            )
            caught_up = False

        if caught_up and until_timestamp:
            # Everything up to `until_timestamp` was read, not only up to the
            # last transfer found
            last_committed_timestamp = until_timestamp

        with transaction.atomic():
            number_created = self._store_transfer_rows(ethereum_txs, internal_txs)
            if last_committed_timestamp != cursor.last_consensus_timestamp:
                cursor.last_consensus_timestamp = last_committed_timestamp
                cursor.save(update_fields=["last_consensus_timestamp"])

        return caught_up, number_created

    def process_feed(
        self,
        feed_cursor: HederaTransferFeedCursor,
        following_cursors: list[HederaSafeTransferCursor],
        lookups: CachedMirrorNodeLookups,
    ) -> int:
        """
        Read the network-wide feed from ``feed_cursor`` on, committing the
        transfers and the feed cursor page by page. A Mirror Node error
        stops the feed, keeping the pages already read.

        :return: Number of InternalTx rows created
        """
        until_timestamp = self.get_feed_until_timestamp()
        if not consensus_timestamp_is_before(
            feed_cursor.last_consensus_timestamp, until_timestamp
        ):
            return 0

        safe_address_by_account_id = {
            cursor.hedera_account_id: cursor.safe_contract_id
            for cursor in following_cursors
        }
        number_internal_txs_created = 0
        pages = self.client.get_network_crypto_transfer_pages(
            feed_cursor.last_consensus_timestamp, until_timestamp
        )
        try:
            for number_created in self._process_feed_pages(
                feed_cursor, pages, safe_address_by_account_id, lookups
            ):
                number_internal_txs_created += number_created
        except Exception:
            logger.exception(
                "Error reading the Hedera native transfer feed after "
                "consensus_timestamp=%s",
                feed_cursor.last_consensus_timestamp,
            )

        logger.info(
            "Hedera native transfer feed is at consensus_timestamp=%s, %.0f seconds behind now",
            feed_cursor.last_consensus_timestamp,
            timezone.now().timestamp() - float(feed_cursor.last_consensus_timestamp),
        )
        return number_internal_txs_created

    def _process_feed_pages(
        self,
        feed_cursor: HederaTransferFeedCursor,
        pages: Iterator[list[dict]],
        safe_address_by_account_id: dict[str, str],
        lookups: CachedMirrorNodeLookups,
    ) -> Iterator[int]:
        """
        :return: Number of InternalTx rows created, once per committed page
        """
        for page_number, page in enumerate(
            itertools.islice(pages, self.feed_max_pages_per_run), start=1
        ):
            ethereum_txs: list[EthereumTx] = []
            internal_txs: list[InternalTx] = []
            last_committed_timestamp = feed_cursor.last_consensus_timestamp
            block_number_unresolved = False
            page_receivers = [
                (
                    mirror_tx,
                    sorted(
                        {
                            transfer["account"]
                            for transfer in mirror_tx.get("transfers") or []
                            if transfer["amount"] > 0
                            and transfer["account"] in safe_address_by_account_id
                        }
                    ),
                )
                for mirror_tx in page
            ]
            lookups.prefetch_block_numbers(
                [
                    mirror_tx["consensus_timestamp"]
                    for mirror_tx, safe_account_ids in page_receivers
                    if safe_account_ids
                ]
            )
            for mirror_tx, safe_account_ids in page_receivers:
                try:
                    tx_rows = [
                        self._build_transfer_rows(
                            mirror_tx,
                            safe_address_by_account_id[account_id],
                            account_id,
                            lookups,
                        )
                        for account_id in safe_account_ids
                    ]
                except UnresolvedBlockNumber as exc:
                    logger.warning(
                        "Could not resolve block number for Hedera "
                        "consensus_timestamp=%s, stopping the transfer feed until "
                        "next run",
                        exc,
                    )
                    block_number_unresolved = True
                    break
                for ethereum_tx, new_internal_txs in tx_rows:
                    if ethereum_tx:
                        ethereum_txs.append(ethereum_tx)
                        internal_txs.extend(new_internal_txs)
                last_committed_timestamp = mirror_tx["consensus_timestamp"]

            with transaction.atomic():
                number_created = self._store_transfer_rows(ethereum_txs, internal_txs)
                if last_committed_timestamp != feed_cursor.last_consensus_timestamp:
                    feed_cursor.last_consensus_timestamp = last_committed_timestamp
                    feed_cursor.save(
                        update_fields=["last_consensus_timestamp", "updated"]
                    )
            yield number_created

            if block_number_unresolved:
                break
            if (
                page_number == self.feed_max_pages_per_run
                and len(page) == MIRROR_NODE_PAGE_SIZE
            ):
                # Only a full page can be followed by another one
                logger.warning(
                    "Hedera native transfer feed read its limit of %d pages and "
                    "stopped at consensus_timestamp=%s, anything after it is "
                    "read on the next run",
                    page_number,
                    feed_cursor.last_consensus_timestamp,
                )

    def _build_transfer_rows(
        self,
        mirror_tx: dict,
        safe_address: str,
        safe_account_id: str,
        lookups: CachedMirrorNodeLookups,
    ) -> tuple[EthereumTx | None, list[InternalTx]]:
        """
        :return: The synthetic EthereumTx and its InternalTx legs for the HBAR
            ``mirror_tx`` moved into the Safe, or ``(None, [])`` if it didn't
        :raises UnresolvedBlockNumber:
        """
        if mirror_tx.get("result") != "SUCCESS":
            return None, []
        legs = extract_transfer_legs(mirror_tx, safe_account_id)
        if not legs:
            return None, []

        consensus_timestamp = mirror_tx["consensus_timestamp"]
        block_number = lookups.get_block_number(consensus_timestamp)
        timestamp = consensus_timestamp_to_datetime(consensus_timestamp)
        ethereum_tx = EthereumTx(
            tx_hash=build_synthetic_tx_hash(mirror_tx, safe_address),
            # Deliberately never linked to a real EthereumBlock
            # (see execution_date/get_block_number's fallback to
            # the InternalTx leg's own always-set values): doing
            # so would make this row's survival depend on that
            # block never being reorg-cascade-deleted, which the
            # Hedera-specific sync cursor has no way to detect
            # or recover from.
            block=None,
            status=1,
            gas=0,
            gas_used=0,
            gas_price=0,
            data=None,
            nonce=0,
            type=0,
            _from=None,
            to=safe_address,
            value=0,
            hedera_transaction_id=mirror_tx["transaction_id"],
        )

        internal_txs = []
        for index, leg in enumerate(legs):
            counterparty_evm_address = lookups.get_evm_address(
                leg["counterparty_account_id"]
            )
            if index == 0:
                ethereum_tx._from = counterparty_evm_address

            internal_txs.append(
                InternalTx(
                    ethereum_tx=ethereum_tx,
                    timestamp=timestamp,
                    block_number=block_number,
                    _from=counterparty_evm_address,
                    gas=0,
                    data=None,
                    to=safe_address,
                    value=leg["amount_tinybar"] * TINYBAR_TO_WEIBAR,
                    gas_used=0,
                    contract_address=None,
                    code=None,
                    output=None,
                    refund_address=None,
                    tx_type=InternalTxType.CALL.value,
                    call_type=EthereumTxCallType.CALL.value,
                    trace_address=str(index),
                    error=None,
                )
            )
        return ethereum_tx, internal_txs

    @staticmethod
    def _store_transfer_rows(
        ethereum_txs: list[EthereumTx], internal_txs: list[InternalTx]
    ) -> int:
        """
        Insert the rows not stored yet, so a range read twice fills in only
        what's missing. InternalTx legs already stored are skipped before
        inserting, as `bulk_create` would send `post_save(created=True)` for
        them anyway, and so their events again.

        :return: Number of InternalTx rows created
        """
        if not ethereum_txs:
            return 0
        stored_legs = {
            (HexBytes(tx_hash), trace_address)
            for tx_hash, trace_address in InternalTx.objects.filter(
                ethereum_tx_id__in=[ethereum_tx.tx_hash for ethereum_tx in ethereum_txs]
            ).values_list("ethereum_tx_id", "trace_address")
        }
        new_internal_txs = [
            internal_tx
            for internal_tx in internal_txs
            if (HexBytes(internal_tx.ethereum_tx.tx_hash), internal_tx.trace_address)
            not in stored_legs
        ]
        # No events are sent for these two, so stored rows can be sent again
        EthereumTx.objects.bulk_create_from_generator(
            iter(ethereum_txs), ignore_conflicts=True
        )
        InternalTx.objects.bulk_create_from_generator(
            iter(new_internal_txs), ignore_conflicts=True
        )
        SafeRelevantTransaction.objects.bulk_create(
            [
                SafeRelevantTransaction(
                    ethereum_tx=internal_tx.ethereum_tx,
                    safe=internal_tx.to,
                    timestamp=internal_tx.timestamp,
                )
                for internal_tx in internal_txs
            ],
            ignore_conflicts=True,
        )
        return len(new_internal_txs)
