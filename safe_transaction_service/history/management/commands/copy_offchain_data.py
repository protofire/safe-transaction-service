"""
Copies the data that only exists in the database of another transaction service (the "source") into
the database of this one (the "destination"), after the destination was created from a backup of the
source and kept indexing the chain on its own.

Only data that cannot be rebuilt from the chain is copied:

- multisig-transactions: queued (not executed) transactions, and ``proposer``, ``proposed_by_delegate``,
  ``origin`` and ``trusted`` of the ones the destination indexed from the chain
- multisig-confirmations: off-chain confirmations (signatures not coming from an Ethereum transaction)
- delegates, safe-messages (with their confirmations), account-abstraction (user/safe operations)
- tokens (curated flags and logos), token-lists, contracts (and their ABIs)

On-chain tables (blocks, transactions, traces, transfers, Safe statuses...) and indexing cursors are never
touched. The command is idempotent, so it can be run several times (e.g. a dry run, a first copy and a
final copy once writes to the source are frozen).

Rows are written with ``bulk_create``/``bulk_update`` on the base managers, so no ``post_save`` signal runs.
``DISABLE_SERVICE_EVENTS`` is forced while the command runs, so no webhook/event is sent either (not even
for the rows that are deleted, or in a dry run). The cached tx list views of the Safes whose queued
transactions or confirmations changed are removed once the changes are committed. The source database is
opened read-only, with a single REPEATABLE READ snapshot.

Example::

    python manage.py copy_offchain_data --since 2026-10-12T10:00:00Z \\
        --source-url psql://user:pass@old-db:5432/safe_tx            # dry run
    python manage.py copy_offchain_data --since ... --source-url ... --apply
"""

import argparse
import copy
import json
import os
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import islice
from typing import Any
from urllib.parse import urlsplit

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, connections, transaction
from django.db.models import F, Model, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

import environ
from hexbytes import HexBytes
from psycopg import IsolationLevel
from safe_eth.eth.utils import fast_keccak

from safe_transaction_service.account_abstraction.models import (
    SafeOperation,
    SafeOperationConfirmation,
    UserOperation,
    UserOperationReceipt,
)
from safe_transaction_service.contracts.models import Contract, ContractAbi
from safe_transaction_service.history.cache import (
    CacheSafeTxsView,
    remove_cache_view_for_addresses,
)
from safe_transaction_service.history.models import (
    Chain,
    EthereumTx,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeContractDelegate,
)
from safe_transaction_service.safe_messages.models import (
    SafeMessage,
    SafeMessageConfirmation,
)
from safe_transaction_service.tokens.models import Token, TokenList

SOURCE_ALIAS = "offchain_source"
MASS_DELETE_RATIO = 0.2  # Deleting more than this fraction of the destination rows needs --allow-mass-delete
MASS_DELETE_MIN_ROWS = 5  # ...unless there are only a few
MIGRATION_APPS = [
    "history",
    "safe_messages",
    "tokens",
    "contracts",
    "account_abstraction",
]
SECTIONS = [  # Dependency order
    "contracts",
    "tokens",
    "token-lists",
    "delegates",
    "multisig-transactions",
    "multisig-confirmations",
    "safe-messages",
    "account-abstraction",
]


def _hb(value: Any) -> bytes | None:
    """Normalize hashes (bytes, memoryview, hex string, HexBytes) so they can be compared"""
    if value is None:
        return None
    if isinstance(value, memoryview):
        value = bytes(value)
    return bytes(HexBytes(value))


def _addr(value: str | None) -> str | None:
    return value.lower() if value else value


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value} must be 1 or greater")
    return number


def _non_negative_int(value: str) -> int:
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError(f"{value} must be 0 or greater")
    return number


def _chunked(iterable: Iterable, size: int) -> Iterator[list]:
    iterator = iter(iterable)
    while chunk := list(islice(iterator, size)):
        yield chunk


class Stats:
    def __init__(self) -> None:
        self.counters: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self.warnings: list[str] = []

    def add(self, section: str, key: str, amount: int = 1) -> None:
        if amount:
            self.counters[section][key] += amount

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < 200:
            self.warnings.append(message)


class Command(BaseCommand):
    help = (
        "Copies off-chain data (queued multisig txs, off-chain confirmations, delegates, safe messages, "
        "4337 operations, token and contract curation) from another transaction service database"
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--source-url",
            default=None,
            help="Database URL of the service we copy from (e.g. psql://user:pass@host:5432/db). "
            "Defaults to the LEGACY_DATABASE_URL environment variable",
        )
        parser.add_argument(
            "--since",
            required=True,
            help="Moment the destination database backup was taken (ISO 8601, UTC if no timezone). "
            "Only rows created/modified after it are looked at (except tables compared completely: "
            "delegates, token lists, contracts and curated tokens)",
        )
        parser.add_argument(
            "--margin-minutes",
            type=_non_negative_int,
            default=60,
            help="Safety margin subtracted from --since (default: 60)",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it everything runs inside a transaction that is rolled back "
            "(dry run) and only the report is shown",
        )
        parser.add_argument(
            "--only",
            nargs="+",
            choices=SECTIONS,
            default=None,
            help="Only run these sections (default: all)",
        )
        parser.add_argument(
            "--all-tokens",
            action="store_true",
            help="Also copy source tokens that are not curated (no spam/trusted/events_bugged/logo/copy_price) "
            "and are missing in the destination. By default the destination indexes them again by itself",
        )
        parser.add_argument(
            "--no-delete",
            action="store_true",
            help="Don't mirror deletions made in the source after the backup (queued multisig txs and delegates)",
        )
        parser.add_argument(
            "--allow-mass-delete",
            action="store_true",
            help="Allow deleting more than 20%% (and at least 5 rows) of the destination queued txs/delegates "
            "(safety guard against pointing to the wrong database)",
        )
        parser.add_argument("--batch-size", type=_positive_int, default=500)
        parser.add_argument(
            "--ignore-migration-mismatch",
            action="store_true",
            help="Don't abort if source has migrations that destination does not have",
        )

    # ------------------------------------------------------------------ entry point
    def handle(self, *args, **options):
        self.batch: int = options["batch_size"]
        self.apply: bool = options["apply"]
        self.sections: list[str] = options["only"] or SECTIONS
        self.all_tokens: bool = options["all_tokens"]
        self.sync_deletes: bool = not options["no_delete"]
        self.allow_mass_delete: bool = options["allow_mass_delete"]
        self.stats = Stats()
        self._abi_map: dict[int, int | None] = {}
        self._confirmation_hashes: set[bytes] | None = None
        self._columns_cache: dict[str, set[str]] = {}
        self._touched_safes: set[str] = set()

        url = options["source_url"] or os.environ.get("LEGACY_DATABASE_URL")
        if not url:
            raise CommandError("Provide --source-url or LEGACY_DATABASE_URL")

        self.since = self._parse_since(options["since"])
        self.since_eff = self.since - timedelta(minutes=options["margin_minutes"])

        previous_disable_events = settings.DISABLE_SERVICE_EVENTS
        try:
            # Deleting queued txs or delegates would send webhooks. This process must never send events
            settings.DISABLE_SERVICE_EVENTS = True
            self._configure_source(url)
            self._preflight(options["ignore_migration_mismatch"])
            self.stdout.write(
                f"Mode: {'APPLY' if self.apply else 'DRY RUN (nothing is written)'} - "
                f"looking at rows created/modified since {self.since_eff.isoformat()}"
            )
            # One snapshot of the source for the whole run
            with transaction.atomic(using=SOURCE_ALIAS):
                if self.apply:
                    self._run_sections()
                else:
                    with transaction.atomic():
                        self._run_sections()
                        transaction.set_rollback(True)
        finally:
            settings.DISABLE_SERVICE_EVENTS = previous_disable_events
            self._close_source()

        self._report()

    def _parse_since(self, value: str) -> datetime:
        try:
            parsed = parse_datetime(value)
            if parsed is None and (date := parse_date(value)):
                parsed = datetime(date.year, date.month, date.day)
        except ValueError:  # Well formatted but not valid (e.g. month 13)
            parsed = None
        if parsed is None:
            raise CommandError(f"Cannot parse --since={value}, use ISO 8601")
        if parsed.tzinfo is None:
            self.stdout.write("--since has no timezone, assuming UTC")
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    def _run_sections(self) -> None:
        runners = {
            "contracts": self._sync_contracts,
            "tokens": self._sync_tokens,
            "token-lists": self._sync_token_lists,
            "delegates": self._sync_delegates,
            "multisig-transactions": self._sync_multisig_transactions,
            "multisig-confirmations": self._sync_multisig_confirmations,
            "safe-messages": self._sync_safe_messages,
            "account-abstraction": self._sync_account_abstraction,
        }
        for section in SECTIONS:
            if section in self.sections:
                self.stdout.write(f"-> {section}")
                # Commit per section, indexers keep running on the destination meanwhile
                with transaction.atomic():
                    runners[section]()
                if self.apply:
                    self._remove_cached_views()

    # ------------------------------------------------------------------ connections
    def _configure_source(self, url: str) -> None:
        parsed = environ.Env.db_url_config(url)
        config = copy.deepcopy(connections[DEFAULT_DB_ALIAS].settings_dict)
        for key in ("NAME", "USER", "PASSWORD", "HOST", "PORT"):
            config[key] = parsed.get(key, "") or ""
        options = dict(parsed.get("OPTIONS", {}) or {})
        extra = "-c statement_timeout=0 -c default_transaction_read_only=on"
        options["options"] = f"{options.get('options', '')} {extra}".strip()
        options["isolation_level"] = IsolationLevel.REPEATABLE_READ
        config["OPTIONS"] = options  # No connection pool
        config["CONN_MAX_AGE"] = 0
        config["ATOMIC_REQUESTS"] = False
        config["AUTOCOMMIT"] = True
        try:  # A connection created with older settings must not be reused
            connections[SOURCE_ALIAS].close()
            del connections[SOURCE_ALIAS]
        except Exception:  # noqa: BLE001
            pass
        connections.settings[SOURCE_ALIAS] = config
        netloc = urlsplit(url)
        # Never print the password
        self.source_display = f"{netloc.hostname}:{netloc.port or ''}/{config['NAME']} (user {config['USER']})"

    def _close_source(self) -> None:
        try:
            connections[SOURCE_ALIAS].close()
            del connections[SOURCE_ALIAS]
        except Exception:  # noqa: BLE001
            pass
        connections.settings.pop(SOURCE_ALIAS, None)

    def _preflight(self, ignore_migration_mismatch: bool) -> None:
        source, dest = connections[SOURCE_ALIAS], connections[DEFAULT_DB_ALIAS]

        def identity(connection) -> tuple:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), pg_postmaster_start_time(), current_schema(), "
                    "current_setting('transaction_read_only')"
                )
                return cursor.fetchone()

        try:
            source_id = identity(source)
        except Exception as exc:
            raise CommandError(
                f"Cannot connect to source {self.source_display}: {exc}"
            ) from exc
        dest_id = identity(dest)
        self.stdout.write(
            f"Source: {self.source_display} - Destination: "
            f"{dest.settings_dict['HOST']}/{dest.settings_dict['NAME']}"
        )
        if source_id[:3] == dest_id[:3]:
            raise CommandError("Source and destination are the same database")
        if source_id[3] != "on":
            raise CommandError("Source connection must be read-only")

        source_chains = set(self._src(Chain).values_list("chain_id", flat=True))
        dest_chains = set(Chain.objects.values_list("chain_id", flat=True))
        if not source_chains or not dest_chains:
            raise CommandError(
                "Cannot check that both databases belong to the same chain, Chain table is empty in "
                f"{'source' if not source_chains else 'destination'}"
            )
        if source_chains != dest_chains:
            raise CommandError(
                f"Chain id mismatch: source={source_chains} destination={dest_chains}"
            )

        def migrations(connection) -> set[tuple[str, str]]:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT app, name FROM django_migrations WHERE app = ANY(%s)",
                    [MIGRATION_APPS],
                )
                return set(cursor.fetchall())

        source_migrations, dest_migrations = migrations(source), migrations(dest)
        if only_source := source_migrations - dest_migrations:
            message = f"Source has migrations not applied in destination: {sorted(only_source)[:5]}"
            if not ignore_migration_mismatch:
                raise CommandError(
                    message + " (use --ignore-migration-mismatch to continue)"
                )
            self.stats.warn(message)
        if only_dest := dest_migrations - source_migrations:
            self.stats.warn(
                f"Destination has {len(only_dest)} migrations not in source, new columns get default values"
            )

    # ------------------------------------------------------------------ helpers
    def _src(self, model: type[Model]):
        return model._base_manager.using(SOURCE_ALIAS)

    def _dst(self, model: type[Model]):
        return model._base_manager.all()

    def _source_columns(self, model: type[Model]) -> set[str]:
        table = model._meta.db_table
        if table not in self._columns_cache:
            connection = connections[SOURCE_ALIAS]
            with connection.cursor() as cursor:
                if table in connection.introspection.table_names(cursor):
                    self._columns_cache[table] = {
                        column.name
                        for column in connection.introspection.get_table_description(
                            cursor, table
                        )
                    }
                else:
                    self._columns_cache[table] = set()
        return self._columns_cache[table]

    def _available(self, model: type[Model], section: str) -> bool:
        if not self._source_columns(model):
            self.stats.warn(
                f"[{section}] table {model._meta.db_table} does not exist in source, skipped"
            )
            return False
        if (
            model._meta.db_table
            not in connections[DEFAULT_DB_ALIAS].introspection.table_names()
        ):
            self.stats.warn(
                f"[{section}] table {model._meta.db_table} does not exist in destination, skipped"
            )
            return False
        return True

    def _fields(
        self, model: type[Model], exclude: tuple[str, ...] = ("id",)
    ) -> list[str]:
        """Attnames of the fields present in the source table (a missing column gets its default)"""
        columns = self._source_columns(model)
        return [
            field.attname
            for field in model._meta.concrete_fields
            if field.attname not in exclude and field.column in columns
        ]

    def _insert(self, model: type[Model], objs: list[Model], section: str) -> None:
        for chunk in _chunked(objs, self.batch):
            model._base_manager.bulk_create(chunk, ignore_conflicts=True)
        self.stats.add(section, "inserted", len(objs))

    def _update(
        self,
        model: type[Model],
        updates: list[tuple[Model, list[str]]],
        section: str,
    ) -> None:
        """
        :param updates: Objects with the names of the fields that changed in each of them. Objects are
            grouped by changed fields, so a field is never written for an object that did not change it
            (with the value read before, overwriting what the indexers could have written meanwhile)
        """
        groups: dict[tuple[str, ...], list[Model]] = defaultdict(list)
        for obj, changed in updates:
            groups[tuple(sorted(changed))].append(obj)
        for fields, objs in groups.items():
            model._base_manager.bulk_update(objs, list(fields), batch_size=self.batch)
        self.stats.add(section, "updated", len(updates))

    def _touch_safes(self, safes: Iterable[str | None]) -> None:
        """Remember Safes whose cached tx list views must be removed once the changes are committed"""
        self._touched_safes.update(safe for safe in safes if safe)

    def _remove_cached_views(self) -> None:
        """
        ``bulk_*`` and ``DISABLE_SERVICE_EVENTS`` skip the signal that removes the cache of the views listing
        the multisig transactions of a Safe, so it must be done here
        """
        safes, self._touched_safes = self._touched_safes, set()
        if not safes or not settings.CACHE_VIEW_DEFAULT_TIMEOUT:
            return
        try:
            remove_cache_view_for_addresses(
                CacheSafeTxsView.LIST_MULTISIGTRANSACTIONS_VIEW_CACHE_KEY, list(safes)
            )
        except Exception as exc:  # noqa: BLE001
            self.stats.warn(
                f"Cannot remove cached views of {len(safes)} Safes, they expire by themselves: {exc}"
            )

    def _guard_deletes(self, section: str, to_delete: int, total: int) -> None:
        if (
            to_delete >= MASS_DELETE_MIN_ROWS
            and to_delete > int(total * MASS_DELETE_RATIO)
            and not self.allow_mass_delete
        ):
            raise CommandError(
                f"[{section}] would delete {to_delete} of {total} rows in destination. This looks wrong "
                f"(wrong source database?). Use --allow-mass-delete if that is expected"
            )

    # ------------------------------------------------------------------ contracts
    def _map_abi(self, source_abi_id: int | None) -> int | None:
        """
        ContractAbi ids differ between databases, ABIs are matched by ``abi_hash`` (recomputed only when it is
        empty in old rows)
        """
        if source_abi_id is None:
            return None
        if source_abi_id in self._abi_map:
            return self._abi_map[source_abi_id]
        row = (
            self._src(ContractAbi)
            .filter(pk=source_abi_id)
            .values("abi", "description", "relevance", "abi_hash")
            .first()
        )
        mapped = None
        if row is None:
            self.stats.warn(f"ContractAbi id={source_abi_id} not found in source")
        else:
            abi = json.loads(row["abi"]) if isinstance(row["abi"], str) else row["abi"]
            abi_hash = row["abi_hash"] or fast_keccak(
                json.dumps(abi, separators=(",", ":")).encode()
            )
            contract_abi = ContractAbi._base_manager.filter(abi_hash=abi_hash).first()
            if contract_abi is None:
                # `save` computes the hash and keeps it unique
                contract_abi = ContractAbi(
                    abi=abi,
                    description=row["description"] or "",
                    relevance=row["relevance"],
                )
                contract_abi.save()
                self.stats.add("contracts", "abis inserted")
            mapped = contract_abi.pk
        self._abi_map[source_abi_id] = mapped
        return mapped

    def _sync_contracts(self) -> None:
        section = "contracts"
        if not self._available(Contract, section):
            return
        fields = self._fields(Contract)
        rows = self._src(Contract).values(*fields).iterator(chunk_size=self.batch)
        for chunk in _chunked(rows, self.batch):
            existing = {
                _addr(obj.address): obj
                for obj in self._dst(Contract).filter(
                    pk__in=[row["address"] for row in chunk]
                )
            }
            to_insert, to_update = [], []
            for row in chunk:
                abi_id = self._map_abi(row.get("contract_abi_id"))
                obj = existing.get(_addr(row["address"]))
                if obj is None:
                    data = {k: v for k, v in row.items() if k != "contract_abi_id"}
                    to_insert.append(Contract(**data, contract_abi_id=abi_id))
                    continue
                changed = []
                # Source (the service used by people) wins on non-empty values, it never blanks destination
                for name in ("name", "display_name"):
                    if row.get(name) and row[name] != getattr(obj, name):
                        setattr(obj, name, row[name])
                        changed.append(name)
                if row.get("logo") and row["logo"] != (
                    obj.logo.name if obj.logo else ""
                ):
                    obj.logo = row["logo"]
                    changed.append("logo")
                if abi_id is not None and abi_id != obj.contract_abi_id:
                    obj.contract_abi_id = abi_id
                    changed.append("contract_abi")
                if (
                    "trusted_for_delegate_call" in row
                    and row["trusted_for_delegate_call"]
                    != obj.trusted_for_delegate_call
                ):
                    obj.trusted_for_delegate_call = row["trusted_for_delegate_call"]
                    changed.append("trusted_for_delegate_call")
                if changed:
                    to_update.append((obj, changed))
                else:
                    self.stats.add(section, "unchanged")
            self._insert(Contract, to_insert, section)
            self._update(Contract, to_update, section)

    # ------------------------------------------------------------------ tokens
    def _merge_tokens(self, rows: Iterable[dict], seen: set[str]) -> None:
        section = "tokens"
        for chunk in _chunked(rows, self.batch):
            existing = {
                _addr(obj.address): obj
                for obj in self._dst(Token).filter(
                    pk__in=[row["address"] for row in chunk]
                )
            }
            to_insert, to_update = [], []
            for row in chunk:
                seen.add(_addr(row["address"]))
                obj = existing.get(_addr(row["address"]))
                if obj is None:
                    to_insert.append(Token(**row))
                    continue
                changed = []
                # Curated flags: source is the truth (a flag can also have been removed after the backup)
                for name in ("spam", "trusted", "events_bugged"):
                    if name in row and row[name] != getattr(obj, name):
                        setattr(obj, name, row[name])
                        changed.append(name)
                if row.get("logo_uri") and row["logo_uri"] != obj.logo_uri:
                    obj.logo_uri = row["logo_uri"]
                    changed.append("logo_uri")
                if row.get("logo") and row["logo"] != (
                    obj.logo.name if obj.logo else ""
                ):
                    obj.logo = row["logo"]
                    changed.append("logo")
                if row.get("copy_price") and row["copy_price"] != obj.copy_price:
                    obj.copy_price = row["copy_price"]
                    changed.append("copy_price")
                if changed:
                    to_update.append((obj, changed))
                else:
                    self.stats.add(section, "unchanged")
            self._insert(Token, to_insert, section)
            self._update(Token, to_update, section)

    def _sync_tokens(self) -> None:
        if not self._available(Token, "tokens"):
            return
        fields = self._fields(Token)
        curated = (
            Q(spam=True)
            | Q(trusted=True)
            | Q(events_bugged=True)
            | Q(copy_price__isnull=False)
            | Q(logo_uri__gt="")
            | Q(logo__gt="")
        )
        queryset = self._src(Token)
        if not self.all_tokens:
            queryset = queryset.filter(curated)
        seen: set[str] = set()
        self._merge_tokens(
            queryset.values(*fields).iterator(chunk_size=self.batch), seen
        )
        # Tokens curated in destination but not (anymore) in source: the flag was removed after the backup
        dest_curated = [
            _addr(address)
            for address in self._dst(Token).filter(curated).values_list("pk", flat=True)
        ]
        for chunk in _chunked([a for a in dest_curated if a not in seen], self.batch):
            rows = self._src(Token).filter(pk__in=chunk).values(*fields)
            self._merge_tokens(list(rows), seen)

    def _sync_token_lists(self) -> None:
        section = "token-lists"
        if not self._available(TokenList, section):
            return
        fields = self._fields(TokenList)
        existing = {obj.url: obj for obj in self._dst(TokenList)}
        to_insert, to_update = [], []
        for row in self._src(TokenList).values(*fields):
            obj = existing.get(row["url"])
            if obj is None:
                to_insert.append(TokenList(**row))
                continue
            changed = [
                name
                for name in ("description", "auth_type", "auth_key", "auth_value")
                if name in row and row[name] != getattr(obj, name)
            ]
            for name in changed:
                setattr(obj, name, row[name])
            if changed:
                to_update.append((obj, changed))
            else:
                self.stats.add(section, "unchanged")
        self._insert(TokenList, to_insert, section)
        self._update(TokenList, to_update, section)

    # ------------------------------------------------------------------ delegates
    def _sync_delegates(self) -> None:
        section = "delegates"
        if not self._available(SafeContractDelegate, section):
            return
        fields = self._fields(SafeContractDelegate)

        # `safe_contract` can be NULL (valid for every Safe of the delegator) and Postgres unique constraints do
        # not match NULLs, so duplicates must be avoided here
        def key(safe_contract_id, delegate, delegator) -> tuple:
            return _addr(safe_contract_id), _addr(delegate), _addr(delegator)

        # Table has no timestamps (and is small): compare it completely, including deletions. A key can have
        # several rows when the Safe is NULL
        existing: dict[tuple, list[SafeContractDelegate]] = defaultdict(list)
        for obj in self._dst(SafeContractDelegate):
            existing[key(obj.safe_contract_id, obj.delegate, obj.delegator)].append(obj)
        source_rows = list(self._src(SafeContractDelegate).values(*fields))
        safes = {
            _addr(row["safe_contract_id"])
            for row in source_rows
            if row["safe_contract_id"]
        }
        safes_in_dest = {
            _addr(address)
            for chunk in _chunked(safes, self.batch)
            for address in self._dst(SafeContract)
            .filter(pk__in=chunk)
            .values_list("pk", flat=True)
        }
        source_keys, to_insert, to_update = set(), [], []
        for row in source_rows:
            row_key = key(row["safe_contract_id"], row["delegate"], row["delegator"])
            if row_key in source_keys:  # Duplicate in source
                continue
            source_keys.add(row_key)
            obj = existing[row_key][0] if row_key in existing else None
            if obj is None:
                if (
                    row["safe_contract_id"]
                    and _addr(row["safe_contract_id"]) not in safes_in_dest
                ):
                    self.stats.add(
                        section, "skipped (Safe not indexed in destination yet)"
                    )
                    self.stats.warn(
                        f"[delegates] Safe {row['safe_contract_id']} is not in destination, "
                        f"run again once it is indexed"
                    )
                    continue
                to_insert.append(SafeContractDelegate(**row))
                continue
            changed = [
                name
                for name in ("label", "read", "write", "expiry_date")
                if name in row and row[name] != getattr(obj, name)
            ]
            for name in changed:
                setattr(obj, name, row[name])
            if changed:
                to_update.append((obj, changed))
            else:
                self.stats.add(section, "unchanged")
        self._insert(SafeContractDelegate, to_insert, section)
        self._update(SafeContractDelegate, to_update, section)

        if self.sync_deletes:
            # A delegate removed in source must not keep being able to propose transactions. Duplicates (NULL Safe)
            # of the ones kept are removed too
            removed = [
                obj.pk
                for row_key, objs in existing.items()
                for obj in (objs[1:] if row_key in source_keys else objs)
            ]
            self._guard_deletes(
                section, len(removed), sum(len(objs) for objs in existing.values())
            )
            for chunk in _chunked(removed, self.batch):
                # Raw delete: the post_delete handler sends a webhook without checking DISABLE_SERVICE_EVENTS
                queryset = self._dst(SafeContractDelegate).filter(pk__in=chunk)
                queryset._raw_delete(queryset.db)
            self.stats.add(section, "deleted (removed in source)", len(removed))

    # ------------------------------------------------------------------ multisig transactions
    def _offchain_confirmation_hashes(self) -> set[bytes]:
        if self._confirmation_hashes is None:
            rows = (
                self._src(MultisigConfirmation)
                .filter(ethereum_tx__isnull=True, created__gte=self.since_eff)
                .values_list("multisig_transaction_hash", "multisig_transaction_id")
                .iterator(chunk_size=self.batch)
            )
            self._confirmation_hashes = {
                _hb(tx_hash or tx_id) for tx_hash, tx_id in rows if tx_hash or tx_id
            }
        return self._confirmation_hashes

    def _merge_multisig_txs(self, rows: Iterable[dict]) -> None:
        section = "multisig-transactions"
        meta_fields = ("proposer", "proposed_by_delegate", "origin", "trusted")
        for chunk in _chunked(rows, self.batch):
            hashes = [row["safe_tx_hash"] for row in chunk]
            existing = {
                _hb(obj.pk): obj
                for obj in self._dst(MultisigTransaction)
                .filter(pk__in=hashes)
                .only("safe_tx_hash", "safe", "ethereum_tx_id", *meta_fields)
            }
            ethereum_tx_ids = [
                row["ethereum_tx_id"] for row in chunk if row.get("ethereum_tx_id")
            ]
            indexed = {
                _hb(pk)
                for pk in self._dst(EthereumTx)
                .filter(pk__in=ethereum_tx_ids)
                .values_list("pk", flat=True)
            }
            to_insert, to_update = [], []
            for row in chunk:
                obj = existing.get(_hb(row["safe_tx_hash"]))
                if obj is None:
                    data = dict(row)
                    if (
                        data.get("ethereum_tx_id")
                        and _hb(data["ethereum_tx_id"]) not in indexed
                    ):
                        # Executed in source but not indexed here yet. The tx processor fills ethereum_tx,
                        # failed, signatures and trusted when it reaches the execution
                        data["ethereum_tx_id"] = None
                        data["failed"] = None
                        self.stats.add(
                            section, "inserted as queued (execution not indexed yet)"
                        )
                    to_insert.append(MultisigTransaction(**data))
                    continue
                # Transactions indexed from the chain miss what only the API knows
                changed = []
                for name in ("proposer", "proposed_by_delegate"):
                    if row.get(name) and not getattr(obj, name):
                        setattr(obj, name, row[name])
                        changed.append(name)
                if row.get("origin") and not obj.origin:
                    obj.origin = row["origin"]
                    changed.append("origin")
                if row.get("trusted") and not obj.trusted:
                    obj.trusted = True
                    changed.append("trusted")
                if changed:
                    to_update.append((obj, changed))
                else:
                    self.stats.add(section, "unchanged")
            self._insert(MultisigTransaction, to_insert, section)
            self._update(MultisigTransaction, to_update, section)
            self._touch_safes(tx.safe for tx in to_insert)
            self._touch_safes(tx.safe for tx, _ in to_update)
            self._bind_waiting_confirmations([_hb(tx.safe_tx_hash) for tx in to_insert])

    def _bind_waiting_confirmations(self, tx_hashes: list[bytes]) -> None:
        """
        Confirmations indexed from the chain (``approveHash``) wait without transaction until it exists.
        ``bulk_create`` skips the signal that binds them and marks the transaction as trusted
        """
        section = "multisig-transactions"
        for chunk in _chunked(tx_hashes, self.batch):
            waiting = self._dst(MultisigConfirmation).filter(
                multisig_transaction__isnull=True, multisig_transaction_hash__in=chunk
            )
            hashes = list(
                {
                    _hb(tx_hash)
                    for tx_hash in waiting.values_list(
                        "multisig_transaction_hash", flat=True
                    )
                }
            )
            if not hashes:
                continue
            updated = waiting.update(
                multisig_transaction_id=F("multisig_transaction_hash")
            )
            self.stats.add(
                section, "confirmations linked to the inserted transaction", updated
            )
            transactions = self._dst(MultisigTransaction).filter(pk__in=hashes)
            self._touch_safes(transactions.values_list("safe", flat=True))
            transactions.update(trusted=True, modified=timezone.now())

    def _sync_multisig_transactions(self) -> None:
        section = "multisig-transactions"
        if not self._available(MultisigTransaction, section):
            return
        fields = self._fields(MultisigTransaction)
        seen: set[bytes] = set()

        def tracked(rows: Iterable[dict]) -> Iterator[dict]:
            for row in rows:
                seen.add(_hb(row["safe_tx_hash"]))
                yield row

        recent = (
            self._src(MultisigTransaction)
            .filter(modified__gte=self.since_eff)
            .values(*fields)
            .iterator(chunk_size=self.batch)
        )
        self._merge_multisig_txs(tracked(recent))
        # `trusted` is changed with `.update()` (no `modified`) when a confirmation is added to a queued tx
        extra = [h for h in self._offchain_confirmation_hashes() if h not in seen]
        for chunk in _chunked(extra, self.batch):
            rows = self._src(MultisigTransaction).filter(pk__in=chunk).values(*fields)
            self._merge_multisig_txs(list(rows))

        if self.sync_deletes:
            # Queued transactions can be deleted through the API. The destination never creates queued rows by
            # itself, so a queued row that does not exist in source was deleted there
            queued = [
                _hb(pk)
                for pk in self._dst(MultisigTransaction)
                .filter(ethereum_tx__isnull=True)
                .values_list("pk", flat=True)
            ]
            removed: list[bytes] = []
            for chunk in _chunked(queued, self.batch):
                present = {
                    _hb(pk)
                    for pk in self._src(MultisigTransaction)
                    .filter(pk__in=chunk)
                    .values_list("pk", flat=True)
                }
                removed.extend(h for h in chunk if h not in present)
            self._guard_deletes(section, len(removed), len(queued))
            deleted = 0
            for chunk in _chunked(removed, self.batch):
                # The destination indexer keeps running: only delete the rows that are still queued once locked
                # (one executed meanwhile is on-chain data), and keep the on-chain confirmations waiting for it
                doomed = list(
                    self._dst(MultisigTransaction)
                    .select_for_update()
                    .filter(pk__in=chunk, ethereum_tx__isnull=True)
                    .values_list("pk", "safe")
                )
                pks = [pk for pk, _ in doomed]
                self._dst(MultisigConfirmation).filter(
                    multisig_transaction_id__in=pks, ethereum_tx__isnull=False
                ).update(multisig_transaction=None)
                self._dst(MultisigTransaction).filter(pk__in=pks).delete()
                self._touch_safes(safe for _, safe in doomed)
                deleted += len(pks)
            self.stats.add(section, "deleted (removed in source)", deleted)

    # ------------------------------------------------------------------ multisig confirmations
    def _sync_multisig_confirmations(self) -> None:
        section = "multisig-confirmations"
        if not self._available(MultisigConfirmation, section):
            return
        fields = [
            f
            for f in self._fields(MultisigConfirmation)
            if f not in ("ethereum_tx_id", "multisig_transaction_id")
        ]
        rows = (
            self._src(MultisigConfirmation)
            .filter(ethereum_tx__isnull=True, created__gte=self.since_eff)
            .values(*fields, "multisig_transaction_id")
            .iterator(chunk_size=self.batch)
        )
        linked_hashes: set[bytes] = set()
        for chunk in _chunked(rows, self.batch):
            for row in chunk:
                row["_hash"] = _hb(
                    row.get("multisig_transaction_hash")
                    or row["multisig_transaction_id"]
                )
            chunk = [row for row in chunk if row["_hash"]]
            hashes = list({row["_hash"] for row in chunk})
            existing = {
                (_hb(tx_hash), _addr(owner))
                for tx_hash, owner in self._dst(MultisigConfirmation)
                .filter(multisig_transaction_hash__in=hashes)
                .values_list("multisig_transaction_hash", "owner")
            }
            txs_in_dest = {
                _hb(pk): safe
                for pk, safe in self._dst(MultisigTransaction)
                .filter(pk__in=hashes)
                .values_list("pk", "safe")
            }
            linked_hashes.update(txs_in_dest)
            to_insert = []
            for row in chunk:
                row_key = (row["_hash"], _addr(row["owner"]))
                if row_key in existing:
                    self.stats.add(section, "already in destination")
                    continue
                existing.add(row_key)
                data = {
                    k: v
                    for k, v in row.items()
                    if k not in ("_hash", "multisig_transaction_id")
                }
                data["multisig_transaction_hash"] = HexBytes(row["_hash"])
                to_insert.append(
                    MultisigConfirmation(
                        **data,
                        ethereum_tx_id=None,
                        multisig_transaction_id=(
                            HexBytes(row["_hash"])
                            if row["_hash"] in txs_in_dest
                            else None
                        ),
                    )
                )
                if row["_hash"] not in txs_in_dest:
                    self.stats.add(
                        section, "inserted without transaction (not in destination)"
                    )
                else:
                    self._touch_safes([txs_in_dest[row["_hash"]]])
            self._insert(MultisigConfirmation, to_insert, section)

        # Confirmations already in destination waiting for a transaction that exists now
        for chunk in _chunked(list(linked_hashes), self.batch):
            updated = (
                self._dst(MultisigConfirmation)
                .filter(
                    multisig_transaction__isnull=True,
                    multisig_transaction_hash__in=chunk,
                )
                .update(multisig_transaction_id=F("multisig_transaction_hash"))
            )
            self.stats.add(section, "linked to its transaction", updated)
            if updated:
                self._touch_safes(
                    self._dst(MultisigTransaction)
                    .filter(pk__in=chunk)
                    .values_list("safe", flat=True)
                )

    # ------------------------------------------------------------------ safe messages
    def _sync_safe_messages(self) -> None:
        section = "safe-messages"
        if not (
            self._available(SafeMessage, section)
            and self._available(SafeMessageConfirmation, section)
        ):
            return
        message_fields = self._fields(SafeMessage)
        confirmation_fields = self._fields(SafeMessageConfirmation)

        message_hashes = {
            _hb(pk)
            for pk in self._src(SafeMessage)
            .filter(created__gte=self.since_eff)
            .values_list("pk", flat=True)
        }
        confirmations = list(
            self._src(SafeMessageConfirmation)
            .filter(created__gte=self.since_eff, safe_message__isnull=False)
            .values(*confirmation_fields)
        )
        # Confirmations added to messages created before the backup need their message to exist
        message_hashes |= {_hb(row["safe_message_id"]) for row in confirmations}

        for chunk in _chunked(sorted(message_hashes), self.batch):
            existing = {
                _hb(pk)
                for pk in self._dst(SafeMessage)
                .filter(pk__in=chunk)
                .values_list("pk", flat=True)
            }
            rows = self._src(SafeMessage).filter(pk__in=chunk).values(*message_fields)
            to_insert = [
                SafeMessage(**row)
                for row in rows
                if _hb(row["message_hash"]) not in existing
            ]
            self.stats.add(section, "messages already in destination", len(existing))
            self._insert(SafeMessage, to_insert, section + " (messages)")

        for chunk in _chunked(confirmations, self.batch):
            hashes = list({_hb(row["safe_message_id"]) for row in chunk})
            messages_in_dest = {
                _hb(pk)
                for pk in self._dst(SafeMessage)
                .filter(pk__in=hashes)
                .values_list("pk", flat=True)
            }
            existing = {
                (_hb(message_hash), _addr(owner))
                for message_hash, owner in self._dst(SafeMessageConfirmation)
                .filter(safe_message_id__in=hashes)
                .values_list("safe_message_id", "owner")
            }
            to_insert = []
            for row in chunk:
                row_key = (_hb(row["safe_message_id"]), _addr(row["owner"]))
                if row_key in existing:
                    self.stats.add(section, "confirmations already in destination")
                    continue
                if row_key[0] not in messages_in_dest:
                    self.stats.add(section, "confirmations skipped (message missing)")
                    continue
                existing.add(row_key)
                to_insert.append(SafeMessageConfirmation(**row))
            self._insert(
                SafeMessageConfirmation, to_insert, section + " (confirmations)"
            )

    # ------------------------------------------------------------------ account abstraction
    def _sync_account_abstraction(self) -> None:
        section = "account-abstraction"
        models = (
            UserOperation,
            UserOperationReceipt,
            SafeOperation,
            SafeOperationConfirmation,
        )
        # Every missing table is reported, so the check is not short-circuited
        available = [self._available(model, section) for model in models]
        if not all(available):
            return
        operation_fields = self._fields(SafeOperation)
        user_operation_fields = self._fields(UserOperation)
        receipt_fields = self._fields(UserOperationReceipt)
        confirmation_fields = self._fields(SafeOperationConfirmation)

        operation_hashes = {
            _hb(pk)
            for pk in self._src(SafeOperation)
            .filter(created__gte=self.since_eff)
            .values_list("pk", flat=True)
        }
        confirmations = list(
            self._src(SafeOperationConfirmation)
            .filter(created__gte=self.since_eff)
            .values(*confirmation_fields)
        )
        operation_hashes |= {_hb(row["safe_operation_id"]) for row in confirmations}

        for chunk in _chunked(sorted(operation_hashes), self.batch):
            in_dest = {
                _hb(pk)
                for pk in self._dst(SafeOperation)
                .filter(pk__in=chunk)
                .values_list("pk", flat=True)
            }
            missing = [
                row
                for row in self._src(SafeOperation)
                .filter(pk__in=chunk)
                .values(*operation_fields)
                if _hb(row["hash"]) not in in_dest
            ]
            self.stats.add(
                section, "safe operations already in destination", len(in_dest)
            )
            if not missing:
                continue
            user_operation_hashes = [row["user_operation_id"] for row in missing]
            user_operations_in_dest = {
                _hb(pk)
                for pk in self._dst(UserOperation)
                .filter(pk__in=user_operation_hashes)
                .values_list("pk", flat=True)
            }
            source_user_operations = {
                _hb(row["hash"]): row
                for row in self._src(UserOperation)
                .filter(pk__in=user_operation_hashes)
                .values(*user_operation_fields)
            }
            ethereum_tx_ids = [
                row["ethereum_tx_id"]
                for row in source_user_operations.values()
                if row.get("ethereum_tx_id")
            ]
            indexed = {
                _hb(pk)
                for pk in self._dst(EthereumTx)
                .filter(pk__in=ethereum_tx_ids)
                .values_list("pk", flat=True)
            }
            new_user_operations, new_operations, new_hashes = [], [], []
            for row in missing:
                user_operation_hash = _hb(row["user_operation_id"])
                if user_operation_hash not in user_operations_in_dest:
                    user_operation = source_user_operations.get(user_operation_hash)
                    if user_operation is None:
                        self.stats.warn(
                            f"[account-abstraction] UserOperation {user_operation_hash.hex()} not found in source"
                        )
                        continue
                    if (
                        user_operation.get("ethereum_tx_id")
                        and _hb(user_operation["ethereum_tx_id"]) not in indexed
                    ):
                        # Executed on chain but not indexed here yet: the indexer will create it
                        self.stats.add(section, "skipped (execution not indexed yet)")
                        self.stats.warn(
                            "[account-abstraction] some executed operations are not indexed in destination "
                            "yet, run again when it is synced"
                        )
                        continue
                    new_user_operations.append(UserOperation(**user_operation))
                    new_hashes.append(user_operation_hash)
                new_operations.append(SafeOperation(**row))
            self._insert(
                UserOperation, new_user_operations, section + " (user operations)"
            )
            receipts = [
                UserOperationReceipt(**row)
                for row in self._src(UserOperationReceipt)
                .filter(user_operation_id__in=new_hashes)
                .values(*receipt_fields)
            ]
            self._insert(UserOperationReceipt, receipts, section + " (receipts)")
            self._insert(SafeOperation, new_operations, section + " (safe operations)")

        for chunk in _chunked(confirmations, self.batch):
            hashes = list({_hb(row["safe_operation_id"]) for row in chunk})
            operations_in_dest = {
                _hb(pk)
                for pk in self._dst(SafeOperation)
                .filter(pk__in=hashes)
                .values_list("pk", flat=True)
            }
            existing = {
                (_hb(operation_hash), _addr(owner))
                for operation_hash, owner in self._dst(SafeOperationConfirmation)
                .filter(safe_operation_id__in=hashes)
                .values_list("safe_operation_id", "owner")
            }
            to_insert = []
            for row in chunk:
                row_key = (_hb(row["safe_operation_id"]), _addr(row["owner"]))
                if row_key in existing:
                    self.stats.add(section, "confirmations already in destination")
                    continue
                if row_key[0] not in operations_in_dest:
                    self.stats.add(section, "confirmations skipped (operation missing)")
                    continue
                existing.add(row_key)
                to_insert.append(SafeOperationConfirmation(**row))
            self._insert(
                SafeOperationConfirmation, to_insert, section + " (confirmations)"
            )

    # ------------------------------------------------------------------ report
    def _report(self) -> None:
        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS("APPLIED")
            if self.apply
            else self.style.WARNING("DRY RUN, nothing was written")
        )
        for section, counters in self.stats.counters.items():
            self.stdout.write(f"  {section}")
            for key, value in counters.items():
                self.stdout.write(f"    {key}: {value}")
        for warning in self.stats.warnings:
            self.stdout.write(self.style.WARNING(f"WARNING: {warning}"))
        if not self.apply:
            self.stdout.write("Run again with --apply to write the changes")
