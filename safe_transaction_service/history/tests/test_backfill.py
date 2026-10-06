import os
import signal
from collections.abc import Callable
from io import StringIO
from itertools import pairwise
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from celery.exceptions import SoftTimeLimitExceeded
from eth_account import Account
from hexbytes import HexBytes
from requests import Timeout
from web3.exceptions import Web3RPCError

from ..indexers.backfill import (
    BACKFILL_RETRYABLE_ERRORS,
    BackfillCursorMixin,
    BackfillErc20EventsIndexer,
    BackfillGaveUp,
    BackfillSafeEventsIndexer,
    build_backfill_indexers,
    run_backfill,
)
from ..indexers.ethereum_indexer import FindRelevantElementsException
from ..models import IndexingStatus
from ..services import IndexService, IndexServiceProvider

START = 100
END = 150


class WindowRecorder:
    """
    Replaces `find_relevant_elements`, recording every `(from, to)` window and raising
    the exception returned by `fail(from_block_number, to_block_number, call_number)`.
    Like the real node query, it runs inside `auto_adjust_block_limit`
    """

    indexer = None

    def __init__(
        self, fail: Callable[[int, int, int], BaseException | None] | None = None
    ):
        self.fail = fail
        self.windows: list[tuple[int, int]] = []
        self.successful_windows: list[tuple[int, int]] = []

    def __call__(self, addresses, from_block_number, to_block_number, **kwargs):
        self.windows.append((from_block_number, to_block_number))
        with self.indexer.auto_adjust_block_limit(from_block_number, to_block_number):
            if self.fail and (
                exception := self.fail(
                    from_block_number, to_block_number, len(self.windows)
                )
            ):
                raise exception
        self.successful_windows.append((from_block_number, to_block_number))
        return []


class TestBackfill(TestCase):
    def setUp(self):
        # `EthereumIndexer.__init__` replaces the client of the `IndexService` singleton
        index_service = IndexServiceProvider()
        self.addCleanup(
            setattr, index_service, "ethereum_client", index_service.ethereum_client
        )
        self.address = Account.create().address
        # Every window looks fast, so `auto_adjust_block_limit` doubles the limit
        time_patcher = mock.patch(
            "safe_transaction_service.history.indexers.ethereum_indexer.time.time",
            return_value=0,
        )
        time_patcher.start()
        self.addCleanup(time_patcher.stop)

    def run_recorded(
        self, indexer, recorder: WindowRecorder, start: int = START, end: int = END
    ) -> int:
        recorder.indexer = indexer
        with mock.patch.object(indexer, "find_relevant_elements", side_effect=recorder):
            return run_backfill(indexer, {self.address}, start, end)

    def assert_coverage(self, windows: list[tuple[int, int]], start: int, end: int):
        """
        Windows are contiguous, don't overlap and cover exactly `[start, end]`
        """
        self.assertTrue(windows)
        self.assertEqual(windows[0][0], start)
        for (_, previous_to), (from_, to) in pairwise(windows):
            self.assertEqual(from_, previous_to + 1)
            self.assertLessEqual(from_, to)
        self.assertEqual(windows[-1][1], end)

    def window_sizes(self, windows: list[tuple[int, int]]) -> list[int]:
        return [to - from_ + 1 for from_, to in windows]

    def test_run_backfill(self):
        safe_events_indexer, _ = build_backfill_indexers(8)
        recorder = WindowRecorder()
        self.run_recorded(safe_events_indexer, recorder)
        self.assert_coverage(recorder.successful_windows, START, END)
        self.assertLessEqual(max(self.window_sizes(recorder.windows)), 8)
        self.assertEqual(safe_events_indexer.cursor, END + 1)

    def test_rejection_is_retried(self):
        exceptions = [
            FindRelevantElementsException("Request error"),
            SoftTimeLimitExceeded(),
            Timeout(),
            ValueError("Possible reorg"),
            Web3RPCError("max-block-range exceeded"),
        ]
        self.assertEqual(
            {exception.__class__ for exception in exceptions},
            set(BACKFILL_RETRYABLE_ERRORS),
        )
        for exception in exceptions:
            with self.subTest(exception=exception.__class__.__name__):
                safe_events_indexer, _ = build_backfill_indexers(8)
                recorder = WindowRecorder(
                    lambda from_, to, call_number, exception=exception: (
                        exception if call_number == 3 else None
                    )
                )
                self.run_recorded(safe_events_indexer, recorder)
                failed_window, retried_window = recorder.windows[2], recorder.windows[3]
                # Same `from`, and the window shrinks to 1
                self.assertEqual(retried_window[0], failed_window[0])
                self.assertEqual(self.window_sizes([retried_window]), [1])
                self.assert_coverage(recorder.successful_windows, START, END)

    def test_regrow_after_rejection(self):
        safe_events_indexer, _ = build_backfill_indexers(16)
        recorder = WindowRecorder(
            lambda from_, to, call_number: (
                Web3RPCError("rejected") if call_number == 1 else None
            )
        )
        self.run_recorded(safe_events_indexer, recorder)
        sizes = self.window_sizes(recorder.windows)
        self.assertEqual(sizes[:6], [16, 1, 2, 4, 8, 16])
        self.assertLessEqual(max(sizes), 16)
        self.assert_coverage(recorder.successful_windows, START, END)

    def test_give_up_and_resume(self):
        safe_events_indexer, _ = build_backfill_indexers(8)

        # Bad region from block 120: every window including it fails
        recorder = WindowRecorder(
            lambda from_, to, call_number: (
                Web3RPCError("rejected") if to >= 120 else None
            )
        )
        with self.assertRaises(BackfillGaveUp) as context:
            self.run_recorded(safe_events_indexer, recorder)
        cursor = context.exception.cursor
        self.assertEqual(cursor, 120)
        self.assertEqual(recorder.windows[-3:], [(120, 120)] * 3)

        # Resume from the cursor
        resume_recorder = WindowRecorder()
        self.run_recorded(safe_events_indexer, resume_recorder, start=cursor)
        self.assert_coverage(
            recorder.successful_windows + resume_recorder.successful_windows,
            START,
            END,
        )

    def test_backfill_cursor_mixin_order(self):
        for indexer_class in (BackfillSafeEventsIndexer, BackfillErc20EventsIndexer):
            with self.subTest(indexer_class=indexer_class.__name__):
                self.assertIs(
                    indexer_class.get_from_block_number,
                    BackfillCursorMixin.get_from_block_number,
                )
                self.assertIs(
                    indexer_class.update_monitored_addresses,
                    BackfillCursorMixin.update_monitored_addresses,
                )

        # The ERC20 indexer must not use or modify `IndexingStatus`
        # Sentinel below `start`: with swapped bases the first window would start
        # from it, and it would be updated
        start, end = 2_000, 2_050
        sentinel_block_number = start - 1_000
        IndexingStatus.objects.set_erc20_721_indexing_status(sentinel_block_number)
        _, erc20_events_indexer = build_backfill_indexers(8)
        recorder = WindowRecorder()
        self.run_recorded(erc20_events_indexer, recorder, start=start, end=end)
        self.assertEqual(recorder.windows[0][0], start)
        self.assert_coverage(recorder.successful_windows, start, end)
        self.assertEqual(
            IndexingStatus.objects.get_erc20_721_indexing_status().block_number,
            sentinel_block_number,
        )

    def test_run_backfill_edge_cases(self):
        safe_events_indexer, _ = build_backfill_indexers(8)
        recorder = WindowRecorder()
        self.run_recorded(safe_events_indexer, recorder, start=START, end=START)
        self.assertEqual(recorder.windows, [(START, START)])

        recorder = WindowRecorder()
        self.assertEqual(
            self.run_recorded(safe_events_indexer, recorder, start=END, end=START), 0
        )
        self.assertEqual(recorder.windows, [])


class TestBackfillCommandExit(TestCase):
    """
    Every non-successful exit of `backfill_whitelisted_safe` prints how to resume
    """

    TO_BLOCK_NUMBER = 1_000

    def setUp(self):
        index_service = IndexServiceProvider()
        self.addCleanup(
            setattr, index_service, "ethereum_client", index_service.ethereum_client
        )
        self.address = Account.create().address
        safe_events_indexer, _ = build_backfill_indexers(8)
        safe_setup_topic = next(
            topic
            for topic, events in safe_events_indexer.events_to_listen.items()
            if any(event.event_name == "SafeSetup" for event in events)
        )
        self.receipt = {
            "status": 1,
            "blockNumber": START,
            "logs": [{"address": self.address, "topics": [HexBytes(safe_setup_topic)]}],
        }

    def call_backfill(self, run_backfill_side_effect, stderr: StringIO, **kwargs):
        with (
            override_settings(
                WHITELISTED_SAFES=frozenset({self.address}), ETH_L2_NETWORK=True
            ),
            mock.patch(
                "safe_transaction_service.history.management.commands."
                "backfill_whitelisted_safe.Command._seed_creation"
            ),
            mock.patch(
                "safe_transaction_service.history.management.commands."
                "backfill_whitelisted_safe.run_backfill",
                side_effect=run_backfill_side_effect,
            ),
            mock.patch(
                "safe_eth.eth.EthereumClient.get_transaction_receipt",
                return_value=self.receipt,
            ),
        ):
            call_command(
                "backfill_whitelisted_safe",
                f"--address={self.address}",
                f"--creation-tx-hash=0x{'12' * 32}",
                f"--to-block-number={self.TO_BLOCK_NUMBER}",
                stdout=StringIO(),
                stderr=stderr,
                **kwargs,
            )

    @staticmethod
    def fail_in_phase(phase_indexer_class, exception_factory, cursor: int):
        """
        :return: `run_backfill` replacement failing for `phase_indexer_class` after
            moving its cursor to `cursor`
        """

        def side_effect(indexer, addresses, start, end):
            if isinstance(indexer, phase_indexer_class):
                indexer.set_cursor(cursor)
                raise exception_factory(cursor)
            return 0

        return side_effect

    def test_resume_printed_on_every_exit(self):
        def sigterm(cursor):
            os.kill(os.getpid(), signal.SIGTERM)
            return RuntimeError("SIGTERM was not converted")

        for exception_factory, expected_exception in (
            (lambda cursor: BackfillGaveUp(cursor, Web3RPCError("x")), CommandError),
            (lambda cursor: RuntimeError("unexpected"), RuntimeError),
            (lambda cursor: KeyboardInterrupt(), KeyboardInterrupt),
            (sigterm, KeyboardInterrupt),
        ):
            for phase_indexer_class, cursor, skip_events in (
                (BackfillSafeEventsIndexer, 123, ""),
                (BackfillErc20EventsIndexer, 456, " --skip-events"),
            ):
                with self.subTest(
                    expected_exception=expected_exception.__name__,
                    phase=phase_indexer_class.__name__,
                ):
                    previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
                    stderr = StringIO()
                    with self.assertRaises(expected_exception) as context:
                        self.call_backfill(
                            self.fail_in_phase(
                                phase_indexer_class, exception_factory, cursor
                            ),
                            stderr,
                        )
                    self.assertIn(
                        f"Resume with: --from-block-number {cursor}{skip_events}\n",
                        stderr.getvalue(),
                    )
                    if isinstance(context.exception, CommandError):
                        self.assertEqual(context.exception.returncode, 1)
                    self.assertIs(
                        signal.getsignal(signal.SIGTERM), previous_sigterm_handler
                    )

    def test_resume_printed_on_processing_error(self):
        stderr = StringIO()
        with (
            mock.patch.object(
                IndexService, "reprocess_addresses", side_effect=RuntimeError("db")
            ),
            self.assertRaises(RuntimeError),
        ):
            self.call_backfill(lambda *args: 0, stderr)
        self.assertIn(
            f"Resume with: --from-block-number {self.TO_BLOCK_NUMBER + 1} --skip-events",
            stderr.getvalue(),
        )

    def test_no_resume_on_success(self):
        stderr = StringIO()
        with (
            mock.patch.object(IndexService, "reprocess_addresses"),
            mock.patch.object(
                IndexService, "process_decoded_txs_for_safe", return_value=0
            ),
        ):
            self.call_backfill(lambda *args: 0, stderr)
        self.assertNotIn("Resume with", stderr.getvalue())
