import json
from functools import cache
from typing import Optional

from django.db.models import Avg, Count, DecimalField, Max, Min, Q, Sum, Value
from django.db.models.functions import Coalesce, Trunc
from django.utils import timezone

from safe_transaction_service.history.models import (
    ERC20Transfer,
    ERC721Transfer,
    InternalTx,
    ModuleTransaction,
    MultisigConfirmation,
    MultisigTransaction,
    SafeContract,
    SafeStatus,
)
from safe_transaction_service.utils.redis import get_redis

from safe_transaction_service import __version__


@cache
def get_analytics_service() -> "AnalyticsService":
    return AnalyticsService()


def _parse_window(window: str) -> Optional[int]:
    """Parse window string like '7d', '30d', '90d' into days. Returns None if invalid."""
    window = window.strip().lower()
    if window.endswith("d"):
        try:
            return int(window[:-1])
        except ValueError:
            return None
    return None


class AnalyticsService:
    REDIS_TRANSACTIONS_PER_SAFE_APP = "analytics_transactions_per_safe_app"
    REDIS_SAFE_STATISTICS = "analytics_safe_statistics"
    REDIS_ACTIVE_SAFES_PREFIX = "analytics_active_safes_"
    REDIS_ACTIVE_OWNERS_PREFIX = "analytics_active_owners_"
    REDIS_SAFE_SEGMENTS = "analytics_safe_segments"
    REDIS_TVL = "analytics_tvl"

    def get_safe_transactions_per_safe_app(self) -> list[dict]:
        redis = get_redis()
        analytic_result = redis.get(self.REDIS_TRANSACTIONS_PER_SAFE_APP)
        if analytic_result:
            return json.loads(analytic_result)
        else:
            return []

    def get_safe_statistics(self) -> dict:
        redis = get_redis()
        analytic_result = redis.get(self.REDIS_SAFE_STATISTICS)
        if analytic_result:
            return json.loads(analytic_result)
        else:
            return {
                "total_safes": 0,
                "total_owners": 0,
                "unique_owners": 0,
                "balance_wei": 0,
                "safes_with_balance": 0,
                "timestamp": None,
            }

    # ── A.1 Summary ──────────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Direct query — fleet-level summary metrics."""
        from safe_transaction_service.utils.ethereum import get_chain_id

        dates = SafeContract.objects.aggregate(
            first=Min("ethereum_tx__block__timestamp"),
            last=Max("ethereum_tx__block__timestamp"),
        )
        return {
            "total_safes": SafeContract.objects.count(),
            "total_multisig_txs": MultisigTransaction.objects.count(),
            "total_module_txs": ModuleTransaction.objects.count(),
            "total_erc20_transfers": ERC20Transfer.objects.count(),
            "total_erc721_transfers": ERC721Transfer.objects.count(),
            "first_safe_created": dates["first"],
            "last_safe_created": dates["last"],
            "chain_id": get_chain_id(),
            "service_version": __version__,
        }

    # ── A.2 Active Safes (Redis-cached) ──────────────────────────────

    def get_active_safes(self, window: str) -> dict:
        redis = get_redis()
        result = redis.get(self.REDIS_ACTIVE_SAFES_PREFIX + window)
        if result:
            return json.loads(result)
        return {"window": window, "active_safes": 0, "computed_at": None}

    # ── A.3 Safe Creations Time Series (direct query) ────────────────

    def get_safe_creations(self, date_from, date_to, interval: str) -> list[dict]:
        qs = SafeContract.objects.all()
        if date_from:
            qs = qs.filter(ethereum_tx__block__timestamp__gte=date_from)
        if date_to:
            qs = qs.filter(ethereum_tx__block__timestamp__lte=date_to)

        rows = (
            qs.annotate(
                period=Trunc("ethereum_tx__block__timestamp", interval)
            )
            .values("period")
            .annotate(count=Count("address"))
            .order_by("period")
        )
        return [
            {"period": row["period"].date().isoformat(), "count": row["count"]}
            for row in rows
            if row["period"] is not None
        ]

    # ── A.4 Active Owners (Redis-cached) ─────────────────────────────

    def get_active_owners(self, window: str) -> dict:
        redis = get_redis()
        result = redis.get(self.REDIS_ACTIVE_OWNERS_PREFIX + window)
        if result:
            return json.loads(result)
        return {"window": window, "active_owners": 0, "computed_at": None}

    # ── A.5 TX Volume (direct query) ─────────────────────────────────

    def get_tx_volume(self, window: str) -> dict:
        days = _parse_window(window)
        if days is None:
            days = 30
        cutoff = timezone.now() - timezone.timedelta(days=days)

        multisig_qs = MultisigTransaction.objects.filter(created__gte=cutoff)
        total_multisig = multisig_qs.count()
        executed_multisig = multisig_qs.exclude(ethereum_tx=None).count()

        module_txs = ModuleTransaction.objects.filter(created__gte=cutoff).count()

        value_agg = multisig_qs.aggregate(
            total=Coalesce(Sum("value"), Value(0), output_field=DecimalField())
        )
        total_value_wei = str(int(value_agg["total"]))

        avg_conf = (
            MultisigConfirmation.objects.filter(created__gte=cutoff)
            .values("multisig_transaction")
            .annotate(conf_count=Count("id"))
            .aggregate(avg=Avg("conf_count"))
        )

        return {
            "window": window,
            "total_multisig_txs": total_multisig,
            "executed_multisig_txs": executed_multisig,
            "module_txs": module_txs,
            "total_value_wei": total_value_wei,
            "avg_confirmations": round(avg_conf["avg"] or 0, 1),
            "computed_at": timezone.now(),
        }

    # ── A.6 Safe Segments (Redis-cached) ─────────────────────────────

    def get_safe_segments(self) -> dict:
        redis = get_redis()
        result = redis.get(self.REDIS_SAFE_SEGMENTS)
        if result:
            return json.loads(result)
        return {
            "personal": 0,
            "team": 0,
            "enterprise": 0,
            "with_modules": 0,
            "avg_threshold": 0.0,
            "avg_owners": 0.0,
            "computed_at": None,
        }

    # ── A.7 TVL (Redis-cached) ───────────────────────────────────────

    def get_tvl(self) -> dict:
        redis = get_redis()
        result = redis.get(self.REDIS_TVL)
        if result:
            return json.loads(result)
        return {
            "total_safes_with_balance": 0,
            "native_balance_wei": "0",
            "erc20_token_count": 0,
            "top_tokens": [],
            "computed_at": None,
        }

    # ── A.8 Token Volume (direct query) ──────────────────────────────

    def get_token_volume(self, window: str) -> dict:
        days = _parse_window(window)
        if days is None:
            days = 30
        cutoff = timezone.now() - timezone.timedelta(days=days)

        qs = ERC20Transfer.objects.filter(timestamp__gte=cutoff)
        total_transfers = qs.count()
        unique_tokens = qs.values("address").distinct().count()

        top_tokens = list(
            qs.values("address")
            .annotate(
                transfer_count=Count("*"),
                total_value=Sum("value"),
            )
            .order_by("-transfer_count")[:20]
        )

        return {
            "window": window,
            "total_erc20_transfers": total_transfers,
            "unique_tokens": unique_tokens,
            "top_tokens": [
                {
                    "address": t["address"],
                    "transfer_count": t["transfer_count"],
                    "total_value": str(t["total_value"] or 0),
                }
                for t in top_tokens
            ],
            "computed_at": timezone.now(),
        }
