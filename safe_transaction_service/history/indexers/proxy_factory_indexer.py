from functools import cached_property
from logging import getLogger
from typing import List, Optional, Sequence
import time

from safe_eth.eth import EthereumClient
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.contracts import (
    get_proxy_factory_V1_1_1_contract,
    get_proxy_factory_V1_3_0_contract,
    get_proxy_factory_V1_4_1_contract,
)
from safe_eth.util.util import to_0x_hex_str
from web3.contract.contract import ContractEvent
from web3.types import EventData, LogReceipt

from ..models import ProxyFactory, SafeContract
from .events_indexer import EventsIndexer

logger = getLogger(__name__)


class ProxyFactoryIndexerProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            cls.instance = cls.get_new_instance()

        return cls.instance

    @classmethod
    def get_new_instance(cls) -> "ProxyFactoryIndexer":
        from django.conf import settings

        return ProxyFactoryIndexer(EthereumClient(settings.ETHEREUM_NODE_URL))

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class ProxyFactoryIndexer(EventsIndexer):
    @cached_property
    def contract_events(self) -> List[ContractEvent]:
        logger.debug("%s: Initializing contract events", self.__class__.__name__)
        proxy_factory_v1_1_1_contract = get_proxy_factory_V1_1_1_contract(
            self.ethereum_client.w3
        )
        proxy_factory_v1_3_0_contract = get_proxy_factory_V1_3_0_contract(
            self.ethereum_client.w3
        )
        proxy_factory_v_1_4_1_contract = get_proxy_factory_V1_4_1_contract(
            self.ethereum_client.w3
        )
        contract_events = [
            # event ProxyCreation(Proxy proxy)
            proxy_factory_v1_1_1_contract.events.ProxyCreation(),
            # event ProxyCreation(GnosisSafeProxy proxy, address singleton)
            proxy_factory_v1_3_0_contract.events.ProxyCreation(),
            # event ProxyCreation(SafeProxy indexed proxy, address singleton)
            proxy_factory_v_1_4_1_contract.events.ProxyCreation(),
        ]
        logger.debug(
            "%s: Initialized %d contract events", 
            self.__class__.__name__, 
            len(contract_events)
        )
        return contract_events

    @property
    def database_field(self):
        return "tx_block_number"

    @property
    def database_queryset(self):
        return ProxyFactory.objects.all()

    def _process_decoded_element(
        self, decoded_element: EventData
    ) -> Optional[SafeContract]:
        contract_address = decoded_element["args"]["proxy"]
        if contract_address != NULL_ADDRESS:
            if (block_number := decoded_element["blockNumber"]) == 0:
                transaction_hash = to_0x_hex_str(decoded_element["transactionHash"])
                log_msg = (
                    f"Events are reporting blockNumber=0 for tx-hash={transaction_hash}"
                )
                logger.error(log_msg)
                raise ValueError(log_msg)

            logger.debug(
                "%s: Found proxy contract at %s in tx %s, block %d", 
                self.__class__.__name__,
                contract_address,
                to_0x_hex_str(decoded_element["transactionHash"]),
                block_number
            )
            return SafeContract(
                address=contract_address,
                ethereum_tx_id=decoded_element["transactionHash"],
            )
        else:
            logger.debug(
                "%s: Ignoring NULL_ADDRESS proxy in tx %s", 
                self.__class__.__name__,
                to_0x_hex_str(decoded_element["transactionHash"])
            )
            return None

    def process_elements(
        self, log_receipts: Sequence[LogReceipt]
    ) -> List[SafeContract]:
        """
        Process all logs

        :param log_receipts: Iterable of Events fetched using `web3.eth.getLogs`
        :return: List of `SafeContract` already stored in database
        """
        start_time = time.time()
        logger.info(
            "%s: Processing %d log receipts", 
            self.__class__.__name__, 
            len(log_receipts)
        )
        
        safe_contracts = super().process_elements(log_receipts)
        
        if safe_contracts:
            logger.info(
                "%s: Creating %d SafeContract objects", 
                self.__class__.__name__, 
                len(safe_contracts)
            )
            bulk_create_start = time.time()
            SafeContract.objects.bulk_create(safe_contracts, ignore_conflicts=True)
            bulk_create_time = time.time() - bulk_create_start
            logger.info(
                "%s: Finished bulk_create in %.2f seconds", 
                self.__class__.__name__, 
                bulk_create_time
            )
        
        process_time = time.time() - start_time
        logger.info(
            "%s: Finished processing %d log receipts in %.2f seconds, found %d new contracts", 
            self.__class__.__name__, 
            len(log_receipts),
            process_time,
            len(safe_contracts)
        )
        
        return safe_contracts
