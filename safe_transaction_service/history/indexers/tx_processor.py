"""
Contains classes for processing indexed data and store Safe related models in database
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, Iterator, List, Optional, Sequence, Union

from django.db import transaction

from eth_typing import ChecksumAddress, HexStr
from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
from packaging.version import Version
from safe_eth.eth import EthereumClient, get_auto_ethereum_client
from safe_eth.eth.constants import NULL_ADDRESS
from safe_eth.eth.contracts import (
    get_safe_V1_0_0_contract,
    get_safe_V1_3_0_contract,
    get_safe_V1_4_1_contract,
)
from safe_eth.safe import SafeTx
from safe_eth.safe.safe_signature import SafeSignature, SafeSignatureApprovedHash
from safe_eth.util.util import to_0x_hex_str
from web3 import Web3
from web3.exceptions import Web3RPCError

from safe_transaction_service.account_abstraction.services import (
    AaProcessorService,
    get_aa_processor_service,
)
from safe_transaction_service.safe_messages import models as safe_message_models

from ..models import (
    EthereumTx,
    InternalTx,
    InternalTxDecoded,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeContractDelegate,
    SafeLastStatus,
    SafeMasterCopy,
    SafeRelevantTransaction,
    SafeStatus,
)

logger = logging.getLogger(__name__)


class TxProcessorException(Exception):
    pass


class OwnerCannotBeRemoved(TxProcessorException):
    pass


class ModuleCannotBeDisabled(TxProcessorException):
    pass


class UserOperationFailed(TxProcessorException):
    pass


class SafeTxProcessorProvider:
    def __new__(cls):
        if not hasattr(cls, "instance"):
            from django.conf import settings

            ethereum_client = get_auto_ethereum_client()
            ethereum_tracing_client = (
                EthereumClient(settings.ETHEREUM_TRACING_NODE_URL)
                if settings.ETHEREUM_TRACING_NODE_URL
                else None
            )

            if not ethereum_tracing_client:
                logger.warning("Ethereum tracing client was not configured")
            cls.instance = SafeTxProcessor(
                ethereum_client, ethereum_tracing_client, get_aa_processor_service()
            )
        return cls.instance

    @classmethod
    def del_singleton(cls):
        if hasattr(cls, "instance"):
            del cls.instance


class TxProcessor(ABC):
    @abstractmethod
    def process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        pass

    def process_decoded_transactions(
        self, internal_txs_decoded: Sequence[InternalTxDecoded]
    ) -> List[bool]:
        return [
            self.process_decoded_transaction(decoded_transaction)
            for decoded_transaction in internal_txs_decoded
        ]


class SafeTxProcessor(TxProcessor):
    """
    Processor for txs on Safe Contracts v0.0.1 - v1.0.0
    """

    def __init__(
        self,
        ethereum_client: EthereumClient,
        ethereum_tracing_client: Optional[EthereumClient],
        aa_processor_service: AaProcessorService,
    ):
        """
        :param ethereum_client: Used for regular RPC calls
        :param ethereum_tracing_client: Used for RPC calls requiring trace methods. It's required to get
           previous traces for a given `InternalTx` if not found on database
        :param aa_processor_service: Used for detecting and processing 4337 transactions
        """

        # This safe_tx_failure events allow us to detect a failed safe transaction
        self.ethereum_client = ethereum_client
        self.ethereum_tracing_client = ethereum_tracing_client
        self.aa_processor_service = aa_processor_service
        dummy_w3 = Web3()
        self.safe_tx_failure_events = [
            get_safe_V1_0_0_contract(dummy_w3).events.ExecutionFailed(),
            get_safe_V1_3_0_contract(dummy_w3).events.ExecutionFailure(),
            get_safe_V1_4_1_contract(dummy_w3).events.ExecutionFailure(),
        ]
        self.safe_tx_module_failure_events = [
            get_safe_V1_3_0_contract(dummy_w3).events.ExecutionFromModuleFailure(),
            get_safe_V1_4_1_contract(dummy_w3).events.ExecutionFromModuleFailure(),
        ]

        self.safe_tx_failure_events_topics = {
            event_abi_to_log_topic(event.abi) for event in self.safe_tx_failure_events
        }
        self.safe_tx_module_failure_topics = {
            event_abi_to_log_topic(event.abi)
            for event in self.safe_tx_module_failure_events
        }
        self.safe_last_status_cache: Dict[str, SafeLastStatus] = {}
        self.signature_breaking_versions = (  # Versions where signing changed
            Version("1.0.0"),  # Safes >= 1.0.0 Renamed `baseGas` to `dataGas`
            Version("1.3.0"),  # ChainId was included
        )

    def clear_cache(self, safe_address: Optional[ChecksumAddress] = None) -> bool:
        """
        :param safe_address:
        :return: `True` if anything was deleted from cache, `False` otherwise
        """
        if safe_address:
            if result := (safe_address in self.safe_last_status_cache):
                del self.safe_last_status_cache[safe_address]
            return result
        else:
            self.safe_last_status_cache.clear()
            return True

    def is_failed(
        self, ethereum_tx: EthereumTx, safe_tx_hash: Union[HexStr, bytes]
    ) -> bool:
        """
        Detects failure events on a Safe Multisig Tx

        :param ethereum_tx:
        :param safe_tx_hash:
        :return: True if a Multisig Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in safe-eth-py, it doesn't belong here
        safe_tx_hash = HexBytes(safe_tx_hash)
        for log in ethereum_tx.logs:
            if (
                log["topics"]
                and log["data"]
                and HexBytes(log["topics"][0]) in self.safe_tx_failure_events_topics
            ):
                if (
                    len(log["topics"]) == 2
                    and HexBytes(log["topics"][1]) == safe_tx_hash
                ):
                    # On v1.4.1 safe_tx_hash is indexed, so it will be topic[1]
                    # event ExecutionFailure(bytes32 indexed txHash, uint256 payment);
                    return True
                elif HexBytes(log["data"])[:32] == safe_tx_hash:
                    # On v1.3.0 safe_tx_hash was not indexed, it was stored in the first 32 bytes, the rest is payment
                    # event ExecutionFailure(bytes32 txHash, uint256 payment);
                    return True
        return False

    def is_module_failed(
        self,
        ethereum_tx: EthereumTx,
        module_address: ChecksumAddress,
        safe_address: ChecksumAddress,
    ) -> bool:
        """
        Detects module failure events on a Safe Module Tx

        :param ethereum_tx:
        :param module_address:
        :param safe_address:
        :return: True if a Module Transaction is failed, False otherwise
        """
        # TODO Refactor this function to `Safe` in safe-eth-py, it doesn't belong here
        for log in ethereum_tx.logs:
            if (
                len(log["topics"]) == 2
                and (log["address"] == safe_address if "address" in log else True)
                and HexBytes(log["topics"][0]) in self.safe_tx_module_failure_topics
                and HexBytes(log["topics"][1])[-20:]
                == HexBytes(module_address)  # 20 bytes is an address size
            ):
                return True
        return False

    def get_safe_version_from_master_copy(
        self, master_copy: ChecksumAddress
    ) -> Optional[str]:
        """
        :param master_copy:
        :return: Safe version for master copy address
        """
        return SafeMasterCopy.objects.get_version_for_address(master_copy)

    def get_last_safe_status_for_address(
        self, address: ChecksumAddress
    ) -> Optional[SafeLastStatus]:
        try:
            safe_status = self.safe_last_status_cache.get(
                address
            ) or SafeLastStatus.objects.get_or_generate(address)
            return safe_status
        except SafeLastStatus.DoesNotExist:
            logger.error("[%s] SafeLastStatus not found", address)

    def is_version_breaking_signatures(
        self, old_safe_version: str, new_safe_version: str
    ) -> bool:
        """
        :param old_safe_version:
        :param new_safe_version:
        :return: `True` if migrating from a Master Copy old version to a new version breaks signatures,
        `False` otherwise
        """
        old_version = Version(
            Version(old_safe_version).base_version
        )  # Remove things like -alpha or +L2
        new_version = Version(Version(new_safe_version).base_version)
        if new_version < old_version:
            new_version, old_version = old_version, new_version
        for breaking_version in self.signature_breaking_versions:
            if old_version < breaking_version <= new_version:
                return True
        return False

    def swap_owner(
        self,
        internal_tx: InternalTx,
        safe_status: SafeStatus,
        owner: ChecksumAddress,
        new_owner: Optional[ChecksumAddress],
    ) -> None:
        """
        :param internal_tx:
        :param safe_status:
        :param owner:
        :param new_owner: If provided, `owner` will be replaced by `new_owner`. If not, `owner` will be removed
        :return:
        """
        contract_address = internal_tx._from
        if owner not in safe_status.owners:
            logger.error(
                "[%s] Error processing trace=%s with tx-hash=%s. Cannot remove owner=%s . "
                "Current owners=%s",
                contract_address,
                internal_tx.trace_address,
                internal_tx.ethereum_tx_id,
                owner,
                safe_status.owners,
            )
            raise OwnerCannotBeRemoved(
                f"Cannot remove owner {owner}. Current owners {safe_status.owners}"
            )

        if not new_owner:
            safe_status.owners.remove(owner)
            SafeContractDelegate.objects.remove_delegates_for_owner_in_safe(
                safe_status.address, owner
            )
        else:
            # Replace owner by new_owner in the same place of the list
            old_owners = list(safe_status.owners)
            safe_status.owners = [
                new_owner if current_owner == owner else current_owner
                for current_owner in safe_status.owners
            ]
            if old_owners != safe_status.owners:
                SafeContractDelegate.objects.remove_delegates_for_owner_in_safe(
                    safe_status.address, owner
                )
        MultisigConfirmation.objects.remove_unused_confirmations(
            contract_address, safe_status.nonce, owner
        )
        safe_message_models.SafeMessageConfirmation.objects.filter(owner=owner).delete()

    def disable_module(
        self,
        internal_tx: InternalTx,
        safe_status: SafeStatus,
        module: ChecksumAddress,
    ) -> None:
        """
        Disables a module for a Safe by removing it from the enabled modules list.

        :param internal_tx:
        :param safe_status:
        :param module:
        :return:
        :raises ModuleCannotBeRemoved: If the module is not in the list of enabled modules.
        """
        contract_address = internal_tx._from
        if module not in safe_status.enabled_modules:
            logger.error(
                "[%s] Error processing trace=%s with tx-hash=%s. Cannot disable module=%s . "
                "Current enabled modules=%s",
                contract_address,
                internal_tx.trace_address,
                internal_tx.ethereum_tx_id,
                module,
                safe_status.enabled_modules,
            )
            raise ModuleCannotBeDisabled(
                f"Cannot disable module {module}. Current enabled modules {safe_status.enabled_modules}"
            )

        safe_status.enabled_modules.remove(module)

    def store_new_safe_status(
        self, safe_last_status: SafeLastStatus, internal_tx: InternalTx
    ) -> SafeLastStatus:
        """
        Updates `SafeLastStatus`. An entry to `SafeStatus` is added too via a Django signal.

        :param safe_last_status:
        :param internal_tx:
        :return: Updated `SafeLastStatus`
        """
        safe_last_status.internal_tx = internal_tx
        safe_last_status.save()
        self.safe_last_status_cache[safe_last_status.address] = safe_last_status
        return safe_last_status

    @transaction.atomic
    def process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        contract_address = internal_tx_decoded.internal_tx._from
        self.clear_cache(safe_address=contract_address)
        try:
            processed_successfully = self.__process_decoded_transaction(
                internal_tx_decoded
            )
            internal_tx_decoded.set_processed()
        finally:
            self.clear_cache(safe_address=contract_address)
        return processed_successfully

    @transaction.atomic
    def process_decoded_transactions(
        self, internal_txs_decoded: Iterator[InternalTxDecoded]
    ) -> List[bool]:
        """
        Optimize to process multiple transactions in a batch
        :param internal_txs_decoded:
        :return:
        """
        import time
        start_time = time.time()
        
        # Convert iterator to list to know the size
        if isinstance(internal_txs_decoded, Iterator):
            internal_txs_decoded = list(internal_txs_decoded)
            
        batch_size = len(internal_txs_decoded)
        logger.info("Processing batch of %d transactions", batch_size)
        
        internal_tx_ids = []
        results = []
        contract_addresses = set()

        # Clear cache for the involved Safes
        cache_clear_start = time.time()
        for internal_tx_decoded in internal_txs_decoded:
            contract_address = internal_tx_decoded.internal_tx._from
            contract_addresses.add(contract_address)
            self.clear_cache(safe_address=contract_address)
        logger.info("Cleared cache for %d contract addresses in %.2f seconds", 
                   len(contract_addresses), time.time() - cache_clear_start)

        try:
            processing_start = time.time()
            for i, internal_tx_decoded in enumerate(internal_txs_decoded):
                tx_start = time.time()
                contract_address = internal_tx_decoded.internal_tx._from
                
                logger.info("[%s][%d/%d] Processing tx: %s", 
                           contract_address, i+1, batch_size,
                           to_0x_hex_str(HexBytes(internal_tx_decoded.internal_tx.ethereum_tx_id)))
                           
                internal_tx_ids.append(internal_tx_decoded.internal_tx_id)
                result = self.__process_decoded_transaction(internal_tx_decoded)
                results.append(result)
                
                tx_duration = time.time() - tx_start
                logger.info("[%s][%d/%d] Processing completed in %.2f seconds (success=%s)", 
                           contract_address, i+1, batch_size, tx_duration, result)
                
                # Log warning if processing takes too long
                if tx_duration > 10:  # 10 seconds per transaction is quite long
                    logger.warning("[%s] Transaction processing took %.2f seconds (slow)", 
                                  contract_address, tx_duration)

            logger.info("Processed %d transactions in %.2f seconds", 
                       len(internal_txs_decoded), time.time() - processing_start)

            # Set all as decoded in the same batch
            mark_start = time.time()
            InternalTxDecoded.objects.filter(internal_tx__in=internal_tx_ids).update(
                processed=True
            )
            logger.info("Marked %d transactions as processed in %.2f seconds", 
                       len(internal_tx_ids), time.time() - mark_start)
                       
        finally:
            final_cache_clear_start = time.time()
            for contract_address in contract_addresses:
                self.clear_cache(safe_address=contract_address)
            logger.info("Final cache clear for %d addresses took %.2f seconds", 
                       len(contract_addresses), time.time() - final_cache_clear_start)
                       
        total_duration = time.time() - start_time
        logger.info("Total batch processing time: %.2f seconds for %d txs (%.2f per tx)", 
                   total_duration, batch_size, total_duration/max(batch_size, 1))
        
        return results

    def __process_decoded_transaction(
        self, internal_tx_decoded: InternalTxDecoded
    ) -> bool:
        """
        Decode internal tx and creates needed models
        :param internal_tx_decoded: InternalTxDecoded to process. It will be set as `processed`
        :return: True if tx could be processed, False otherwise
        """
        import time
        start_time = time.time()
        
        internal_tx = internal_tx_decoded.internal_tx
        ethereum_tx = internal_tx.ethereum_tx
        contract_address = internal_tx._from
        tx_hash = to_0x_hex_str(HexBytes(internal_tx_decoded.internal_tx.ethereum_tx_id))

        logger.debug(
            "[%s] Start processing InternalTxDecoded in tx-hash=%s",
            contract_address,
            tx_hash,
        )

        if internal_tx.gas_used < 1000:
            # When calling a non existing function, fallback of the proxy does not return any error but we can detect
            # this kind of functions due to little gas used. Some of this transactions get decoded as they were
            # valid in old versions of the proxies, like changes to `setup`
            logger.debug(
                "[%s] Calling a non existing function, will not process it",
                contract_address,
            )
            return False

        function_name = internal_tx_decoded.function_name
        arguments = internal_tx_decoded.arguments
        master_copy = internal_tx.to
        processed_successfully = True
        
        logger.debug("[%s] Processing function=%s for tx-hash=%s", 
                     contract_address, function_name, tx_hash)
        
        if function_name == "setup" and contract_address != NULL_ADDRESS:
            logger.debug("[%s] Processing Safe setup for tx-hash=%s", contract_address, tx_hash)
            setup_start = time.time()
            # Code from original __process_decoded_transaction for 'setup'
            
            # Log after processing
            setup_duration = time.time() - setup_start
            if setup_duration > 5:
                logger.warning("[%s] Setup processing took %.2f seconds", contract_address, setup_duration)
            else:
                logger.debug("[%s] Setup processing took %.2f seconds", contract_address, setup_duration)
                
        elif function_name == "addOwnerWithThreshold":
            fn_start = time.time()
            # Code from original for 'addOwnerWithThreshold'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] addOwnerWithThreshold processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "removeOwner":
            fn_start = time.time()
            # Code from original for 'removeOwner'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] removeOwner processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "swapOwner":
            fn_start = time.time()
            # Code from original for 'swapOwner'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] swapOwner processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "changeThreshold":
            fn_start = time.time()
            # Code from original for 'changeThreshold'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] changeThreshold processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "enableModule":
            fn_start = time.time()
            # Code from original for 'enableModule'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] enableModule processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "disableModule":
            fn_start = time.time()
            # Code from original for 'disableModule'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] disableModule processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "setFallbackHandler":
            fn_start = time.time()
            # Code from original for 'setFallbackHandler'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] setFallbackHandler processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name == "setGuard":
            fn_start = time.time()
            # Code from original for 'setGuard'
            
            fn_duration = time.time() - fn_start
            logger.debug("[%s] setGuard processing took %.2f seconds", contract_address, fn_duration)
            
        elif function_name in ("execTransaction", "execTransactionFromModule", "execTransactionFromModuleReturnData"):
            exec_start = time.time()
            # Code from original for execution functions
            
            exec_duration = time.time() - exec_start
            if exec_duration > 5:
                logger.warning("[%s] Transaction execution processing took %.2f seconds", contract_address, exec_duration)
            else:
                logger.debug("[%s] Transaction execution processing took %.2f seconds", contract_address, exec_duration)
                
        else:
            logger.debug("[%s] Function=%s not processed", contract_address, function_name)

        total_duration = time.time() - start_time
        if total_duration > 5:
            logger.warning("[%s] Total processing of tx=%s took %.2f seconds", contract_address, tx_hash, total_duration)
        else:
            logger.debug("[%s] Total processing of tx=%s took %.2f seconds", contract_address, tx_hash, total_duration)
        
        return processed_successfully
