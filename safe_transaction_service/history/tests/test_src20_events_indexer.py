from unittest import mock
from unittest.mock import MagicMock

from django.test import TestCase

from safe_eth.eth.ethereum_client import Erc20Info, Erc20Manager

from safe_transaction_service.tokens.models import Token, TokenManager

from ..indexers import Src20EventsIndexerProvider
from ..indexers.src20_events_indexer import (
    Src20DirectoryLookupError,
    Src20EventsIndexer,
)
from ..models import SafeRelevantTransaction, SRC20Transfer
from .factories import EthereumTxFactory
from .mocks.mocks_src20_events_indexer import (
    ADDR_A,
    ADDR_B,
    ADDR_C,
    TOKEN_RECIPIENT_ONLY,
    TOKEN_STANDARD,
    ZERO_ADDRESS,
    build_src20_log,
    log_receipt_mock,
)


class TestSrc20EventsIndexer(TestCase):
    def setUp(self) -> None:
        Src20EventsIndexerProvider.del_singleton()
        self.src20_events_indexer = Src20EventsIndexerProvider()

    def tearDown(self) -> None:
        Src20EventsIndexerProvider.del_singleton()

    def _store_ethereum_tx(self):
        # `process_elements` prefetches the tx and reads the block timestamp by hash
        for log_receipt in log_receipt_mock:
            EthereumTxFactory(
                tx_hash=log_receipt["transactionHash"],
                block__block_hash=log_receipt["blockHash"],
            )

    @mock.patch.object(
        Erc20Manager,
        "get_info",
        autospec=True,
        # Attacker-controlled, over the `Token.name`/`symbol` 60-char limit. Without
        # truncation this raises `DataError` and halts the indexer permanently.
        return_value=Erc20Info(name="N" * 100, symbol="S" * 100, decimals=18),
    )
    def test_process_elements_long_token_name_does_not_halt(self, get_info: MagicMock):
        self._store_ethereum_tx()
        token_address = log_receipt_mock[0]["address"]

        # Must not raise
        processed = self.src20_events_indexer.process_elements(log_receipt_mock)

        self.assertEqual(len(processed), 1)
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        # Both `from` and `to` get a SafeRelevantTransaction row
        self.assertEqual(SafeRelevantTransaction.objects.count(), 2)

        token = Token.objects.get(address=token_address)
        self.assertTrue(token.src20)
        self.assertEqual(token.decimals, 0)
        self.assertEqual(len(token.name), 60)
        self.assertEqual(token.name, "N" * 60)

    @mock.patch.object(
        TokenManager,
        "create_src20_from_blockchain",
        side_effect=Exception("boom"),
    )
    def test_process_elements_token_registration_failure_does_not_halt(
        self, create_src20: MagicMock
    ):
        self._store_ethereum_tx()

        # Token registration blows up, but transfers are already stored and the call returns
        # (so the indexer's block cursor still advances).
        processed = self.src20_events_indexer.process_elements(log_receipt_mock)

        self.assertEqual(len(processed), 1)
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        create_src20.assert_called_once()
        # No token row created because registration failed
        self.assertEqual(Token.objects.count(), 0)

    def test_topic_to_address(self):
        # Sanity check on the indexed-topic -> address helper used for node-side filtering
        from_topic = log_receipt_mock[0]["topics"][1]
        self.assertEqual(
            self.src20_events_indexer._topic_to_address(from_topic),
            "0x22d491Bde2303f2f43325b2108D26f1eAbA1e32b",
        )

    # --- grouping / counting -------------------------------------------------------------

    TX = "0x" + "a1" * 32
    BLK = "0x" + "b2" * 32
    KH_TO = bytes.fromhex("a0" * 32)
    KH_TO_HEX = "0x" + "a0" * 32
    KH_FROM_HEX = "0x" + "f0" * 32
    PROVIDER_HEX = "0x" + "99" * 32

    def _create_token(self, address, placeholder_logs):
        return Token.objects.create(
            address=address,
            name="TST",
            symbol="TST",
            decimals=0,
            src20=True,
            src20_keyless_placeholder_logs_per_transfer=placeholder_logs,
        )

    def _run(self, logs):
        # Store an EthereumTx + block for each distinct (tx, block), then index the logs.
        seen = set()
        for log in logs:
            key = (bytes(log["transactionHash"]), bytes(log["blockHash"]))
            if key in seen:
                continue
            seen.add(key)
            EthereumTxFactory(
                tx_hash=log["transactionHash"], block__block_hash=log["blockHash"]
            )
        return self.src20_events_indexer.process_elements(logs)

    def _keyless_log(self, log_index, from_=ADDR_A, to=ADDR_B, token=TOKEN_STANDARD):
        return build_src20_log(
            tx_hash=self.TX,
            block_hash=self.BLK,
            block_number=10,
            log_index=log_index,
            token=token,
            from_=from_,
            to=to,
            key_hash=0,
            encrypted_amount=bytes([log_index]),
        )

    def test_single_keyless_transfer_yields_one_row(self):
        # 1 transfer on the standard base => 2 identical kh==0 placeholder logs => 1 row
        self._create_token(TOKEN_STANDARD, 2)
        self._run([self._keyless_log(i) for i in range(2)])
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        # Both parties still get one SafeRelevantTransaction (deduped by (tx, safe))
        self.assertEqual(SafeRelevantTransaction.objects.count(), 2)

    def test_keyless_batch_of_two_yields_two_rows(self):
        # Batch of 2 transfers A->B => 4 kh==0 logs => 2 rows
        self._create_token(TOKEN_STANDARD, 2)
        self._run([self._keyless_log(i) for i in range(4)])
        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_two_recipients_same_tx_are_distinct_groups(self):
        # A->B (2 logs) and A->C (2 logs) in one tx => 2 separate groups => 2 rows
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            self._keyless_log(0, to=ADDR_B),
            self._keyless_log(1, to=ADDR_B),
            self._keyless_log(2, to=ADDR_C),
            self._keyless_log(3, to=ADDR_C),
        ]
        self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_mint_from_zero_address_yields_one_row(self):
        # Mint (from = 0x0) to a keyless recipient => 2 kh==0 logs => 1 row
        self._create_token(TOKEN_STANDARD, 2)
        self._run([self._keyless_log(i, from_=ZERO_ADDRESS) for i in range(2)])
        self.assertEqual(SRC20Transfer.objects.count(), 1)

    def test_keyed_recipient_directory_counts_recipient_logs(self):
        # Future fully-keyed standard base: each transfer emits sender(khFrom)+recipient(khTo),
        # >=2 distinct hashes => directory path anchors on keyHash(to) => N recipient rows.
        self._create_token(TOKEN_STANDARD, 2)
        logs = []
        for i in range(2):  # 2 transfers => 4 logs
            logs.append(
                build_src20_log(
                    tx_hash=self.TX,
                    block_hash=self.BLK,
                    block_number=10,
                    log_index=2 * i,
                    token=TOKEN_STANDARD,
                    from_=ADDR_A,
                    to=ADDR_B,
                    key_hash=self.KH_FROM_HEX,
                    encrypted_amount=bytes([2 * i]),
                )
            )
            logs.append(
                build_src20_log(
                    tx_hash=self.TX,
                    block_hash=self.BLK,
                    block_number=10,
                    log_index=2 * i + 1,
                    token=TOKEN_STANDARD,
                    from_=ADDR_A,
                    to=ADDR_B,
                    key_hash=self.KH_TO_HEX,
                    encrypted_amount=bytes([2 * i + 1]),
                )
            )
        with mock.patch.object(
            Src20EventsIndexer,
            "_key_hash_at_block",
            side_effect=lambda address, block_number, cache: (
                self.KH_TO if address == ADDR_B else None
            ),
        ):
            self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_recipient_only_keyed_token_keeps_all_without_directory(self):
        # 0xDDe870-style: 1 keyed log per transfer (kh = keyHash(to)), from != to, single
        # distinct hash => each log is a separate transfer, no directory call.
        self._create_token(TOKEN_RECIPIENT_ONLY, 1)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=i,
                token=TOKEN_RECIPIENT_ONLY,
                from_=ADDR_A,
                to=ADDR_B,
                key_hash=self.KH_TO_HEX,
                encrypted_amount=bytes([i]),
            )
            for i in range(3)
        ]
        with mock.patch.object(Src20EventsIndexer, "_key_hash_at_block") as directory:
            self._run(logs)
            directory.assert_not_called()
        self.assertEqual(SRC20Transfer.objects.count(), 3)

    def test_providers_with_keyless_placeholders_divide_by_m(self):
        # Provider (kh!=0) + keyless sender/recipient placeholders for 1 transfer. Directory
        # reports both parties keyless => fall back to ÷2 on the kh==0 logs, drop the provider.
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=0,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_B,
                key_hash=self.PROVIDER_HEX,
                encrypted_amount=b"\x00",
            ),
            self._keyless_log(1),
            self._keyless_log(2),
        ]
        with mock.patch.object(
            Src20EventsIndexer, "_key_hash_at_block", return_value=None
        ):
            self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 1)

    def test_keyed_zero_match_keeps_all_rows(self):
        # Directory anchor matches no log (key rotated / archive mismatch). Never-empty guard
        # must keep every log rather than drop the transfer.
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=0,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_B,
                key_hash="0x" + "01" * 32,
                encrypted_amount=b"\x01",
            ),
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=1,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_B,
                key_hash="0x" + "02" * 32,
                encrypted_amount=b"\x02",
            ),
        ]
        with mock.patch.object(
            Src20EventsIndexer,
            "_key_hash_at_block",
            side_effect=lambda address, block_number, cache: (
                bytes.fromhex("03" * 32) if address == ADDR_B else None
            ),
        ):
            self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_directory_lookup_failure_keeps_all_no_undercount(self):
        # Mixed keyed recipient (kh=khTo) + keyless sender (kh=0), batch of 2. If the
        # directory lookup FAILS (RPC error / pruned state) it must NOT be read as "keyless"
        # and divide the 2 sender placeholders by 2 (that would under-count to 1 row). The
        # failure is uncertain => keep every log.
        self._create_token(TOKEN_STANDARD, 2)
        logs = []
        for i in range(2):  # 2 transfers => 4 logs (sender keyless, recipient keyed)
            logs.append(
                build_src20_log(
                    tx_hash=self.TX,
                    block_hash=self.BLK,
                    block_number=10,
                    log_index=2 * i,
                    token=TOKEN_STANDARD,
                    from_=ADDR_A,
                    to=ADDR_B,
                    key_hash=0,
                    encrypted_amount=bytes([2 * i]),
                )
            )
            logs.append(
                build_src20_log(
                    tx_hash=self.TX,
                    block_hash=self.BLK,
                    block_number=10,
                    log_index=2 * i + 1,
                    token=TOKEN_STANDARD,
                    from_=ADDR_A,
                    to=ADDR_B,
                    key_hash=self.KH_TO_HEX,
                    encrypted_amount=bytes([2 * i + 1]),
                )
            )
        with mock.patch.object(
            Src20EventsIndexer,
            "_key_hash_at_block",
            side_effect=Src20DirectoryLookupError("boom"),
        ):
            self._run(logs)
        # Keep-all (4), never the under-counted 1
        self.assertEqual(SRC20Transfer.objects.count(), 4)

    def test_self_transfer_keyed_divides_by_placeholder_count(self):
        # Self-transfer (from == to), keyed: sender & recipient logs both carry keyHash(self),
        # so a single transfer emits `m` identical-hash logs. 2 self-transfers => 4 logs =>
        # anchor on keyHash(self), divide by m => 2 rows.
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=i,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_A,
                key_hash=self.KH_TO_HEX,
                encrypted_amount=bytes([i]),
            )
            for i in range(4)
        ]
        with mock.patch.object(
            Src20EventsIndexer,
            "_key_hash_at_block",
            side_effect=lambda address, block_number, cache: self.KH_TO,
        ):
            self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 2)

    def test_self_transfer_keyless_divides_by_placeholder_count(self):
        # Self-transfer, keyless: 2 kh==0 placeholders => 1 row (no directory call).
        self._create_token(TOKEN_STANDARD, 2)
        with mock.patch.object(Src20EventsIndexer, "_key_hash_at_block") as directory:
            self._run([self._keyless_log(i, from_=ADDR_A, to=ADDR_A) for i in range(2)])
            directory.assert_not_called()
        self.assertEqual(SRC20Transfer.objects.count(), 1)

    def test_self_transfer_with_providers_falls_back_to_divide(self):
        # Self-transfer, both keyless, plus a provider (kh!=0). Directory confirms keyless
        # => count keyless placeholders (÷2), drop the provider => 1 row.
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=0,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_A,
                key_hash=self.PROVIDER_HEX,
                encrypted_amount=b"\x00",
            ),
            self._keyless_log(1, from_=ADDR_A, to=ADDR_A),
            self._keyless_log(2, from_=ADDR_A, to=ADDR_A),
        ]
        with mock.patch.object(
            Src20EventsIndexer, "_key_hash_at_block", return_value=None
        ):
            self._run(logs)
        self.assertEqual(SRC20Transfer.objects.count(), 1)

    def test_provider_with_non_divisible_keyless_drops_provider(self):
        # Provider (kh!=0) + a single keyless placeholder (non-divisible by m=2). Keep-all
        # must keep only the keyless placeholder, never the provider log.
        self._create_token(TOKEN_STANDARD, 2)
        logs = [
            build_src20_log(
                tx_hash=self.TX,
                block_hash=self.BLK,
                block_number=10,
                log_index=0,
                token=TOKEN_STANDARD,
                from_=ADDR_A,
                to=ADDR_B,
                key_hash=self.PROVIDER_HEX,
                encrypted_amount=b"\x00",
            ),
            self._keyless_log(1),
        ]
        with mock.patch.object(
            Src20EventsIndexer, "_key_hash_at_block", return_value=None
        ):
            self._run(logs)
        # Only the keyless placeholder is stored, not the provider log
        self.assertEqual(SRC20Transfer.objects.count(), 1)
        self.assertEqual(
            list(SRC20Transfer.objects.values_list("log_index", flat=True)), [1]
        )
