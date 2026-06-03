import datetime
from collections import OrderedDict
from collections.abc import Iterator, Sequence
from logging import getLogger
from typing import NamedTuple

from django.db.models import QuerySet
from django.db.models.query import EmptyQuerySet

from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from safe_eth.eth import EthereumClient
from safe_eth.eth.utils import fast_to_checksum_address
from safe_eth.util.util import to_0x_hex_str
from web3 import Web3
from web3.contract.contract import ContractEvent
from web3.types import EventData, FilterParams, LogReceipt

from ...tokens.constants import get_src20_keyless_placeholder_logs
from ...tokens.models import Token
from ...utils.utils import FixedSizeDict
from ..models import (
    SRC20_TRANSFER_TOPIC,
    IndexingStatus,
    SafeContract,
    SafeRelevantTransaction,
    SRC20Transfer,
    TokenTransfer,
)
from .events_indexer import EventsIndexer

logger = getLogger(__name__)


class Src20DirectoryLookupError(Exception):
    """
    Raised when the directory precompile lookup itself FAILS (RPC error, pruned/archive
    state unavailable, etc.). This is deliberately distinct from a successful lookup that
    returns "no key": a failure must never be read as "keyless", because that would let the
    keyless `÷m` path under-count a genuinely keyed transfer. Callers treat this as
    "uncertain" and keep every log.
    """


# Seismic "directory" precompile exposing per-address encryption-key state. Used to resolve
# a party's `keyHash` at a historical block when a SRC20 group contains keyed (`encryptKeyHash
# != 0`) logs, so the recipient/sender log can be anchored exactly. `keyHash(address)` is the
# same value the token writes into the `Transfer` event's `encryptKeyHash` topic.
SRC20_DIRECTORY_PRECOMPILE_ADDRESS = Web3.to_checksum_address(
    "0x1000000000000000000000000000000000000004"
)
SRC20_DIRECTORY_ABI = [
    {
        "name": "checkHasKey",  # selector 0x8b2f9fd1
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "addr", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "keyHash",  # selector 0xeefbaf18
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "addr", "type": "address"}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]


# Transfer(address indexed from, address indexed to, bytes32 indexed encryptKeyHash, bytes encryptedAmount)
SRC20_TRANSFER_EVENT_ABI = {
    "anonymous": False,
    "name": "Transfer",
    "type": "event",
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": True, "name": "encryptKeyHash", "type": "bytes32"},
        {"indexed": False, "name": "encryptedAmount", "type": "bytes"},
    ],
}


class AddressesCache(NamedTuple):
    addresses: set[ChecksumAddress]
    last_checked: datetime.datetime | None


class Src20EventsIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = cls.get_new_instance()
        return cls.instance

    @classmethod
    def get_new_instance(cls) -> "Src20EventsIndexer":
        from django.conf import settings

        return Src20EventsIndexer(
            EthereumClient(settings.ETHEREUM_NODE_URL),
            eth_erc20_load_addresses_chunk_size=settings.ETH_ERC20_LOAD_ADDRESSES_CHUNK_SIZE,
        )

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class Src20EventsIndexer(EventsIndexer):
    """
    Indexes SRC20 confidential token `Transfer` events.

    `Transfer(address indexed from, address indexed to, bytes32 indexed encryptKeyHash, bytes encryptedAmount)`

    The event topic differs from the canonical ERC20/721 `Transfer` topic, so it's indexed
    independently from `Erc20EventsIndexer`. As `from`/`to` are indexed, both incoming and
    outgoing transfers for monitored Safes are captured. The amount is encrypted on-chain,
    so transfers are stored with no readable value (exposed as `value=0`).
    """

    # The emitter is the SRC20 token contract (not the Safe), so we cannot filter logs by
    # the monitored Safe addresses at the node. We fetch all SRC20 `Transfer` logs by topic
    # and filter `from`/`to` against the monitored addresses ourselves.
    IGNORE_ADDRESSES_ON_LOG_FILTER = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._processed_element_cache = FixedSizeDict(maxlen=40_000)  # Around 3MiB
        self.addresses_cache: AddressesCache | None = None
        self.eth_erc20_load_addresses_chunk_size = kwargs.get(
            "eth_erc20_load_addresses_chunk_size", 500_000
        )

    @property
    def contract_events(self) -> list[ContractEvent]:
        """
        :return: Web3 ContractEvent used to decode SRC20 `Transfer` logs
        """
        return [Web3().eth.contract(abi=[SRC20_TRANSFER_EVENT_ABI]).events.Transfer()]

    @property
    def database_field(self):
        # Not used: block tracking is delegated to `IndexingStatus` (see overrides below)
        return "src20_block_number"

    @property
    def database_queryset(self) -> QuerySet:
        return SafeContract.objects.all()

    @staticmethod
    def _topic_to_address(topic) -> ChecksumAddress:
        """
        :param topic: 32-byte indexed topic holding a left-padded address
        :return: Checksummed address
        """
        return fast_to_checksum_address(bytes(HexBytes(topic)[-20:]))

    def _do_node_query(
        self,
        addresses: set[ChecksumAddress],
        from_block_number: int,
        to_block_number: int,
    ) -> list[LogReceipt]:
        """
        Fetch every SRC20 `Transfer` log in the block range and keep only those where a
        monitored Safe is the sender or the recipient.
        """
        parameters: FilterParams = {
            "fromBlock": from_block_number,
            "toBlock": to_block_number,
            "topics": [to_0x_hex_str(SRC20_TRANSFER_TOPIC)],
        }
        with self.auto_adjust_block_limit(from_block_number, to_block_number):
            log_receipts = self.ethereum_client.slow_w3.eth.get_logs(parameters)

        result = []
        for log_receipt in log_receipts:
            topics = log_receipt["topics"]
            if len(topics) < 3:
                continue
            _from = self._topic_to_address(topics[1])
            to = self._topic_to_address(topics[2])
            if _from in addresses or to in addresses:
                result.append(log_receipt)
        return result

    def _process_decoded_element(self, decoded_element: EventData) -> None:
        """
        Not used as `process_elements` is redefined using custom processors
        """
        pass

    @staticmethod
    def _log_key_hash(decoded_element: EventData) -> bytes:
        """
        :return: The `encryptKeyHash` of a decoded `Transfer` event as 32 raw bytes
            (empty/zero hashes are normalized to all-zero bytes via `int.from_bytes`).
        """
        return bytes(HexBytes(decoded_element["args"].get("encryptKeyHash") or b""))

    @classmethod
    def _is_keyed(cls, decoded_element: EventData) -> bool:
        return int.from_bytes(cls._log_key_hash(decoded_element), "big") != 0

    def _keyless_placeholder_logs_map(
        self, token_addresses: set[ChecksumAddress]
    ) -> dict[ChecksumAddress, int]:
        """
        :return: For each token, the number of `encryptKeyHash == 0` logs a single transfer
            emits when both parties are keyless (the keyless/self divisor). The value stored
            on the `Token` row is authoritative once the token is registered; tokens not yet
            registered fall back to the config map (both yield identical values).
        """
        stored = dict(
            Token.objects.filter(address__in=token_addresses).values_list(
                "address", "src20_keyless_placeholder_logs_per_transfer"
            )
        )
        return {
            address: stored.get(address) or get_src20_keyless_placeholder_logs(address)
            for address in token_addresses
        }

    def _key_hash_at_block(
        self,
        address: ChecksumAddress,
        block_number: int,
        cache: dict[tuple[ChecksumAddress, int], bytes | None],
    ) -> bytes | None:
        """
        Resolve `keyHash(address)` from the directory precompile at a historical block.

        :return: 32 raw bytes if the address has a key at that block, or `None` if the
            directory CONFIRMS the address is keyless.
        :raises Src20DirectoryLookupError: if the lookup itself fails (RPC error, pruned
            state). A failure must not be conflated with a confirmed "no key" — see the
            exception docstring. Callers keep every log when this is raised.
        """
        key = (address, block_number)
        if key in cache:
            return cache[key]

        try:
            directory = self.ethereum_client.w3.eth.contract(
                address=SRC20_DIRECTORY_PRECOMPILE_ADDRESS, abi=SRC20_DIRECTORY_ABI
            )
            result: bytes | None = None
            if directory.functions.checkHasKey(address).call(
                block_identifier=block_number
            ):
                key_hash = bytes(
                    HexBytes(
                        directory.functions.keyHash(address).call(
                            block_identifier=block_number
                        )
                    )
                )
                if int.from_bytes(key_hash, "big"):
                    result = key_hash
        except Exception as exc:
            # Do NOT cache and do NOT return None: a failed lookup is "uncertain", not
            # "keyless". Returning None here would let the keyless `÷m` path under-count a
            # genuinely keyed transfer (and make the dedupe command delete rows the indexer
            # kept when it later runs against a pruned node).
            logger.warning(
                "SRC20 directory lookup failed for address=%s block=%d; keeping all logs",
                address,
                block_number,
                exc_info=True,
            )
            raise Src20DirectoryLookupError(address, block_number) from exc

        cache[key] = result
        return result

    def _keyless_representatives(
        self,
        zero_logs: list[EventData],
        keyless_logs_per_transfer: int,
        group_logs: list[EventData],
    ) -> list[EventData]:
        """
        Collapse the `encryptKeyHash == 0` placeholders of a group to one log per logical
        transfer by keeping every `m`-th (the recipient — last placeholder of each transfer).
        Keeps every keyless placeholder (never under-counts) if the count is not divisible
        by `m`. Provider (non-zero `kh`) logs are never returned — only `zero_logs`.
        """
        if keyless_logs_per_transfer <= 1:
            return zero_logs
        if zero_logs and len(zero_logs) % keyless_logs_per_transfer != 0:
            logger.warning(
                "SRC20: %d keyless logs not divisible by placeholder count %d "
                "(tx=%s token=%s); keeping all keyless logs to avoid mis-dividing",
                len(zero_logs),
                keyless_logs_per_transfer,
                to_0x_hex_str(group_logs[0]["transactionHash"]),
                group_logs[0]["address"],
            )
            # Keep only the keyless placeholders, never the provider logs (which would be
            # stored as bogus extra transfers). `group_logs` is unused here on purpose.
            return zero_logs
        return zero_logs[keyless_logs_per_transfer - 1 :: keyless_logs_per_transfer]

    def _select_representatives(
        self,
        group_logs: list[EventData],
        keyless_logs_per_transfer: int,
        from_: ChecksumAddress,
        to: ChecksumAddress,
        key_hash_cache: dict[tuple[ChecksumAddress, int], bytes | None],
    ) -> list[EventData]:
        """
        Pick the representative logs for one `(tx, token, from, to)` group so that the number
        of stored rows equals the number of logical transfers. See module docstring / plan
        for the full rule. Guarantees a non-empty group never yields zero representatives.
        """
        group_logs = sorted(group_logs, key=lambda element: element["logIndex"])
        zero_logs = [e for e in group_logs if not self._is_keyed(e)]
        nonzero_logs = [e for e in group_logs if self._is_keyed(e)]

        try:
            reps = self._compute_representatives(
                group_logs,
                zero_logs,
                nonzero_logs,
                keyless_logs_per_transfer,
                from_,
                to,
                key_hash_cache,
            )
        except Src20DirectoryLookupError:
            # Could not determine whether a party is keyed, so we cannot safely divide.
            # Keep every log (over-count is acceptable; under-count is not).
            logger.warning(
                "SRC20: directory lookup failed for group tx=%s token=%s from=%s to=%s "
                "size=%d; keeping all logs",
                to_0x_hex_str(group_logs[0]["transactionHash"]),
                group_logs[0]["address"],
                from_,
                to,
                len(group_logs),
            )
            return group_logs

        if group_logs and not reps:
            # Never-empty guard: a real transfer must never be dropped (key rotation,
            # archive-state mismatch, or an unexpected emission shape) — keep every log.
            logger.warning(
                "SRC20: empty selection for group tx=%s token=%s from=%s to=%s size=%d; "
                "keeping all logs",
                to_0x_hex_str(group_logs[0]["transactionHash"]),
                group_logs[0]["address"],
                from_,
                to,
                len(group_logs),
            )
            return group_logs
        return reps

    def _compute_representatives(
        self,
        group_logs: list[EventData],
        zero_logs: list[EventData],
        nonzero_logs: list[EventData],
        keyless_logs_per_transfer: int,
        from_: ChecksumAddress,
        to: ChecksumAddress,
        key_hash_cache: dict[tuple[ChecksumAddress, int], bytes | None],
    ) -> list[EventData]:
        if not nonzero_logs:
            # Fully keyless (the only case on this testnet today): divide the placeholders.
            return self._keyless_representatives(
                zero_logs, keyless_logs_per_transfer, group_logs
            )

        distinct_hashes = {self._log_key_hash(e) for e in nonzero_logs}
        if not zero_logs and len(distinct_hashes) == 1 and from_ != to:
            # Recipient-only keyed token (e.g. 0xDDe870…): for from != to a single transfer's
            # viewer hashes are all distinct, so identical-hash logs are separate transfers.
            # Exact count with no directory call.
            return nonzero_logs

        # A party may be keyed (possibly mixed with keyless placeholders or providers):
        # anchor on the directory's keyHash at the tx's block.
        block_number = group_logs[0]["blockNumber"]
        kh_to = self._key_hash_at_block(to, block_number, key_hash_cache)
        kh_from = self._key_hash_at_block(from_, block_number, key_hash_cache)

        if from_ == to:
            anchor = kh_to or kh_from
            if anchor is None:
                # Self-transfer but both parties keyless: the non-zero logs are providers;
                # count the keyless placeholders instead.
                return self._keyless_representatives(
                    zero_logs, keyless_logs_per_transfer, group_logs
                )
            matched = [e for e in nonzero_logs if self._log_key_hash(e) == anchor]
            if not matched or len(matched) % keyless_logs_per_transfer != 0:
                logger.warning(
                    "SRC20: self-transfer keyed mismatch (tx=%s token=%s matched=%d m=%d); "
                    "keeping all logs",
                    to_0x_hex_str(group_logs[0]["transactionHash"]),
                    group_logs[0]["address"],
                    len(matched),
                    keyless_logs_per_transfer,
                )
                return group_logs
            return matched[keyless_logs_per_transfer - 1 :: keyless_logs_per_transfer]

        if kh_to is not None:
            return [e for e in nonzero_logs if self._log_key_hash(e) == kh_to]
        if kh_from is not None:
            return [e for e in nonzero_logs if self._log_key_hash(e) == kh_from]

        # Both parties keyless: the non-zero logs are providers — ignore them and count the
        # keyless placeholders only.
        return self._keyless_representatives(
            zero_logs, keyless_logs_per_transfer, group_logs
        )

    def events_to_src20_transfer(
        self, decoded_elements: Sequence[EventData]
    ) -> Iterator[SRC20Transfer]:
        """
        A single SRC20 transfer emits one `Transfer` log per "viewer" of the encrypted
        amount (sender + recipient placeholders, plus one per registered provider), so a
        raw 1:1 log→row mapping over-counts. Group the decoded events by
        `(tx, token, from, to)`, count the logical transfers per group, and yield only that
        many representative logs (keeping their real, distinct `log_index`).
        """
        groups: OrderedDict[tuple, list[EventData]] = OrderedDict()
        for decoded_element in decoded_elements:
            args = decoded_element["args"]
            if "from" not in args or "to" not in args:
                continue
            group_key = (
                decoded_element["transactionHash"],
                decoded_element["address"],
                args["from"],
                args["to"],
            )
            groups.setdefault(group_key, []).append(decoded_element)

        token_addresses = {address for (_tx, address, _from, _to) in groups}
        keyless_logs_per_transfer = self._keyless_placeholder_logs_map(token_addresses)
        key_hash_cache: dict[tuple[ChecksumAddress, int], bytes | None] = {}

        for (_tx, token, from_, to), group_logs in groups.items():
            for representative in self._select_representatives(
                group_logs,
                keyless_logs_per_transfer[token],
                from_,
                to,
                key_hash_cache,
            ):
                try:
                    yield SRC20Transfer.from_decoded_event(representative)
                except ValueError:
                    pass

    def events_to_safe_relevant_transaction(
        self, decoded_elements: Sequence[EventData]
    ) -> Iterator[SafeRelevantTransaction]:
        for decoded_element in decoded_elements:
            try:
                yield from SafeRelevantTransaction.from_erc20_721_event(decoded_element)
            except ValueError:
                pass

    def process_elements(
        self, log_receipts: Sequence[LogReceipt]
    ) -> list[TokenTransfer]:
        """
        Process all SRC20 `Transfer` log receipts found by `find_relevant_elements`

        :param log_receipts: Raw logs to decode and store in database
        :return: List of stored `SRC20Transfer` (range to limit memory usage)
        """
        not_processed_log_receipts = self._filter_not_processed_log_receipts(
            log_receipts
        )
        tx_hashes = OrderedDict.fromkeys(
            [
                log_receipt["transactionHash"]
                for log_receipt in not_processed_log_receipts
            ]
        ).keys()
        if not tx_hashes:
            return []

        self._prefetch_ethereum_txs(tx_hashes)
        decoded_elements: list[EventData] = self.decode_elements(
            not_processed_log_receipts
        )

        logger.debug("Storing SRC20Transfer objects")
        result_src20 = SRC20Transfer.objects.bulk_create_from_generator(
            self.events_to_src20_transfer(decoded_elements),
            ignore_conflicts=True,
        )
        logger.debug("Stored %d SRC20 Events", result_src20)

        result_safe_relevant_transaction = (
            SafeRelevantTransaction.objects.bulk_create_from_generator(
                self.events_to_safe_relevant_transaction(decoded_elements),
                ignore_conflicts=True,
            )
        )
        logger.debug(
            "Stored %d Safe Relevant Transactions", result_safe_relevant_transaction
        )

        # Register the SRC20 token(s) so the tokens API and `tokenInfo` resolve, and the
        # transfer is exposed as `SRC20_TRANSFER`
        for token_address in {
            decoded_element["address"] for decoded_element in decoded_elements
        }:
            # Token registration must never halt indexing: transfers are already stored, and
            # the block cursor only advances if `process_elements` returns. A failure here
            # (e.g. unexpected DB/RPC error) would otherwise re-fail the same block forever.
            try:
                Token.objects.create_src20_from_blockchain(token_address)
            except Exception:
                logger.warning(
                    "Could not register SRC20 token=%s", token_address, exc_info=True
                )

        self._mark_log_receipts_processed(not_processed_log_receipts)
        return range(result_src20)  # Hack to avoid keeping models in RAM

    def get_almost_updated_addresses(
        self, current_block_number: int
    ) -> set[ChecksumAddress]:
        """
        :param current_block_number:
        :return: Monitored addresses to be processed (every Safe), cached between runs
        """
        logger.debug("%s: Retrieving monitored addresses", self.__class__.__name__)

        last_checked: datetime.datetime | None
        if self.addresses_cache:
            query = self.database_queryset.filter(
                created__gte=self.addresses_cache.last_checked
            )
            addresses = self.addresses_cache.addresses
            last_checked = self.addresses_cache.last_checked
        else:
            query = self.database_queryset.all()
            addresses = set()
            last_checked = None

        created: datetime.datetime | None = None
        for i, (created, address) in enumerate(
            query.values_list("created", "address")
            .order_by("created")
            .iterator(chunk_size=self.eth_erc20_load_addresses_chunk_size)
        ):
            addresses.add(address)
            if i % self.eth_erc20_load_addresses_chunk_size == 0:
                self.addresses_cache = AddressesCache(addresses, created)

        if created:
            last_checked = created

        if last_checked:
            self.addresses_cache = AddressesCache(addresses, last_checked)

        logger.debug("%s: Retrieved monitored addresses", self.__class__.__name__)
        return addresses

    def get_not_updated_addresses(self, current_block_number: int) -> EmptyQuerySet:
        return self.database_queryset.none()

    def get_from_block_number(
        self, addresses: set[ChecksumAddress] | None = None
    ) -> int | None:
        return IndexingStatus.objects.get_src20_indexing_status().block_number

    def update_monitored_addresses(
        self,
        addresses: set[ChecksumAddress],
        from_block_number: int,
        to_block_number: int,
    ) -> bool:
        new_to_block_number = to_block_number + 1
        updated = IndexingStatus.objects.set_src20_indexing_status(
            new_to_block_number, from_block_number=from_block_number
        )
        if not updated:
            logger.warning(
                "%s: Possible reorg - Cannot update src20 indexing status "
                "from-block-number=%d to-block-number=%d",
                self.__class__.__name__,
                from_block_number,
                to_block_number,
            )
        return updated
