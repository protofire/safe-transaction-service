import copy
import json
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.conf import settings
from django.core.management import CommandError, call_command
from django.db import connection, connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from eth_account import Account
from hexbytes import HexBytes
from safe_eth.eth.utils import fast_keccak, fast_keccak_text

from safe_transaction_service.account_abstraction.models import (
    SafeOperation,
    SafeOperationConfirmation,
    UserOperation,
    UserOperationReceipt,
)
from safe_transaction_service.contracts.models import Contract, ContractAbi
from safe_transaction_service.safe_messages.models import (
    SafeMessage,
    SafeMessageConfirmation,
)
from safe_transaction_service.tokens.models import Token, TokenList

from ..management.commands.copy_offchain_data import Command
from ..models import (
    Chain,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContractDelegate,
)
from .factories import (
    EthereumTxFactory,
    MultisigTransactionFactory,
    SafeContractDelegateFactory,
    SafeContractFactory,
)

SOURCE_SCHEMA = "offchain_src"
SOURCE_RW_ALIAS = "offchain_src_rw"
COPIED_TABLES = [
    "history_chain",
    "history_multisigtransaction",
    "history_multisigconfirmation",
    "history_safecontractdelegate",
    "safe_messages_safemessage",
    "safe_messages_safemessageconfirmation",
    "tokens_token",
    "tokens_tokenlist",
    "contracts_contract",
    "contracts_contractabi",
    "account_abstraction_useroperation",
    "account_abstraction_useroperationreceipt",
    "account_abstraction_safeoperation",
    "account_abstraction_safeoperationconfirmation",
    "django_migrations",
]


def _register_alias(alias: str, search_path: str | None = None) -> None:
    """
    Django only allows aliases declared before the test class is set up. They mirror the default test
    database, so no extra database is created
    """
    config = copy.deepcopy(connection.settings_dict)
    config["OPTIONS"] = (
        {"options": f"-c search_path={search_path}"} if search_path else {}
    )
    config["TEST"] = {"MIRROR": "default"}
    connections.settings[alias] = config


_register_alias(SOURCE_RW_ALIAS, SOURCE_SCHEMA)
_register_alias("offchain_source")  # Alias used by the command, replaced when it runs


def _hash(name: str) -> str:
    return HexBytes(fast_keccak_text(name)).hex()


def _address() -> str:
    return Account.create().address


class TestCopyOffchainData(TransactionTestCase):
    """
    The "source" service database is emulated with another schema (``offchain_src``) of the test
    database: same tables, other data, reached through a different connection (``search_path``)
    """

    databases = {"default", SOURCE_RW_ALIAS, "offchain_source"}

    def setUp(self):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE")
            cursor.execute(f"CREATE SCHEMA {SOURCE_SCHEMA}")
            for table in COPIED_TABLES:
                cursor.execute(
                    f"CREATE TABLE {SOURCE_SCHEMA}.{table} (LIKE public.{table} INCLUDING ALL)"
                )
            cursor.execute(
                f"INSERT INTO {SOURCE_SCHEMA}.django_migrations SELECT * FROM public.django_migrations"
            )
        settings_dict = connection.settings_dict
        self.source_url = (
            f"psql://{settings_dict['USER']}:{settings_dict['PASSWORD']}@{settings_dict['HOST']}:"
            f"{settings_dict['PORT']}/{settings_dict['NAME']}"
            f"?options=-c%20search_path%3D{SOURCE_SCHEMA}"
        )
        # Both databases must say which chain they belong to
        Chain.objects.create(chain_id=1)
        Chain._base_manager.using(SOURCE_RW_ALIAS).bulk_create([Chain(chain_id=1)])
        queue_patcher = mock.patch(
            "safe_transaction_service.history.signals.get_queue_service"
        )
        self.queue_mock = queue_patcher.start()
        self.addCleanup(queue_patcher.stop)
        self.since = timezone.now() - timedelta(days=1)
        self.old = timezone.now() - timedelta(days=30)

    def tearDown(self):
        connections[SOURCE_RW_ALIAS].close()
        with connection.cursor() as cursor:
            cursor.execute(f"DROP SCHEMA IF EXISTS {SOURCE_SCHEMA} CASCADE")

    # --------------------------------------------------------------- helpers
    def run_command(self, *args: str, apply: bool = True) -> str:
        out = StringIO()
        call_command(
            "copy_offchain_data",
            f"--source-url={self.source_url}",
            f"--since={self.since.isoformat()}",
            *(["--apply"] if apply else []),
            *args,
            stdout=out,
        )
        return out.getvalue()

    def src_save(self, obj):
        # bulk_create: no signals (they would try to bind/queue events using the default database)
        type(obj)._base_manager.using(SOURCE_RW_ALIAS).bulk_create([obj])
        return obj

    def src_make_old(self, model, **filters):
        model._base_manager.using(SOURCE_RW_ALIAS).filter(**filters).update(
            created=self.old, modified=self.old
        )

    def src_tx(self, **kwargs) -> MultisigTransaction:
        defaults = {
            "safe_tx_hash": _hash(f"tx-{_address()}"),
            "safe": _address(),
            "to": _address(),
            "value": 0,
            "data": b"",
            "operation": 0,
            "safe_tx_gas": 0,
            "base_gas": 0,
            "gas_price": 0,
            "nonce": 0,
            "origin": {},
        }
        return self.src_save(MultisigTransaction(**{**defaults, **kwargs}))

    def src_confirmation(self, tx_hash: str, owner: str | None = None, **kwargs):
        defaults = {
            "multisig_transaction_hash": tx_hash,
            "multisig_transaction_id": None,
            "owner": owner or _address(),
            "signature": HexBytes(b"\x01" * 65),
            "signature_type": 2,  # EOA
            "ethereum_tx": None,
        }
        return self.src_save(MultisigConfirmation(**{**defaults, **kwargs}))

    # --------------------------------------------------------------- tests
    def test_copy_multisig_transactions_and_confirmations(self):
        owner = _address()
        proposer = _address()
        delegate = _address()

        # Queued tx that only exists in source
        tx_new = self.src_tx(
            proposer=proposer, origin={"name": "app"}, trusted=True, nonce=3
        )
        self.src_confirmation(tx_new.safe_tx_hash, owner)
        self.src_confirmation(tx_new.safe_tx_hash)

        # Queued tx in both, source got a confirmation after the backup (``trusted`` is set with .update())
        tx_both_dest = MultisigTransactionFactory(
            ethereum_tx=None, proposer=proposer, origin={}, trusted=False
        )
        self.src_tx(
            safe_tx_hash=tx_both_dest.safe_tx_hash,
            safe=tx_both_dest.safe,
            nonce=tx_both_dest.nonce,
            proposer=proposer,
            trusted=True,
        )
        self.src_make_old(
            MultisigTransaction, safe_tx_hash=tx_both_dest.safe_tx_hash
        )  # Not modified since the backup
        self.src_confirmation(tx_both_dest.safe_tx_hash, owner)
        # ...and the destination already has a confirmation for it that waits for the transaction
        orphan_in_dest = MultisigConfirmation.objects.create(
            ethereum_tx=None,
            multisig_transaction=None,
            multisig_transaction_hash=tx_both_dest.safe_tx_hash,
            owner=_address(),
            signature=HexBytes(b"\x03" * 65),
            signature_type=2,
        )

        # Executed tx indexed from the chain by the destination: only the API metadata is missing there
        ethereum_tx = EthereumTxFactory()
        tx_executed_dest = MultisigTransactionFactory(
            ethereum_tx=ethereum_tx, proposer=None, origin={}, trusted=False
        )
        self.src_tx(
            safe_tx_hash=tx_executed_dest.safe_tx_hash,
            safe=tx_executed_dest.safe,
            nonce=tx_executed_dest.nonce,
            ethereum_tx_id=ethereum_tx.tx_hash,
            failed=False,
            proposer=proposer,
            proposed_by_delegate=delegate,
            origin={"url": "https://app"},
            trusted=True,
        )

        # Executed in source, not indexed by the destination yet
        tx_executed_source = self.src_tx(
            ethereum_tx_id=_hash("not-indexed-yet"), failed=True, proposer=proposer
        )

        # Queued in destination (from the backup), deleted in source
        tx_deleted = MultisigTransactionFactory(ethereum_tx=None)
        MultisigConfirmation.objects.create(
            ethereum_tx=None,
            multisig_transaction=tx_deleted,
            multisig_transaction_hash=tx_deleted.safe_tx_hash,
            owner=owner,
            signature=HexBytes(b"\x04" * 65),
            signature_type=2,
        )

        # Old tx that is not in destination: not touched by the delta
        tx_old = self.src_tx()
        self.src_make_old(MultisigTransaction, safe_tx_hash=tx_old.safe_tx_hash)

        # Confirmation of a transaction that does not exist anywhere: kept by hash
        orphan_hash = _hash("orphan-tx")
        self.src_confirmation(orphan_hash)

        # On-chain confirmations (approveHash) are indexed by the destination, never copied
        onchain_tx = EthereumTxFactory()
        self.src_confirmation(
            _hash("approved"), ethereum_tx_id=onchain_tx.tx_hash, signature_type=1
        )

        # Dry run does not write anything
        output = self.run_command(apply=False)
        self.assertIn("DRY RUN", output)
        self.assertFalse(
            MultisigTransaction.objects.filter(
                safe_tx_hash=tx_new.safe_tx_hash
            ).exists()
        )
        self.assertTrue(
            MultisigTransaction.objects.filter(
                safe_tx_hash=tx_deleted.safe_tx_hash
            ).exists()
        )

        self.queue_mock.reset_mock()
        output = self.run_command()
        self.queue_mock.assert_not_called()  # No webhooks for copied/deleted rows
        self.assertIn("APPLIED", output)

        # New queued tx, with its timestamps and confirmations
        copied = MultisigTransaction.objects.get(safe_tx_hash=tx_new.safe_tx_hash)
        self.assertIsNone(copied.ethereum_tx_id)
        self.assertEqual(copied.proposer, proposer)
        self.assertEqual(copied.origin, {"name": "app"})
        self.assertTrue(copied.trusted)
        self.assertEqual(copied.created, tx_new.created)
        self.assertEqual(copied.modified, tx_new.modified)
        self.assertEqual(copied.confirmations.count(), 2)
        self.assertEqual(
            copied.confirmations.get(owner=owner).created,
            MultisigConfirmation.objects.using(SOURCE_RW_ALIAS)
            .get(multisig_transaction_hash=tx_new.safe_tx_hash, owner=owner)
            .created,
        )

        # Existing queued tx: trusted is propagated, new confirmation copied and linked
        tx_both_dest.refresh_from_db()
        self.assertTrue(tx_both_dest.trusted)
        self.assertEqual(tx_both_dest.confirmations.count(), 2)
        orphan_in_dest.refresh_from_db()
        self.assertEqual(
            orphan_in_dest.multisig_transaction_id, tx_both_dest.safe_tx_hash
        )

        # Executed tx keeps what the indexer built and gets the metadata
        tx_executed_dest.refresh_from_db()
        self.assertEqual(tx_executed_dest.ethereum_tx_id, ethereum_tx.tx_hash)
        self.assertFalse(tx_executed_dest.failed)
        self.assertEqual(tx_executed_dest.proposer, proposer)
        self.assertEqual(tx_executed_dest.proposed_by_delegate, delegate)
        self.assertEqual(tx_executed_dest.origin, {"url": "https://app"})
        self.assertTrue(tx_executed_dest.trusted)

        # Executed in source but not indexed: inserted as queued, the tx processor completes it later
        pending = MultisigTransaction.objects.get(
            safe_tx_hash=tx_executed_source.safe_tx_hash
        )
        self.assertIsNone(pending.ethereum_tx_id)
        self.assertIsNone(pending.failed)
        self.assertEqual(pending.proposer, proposer)

        # Deleted in source -> deleted in destination, with its confirmations
        self.assertFalse(
            MultisigTransaction.objects.filter(
                safe_tx_hash=tx_deleted.safe_tx_hash
            ).exists()
        )
        self.assertFalse(
            MultisigConfirmation.objects.filter(
                multisig_transaction_hash=tx_deleted.safe_tx_hash
            ).exists()
        )

        self.assertFalse(
            MultisigTransaction.objects.filter(
                safe_tx_hash=tx_old.safe_tx_hash
            ).exists()
        )

        orphan = MultisigConfirmation.objects.get(multisig_transaction_hash=orphan_hash)
        self.assertIsNone(orphan.multisig_transaction_id)
        self.assertIsNone(orphan.ethereum_tx_id)
        self.assertFalse(
            MultisigConfirmation.objects.filter(
                multisig_transaction_hash=_hash("approved")
            ).exists()
        )

        # Idempotent: a second run changes nothing
        output = self.run_command()
        for word in ("inserted", "updated", "deleted"):
            self.assertNotIn(word, output)
        self.assertEqual(MultisigConfirmation.objects.count(), 5)

    def test_delete_guards(self):
        for _ in range(60):
            MultisigTransactionFactory(ethereum_tx=None)
        with self.assertRaisesMessage(CommandError, "would delete 60 of 60"):
            self.run_command()
        self.assertEqual(MultisigTransaction.objects.count(), 60)
        # Deletions can be skipped...
        self.run_command("--no-delete")
        self.assertEqual(MultisigTransaction.objects.count(), 60)
        # ...or explicitly allowed
        self.run_command("--allow-mass-delete")
        self.assertEqual(MultisigTransaction.objects.count(), 0)

    def test_same_database_is_refused(self):
        self.source_url = self.source_url.split("?")[0]
        with self.assertRaisesMessage(CommandError, "same database"):
            self.run_command()

    def test_delegates(self):
        safe = SafeContractFactory()
        updated_delegate = SafeContractDelegateFactory(safe_contract=safe, label="old")
        removed_delegate = SafeContractDelegateFactory(safe_contract=safe)
        owner = _address()
        new_delegate = _address()
        global_delegate = _address()

        self.src_save(
            SafeContractDelegate(
                safe_contract_id=safe.address,
                delegate=updated_delegate.delegate,
                delegator=updated_delegate.delegator,
                label="new label",
                read=True,
                write=False,
                expiry_date=None,
            )
        )
        self.src_save(
            SafeContractDelegate(
                safe_contract_id=safe.address,
                delegate=new_delegate,
                delegator=owner,
                label="new",
            )
        )
        # Valid for every Safe of the delegator (NULL Safe: unique constraint can not detect duplicates)
        self.src_save(
            SafeContractDelegate(
                safe_contract_id=None,
                delegate=global_delegate,
                delegator=owner,
                label="global",
            )
        )
        # Safe not indexed by the destination yet
        self.src_save(
            SafeContractDelegate(
                safe_contract_id=_address(),
                delegate=_address(),
                delegator=owner,
                label="safe-missing",
            )
        )

        # Deleting delegates must not send events, not even in a dry run
        self.queue_mock.reset_mock()
        self.run_command(apply=False)
        self.queue_mock.assert_not_called()
        self.assertTrue(
            SafeContractDelegate.objects.filter(pk=removed_delegate.pk).exists()
        )

        output = self.run_command()
        self.queue_mock.assert_not_called()
        self.assertIn("Safe not indexed in destination yet", output)
        delegates = {d.label: d for d in SafeContractDelegate.objects.all()}
        self.assertEqual(set(delegates), {"new label", "new", "global"})
        self.assertFalse(delegates["new label"].write)
        self.assertIsNone(delegates["new label"].expiry_date)
        self.assertIsNone(delegates["global"].safe_contract_id)
        self.assertFalse(
            SafeContractDelegate.objects.filter(pk=removed_delegate.pk).exists()
        )

        # No duplicates on a second run, also for delegates without Safe
        self.run_command()
        self.assertEqual(SafeContractDelegate.objects.count(), 3)

    def test_safe_messages(self):
        owner = _address()
        message_in_dest = SafeMessage.objects.create(
            message_hash=_hash("message-both"),
            safe=_address(),
            message="in both",
            proposed_by=owner,
            origin={},
        )
        new_message = self.src_save(
            SafeMessage(
                message_hash=_hash("message-new"),
                safe=_address(),
                message={"eip712": True},
                proposed_by=owner,
                safe_app_id=5,
                origin={"url": "x"},
            )
        )
        # Message created before the backup but missing in destination, with a new confirmation
        message_missing = self.src_save(
            SafeMessage(
                message_hash=_hash("message-missing"),
                safe=_address(),
                message="missing",
                proposed_by=owner,
                origin={},
            )
        )
        self.src_make_old(SafeMessage, message_hash=message_missing.message_hash)
        for message in (message_in_dest, new_message, message_missing):
            self.src_save(
                SafeMessageConfirmation(
                    safe_message_id=message.message_hash
                    if isinstance(message.message_hash, str)
                    else HexBytes(message.message_hash).hex(),
                    owner=owner,
                    signature=HexBytes(b"\x05" * 65),
                    signature_type=2,
                )
            )

        self.run_command()
        self.assertEqual(SafeMessage.objects.count(), 3)
        self.assertEqual(SafeMessageConfirmation.objects.count(), 3)
        copied = SafeMessage.objects.get(message_hash=new_message.message_hash)
        self.assertEqual(copied.message, {"eip712": True})
        self.assertEqual(copied.safe_app_id, 5)
        self.assertEqual(copied.created, new_message.created)

        output = self.run_command()
        self.assertNotIn("inserted", output)
        self.assertEqual(SafeMessageConfirmation.objects.count(), 3)

    def test_account_abstraction(self):
        owner = _address()
        safe = _address()
        entry_point = _address()

        def user_operation(name: str, ethereum_tx_id=None) -> UserOperation:
            return self.src_save(
                UserOperation(
                    hash=_hash(name),
                    ethereum_tx_id=ethereum_tx_id,
                    sender=safe,
                    nonce=0,
                    init_code=b"",
                    call_data=b"\x01",
                    call_gas_limit=1,
                    verification_gas_limit=1,
                    pre_verification_gas=1,
                    max_fee_per_gas=1,
                    max_priority_fee_per_gas=1,
                    paymaster=None,
                    paymaster_data=None,
                    signature=b"\x00" * 65,
                    entry_point=entry_point,
                )
            )

        new_user_operation = user_operation("uo-new")
        new_operation = self.src_save(
            SafeOperation(
                hash=_hash("so-new"),
                user_operation_id=new_user_operation.hash,
                module_address=_address(),
            )
        )
        self.src_save(
            SafeOperationConfirmation(
                safe_operation_id=new_operation.hash,
                owner=owner,
                signature=HexBytes(b"\x06" * 65),
                signature_type=2,
            )
        )
        # Executed in source but its transaction is not indexed by the destination yet: skipped for now
        executed_user_operation = user_operation(
            "uo-executed", ethereum_tx_id=_hash("not-indexed-yet")
        )
        self.src_save(
            UserOperationReceipt(
                user_operation_id=executed_user_operation.hash,
                actual_gas_cost=1,
                actual_gas_used=1,
                success=True,
                reason="",
                deposited=1,
            )
        )
        self.src_save(
            SafeOperation(
                hash=_hash("so-executed"),
                user_operation_id=executed_user_operation.hash,
                module_address=_address(),
            )
        )
        # Receipt travels with a copied user operation
        receipt_user_operation = user_operation("uo-receipt")
        self.src_save(
            UserOperationReceipt(
                user_operation_id=receipt_user_operation.hash,
                actual_gas_cost=5,
                actual_gas_used=6,
                success=False,
                reason="reverted",
                deposited=7,
            )
        )
        self.src_save(
            SafeOperation(
                hash=_hash("so-receipt"),
                user_operation_id=receipt_user_operation.hash,
                module_address=_address(),
            )
        )

        output = self.run_command()
        self.assertIn("not indexed in destination", output)
        self.assertEqual(
            {
                HexBytes(h).hex()
                for h in SafeOperation.objects.values_list("pk", flat=True)
            },
            {_hash("so-new"), _hash("so-receipt")},
        )
        self.assertEqual(UserOperation.objects.count(), 2)
        self.assertEqual(SafeOperationConfirmation.objects.count(), 1)
        self.assertEqual(UserOperationReceipt.objects.get().reason, "reverted")

        output = self.run_command()
        self.assertNotIn("inserted", output)
        self.assertEqual(SafeOperation.objects.count(), 2)

    def test_tokens_and_token_lists(self):
        dest_trusted = Token.objects.create(
            address=_address(), name="A", symbol="A", decimals=18, trusted=False
        )
        dest_unspammed = Token.objects.create(
            address=_address(), name="B", symbol="B", decimals=18, spam=True
        )
        dest_untouched = Token.objects.create(
            address=_address(), name="C", symbol="C", decimals=18
        )
        for token, extra in (
            (dest_trusted, {"trusted": True, "logo_uri": "https://logo"}),
            (dest_unspammed, {"spam": False}),
            (dest_untouched, {}),
        ):
            self.src_save(
                Token(
                    address=token.address,
                    name=token.name,
                    symbol=token.symbol,
                    decimals=18,
                    **extra,
                )
            )
        spam_token = self.src_save(
            Token(address=_address(), name="Spam", symbol="SPM", decimals=18, spam=True)
        )
        plain_token = self.src_save(
            Token(address=_address(), name="Plain", symbol="PLN", decimals=18)
        )
        logo_token = self.src_save(
            Token(
                address=_address(),
                name="Logo",
                symbol="LGO",
                decimals=None,
                logo="tokens/logos/x.png",
                copy_price=dest_untouched.address,
            )
        )

        TokenList.objects.create(url="https://list-1", description="list one")
        self.src_save(
            TokenList(
                url="https://list-1",
                description="list one",
                auth_type="bearer",
                auth_key="Authorization",
                auth_value="secret",
            )
        )
        self.src_save(TokenList(url="https://list-2", description="list two"))

        self.run_command()
        dest_trusted.refresh_from_db()
        dest_unspammed.refresh_from_db()
        self.assertTrue(dest_trusted.trusted)
        self.assertEqual(dest_trusted.logo_uri, "https://logo")
        self.assertFalse(dest_unspammed.spam)  # Flag removed in source after the backup
        self.assertTrue(Token.objects.get(address=spam_token.address).spam)
        self.assertEqual(
            Token.objects.get(address=logo_token.address).logo.name,
            "tokens/logos/x.png",
        )
        self.assertEqual(
            Token.objects.get(address=logo_token.address).copy_price,
            dest_untouched.address,
        )
        # Not curated tokens are indexed again by the destination
        self.assertFalse(Token.objects.filter(address=plain_token.address).exists())
        self.assertEqual(
            TokenList.objects.get(url="https://list-1").auth_value, "secret"
        )
        self.assertTrue(TokenList.objects.filter(url="https://list-2").exists())

        output = self.run_command()
        self.assertNotIn("inserted", output)
        self.assertNotIn("updated", output)

        self.run_command("--all-tokens", "--only", "tokens")
        self.assertTrue(Token.objects.filter(address=plain_token.address).exists())

    def test_contracts_and_abis(self):
        abi_in_dest = ContractAbi.objects.create(
            abi=[{"type": "function", "name": "dest", "inputs": []}], description="d"
        )
        abi_shared = [{"type": "function", "name": "shared", "inputs": []}]
        ContractAbi.objects.create(abi=abi_shared, description="shared")
        # Source ids differ from destination ids, and an old row has no `abi_hash`
        source_abis = ContractAbi._base_manager.using(SOURCE_RW_ALIAS).bulk_create(
            [
                ContractAbi(
                    abi=[{"type": "function", "name": "source-only", "inputs": []}],
                    description="no hash",
                    abi_hash=None,
                ),
                ContractAbi(
                    abi=abi_shared,
                    description="shared",
                    abi_hash=fast_keccak(
                        json.dumps(abi_shared, separators=(",", ":")).encode()
                    ),
                ),
            ]
        )
        no_hash_abi, shared_abi = source_abis

        existing_contract = Contract.objects.create(
            address=_address(),
            name="",
            trusted_for_delegate_call=True,
            contract_abi=abi_in_dest,
        )
        self.src_save(
            Contract(
                address=existing_contract.address,
                name="Named",
                display_name="Display",
                logo="contracts/logos/a.png",
                contract_abi_id=shared_abi.pk,
                trusted_for_delegate_call=False,  # Trust revoked after the backup
            )
        )
        new_contract = self.src_save(
            Contract(
                address=_address(),
                name="New",
                contract_abi_id=no_hash_abi.pk,
                trusted_for_delegate_call=True,
            )
        )
        no_abi_contract = self.src_save(Contract(address=_address(), name="No abi"))

        self.run_command()
        existing_contract.refresh_from_db()
        self.assertEqual(existing_contract.name, "Named")
        self.assertEqual(existing_contract.display_name, "Display")
        self.assertEqual(existing_contract.logo.name, "contracts/logos/a.png")
        self.assertFalse(existing_contract.trusted_for_delegate_call)
        self.assertEqual(existing_contract.contract_abi.description, "shared")
        self.assertEqual(
            ContractAbi.objects.count(), 3
        )  # Matched by hash, no duplicates
        copied = Contract.objects.get(address=new_contract.address)
        self.assertEqual(copied.contract_abi.abi[0]["name"], "source-only")
        self.assertTrue(copied.trusted_for_delegate_call)
        self.assertIsNone(
            Contract.objects.get(address=no_abi_contract.address).contract_abi
        )

        output = self.run_command()
        self.assertNotIn("inserted", output)
        self.assertNotIn("updated", output)
        self.assertEqual(ContractAbi.objects.count(), 3)

    def test_delete_guard_on_small_tables(self):
        for _ in range(6):
            MultisigTransactionFactory(ethereum_tx=None)
        # Every row of a small table is not a reason to skip the guard...
        with self.assertRaisesMessage(CommandError, "would delete 6 of 6"):
            self.run_command()
        self.assertEqual(MultisigTransaction.objects.count(), 6)
        # ...but a few rows can be deleted
        for pk in list(MultisigTransaction.objects.values_list("pk", flat=True))[:2]:
            MultisigTransaction.objects.filter(pk=pk).delete()
        self.run_command()
        self.assertEqual(MultisigTransaction.objects.count(), 0)

    def test_chain_must_be_known_and_equal(self):
        Chain.objects.all().delete()
        with self.assertRaisesMessage(CommandError, "Chain table is empty"):
            self.run_command()
        Chain.objects.create(chain_id=2)
        with self.assertRaisesMessage(CommandError, "Chain id mismatch"):
            self.run_command()

    def test_arguments_are_validated(self):
        with self.assertRaisesMessage(CommandError, "Cannot parse --since"):
            self.run_command("--since=2026-13-45")
        with self.assertRaises(CommandError):
            self.run_command("--batch-size=0")
        with self.assertRaises(CommandError):
            self.run_command("--margin-minutes=-1")

    def test_service_events_flag_is_restored(self):
        self.assertFalse(settings.DISABLE_SERVICE_EVENTS)
        self.source_url = "psql://user:password@localhost:notaport/database"
        with self.assertRaises(Exception):  # noqa: B017
            self.run_command()
        self.assertFalse(settings.DISABLE_SERVICE_EVENTS)

    def test_delete_keeps_transactions_executed_meanwhile(self):
        removed_tx = MultisigTransactionFactory(ethereum_tx=None)
        executed_tx = MultisigTransactionFactory(ethereum_tx=None)
        ethereum_tx = EthereumTxFactory()

        def execute_meanwhile(section: str, *args, **kwargs) -> None:
            # The destination indexer executes the transaction after the queued ones were listed
            if section == "multisig-transactions":
                MultisigTransaction.objects.filter(pk=executed_tx.pk).update(
                    ethereum_tx=ethereum_tx
                )

        with mock.patch.object(
            Command, "_guard_deletes", side_effect=execute_meanwhile
        ):
            output = self.run_command()
        self.assertFalse(MultisigTransaction.objects.filter(pk=removed_tx.pk).exists())
        executed_tx.refresh_from_db()
        self.assertEqual(executed_tx.ethereum_tx_id, ethereum_tx.tx_hash)
        self.assertIn("deleted (removed in source): 1", output)

    def test_delete_keeps_onchain_confirmations(self):
        removed_tx = MultisigTransactionFactory(ethereum_tx=None)
        onchain_confirmation = MultisigConfirmation.objects.create(
            ethereum_tx=EthereumTxFactory(),
            multisig_transaction=removed_tx,
            multisig_transaction_hash=removed_tx.safe_tx_hash,
            owner=_address(),
            signature=None,
            signature_type=1,  # approveHash
        )
        self.run_command()
        self.assertFalse(MultisigTransaction.objects.filter(pk=removed_tx.pk).exists())
        # Indexed from the chain, it waits for its transaction like the indexer leaves it
        onchain_confirmation.refresh_from_db()
        self.assertIsNone(onchain_confirmation.multisig_transaction_id)

    def test_inserted_transaction_binds_onchain_confirmations(self):
        # approveHash indexed by the destination before the transaction was proposed. The source has no
        # off-chain confirmation for it
        tx = self.src_tx(proposer=_address())
        onchain_confirmation = MultisigConfirmation.objects.create(
            ethereum_tx=EthereumTxFactory(),
            multisig_transaction=None,
            multisig_transaction_hash=tx.safe_tx_hash,
            owner=_address(),
            signature=None,
            signature_type=1,  # approveHash
        )
        self.assertFalse(tx.trusted)
        self.run_command()
        onchain_confirmation.refresh_from_db()
        self.assertEqual(onchain_confirmation.multisig_transaction_id, tx.safe_tx_hash)
        self.assertTrue(
            MultisigTransaction.objects.get(safe_tx_hash=tx.safe_tx_hash).trusted
        )

    def test_changed_fields_of_other_rows_are_not_written(self):
        # Row A only needs ``proposer`` and row B only needs ``trusted``: ``trusted`` must not be written back
        # for A with the value read before the indexer changed it
        proposer = _address()
        needs_proposer = MultisigTransactionFactory(
            ethereum_tx=None, proposer=None, trusted=False
        )
        needs_trusted = MultisigTransactionFactory(
            ethereum_tx=None, proposer=proposer, trusted=False
        )
        for tx, trusted in ((needs_proposer, False), (needs_trusted, True)):
            self.src_tx(
                safe_tx_hash=tx.safe_tx_hash,
                safe=tx.safe,
                nonce=tx.nonce,
                proposer=proposer,
                trusted=trusted,
            )

        original_update = Command._update

        def update_meanwhile(command, model, updates, section):
            if section == "multisig-transactions":
                MultisigTransaction.objects.filter(pk=needs_proposer.pk).update(
                    trusted=True
                )
            return original_update(command, model, updates, section)

        with mock.patch.object(
            Command, "_update", autospec=True, side_effect=update_meanwhile
        ):
            self.run_command()
        needs_proposer.refresh_from_db()
        needs_trusted.refresh_from_db()
        self.assertEqual(needs_proposer.proposer, proposer)
        self.assertTrue(needs_proposer.trusted)
        self.assertTrue(needs_trusted.trusted)

    def test_cached_views_of_changed_safes_are_removed(self):
        deleted_tx = MultisigTransactionFactory(ethereum_tx=None)
        new_tx = self.src_tx()
        unrelated_tx = MultisigTransactionFactory(ethereum_tx=None)
        self.src_tx(
            safe_tx_hash=unrelated_tx.safe_tx_hash,
            safe=unrelated_tx.safe,
            nonce=unrelated_tx.nonce,
        )
        self.src_make_old(MultisigTransaction, safe_tx_hash=unrelated_tx.safe_tx_hash)
        path = (
            "safe_transaction_service.history.management.commands.copy_offchain_data."
            "remove_cache_view_for_addresses"
        )
        with override_settings(CACHE_VIEW_DEFAULT_TIMEOUT=60):
            with mock.patch(path) as remove_cache_mock:
                self.run_command(apply=False)
                remove_cache_mock.assert_not_called()  # Nothing was written
                self.run_command()
        remove_cache_mock.assert_called_once()
        tag, safes = remove_cache_mock.call_args.args
        self.assertEqual(tag, "multisigtransactionsview")
        self.assertEqual(set(safes), {deleted_tx.safe, new_tx.safe})

    def test_delegates_without_safe(self):
        # Postgres does not detect duplicates of rows without Safe: keep one of them, and remove all of them
        # when the delegate was removed in source
        delegator = _address()
        kept, removed = _address(), _address()
        for label, delegate in (
            ("kept-1", kept),
            ("kept-2", kept),
            ("removed-1", removed),
            ("removed-2", removed),
        ):
            SafeContractDelegate._base_manager.bulk_create(
                [
                    SafeContractDelegate(
                        safe_contract=None,
                        delegate=delegate,
                        delegator=delegator,
                        label=label,
                    )
                ]
            )
        # Duplicated in source: copied once
        new = _address()
        for _ in range(2):
            self.src_save(
                SafeContractDelegate(
                    safe_contract_id=None,
                    delegate=new,
                    delegator=delegator,
                    label="new",
                )
            )
        self.src_save(
            SafeContractDelegate(
                safe_contract_id=None,
                delegate=kept,
                delegator=delegator,
                label="kept",
            )
        )

        self.queue_mock.reset_mock()
        self.run_command()
        self.queue_mock.assert_not_called()
        self.assertEqual(
            sorted(SafeContractDelegate.objects.values_list("delegate", flat=True)),
            sorted([kept, new]),
        )
        self.assertEqual(SafeContractDelegate.objects.get(delegate=kept).label, "kept")

    def test_missing_source_tables_are_skipped(self):
        with connection.cursor() as cursor:
            cursor.execute(
                f"DROP TABLE {SOURCE_SCHEMA}.account_abstraction_safeoperationconfirmation"
            )
        output = self.run_command("--only", "account-abstraction")
        self.assertIn("does not exist in source", output)
