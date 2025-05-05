import logging
import time
import concurrent.futures
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.db.models import Prefetch, Q, F

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

# Database query timeout in milliseconds
QUERY_TIMEOUT_MS = getattr(settings, 'QUERY_TIMEOUT_MS', 30000)  # 30 seconds default

# Recommended database indices to create for optimal performance:
"""
-- IMPORTANT: Add these indices to dramatically improve query performance
-- For MultisigTransaction queries
CREATE INDEX IF NOT EXISTS idx_multisig_ethereum_tx_id ON history_multisigtransaction (ethereum_tx_id, nonce, created DESC);

-- For ModuleTransaction queries
CREATE INDEX IF NOT EXISTS idx_module_ethereum_tx_id ON history_moduletransaction (internal_tx_id);
CREATE INDEX IF NOT EXISTS idx_internal_tx_ethereum_tx ON history_internaltx (ethereum_tx_id);

-- For ERC20 and ERC721 transfers
CREATE INDEX IF NOT EXISTS idx_erc20_transfer_ethereum_tx ON history_erc20transfer (ethereum_tx_id, log_index);
CREATE INDEX IF NOT EXISTS idx_erc721_transfer_ethereum_tx ON history_erc721transfer (ethereum_tx_id, log_index);
CREATE INDEX IF NOT EXISTS idx_internal_tx_value ON history_internaltx (ethereum_tx_id) WHERE value > 0;

-- For token lookups
CREATE INDEX IF NOT EXISTS idx_token_address ON tokens_token (address);
"""

# Set defaults if not in settings
TOKEN_CACHE_TTL = getattr(settings, 'TOKEN_CACHE_TTL', 86400)  # 24 hours default
MAX_BATCH_SIZE = getattr(settings, 'MAX_TRANSACTION_BATCH_SIZE', 1000)


class GlobalTransactionService:
    """
    Service to efficiently fetch and process transactions globally (across all Safe contracts).
    Optimized for large datasets with batch loading and minimal database queries.
    """

    def __init__(self):
        self.reset_metrics()

    def reset_metrics(self):
        """Reset performance metrics for a new request"""
        self.performance_metrics = {
            "queries": [],
            "total_time": 0,
            "cache_hits": 0,
            "cache_misses": 0,
        }

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
        self.reset_metrics()
        start_total = time.time()

        if not ethereum_tx_ids:
            return []

        logger.info(
            "[%s] Starting transaction processing for %d ethereum_tx_ids",
            __name__,
            len(ethereum_tx_ids),
        )

        # Process in batches to prevent excessive memory usage
        result = []
        batch_size = min(MAX_BATCH_SIZE, len(ethereum_tx_ids))
        total_batches = (len(ethereum_tx_ids) + batch_size - 1) // batch_size
        
        for i in range(0, len(ethereum_tx_ids), batch_size):
            batch = ethereum_tx_ids[i:i+batch_size]
            batch_num = i//batch_size + 1
            logger.info(f"Processing batch {batch_num} of {total_batches} ({len(batch)} tx_ids)")
            
            try:
                batch_result = self._process_transaction_batch(batch)
                result.extend(batch_result)
                logger.info(f"Batch {batch_num} completed successfully with {len(batch_result)} results")
            except Exception as e:
                logger.error(f"Error processing batch {batch_num}: {str(e)}", exc_info=True)
                # Continue with next batch instead of failing completely

        self.performance_metrics["total_time"] = time.time() - start_total
        
        # Log performance metrics
        self._log_performance_metrics()
        
        logger.info(f"Processing completed successfully with {len(result)} total transactions")
        
        return result
        
    def _process_transaction_batch(self, ethereum_tx_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Process a batch of ethereum_tx_ids with parallelized fetching of different data types.
        
        Args:
            ethereum_tx_ids: List of ethereum transaction hash strings for this batch
            
        Returns:
            List of transaction data for this batch
        """
        batch_start = time.time()
        logger.info(f"Starting batch processing for {len(ethereum_tx_ids)} tx_ids")
        
        # Default empty results in case of failures
        multisig_transactions = {}
        module_transactions = {}
        ethereum_transactions = {}
        transfers_by_tx = {}
        
        # Use parallel execution for independent database queries
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            # Submit parallel tasks
            multisig_future = executor.submit(self._fetch_multisig_transactions, ethereum_tx_ids)
            module_future = executor.submit(self._fetch_module_transactions, ethereum_tx_ids)
            ethereum_future = executor.submit(self._fetch_ethereum_transactions, ethereum_tx_ids)
            transfers_future = executor.submit(self._fetch_all_transfers, ethereum_tx_ids)
            
            # Set timeout for each future
            FUTURE_TIMEOUT = 30  # seconds
            
            # Track when each future completes
            logger.info(f"Waiting for parallel queries to complete (timeout: {FUTURE_TIMEOUT}s per query)...")
            try:
                # Try to get multisig transactions with timeout
                multisig_start = time.time()
                multisig_transactions = multisig_future.result(timeout=FUTURE_TIMEOUT)
                multisig_time = time.time() - multisig_start
                logger.info(f"Multisig query returned {len(multisig_transactions)} tx groups in {multisig_time:.2f}s")
            except concurrent.futures.TimeoutError:
                logger.error(f"Multisig transactions query TIMED OUT after {FUTURE_TIMEOUT}s")
            except Exception as e:
                logger.error(f"Multisig transactions query failed: {str(e)}")
                
            try:
                # Try to get module transactions with timeout
                module_start = time.time()
                module_transactions = module_future.result(timeout=FUTURE_TIMEOUT)
                module_time = time.time() - module_start
                logger.info(f"Module query returned {len(module_transactions)} tx groups in {module_time:.2f}s")
            except concurrent.futures.TimeoutError:
                logger.error(f"Module transactions query TIMED OUT after {FUTURE_TIMEOUT}s")
            except Exception as e:
                logger.error(f"Module transactions query failed: {str(e)}")
                
            try:
                # Try to get ethereum transactions with timeout
                ethereum_start = time.time()
                ethereum_transactions = ethereum_future.result(timeout=FUTURE_TIMEOUT)
                ethereum_time = time.time() - ethereum_start
                logger.info(f"Ethereum query returned {len(ethereum_transactions)} txs in {ethereum_time:.2f}s")
            except concurrent.futures.TimeoutError:
                logger.error(f"Ethereum transactions query TIMED OUT after {FUTURE_TIMEOUT}s")
            except Exception as e:
                logger.error(f"Ethereum transactions query failed: {str(e)}")
                
            try:
                # Try to get transfers with timeout
                transfers_start = time.time()
                transfers_by_tx = transfers_future.result(timeout=FUTURE_TIMEOUT)
                transfers_time = time.time() - transfers_start
                logger.info(f"Transfers query returned data for {len(transfers_by_tx)} txs in {transfers_time:.2f}s")
            except concurrent.futures.TimeoutError:
                logger.error(f"Transfers query TIMED OUT after {FUTURE_TIMEOUT}s")
                # Continue with empty transfers
            except Exception as e:
                logger.error(f"Transfers query failed: {str(e)}")
        
        parallel_time = time.time() - batch_start
        self.performance_metrics["queries"].append({
            "name": "Parallel query execution",
            "time": parallel_time
        })
        
        # Process only if we have at least ethereum transactions data
        if not ethereum_transactions:
            logger.error("No ethereum transaction data available - unable to process batch")
            return []
        
        # These steps need to be processed sequentially
        token_start = time.time()
        logger.info("Starting token lookups...")
        token_addresses = self._extract_token_addresses(transfers_by_tx)
        logger.info(f"Found {len(token_addresses)} token addresses to look up")
        tokens_by_address = self._fetch_tokens_with_cache(token_addresses)
        logger.info(f"Token lookup completed in {time.time() - token_start:.2f}s")
        self._map_tokens_to_transfers(transfers_by_tx, tokens_by_address)
        token_time = time.time() - token_start
        
        # Build final result
        build_start = time.time()
        logger.info("Building final result...")
        result = self._build_final_result(
            ethereum_tx_ids,
            multisig_transactions,
            module_transactions,
            ethereum_transactions,
            transfers_by_tx
        )
        logger.info(f"Result built with {len(result)} items in {time.time() - build_start:.2f}s")
        build_time = time.time() - build_start
        
        self.performance_metrics["queries"].append({
            "name": "Batch processing",
            "count": len(result),
            "time": time.time() - batch_start
        })
        
        # Overall timing
        total_time = time.time() - batch_start
        logger.info(f"Total batch processing time: {total_time:.2f}s")
        
        return result

    def _log_performance_metrics(self):
        """Log performance metrics to console"""
        logger.info("Performance metrics for GlobalTransactionService:")
        for query in self.performance_metrics["queries"]:
            logger.info(f"  {query.get('name', 'N/A')}: {query.get('count', 'N/A')} records in {query['time']:.4f} seconds")
        if "cache_hits" in self.performance_metrics:
            logger.info(f"  Cache hits: {self.performance_metrics['cache_hits']}")
            logger.info(f"  Cache misses: {self.performance_metrics['cache_misses']}")
        logger.info(f"  Total time: {self.performance_metrics['total_time']:.4f} seconds")

    def get_performance_metrics(self) -> Dict[str, Any]:
        """Return the current performance metrics"""
        return self.performance_metrics

    def _fetch_multisig_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[MultisigTransaction]]:
        """
        Fetch all MultisigTransactions for the given ethereum_tx_ids.
        Using optimized query with proper indices.
        """
        start_time = time.time()
        
        # Set query timeout
        with connection.cursor() as cursor:
            cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
            logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for multisig transactions query")
        
        try:
            # Convert binary IDs to hex format if needed for consistent querying
            normalized_ids = []
            for tx_id in ethereum_tx_ids:
                if isinstance(tx_id, bytes):
                    normalized_ids.append(tx_id)
                elif tx_id.startswith('0x'):
                    # Convert hex string to bytes
                    normalized_ids.append(bytes.fromhex(tx_id[2:]))
                else:
                    # Already hex string without prefix
                    normalized_ids.append(bytes.fromhex(tx_id))
            
            logger.debug(f"Fetching multisig transactions with {len(normalized_ids)} tx IDs")
            # Use select_related and prefetch_related to minimize database queries
            queryset = (
                MultisigTransaction.objects.filter(ethereum_tx_id__in=normalized_ids)
                .with_confirmations_required()
                .prefetch_related("confirmations")
                .select_related("ethereum_tx__block")
                .order_by("-nonce", "-created")
            )
            
            # Force evaluation of queryset
            multisig_txs = list(queryset)
            query_count = len(multisig_txs)
            
            # Helper function for consistent tx_id format
            def ensure_string_tx_id(tx_id):
                return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
            
            # Group by ethereum_tx_id for efficient lookups
            result = {}
            for tx in multisig_txs:
                tx_id = ensure_string_tx_id(tx.ethereum_tx_id)
                result.setdefault(tx_id, []).append(tx)
                
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch multisig transactions",
                "count": query_count,
                "time": elapsed
            })
            
            logger.debug(
                "[%s] Fetched %d MultisigTransactions for %d ethereum_tx_ids",
                __name__,
                query_count,
                len(ethereum_tx_ids),
            )
            
            return result
        except Exception as e:
            logger.error(f"Error fetching multisig transactions: {str(e)}")
            # Return empty dict on error to allow processing to continue
            return {}

    def _fetch_module_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[ModuleTransaction]]:
        """
        Fetch all ModuleTransactions for the given ethereum_tx_ids.
        Using optimized query with proper indices.
        """
        start_time = time.time()
        
        # Set query timeout
        with connection.cursor() as cursor:
            cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
            logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for module transactions query")
        
        try:
            # Convert binary IDs to hex format if needed for consistent querying
            normalized_ids = []
            for tx_id in ethereum_tx_ids:
                if isinstance(tx_id, bytes):
                    normalized_ids.append(tx_id)
                elif tx_id.startswith('0x'):
                    # Convert hex string to bytes
                    normalized_ids.append(bytes.fromhex(tx_id[2:]))
                else:
                    # Already hex string without prefix
                    normalized_ids.append(bytes.fromhex(tx_id))
            
            logger.debug(f"Fetching module transactions with {len(normalized_ids)} tx IDs")
            queryset = (
                ModuleTransaction.objects.filter(internal_tx__ethereum_tx_id__in=normalized_ids)
                .select_related("internal_tx__ethereum_tx", "internal_tx__ethereum_tx__block")
            )
            
            # Force evaluation of queryset
            module_txs = list(queryset)
            query_count = len(module_txs)
            
            # Helper function for consistent tx_id format
            def ensure_string_tx_id(tx_id):
                return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
            
            # Group by ethereum_tx_id for efficient lookups
            result = {}
            for tx in module_txs:
                eth_tx_id = ensure_string_tx_id(tx.internal_tx.ethereum_tx_id)
                result.setdefault(eth_tx_id, []).append(tx)
            
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch module transactions",
                "count": query_count,
                "time": elapsed
            })
            
            logger.debug(
                "[%s] Fetched %d ModuleTransactions for %d ethereum_tx_ids",
                __name__,
                query_count,
                len(ethereum_tx_ids),
            )
            
            return result
        except Exception as e:
            logger.error(f"Error fetching module transactions: {str(e)}")
            # Return empty dict on error to allow processing to continue
            return {}

    def _fetch_ethereum_transactions(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, EthereumTx]:
        """
        Fetch all EthereumTxs for the given ethereum_tx_ids.
        Using optimized query with proper indices.
        """
        start_time = time.time()
        
        # Set query timeout
        with connection.cursor() as cursor:
            cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
            logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for ethereum transactions query")
        
        try:
            # Convert binary IDs to hex format if needed for consistent querying
            normalized_ids = []
            for tx_id in ethereum_tx_ids:
                if isinstance(tx_id, bytes):
                    normalized_ids.append(tx_id)
                elif tx_id.startswith('0x'):
                    # Convert hex string to bytes
                    normalized_ids.append(bytes.fromhex(tx_id[2:]))
                else:
                    # Already hex string without prefix
                    normalized_ids.append(bytes.fromhex(tx_id))
            
            logger.debug(f"Fetching ethereum transactions with {len(normalized_ids)} tx IDs")
            queryset = (
                EthereumTx.objects.filter(tx_hash__in=normalized_ids)
                .select_related("block")
            )
            
            # Force evaluation and map by tx_hash
            result = {}
            for tx in queryset:
                tx_id = tx.tx_hash.hex() if isinstance(tx.tx_hash, bytes) else tx.tx_hash
                result[tx_id] = tx
            
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch ethereum transactions",
                "count": len(result),
                "time": elapsed
            })
            
            logger.debug(
                "[%s] Fetched %d EthereumTxs for %d ethereum_tx_ids",
                __name__,
                len(result),
                len(ethereum_tx_ids),
            )
            
            return result
        except Exception as e:
            logger.error(f"Error fetching ethereum transactions: {str(e)}")
            # Return empty dict on error to allow processing to continue
            return {}

    def _fetch_all_transfers(
        self, ethereum_tx_ids: List[str]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Fetch all transfers (ERC20, ERC721, Ether) for the given ethereum_tx_ids.
        Using sequential fetching for different transfer types to avoid parallelization issues.
        """
        start_time = time.time()
        transfers_by_tx = {}
        logger.info(f"Starting transfer fetching for {len(ethereum_tx_ids)} tx_ids")
        
        # Convert binary IDs to hex format if needed for consistent querying
        normalized_ids = []
        for tx_id in ethereum_tx_ids:
            if isinstance(tx_id, bytes):
                normalized_ids.append(tx_id)
            elif tx_id.startswith('0x'):
                # Convert hex string to bytes
                normalized_ids.append(bytes.fromhex(tx_id[2:]))
            else:
                # Already hex string without prefix
                normalized_ids.append(bytes.fromhex(tx_id))
        
        # Fetch transfers sequentially instead of in parallel
        logger.info("Fetching transfers sequentially...")
        
        # Fetch ERC20 transfers
        erc20_start = time.time()
        logger.info("Fetching ERC20 transfers...")
        try:
            erc20_transfers = self._fetch_erc20_transfers(normalized_ids)
            logger.info(f"ERC20 transfers fetched in {time.time() - erc20_start:.2f}s: {len(erc20_transfers)} results")
        except Exception as e:
            logger.error(f"Error fetching ERC20 transfers: {str(e)}")
            erc20_transfers = []
        
        # Fetch ERC721 transfers
        erc721_start = time.time()
        logger.info("Fetching ERC721 transfers...")
        try:
            erc721_transfers = self._fetch_erc721_transfers(normalized_ids)
            logger.info(f"ERC721 transfers fetched in {time.time() - erc721_start:.2f}s: {len(erc721_transfers)} results")
        except Exception as e:
            logger.error(f"Error fetching ERC721 transfers: {str(e)}")
            erc721_transfers = []
        
        # Fetch Ether transfers
        ether_start = time.time()
        logger.info("Fetching Ether transfers...")
        try:
            ether_transfers = self._fetch_ether_transfers(normalized_ids)
            logger.info(f"Ether transfers fetched in {time.time() - ether_start:.2f}s: {len(ether_transfers)} results")
        except Exception as e:
            logger.error(f"Error fetching Ether transfers: {str(e)}")
            ether_transfers = []
        
        # Helper function to ensure consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        logger.info("Processing ERC20 transfers...")
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
        
        logger.info("Processing ERC721 transfers...")
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
        
        logger.info("Processing Ether transfers...")
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
        
        total_transfers = sum(len(transfers) for transfers in transfers_by_tx.values())
        elapsed = time.time() - start_time
        
        self.performance_metrics["queries"].append({
            "name": "Fetch all transfers (sequential)",
            "count": total_transfers,
            "time": elapsed
        })
        
        logger.info(f"All transfer processing completed in {elapsed:.2f}s")
        logger.debug(
            "[%s] Fetched %d transfers for %d ethereum_tx_ids",
            __name__,
            total_transfers,
            len(ethereum_tx_ids),
        )
        
        return transfers_by_tx
    
    def _fetch_erc20_transfers(self, ethereum_tx_ids: List):
        """Fetch ERC20 transfers for the specified transaction IDs"""
        start_time = time.time()
        
        try:
            # Set query timeout
            with connection.cursor() as cursor:
                cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
                logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for ERC20 transfers query")
            
            # Add EXPLAIN query first to check execution plan
            logger.debug("Running EXPLAIN for ERC20 transfers query")
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        "EXPLAIN ANALYZE SELECT * FROM history_erc20transfer WHERE ethereum_tx_id IN %s",
                        (tuple(ethereum_tx_ids),)
                    )
                    execution_plan = cursor.fetchall()
                    for line in execution_plan:
                        logger.debug(f"ERC20 EXPLAIN: {line[0]}")
                except Exception as e:
                    logger.error(f"Error running EXPLAIN for ERC20 transfers: {str(e)}")
            
            logger.info(f"Executing ERC20 transfers query for {len(ethereum_tx_ids)} tx_ids")
            erc20_transfers = (
                ERC20Transfer.objects.filter(ethereum_tx_id__in=ethereum_tx_ids)
                .select_related("ethereum_tx__block")
            )
            
            # Force evaluation
            query_start = time.time()
            erc20_list = list(erc20_transfers)
            query_time = time.time() - query_start
            
            logger.info(f"ERC20 transfers query execution completed in {query_time:.4f}s")
            if query_time > 2.0:  # Flag slow queries
                logger.warning(f"SLOW QUERY: ERC20 transfers took {query_time:.4f}s for {len(erc20_list)} records")
            
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch ERC20 transfers",
                "count": len(erc20_list),
                "time": elapsed
            })
            
            return erc20_list
        except Exception as e:
            logger.error(f"Error fetching ERC20 transfers: {str(e)}", exc_info=True)
            # Return empty list on error to allow processing to continue
            return []
    
    def _fetch_erc721_transfers(self, ethereum_tx_ids: List):
        """Fetch ERC721 transfers for the specified transaction IDs"""
        start_time = time.time()
        
        try:
            # Set query timeout
            with connection.cursor() as cursor:
                cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
                logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for ERC721 transfers query")
            
            # Add EXPLAIN query first to check execution plan
            logger.debug("Running EXPLAIN for ERC721 transfers query")
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        "EXPLAIN ANALYZE SELECT * FROM history_erc721transfer WHERE ethereum_tx_id IN %s",
                        (tuple(ethereum_tx_ids),)
                    )
                    execution_plan = cursor.fetchall()
                    for line in execution_plan:
                        logger.debug(f"ERC721 EXPLAIN: {line[0]}")
                except Exception as e:
                    logger.error(f"Error running EXPLAIN for ERC721 transfers: {str(e)}")
            
            logger.info(f"Executing ERC721 transfers query for {len(ethereum_tx_ids)} tx_ids")
            erc721_transfers = (
                ERC721Transfer.objects.filter(ethereum_tx_id__in=ethereum_tx_ids)
                .select_related("ethereum_tx__block")
            )
            
            # Force evaluation
            query_start = time.time()
            erc721_list = list(erc721_transfers)
            query_time = time.time() - query_start
            
            logger.info(f"ERC721 transfers query execution completed in {query_time:.4f}s")
            if query_time > 2.0:  # Flag slow queries
                logger.warning(f"SLOW QUERY: ERC721 transfers took {query_time:.4f}s for {len(erc721_list)} records")
            
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch ERC721 transfers",
                "count": len(erc721_list),
                "time": elapsed
            })
            
            return erc721_list
        except Exception as e:
            logger.error(f"Error fetching ERC721 transfers: {str(e)}", exc_info=True)
            # Return empty list on error to allow processing to continue
            return []
    
    def _fetch_ether_transfers(self, ethereum_tx_ids: List):
        """Fetch Ether transfers for the specified transaction IDs"""
        start_time = time.time()
        
        try:
            # Set query timeout
            with connection.cursor() as cursor:
                cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
                logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for Ether transfers query")
            
            # Add EXPLAIN query first to check execution plan
            logger.debug("Running EXPLAIN for Ether transfers query")
            with connection.cursor() as cursor:
                try:
                    cursor.execute(
                        "EXPLAIN ANALYZE SELECT * FROM history_internaltx WHERE ethereum_tx_id IN %s AND value > 0",
                        (tuple(ethereum_tx_ids),)
                    )
                    execution_plan = cursor.fetchall()
                    for line in execution_plan:
                        logger.debug(f"Ether EXPLAIN: {line[0]}")
                except Exception as e:
                    logger.error(f"Error running EXPLAIN for Ether transfers: {str(e)}")
            
            logger.info(f"Executing Ether transfers query for {len(ethereum_tx_ids)} tx_ids")
            ether_transfers = (
                InternalTx.objects.filter(ethereum_tx_id__in=ethereum_tx_ids, value__gt=0)
                .select_related("ethereum_tx__block")
            )
            
            # Force evaluation
            query_start = time.time()
            ether_list = list(ether_transfers)
            query_time = time.time() - query_start
            
            logger.info(f"Ether transfers query execution completed in {query_time:.4f}s")
            if query_time > 2.0:  # Flag slow queries
                logger.warning(f"SLOW QUERY: Ether transfers took {query_time:.4f}s for {len(ether_list)} records")
            
            elapsed = time.time() - start_time
            self.performance_metrics["queries"].append({
                "name": "Fetch Ether transfers",
                "count": len(ether_list),
                "time": elapsed
            })
            
            return ether_list
        except Exception as e:
            logger.error(f"Error fetching Ether transfers: {str(e)}", exc_info=True)
            # Return empty list on error to allow processing to continue
            return []

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

    def _fetch_tokens_with_cache(self, token_addresses: Set[str]) -> Dict[str, Token]:
        """
        Fetch token information with caching for improved performance.
        Token information rarely changes, so can be cached for longer periods.
        """
        if not token_addresses:
            return {}
            
        start_time = time.time()
        tokens_by_address = {}
        addresses_to_fetch = set()
        
        # Try to get tokens from cache first
        for address in token_addresses:
            cache_key = f"token_info_{address}"
            cached_token = cache.get(cache_key)
            if cached_token:
                tokens_by_address[address] = cached_token
                self.performance_metrics["cache_hits"] += 1
            else:
                addresses_to_fetch.add(address)
                self.performance_metrics["cache_misses"] += 1
        
        # Fetch only tokens that aren't in cache
        if addresses_to_fetch:
            # Set query timeout
            with connection.cursor() as cursor:
                cursor.execute(f'SET statement_timeout = {QUERY_TIMEOUT_MS};')
                logger.debug(f"Set statement timeout to {QUERY_TIMEOUT_MS}ms for token information query")
            
            try:
                logger.debug(f"Fetching {len(addresses_to_fetch)} tokens from database")
                tokens = Token.objects.filter(address__in=addresses_to_fetch)
                
                # Update cache and result dict
                for token in tokens:
                    tokens_by_address[token.address] = token
                    cache_key = f"token_info_{token.address}"
                    cache.set(cache_key, token, TOKEN_CACHE_TTL)
            except Exception as e:
                logger.error(f"Error fetching token information: {str(e)}")
                # Continue with whatever tokens we have from cache
                pass
        
        elapsed = time.time() - start_time
        self.performance_metrics["queries"].append({
            "name": "Fetch token information (with cache)",
            "count": len(tokens_by_address),
            "cache_hits": len(token_addresses) - len(addresses_to_fetch),
            "db_queries": 1 if addresses_to_fetch else 0,
            "time": elapsed
        })
        
        logger.debug(
            "[%s] Fetched %d tokens for %d addresses (cache hits: %d, misses: %d)",
            __name__,
            len(tokens_by_address),
            len(token_addresses),
            len(token_addresses) - len(addresses_to_fetch),
            len(addresses_to_fetch),
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
        start_time = time.time()
        
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
        
        elapsed = time.time() - start_time
        self.performance_metrics["queries"].append({
            "name": "Map tokens to transfers",
            "time": elapsed
        })

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
        Optimized for minimal in-memory processing.
        """
        start_time = time.time()
        
        # Create result dict for maintaining transaction order
        result_dict = {tx_id: None for tx_id in ethereum_tx_ids}
        
        # Set to track processed transactions to avoid duplicates
        processed_multisig_hashes = set()
        processed_module_ids = set()
        
        # Helper function for consistent tx_id format
        def ensure_string_tx_id(tx_id):
            return tx_id.hex() if isinstance(tx_id, bytes) else tx_id
        
        # Process in order of the original ethereum_tx_ids to maintain ordering
        for eth_tx_id in ethereum_tx_ids:
            # Normalize eth_tx_id format
            norm_eth_tx_id = ensure_string_tx_id(eth_tx_id)
            
            # Get all data for this ethereum_tx
            multisig_txs = multisig_transactions.get(norm_eth_tx_id, [])
            module_txs = module_transactions.get(norm_eth_tx_id, [])
            ethereum_tx = ethereum_transactions.get(norm_eth_tx_id)
            transfers = transfers_by_tx.get(norm_eth_tx_id, [])
            
            # Process MultisigTransactions
            has_specific_tx = False
            for multisig_tx in multisig_txs:
                # Ensure unique identifier for safe_tx_hash
                safe_tx_hash = multisig_tx.safe_tx_hash.hex() if isinstance(multisig_tx.safe_tx_hash, bytes) else multisig_tx.safe_tx_hash
                
                if safe_tx_hash not in processed_multisig_hashes:
                    processed_multisig_hashes.add(safe_tx_hash)
                    # Format according to the required structure
                    tx_data = self._format_multisig_transaction(multisig_tx, transfers)
                    result_dict[norm_eth_tx_id] = tx_data
                    has_specific_tx = True
            
            # Process ModuleTransactions
            for module_tx in module_txs:
                if module_tx.internal_tx_id not in processed_module_ids:
                    processed_module_ids.add(module_tx.internal_tx_id)
                    # Format according to the required structure
                    tx_data = self._format_module_transaction(module_tx, transfers)
                    result_dict[norm_eth_tx_id] = tx_data
                    has_specific_tx = True
            
            # Process EthereumTx if it wasn't part of a Multisig or Module transaction
            # but has transfers (usually incoming transfers)
            if (not has_specific_tx and ethereum_tx and transfers):
                # Format according to the required structure
                tx_data = self._format_ethereum_transaction(ethereum_tx, transfers)
                result_dict[norm_eth_tx_id] = tx_data
        
        # Build final result list, preserving order and filtering None values
        final_result = [tx_data for tx_id, tx_data in result_dict.items() if tx_data is not None]
        
        elapsed = time.time() - start_time
        self.performance_metrics["queries"].append({
            "name": "Build final result",
            "count": len(final_result),
            "time": elapsed
        })
        
        return final_result

    def _format_multisig_transaction(
        self, tx: MultisigTransaction, transfers: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Format a MultisigTransaction according to the required response structure.
        Optimized for performance with pre-computed values.
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
        
        # Handle confirmations efficiently
        if hasattr(tx, 'confirmations'):
            confirmations = tx.confirmations
            # Handle RelatedManager (Django ORM)
            if hasattr(confirmations, 'all'):
                # Convert to list to avoid additional database queries
                confirmations = list(confirmations.all())
            
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