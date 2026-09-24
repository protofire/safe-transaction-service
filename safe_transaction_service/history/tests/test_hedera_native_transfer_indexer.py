import datetime
from unittest.mock import MagicMock

from django.test import SimpleTestCase, TestCase

from ..indexers.hedera_native_transfer_indexer import (
    HederaNativeTransferIndexer,
    consensus_timestamp_to_datetime,
    datetime_to_consensus_timestamp,
    extract_transfer_legs,
)
from ..models import (
    EthereumTx,
    HederaSafeTransferCursor,
    InternalTx,
    SafeRelevantTransaction,
)
from ..services.transaction_service import TransactionServiceProvider
from .factories import EthereumBlockFactory, SafeContractFactory

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

    def test_outgoing_transfer_where_safe_is_the_payer(self):
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
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [
                {
                    "counterparty_account_id": "0.0.2222222",
                    "amount_tinybar": -300_000_000,
                }
            ],
        )

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

    def test_safe_only_pays_fee_for_unrelated_transfer_returns_empty(self):
        # Safe is only the transaction's payer/fee-sponsor; the actual transfer
        # is between two unrelated other accounts. No real transfer touched
        # the Safe, so this must return nothing.
        mirror_tx = {
            "transaction_id": f"{SAFE_ACCOUNT_ID}-1700000000-000000005",
            "charged_tx_fee": 1_200_000,
            "transfers": [
                {"account": SAFE_ACCOUNT_ID, "amount": -1_200_000},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 800_000},
                {"account": "0.0.1111111", "amount": -500_000_000},
                {"account": "0.0.2222222", "amount": 500_000_000},
            ],
        }
        self.assertEqual(extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID), [])

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
        # A third party (not the Safe, not the counterparty) sponsors the fee.
        # Their leg, after fee-adjustment, nets to zero and must not appear.
        mirror_tx = {
            "transaction_id": "0.0.9999999-1700000000-000000007",
            "charged_tx_fee": 1_000_000,
            "transfers": [
                {"account": "0.0.9999999", "amount": -1_000_000},
                {"account": "0.0.7", "amount": 400_000},
                {"account": "0.0.98", "amount": 600_000},
                {"account": SAFE_ACCOUNT_ID, "amount": -500_000_000},
                {"account": "0.0.2222222", "amount": 500_000_000},
            ],
        }
        legs = extract_transfer_legs(mirror_tx, SAFE_ACCOUNT_ID)
        self.assertEqual(
            legs,
            [
                {
                    "counterparty_account_id": "0.0.2222222",
                    "amount_tinybar": -500_000_000,
                }
            ],
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

OUTGOING_TX = {
    "transaction_id": "0.0.10127045-1700000100-000000002",
    "consensus_timestamp": "1700000100.000000002",
    "result": "SUCCESS",
    "charged_tx_fee": 1_200_000,
    "transfers": [
        {"account": "0.0.10127045", "amount": -(300_000_000 + 1_200_000)},
        {"account": "0.0.7", "amount": 400_000},
        {"account": "0.0.98", "amount": 800_000},
        {"account": "0.0.2222222", "amount": 300_000_000},
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

        expected_timestamp = datetime_to_consensus_timestamp(self.safe_contract.created)
        self.client.get_crypto_transfers.assert_called_once_with(
            "0.0.10127045", after_timestamp=expected_timestamp
        )
        self.safe_contract.refresh_from_db()
        self.assertEqual(
            self.safe_contract.hedera_transfer_cursor.last_consensus_timestamp,
            expected_timestamp,
        )

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

    def test_links_synthetic_ethereum_tx_to_existing_ethereum_block(self):
        # The Mirror-Node-resolved block number (12345, per setUp) already
        # has a real EthereumBlock row, created by the existing EVM indexer.
        # The synthetic EthereumTx should be linked to it.
        EthereumBlockFactory(number=12345)
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        self.indexer.process_safe(self.safe_contract)

        internal_tx = InternalTx.objects.get()
        self.assertEqual(internal_tx.ethereum_tx.block_id, 12345)

    def test_leaves_ethereum_tx_block_none_when_no_matching_block_exists(self):
        # No EthereumBlock row exists for the resolved block number (12345) —
        # unchanged default behavior: block stays None.
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX])
        self.indexer.process_safe(self.safe_contract)

        internal_tx = InternalTx.objects.get()
        self.assertIsNone(internal_tx.ethereum_tx.block_id)

    def test_creates_safe_relevant_transaction_for_both_directions(self):
        self.client.get_crypto_transfers.return_value = iter([INCOMING_TX, OUTGOING_TX])
        self.indexer.process_safe(self.safe_contract)

        relevant = SafeRelevantTransaction.objects.filter(
            safe=self.safe_contract.address
        )
        self.assertEqual(relevant.count(), 2)

    def test_outgoing_transfer_sets_correct_from_and_to(self):
        self.client.get_crypto_transfers.return_value = iter([OUTGOING_TX])
        self.indexer.process_safe(self.safe_contract)

        internal_tx = InternalTx.objects.get()
        self.assertEqual(internal_tx._from, self.safe_contract.address)
        self.assertEqual(internal_tx.to, "0xbbBB00000000000000000000000000000000bBbb")
        self.assertEqual(internal_tx.value, 300_000_000 * 10**10)
        self.assertEqual(internal_tx.ethereum_tx._from, internal_tx._from)
        self.assertEqual(internal_tx.ethereum_tx.to, internal_tx.to)

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
        self.client.resolve_evm_address.side_effect = (
            lambda account_id: {
                "0.0.1111111": "0xAAAA00000000000000000000000000000000aaaa",
                "0.0.3333333": "0xcCcc00000000000000000000000000000000cCcC",
            }[account_id]
        )
        self.client.get_crypto_transfers.return_value = iter([multi_leg_tx])

        created_count = self.indexer.process_safe(self.safe_contract)

        self.assertEqual(created_count, 2)
        first_internal_tx = InternalTx.objects.order_by("trace_address").first()
        self.assertEqual(first_internal_tx.trace_address, "0")
        ethereum_tx = first_internal_tx.ethereum_tx
        self.assertEqual(ethereum_tx._from, first_internal_tx._from)
        self.assertEqual(ethereum_tx.to, first_internal_tx.to)

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
            datetime_to_consensus_timestamp(self.safe_contract.created),
        )

    def test_account_not_found_returns_zero_without_creating_cursor_account_id(self):
        self.client.resolve_account_id.return_value = None
        created_count = self.indexer.process_safe(self.safe_contract)
        self.assertEqual(created_count, 0)
        self.client.get_crypto_transfers.assert_not_called()


class TestHederaNativeTransferIndexerProcessAllSafes(TestCase):
    def test_processes_every_tracked_safe(self):
        client = MagicMock()
        client.resolve_account_id.return_value = "0.0.10127045"
        client.get_crypto_transfers.return_value = iter([])
        indexer = HederaNativeTransferIndexer(client=client)
        SafeContractFactory()
        SafeContractFactory()

        number_safes, number_internal_txs = indexer.process_all_safes()

        self.assertEqual(number_safes, 2)
        self.assertEqual(number_internal_txs, 0)

    def test_one_safe_raising_does_not_prevent_processing_the_others(self):
        failing_safe = SafeContractFactory()
        ok_safe = SafeContractFactory()

        client = MagicMock()

        def resolve_account_id(address):
            if address == failing_safe.address:
                raise ValueError("Mirror Node blew up resolving this Safe")
            return "0.0.10127045"

        client.resolve_account_id.side_effect = resolve_account_id
        client.get_crypto_transfers.return_value = iter([])
        indexer = HederaNativeTransferIndexer(client=client)

        number_safes, number_internal_txs = indexer.process_all_safes()

        # Only the healthy Safe counts as processed; the failing one is
        # logged and skipped rather than aborting the whole run.
        self.assertEqual(number_safes, 1)
        self.assertEqual(number_internal_txs, 0)

        ok_safe.refresh_from_db()
        self.assertEqual(
            ok_safe.hedera_transfer_cursor.hedera_account_id, "0.0.10127045"
        )
        failing_safe.refresh_from_db()
        self.assertIsNone(failing_safe.hedera_transfer_cursor.hedera_account_id)

    def test_banned_safe_is_skipped(self):
        banned_safe = SafeContractFactory(banned=True)
        ok_safe = SafeContractFactory()

        client = MagicMock()
        client.resolve_account_id.return_value = "0.0.10127045"
        client.get_crypto_transfers.return_value = iter([])
        indexer = HederaNativeTransferIndexer(client=client)

        number_safes, _ = indexer.process_all_safes()

        self.assertEqual(number_safes, 1)
        client.resolve_account_id.assert_called_once_with(ok_safe.address)
        self.assertFalse(
            HederaSafeTransferCursor.objects.filter(safe_contract=banned_safe).exists()
        )


class TestHederaNativeTransferVisibleThroughExistingConsumers(TestCase):
    def tearDown(self):
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
