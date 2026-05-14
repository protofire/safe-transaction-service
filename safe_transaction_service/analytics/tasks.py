import json
import logging
import time

from django.db import connection
from django.db.models import Count, F, Q
from django.utils import timezone

from celery import app
from dateutil.relativedelta import relativedelta

from safe_transaction_service.analytics.services.analytics_service import (
    AnalyticsService,
)
from safe_transaction_service.history.models import (
    MultisigTransaction,
    SafeContract,
    SafeLastStatus,
)
from safe_transaction_service.utils.celery import task_timeout
from safe_transaction_service.utils.redis import get_redis
from safe_transaction_service.utils.tasks import LOCK_TIMEOUT

logger = logging.getLogger(__name__)

BALANCE_BATCH_SQL = """
    SELECT
        COALESCE(SUM(CASE WHEN balance > 0 THEN balance ELSE 0 END), 0),
        COUNT(*) FILTER (WHERE balance > 0)
    FROM (
        SELECT
            addr,
            SUM(CASE WHEN direction = 1 THEN value ELSE -value END) AS balance
        FROM (
            SELECT it."to" AS addr, it.value, 1 AS direction
            FROM history_internaltx it
            WHERE it."to" = ANY(%s)
              AND it.call_type = 0 AND it.value > 0 AND it.error IS NULL
            UNION ALL
            SELECT it."_from" AS addr, it.value, -1 AS direction
            FROM history_internaltx it
            WHERE it."_from" = ANY(%s)
              AND it.call_type = 0 AND it.value > 0 AND it.error IS NULL
        ) transfers
        GROUP BY addr
    ) safe_balances
"""


def _calculate_native_balances_from_db() -> tuple[int, int]:
    """
    Calculate native token balances using DB aggregation on InternalTx,
    processed in batches to stay within the 50-second statement timeout.
    """
    start_time = time.time()
    batch_size = 5000
    total_balance_wei = 0
    total_safes_with_balance = 0
    processed = 0

    total_safes = SafeContract.objects.count()
    logger.info(
        f"Starting DB balance calculation for {total_safes} Safes "
        f"in batches of {batch_size}"
    )

    queryset = SafeContract.objects.values_list("address", flat=True).order_by("pk")
    offset = 0
    batch_number = 0

    while True:
        batch_number += 1
        addresses = list(queryset[offset : offset + batch_size])
        if not addresses:
            break

        offset += len(addresses)
        processed += len(addresses)
        batch_start = time.time()

        # Convert checksummed address strings to bytes for bytea comparison
        address_bytes = [bytes.fromhex(addr[2:]) for addr in addresses]

        try:
            with connection.cursor() as cursor:
                cursor.execute(BALANCE_BATCH_SQL, [address_bytes, address_bytes])
                row = cursor.fetchone()

            batch_balance = int(row[0]) if row[0] else 0
            batch_count = int(row[1]) if row[1] else 0
            total_balance_wei += batch_balance
            total_safes_with_balance += batch_count

            logger.info(
                f"Batch {batch_number}: {processed}/{total_safes} "
                f"({time.time() - batch_start:.2f}s). "
                f"Running total: {total_balance_wei} wei, "
                f"{total_safes_with_balance} with balance"
            )
        except Exception as e:
            logger.error(
                f"Batch {batch_number} failed after "
                f"{time.time() - batch_start:.2f}s, "
                f"addresses[0]={addresses[0] if addresses else 'N/A'}, "
                f"addresses[-1]={addresses[-1] if addresses else 'N/A'}: {e}"
            )
            # Continue with next batch — partial results still useful

    elapsed = time.time() - start_time
    logger.info(
        f"DB balance calculation completed in {elapsed:.2f}s. "
        f"Total balance: {total_balance_wei} wei, "
        f"Safes with balance: {total_safes_with_balance}/{processed}"
    )
    return total_balance_wei, total_safes_with_balance


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
    Calculate Safe statistics using DB aggregation on InternalTx for balance checking.

    Uses batched SQL queries instead of individual RPC calls to avoid timeouts
    on networks with 100K+ Safes.
    """
    try:
        task_start_time = time.time()
        logger.info("Starting Safe statistics task...")

        # Total number of created Safes (all proxy factories included)
        safes_count_start = time.time()
        total_safes = SafeContract.objects.count()
        safes_count_time = time.time() - safes_count_start
        logger.info(f"Counted {total_safes} total Safes in {safes_count_time:.2f}s")

        # Get all owners from SafeLastStatus to get current state
        # This gives us the most up-to-date owner information for each Safe
        owners_query_start = time.time()
        owners_data = (
            SafeLastStatus.objects.exclude(owners__isnull=True)
            .exclude(owners=[])
            .values_list("owners", flat=True)
        )
        owners_query_time = time.time() - owners_query_start
        logger.info(
            f"Fetched owner data from SafeLastStatus in {owners_query_time:.2f}s"
        )

        # Count total owners and unique owners
        owners_processing_start = time.time()
        all_owners = set()
        total_owners_count = 0
        for owner_list in owners_data.iterator():  # Use iterator for memory efficiency
            if owner_list:  # Ensure the list is not empty
                total_owners_count += len(owner_list)
                all_owners.update(owner_list)

        unique_owners = len(all_owners)
        owners_processing_time = time.time() - owners_processing_start

        logger.info(
            f"Processed owner statistics in {owners_processing_time:.2f}s: "
            f"total_owners={total_owners_count}, unique_owners={unique_owners}"
        )

        # Calculate native token balances for all Safes using the optimized function
        logger.info(f"Starting balance calculation for {total_safes} Safes")

        total_balance_wei = 0
        total_safes_with_balance = 0

        if total_safes > 0:
            balance_calculation_start = time.time()
            try:
                total_balance_wei, total_safes_with_balance = (
                    _calculate_native_balances_from_db()
                )
                balance_calculation_time = time.time() - balance_calculation_start
                logger.info(
                    f"Balance calculation completed in {balance_calculation_time:.2f}s. "
                    f"Total balance: {total_balance_wei} wei, "
                    f"Safes with balance: {total_safes_with_balance}/{total_safes}"
                )
            except Exception as e:
                balance_calculation_time = time.time() - balance_calculation_start
                logger.error(
                    f"Failed to calculate balances after {balance_calculation_time:.2f}s: {e}"
                )
                # Continue without balance data rather than failing the entire task

        # Create and store statistics
        statistics_creation_start = time.time()
        statistics = {
            "total_safes": total_safes,
            "total_owners": total_owners_count,
            "unique_owners": unique_owners,
            "balance_wei": total_balance_wei,
            "safes_with_balance": total_safes_with_balance,
            "timestamp": timezone.now().isoformat(),
        }
        statistics_creation_time = time.time() - statistics_creation_start

        # Store in Redis
        redis_storage_start = time.time()
        redis_key = AnalyticsService.REDIS_SAFE_STATISTICS
        redis = get_redis()
        redis.set(redis_key, json.dumps(statistics))
        redis_storage_time = time.time() - redis_storage_start

        total_task_time = time.time() - task_start_time

        logger.info(
            f"Safe statistics task completed successfully in {total_task_time:.2f}s. "
            f"Breakdown: statistics creation: {statistics_creation_time:.3f}s, "
            f"redis storage: {redis_storage_time:.3f}s. "
            f"Data saved to Redis key '{redis_key}'."
        )

        return True
    except Exception as e:
        if "task_start_time" in locals():
            total_task_time = time.time() - task_start_time
            logger.error(
                f"Safe statistics task failed after {total_task_time:.2f}s: {e}"
            )
        else:
            logger.error(f"Safe statistics task failed: {e}")
        # In case of any error, return False but don't raise
        # This prevents the task from failing completely
        return False
