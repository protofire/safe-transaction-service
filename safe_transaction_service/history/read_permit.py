"""
Read access to pending (not executed) multisig transactions: owner ReadPermit and
service key authentication, entitlement check, response redaction and the bare SRC20
transfer rule for proposals.
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache as django_cache
from django.http import Http404
from django.utils.cache import patch_cache_control, patch_vary_headers

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from rest_framework.authentication import BaseAuthentication, get_authorization_header
from rest_framework.exceptions import ValidationError
from safe_eth.eth.eip712 import eip712_encode_hash
from safe_eth.eth.utils import fast_is_checksum_address
from safe_eth.safe.multi_send import MultiSend
from safe_eth.safe.safe_signature import SafeSignature, SafeSignatureType

from safe_transaction_service.utils.ethereum import get_chain_id
from safe_transaction_service.utils.serializers import get_safe_owners

from .models import MultisigTransaction

logger = logging.getLogger(__name__)

READ_PERMIT_FIELDS = {"owner", "safe", "chainId", "expiry", "sig"}
SRC20_TRANSFER_SELECTOR = bytes.fromhex("b10c99b5")
SRC20_TRANSFER_DATA_LENGTH = 68
MULTISEND_SELECTOR = bytes.fromhex("8d80ff0a")


@dataclass(frozen=True)
class ReadPrincipal:
    owner: ChecksumAddress | None = None
    safe: ChecksumAddress | None = None
    service: bool = False


def get_read_permit_hash(
    safe: ChecksumAddress, owner: ChecksumAddress, expiry: int, chain_id: int
) -> bytes:
    return eip712_encode_hash(
        {
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
                "name": settings.READ_PERMIT_DOMAIN_NAME,
                "version": "1",
                "chainId": chain_id,
            },
            "message": {"safe": safe, "owner": owner, "expiry": expiry},
        }
    )


def _is_service_key(token: str) -> bool:
    candidate = token.encode()
    return any(
        hmac.compare_digest(candidate, key.encode())
        for key in settings.TX_SERVICE_READ_SERVICE_KEYS
    )


def _parse_read_permit(token: str) -> ReadPrincipal | None:
    permit = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
    if not isinstance(permit, dict) or set(permit) != READ_PERMIT_FIELDS:
        return None
    owner, safe = permit["owner"], permit["safe"]
    chain_id, expiry, sig = permit["chainId"], permit["expiry"], permit["sig"]
    if not (fast_is_checksum_address(owner) and fast_is_checksum_address(safe)):
        return None
    if type(chain_id) is not int or type(expiry) is not int or type(sig) is not str:
        return None
    if chain_id != get_chain_id():
        return None
    ttl = expiry - int(time.time())
    if ttl <= 0 or ttl > settings.READ_PERMIT_MAX_TTL_SECONDS:
        return None
    signature = HexBytes(sig)
    if len(signature) != 65:
        return None
    safe_signatures = SafeSignature.parse_signature(
        signature, get_read_permit_hash(safe, owner, expiry, chain_id)
    )
    # shortcut: EIP-1271 contract owners not supported, add on request
    if (
        len(safe_signatures) != 1
        or safe_signatures[0].signature_type != SafeSignatureType.EOA
        or safe_signatures[0].owner != owner
    ):
        return None
    return ReadPrincipal(owner=owner, safe=safe)


class OwnerPermitAuthentication(BaseAuthentication):
    """
    `Authorization: Bearer <credential>` where credential is a service key or a base64url
    encoded JSON ReadPermit. Fail-soft: a missing or invalid credential leaves the caller
    anonymous, it never causes 401/403. No Django user is created, the principal is
    available as `request.auth`.
    """

    keyword = "Bearer"

    def authenticate(self, request):
        try:
            parts = get_authorization_header(request).split()
            if len(parts) != 2 or parts[0].lower() != self.keyword.lower().encode():
                return None
            token = parts[1].decode()
            if _is_service_key(token):
                principal = ReadPrincipal(service=True)
            else:
                principal = _parse_read_permit(token)
        except Exception:  # Untrusted input, any failure means anonymous
            return None
        return (AnonymousUser(), principal) if principal else None


def _get_current_owners(safe: ChecksumAddress) -> list[ChecksumAddress]:
    cache_key = f"read_permit_owners:{safe}"
    owners = django_cache.get(cache_key)
    if owners is None:
        try:
            owners = get_safe_owners(safe)
        except ValidationError:
            return []  # Fail closed, do not cache the failure
        django_cache.set(cache_key, owners, settings.READ_PERMIT_OWNERS_CACHE_SECONDS)
    return owners


def is_entitled(request, safe: ChecksumAddress | None) -> bool:
    principal = request.auth
    if not isinstance(principal, ReadPrincipal):
        return False
    if principal.service:
        return True
    return principal.safe == safe and principal.owner in _get_current_owners(safe)


def redact_pending_row(row: dict[str, Any]) -> dict[str, Any]:
    # `is_executed` must go together with `data`: SDKs then fail loudly instead of
    # signing a wrong safeTxHash or executing another tx with the same nonce
    redacted = {key: value for key, value in row.items() if key != "is_executed"}
    redacted.update(data=None, data_decoded=None, origin=None, signatures=None)
    redacted["confirmations"] = [
        {**confirmation, "signature": None}
        for confirmation in row.get("confirmations") or []
    ]
    return redacted


def get_weak_etag(results) -> str:
    return 'W/"' + hashlib.md5(str(results).encode()).hexdigest() + '"'


def patch_pending_tx_cache_headers(response):
    # The body depends on `Authorization`: shared caches (proxy, CDN) must not store it
    if settings.PENDING_TX_REDACTION_MODE != "off":
        patch_vary_headers(response, ("Authorization",))
        patch_cache_control(response, private=True, no_store=True)
    return response


class PendingTxListRedactionMixin:
    """
    Redacts unexecuted multisig rows of paginated responses for non entitled callers.
    It runs in `finalize_response`, after the view cache, so the cache always stores the
    full caller-independent response.
    """

    def redact_results(
        self, request, results: list[dict[str, Any]], enforce: bool
    ) -> list[dict[str, Any]]:
        entitled_by_safe: dict[ChecksumAddress, bool] = {}
        redacted_hashes = []
        new_results = []
        for row in results:
            if row.get("is_executed") is False:
                safe = row["safe"]
                if safe not in entitled_by_safe:
                    entitled_by_safe[safe] = is_entitled(request, safe)
                if not entitled_by_safe[safe]:
                    redacted_hashes.append(row["safe_tx_hash"])
                    if enforce:
                        row = redact_pending_row(row)
            new_results.append(row)
        if redacted_hashes:
            logger.info(
                "%s pending multisig txs safe_tx_hashes=%s",
                "Redacted" if enforce else "Would redact",
                redacted_hashes,
            )
        return new_results

    def finalize_response(self, request, response, *args, **kwargs):
        mode = settings.PENDING_TX_REDACTION_MODE
        # HEAD runs the full `get()` and gunicorn does not strip the body, so redact it too
        if (
            mode != "off"
            and request.method in ("GET", "HEAD")
            and response.status_code == 200
        ):
            enforce = mode == "enforce"
            results = self.redact_results(request, response.data["results"], enforce)
            if enforce:
                response.data = {**response.data, "results": results}
                if response.has_header("ETag"):
                    response["ETag"] = get_weak_etag(results)
        return patch_pending_tx_cache_headers(
            super().finalize_response(request, response, *args, **kwargs)
        )


class PendingTxConfirmationsRedactionMixin(PendingTxListRedactionMixin):
    """
    Nulls `signature` of confirmations of an unexecuted multisig tx for non entitled callers
    """

    def redact_results(
        self, request, results: list[dict[str, Any]], enforce: bool
    ) -> list[dict[str, Any]]:
        safe_tx_hash = self.kwargs["safe_tx_hash"]
        multisig_tx = (
            MultisigTransaction.objects.select_related("ethereum_tx")
            .filter(safe_tx_hash=safe_tx_hash)
            .first()
        )
        if (multisig_tx and multisig_tx.executed) or is_entitled(
            request, multisig_tx.safe if multisig_tx else None
        ):
            return results
        logger.info(
            "%s confirmation signatures of pending multisig tx safe_tx_hash=%s",
            "Redacted" if enforce else "Would redact",
            safe_tx_hash,
        )
        return [{**row, "signature": None} for row in results] if enforce else results


class PendingTxDetailRedactionMixin:
    """
    A non entitled caller gets the same 404 for an unexecuted multisig tx as for a missing one
    """

    def get_object(self):
        multisig_tx = super().get_object()
        mode = settings.PENDING_TX_REDACTION_MODE
        if (
            mode != "off"
            and not multisig_tx.executed
            and not is_entitled(self.request, multisig_tx.safe)
        ):
            logger.info(
                "%s pending multisig tx safe_tx_hash=%s",
                "Hidden" if mode == "enforce" else "Would hide",
                multisig_tx.safe_tx_hash,
            )
            if mode == "enforce":
                raise Http404(
                    f"No {MultisigTransaction._meta.object_name} matches the given query."
                )
        return multisig_tx

    def finalize_response(self, request, response, *args, **kwargs):
        return patch_pending_tx_cache_headers(
            super().finalize_response(request, response, *args, **kwargs)
        )


def contains_bare_src20_transfer(data: bytes | None) -> bool:
    """
    :return: `True` if `data` is a SRC20 `transfer` without extra calldata, directly or
        inside a (nested) MultiSend
    """
    if not data:
        return False
    if data[:4] == SRC20_TRANSFER_SELECTOR and len(data) == SRC20_TRANSFER_DATA_LENGTH:
        return True
    if data[:4] != MULTISEND_SELECTOR:
        return False
    try:
        return any(
            contains_bare_src20_transfer(multisend_tx.data)
            for multisend_tx in MultiSend.from_transaction_data(data)
        )
    except Exception:  # Malformed MultiSend calldata from an untrusted proposal
        return False
