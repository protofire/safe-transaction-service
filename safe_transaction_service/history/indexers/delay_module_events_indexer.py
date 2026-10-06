from collections.abc import Sequence
from functools import cached_property
from logging import getLogger

from django.db.models import QuerySet

from eth_typing import ChecksumAddress
from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS
from web3.contract.contract import ContractEvent
from web3.types import EventData, LogReceipt

from ..models import DelayModuleTransaction, EthereumBlock, IndexingStatus
from .events_indexer import EventsIndexer

logger = getLogger(__name__)

# Zodiac Delay Modifier
# event TransactionAdded(uint256 indexed queueNonce, bytes32 indexed txHash, address to, uint256 value,
#                        bytes data, Enum.Operation operation)
DELAY_TRANSACTION_ADDED_EVENT_ABI = {
    "anonymous": False,
    "inputs": [
        {"indexed": True, "name": "queueNonce", "type": "uint256"},
        {"indexed": True, "name": "txHash", "type": "bytes32"},
        {"indexed": False, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"},
        {"indexed": False, "name": "data", "type": "bytes"},
        {"indexed": False, "name": "operation", "type": "uint8"},
    ],
    "name": "TransactionAdded",
    "type": "event",
}


class DelayModuleEventsIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = cls.get_new_instance()
        return cls.instance

    @classmethod
    def get_new_instance(cls) -> "DelayModuleEventsIndexer":
        from django.conf import settings

        return DelayModuleEventsIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class DelayModuleEventsIndexer(EventsIndexer):
    """
    Indexes Zodiac Delay Modifier `TransactionAdded` events emitted by any contract.

    They are emitted when a module (e.g. an Account Recovery recoverer) queues a transaction, which
    is not a Safe transaction, so no other indexer stores it. Progress is tracked in `IndexingStatus`
    as there are no addresses to monitor.
    """

    IGNORE_ADDRESSES_ON_LOG_FILTER = True

    @cached_property
    def contract_events(self) -> list[ContractEvent]:
        contract = self.ethereum_client.w3.eth.contract(
            abi=[DELAY_TRANSACTION_ADDED_EVENT_ABI]
        )
        return [contract.events.TransactionAdded()]

    @property
    def database_field(self):
        return "block_number"

    @property
    def database_queryset(self) -> QuerySet:
        return IndexingStatus.objects.none()

    def get_from_block_number(
        self, addresses: set[ChecksumAddress] | None = None
    ) -> int | None:
        return IndexingStatus.objects.get_delay_module_indexing_status().block_number

    def get_almost_updated_addresses(
        self, current_block_number: int
    ) -> set[ChecksumAddress]:
        """
        :return: Placeholder address, so the indexing loop runs. Logs are not filtered by address
        """
        return {NULL_ADDRESS}

    def get_not_updated_addresses(
        self, current_block_number: int
    ) -> set[ChecksumAddress]:
        return set()

    def update_monitored_addresses(
        self,
        addresses: set[ChecksumAddress],
        from_block_number: int,
        to_block_number: int,
    ) -> bool:
        updated = IndexingStatus.objects.set_delay_module_indexing_status(
            to_block_number + 1, from_block_number=from_block_number
        )
        if not updated:
            logger.warning(
                "%s: Possible reorg - Cannot update delay module indexing status from-block-number=%d "
                "to-block-number=%d",
                self.__class__.__name__,
                from_block_number,
                to_block_number,
            )
        return updated

    def _process_decoded_element(
        self, decoded_element: EventData
    ) -> DelayModuleTransaction:
        args = decoded_element["args"]
        return DelayModuleTransaction(
            ethereum_tx_id=decoded_element["transactionHash"],
            log_index=decoded_element["logIndex"],
            block_number=decoded_element["blockNumber"],
            timestamp=EthereumBlock.objects.get_timestamp_by_hash(
                decoded_element["blockHash"]
            ),
            module=decoded_element["address"],
            queue_nonce=args["queueNonce"],
            module_tx_hash=args["txHash"],
            to=args["to"],
            value=args["value"],
            data=args["data"],
            operation=args["operation"],
        )

    def process_elements(
        self, log_receipts: Sequence[LogReceipt]
    ) -> list[DelayModuleTransaction]:
        delay_module_transactions = super().process_elements(log_receipts)
        if delay_module_transactions:
            DelayModuleTransaction.objects.bulk_create(
                delay_module_transactions, ignore_conflicts=True
            )
        return delay_module_transactions
