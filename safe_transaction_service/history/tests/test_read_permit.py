import base64
import json
import time
from unittest import mock
from urllib.parse import urlencode

from django.conf import settings
from django.test import override_settings
from django.urls import reverse

from eth_account import Account
from eth_account.messages import encode_typed_data
from hexbytes import HexBytes
from rest_framework import status
from rest_framework.test import APIRequestFactory, APITestCase
from safe_eth.eth.utils import fast_keccak_text
from safe_eth.safe.multi_send import MultiSendOperation, MultiSendTx
from safe_eth.safe.tests.safe_test_case import SafeTestCaseMixin
from safe_eth.util.util import to_0x_hex_str

from ...events.services.queue_service import QueueService
from ..cache import CacheSafeTxsView
from ..helpers import DelegateSignatureHelperV2
from ..models import EthereumTx, MultisigConfirmation, MultisigTransaction
from ..read_permit import (
    OwnerPermitAuthentication,
    ReadPrincipal,
    contains_bare_src20_transfer,
    is_entitled,
    redact_pending_row,
)
from ..services.event_service import build_event_payload
from .factories import MultisigConfirmationFactory, MultisigTransactionFactory

SRC20_BARE_TRANSFER = HexBytes(
    "0xb10c99b5" + "00" * 12 + "11" * 20 + "00" * 31 + "2a"  # amount = 42
)


def build_permit_token(
    signer: Account,
    owner: str,
    safe: str,
    chain_id: int,
    expiry: int | None = None,
    domain_name: str | None = None,
) -> str:
    expiry = int(time.time()) + 3600 if expiry is None else expiry
    signable = encode_typed_data(
        full_message={
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                ],
                "ReadPermit": [
                    {"name": "safe", "type": "address"},
                    {"name": "owner", "type": "address"},
                    {"name": "expiry", "type": "uint256"},
                ],
            },
            "primaryType": "ReadPermit",
            "domain": {
                "name": domain_name or settings.READ_PERMIT_DOMAIN_NAME,
                "version": "1",
                "chainId": chain_id,
            },
            "message": {"safe": safe, "owner": owner, "expiry": expiry},
        }
    )
    sig = to_0x_hex_str(signer.sign_message(signable).signature)
    permit = {
        "owner": owner,
        "safe": safe,
        "chainId": chain_id,
        "expiry": expiry,
        "sig": sig,
    }
    return base64.urlsafe_b64encode(json.dumps(permit).encode()).rstrip(b"=").decode()


class TestOwnerPermitAuthentication(SafeTestCaseMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.owner = Account.create()
        self.safe = Account.create().address
        self.chain_id = self.ethereum_client.get_chain_id()

    def authenticate(self, authorization: str | None):
        headers = {"HTTP_AUTHORIZATION": authorization} if authorization else {}
        request = APIRequestFactory().get("/", **headers)
        return OwnerPermitAuthentication().authenticate(request)

    def bearer(self, **kwargs) -> str:
        params = {
            "signer": self.owner,
            "owner": self.owner.address,
            "safe": self.safe,
            "chain_id": self.chain_id,
        }
        params.update(kwargs)
        return "Bearer " + build_permit_token(**params)

    def test_valid_permit(self):
        user, principal = self.authenticate(self.bearer())
        self.assertFalse(user.is_authenticated)
        self.assertEqual(
            principal, ReadPrincipal(owner=self.owner.address, safe=self.safe)
        )

    def test_invalid_permits_are_anonymous(self):
        now = int(time.time())
        delegate_hash, _ = DelegateSignatureHelperV2.calculate_hash_and_preimage(
            self.owner.address, self.chain_id
        )
        delegate_sig = to_0x_hex_str(
            self.owner.unsafe_sign_hash(delegate_hash)["signature"]
        )
        delegate_permit = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "owner": self.owner.address,
                    "safe": self.safe,
                    "chainId": self.chain_id,
                    "expiry": now + 3600,
                    "sig": delegate_sig,
                }
            ).encode()
        ).decode()
        for authorization in (
            None,
            "Bearer",
            "Bearer some-api-key",
            "Bearer " + base64.urlsafe_b64encode(b"not json").decode(),
            "Bearer " + base64.urlsafe_b64encode(b'{"owner": 1}').decode(),
            "Bearer " + delegate_permit,
            self.bearer(domain_name="Another environment"),
            self.bearer(chain_id=self.chain_id + 1),
            self.bearer(expiry=now - 1),
            self.bearer(expiry=now + settings.READ_PERMIT_MAX_TTL_SECONDS + 60),
            self.bearer(signer=Account.create()),
            "Token "
            + build_permit_token(
                self.owner, self.owner.address, self.safe, self.chain_id
            ),
        ):
            with self.subTest(authorization=authorization):
                self.assertIsNone(self.authenticate(authorization))

    def test_service_key(self):
        self.assertIsNone(self.authenticate("Bearer service-key"))
        with override_settings(TX_SERVICE_READ_SERVICE_KEYS=["service-key"]):
            _, principal = self.authenticate("Bearer service-key")
            self.assertEqual(principal, ReadPrincipal(service=True))
            self.assertIsNone(self.authenticate("Bearer service-key-2"))


class TestPendingTxRedaction(SafeTestCaseMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.owner = Account.create()
        self.safe_address = self.deploy_test_safe(owners=[self.owner.address]).address
        self.chain_id = self.ethereum_client.get_chain_id()
        self.pending_tx = MultisigTransactionFactory(
            safe=self.safe_address,
            ethereum_tx=None,
            data=SRC20_BARE_TRANSFER,
            trusted=True,
        )
        MultisigConfirmationFactory(
            multisig_transaction=self.pending_tx,
            force_sign_with_account=self.owner,
        )
        self.executed_tx = MultisigTransactionFactory(
            safe=self.safe_address, data=SRC20_BARE_TRANSFER, trusted=True
        )

    def owner_permit(self, safe: str | None = None, signer: Account | None = None):
        signer = signer or self.owner
        return "Bearer " + build_permit_token(
            signer, signer.address, safe or self.safe_address, self.chain_id
        )

    def get(self, url: str, authorization: str | None = None):
        headers = {"HTTP_AUTHORIZATION": authorization} if authorization else {}
        return self.client.get(url, format="json", **headers)

    def head(self, url: str, authorization: str | None = None):
        headers = {"HTTP_AUTHORIZATION": authorization} if authorization else {}
        return self.client.head(url, **headers)

    def list_urls(self):
        return [
            reverse(
                f"{version}:history:multisig-transactions", args=(self.safe_address,)
            )
            for version in ("v1", "v2")
        ]

    def detail_urls(self, safe_tx_hash: str):
        return [
            reverse(f"{version}:history:multisig-transaction", args=(safe_tx_hash,))
            for version in ("v1", "v2")
        ]

    def rows_by_hash(self, response):
        return {row["safe_tx_hash"]: row for row in response.data["results"]}

    def assert_full(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = self.rows_by_hash(response)[self.pending_tx.safe_tx_hash]
        self.assertFalse(row["is_executed"])
        self.assertEqual(row["data"], to_0x_hex_str(SRC20_BARE_TRANSFER))
        self.assertIsNotNone(row["confirmations"][0]["signature"])

    def assert_redacted(self, response):
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = self.rows_by_hash(response)
        row = rows[self.pending_tx.safe_tx_hash]
        # Required shape b: no `is_executed` and no calldata
        self.assertNotIn("is_executed", row)
        for field in ("data", "data_decoded", "origin", "signatures"):
            self.assertIsNone(row[field])
        self.assertEqual(len(row["confirmations"]), 1)
        self.assertEqual(row["confirmations"][0]["owner"], self.owner.address)
        self.assertIsNone(row["confirmations"][0]["signature"])
        self.assertEqual(row["to"], self.pending_tx.to)
        self.assertEqual(str(row["nonce"]), str(self.pending_tx.nonce))
        self.assertEqual(str(row["value"]), str(self.pending_tx.value))
        # Never a row with `is_executed` and without data (shapes a / a2)
        for any_row in rows.values():
            if "is_executed" in any_row:
                self.assertTrue(any_row["is_executed"])
                self.assertIsNotNone(any_row["data"])
        executed_row = rows[self.executed_tx.safe_tx_hash]
        self.assertTrue(executed_row["is_executed"])
        self.assertEqual(executed_row["data"], to_0x_hex_str(SRC20_BARE_TRANSFER))

    def test_mode_off_is_unchanged(self):
        for url in self.list_urls():
            self.assert_full(self.get(url))
        for url in self.detail_urls(self.pending_tx.safe_tx_hash):
            self.assertEqual(self.get(url).status_code, status.HTTP_200_OK)

    @override_settings(PENDING_TX_REDACTION_MODE="log")
    def test_mode_log_only_logs(self):
        with self.assertLogs(
            "safe_transaction_service.history.read_permit", level="INFO"
        ) as logs:
            for url in self.list_urls():
                self.assert_full(self.get(url))
            for url in self.detail_urls(self.pending_tx.safe_tx_hash):
                self.assertEqual(self.get(url).status_code, status.HTTP_200_OK)
        self.assertIn(self.pending_tx.safe_tx_hash, "".join(logs.output))
        self.assertNotIn(SRC20_BARE_TRANSFER.hex(), "".join(logs.output))

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_enforce_list(self):
        another_safe_owner = Account.create()
        another_safe = self.deploy_test_safe(owners=[another_safe_owner.address])
        non_owner = Account.create()
        for url in self.list_urls():
            with self.subTest(url=url):
                self.assert_redacted(self.get(url))
                self.assert_redacted(self.get(url, "Bearer garbage"))
                self.assert_redacted(self.get(url, self.owner_permit(signer=non_owner)))
                # Owner of another Safe, with a permit for that Safe
                self.assert_redacted(
                    self.get(
                        url,
                        self.owner_permit(
                            safe=another_safe.address, signer=another_safe_owner
                        ),
                    )
                )
                self.assert_full(self.get(url, self.owner_permit()))
                with override_settings(TX_SERVICE_READ_SERVICE_KEYS=["service-key"]):
                    self.assert_full(self.get(url, "Bearer service-key"))

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_enforce_head(self):
        # The test client strips HEAD bodies, gunicorn does not: check `response.data`
        for url in self.list_urls():
            with self.subTest(url=url):
                # Without a warm view cache HEAD fails here (no HEAD serializer)
                self.get(url)
                self.assert_redacted(self.head(url))
                self.assert_full(self.head(url, self.owner_permit()))

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_enforce_all_transactions(self):
        # Sent to the mempool but not mined: listed but not executed
        self.pending_tx = MultisigTransactionFactory(
            safe=self.safe_address, data=SRC20_BARE_TRANSFER, trusted=True
        )
        EthereumTx.objects.filter(tx_hash=self.pending_tx.ethereum_tx_id).update(
            block=None
        )
        MultisigConfirmationFactory(
            multisig_transaction=self.pending_tx,
            force_sign_with_account=self.owner,
        )
        for version in ("v1", "v2"):
            url = reverse(f"{version}:history:all-transactions", args=(self.safe_address,))
            with self.subTest(url=url):
                full = self.get(url, self.owner_permit())
                self.assert_full(full)
                redacted = self.get(url)
                self.assert_redacted(redacted)
                head = self.head(url)
                self.assert_redacted(head)
                # ETag must not be a hash of the hidden content
                self.assertEqual(head["ETag"], redacted["ETag"])
                self.assertNotEqual(head["ETag"], full["ETag"])

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_enforce_detail(self):
        missing_hash = to_0x_hex_str(fast_keccak_text("missing"))
        for missing_url, url, executed_url in zip(
            self.detail_urls(missing_hash),
            self.detail_urls(self.pending_tx.safe_tx_hash),
            self.detail_urls(self.executed_tx.safe_tx_hash),
            strict=True,
        ):
            with self.subTest(url=url):
                real_404 = self.get(missing_url)
                self.assertEqual(real_404.status_code, status.HTTP_404_NOT_FOUND)
                response = self.get(url)
                self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
                self.assertEqual(response.content, real_404.content)

                response = self.get(url, self.owner_permit())
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    response.data["data"], to_0x_hex_str(SRC20_BARE_TRANSFER)
                )

                response = self.get(executed_url)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertTrue(response.data["is_executed"])
                self.assertEqual(
                    response.data["data"], to_0x_hex_str(SRC20_BARE_TRANSFER)
                )

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_enforce_confirmations(self):
        url = reverse(
            "v1:history:multisig-transaction-confirmations",
            args=(self.pending_tx.safe_tx_hash,),
        )
        response = self.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["results"][0]["signature"])
        self.assertEqual(response.data["results"][0]["owner"], self.owner.address)
        response = self.get(url, self.owner_permit())
        self.assertIsNotNone(response.data["results"][0]["signature"])

    @override_settings(PENDING_TX_REDACTION_MODE="enforce")
    def test_view_cache_does_not_leak_across_callers(self):
        self.assertTrue(settings.CACHE_VIEW_DEFAULT_TIMEOUT)
        for url in self.list_urls():
            with self.subTest(url=url):
                CacheSafeTxsView(
                    CacheSafeTxsView.LIST_MULTISIGTRANSACTIONS_VIEW_CACHE_KEY,
                    self.safe_address,
                ).remove_cache()
                self.assert_full(self.get(url, self.owner_permit()))
                self.assert_redacted(self.get(url))

                CacheSafeTxsView(
                    CacheSafeTxsView.LIST_MULTISIGTRANSACTIONS_VIEW_CACHE_KEY,
                    self.safe_address,
                ).remove_cache()
                self.assert_redacted(self.get(url))
                # Cache stores the caller-independent full response
                cached = CacheSafeTxsView(
                    CacheSafeTxsView.LIST_MULTISIGTRANSACTIONS_VIEW_CACHE_KEY,
                    self.safe_address,
                ).get_cache_data(urlencode([]))
                self.assertIn(to_0x_hex_str(SRC20_BARE_TRANSFER), str(cached))
                self.assert_full(self.get(url, self.owner_permit()))

    @override_settings(
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
    )
    def test_owners_cache(self):
        request = mock.MagicMock(
            auth=ReadPrincipal(owner=self.owner.address, safe=self.safe_address)
        )
        with mock.patch(
            "safe_transaction_service.history.read_permit.get_safe_owners",
            return_value=[self.owner.address],
        ) as get_safe_owners_mock:
            self.assertTrue(is_entitled(request, self.safe_address))
            self.assertTrue(is_entitled(request, self.safe_address))
            self.assertFalse(is_entitled(request, Account.create().address))
            get_safe_owners_mock.assert_called_once_with(self.safe_address)

    def test_redact_pending_row(self):
        row = {
            "safe_tx_hash": "0x01",
            "is_executed": False,
            "data": "0x02",
            "data_decoded": {},
            "origin": "{}",
            "signatures": "0x03",
            "nonce": 1,
            "confirmations": [{"owner": "0x04", "signature": "0x05"}],
        }
        self.assertEqual(
            redact_pending_row(row),
            {
                "safe_tx_hash": "0x01",
                "data": None,
                "data_decoded": None,
                "origin": None,
                "signatures": None,
                "nonce": 1,
                "confirmations": [{"owner": "0x04", "signature": None}],
            },
        )
        self.assertFalse(row["is_executed"])  # Input is not mutated


class TestSrc20BareTransferRule(SafeTestCaseMixin, APITestCase):
    def multi_send_data(self, *datas: bytes) -> HexBytes:
        return self.multi_send.build_tx_data(
            [
                MultiSendTx(MultiSendOperation.CALL, Account.create().address, 0, data)
                for data in datas
            ]
        )

    def test_contains_bare_src20_transfer(self):
        salted = SRC20_BARE_TRANSFER + b"\x01"
        self.assertTrue(contains_bare_src20_transfer(SRC20_BARE_TRANSFER))
        self.assertFalse(contains_bare_src20_transfer(salted))
        self.assertFalse(contains_bare_src20_transfer(None))
        self.assertFalse(contains_bare_src20_transfer(b""))
        self.assertFalse(
            contains_bare_src20_transfer(b"\xa9\x05\x9c\xbb" + b"\x00" * 64)
        )
        self.assertTrue(
            contains_bare_src20_transfer(self.multi_send_data(b"", SRC20_BARE_TRANSFER))
        )
        self.assertFalse(contains_bare_src20_transfer(self.multi_send_data(salted)))
        self.assertTrue(
            contains_bare_src20_transfer(
                self.multi_send_data(self.multi_send_data(SRC20_BARE_TRANSFER))
            )
        )
        self.assertFalse(
            contains_bare_src20_transfer(bytes.fromhex("8d80ff0a") + b"\x01" * 10)
        )

    def propose(self, version: str, data: bytes, base_gas: int, nonce: int):
        owner = Account.create()
        safe = self.deploy_test_safe(owners=[owner.address])
        tx = {
            "to": Account.create().address,
            "value": 0,
            "data": to_0x_hex_str(data),
            "operation": 0,
            "nonce": nonce,
            "safeTxGas": 0,
            "baseGas": base_gas,
            "gasPrice": 0,
            "gasToken": "0x0000000000000000000000000000000000000000",
            "refundReceiver": "0x0000000000000000000000000000000000000000",
            "sender": owner.address,
        }
        safe_tx = safe.build_multisig_tx(
            tx["to"],
            tx["value"],
            data,
            tx["operation"],
            tx["safeTxGas"],
            tx["baseGas"],
            tx["gasPrice"],
            tx["gasToken"],
            tx["refundReceiver"],
            safe_nonce=tx["nonce"],
        )
        tx["contractTransactionHash"] = to_0x_hex_str(safe_tx.safe_tx_hash)
        tx["signature"] = to_0x_hex_str(
            owner.unsafe_sign_hash(safe_tx.safe_tx_hash)["signature"]
        )
        return self.client.post(
            reverse(f"{version}:history:multisig-transactions", args=(safe.address,)),
            format="json",
            data=tx,
        )

    def test_propose(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                with self.assertLogs(
                    "safe_transaction_service.history.serializers", level="WARNING"
                ):
                    response = self.propose(version, SRC20_BARE_TRANSFER, 0, 0)
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)

                with override_settings(SRC20_BARE_TRANSFER_RULE="enforce"):
                    response = self.propose(version, SRC20_BARE_TRANSFER, 0, 0)
                    self.assertEqual(
                        response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
                    )
                    self.assertIn(
                        "add a random salt to calldata or a random baseGas",
                        str(response.data),
                    )
                    response = self.propose(
                        version,
                        self.multi_send_data(SRC20_BARE_TRANSFER),
                        0,
                        0,
                    )
                    self.assertEqual(
                        response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY
                    )
                    response = self.propose(version, SRC20_BARE_TRANSFER, 12345, 0)
                    self.assertEqual(response.status_code, status.HTTP_201_CREATED)
                    response = self.propose(
                        version, SRC20_BARE_TRANSFER + b"\x07", 0, 0
                    )
                    self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class TestEventPayloadsWithoutData(SafeTestCaseMixin, APITestCase):
    @mock.patch.object(QueueService, "send_event")
    def test_payloads(self, send_event_mock):
        pending_tx = MultisigTransactionFactory(
            ethereum_tx=None, data=SRC20_BARE_TRANSFER
        )
        payload = build_event_payload(MultisigTransaction, pending_tx)[0]
        self.assertEqual(payload["type"], "PENDING_MULTISIG_TRANSACTION")
        self.assertNotIn("data", payload)

        confirmation = MultisigConfirmationFactory(multisig_transaction=pending_tx)
        payload = build_event_payload(MultisigConfirmation, confirmation)[0]
        self.assertEqual(payload["type"], "NEW_CONFIRMATION")
        self.assertNotIn("data", payload)

        executed_tx = MultisigTransactionFactory(data=SRC20_BARE_TRANSFER)
        payload = build_event_payload(MultisigTransaction, executed_tx)[0]
        self.assertEqual(payload["type"], "EXECUTED_MULTISIG_TRANSACTION")
        self.assertEqual(payload["data"], to_0x_hex_str(SRC20_BARE_TRANSFER))
