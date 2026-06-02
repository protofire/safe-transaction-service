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

    def events_to_src20_transfer(
        self, decoded_elements: Sequence[EventData]
    ) -> Iterator[SRC20Transfer]:
        for decoded_element in decoded_elements:
            try:
                yield SRC20Transfer.from_decoded_event(decoded_element)
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
