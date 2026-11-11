import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from ...utils.redis import get_redis
from ..clients.exceptions import HederaMirrorNodeClientException
from ..clients.hedera_mirror_node_client import hedera_account_id_to_long_zero_address
from ..indexers.hedera_native_transfer_indexer import (
    INDEXING_LOCK_ID,
    CachedMirrorNodeLookups,
    HederaNativeTransferIndexer,
    UnresolvedBlockNumber,
    build_synthetic_tx_hash,
    consensus_timestamp_to_datetime,
    datetime_to_consensus_timestamp,
    extract_transfer_legs,
)
from ..models import (
    EthereumTx,
    HederaSafeTransferCursor,
    HederaTransferFeedCursor,
    InternalTx,
    SafeContract,
    SafeRelevantTransaction,
)
from ..services.transaction_service import TransactionServiceProvider
from .factories import (
    EthereumBlockFactory,
    EthereumTxFactory,
    InternalTxFactory,
    SafeContractFactory,
)

SAFE_ACCOUNT_ID = "0.0.10127045"


class TestExtractTransferLegsSimple(SimpleTestCase):
    def test_incoming_transfer_uses_safes_own_leg_not_payers_fee_inflated_leg(self):
        # Payer (0.0.1111111) sends 5 HBAR (500_000_000 tinybar) to the Safe.
        # Their own ledger leg is debited for transfer + fee; the Safe's own
        # leg is exactly the amount it received.
        mirror_tx = {
            "transaction_id": "0.0.1111111-1700000000-000000001",
            "charged_tx_fee": 1_440_097,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(500_000_000 + 1_440_097)},
                {"account": "0.0.7", "amount": 500_000},  # node fee
                {"account": "0.0.98", "amount": 940_097},  # network fee
                {"account": SAFE_ACCOUNT_ID, "amount": 500_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [{"counterparty_account_id": "0.0.1111111", "amount_tinybar": 500_000_000}],
        )

    def test_safe_leg_not_positive_returns_empty(self):
        # A Safe's Hedera account has no externally-held key (it's created
        # with a self-referencing ContractID key), so only the Safe
        # contract's own EVM execution can move value out of it — a Safe
        # can never be the payer/sender in a native CryptoTransfer. This
        # guard is defensive: it should never see a non-positive Safe leg
        # in practice, but must not misbehave if it ever did.
        mirror_tx = {
            "transaction_id": f"{SAFE_ACCOUNT_ID}-1700000000-000000002",
            "charged_tx_fee": 1_200_000,
            "transfers": [
                {"account": SAFE_ACCOUNT_ID, "amount": -(300_000_000 + 1_200_000)},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 800_000},
                {"account": "0.0.2222222", "amount": 300_000_000},
            ],
        }
        self.assertEqual(extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID), [])

    def test_safe_not_a_party_returns_empty(self):
        mirror_tx = {
            "transaction_id": "0.0.1111111-1700000000-000000003",
            "charged_tx_fee": 100_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -100_000},
                {"account": "0.0.7", "amount": 100_000},
            ],
        }
        self.assertEqual(extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID), [])

    def test_multi_party_split_uses_each_counterpartys_own_amount(self):
        # Two senders (0.0.1111111 is the payer, 0.0.3333333 is not) both
        # send HBAR to the Safe in one batched transfer.
        mirror_tx = {
            "transaction_id": "0.0.1111111-1700000000-000000004",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(100_000_000 + 1_000_000)},
                {"account": "0.0.3333333", "amount": -200_000_000},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 600_000},
                {"account": SAFE_ACCOUNT_ID, "amount": 300_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertCountEqual(
            legs,
            [
                {
                    "counterparty_account_id": "0.0.1111111",
                    "amount_tinybar": 100_000_000,
                },
                {
                    "counterparty_account_id": "0.0.3333333",
                    "amount_tinybar": 200_000_000,
                },
            ],
        )

    def test_one_sender_multiple_receivers_including_safe_credits_only_safes_share(
        self,
    ):
        # 0.0.1111111 (the payer) sends HBAR split between the Safe and another
        # receiver in one transaction. The Safe must be credited only its own
        # share, not the sender's full amount, and must NOT get a phantom leg
        # for the other receiver's share.
        mirror_tx = {
            "transaction_id": "0.0.1111111-1700000000-000000006",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(300_000_000 + 1_000_000)},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 600_000},
                {"account": SAFE_ACCOUNT_ID, "amount": 200_000_000},
                {"account": "0.0.3333333", "amount": 100_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [{"counterparty_account_id": "0.0.1111111", "amount_tinybar": 200_000_000}],
        )

    def test_third_party_fee_sponsor_leg_does_not_produce_zero_amount_leg(self):
        # A third party (not the Safe, not the real sender) sponsors the
        # fee and sends nothing itself. Their leg, after fee-adjustment,
        # nets to zero and must not appear.
        mirror_tx = {
            "transaction_id": "0.0.9999999-1700000000-000000007",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.9999999", "amount": -1_000_000},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 600_000},
                {"account": "0.0.1111111", "amount": -500_000_000},
                {"account": SAFE_ACCOUNT_ID, "amount": 500_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [{"counterparty_account_id": "0.0.1111111", "amount_tinybar": 500_000_000}],
        )

    def test_unrecognized_fee_collector_account_is_naturally_excluded(self):
        # Real-world regression: 0.0.802 (a real Hedera fee-collector account
        # observed on mainnet) isn't a node/staking-reward account an
        # earlier version of this function had to explicitly enumerate to
        # exclude. Since a Safe can only ever receive (never send) in a
        # native transfer, every fee-collector leg is positive — same sign
        # as the Safe's own positive receipt — so it's excluded by the sign
        # check alone, with no need to recognize the specific account.
        mirror_tx = {
            "transaction_id": "0.0.1111111-1700000000-000000008",
            "charged_tx_fee": 150_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(500_000_000 + 150_000)},
                {"account": "0.0.7", "amount": 50_000},
                {"account": "0.0.98", "amount": 50_000},
                {"account": "0.0.802", "amount": 50_000},
                {"account": SAFE_ACCOUNT_ID, "amount": 500_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [{"counterparty_account_id": "0.0.1111111", "amount_tinybar": 500_000_000}],
        )


class TestBuildSyntheticTxHash(SimpleTestCase):
    SAFE_A = "0x1111111111111111111111111111111111111111"
    SAFE_B = "0x2222222222222222222222222222222222222222"
    MIRROR_TX = {"transaction_id": "0.0.4000000-1791300071-257082119", "nonce": 0}

    def test_is_stable_across_runs(self):
        self.assertEqual(
            build_synthetic_tx_hash(self.MIRROR_TX, self.SAFE_A),
            build_synthetic_tx_hash(dict(self.MIRROR_TX), self.SAFE_A),
        )

    def test_differs_per_safe_for_the_same_hedera_transaction(self):
        self.assertNotEqual(
            build_synthetic_tx_hash(self.MIRROR_TX, self.SAFE_A),
            build_synthetic_tx_hash(self.MIRROR_TX, self.SAFE_B),
        )

    def test_differs_per_child_record_nonce(self):
        self.assertNotEqual(
            build_synthetic_tx_hash({**self.MIRROR_TX, "nonce": 1}, self.SAFE_A),
            build_synthetic_tx_hash({**self.MIRROR_TX, "nonce": 2}, self.SAFE_A),
        )

    def test_missing_nonce_is_treated_as_top_level_record(self):
        self.assertEqual(
            build_synthetic_tx_hash(
                {"transaction_id": self.MIRROR_TX["transaction_id"]}, self.SAFE_A
            ),
            build_synthetic_tx_hash(self.MIRROR_TX, self.SAFE_A),
        )


class TestConsensusTimestampToDatetime(SimpleTestCase):
    def test_converts_to_aware_utc_datetime(self):
        result = consensus_timestamp_to_datetime("1787164650.039440869")
        self.assertEqual(result.tzinfo, datetime.UTC)
        self.assertEqual(result.year, 2026)

    def test_round_trips_through_datetime_to_consensus_timestamp(self):
        dt = datetime.datetime(2026, 1, 1, 12, 0, 0, 500000, tzinfo=datetime.UTC)
        timestamp_str = datetime_to_consensus_timestamp(dt)
        self.assertEqual(timestamp_str, "1767268800.500000000")
        self.assertEqual(consensus_timestamp_to_datetime(timestamp_str), dt)


INCOMING_TX = {
    "transaction_id": "0.0.1111111-1700000000-000000001",
    "consensus_timestamp": "1700000000.000000001",
    "result": "SUCCESS",
    "charged_tx_fee": 1_440_097,
    "transfers": [
        {"account": "0.0.1111111", "amount": -(500_000_000 + 1_440_097)},
        {"account": "0.0.7", "amount": 500_000},
        {"account": "0.0.98", "amount": 940_097},
        {"account": "0.0.10127045", "amount": 500_000_000},
    ],
}

SECOND_INCOMING_TX = {
    "transaction_id": "0.0.2222222-1700000100-000000002",
    "consensus_timestamp": "1700000100.000000002",
    "result": "SUCCESS",
    "charged_tx_fee": 1_200_000,
    "transfers": [
        {"account": "0.0.2222222", "amount": -(300_000_000 + 1_200_000)},
        {"account": "0.0.7", "amount": 400_000},
        {"account": "0.0.98", "amount": 800_000},
        {"account": "0.0.10127045", "amount": 300_000_000},
    ],
}


class TestHederaNativeTransferIndexerProcessSafe(TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.resolve_account_id.return_value = "0.0.10127045"
        self.client.resolve_block_number.return_value = 12345
        self.client.resolve_evm_address.side_effect = lambda account_id: {
            "0.0.1111111": "0xAAAA00000000000000000000000000000000aaaa",
            "0.0.2222222": "0xbbBB00000000000000000000000000000000bBbb",
        }[account_id]
        self.indexer = HederaNativeTransferIndexer(client=self.client)
        self.safe_contract = SafeContractFactory(
            address="0x1234567890123456789012345678901234567890"
        )

    def test_resolves_and_persists_account_id_on_first_run(self):
        self.client.get_crypto_transfers.return_value = iter([])
        self.indexer.process_safe(self.safe_contract)
        self.safe_contract.refresh_from_db()
        self.assertEqual(
            self.safe_contract.hedera_transfer_cursor.hedera_account_id, "0.0.10127045"
        )

    def test_seeds_cursor_at_safe_creation_time_on_first_run_not_full_history(self):
        self.client.get_crypto_transfers.return_value = iter([])
        self.indexer.process_safe(self.safe_contract)

        expected_timestamp = datetime_to_consensus_timestamp(
            self.safe_contract.ethereum_tx.execution_date
        )
        self.client.get_crypto_transfers.assert_called_once_with(
            "0.0.10127045", after_timestamp=expected_timestamp, until_timestamp=None
        )
        self.safe_contract.refresh_from_db()
        self.assertEqual(
            self.safe_contract.hedera_transfer_cursor.last_consensus_timestamp,
            expected_timestamp,
        )

    def test_catches_deposit_made_before_our_own_indexer_noticed_the_safe(self):
        # `SafeContract.created` (auto_now_add) is when *our own EVM
        # indexer* happened to notice the Safe — it lags the Safe's real
        # on-chain creation by however long block confirmation takes. A
        # HashPack/SDK deposit made in that gap must still be caught: the
        # cursor seeds from the real creation timestamp
        # (ethereum_tx.execution_date), not from `created`.
        safe_contract = SafeContractFactory(
            address="0xABCDEF0123456789012345678901234567890ABC",
            ethereum_tx=EthereumTxFactory(
                block=EthereumBlockFactory(
                    timestamp=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
                )
            ),
        )
        self.assertGreater(
            safe_contract.created, safe_contract.ethereum_tx.execution_date
        )

        deposit_before_db_insert = {
            "transaction_id": "0.0.1111111-1767225700-000000001",
            "consensus_timestamp": "1767225700.000000001",
            "result": "SUCCESS",
            "charged_tx_fee": 1_440_097,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(500_000_000 + 1_440_097)},
                {"account": "0.0.7", "amount": 500_000},
                {"account": "0.0.98", "amount": 940_097},
                {"account": "0.0.10127045", "amount": 500_000_000},
            ],
        }
        self.client.get_crypto_transfers.return_value = iter([deposit_before_db_insert])
        created_count = self.indexer.process_safe(safe_contract)

        self.assertEqual(created_count, 1)
        self.assertTrue(InternalTx.objects.filter(to=safe_contract.address).exists())

    def test_creates_internal_tx_and_ethereum_tx_for_incoming_transfer(self):
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        created_count = self.indexer.process_safe(self.safe_contract)

        self.assertEqual(created_count, 1)
        internal_tx = InternalTx.objects.get()
        self.assertEqual(internal_tx.to, self.safe_contract.address)
        self.assertEqual(
            internal_tx._from, "0xAAAA00000000000000000000000000000000aaaa"
        )
        self.assertEqual(internal_tx.value, 500_000_000 * 10**10)
        self.assertEqual(internal_tx.block_number, 12345)
        self.assertTrue(internal_tx.is_ether_transfer)
        # +1 for the Safe's own creation EthereumTx that SafeContractFactory
        # always creates, on top of the 1 hedera-native EthereumTx we built.
        self.assertEqual(EthereumTx.objects.count(), 2)
        # The wrapping EthereumTx's own from/to must reflect the real
        # transfer direction (matching what the all-transactions API
        # surfaces at the top level), not always the Safe's own address.
        self.assertEqual(internal_tx.ethereum_tx._from, internal_tx._from)
        self.assertEqual(internal_tx.ethereum_tx.to, internal_tx.to)

    def test_never_links_ethereum_tx_to_a_real_ethereum_block(self):
        # Deliberately never linked, even when a real EthereumBlock row for
        # the resolved block number (12345, per setUp) already exists —
        # linking would make this row's survival depend on that block never
        # being reorg-cascade-deleted later, which the Hedera-specific sync
        # cursor has no way to detect or recover from. execution_date/
        # get_block_number fall back to the InternalTx leg's own
        # always-set timestamp/block_number instead, so nothing downstream
        # needs the link.
        EthereumBlockFactory(number=12345)
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        self.indexer.process_safe(self.safe_contract)

        internal_tx = InternalTx.objects.get()
        self.assertIsNone(internal_tx.ethereum_tx.block_id)

    def test_creates_safe_relevant_transaction_for_each_incoming_transfer(self):
        self.client.get_crypto_transfers.return_value = iter(
            [INCOMING_TX, SECOND_INCOMING_TX]
        )
        self.indexer.process_safe(self.safe_contract)

        relevant = SafeRelevantTransaction.objects.filter(
            safe=self.safe_contract.address
        )
        self.assertEqual(relevant.count(), 2)

    def test_multi_leg_transfer_uses_first_legs_direction_for_wrapping_ethereum_tx(
        self,
    ):
        # A single Mirror Node transaction split across multiple legs has no
        # single correct top-level from/to; the wrapping EthereumTx should
        # use the first leg's direction, matching the InternalTx it produced.
        multi_leg_tx = {
            "transaction_id": "0.0.1111111-1700000200-000000003",
            "consensus_timestamp": "1700000200.000000003",
            "result": "SUCCESS",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(100_000_000 + 1_000_000)},
                {"account": "0.0.3333333", "amount": -200_000_000},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 600_000},
                {"account": "0.0.10127045", "amount": 300_000_000},
            ],
        }
        self.client.resolve_evm_address.side_effect = lambda account_id: {
            "0.0.1111111": "0xAAAA00000000000000000000000000000000aaaa",
            "0.0.3333333": "0xcCcc00000000000000000000000000000000cCcC",
        }[account_id]
        self.client.get_crypto_transfers.return_value = iter([multi_leg_tx])

        created_count = self.indexer.process_safe(self.safe_contract)

        self.assertEqual(created_count, 2)
        first_internal_tx = InternalTx.objects.order_by("trace_address").first()
        self.assertEqual(first_internal_tx.trace_address, "0")
        ethereum_tx = first_internal_tx.ethereum_tx
        self.assertEqual(ethereum_tx._from, first_internal_tx._from)
        self.assertEqual(ethereum_tx.to, first_internal_tx.to)

    def test_one_hedera_transaction_paying_two_safes_indexes_each_safe(self):
        # A single CRYPTOTRANSFER can credit several accounts at once (e.g. a
        # SentX NFT purchase pays seller, royalties and the marketplace).
        other_safe_contract = SafeContractFactory(
            address="0x2222222222222222222222222222222222222222"
        )
        other_safe_account_id = "0.0.10127046"
        shared_tx = {
            "transaction_id": "0.0.1111111-1700000300-000000004",
            "consensus_timestamp": "1700000300.000000004",
            "result": "SUCCESS",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.1111111", "amount": -(700_000_000 + 1_000_000)},
                {"account": "0.0.7", "amount": 100_000},
                {"account": "0.0.98", "amount": 900_000},
                {"account": "0.0.10127045", "amount": 600_000_000},
                {"account": other_safe_account_id, "amount": 100_000_000},
            ],
        }

        self.client.get_crypto_transfers.return_value = iter([shared_tx])
        self.assertEqual(self.indexer.process_safe(self.safe_contract), 1)
        self.client.resolve_account_id.return_value = other_safe_account_id
        self.client.get_crypto_transfers.return_value = iter([shared_tx])
        self.assertEqual(self.indexer.process_safe(other_safe_contract), 1)

        for safe_contract, value_tinybar in (
            (self.safe_contract, 600_000_000),
            (other_safe_contract, 100_000_000),
        ):
            with self.subTest(safe=safe_contract.address):
                internal_tx = InternalTx.objects.get(to=safe_contract.address)
                self.assertEqual(internal_tx.value, value_tinybar * 10**10)
                self.assertEqual(internal_tx.ethereum_tx.to, safe_contract.address)
                self.assertEqual(
                    internal_tx.ethereum_tx.hedera_transaction_id,
                    shared_tx["transaction_id"],
                )
                self.assertTrue(
                    SafeRelevantTransaction.objects.filter(
                        ethereum_tx=internal_tx.ethereum_tx,
                        safe=safe_contract.address,
                    ).exists()
                )
        self.assertNotEqual(
            InternalTx.objects.get(to=self.safe_contract.address).ethereum_tx_id,
            InternalTx.objects.get(to=other_safe_contract.address).ethereum_tx_id,
        )

    def test_child_records_sharing_a_transaction_id_are_indexed_separately(self):
        # Child records created by a contract call reuse the parent's
        # transaction_id and differ only by nonce.
        child_records = [
            {
                **INCOMING_TX,
                "nonce": nonce,
                "consensus_timestamp": f"1700000000.00000000{nonce + 1}",
            }
            for nonce in (1, 2)
        ]
        self.client.get_crypto_transfers.return_value = iter(child_records)

        created_count = self.indexer.process_safe(self.safe_contract)

        self.assertEqual(created_count, 2)
        internal_txs = InternalTx.objects.filter(to=self.safe_contract.address)
        self.assertEqual(internal_txs.count(), 2)
        self.assertEqual(
            len({internal_tx.ethereum_tx_id for internal_tx in internal_txs}), 2
        )

    def test_advances_cursor_and_is_idempotent_on_rerun(self):
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        self.indexer.process_safe(self.safe_contract)
        self.safe_contract.refresh_from_db()
        self.assertEqual(
            self.safe_contract.hedera_transfer_cursor.last_consensus_timestamp,
            "1700000000.000000001",
        )

        # Re-running with the same (already-seen) transaction must not
        # duplicate rows.
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        self.indexer.process_safe(self.safe_contract)
        self.assertEqual(InternalTx.objects.count(), 1)
        # +1 for the Safe's own creation EthereumTx that SafeContractFactory
        # always creates, on top of the 1 hedera-native EthereumTx we built.
        self.assertEqual(EthereumTx.objects.count(), 2)

    def test_stops_batch_without_advancing_cursor_when_block_number_unresolvable(self):
        self.client.resolve_block_number.return_value = None
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        created_count = self.indexer.process_safe(self.safe_contract)

        self.assertEqual(created_count, 0)
        self.assertEqual(InternalTx.objects.count(), 0)
        self.safe_contract.refresh_from_db()
        # The cursor stays at its first-run seed (the Safe's creation time) —
        # it must NOT be None (get_or_create already ran and seeded it
        # before this transaction was even fetched) and must NOT advance to
        # INCOMING_TX's consensus_timestamp, since that transaction's block
        # number could not be resolved and was never actually committed.
        self.assertEqual(
            self.safe_contract.hedera_transfer_cursor.last_consensus_timestamp,
            datetime_to_consensus_timestamp(
                self.safe_contract.ethereum_tx.execution_date
            ),
        )

    def test_account_not_found_returns_zero_without_creating_cursor_account_id(self):
        self.client.resolve_account_id.return_value = None
        created_count = self.indexer.process_safe(self.safe_contract)
        self.assertEqual(created_count, 0)
        self.client.get_crypto_transfers.assert_not_called()


FEED_CURSOR = "1767225600.000000000"  # 2026-01-01 00:00:00 UTC
FEED_UNTIL = "1767226200.000000000"  # 10 minutes later
SAFE_CREATION_DATE = datetime.datetime(2025, 12, 1, tzinfo=datetime.UTC)


def incoming_tx(
    consensus_timestamp: str, *receivers: tuple[str, int], sender: str = "0.0.1111111"
) -> dict:
    """
    :param consensus_timestamp:
    :param receivers: ``(account_id, amount_tinybar)`` credited by the transfer
    :param sender: Payer and only sender of the transfer
    :return: Mirror Node ``CRYPTOTRANSFER`` transaction
    """
    fee = 1_000_000
    total = sum(amount for _, amount in receivers)
    return {
        "transaction_id": f"{sender}-{consensus_timestamp.replace('.', '-')}",
        "consensus_timestamp": consensus_timestamp,
        "result": "SUCCESS",
        "charged_tx_fee": fee,
        "transfers": [
            {"account": sender, "amount": -(total + fee)},
            {"account": "0.0.98", "amount": fee},
            *({"account": account, "amount": amount} for account, amount in receivers),
        ],
    }


class FakeMirrorNode:
    """
    Serves ``transactions`` honouring the account and timestamp filters, the
    way Mirror Node does, so the indexer can be run several times in a row.
    """

    def __init__(self, page_size: int = 100):
        self.page_size = page_size
        self.transactions: list[dict] = []
        self.account_ids: dict[str, str] = {}
        self.unresolvable_timestamps: set[str] = set()
        self.feed_error_after_pages: int | None = None

    def resolve_account_id(self, address: str) -> str | None:
        return self.account_ids.get(address)

    @staticmethod
    def resolve_evm_address(account_id: str) -> str:
        return hedera_account_id_to_long_zero_address(account_id)

    def resolve_block_number(self, consensus_timestamp: str) -> int | None:
        if consensus_timestamp in self.unresolvable_timestamps:
            return None
        return int(Decimal(consensus_timestamp))

    def get_blocks(self, from_consensus_timestamp: str) -> list[dict]:
        # One block per second. Blocks holding an unresolvable timestamp are
        # not ingested yet, and neither is anything after them
        first_block = int(Decimal(from_consensus_timestamp))
        not_ingested = min(
            (int(Decimal(timestamp)) for timestamp in self.unresolvable_timestamps),
            default=None,
        )
        return [
            {
                "number": number,
                "timestamp": {
                    "from": f"{number}.000000000",
                    "to": f"{number}.999999999",
                },
            }
            for number in range(first_block, first_block + 100)
            if not_ingested is None or number < not_ingested
        ]

    def _in_range(
        self, mirror_tx: dict, after_timestamp: str | None, until_timestamp: str | None
    ) -> bool:
        timestamp = Decimal(mirror_tx["consensus_timestamp"])
        return (after_timestamp is None or timestamp > Decimal(after_timestamp)) and (
            until_timestamp is None or timestamp <= Decimal(until_timestamp)
        )

    def get_crypto_transfers(
        self,
        account_id: str,
        after_timestamp: str | None = None,
        until_timestamp: str | None = None,
    ):
        return iter(
            [
                mirror_tx
                for mirror_tx in sorted(
                    self.transactions, key=lambda tx: Decimal(tx["consensus_timestamp"])
                )
                if self._in_range(mirror_tx, after_timestamp, until_timestamp)
                and any(t["account"] == account_id for t in mirror_tx["transfers"])
            ]
        )

    def get_network_crypto_transfer_pages(
        self, after_timestamp: str, until_timestamp: str
    ):
        matching = [
            mirror_tx
            for mirror_tx in sorted(
                self.transactions, key=lambda tx: Decimal(tx["consensus_timestamp"])
            )
            if self._in_range(mirror_tx, after_timestamp, until_timestamp)
        ]
        for page_number, start in enumerate(range(0, len(matching), self.page_size)):
            if (
                self.feed_error_after_pages is not None
                and page_number >= self.feed_error_after_pages
            ):
                raise HederaMirrorNodeClientException("Mirror Node returned 503")
            yield matching[start : start + self.page_size]


class TestCachedMirrorNodeLookups(SimpleTestCase):
    def test_prefetch_matches_resolve_block_number(self):
        # A timestamp between two blocks belongs to the next one, like
        # `resolve_block_number`'s `timestamp=gte:` lookup
        client = MagicMock()
        client.get_blocks.side_effect = [
            [
                {"number": 10, "timestamp": {"from": "100.0", "to": "101.5"}},
                {"number": 11, "timestamp": {"from": "102.0", "to": "103.5"}},
            ],
            [{"number": 12, "timestamp": {"from": "104.0", "to": "105.5"}}],
        ]
        lookups = CachedMirrorNodeLookups(client)

        lookups.prefetch_block_numbers(["101.5", "100.2", "101.8", "105.0"])

        self.assertEqual(
            [client.get_blocks.call_args_list[i][0][0] for i in range(2)],
            ["100.2", "105.0"],
        )
        self.assertEqual(
            {
                timestamp: lookups.get_block_number(timestamp)
                for timestamp in ("100.2", "101.5", "101.8", "105.0")
            },
            {"100.2": 10, "101.5": 10, "101.8": 11, "105.0": 12},
        )
        client.resolve_block_number.assert_not_called()

    def test_prefetch_stops_when_blocks_do_not_reach_the_timestamps(self):
        client = MagicMock()
        client.get_blocks.return_value = [
            {"number": 9, "timestamp": {"from": "98.0", "to": "99.5"}}
        ]
        client.resolve_block_number.return_value = 10
        lookups = CachedMirrorNodeLookups(client)

        lookups.prefetch_block_numbers(["100.2"])

        client.get_blocks.assert_called_once()
        self.assertEqual(lookups.get_block_number("100.2"), 10)

    def test_prefetch_leaves_not_ingested_timestamps_to_single_lookups(self):
        client = MagicMock()
        client.get_blocks.return_value = []
        client.resolve_block_number.return_value = None
        lookups = CachedMirrorNodeLookups(client)

        lookups.prefetch_block_numbers(["100.2"])

        with self.assertRaises(UnresolvedBlockNumber):
            lookups.get_block_number("100.2")


class TestHederaNativeTransferIndexerProcessAllSafes(TestCase):
    def setUp(self):
        self.mirror_node = FakeMirrorNode()
        self.client = MagicMock(wraps=self.mirror_node)
        self.indexer = self._build_indexer()
        HederaTransferFeedCursor.objects.create(last_consensus_timestamp=FEED_CURSOR)

    def _build_indexer(self, **kwargs) -> HederaNativeTransferIndexer:
        indexer = HederaNativeTransferIndexer(client=self.client, **kwargs)
        indexer.get_feed_until_timestamp = lambda: FEED_UNTIL
        return indexer

    def _create_safe(
        self,
        account_id: str,
        created_at: datetime.datetime = SAFE_CREATION_DATE,
        cursor_timestamp: str | None = None,
        **kwargs,
    ):
        safe_contract = SafeContractFactory(
            ethereum_tx=EthereumTxFactory(
                block=EthereumBlockFactory(timestamp=created_at)
            ),
            **kwargs,
        )
        self.mirror_node.account_ids[safe_contract.address] = account_id
        if cursor_timestamp:
            HederaSafeTransferCursor.objects.create(
                safe_contract=safe_contract,
                hedera_account_id=account_id,
                last_consensus_timestamp=cursor_timestamp,
            )
        return safe_contract

    @staticmethod
    def _cursor_timestamp(safe_contract) -> str:
        return HederaSafeTransferCursor.objects.get(
            safe_contract=safe_contract
        ).last_consensus_timestamp

    @staticmethod
    def _is_following_feed(safe_contract) -> bool:
        return HederaSafeTransferCursor.objects.get(
            safe_contract=safe_contract
        ).following_feed

    @staticmethod
    def _feed_timestamp() -> str:
        return HederaTransferFeedCursor.objects.get().last_consensus_timestamp

    def _received_tinybar(self, safe_contract) -> list[int]:
        return [
            value // 10**10
            for value in InternalTx.objects.filter(to=safe_contract.address)
            .order_by("timestamp")
            .values_list("value", flat=True)
        ]

    def test_first_run_starts_the_feed_lag_seconds_behind_now(self):
        HederaTransferFeedCursor.objects.all().delete()
        indexer = HederaNativeTransferIndexer(client=self.client, feed_lag_seconds=30)

        indexer.process_all_safes()

        feed_timestamp = Decimal(self._feed_timestamp())
        now = Decimal(timezone.now().timestamp())
        self.assertLess(feed_timestamp, now - 29)
        self.assertGreater(feed_timestamp, now - 40)

    def test_new_safe_is_caught_up_to_the_feed_cursor_then_follows_the_feed(self):
        safe_contract = self._create_safe("0.0.5000001")
        self.mirror_node.transactions = [
            # Before the feed cursor: only its own catch-up query can see it
            incoming_tx("1767225000.000000001", ("0.0.5000001", 100)),
            # After the feed cursor: read from the network-wide feed
            incoming_tx("1767225900.000000001", ("0.0.5000001", 200)),
        ]

        number_safes, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual((number_safes, number_internal_txs), (1, 2))
        self.assertEqual(self._received_tinybar(safe_contract), [100, 200])
        self.client.get_crypto_transfers.assert_called_once_with(
            "0.0.5000001",
            after_timestamp=datetime_to_consensus_timestamp(SAFE_CREATION_DATE),
            until_timestamp=FEED_CURSOR,
        )
        self.client.get_network_crypto_transfer_pages.assert_called_once_with(
            FEED_CURSOR, FEED_UNTIL
        )
        self.assertEqual(self._feed_timestamp(), "1767225900.000000001")
        # Caught up to where the feed was, followed it from there
        self.assertEqual(self._cursor_timestamp(safe_contract), FEED_CURSOR)
        self.assertTrue(self._is_following_feed(safe_contract))

    def test_safes_following_the_feed_need_no_request_of_their_own(self):
        safe_contracts = [
            self._create_safe(f"0.0.60000{index:02d}", cursor_timestamp=FEED_CURSOR)
            for index in range(20)
        ]
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.6000003", 300)),
            incoming_tx("1767225800.000000001", ("0.0.9999999", 1)),  # not a Safe
        ]

        number_safes, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual((number_safes, number_internal_txs), (20, 1))
        self.assertEqual(self._received_tinybar(safe_contracts[3]), [300])
        self.client.resolve_account_id.assert_not_called()
        self.client.get_crypto_transfers.assert_not_called()
        self.client.get_network_crypto_transfer_pages.assert_called_once()
        for safe_contract in safe_contracts:
            self.assertTrue(self._is_following_feed(safe_contract))

    def test_feed_resolves_the_block_numbers_of_a_page_with_one_request(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx(f"17672257{second:02d}.000000001", ("0.0.5000001", second))
            for second in range(1, 31)
        ]

        self.indexer.process_all_safes()

        self.assertEqual(len(self._received_tinybar(safe_contract)), 30)
        self.client.get_blocks.assert_called_once_with("1767225701.000000001")
        self.client.resolve_block_number.assert_not_called()
        self.assertEqual(
            sorted(
                InternalTx.objects.filter(to=safe_contract.address).values_list(
                    "block_number", flat=True
                )
            ),
            list(range(1767225701, 1767225731)),
        )

    def test_catch_up_resolves_the_block_numbers_of_a_page_with_one_request(self):
        safe_contract = self._create_safe("0.0.5000001")
        self.mirror_node.transactions = [
            incoming_tx(f"17672255{second:02d}.000000001", ("0.0.5000001", second))
            for second in range(1, 31)
        ]

        self.indexer.process_all_safes()

        self.assertEqual(len(self._received_tinybar(safe_contract)), 30)
        self.client.get_blocks.assert_called_once_with("1767225501.000000001")
        self.client.resolve_block_number.assert_not_called()

    def test_safe_created_after_the_feed_cursor_needs_no_catch_up_query(self):
        safe_contract = self._create_safe(
            "0.0.5000001",
            created_at=datetime.datetime(2026, 1, 1, 0, 5, tzinfo=datetime.UTC),
        )
        self.mirror_node.transactions = [
            incoming_tx("1767226000.000000001", ("0.0.5000001", 100)),
        ]

        self.indexer.process_all_safes()

        self.client.get_crypto_transfers.assert_not_called()
        self.assertEqual(self._received_tinybar(safe_contract), [100])

    def test_one_feed_transaction_paying_several_safes_indexes_each_of_them(self):
        safe_a = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        safe_b = self._create_safe("0.0.5000002", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx(
                "1767225700.000000001",
                ("0.0.5000001", 600),
                ("0.0.7777777", 50),  # not a Safe
                ("0.0.5000002", 100),
            ),
        ]

        _, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual(number_internal_txs, 2)
        self.assertEqual(self._received_tinybar(safe_a), [600])
        self.assertEqual(self._received_tinybar(safe_b), [100])
        for safe_contract in (safe_a, safe_b):
            self.assertTrue(
                SafeRelevantTransaction.objects.filter(
                    safe=safe_contract.address
                ).exists()
            )

    def test_feed_ignores_transfers_sent_by_a_safe(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx(
                "1767225700.000000001", ("0.0.7777777", 100), sender="0.0.5000001"
            ),
        ]

        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [])

    def test_rerunning_the_feed_over_the_same_range_does_not_duplicate_rows(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
        ]
        self.indexer.process_all_safes()
        HederaTransferFeedCursor.objects.update(last_consensus_timestamp=FEED_CURSOR)

        _, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual(number_internal_txs, 0)
        self.assertEqual(self._received_tinybar(safe_contract), [100])

    def test_rereading_a_recent_transfer_does_not_send_its_events_again(self):
        # Events are sent for transfers younger than an hour, so a range read
        # twice (cursor moved back to reindex) would notify twice
        now = timezone.now()
        start = datetime_to_consensus_timestamp(now - datetime.timedelta(minutes=10))
        HederaTransferFeedCursor.objects.update(last_consensus_timestamp=start)
        self._create_safe("0.0.5000001", cursor_timestamp=start)
        self.mirror_node.transactions = [
            incoming_tx(
                datetime_to_consensus_timestamp(now - datetime.timedelta(minutes=5)),
                ("0.0.5000001", 100),
            )
        ]
        indexer = HederaNativeTransferIndexer(client=self.client)
        queue_service = MagicMock()
        with patch(
            "safe_transaction_service.history.signals.get_queue_service",
            return_value=queue_service,
        ):
            indexer.process_all_safes()
            number_events = queue_service.send_event.call_count
            HederaTransferFeedCursor.objects.update(last_consensus_timestamp=start)
            indexer.process_all_safes()

        self.assertGreater(number_events, 0)
        self.assertEqual(queue_service.send_event.call_count, number_events)
        self.assertEqual(InternalTx.objects.count(), 1)

    def test_rereading_a_range_restores_missing_legs(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
        ]
        self.indexer.process_all_safes()
        InternalTx.objects.all().delete()
        SafeRelevantTransaction.objects.all().delete()
        HederaTransferFeedCursor.objects.update(last_consensus_timestamp=FEED_CURSOR)

        _, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual(number_internal_txs, 1)
        self.assertEqual(self._received_tinybar(safe_contract), [100])
        self.assertEqual(SafeRelevantTransaction.objects.count(), 1)

    def test_safe_still_catching_up_is_left_out_of_the_feed_until_done(self):
        indexer = self._build_indexer(max_txs_per_safe_per_run=1)
        safe_contract = self._create_safe("0.0.5000001")
        self.mirror_node.transactions = [
            incoming_tx("1767225000.000000001", ("0.0.5000001", 100)),
            incoming_tx("1767225100.000000001", ("0.0.5000001", 200)),
            incoming_tx("1767225700.000000001", ("0.0.5000001", 300)),
        ]

        number_safes, _ = indexer.process_all_safes()

        self.assertEqual(number_safes, 0)
        self.assertEqual(self._received_tinybar(safe_contract), [100])
        self.assertEqual(self._cursor_timestamp(safe_contract), "1767225000.000000001")
        self.assertFalse(self._is_following_feed(safe_contract))
        # The feed moved on without it, its next catch-up covers the gap
        self.assertEqual(self._feed_timestamp(), "1767225700.000000001")

        indexer.max_txs_per_safe_per_run = 500
        number_safes, _ = indexer.process_all_safes()

        self.assertEqual(number_safes, 1)
        self.assertEqual(self._received_tinybar(safe_contract), [100, 200, 300])
        self.assertEqual(self._cursor_timestamp(safe_contract), "1767225700.000000001")
        self.assertTrue(self._is_following_feed(safe_contract))

    def test_unbanned_safe_is_caught_up_from_where_it_left_the_feed(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
        ]
        self.indexer.process_all_safes()

        SafeContract.objects.filter(address=safe_contract.address).update(banned=True)
        self.mirror_node.transactions.append(
            incoming_tx("1767225800.000000001", ("0.0.5000001", 200))
        )
        self.mirror_node.transactions.append(
            incoming_tx("1767225900.000000001", ("0.0.9999999", 1))
        )
        number_safes, _ = self.indexer.process_all_safes()
        self.assertEqual(number_safes, 0)
        self.assertEqual(self._received_tinybar(safe_contract), [100])
        # Left the feed where it was when the ban was noticed
        self.assertEqual(self._cursor_timestamp(safe_contract), "1767225700.000000001")
        self.assertFalse(self._is_following_feed(safe_contract))

        SafeContract.objects.filter(address=safe_contract.address).update(banned=False)
        self.indexer.process_all_safes()
        self.assertEqual(self._received_tinybar(safe_contract), [100, 200])
        self.client.get_crypto_transfers.assert_called_once_with(
            "0.0.5000001",
            after_timestamp="1767225700.000000001",
            until_timestamp="1767225900.000000001",
        )

    def test_feed_stops_at_its_page_limit_and_resumes_on_the_next_run(self):
        self.mirror_node.page_size = 2
        indexer = self._build_indexer(feed_max_pages_per_run=1)
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx(f"17672257{second}0.000000001", ("0.0.5000001", second))
            for second in range(1, 6)
        ]

        indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [1, 2])
        self.assertEqual(self._feed_timestamp(), "1767225720.000000001")

        for _ in range(2):
            indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [1, 2, 3, 4, 5])
        self.client.get_crypto_transfers.assert_not_called()

    def test_unresolved_block_number_stops_the_feed_before_that_transfer(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
            incoming_tx("1767225800.000000001", ("0.0.5000001", 200)),
            incoming_tx("1767225900.000000001", ("0.0.5000001", 300)),
        ]
        self.mirror_node.unresolvable_timestamps = {"1767225800.000000001"}

        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [100])
        self.assertEqual(self._feed_timestamp(), "1767225700.000000001")

        self.mirror_node.unresolvable_timestamps = set()
        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [100, 200, 300])

    def test_feed_page_limit_downloads_no_page_it_does_not_process(self):
        self.mirror_node.page_size = 1
        self.mirror_node.transactions = [
            incoming_tx(f"1767225700.00000000{i}", ("0.0.9", 1)) for i in range(1, 4)
        ]
        pages = self.mirror_node.get_network_crypto_transfer_pages
        downloaded = []

        def counting_pages(*args):
            for page in pages(*args):
                downloaded.append(page)
                yield page

        self.client.get_network_crypto_transfer_pages.side_effect = counting_pages
        self._build_indexer(feed_max_pages_per_run=2).process_all_safes()

        self.assertEqual(len(downloaded), 2)
        self.assertEqual(self._feed_timestamp(), "1767225700.000000002")

    def test_feed_page_limit_warns_only_if_there_may_be_more_to_read(self):
        logger_name = (
            "safe_transaction_service.history.indexers.hedera_native_transfer_indexer"
        )
        indexer = self._build_indexer(feed_max_pages_per_run=1)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.9", 1))
        ]
        with self.assertNoLogs(logger_name, level="WARNING"):
            indexer.process_all_safes()

        self.mirror_node.transactions = [
            incoming_tx(f"1767225800.{i:09d}", ("0.0.9", 1)) for i in range(1, 101)
        ]
        with self.assertLogs(logger_name, level="WARNING"):
            indexer.process_all_safes()

    def test_catch_up_error_keeps_the_pages_already_read(self):
        safe_contract = self._create_safe("0.0.5000001")
        transfers = [
            incoming_tx(f"1767225{second:03d}.000000001", ("0.0.5000001", second))
            for second in range(1, 151)
        ]

        def failing_after_150_transfers(*args, **kwargs):
            yield from transfers
            raise HederaMirrorNodeClientException("Mirror Node returned 503")

        self.client.get_crypto_transfers.side_effect = failing_after_150_transfers
        self.indexer.process_all_safes()

        # The full page of 100 is kept, the partial one is read again
        self.assertEqual(self._received_tinybar(safe_contract), list(range(1, 101)))
        self.assertEqual(self._cursor_timestamp(safe_contract), "1767225100.000000001")
        self.assertFalse(self._is_following_feed(safe_contract))

        self.client.get_crypto_transfers.side_effect = (
            self.mirror_node.get_crypto_transfers
        )
        self.mirror_node.transactions = transfers
        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), list(range(1, 151)))
        self.assertTrue(self._is_following_feed(safe_contract))

    def test_run_does_nothing_while_another_run_holds_the_lock(self):
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
        ]
        other_run = connection.copy()
        try:
            with other_run.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_lock(%s)", [INDEXING_LOCK_ID])

            self.assertIsNone(self.indexer.process_all_safes())
            self.assertEqual(self._feed_timestamp(), FEED_CURSOR)
            self.client.get_network_crypto_transfer_pages.assert_not_called()
        finally:
            with other_run.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [INDEXING_LOCK_ID])
            other_run.close()

        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [100])

    def test_feed_error_keeps_the_pages_already_read(self):
        self.mirror_node.page_size = 1
        self.mirror_node.feed_error_after_pages = 1
        safe_contract = self._create_safe("0.0.5000001", cursor_timestamp=FEED_CURSOR)
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
            incoming_tx("1767225800.000000001", ("0.0.5000001", 200)),
        ]

        number_safes, number_internal_txs = self.indexer.process_all_safes()

        self.assertEqual((number_safes, number_internal_txs), (1, 1))
        self.assertEqual(self._feed_timestamp(), "1767225700.000000001")

        self.mirror_node.feed_error_after_pages = None
        self.indexer.process_all_safes()

        self.assertEqual(self._received_tinybar(safe_contract), [100, 200])

    def test_safes_keep_following_the_feed_after_an_interrupted_run(self):
        # A run killed after committing some feed pages (deploy, task
        # timeout) must not push the Safes it was following back to their
        # own per-account catch-up: a busy one could never catch up again
        safe_contract = self._create_safe("0.0.5000001")
        self.mirror_node.page_size = 1
        self.mirror_node.transactions = [
            incoming_tx("1767225700.000000001", ("0.0.5000001", 100)),
            incoming_tx("1767225800.000000001", ("0.0.5000001", 200)),
        ]
        pages = self.mirror_node.get_network_crypto_transfer_pages

        def interrupted_after_first_page(*args):
            page_iterator = pages(*args)
            yield next(page_iterator)
            raise KeyboardInterrupt  # Not an Exception: escapes every handler

        self.client.get_network_crypto_transfer_pages.side_effect = (
            interrupted_after_first_page
        )
        with self.assertRaises(KeyboardInterrupt):
            self.indexer.process_all_safes()
        self.assertEqual(self._feed_timestamp(), "1767225700.000000001")
        self.client.get_crypto_transfers.reset_mock()

        self.client.get_network_crypto_transfer_pages.side_effect = pages
        self.indexer.process_all_safes()

        self.client.get_crypto_transfers.assert_not_called()
        self.assertEqual(self._received_tinybar(safe_contract), [100, 200])

    def test_one_safe_raising_does_not_prevent_processing_the_others(self):
        failing_safe = self._create_safe("0.0.5000001")
        ok_safe = self._create_safe("0.0.5000002")

        def resolve_account_id(address):
            if address == failing_safe.address:
                raise ValueError("Mirror Node blew up resolving this Safe")
            return self.mirror_node.resolve_account_id(address)

        self.client.resolve_account_id.side_effect = resolve_account_id

        number_safes, _ = self.indexer.process_all_safes()

        # Only the healthy Safe follows the feed; the failing one is logged
        # and retried on the next run rather than aborting the whole run.
        self.assertEqual(number_safes, 1)
        self.assertEqual(
            HederaSafeTransferCursor.objects.get(
                safe_contract=ok_safe
            ).hedera_account_id,
            "0.0.5000002",
        )
        self.assertIsNone(
            HederaSafeTransferCursor.objects.get(
                safe_contract=failing_safe
            ).hedera_account_id
        )

    def test_banned_safe_is_skipped(self):
        banned_safe = self._create_safe("0.0.5000001", banned=True)
        ok_safe = self._create_safe("0.0.5000002")

        number_safes, _ = self.indexer.process_all_safes()

        self.assertEqual(number_safes, 1)
        self.client.resolve_account_id.assert_called_once_with(ok_safe.address)
        self.assertFalse(
            HederaSafeTransferCursor.objects.filter(safe_contract=banned_safe).exists()
        )


class TestHederaNativeTransferVisibleThroughExistingConsumers(TestCase):
    def setUp(self):
        # All-transactions responses are cached in Redis, keyed by tx hash
        get_redis().flushall()

    def tearDown(self):
        get_redis().flushall()
        TransactionServiceProvider.del_singleton()

    def test_incoming_native_transfer_appears_in_transaction_service_feed(self):
        client = MagicMock()
        client.resolve_account_id.return_value = "0.0.10127045"
        client.resolve_block_number.return_value = 12345
        client.resolve_evm_address.return_value = (
            "0xaaaa00000000000000000000000000000000aaaa"
        )
        client.get_crypto_transfers.return_value = iter([INCOMING_TX])

        safe_contract = SafeContractFactory(
            address="0x1234567890123456789012345678901234567890"
        )
        indexer = HederaNativeTransferIndexer(client=client)
        indexer.process_safe(safe_contract)

        transaction_service = TransactionServiceProvider()
        identifiers = transaction_service.get_all_tx_identifiers(safe_contract.address)
        self.assertEqual(len(identifiers), 1)

        ether_transfers = list(
            InternalTx.objects.ether_txs_for_address(safe_contract.address)
        )
        self.assertEqual(len(ether_transfers), 1)
        self.assertEqual(ether_transfers[0]._value, 500_000_000 * 10**10)
        self.assertEqual(
            ether_transfers[0].ethereum_tx.hedera_transaction_id,
            INCOMING_TX["transaction_id"],
        )

    def test_all_transactions_api_never_returns_null_block_number_or_execution_date(
        self,
    ):
        # A synthetic EthereumTx has no linked EthereumBlock until a real
        # EthereumBlock row for its resolved block number exists locally
        # (see test_leaves_ethereum_tx_block_none_when_no_matching_block_exists).
        # Downstream API consumers (e.g. the Safe Client Gateway) validate
        # this endpoint's blockNumber/executionDate as always-present, so
        # EthereumTx.execution_date and the serializer's get_block_number
        # must fall back to the InternalTx leg's own (always-set) values
        # rather than ever surfacing null for these fields.
        client = MagicMock()
        client.resolve_account_id.return_value = "0.0.10127045"
        client.resolve_block_number.return_value = 12345
        client.resolve_evm_address.return_value = (
            "0xaaaa00000000000000000000000000000000aaaa"
        )
        client.get_crypto_transfers.return_value = iter([INCOMING_TX])

        safe_contract = SafeContractFactory(
            address="0x1234567890123456789012345678901234567890"
        )
        indexer = HederaNativeTransferIndexer(client=client)
        indexer.process_safe(safe_contract)

        internal_tx = InternalTx.objects.get()
        self.assertIsNone(internal_tx.ethereum_tx.block_id)

        response = self.client.get(
            reverse("v2:history:all-transactions", args=(safe_contract.address,))
        )
        self.assertEqual(response.status_code, 200)
        results = response.json()["results"]
        self.assertEqual(len(results), 1)
        self.assertIsNotNone(results[0]["blockNumber"])
        self.assertEqual(results[0]["blockNumber"], internal_tx.block_number)
        self.assertIsNotNone(results[0]["executionDate"])
        self.assertEqual(
            results[0]["hederaTransactionId"],
            INCOMING_TX["transaction_id"],
        )

    def _index_incoming_transfer(self):
        client = MagicMock()
        client.resolve_account_id.return_value = "0.0.10127045"
        client.resolve_block_number.return_value = 12345
        client.resolve_evm_address.return_value = (
            "0xaaaa00000000000000000000000000000000aaaa"
        )
        client.get_crypto_transfers.return_value = iter([INCOMING_TX])

        safe_contract = SafeContractFactory(
            address="0x1234567890123456789012345678901234567890"
        )
        HederaNativeTransferIndexer(client=client).process_safe(safe_contract)
        return safe_contract

    @override_settings(HEDERA_MIRROR_NODE_URL="https://mirror.example")
    def test_transfers_expose_hedera_transaction_id(self):
        safe_contract = self._index_incoming_transfer()
        evm_internal_tx = InternalTxFactory(to=safe_contract.address, value=7)

        for url_name in ("v1:history:transfers", "v1:history:incoming-transfers"):
            with self.subTest(url_name=url_name):
                response = self.client.get(
                    reverse(url_name, args=(safe_contract.address,))
                )
                self.assertEqual(response.status_code, 200)
                hedera_transaction_ids = {
                    result["transactionHash"]: result["hederaTransactionId"]
                    for result in response.json()["results"]
                }
                self.assertEqual(len(hedera_transaction_ids), 2)
                self.assertIsNone(
                    hedera_transaction_ids[evm_internal_tx.ethereum_tx_id]
                )
                synthetic_tx_hash = EthereumTx.objects.get(
                    hedera_transaction_id__isnull=False
                ).tx_hash
                self.assertEqual(
                    hedera_transaction_ids[synthetic_tx_hash],
                    INCOMING_TX["transaction_id"],
                )

        response = self.client.get(
            reverse("v2:history:all-transactions", args=(safe_contract.address,))
        )
        self.assertEqual(response.status_code, 200)
        synthetic_tx = next(
            result
            for result in response.json()["results"]
            if result["hederaTransactionId"]
        )
        self.assertEqual(
            synthetic_tx["transfers"][0]["hederaTransactionId"],
            INCOMING_TX["transaction_id"],
        )

    @override_settings(HEDERA_MIRROR_NODE_URL=None)
    def test_transfers_do_not_join_ethereum_tx_when_hedera_indexing_disabled(self):
        safe_address = "0x1234567890123456789012345678901234567890"
        queryset = InternalTx.objects.ether_and_token_txs(safe_address)
        self.assertNotIn("history_ethereumtx", str(queryset.query))

        InternalTxFactory(to=safe_address, value=7)
        transfers = list(queryset)
        self.assertEqual(len(transfers), 1)
        self.assertIsNone(transfers[0]["hedera_transaction_id"])
