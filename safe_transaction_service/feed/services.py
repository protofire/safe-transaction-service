import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from django.db.models import Prefetch, Q

from safe_transaction_service.history.models import (
    ERC20Transfer,
    ERC721Transfer,
    EthereumTx,
    InternalTx,
    ModuleTransaction,
    MultisigTransaction,
)
from safe_transaction_service.tokens.models import Token


logger = logging.getLogger(__name__)


class GlobalTransactionService:
    """
    Service to efficiently fetch and process transactions globally (across all Safe contracts).
    Optimized for large datasets with batch loading and minimal database queries.
    """

    def get_transactions_with_transfers_from_tx_ids(
        self, ethereum_tx_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Fetch all transactions and their transfers based on ethereum_tx_ids.
        This method is optimized for batch loading to minimize database queries.
        
        Args:
            ethereum_tx_ids: List of ethereum transaction hash strings
            
        Returns:
            List of serializable transaction dictionaries with their associated transfers
        """
        if not ethereum_tx_ids:
            return []

        logger.debug(
            "[%s] Fetching detailed transaction data for %d ethereum_tx_ids",
            __name__,
            len(ethereum_tx_ids),
        )

        # 1. Fetch MultisigTransactions efficiently with all related data
        multisig_transactions = self._fetch_multisig_transactions(ethereum_tx_ids)
        
        # 2. Fetch ModuleTransactions efficiently with all related data
        module_transactions = self._fetch_module_transactions(ethereum_tx_ids)
        
        # 3. Fetch EthereumTxs that don't have associated Safe-specific transactions
        # but might have transfers (e.g., incoming transfers)
        ethereum_transactions = self._fetch_ethereum_transactions(ethereum_tx_ids)
        
        # 4. Fetch all transfers: ERC20, ERC721, and Ether transfers (native)
        transfers_by_tx = self._fetch_all_transfers(ethereum_tx_ids)
        
        # 5. Fetch token information for all transfers in a single query
        token_addresses = self._extract_token_addresses(transfers_by_tx)
        tokens_by_address = self._fetch_tokens(token_addresses)
        
        # 6. Map tokens to transfers
        self._map_tokens_to_transfers(transfers_by_tx, tokens_by_address)
        
        # 7. Combine results and build final response
        return self._build_final_result(
            ethereum_tx_ids,
            multisig_transactions,
            module_transactions,
            ethereum_transactions,
            transfers_by_tx
        )

    def _fetch_multisig_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[MultisigTransaction]]:
        """
        Fetch all MultisigTransactions for the given ethereum_tx_ids.
        """
        # Use select_related and prefetch_related to minimize database queries
        queryset = (
            MultisigTransaction.objects.filter(ethereum_tx_id__in=ethereum_tx_ids)
            .with_confirmations_required()
            .prefetch_related("confirmations")
            .select_related("ethereum_tx__block")
            .order_by("-nonce", "-created")
        )
        
        # Helper function for consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        # Group by ethereum_tx_id for efficient lookups
        result = {}
        for tx in queryset:
            tx_id = ensure_string_tx_id(tx.ethereum_tx_id)
            result.setdefault(tx_id, []).append(tx)
            
        logger.debug(
            "[%s] Fetched %d MultisigTransactions for %d ethereum_tx_ids",
            __name__,
            queryset.count(),
            len(ethereum_tx_ids),
        )
        
        return result

    def _fetch_module_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[ModuleTransaction]]:
        """
        Fetch all ModuleTransactions for the given ethereum_tx_ids.
        """
        queryset = (
            ModuleTransaction.objects.filter(internal_tx__ethereum_tx_id__in=ethereum_tx_ids)
            .select_related("internal_tx__ethereum_tx", "internal_tx__ethereum_tx__block")
        )
        
        # Helper function for consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        # Group by ethereum_tx_id for efficient lookups
        result = {}
        for tx in queryset:
            eth_tx_id = ensure_string_tx_id(tx.internal_tx.ethereum_tx_id)
            result.setdefault(eth_tx_id, []).append(tx)
            
        logger.debug(
            "[%s] Fetched %d ModuleTransactions for %d ethereum_tx_ids",
            __name__,
            queryset.count(),
            len(ethereum_tx_ids),
        )
        
        return result

    def _fetch_ethereum_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, EthereumTx]:
        """
        Fetch all EthereumTxs for the given ethereum_tx_ids.
        """
        queryset = (
            EthereumTx.objects.filter(tx_hash__in=ethereum_tx_ids)
            .select_related("block")
        )
        
        # Helper function for consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        # Map by tx_hash for efficient lookups
        result = {}
        for tx in queryset:
            tx_id = ensure_string_tx_id(tx.tx_hash)
            result[tx_id] = tx
        
        logger.debug(
            "[%s] Fetched %d EthereumTxs for %d ethereum_tx_ids",
            __name__,
            len(result),
            len(ethereum_tx_ids),
        )
        
        return result

    def _fetch_all_transfers(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch all transfers (ERC20, ERC721, Ether) for the given ethereum_tx_ids.
        """
        # Fetch all transfers in 3 efficient queries
        erc20_transfers = (
            ERC20Transfer.objects.filter(ethereum_tx_id__in=ethereum_tx_ids)
            .select_related("ethereum_tx__block")
        )
        
        erc721_transfers = (
            ERC721Transfer.objects.filter(ethereum_tx_id__in=ethereum_tx_ids)
            .select_related("ethereum_tx__block")
        )
        
        ether_transfers = (
            InternalTx.objects.filter(ethereum_tx_id__in=ethereum_tx_ids, value__gt=0)
            .select_related("ethereum_tx__block")
        )
        
        # Process and combine transfers
        transfers_by_tx = {}
        
        # Helper function to ensure consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        # Process ERC20 transfers
        for transfer in erc20_transfers:
            tx_id = ensure_string_tx_id(transfer.ethereum_tx_id)
            transfer_data = {
                "type": "ERC20_TRANSFER",
                "executionDate": transfer.ethereum_tx.block.timestamp.isoformat(),
                "blockNumber": transfer.ethereum_tx.block.number,
                "transactionHash": tx_id,
                "to": transfer.to,
                "from": transfer._from,
                "value": str(transfer.value),
                "tokenId": None,
                "tokenAddress": transfer.address,
                "tokenInfo": None,  # Will be populated later
                "transferId": f"e_{tx_id}_{transfer.log_index}",
            }
            transfers_by_tx.setdefault(tx_id, []).append(transfer_data)
        
        # Process ERC721 transfers
        for transfer in erc721_transfers:
            tx_id = ensure_string_tx_id(transfer.ethereum_tx_id)
            transfer_data = {
                "type": "ERC721_TRANSFER",
                "executionDate": transfer.ethereum_tx.block.timestamp.isoformat(),
                "blockNumber": transfer.ethereum_tx.block.number,
                "transactionHash": tx_id,
                "to": transfer.to,
                "from": transfer._from,
                "value": None,
                "tokenId": str(transfer.token_id),
                "tokenAddress": transfer.address,
                "tokenInfo": None,  # Will be populated later
                "transferId": f"e_{tx_id}_{transfer.log_index}",
            }
            transfers_by_tx.setdefault(tx_id, []).append(transfer_data)
        
        # Process Ether transfers
        for transfer in ether_transfers:
            tx_id = ensure_string_tx_id(transfer.ethereum_tx_id)
            transfer_data = {
                "type": "ETHER_TRANSFER",
                "executionDate": transfer.ethereum_tx.block.timestamp.isoformat(),
                "blockNumber": transfer.ethereum_tx.block.number,
                "transactionHash": tx_id,
                "to": transfer.to,
                "from": transfer._from,
                "value": str(transfer.value),
                "tokenId": None,
                "tokenAddress": None,
                "tokenInfo": None,
                "transferId": f"i_{tx_id}_{transfer.trace_address}",
            }
            transfers_by_tx.setdefault(tx_id, []).append(transfer_data)
        
        logger.debug(
            "[%s] Fetched %d transfers for %d ethereum_tx_ids",
            __name__,
            sum(len(transfers) for transfers in transfers_by_tx.values()),
            len(ethereum_tx_ids),
        )
        
        return transfers_by_tx

    def _extract_token_addresses(
        self, transfers_by_tx: Dict[str, List[Dict[str, Any]]]
    ) -> Set[str]:
        """
        Extract all unique token addresses from transfers.
        """
        token_addresses = set()
        for transfers in transfers_by_tx.values():
            for transfer in transfers:
                if transfer.get("tokenAddress"):
                    token_addresses.add(transfer["tokenAddress"])
        return token_addresses

    def _fetch_tokens(self, token_addresses: Set[str]) -> Dict[str, Token]:
        """
        Fetch token information for all token addresses in a single query.
        """
        if not token_addresses:
            return {}
            
        tokens = Token.objects.filter(address__in=token_addresses)
        tokens_by_address = {token.address: token for token in tokens}
        
        logger.debug(
            "[%s] Fetched %d tokens for %d token addresses",
            __name__,
            len(tokens_by_address),
            len(token_addresses),
        )
        
        return tokens_by_address

    def _map_tokens_to_transfers(
        self, 
        transfers_by_tx: Dict[str, List[Dict[str, Any]]],
        tokens_by_address: Dict[str, Token]
    ) -> None:
        """
        Map token information to transfers.
        """
        for transfers in transfers_by_tx.values():
            for transfer in transfers:
                token_address = transfer.get("tokenAddress")
                if token_address and token_address in tokens_by_address:
                    token = tokens_by_address[token_address]
                    # Format token info according to the required structure
                    transfer["tokenInfo"] = {
                        "type": "ERC20" if transfer["type"] == "ERC20_TRANSFER" else "ERC721",
                        "address": token_address,
                        "name": token.name,
                        "symbol": token.symbol,
                        "decimals": token.decimals,
                        "logoUri": token.logo_uri,
                    }

    def _build_final_result(
        self,
        ethereum_tx_ids: List[str],
        multisig_transactions: Dict[str, List[MultisigTransaction]],
        module_transactions: Dict[str, List[ModuleTransaction]],
        ethereum_transactions: Dict[str, EthereumTx],
        transfers_by_tx: Dict[str, List[Dict[str, Any]]],
    ) -> List[Dict[str, Any]]:
        """
        Build the final result by combining all data.
        """
        # Set to track processed transactions to avoid duplicates
        processed_multisig_hashes = set()
        processed_module_ids = set()
        processed_ethereum_txs = set()
        
        final_result = []
        
        # Normalize ethereum_tx_ids to ensure consistent format
        normalized_ethereum_tx_ids = [
            tx_id.hex() if isinstance(tx_id, bytes) else tx_id 
            for tx_id in ethereum_tx_ids
        ]
        
        # Process in order of the original ethereum_tx_ids to maintain ordering
        for eth_tx_id in normalized_ethereum_tx_ids:
            # Get all data for this ethereum_tx
            multisig_txs = multisig_transactions.get(eth_tx_id, [])
            module_txs = module_transactions.get(eth_tx_id, [])
            ethereum_tx = ethereum_transactions.get(eth_tx_id)
            transfers = transfers_by_tx.get(eth_tx_id, [])
            
            # Process MultisigTransactions
            for multisig_tx in multisig_txs:
                # Ensure unique identifier for safe_tx_hash
                safe_tx_hash = multisig_tx.safe_tx_hash.hex() if isinstance(multisig_tx.safe_tx_hash, bytes) else multisig_tx.safe_tx_hash
                
                if safe_tx_hash not in processed_multisig_hashes:
                    processed_multisig_hashes.add(safe_tx_hash)
                    # Format according to the required structure
                    tx_data = self._format_multisig_transaction(multisig_tx, transfers)
                    final_result.append(tx_data)
            
            # Process ModuleTransactions
            for module_tx in module_txs:
                if module_tx.internal_tx_id not in processed_module_ids:
                    processed_module_ids.add(module_tx.internal_tx_id)
                    # Format according to the required structure
                    tx_data = self._format_module_transaction(module_tx, transfers)
                    final_result.append(tx_data)
            
            # Process EthereumTx if it wasn't part of a Multisig or Module transaction
            # but has transfers (usually incoming transfers)
            if (eth_tx_id not in processed_ethereum_txs and 
                not multisig_txs and 
                not module_txs and 
                ethereum_tx and 
                transfers):
                processed_ethereum_txs.add(eth_tx_id)
                # Format according to the required structure
                tx_data = self._format_ethereum_transaction(ethereum_tx, transfers)
                final_result.append(tx_data)
        
        return final_result

    def _format_multisig_transaction(
        self, tx: MultisigTransaction, transfers: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Format a MultisigTransaction according to the required response structure.
        """
        # Handle tx_hash types correctly
        safe_tx_hash = tx.safe_tx_hash.hex() if isinstance(tx.safe_tx_hash, bytes) else tx.safe_tx_hash
        ethereum_tx_id = tx.ethereum_tx_id.hex() if isinstance(tx.ethereum_tx_id, bytes) else tx.ethereum_tx_id
        
        # Basic transaction data
        result = {
            "type": "TRANSACTION",
            "txType": "MULTISIG_TRANSACTION",
            "safe": tx.safe,
            "to": tx.to,
            "value": str(tx.value),
            "data": tx.data.hex() if tx.data else None,
            "operation": tx.operation,
            "gasToken": tx.gas_token,
            "safeTxGas": str(tx.safe_tx_gas),
            "baseGas": str(tx.base_gas),
            "gasPrice": str(tx.gas_price),
            "refundReceiver": tx.refund_receiver,
            "nonce": tx.nonce,
            "executionDate": (tx.ethereum_tx.block.timestamp.isoformat() 
                             if tx.ethereum_tx and tx.ethereum_tx.block 
                             else None),
            "submissionDate": tx.created.isoformat(),
            "modified": tx.modified.isoformat(),
            "blockNumber": (tx.ethereum_tx.block.number 
                           if tx.ethereum_tx and tx.ethereum_tx.block 
                           else None),
            "transactionHash": ethereum_tx_id,
            "safeTxHash": safe_tx_hash,
            "proposer": tx.proposer,
            "proposedByDelegate": tx.proposed_by_delegate,
            "executor": tx.ethereum_tx._from if tx.ethereum_tx else None,
            "isExecuted": bool(tx.ethereum_tx_id),
            "isSuccessful": not tx.failed if tx.failed is not None else None,
            "ethGasPrice": (str(tx.ethereum_tx.gas_price) 
                           if tx.ethereum_tx 
                           else None),
            "maxFeePerGas": (str(tx.ethereum_tx.max_fee_per_gas) 
                            if tx.ethereum_tx and tx.ethereum_tx.max_fee_per_gas 
                            else None),
            "maxPriorityFeePerGas": (str(tx.ethereum_tx.max_priority_fee_per_gas) 
                                    if tx.ethereum_tx and tx.ethereum_tx.max_priority_fee_per_gas 
                                    else None),
            "gasUsed": (tx.ethereum_tx.gas_used 
                       if tx.ethereum_tx and tx.ethereum_tx.gas_used 
                       else None),
            "fee": (str(tx.ethereum_tx.gas_used * tx.ethereum_tx.gas_price) 
                   if tx.ethereum_tx and tx.ethereum_tx.gas_used and tx.ethereum_tx.gas_price 
                   else None),
            "origin": tx.origin,
            "dataDecoded": None,  # Would require transaction decoder service
            "confirmationsRequired": getattr(tx, 'confirmations_required', None),
            "confirmations": [],
            "trusted": tx.trusted,
            "signatures": (tx.signatures.hex() 
                          if isinstance(tx.signatures, bytes) 
                          else tx.signatures) if tx.signatures else None,
            "transfers": transfers,
        }
        
        # Handle confirmations properly - check if it's an attribute, RelatedManager, or list
        if hasattr(tx, 'confirmations'):
            confirmations = tx.confirmations
            # Handle RelatedManager (Django ORM)
            if hasattr(confirmations, 'all'):
                confirmations = confirmations.all()
            
            # Now iterate and format confirmations
            result["confirmations"] = [
                {
                    "owner": confirmation.owner,
                    "submissionDate": confirmation.created.isoformat(),
                    "transactionHash": (confirmation.multisig_transaction_hash.hex() 
                                       if isinstance(confirmation.multisig_transaction_hash, bytes) 
                                       else confirmation.multisig_transaction_hash) if confirmation.multisig_transaction_hash else None,
                    "signature": (confirmation.signature.hex() 
                                if isinstance(confirmation.signature, bytes) 
                                else confirmation.signature) if confirmation.signature else None,
                    "signatureType": confirmation.signature_type,
                }
                for confirmation in confirmations
            ]
        
        return result

    def _format_module_transaction(
        self, tx: ModuleTransaction, transfers: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Format a ModuleTransaction according to the required response structure.
        """
        eth_tx = tx.internal_tx.ethereum_tx
        
        # Handle tx_hash type correctly
        tx_hash = eth_tx.tx_hash.hex() if isinstance(eth_tx.tx_hash, bytes) else eth_tx.tx_hash
        
        result = {
            "type": "TRANSACTION",
            "txType": "MODULE_TRANSACTION",
            "safe": tx.safe,
            "module": tx.module,
            "to": tx.to,
            "value": str(tx.value),
            "data": tx.data.hex() if tx.data else None,
            "operation": tx.operation,
            "executionDate": eth_tx.block.timestamp.isoformat() if eth_tx.block else None,
            "blockNumber": eth_tx.block.number if eth_tx.block else None,
            "isSuccessful": not tx.failed,
            "transactionHash": tx_hash,
            "moduleTransactionId": f"internal_tx_{tx.internal_tx_id}",
            "failed": tx.failed,
            "dataDecoded": None,  # Would require transaction decoder service
            "transfers": transfers,
        }
        
        return result

    def _format_ethereum_transaction(
        self, tx: EthereumTx, transfers: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Format an EthereumTx according to the required response structure.
        """
        # Fix the tx_hash handling to work with both bytes and string
        tx_hash = tx.tx_hash.hex() if isinstance(tx.tx_hash, bytes) else tx.tx_hash
        
        result = {
            "type": "TRANSACTION",
            "txType": "ETHEREUM_TRANSACTION",
            "executionDate": tx.block.timestamp.isoformat() if tx.block else None,
            "blockNumber": tx.block.number if tx.block else None,
            "transactionHash": tx_hash,
            "to": tx.to,
            "from": tx._from,
            "data": tx.data.hex() if tx.data else None,
            "transfers": transfers,
        }
        
        return result 