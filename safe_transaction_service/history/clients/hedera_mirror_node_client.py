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

# Mirror Node's own documented hard ceiling for the `limit` query parameter
# (default 25, minimum 1, maximum 100 — confirmed against the public
# OpenAPI spec). Using the maximum minimizes the number of pages fetched;
# this is not a tunable knob, since any value above it would just be
# rejected/clamped by Mirror Node itself.
MIRROR_NODE_PAGE_SIZE = 100


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
        max_retries: int = 5,
    ):
        super().__init__(request_timeout=request_timeout)
        self.base_url = base_url.rstrip("/") + "/"
        self.api_key = api_key
        self.min_request_interval = 1.0 / rate_limit_rps if rate_limit_rps > 0 else 0.0
        self.max_retries = max_retries
        self._last_request_at: float = 0.0

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        wait_for = self.min_request_interval - elapsed
        if wait_for > 0:
            time.sleep(wait_for)
        self._last_request_at = time.monotonic()

    def _redact_url(self, url: str) -> str:
        """
        Strip ``self.base_url`` from a URL before it's logged or included in
        an exception message. Some Mirror Node providers (see
        ``HEDERA_MIRROR_NODE_URL``'s own documentation) embed their API key
        directly in the URL rather than in a header.

        :param url: A full URL built from ``self.base_url``.
        :return: The same URL with any ``self.base_url`` prefix replaced by
            a fixed, non-secret placeholder.
        """
        if url.startswith(self.base_url):
            return "<mirror-node>/" + url[len(self.base_url) :]
        return "<mirror-node>/..."

    def _get(self, url: str) -> dict:
        headers = {"x-api-key": self.api_key} if self.api_key else {}
        for attempt in range(self.max_retries):
            self._throttle()
            response = self.http_session.get(
                url, headers=headers, timeout=self.request_timeout
            )
            if response.ok:
                return response.json()
            if response.status_code == 404:
                raise HederaMirrorNodeNotFoundException(self._redact_url(url))
            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", 2**attempt))
                logger.warning(
                    "Hedera Mirror Node rate limited on %s, retrying in %.1fs",
                    self._redact_url(url),
                    retry_after,
                )
                time.sleep(retry_after)
                continue
            raise HederaMirrorNodeClientException(
                f"Hedera Mirror Node request to {self._redact_url(url)} failed "
                f"with status={response.status_code} body={response.text}"
            )
        raise HederaMirrorNodeRateLimitException(
            f"Exhausted retries calling {self._redact_url(url)}"
        )

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
        params = (
            f"account.id={account_id}&transactiontype=CRYPTOTRANSFER"
            f"&order=asc&limit={MIRROR_NODE_PAGE_SIZE}"
        )
        if after_timestamp:
            params += f"&timestamp=gt:{after_timestamp}"
        url = urljoin(self.base_url, f"api/v1/transactions?{params}")
        while url:
            data = self._get(url)
            yield from data.get("transactions", [])
            next_link = (data.get("links") or {}).get("next")
            if not next_link:
                url = None
            else:
                # Mirror Node's own `next` link is an absolute path (e.g.
                # "/api/v1/transactions?..."), relative to its own API root
                # — it has no knowledge of a commercial provider's
                # URL-embedded-key prefix (e.g. "/v1/<api-key>/"). Joining
                # it as-is would have urljoin treat the leading "/" as
                # host-root-relative, silently dropping that prefix (and
                # the key with it) from every page after the first.
                # Stripping the leading "/" first makes it join as a
                # relative path instead, preserving self.base_url's full
                # prefix.
                url = urljoin(self.base_url, next_link.lstrip("/"))

    def resolve_block_number(self, consensus_timestamp: str) -> int | None:
        # A bare `timestamp=<value>` filter matches only a block whose `to`
        # boundary exactly equals `<value>` (i.e. only when the target
        # transaction happens to be the very last one in its block) and
        # silently returns nothing otherwise — confirmed against the real
        # Mirror Node API, not just documentation. `gte:` + `order=asc` +
        # `limit=1` reliably returns the first (and only) block whose range
        # actually contains the timestamp, regardless of the transaction's
        # position within it.
        data = self._get(
            urljoin(
                self.base_url,
                f"api/v1/blocks?timestamp=gte:{consensus_timestamp}&order=asc&limit=1",
            )
        )
        blocks = data.get("blocks") or []
        return blocks[0]["number"] if blocks else None
