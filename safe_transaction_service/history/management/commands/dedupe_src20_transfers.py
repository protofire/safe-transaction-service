from itertools import groupby

from django.core.management.base import BaseCommand
from django.db import transaction

from eth_typing import ChecksumAddress

from ....tokens.constants import get_src20_keyless_placeholder_logs
from ....tokens.models import Token
from ...indexers import Src20EventsIndexerProvider
from ...models import SRC20Transfer


class Command(BaseCommand):
    help = (
        "Collapse historical over-counted SRC20 transfers. The original indexer stored one "
        "row per `Transfer` log, but a single transfer emits several logs (sender + recipient "
        "placeholders, plus one per provider). This re-applies the indexer's counting rule to "
        "the already-stored rows — grouping by (tx, token, from, to) and reading each row's "
        "stored `log_index`/`encrypt_key_hash`/`block_number` — and deletes the surplus rows, "
        "keeping the same deterministic representatives the indexer now produces.\n\n"
        "It does NOT touch SafeRelevantTransaction (already one row per (tx, safe)). A cursor "
        "reset is unnecessary: future blocks are handled by the fixed indexer, and re-indexing "
        "a deduped range would re-select the same kept representatives (no-op under "
        "`ignore_conflicts`). Run after the data migration that backfills "
        "`Token.src20_keyless_placeholder_logs_per_transfer`."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--token-address",
            nargs="+",
            help="Limit to these SRC20 token addresses (default: all SRC20 tokens)",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            default=False,
            help="Report how many rows would be deleted without deleting them",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        token_addresses: list[ChecksumAddress] | None = options["token_address"]

        indexer = Src20EventsIndexerProvider()
        # `keyHash(address, block)` lookups are reused across groups in the same run.
        key_hash_cache: dict = {}

        if token_addresses is None:
            token_addresses = list(
                Token.objects.filter(src20=True).values_list("address", flat=True)
            )

        total_deleted = 0
        for token_address in token_addresses:
            placeholder_logs = self._keyless_placeholder_logs(token_address)
            deleted = self._dedupe_token(
                indexer, token_address, placeholder_logs, key_hash_cache, dry_run
            )
            total_deleted += deleted
            if deleted:
                self.stdout.write(
                    self.style.SUCCESS(
                        f"{'Would delete' if dry_run else 'Deleted'} {deleted} surplus "
                        f"SRC20 rows for token={token_address}"
                    )
                )

        self.stdout.write(
            self.style.SUCCESS(
                f"{'Would delete' if dry_run else 'Deleted'} {total_deleted} surplus "
                f"SRC20Transfer rows total"
            )
        )

    @staticmethod
    def _keyless_placeholder_logs(token_address: ChecksumAddress) -> int:
        token = Token.objects.filter(address=token_address).first()
        if token is not None:
            return token.src20_keyless_placeholder_logs_per_transfer
        return get_src20_keyless_placeholder_logs(token_address)

    def _dedupe_token(
        self,
        indexer,
        token_address: ChecksumAddress,
        placeholder_logs: int,
        key_hash_cache: dict,
        dry_run: bool,
    ) -> int:
        rows = (
            SRC20Transfer.objects.filter(address=token_address)
            .order_by("ethereum_tx_id", "_from", "to", "log_index")
            .values(
                "id",
                "ethereum_tx_id",
                "_from",
                "to",
                "log_index",
                "block_number",
                "encrypt_key_hash",
            )
        )

        ids_to_delete: list[int] = []
        for (_tx_hash, from_, to), group in groupby(
            rows, key=lambda r: (r["ethereum_tx_id"], r["_from"], r["to"])
        ):
            group_rows = list(group)
            if len(group_rows) <= 1:
                continue  # Nothing to collapse

            # Reconstruct the minimal `EventData` shape the selector reads.
            pseudo_logs = [
                {
                    "logIndex": r["log_index"],
                    "blockNumber": r["block_number"],
                    "transactionHash": r["ethereum_tx_id"],
                    "address": token_address,
                    "args": {
                        "from": from_,
                        "to": to,
                        "encryptKeyHash": r["encrypt_key_hash"] or b"",
                    },
                }
                for r in group_rows
            ]
            representatives = indexer._select_representatives(
                pseudo_logs, placeholder_logs, from_, to, key_hash_cache
            )
            kept_log_indexes = {log["logIndex"] for log in representatives}
            ids_to_delete.extend(
                r["id"] for r in group_rows if r["log_index"] not in kept_log_indexes
            )

        if ids_to_delete and not dry_run:
            with transaction.atomic():
                # Delete in batches to keep the IN clause and locks bounded.
                for start in range(0, len(ids_to_delete), 5_000):
                    SRC20Transfer.objects.filter(
                        id__in=ids_to_delete[start : start + 5_000]
                    ).delete()

        return len(ids_to_delete)
