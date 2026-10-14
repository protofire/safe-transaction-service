import signal
import threading

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from safe_eth.eth.utils import fast_to_checksum_address
from safe_eth.util.util import to_0x_hex_str

from safe_transaction_service.contracts.tx_decoder import (
    CannotDecode,
    UnexpectedProblemDecoding,
)

from ...indexers.backfill import (
    BackfillCursorMixin,
    BackfillGaveUp,
    build_backfill_indexers,
    build_backfill_internal_tx_indexer,
    run_backfill,
)
from ...models import InternalTx, InternalTxType, SafeContract
from ...services import IndexService, IndexServiceProvider
from ...whitelist import get_whitelisted_safes

SIGTERM_HANDLER_NOT_INSTALLED = object()


class Command(BaseCommand):
    help = """
    Backfill the history of a whitelisted Safe: creation, Safe events (L2) or traces (L1,
    using ETHEREUM_TRACING_NODE_URL) and ERC20/721 transfers from its creation block.

    Restart all the workers with the new WHITELISTED_SAFES **before** running it, so the
    live indexers don't miss the blocks indexed meanwhile. Running it twice is safe.
    If it stops before finishing, it prints how to resume.
    """

    def add_arguments(self, parser):
        parser.add_argument("--address", required=True, help="Safe address")
        parser.add_argument(
            "--creation-tx-hash", required=True, help="Safe creation transaction hash"
        )
        parser.add_argument(
            "--block-process-limit",
            type=int,
            help="Maximum number of blocks to query each time. Default: 5000 on L2, "
            "ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT on L1",
        )
        parser.add_argument(
            "--from-block-number",
            type=int,
            help="Block to start indexing from (to resume). Creation block by default",
        )
        parser.add_argument(
            "--to-block-number",
            type=int,
            help="Last block to index. Current block by default",
        )
        parser.add_argument(
            "--skip-erc20", action="store_true", help="Don't backfill ERC20/721"
        )
        parser.add_argument(
            "--skip-events",
            action="store_true",
            help="Don't backfill Safe events (L2) or traces (L1), to resume after the "
            "ERC20 phase failed",
        )

    def handle(self, *args, **options):
        address = self._get_address(options["address"])
        l2_network = settings.ETH_L2_NETWORK
        if not l2_network and not settings.ETHEREUM_TRACING_NODE_URL:
            raise CommandError(
                "ETHEREUM_TRACING_NODE_URL is required with ETH_L2_NETWORK=False"
            )
        if address not in get_whitelisted_safes():
            raise CommandError(f"{address} is not in WHITELISTED_SAFES")
        block_process_limit = options["block_process_limit"]
        if block_process_limit is None:
            block_process_limit = (
                5_000 if l2_network else settings.ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT
            )
        if block_process_limit < 1:
            raise CommandError("--block-process-limit must be greater than 0")

        safe_events_indexer, erc20_events_indexer = build_backfill_indexers(
            block_process_limit
        )
        # Safe events (L2) or traces (L1)
        indexer = (
            safe_events_indexer
            if l2_network
            else build_backfill_internal_tx_indexer(address, block_process_limit)
        )
        ethereum_client = safe_events_indexer.ethereum_client
        creation_tx_hash = HexBytes(options["creation_tx_hash"])
        receipt = ethereum_client.get_transaction_receipt(creation_tx_hash)
        if not receipt or receipt["status"] != 1:
            raise CommandError(
                f"Creation tx {options['creation_tx_hash']} not found or failed"
            )
        if l2_network:
            safe_setup_topics = {
                topic
                for topic, events in safe_events_indexer.events_to_listen.items()
                if any(event.event_name == "SafeSetup" for event in events)
            }
            if not any(
                log["address"] == address
                and log["topics"]
                and to_0x_hex_str(log["topics"][0]) in safe_setup_topics
                for log in receipt["logs"]
            ):
                raise CommandError(
                    f"Creation tx {options['creation_tx_hash']} has no SafeSetup event for {address}"
                )
        else:
            creation_traces = indexer.ethereum_client.tracing.trace_transaction(
                creation_tx_hash
            )
            if not self._is_creation_tx(indexer, address, creation_traces):
                raise CommandError(
                    f"Creation tx {options['creation_tx_hash']} has no creation or "
                    f"setup trace for {address}"
                )

        start = (
            options["from_block_number"]
            if options["from_block_number"] is not None
            else receipt["blockNumber"]
        )
        end = (
            options["to_block_number"]
            if options["to_block_number"] is not None
            else ethereum_client.current_block_number
        )

        phase = "creation"
        resume_from = start
        skip_events = options["skip_events"]
        previous_sigterm_handler = self._install_sigterm_handler()
        try:
            if l2_network:
                self._seed_creation(safe_events_indexer, address, receipt)
            else:
                self._seed_creation_from_traces(
                    indexer, address, creation_tx_hash, creation_traces
                )

            if not options["skip_events"]:
                phase = "events"
                self._run_phase(indexer, address, start, end)
            skip_events = True
            resume_from = start

            if not options["skip_erc20"]:
                phase = "erc20"
                self._run_phase(erc20_events_indexer, address, start, end)
            resume_from = end + 1

            phase = "processing"
            index_service: IndexService = IndexServiceProvider()
            index_service.reprocess_addresses([address])
            processed = index_service.process_decoded_txs_for_safe(address)
        except BaseException as e:
            if phase in ("events", "erc20"):
                resume_from = self._get_cursor(
                    indexer if phase == "events" else erc20_events_indexer,
                    resume_from,
                )
            self.stderr.write(
                f"Backfill of {address} stopped in phase={phase}. "
                f"Resume with: --from-block-number {resume_from}"
                + (" --skip-events" if skip_events else "")
            )
            if isinstance(e, BackfillGaveUp):
                raise CommandError(str(e)) from e
            raise
        finally:
            self._restore_sigterm_handler(previous_sigterm_handler)

        self.stdout.write(
            self.style.SUCCESS(
                f"Backfilled {address} from block {start} to {end}. "
                f"Processed {processed} decoded txs"
            )
        )

    def _get_address(self, address: str) -> ChecksumAddress:
        try:
            return fast_to_checksum_address(address)
        except ValueError as e:
            raise CommandError(f"{address} is not a valid address") from e

    def _get_cursor(self, indexer: BackfillCursorMixin, default: int) -> int:
        return getattr(indexer, "_cursor", default)

    def _seed_creation(self, safe_events_indexer, address: ChecksumAddress, receipt):
        """
        Index the creation tx receipt: `ProxyCreation` is emitted by the ProxyFactory,
        so it's not found when querying only the Safe address
        """
        logs = [
            log
            for log in receipt["logs"]
            if log["topics"]
            and to_0x_hex_str(log["topics"][0]) in safe_events_indexer.events_to_listen
        ]
        safe_events_indexer.process_elements(logs)
        if not InternalTx.objects.filter(
            contract_address=address, tx_type=InternalTxType.CREATE.value
        ).exists():
            self.stderr.write(
                self.style.WARNING(
                    f"No ProxyCreation found for {address}: its ProxyFactory is not "
                    "standard. Creation info will be missing and master copy will be "
                    "0x0"
                )
            )
        if not SafeContract.objects.filter(address=address).exists():
            self.stderr.write(self.style.WARNING(f"SafeContract {address} not created"))

    def _is_creation_tx(
        self, internal_tx_indexer, address: ChecksumAddress, traces: list
    ) -> bool:
        """
        :return: `True` if the traces create the Safe or call its `setup` (a proxy
            created without initializer is set up later)
        """
        tracing = internal_tx_indexer.ethereum_client.tracing
        for trace in tracing.filter_out_errored_traces(traces):
            action = trace.get("action", {})
            if (trace.get("result") or {}).get("address") == address:
                return True
            if (
                action.get("callType") == "delegatecall"
                and action.get("from") == address
                and action.get("input")
            ):
                tx_decoder = internal_tx_indexer.tx_decoder
                try:
                    function_name, _ = tx_decoder.decode_transaction(action["input"])
                except (CannotDecode, UnexpectedProblemDecoding):
                    continue
                if function_name == "setup":
                    return True
        return False

    def _seed_creation_from_traces(
        self, internal_tx_indexer, address: ChecksumAddress, tx_hash: bytes, traces
    ):
        """
        Index the creation tx traces: it's not in the range when resuming, and for a proxy
        created without initializer no trace of the tx is `from` the Safe
        """
        internal_tx_indexer.process_elements({tx_hash: traces})
        if not InternalTx.objects.filter(
            contract_address=address, tx_type=InternalTxType.CREATE.value
        ).exists():
            self.stderr.write(
                self.style.WARNING(
                    f"No CREATE trace found for {address}: creation info will be missing"
                )
            )

    def _run_phase(self, indexer, address: ChecksumAddress, start: int, end: int):
        self.stdout.write(
            f"Backfilling {indexer.__class__.__name__} for {address} "
            f"from block {start} to {end}"
        )
        elements = run_backfill(indexer, {address}, start, end)
        self.stdout.write(
            self.style.SUCCESS(f"{indexer.__class__.__name__}: {elements} elements")
        )

    def _install_sigterm_handler(self):
        if threading.current_thread() is not threading.main_thread():
            return SIGTERM_HANDLER_NOT_INSTALLED

        def sigterm_handler(signum, frame):
            raise KeyboardInterrupt("SIGTERM")

        return signal.signal(signal.SIGTERM, sigterm_handler)

    def _restore_sigterm_handler(self, previous_handler) -> None:
        if previous_handler is not SIGTERM_HANDLER_NOT_INSTALLED:
            signal.signal(signal.SIGTERM, previous_handler)
