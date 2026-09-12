"""Tests for the incremental native-balance rollup.

The property that matters is the first one: *incremental equals full
recompute*. Everything else here defends a specific way that property can
break — a Safe indexed after the transfers it received, a block that gets
reorged out from under an already-applied delta, a crash between applying
the delta and moving the watermark, a negative balance clamped at the
wrong end of the pipeline.

Block numbers are explicit and start well above the factories' own
sequence so that nothing a `SubFactory` creates can become the chain tip
by accident. `ETH_REORG_BLOCKS` is 1 under `config.settings.test`, so a
single trailing confirmed block is enough to lift the safe head past the
blocks a test actually wrote to.
"""

from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from eth_account import Account

from safe_transaction_service.analytics.models import (
    AnalyticsWatermark,
    SafeNativeBalance,
)
from safe_transaction_service.analytics.tasks import (
    NATIVE_BALANCE_WATERMARK,
    _calculate_native_balances_from_db,
    compute_native_balance_rollup_task,
    native_balance_head_block,
    read_native_balance_rollup,
    run_native_balance_rollup,
)
from safe_transaction_service.history.models import EthereumTxCallType
from safe_transaction_service.history.tests.factories import (
    EthereumBlockFactory,
    EthereumTxFactory,
    InternalTxFactory,
    SafeContractFactory,
)

# Well clear of `EthereumBlockFactory.number`'s 1-based sequence.
BASE_BLOCK = 1_000_000


class NativeBalanceRollupTestCase(TestCase):
    """Shared scaffolding: explicit blocks, explicit confirmation."""

    def setUp(self):
        super().setUp()
        self.next_block = BASE_BLOCK

    def block(self, confirmed: bool = True):
        """A new block, one number above the last one this test made."""
        self.next_block += 1
        return EthereumBlockFactory(number=self.next_block, confirmed=confirmed)

    def safe(self, block=None):
        """A Safe whose creation transaction sits in `block` (a fresh
        confirmed one by default), so the Safe's own indexing height is
        under the test's control."""
        return SafeContractFactory(
            ethereum_tx=EthereumTxFactory(block=block or self.block())
        )

    def transfer(self, block, value: int, to=None, _from=None):
        """One successful native-value internal tx inside `block`."""
        kwargs = {}
        if to is not None:
            kwargs["to"] = to
        if _from is not None:
            kwargs["_from"] = _from
        return InternalTxFactory(
            ethereum_tx=EthereumTxFactory(block=block),
            value=value,
            call_type=EthereumTxCallType.CALL.value,
            error=None,
            **kwargs,
        )

    def advance_head(self, blocks: int = 3):
        """Push the confirmed chain tip past everything written so far, so
        `native_balance_head_block()` covers it. Empty blocks — the point
        is only the height."""
        for _ in range(blocks):
            self.block(confirmed=True)

    def initialise(self, at_block: int = 0):
        """Stand in for `manage.py backfill_native_balances` on an empty
        chain: a watermark at `at_block` and no rows. The first run then
        seeds every Safe at `at_block` and applies everything above it."""
        AnalyticsWatermark.objects.create(
            name=NATIVE_BALANCE_WATERMARK,
            block_number=at_block,
            computed_at="2026-01-01T00:00:00+00:00",
        )

    def totals(self) -> tuple[int, int]:
        rollup = read_native_balance_rollup()
        return rollup["balance_wei"], rollup["safes_with_balance"]


class TestHeadBlock(NativeBalanceRollupTestCase):
    """`native_balance_head_block` is the only thing standing between the
    rollup and a reorg it cannot undo."""

    def test_none_when_nothing_confirmed(self):
        self.block(confirmed=False)
        self.assertIsNone(native_balance_head_block())

    def test_none_when_no_blocks_at_all(self):
        self.assertIsNone(native_balance_head_block())

    def test_confirmed_head_bounds_it(self):
        confirmed = self.block(confirmed=True)
        # Three unconfirmed blocks on top: the depth bound would allow
        # `tip - 1`, the confirmation flag does not.
        for _ in range(3):
            self.block(confirmed=False)
        self.assertEqual(native_balance_head_block(), confirmed.number)

    def test_reorg_depth_bounds_it(self):
        # Everything confirmed, so `confirmed` is not the binding term —
        # the depth backstop is, and holds the head one block (
        # ETH_REORG_BLOCKS == 1 in test settings) below the tip.
        for _ in range(4):
            tip = self.block(confirmed=True)
        self.assertEqual(native_balance_head_block(), tip.number - 1)


class TestIncrementalMatchesFullRecompute(NativeBalanceRollupTestCase):
    """The headline property. `_calculate_native_balances_from_db` is the
    reference the 16-shard chord was itself verified against
    (`TestNativeBalanceShards`), so matching it is what "no regression on
    /tvl/" means."""

    def test_matches_after_several_incremental_runs(self):
        safes = [self.safe() for _ in range(4)]
        self.initialise()

        # Three windows, each closed by its own rollup run: incoming,
        # outgoing and a second incoming leg, so the running total has to
        # survive both signs and repeated touches of the same row.
        window_one = self.block()
        for safe in safes:
            self.transfer(window_one, 1_000, to=safe.address)
        self.advance_head()
        run_native_balance_rollup()

        window_two = self.block()
        self.transfer(window_two, 250, _from=safes[0].address)
        self.transfer(window_two, 4_000, to=safes[1].address)
        self.advance_head()
        run_native_balance_rollup()

        window_three = self.block()
        self.transfer(window_three, 7, to=safes[2].address)
        self.advance_head()
        run_native_balance_rollup()

        reference_balance, reference_count = _calculate_native_balances_from_db()
        self.assertEqual(self.totals(), (reference_balance, reference_count))
        self.assertEqual(reference_balance, 1_000 * 4 - 250 + 4_000 + 7)
        self.assertEqual(reference_count, 4)

    def test_transfers_between_two_safes_net_out(self):
        sender, receiver = self.safe(), self.safe()
        self.initialise()
        block = self.block()
        self.transfer(block, 5_000, to=sender.address)
        self.transfer(block, 2_000, to=receiver.address, _from=sender.address)
        self.advance_head()
        run_native_balance_rollup()

        self.assertEqual(self.totals(), _calculate_native_balances_from_db())
        self.assertEqual(self.totals(), (5_000, 2))

    def test_non_safe_counterparties_are_not_stored(self):
        safe = self.safe()
        self.initialise()
        block = self.block()
        # `to` defaults to a random non-Safe address; only the Safe side
        # of this transfer belongs in the rollup.
        self.transfer(block, 900, _from=safe.address)
        self.advance_head()
        run_native_balance_rollup()

        self.assertEqual(SafeNativeBalance.objects.count(), 1)
        self.assertEqual(SafeNativeBalance.objects.get().balance_wei, Decimal(-900))


class TestIdempotency(NativeBalanceRollupTestCase):
    def test_second_run_without_new_blocks_changes_nothing(self):
        safe = self.safe()
        self.initialise()
        self.transfer(self.block(), 3_000, to=safe.address)
        self.advance_head()

        first = run_native_balance_rollup()
        after_first = self.totals()
        watermark_after_first = AnalyticsWatermark.objects.get(
            name=NATIVE_BALANCE_WATERMARK
        ).block_number

        second = run_native_balance_rollup()

        self.assertEqual(after_first, self.totals())
        self.assertEqual(
            watermark_after_first,
            AnalyticsWatermark.objects.get(name=NATIVE_BALANCE_WATERMARK).block_number,
        )
        self.assertEqual(first["watermark_to"], second["watermark_to"])
        # Nothing left to seed and an empty (W, head] range.
        self.assertEqual(second["seeded_safes"], 0)
        self.assertEqual(second["touched_safes"], 0)

    def test_zero_balance_safes_are_not_reseeded_every_run(self):
        """A Safe that has never moved native value still gets a row —
        otherwise "absent from the rollup" would mean "new Safe" for it on
        every single run, and the seed step would re-walk the fleet."""
        self.safe()
        self.initialise()
        self.advance_head()

        first = run_native_balance_rollup()
        self.assertEqual(first["seeded_safes"], 1)
        self.assertEqual(SafeNativeBalance.objects.count(), 1)
        self.assertEqual(SafeNativeBalance.objects.get().balance_wei, Decimal(0))

        self.advance_head()
        self.assertEqual(run_native_balance_rollup()["seeded_safes"], 0)


class TestSafeIndexedMidWindow(NativeBalanceRollupTestCase):
    """A Safe enters `history_safecontract` only when the indexer gets to
    it, which can be well after the transfers it already received. The
    seed step is bounded by the OLD watermark for exactly this case, and
    getting the bound wrong is silent either way: too low and the Safe
    loses its pre-indexing history forever, too high and its first window
    is counted twice.
    """

    def test_new_safe_keeps_transfers_from_before_it_was_indexed(self):
        existing = self.safe()
        self.initialise()

        # Funded while it is still just an address: the internal tx is
        # indexed, the `history_safecontract` row is not written yet.
        latecomer_address = Account.create().address
        creation_block = self.block()
        self.transfer(creation_block, 40, to=latecomer_address)
        self.transfer(self.block(), 100, to=existing.address)
        self.advance_head()

        run_native_balance_rollup()
        watermark = AnalyticsWatermark.objects.get(
            name=NATIVE_BALANCE_WATERMARK
        ).block_number
        self.assertLess(creation_block.number, watermark)
        # Not a Safe yet, so not in the rollup and not in the totals.
        self.assertFalse(
            SafeNativeBalance.objects.filter(safe_address=latecomer_address).exists()
        )

        # The indexer catches up, and a further transfer lands above the
        # watermark in the same run.
        SafeContractFactory(
            address=latecomer_address,
            ethereum_tx=EthereumTxFactory(block=creation_block),
        )
        self.transfer(self.block(), 7, to=latecomer_address)
        self.advance_head()

        summary = run_native_balance_rollup()
        self.assertEqual(summary["seeded_safes"], 1)

        # 40 from the seed (blocks <= W), 7 from the delta ((W, head]).
        self.assertEqual(
            SafeNativeBalance.objects.get(safe_address=latecomer_address).balance_wei,
            Decimal(47),
        )
        self.assertEqual(self.totals(), _calculate_native_balances_from_db())

    def test_transfers_inside_the_window_are_counted_once(self):
        """The other side of the same bound. Here the Safe's only transfer
        sits *inside* (W, head], where the delta will read it — a seed
        bounded at `head` rather than at `W` would add it a second time.
        """
        self.initialise()
        self.safe()
        self.advance_head()
        run_native_balance_rollup()
        watermark = AnalyticsWatermark.objects.get(
            name=NATIVE_BALANCE_WATERMARK
        ).block_number

        newcomer = self.safe(block=self.block())
        funding_block = self.block()
        self.transfer(funding_block, 500, to=newcomer.address)
        self.advance_head()
        self.assertGreater(funding_block.number, watermark)

        run_native_balance_rollup()

        self.assertEqual(
            SafeNativeBalance.objects.get(safe_address=newcomer.address).balance_wei,
            Decimal(500),
        )
        self.assertEqual(self.totals(), _calculate_native_balances_from_db())


class TestConfirmationBoundary(NativeBalanceRollupTestCase):
    def test_unconfirmed_blocks_are_not_consumed(self):
        safe = self.safe()
        self.initialise()

        settled = self.block(confirmed=True)
        self.transfer(settled, 1_000, to=safe.address)
        self.advance_head()

        # Above the safe head: confirmed=False, so a reorg could still
        # take it — and with it the row whose value we would have added.
        pending = self.block(confirmed=False)
        self.transfer(pending, 999_999, to=safe.address)

        summary = run_native_balance_rollup()
        self.assertEqual(self.totals(), (1_000, 1))
        self.assertLess(summary["watermark_to"], pending.number)

        # Once it confirms and the tip moves past it, the next run picks
        # it up — no backfill, no gap.
        pending.set_confirmed()
        self.advance_head()
        run_native_balance_rollup()
        self.assertEqual(self.totals(), (1_000_999, 1))
        self.assertEqual(self.totals(), _calculate_native_balances_from_db())


class TestNegativeBalances(NativeBalanceRollupTestCase):
    """Stored signed, clamped on read. Clamping on write would make an
    indexing gap permanent."""

    def test_negative_row_is_stored_signed_and_excluded_from_totals(self):
        solvent, underwater = self.safe(), self.safe()
        self.initialise()
        block = self.block()
        self.transfer(block, 800, to=solvent.address)
        # Only the outgoing leg is indexed — the matching incoming one
        # has not been picked up yet.
        self.transfer(block, 300, _from=underwater.address)
        self.advance_head()
        run_native_balance_rollup()

        self.assertEqual(
            SafeNativeBalance.objects.get(safe_address=underwater.address).balance_wei,
            Decimal(-300),
        )
        # Excluded from the sum and from the count, not floored into it.
        self.assertEqual(self.totals(), (800, 1))
        self.assertEqual(self.totals(), _calculate_native_balances_from_db())

    def test_row_recovers_when_the_missing_transfer_is_indexed(self):
        safe = self.safe()
        self.initialise()
        first = self.block()
        self.transfer(first, 300, _from=safe.address)
        self.advance_head()
        run_native_balance_rollup()
        self.assertEqual(self.totals(), (0, 0))

        second = self.block()
        self.transfer(second, 500, to=safe.address)
        self.advance_head()
        run_native_balance_rollup()

        self.assertEqual(
            SafeNativeBalance.objects.get(safe_address=safe.address).balance_wei,
            Decimal(200),
        )
        self.assertEqual(self.totals(), (200, 1))


class TestAtomicity(NativeBalanceRollupTestCase):
    def test_failure_between_delta_and_watermark_leaves_neither(self):
        safe = self.safe()
        self.initialise()
        self.transfer(self.block(), 2_500, to=safe.address)
        self.advance_head()

        with patch(
            "safe_transaction_service.analytics.models."
            "AnalyticsWatermark.objects.update_or_create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertRaises(RuntimeError):
                run_native_balance_rollup()

        # The delta rolled back with the watermark write.
        self.assertEqual(self.totals(), (0, 0))
        self.assertEqual(
            AnalyticsWatermark.objects.get(name=NATIVE_BALANCE_WATERMARK).block_number,
            0,
        )

        # And the retry is a clean first application, not a double one.
        run_native_balance_rollup()
        self.assertEqual(self.totals(), (2_500, 1))
        self.assertEqual(self.totals(), _calculate_native_balances_from_db())


class TestRefusalPaths(NativeBalanceRollupTestCase):
    """Every path that declines to run says why at ERROR and leaves the
    rollup untouched — a wrong number here is worse than a stale one."""

    def test_uninitialised_rollup_refuses(self):
        self.safe()
        self.advance_head()
        with self.assertLogs(
            "safe_transaction_service.analytics.tasks", level="ERROR"
        ) as logs:
            self.assertIsNone(run_native_balance_rollup())
        self.assertIn("backfill_native_balances", logs.output[0])
        self.assertEqual(SafeNativeBalance.objects.count(), 0)

    def test_watermark_ahead_of_head_refuses(self):
        self.safe()
        self.advance_head()
        head = native_balance_head_block()
        self.initialise(at_block=head + 10)

        with self.assertLogs(
            "safe_transaction_service.analytics.tasks", level="ERROR"
        ) as logs:
            self.assertIsNone(run_native_balance_rollup())
        self.assertIn("--restart", logs.output[0])
        self.assertEqual(SafeNativeBalance.objects.count(), 0)
        self.assertEqual(
            AnalyticsWatermark.objects.get(name=NATIVE_BALANCE_WATERMARK).block_number,
            head + 10,
        )

    def test_too_many_unseeded_safes_refuses(self):
        for _ in range(3):
            self.safe()
        self.initialise()
        self.advance_head()

        with patch(
            "safe_transaction_service.analytics.tasks.NATIVE_BALANCE_MAX_SEED_PER_RUN",
            2,
        ):
            with self.assertLogs(
                "safe_transaction_service.analytics.tasks", level="ERROR"
            ) as logs:
                self.assertIsNone(run_native_balance_rollup())
        self.assertIn("backfill_native_balances", logs.output[0])
        self.assertEqual(SafeNativeBalance.objects.count(), 0)

    def test_nothing_confirmed_is_not_an_error(self):
        self.safe(block=self.block(confirmed=False))
        self.initialise()
        self.assertIsNone(run_native_balance_rollup())


class TestReadHelper(NativeBalanceRollupTestCase):
    def test_none_before_initialisation(self):
        self.assertIsNone(read_native_balance_rollup())

    def test_reports_the_watermark_it_read_at(self):
        safe = self.safe()
        self.initialise()
        self.transfer(self.block(), 11, to=safe.address)
        self.advance_head()
        summary = run_native_balance_rollup()

        rollup = read_native_balance_rollup()
        self.assertEqual(rollup["updated_to_block"], summary["watermark_to"])
        self.assertEqual(rollup["balance_wei"], 11)
        self.assertEqual(rollup["safes_with_balance"], 1)
        self.assertEqual(rollup["safe_rows"], 1)


class TestCeleryTask(NativeBalanceRollupTestCase):
    def test_task_runs_the_rollup(self):
        safe = self.safe()
        self.initialise()
        self.transfer(self.block(), 64, to=safe.address)
        self.advance_head()

        compute_native_balance_rollup_task.delay()

        self.assertEqual(self.totals(), (64, 1))
