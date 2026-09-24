import logging
import time
from collections.abc import Iterator
from urllib.parse import urljoin

from safe_eth.eth.utils import fast_to_checksum_address

from safe_transaction_service.tokens.clients.base_client import BaseHTTPClient

from .exceptions import (
    HederaMirrorNodeClientException,
    HederaMirrorNodeNotFoundException,
    HederaMirrorNodeRateLimitException,
)

logger = logging.getLogger(__name__)


def hedera_account_id_to_long_zero_address(account_id: str) -> str:
    """
    Convert a Hedera account id (``shard.realm.num``) to the deterministic
    "long-zero" EVM address Hedera assigns to accounts without a custom EVM
    alias: 20 bytes = 4-byte shard + 8-byte realm + 8-byte entity num.

    :param account_id: e.g. ``"0.0.10127045"``
    :return: checksummed EVM address, e.g. ``"0x0000...009a86c5"``
    """
    shard, realm, num = (int(part) for part in account_id.split("."))
    hex_address = f"{shard:08x}{realm:016x}{num:016x}"
    return fast_to_checksum_address("0x" + hex_address)


class HederaMirrorNodeClient(BaseHTTPClient):
    """
    Thin REST client for the Hedera Mirror Node API. Works unmodified
    against the public free node or a commercial Mirror Node-API-compatible
    provider (e.g. Validation Cloud, Arkhia) by pointing ``base_url`` at it
    and supplying ``api_key``.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        rate_limit_rps: int = 10,
        request_timeout: int = 10,
    ):
        super().__init__(request_timeout=request_timeout)
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.min_request_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait_for = self.min_request_interval - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_at = time.monotonic()

    def _get(self, url: str) -> dict:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        for attempt in range(5):
            self._throttle()
            response = self.http_session.get(
                url, headers=headers, timeout=self.request_timeout
            )
            if response.ok:
                return response.json()
            if response.status_code == 404:
                raise HederaMirrorNodeNotFoundException(url)
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2**attempt))
                logger.warning(
                    "Hedera Mirror Node rate limited on %s, retrying in %.1fs",
                    url,
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            raise HederaMirrorNodeClientException(
                f"Hedera Mirror Node request to {url} failed with "
                f"status={response.status_code} body={response.text}"
            )
        raise HederaMirrorNodeRateLimitException(f"Exhausted retries calling {url}")

    def get_account(self, id_or_evm_address: str) -> dict | None:
        try:
            return self._get(
                urljoin(self.base_url, f"api/v1/accounts/{id_or_evm_address}")
            )
        except HederaMirrorNodeNotFoundException:
            return None

    def resolve_account_id(self, evm_address: str) -> str | None:
        account = self.get_account(evm_address)
        return account["account"] if account else None

    def resolve_evm_address(self, account_id: str) -> str:
        account = self.get_account(account_id)
        evm_address = account.get("evm_address") if account else None
        return evm_address or hedera_account_id_to_long_zero_address(account_id)

    def get_crypto_transfers(
        self, account_id: str, after_timestamp: str | None = None
    ) -> Iterator[dict]:
        params = f"account.id={account_id}&transactiontype=CRYPTOTRANSFER&order=asc&limit=100"
        if after_timestamp:
            params += f"&timestamp=gt:{after_timestamp}"
        url = urljoin(self.base_url, f"api/v1/transactions?{params}")
        while url:
            data = self._get(url)
            yield from data.get("transactions", [])
            next_link = (data.get("links") or {}).get("next")
            url = urljoin(self.base_url, next_link) if next_link else None

    def resolve_block_number(self, consensus_timestamp: str) -> int | None:
        data = self._get(
            urljoin(
                self.base_url, f"api/v1/blocks?timestamp={consensus_timestamp}&limit=1"
            )
        )
        blocks = data.get("blocks") or []
        return blocks[0]["number"] if blocks else None
