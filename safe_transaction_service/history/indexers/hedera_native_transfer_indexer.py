import datetime
import logging

from django.db import transaction

from safe_eth.eth.utils import fast_keccak_text
from safe_eth.util.util import to_0x_hex_str

from ..clients.hedera_mirror_node_client import HederaMirrorNodeClient
from ..models import (
    EthereumBlock,
    EthereumTx,
    EthereumTxCallType,
    InternalTx,
    InternalTxType,
    SafeContract,
    SafeRelevantTransaction,
)

logger = logging.getLogger(__name__)

TINYBAR_TO_WEIBAR = 10**10

# Hedera system accounts that collect network/node fees and staking rewards.
# These never represent a real counterparty and must be excluded when
# figuring out who a Safe actually transacted with.
FEE_COLLECTOR_ACCOUNT_IDS = frozenset(
    {f"0.0.{n}" for n in range(3, 101)} | {"0.0.800", "0.0.801"}
)


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


def extract_transfer_legs(mirror_tx: dict, safe_account_id: str) -> list[dict]:
    """
    Turn a Mirror Node ``CRYPTOTRANSFER`` transaction into the list of HBAR
    movements between ``safe_account_id`` and each real counterparty.

    Anchored on the Safe's own net HBAR change (``safe_net``) rather than
    trusting every remaining non-fee leg wholesale — a Mirror Node
    ``transfers`` list can contain legs unrelated to the Safe's own side of
    the transfer (e.g. the Safe is only the fee payer for a transfer between
    two other parties, or one sender pays multiple receivers and the Safe is
    only one of them). Only legs moving HBAR in the opposite direction of
    ``safe_net`` are real counterparties to the Safe; same-direction legs are
    other parties on the *same* side and are not attributed to the Safe.

    Excludes Hedera's fee-collector/staking-reward accounts. When exactly one
    counterparty remains, the Safe's own net amount is used directly. For a
    true multi-party split, the Safe's net amount is apportioned across
    counterparties in proportion to their own reported (fee-adjusted) leg
    magnitudes, so returned amounts always sum to exactly ``safe_net`` — the
    Safe's credited/debited total can never be inflated by a leg that isn't
    really part of its side of the transfer. The last leg absorbs any
    integer-division remainder so the sum is always exact.

    :param mirror_tx: One entry from Mirror Node's
        ``/api/v1/transactions?transactiontype=CRYPTOTRANSFER`` response.
    :param safe_account_id: The Safe's own Hedera account id, e.g. ``"0.0.X"``.
    :return: ``[{"counterparty_account_id": str, "amount_tinybar": int}, ...]``,
        where ``amount_tinybar`` is signed from the Safe's perspective
        (positive = Safe received).
    """
    transfers = mirror_tx.get("transfers") or []
    safe_leg = next((t for t in transfers if t["account"] == safe_account_id), None)
    if not safe_leg:
        return []

    payer_account_id = mirror_tx["transaction_id"].split("-")[0]
    charged_tx_fee = mirror_tx.get("charged_tx_fee") or 0

    # The Safe's own net HBAR change due to this transaction, fee-adjusted
    # if the Safe itself paid the fee (its raw ledger leg would otherwise
    # include the fee it paid, not just the amount actually transferred).
    safe_net = safe_leg["amount"]
    if payer_account_id == safe_account_id:
        safe_net += charged_tx_fee

    if safe_net == 0:
        # The Safe only paid the transaction fee (or the fee-adjustment
        # exactly cancels its raw leg) — no real transfer touched the Safe.
        return []

    counterparty_legs = []
    for t in transfers:
        if t["account"] == safe_account_id or t["account"] in FEE_COLLECTOR_ACCOUNT_IDS:
            continue
        amount = t["amount"]
        if t["account"] == payer_account_id and amount < 0:
            # This account absorbed the transaction fee as its payer; remove
            # it so the leg reflects only the HBAR actually transferred.
            amount += charged_tx_fee
        if amount == 0:
            continue
        if (amount > 0) == (safe_net > 0):
            # Same direction as the Safe's own change — another party on the
            # Safe's own side of the transfer, not a counterparty to it.
            continue
        counterparty_legs.append({"account": t["account"], "amount": amount})

    if not counterparty_legs:
        return []

    if len(counterparty_legs) == 1:
        return [
            {
                "counterparty_account_id": counterparty_legs[0]["account"],
                "amount_tinybar": safe_net,
            }
        ]

    logger.info(
        "Multi-party Hedera crypto transfer %s involving safe=%s: "
        "apportioning the Safe's net amount across %d counterparties",
        mirror_tx["transaction_id"],
        safe_account_id,
        len(counterparty_legs),
    )

    total_counterparty_magnitude = sum(abs(leg["amount"]) for leg in counterparty_legs)
    safe_sign = 1 if safe_net > 0 else -1
    result = []
    allocated = 0
    for index, leg in enumerate(counterparty_legs):
        if index == len(counterparty_legs) - 1:
            share = abs(safe_net) - allocated
        else:
            share = abs(safe_net) * abs(leg["amount"]) // total_counterparty_magnitude
            allocated += share
        result.append(
            {
                "counterparty_account_id": leg["account"],
                "amount_tinybar": safe_sign * share,
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
            ),
            max_txs_per_safe_per_run=settings.HEDERA_NATIVE_TRANSFER_MAX_TXS_PER_SAFE_PER_RUN,
        )

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class HederaNativeTransferIndexer:
    def __init__(
        self, client: HederaMirrorNodeClient, max_txs_per_safe_per_run: int = 500
    ):
        self.client = client
        self.max_txs_per_safe_per_run = max_txs_per_safe_per_run

    def process_all_safes(self) -> tuple[int, int]:
        """
        :return: (number of Safes processed, number of InternalTx rows created)
        """
        number_safes_processed = 0
        number_internal_txs_created = 0
        for safe_contract in SafeContract.objects.filter(banned=False).iterator():
            try:
                number_internal_txs_created += self.process_safe(safe_contract)
            except Exception:
                logger.exception(
                    "Error indexing Hedera native transfers for safe=%s",
                    safe_contract.address,
                )
                continue
            number_safes_processed += 1
        return number_safes_processed, number_internal_txs_created

    def process_safe(self, safe_contract: SafeContract) -> int:
        from ..models import HederaSafeTransferCursor

        cursor, _ = HederaSafeTransferCursor.objects.get_or_create(
            safe_contract=safe_contract
        )
        if not cursor.hedera_account_id:
            account_id = self.client.resolve_account_id(safe_contract.address)
            if not account_id:
                return 0
            cursor.hedera_account_id = account_id
            # Seed the cursor at the Safe's creation time so the first sync
            # only looks forward from when the Safe started existing,
            # instead of scanning that Hedera account's entire history.
            cursor.last_consensus_timestamp = datetime_to_consensus_timestamp(
                safe_contract.created
            )
            cursor.save(update_fields=["hedera_account_id", "last_consensus_timestamp"])

        ethereum_txs: list[EthereumTx] = []
        internal_txs: list[InternalTx] = []
        # (EthereumTx instance, resolved Hedera block number) pairs, so that
        # after the loop we can look up which of those block numbers already
        # have a real EthereumBlock row (created by the EVM indexer) and
        # link the synthetic EthereumTx to it instead of leaving `block`
        # unset.
        ethereum_tx_block_numbers: list[tuple[EthereumTx, int]] = []
        block_number_cache: dict[str, int | None] = {}
        counterparty_address_cache: dict[str, str] = {}
        last_committed_timestamp = cursor.last_consensus_timestamp

        for processed_count, mirror_tx in enumerate(
            self.client.get_crypto_transfers(
                cursor.hedera_account_id,
                after_timestamp=cursor.last_consensus_timestamp,
            )
        ):
            if processed_count >= self.max_txs_per_safe_per_run:
                break
            consensus_timestamp = mirror_tx["consensus_timestamp"]

            if mirror_tx.get("result") == "SUCCESS":
                legs = extract_transfer_legs(mirror_tx, cursor.hedera_account_id)
                if legs:
                    if consensus_timestamp not in block_number_cache:
                        block_number_cache[consensus_timestamp] = (
                            self.client.resolve_block_number(consensus_timestamp)
                        )
                    block_number = block_number_cache[consensus_timestamp]
                    if block_number is None:
                        logger.warning(
                            "Could not resolve block number for Hedera "
                            "consensus_timestamp=%s, stopping sync for "
                            "safe=%s until next run",
                            consensus_timestamp,
                            safe_contract.address,
                        )
                        break

                    timestamp = consensus_timestamp_to_datetime(consensus_timestamp)
                    tx_hash = to_0x_hex_str(
                        fast_keccak_text(f"hedera-native:{mirror_tx['transaction_id']}")
                    )
                    ethereum_tx = EthereumTx(
                        tx_hash=tx_hash,
                        block=None,
                        status=1,
                        gas=0,
                        gas_used=0,
                        gas_price=0,
                        data=None,
                        nonce=0,
                        type=0,
                        # `_from`/`to` are set below from the first leg,
                        # mirroring how a real single-hop EVM ether transfer
                        # reports the actual sender/receiver at the top
                        # level (EthereumTxWithTransfersResponseSerializer
                        # surfaces these directly as "from"/"to" in the
                        # all-transactions API, separately from the nested
                        # `transfers[]` entries built from InternalTx).
                        _from=None,
                        to=None,
                        value=0,
                    )
                    ethereum_txs.append(ethereum_tx)
                    ethereum_tx_block_numbers.append((ethereum_tx, block_number))

                    for index, leg in enumerate(legs):
                        counterparty_account_id = leg["counterparty_account_id"]
                        if counterparty_account_id not in counterparty_address_cache:
                            counterparty_address_cache[counterparty_account_id] = (
                                self.client.resolve_evm_address(counterparty_account_id)
                            )
                        counterparty_evm_address = counterparty_address_cache[
                            counterparty_account_id
                        ]

                        amount_tinybar = leg["amount_tinybar"]
                        value_weibar = abs(amount_tinybar) * TINYBAR_TO_WEIBAR
                        if amount_tinybar > 0:
                            from_address = counterparty_evm_address
                            to_address = safe_contract.address
                        else:
                            from_address = safe_contract.address
                            to_address = counterparty_evm_address

                        if index == 0:
                            # For a true multi-party split there's no single
                            # correct top-level from/to; the first leg is the
                            # most representative choice and matches the
                            # common single-leg case exactly.
                            ethereum_tx._from = from_address
                            ethereum_tx.to = to_address

                        internal_txs.append(
                            InternalTx(
                                ethereum_tx=ethereum_tx,
                                timestamp=timestamp,
                                block_number=block_number,
                                _from=from_address,
                                gas=0,
                                data=None,
                                to=to_address,
                                value=value_weibar,
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

            last_committed_timestamp = consensus_timestamp

        if not ethereum_txs:
            if last_committed_timestamp != cursor.last_consensus_timestamp:
                cursor.last_consensus_timestamp = last_committed_timestamp
                cursor.save(update_fields=["last_consensus_timestamp"])
            return 0

        # If the Mirror-Node-resolved block number happens to already have a
        # real EthereumBlock row (created by the existing EVM indexer), link
        # the synthetic EthereumTx to it so the tx surfaces a real execution
        # date/block number instead of null. One batched query for all the
        # distinct block numbers resolved in this run, rather than one query
        # per tx.
        distinct_block_numbers = {
            block_number for _, block_number in ethereum_tx_block_numbers
        }
        existing_block_numbers = set(
            EthereumBlock.objects.filter(number__in=distinct_block_numbers).values_list(
                "number", flat=True
            )
        )
        for ethereum_tx, block_number in ethereum_tx_block_numbers:
            if block_number in existing_block_numbers:
                ethereum_tx.block_id = block_number

        # Every InternalTx built above already has the Safe as either `to`
        # or `_from` (extract_transfer_legs only returns legs the Safe is a
        # party to), so every one of them needs a SafeRelevantTransaction —
        # unlike the EVM indexer, an outgoing native transfer has no
        # MultisigTransaction to otherwise surface it in the Safe's feed.
        safe_relevant_txs = [
            SafeRelevantTransaction(
                ethereum_tx=internal_tx.ethereum_tx,
                safe=safe_contract.address,
                timestamp=internal_tx.timestamp,
            )
            for internal_tx in internal_txs
        ]

        with transaction.atomic():
            EthereumTx.objects.bulk_create_from_generator(
                iter(ethereum_txs), ignore_conflicts=True
            )
            InternalTx.objects.bulk_create_from_generator(
                iter(internal_txs), ignore_conflicts=True
            )
            SafeRelevantTransaction.objects.bulk_create(
                safe_relevant_txs, ignore_conflicts=True
            )
            cursor.last_consensus_timestamp = last_committed_timestamp
            cursor.save(update_fields=["last_consensus_timestamp"])

        return len(internal_txs)
