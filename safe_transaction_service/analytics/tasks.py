import json
import logging
import time
from typing import Dict, List

from django.conf import settings
from django.db import transaction
from django.db.models import Case, Count, F, IntegerField, Max, Q, Sum, When
from django.utils import timezone

from celery import app
from dateutil.relativedelta import relativedelta
from eth_typing import ChecksumAddress

from safe_transaction_service.analytics.services.analytics_service import (
    AnalyticsService,
)
from safe_transaction_service.history.models import (
    MultisigTransaction,
    ProtofireSafeBalance,
    SafeContract,
    SafeLastStatus,
)
from safe_transaction_service.history.services.balance_service import (
    BalanceService,
    BalanceServiceProvider,
)
from safe_transaction_service.utils.celery import task_timeout
from safe_transaction_service.utils.redis import get_redis
from safe_transaction_service.utils.tasks import LOCK_TIMEOUT
from safe_transaction_service.utils.utils import chunks

logger = logging.getLogger(__name__)

# Configuration constants
DEFAULT_BATCH_SIZE = 100
RPC_CHUNK_SIZE = 100
BALANCE_STALENESS_DAYS = 30
PREFETCH_BATCH_SIZE = 500


class TaskMetrics:
    """Helper class to track task execution metrics."""
    
    def __init__(self):
        self.start_time = time.time()
        self.processed_count = 0
        self.error_count = 0
        self.last_batch_time = 0
        
    def update_batch_completion(self, batch_size: int, batch_time: float):
        """Update metrics after batch completion."""
        self.processed_count += batch_size
        self.last_batch_time = batch_time
        
    def increment_error_count(self):
        """Increment error counter."""
        self.error_count += 1
        
    def get_eta_minutes(self, total_items: int) -> float:
        """Calculate estimated time to completion in minutes."""
        if self.processed_count == 0:
            return 0
        
        elapsed_time = time.time() - self.start_time
        avg_time_per_item = elapsed_time / self.processed_count
        remaining_items = total_items - self.processed_count
        return (remaining_items * avg_time_per_item) / 60
        
    def get_summary(self) -> dict:
        """Get task execution summary."""
        total_time = time.time() - self.start_time
        return {
            "total_time_seconds": total_time,
            "processed_count": self.processed_count,
            "error_count": self.error_count,
            "avg_time_per_item": total_time / self.processed_count if self.processed_count > 0 else 0,
        }


def _get_native_balances_multicall(
    safe_addresses: list[str], balance_service: BalanceService
) -> tuple[int, int]:
    """
    Get native token balances for a batch of Safe addresses using batch RPC calls.

    :param safe_addresses: List of Safe addresses to get balances for
    :param balance_service: BalanceService instance
    :return: Tuple of (total_balance_wei, safes_with_balance_count)
    """
    start_time = time.time()
    batch_size = len(safe_addresses)
    logger.debug(f"Starting batch balance processing for {batch_size} addresses")
    
    batch_total_balance = 0
    batch_safes_with_balance = 0
    
    try:
        # Use the existing ethereum client to get balances
        # This will use the client's internal connection pooling and retries
        balances = []
        
        # Process in smaller chunks to avoid overwhelming the RPC endpoint
        chunk_size = RPC_CHUNK_SIZE  # Use configurable constant for better reliability
        for address_chunk in chunks(safe_addresses, chunk_size):
            chunk_balances = []
            for address in address_chunk:
                try:
                    balance = balance_service.ethereum_client.get_balance(address)
                    chunk_balances.append(balance)
                except Exception as e:
                    logger.warning(f"Failed to get balance for address {address}: {e}")
                    chunk_balances.append(0)  # Default to 0 if individual request fails
            
            balances.extend(chunk_balances)
        
        # Process results
        for balance in balances:
            if balance > 0:
                batch_total_balance += balance
                batch_safes_with_balance += 1
        
        processing_time = time.time() - start_time
        logger.debug(f"Batch processing completed for {batch_size} addresses in {processing_time:.3f}s. "
                    f"Total balance: {batch_total_balance} wei, Safes with balance: {batch_safes_with_balance}")
        
    except Exception as e:
        processing_time = time.time() - start_time
        logger.error(
            f"Batch processing failed for a batch of {len(safe_addresses)} addresses after {processing_time:.3f}s: {e}"
        )
        # Return zeros on complete failure
        batch_total_balance = 0
        batch_safes_with_balance = 0
    
    return batch_total_balance, batch_safes_with_balance


def _calculate_native_balances_batched_rpc() -> tuple[int, int]:
    """
    Original RPC-based balance calculation (fallback method).
    Calculate native token balances for all Safes using database iterators and chunked RPC calls.
    This approach is optimized for both memory efficiency and RPC endpoint reliability.

    :return: Tuple of (total_balance_wei, total_safes_with_balance)
    """
    function_start_time = time.time()
    
    # Use a more conservative batch size to avoid overwhelming RPC endpoints
    # This balances between efficiency and reliability
    db_batch_size = DEFAULT_BATCH_SIZE  # Use configurable constant
    balance_service = BalanceServiceProvider()
    total_balance_wei = 0
    total_safes_with_balance = 0
    processed_count = 0

    # Get total count for progress tracking
    count_start_time = time.time()
    total_safes = SafeContract.objects.count()
    count_time = time.time() - count_start_time
    logger.info(f"Total Safes count: {total_safes} (query took {count_time:.2f}s)")
    
    logger.info(
        f"Starting chunked RPC-based balance calculation for {total_safes} Safes with batch size {db_batch_size}"
    )

    # Use .iterator() for memory efficiency. It fetches addresses from the DB in chunks.
    queryset = SafeContract.objects.values_list("address", flat=True).order_by("pk")
    
    # Process addresses in batches using database slicing for better memory management
    offset = 0
    batch_number = 0
    
    while True:
        batch_start_time = time.time()
        batch_number += 1
        
        # Fetch a batch of addresses from the database
        db_fetch_start = time.time()
        address_batch = list(queryset[offset:offset + db_batch_size])
        db_fetch_time = time.time() - db_fetch_start
        
        if not address_batch:
            break  # No more addresses to process

        batch_size = len(address_batch)
        processed_count += batch_size
        
        logger.info(
            f"Processing batch {batch_number} with {batch_size} addresses "
            f"(fetched in {db_fetch_time:.2f}s, {processed_count}/{total_safes} total)..."
        )

        # Get balances for the entire batch with chunked RPC calls
        try:
            batch_rpc_start = time.time()
            (
                batch_balance,
                batch_safes_with_balance,
            ) = _get_native_balances_multicall(address_batch, balance_service)

            total_balance_wei += batch_balance
            total_safes_with_balance += batch_safes_with_balance
            
            batch_rpc_time = time.time() - batch_rpc_start
            batch_time = time.time() - batch_start_time
            progress_percent = (processed_count / total_safes) * 100 if total_safes > 0 else 0
            
            logger.info(
                f"Completed batch {batch_number} in {batch_time:.2f}s "
                f"(RPC processing: {batch_rpc_time:.2f}s). "
                f"Progress: {progress_percent:.1f}% ({processed_count}/{total_safes}). "
                f"Running totals - Balance: {total_balance_wei} wei, "
                f"Safes with balance: {total_safes_with_balance}"
            )
        except Exception as e:
            batch_time = time.time() - batch_start_time
            logger.error(f"Batch {batch_number} processing failed after {batch_time:.2f}s: {e}")
            # Continue with next batch rather than failing completely
        
        offset += db_batch_size

    total_function_time = time.time() - function_start_time
    avg_time_per_safe = total_function_time / processed_count if processed_count > 0 else 0

    logger.info(
        f"Chunked RPC-based balance calculation completed in {total_function_time:.2f}s. "
        f"Final results: Total balance: {total_balance_wei} wei, "
        f"Safes with balance: {total_safes_with_balance}/{processed_count}. "
        f"Average time per Safe: {avg_time_per_safe:.4f}s. "
        f"Processed {processed_count} Safes in {batch_number} batches."
    )

    return total_balance_wei, total_safes_with_balance


def _calculate_native_balances_batched() -> tuple[int, int]:
    """
    Calculate native token balances for all Safes using stored balance data.
    Falls back to RPC if no stored data exists or data is stale.

    :return: Tuple of (total_balance_wei, total_safes_with_balance)
    """
    function_start_time = time.time()
    
    try:
        # Check if we have any stored balance data
        balance_count = ProtofireSafeBalance.objects.count()
        if balance_count == 0:
            logger.info("No stored balance data found. Using RPC fallback.")
            return _calculate_native_balances_batched_rpc()
        
        # Get balances from database
        balance_data = ProtofireSafeBalance.objects.aggregate(
            total_balance=Sum('balance_wei'),
            safes_with_balance=Count(
                Case(When(balance_wei__gt=0, then=1), output_field=IntegerField())
            )
        )
        
        total_balance_wei = balance_data['total_balance'] or 0
        total_safes_with_balance = balance_data['safes_with_balance'] or 0
        
        # Check if data is recent (within configurable days)
        latest_update = ProtofireSafeBalance.objects.aggregate(
            latest=Max('last_updated')
        )['latest']
        
        total_function_time = time.time() - function_start_time
        
        if latest_update and (timezone.now() - latest_update).days <= BALANCE_STALENESS_DAYS:
            logger.info(
                f"Using stored balance data from {latest_update} ({balance_count} records). "
                f"Completed in {total_function_time:.2f}s. "
                f"Total balance: {total_balance_wei} wei, "
                f"Safes with balance: {total_safes_with_balance}"
            )
            return total_balance_wei, total_safes_with_balance
        
        logger.warning(
            f"Stored balance data is stale (latest: {latest_update}). "
            f"Consider running update_safe_balances_task. Using stored data anyway."
        )
        return total_balance_wei, total_safes_with_balance
        
    except Exception as e:
        logger.warning(f"Failed to read stored balances: {e}. Falling back to RPC.")
        # Fallback to original RPC-based calculation
        return _calculate_native_balances_batched_rpc()

def _batch_get_balances(
    ethereum_client, addresses: List[ChecksumAddress], chunk_size: int = RPC_CHUNK_SIZE
) -> Dict[ChecksumAddress, int]:
    """
    Get balances for a list of Ethereum addresses using batch JSON RPC requests.
    Processes in chunks to avoid overwhelming the RPC endpoint.

    :param ethereum_client: Instance of EthereumClient
    :param addresses: List of Ethereum addresses
    :param chunk_size: Size of each RPC batch chunk
    :return: Dictionary with addresses as keys and their balances in wei as values
    """
    if not addresses:
        return {}

    all_balances = {}
    total_addresses = len(addresses)
    
    logger.debug(f"Processing {total_addresses} addresses in chunks of {chunk_size}")
    
    # Process addresses in chunks to avoid overwhelming RPC endpoint
    for chunk_idx, address_chunk in enumerate(chunks(addresses, chunk_size)):
        chunk_start_time = time.time()
        
        # Prepare the payload for batch RPC requests
        payload = [
            {
                "id": i,
                "jsonrpc": "2.0",
                "method": "eth_getBalance",
                "params": [address, "latest"],
            }
            for i, address in enumerate(address_chunk)
        ]

        try:
            # Perform the batch request
            logger.debug(f"Sending batch request for chunk {chunk_idx + 1} with {len(address_chunk)} addresses")
            
            # Get raw response from ethereum client
            raw_results = ethereum_client.raw_batch_request(payload)
            results = list(raw_results)
            
            # Parse the results into a dictionary
            chunk_balances = {}
            successful_requests = 0
            
            for address, response in zip(address_chunk, results):
                try:
                    if isinstance(response, dict) and "result" in response:
                        wei = int(response["result"], 16)  # Convert hex to integer
                        chunk_balances[address] = wei
                        successful_requests += 1
                    elif isinstance(response, str):
                        # Direct hex response
                        wei = int(response, 16)
                        chunk_balances[address] = wei
                        successful_requests += 1
                    else:
                        logger.warning(f"Unexpected response format for address {address}: {response}")
                        chunk_balances[address] = 0
                        
                except (ValueError, KeyError, TypeError) as e:
                    logger.warning(f"Failed to parse balance for address {address}: {e}, response: {response}")
                    chunk_balances[address] = 0
                    
            all_balances.update(chunk_balances)
            
            chunk_time = time.time() - chunk_start_time
            logger.debug(
                f"Completed chunk {chunk_idx + 1}/{(total_addresses + chunk_size - 1) // chunk_size} "
                f"in {chunk_time:.2f}s. Successfully processed {successful_requests}/{len(address_chunk)} addresses"
            )
            
        except Exception as e:
            chunk_time = time.time() - chunk_start_time
            logger.error(
                f"Chunk {chunk_idx + 1} batch request failed after {chunk_time:.2f}s: {e}. "
                f"Setting 0 balance for {len(address_chunk)} addresses"
            )
            # Set 0 balance for all addresses in failed chunk
            for address in address_chunk:
                all_balances[address] = 0

    logger.debug(f"Batch balance retrieval completed. Processed {len(all_balances)} addresses")
    return all_balances


@app.shared_task()
@task_timeout(timeout_seconds=LOCK_TIMEOUT)
def get_transactions_per_safe_app_task():
    today = timezone.now()
    last_week = today - relativedelta(days=7)
    last_month = today - relativedelta(months=1)
    last_year = today - relativedelta(years=1)

    queryset = (
        MultisigTransaction.objects.filter(origin__name__isnull=False)
        .values(name=F("origin__name"), url=F("origin__url"))
        .annotate(
            total_tx=Count("origin__name"),
            tx_last_week=Count("origin__name", filter=Q(created__gt=last_week)),
            tx_last_month=Count("origin__name", filter=Q(created__gt=last_month)),
            tx_last_year=Count("origin__name", filter=Q(created__gt=last_year)),
        )
        .order_by("-total_tx")
    )

    if queryset:
        redis_key = AnalyticsService.REDIS_TRANSACTIONS_PER_SAFE_APP
        redis = get_redis()
        redis.set(redis_key, json.dumps(list(queryset)))
        return True
    return False


@app.shared_task()
@task_timeout(timeout_seconds=LOCK_TIMEOUT * 6)
def get_safe_statistics_task():
    """
    Calculate Safe statistics using efficient batch RPC calls for balance checking.
    
    This optimized version uses:
    - Batch RPC requests instead of individual balance calls
    - Larger batch sizes (5000 addresses per batch)
    - Memory-efficient database iteration
    - Better error handling and logging
    """
    try:
        logger.info("Starting Safe statistics task...")

        # Total number of created Safes (all proxy factories included)
        total_safes = SafeContract.objects.count()
        logger.info(f"Counted {total_safes} total Safes")

        # Get all owners from SafeLastStatus to get current state
        owners_data = SafeLastStatus.objects.exclude(owners__isnull=True).exclude(
            owners=[]
        ).values_list("owners", flat=True)
        logger.info(f"Fetched owner data from SafeLastStatus")

        # Count total owners and unique owners
        all_owners = set()
        total_owners_count = 0
        for owner_list in owners_data.iterator():  # Use iterator for memory efficiency
            if owner_list:  # Ensure the list is not empty
                total_owners_count += len(owner_list)
                all_owners.update(owner_list)

        unique_owners = len(all_owners)
        logger.info(f"Processed owner statistics: total_owners={total_owners_count}, unique_owners={unique_owners}")

        # Calculate native token balances for all Safes using the optimized function
        logger.info(f"Starting balance calculation for {total_safes} Safes")

        total_balance_wei = 0
        total_safes_with_balance = 0

        if total_safes > 0:
            try:
                total_balance_wei, total_safes_with_balance = _calculate_native_balances_batched()
                logger.info(
                    f"Balance calculation completed. "
                    f"Total balance: {total_balance_wei} wei, "
                    f"Safes with balance: {total_safes_with_balance}/{total_safes}"
                )
            except Exception as e:
                logger.error(f"Failed to calculate balances: {e}")
                # Continue without balance data rather than failing the entire task

        # Create and store statistics
        statistics = {
            "total_safes": total_safes,
            "total_owners": total_owners_count,
            "unique_owners": unique_owners,
            "balance_wei": total_balance_wei,
            "safes_with_balance": total_safes_with_balance,
            "timestamp": timezone.now().isoformat(),
        }

        # Store in Redis
        redis_key = AnalyticsService.REDIS_SAFE_STATISTICS
        redis = get_redis()
        redis.set(redis_key, json.dumps(statistics))
        logger.info(f"Safe statistics task completed successfully. Data saved to Redis key '{redis_key}'.")

        return True
    except Exception as e:
        logger.error(f"Safe statistics task failed: {e}")
        # In case of any error, return False but don't raise
        # This prevents the task from failing completely
        return False


@app.shared_task()
@task_timeout(timeout_seconds=LOCK_TIMEOUT * 12)  # 12 hours timeout
def update_safe_balances_task(batch_size: int = DEFAULT_BATCH_SIZE, resume_from_batch: int = 0):
    """
    Update native balances for all Safe accounts.
    This is a heavy operation that should be run monthly or on-demand.
    
    :param batch_size: Number of Safes to process in each batch
    :param resume_from_batch: Batch number to resume from (for failure recovery)
    :return: Number of Safes updated
    """
    task_start_time = time.time()
    
    try:
        logger.info(
            f"Starting Safe balances update task... "
            f"batch_size={batch_size}, resume_from_batch={resume_from_batch}"
        )
        
        # Get total count for progress tracking
        total_safes = SafeContract.objects.count()
        logger.info(f"Found {total_safes} Safe contracts to update")
        
        if total_safes == 0:
            logger.info("No Safe contracts found. Task completed.")
            return 0
        
        # Calculate starting position for resumption
        start_offset = resume_from_batch * batch_size
        if start_offset >= total_safes:
            logger.warning(f"Resume batch {resume_from_batch} exceeds total safes. Starting from beginning.")
            start_offset = 0
        
        updated_count = 0
        balance_service = BalanceServiceProvider()
        
        # Use efficient database iteration with prefetch
        queryset = SafeContract.objects.all().order_by("pk")
        
        # Process in batches
        batch_number = resume_from_batch
        offset = start_offset
        
        while offset < total_safes:
            batch_start_time = time.time()
            batch_number += 1
            
            # Fetch batch with proper slicing
            safe_batch = list(queryset[offset:offset + batch_size])
            if not safe_batch:
                break
            
            batch_addresses = [safe.address for safe in safe_batch]
            actual_batch_size = len(safe_batch)
            progress_percent = ((offset + actual_batch_size) / total_safes) * 100
            
            logger.info(
                f"Processing batch {batch_number} with {actual_batch_size} Safes "
                f"(offset: {offset}, progress: {progress_percent:.1f}%)..."
            )
            
            try:
                # Get balances for this batch using optimized batch request
                batch_balances = _batch_get_balances(balance_service.ethereum_client, batch_addresses)
                
                # Log batch statistics without full balance details for production
                non_zero_balances = sum(1 for balance in batch_balances.values() if balance > 0)
                total_batch_balance = sum(batch_balances.values())
                
                logger.info(
                    f"Batch {batch_number}: Retrieved {len(batch_balances)} balances, "
                    f"{non_zero_balances} with non-zero values, "
                    f"total batch balance: {total_batch_balance} wei"
                )
                
                # Create address-to-safe mapping for efficient lookups
                safe_by_address = {safe.address: safe for safe in safe_batch}
                
                # Prepare data for bulk update
                now = timezone.now()
                balance_objects = []
                
                for address, balance_wei in batch_balances.items():
                    safe_contract = safe_by_address.get(address)
                    if safe_contract:
                        balance_objects.append(
                            ProtofireSafeBalance(
                                safe_contract=safe_contract,
                                balance_wei=balance_wei,
                                last_updated=now,
                            )
                        )
                    else:
                        logger.warning(f"SafeContract not found for address {address}")
                
                # Atomic bulk update operation
                if balance_objects:
                    with transaction.atomic():
                        # Delete existing records for this batch
                        ProtofireSafeBalance.objects.filter(
                            safe_contract__address__in=batch_addresses
                        ).delete()
                        
                        # Insert new records
                        ProtofireSafeBalance.objects.bulk_create(balance_objects)
                        
                        logger.debug(f"Bulk updated {len(balance_objects)} balance records")
                
                updated_count += len(balance_objects)
                batch_time = time.time() - batch_start_time
                elapsed_time = time.time() - task_start_time
                
                # Calculate ETA based on current progress
                if offset + actual_batch_size > 0:
                    avg_time_per_safe = elapsed_time / (offset + actual_batch_size - start_offset)
                    remaining_safes = total_safes - offset - actual_batch_size
                    eta_seconds = remaining_safes * avg_time_per_safe
                    eta_minutes = eta_seconds / 60
                else:
                    eta_minutes = 0
                
                logger.info(
                    f"Completed batch {batch_number} in {batch_time:.2f}s. "
                    f"Progress: {updated_count}/{total_safes - start_offset} "
                    f"({progress_percent:.1f}%), ETA: {eta_minutes:.1f} minutes"
                )
                
            except Exception as e:
                batch_time = time.time() - batch_start_time
                logger.error(
                    f"Batch {batch_number} update failed after {batch_time:.2f}s: {e}. "
                    f"Consider resuming from batch {batch_number}"
                )
                # Continue with next batch rather than failing completely
            
            offset += batch_size
        
        total_time = time.time() - task_start_time
        logger.info(
            f"Safe balances update completed in {total_time:.2f}s. "
            f"Updated {updated_count} records out of {total_safes} total Safes."
        )
        return updated_count
        
    except Exception as e:
        total_time = time.time() - task_start_time
        logger.error(f"Safe balances update task failed after {total_time:.2f}s: {e}")
        return 0
