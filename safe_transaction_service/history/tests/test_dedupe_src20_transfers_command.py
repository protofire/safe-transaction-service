from django.core.management import call_command
from django.test import TestCase

from hexbytes import HexBytes

from safe_transaction_service.tokens.models import Token

from ..indexers import Src20EventsIndexerProvider
from ..models import SRC20Transfer
from .factories import EthereumTxFactory, SRC20TransferFactory
from .mocks.mocks_src20_events_indexer import ADDR_A, ADDR_B, TOKEN_STANDARD

ZERO_KEY_HASH = b"\x00" * 32


class TestDedupeSrc20TransfersCommand(TestCase):
    def setUp(self) -> None:
        # The command uses the indexer singleton; keep it isolated per test.
        Src20EventsIndexerProvider.del_singleton()

    def tearDown(self) -> None:
        Src20EventsIndexerProvider.del_singleton()

    def _create_token(self, address, placeholder_logs):
        Token.objects.create(
            address=address,
            name="TST",
            symbol="TST",
            decimals=0,
            src20=True,
            src20_keyless_placeholder_logs_per_transfer=placeholder_logs,
        )

    def _keyless_rows(self, count):
        # Simulate the old indexer's over-counting: `count` keyless logs of ONE tx, same
        # (from, to), at consecutive log indexes.
        ethereum_tx = EthereumTxFactory()
        for log_index in range(count):
            SRC20TransferFactory(
                ethereum_tx=ethereum_tx,
                address=TOKEN_STANDARD,
                _from=ADDR_A,
                to=ADDR_B,
                log_index=log_index,
                encrypt_key_hash=ZERO_KEY_HASH,
            )

    def test_collapses_keyless_duplicates(self):
        self._create_token(TOKEN_STANDARD, 2)
        self._keyless_rows(4)  # 2 logical transfers stored as 4 rows
        self.assertEqual(SRC20Transfer.objects.count(), 4)

        call_command("dedupe_src20_transfers")

        self.assertEqual(SRC20Transfer.objects.count(), 2)
        # The kept rows are the deterministic representatives (every 2nd log: index 1 and 3)
        self.assertEqual(
            sorted(SRC20Transfer.objects.values_list("log_index", flat=True)),
            [1, 3],
        )

    def test_dry_run_deletes_nothing(self):
        self._create_token(TOKEN_STANDARD, 2)
        self._keyless_rows(2)

        call_command("dedupe_src20_transfers", "--dry-run")

        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_keep_all_token_is_not_collapsed(self):
        # A 1-placeholder token (keep-all): rows must be preserved, never under-counted.
        self._create_token(TOKEN_STANDARD, 1)
        ethereum_tx = EthereumTxFactory()
        for log_index, key_hash in enumerate(
            [HexBytes("0x" + "aa" * 32), HexBytes("0x" + "aa" * 32)]
        ):
            SRC20TransferFactory(
                ethereum_tx=ethereum_tx,
                address=TOKEN_STANDARD,
                _from=ADDR_A,
                to=ADDR_B,
                log_index=log_index,
                encrypt_key_hash=key_hash,
            )

        call_command("dedupe_src20_transfers")

        self.assertEqual(SRC20Transfer.objects.count(), 2)
