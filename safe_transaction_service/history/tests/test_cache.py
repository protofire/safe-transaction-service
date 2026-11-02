from django.conf import settings
from django.db import transaction
from django.test import TestCase

from eth_account import Account
from gevent.testing import mock
from redis import Redis

from safe_transaction_service.history.cache import (
    CacheSafeTxsView,
    remove_cache_view_by_instance,
)
from safe_transaction_service.history.models import InternalTx

from .factories import EthereumTxFactory, InternalTxFactory


class TestCacheSafeTxsView(TestCase):
    def test_cache_name(self):
        safe_address = "0x5af394e41D387d507DA9a07D33d47Cf9D8Da656d"
        cache_tag = "testtag"
        cache_instance = CacheSafeTxsView(cache_tag, safe_address)
        self.assertEqual(
            cache_instance.cache_name,
            "testtag:0x5af394e41D387d507DA9a07D33d47Cf9D8Da656d",
        )

    def test_set_and_get_cache_data(self):
        safe_address = "0x5af394e41D387d507DA9a07D33d47Cf9D8Da656d"
        cache_tag = "testtag"
        cache_path = "cache_path"
        some_data = "TestData"
        cache_instance = CacheSafeTxsView(cache_tag, safe_address)
        cache_instance.set_cache_data(cache_path, some_data, 120)
        self.assertEqual(
            some_data, cache_instance.get_cache_data(cache_path).decode("utf-8")
        )

    @mock.patch(
        "safe_transaction_service.history.views.settings.CACHE_VIEW_DEFAULT_TIMEOUT",
        0,
    )
    def test_disable_cache(self):
        # CACHE_VIEW_DEFAULT_TIMEOUT equals to 0 disable the cache storing
        safe_address = "0x5af394e41D387d507DA9a07D33d47Cf9D8Da656d"
        cache_tag = "testtag"
        cache_path = "cache_path"
        some_data = "TestData"
        cache_instance = CacheSafeTxsView(cache_tag, safe_address)
        self.assertFalse(cache_instance.enabled)
        cache_instance.set_cache_data(cache_path, some_data, 120)
        self.assertIsNone(cache_instance.get_cache_data(cache_path))

    def test_remove_cache(self):
        safe_address = "0x5af394e41D387d507DA9a07D33d47Cf9D8Da656d"
        cache_tag = "testtag"
        cache_path = "cache_path"
        some_data = "TestData"
        cache_instance = CacheSafeTxsView(cache_tag, safe_address)
        cache_instance.set_cache_data(cache_path, some_data, 120)
        self.assertEqual(
            some_data, cache_instance.get_cache_data(cache_path).decode("utf-8")
        )
        cache_instance.remove_cache()
        self.assertIsNone(cache_instance.get_cache_data(cache_path))


class TestCacheViewInvalidation(TestCase):
    def _set_transfers_cache(self, address: str) -> CacheSafeTxsView:
        cache_instance = CacheSafeTxsView(
            CacheSafeTxsView.LIST_TRANSFERS_VIEW_CACHE_KEY, address
        )
        cache_instance.set_cache_data("cache_path", "TestData", 120)
        return cache_instance

    def _build_internal_txs(self, to: str, count: int) -> list[InternalTx]:
        ethereum_tx = EthereumTxFactory()
        # `value=0` so they are not ether transfers and no events are sent
        return [
            InternalTxFactory.build(ethereum_tx=ethereum_tx, to=to, value=0)
            for _ in range(count)
        ]

    def test_remove_cache_view_by_instance(self):
        internal_tx = InternalTxFactory()
        to_cache = self._set_transfers_cache(internal_tx.to)
        from_cache = self._set_transfers_cache(internal_tx._from)

        with mock.patch.object(
            Redis, "unlink", autospec=True, side_effect=Redis.unlink
        ) as unlink_mock:
            remove_cache_view_by_instance(internal_tx)

        # Both addresses are removed with a single round trip
        unlink_mock.assert_called_once()
        self.assertIsNone(to_cache.get_cache_data("cache_path"))
        self.assertIsNone(from_cache.get_cache_data("cache_path"))

    @mock.patch.object(settings, "CACHE_VIEW_DEFAULT_TIMEOUT", 0)
    def test_remove_cache_view_by_instance_cache_disabled(self):
        internal_tx = InternalTxFactory()
        with mock.patch.object(Redis, "unlink") as unlink_mock:
            remove_cache_view_by_instance(internal_tx)
        unlink_mock.assert_not_called()

    @mock.patch.object(settings, "CACHE_VIEW_DEFAULT_TIMEOUT", 0)
    def test_bulk_create_cache_disabled(self):
        internal_txs = self._build_internal_txs(Account.create().address, 5)
        with mock.patch.object(Redis, "unlink") as unlink_mock:
            with self.captureOnCommitCallbacks(execute=True):
                InternalTx.objects.bulk_create(internal_txs)
        unlink_mock.assert_not_called()

    def test_bulk_create_deduplicates_and_defers_invalidation(self):
        safe_address = Account.create().address
        safe_cache = self._set_transfers_cache(safe_address)
        internal_txs = self._build_internal_txs(safe_address, 5)

        with mock.patch.object(
            Redis, "unlink", autospec=True, side_effect=Redis.unlink
        ) as unlink_mock:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                InternalTx.objects.bulk_create(internal_txs)
                # Nothing is removed until the transaction commits
                unlink_mock.assert_not_called()
                self.assertIsNotNone(safe_cache.get_cache_data("cache_path"))

        self.assertEqual(len(callbacks), 1)
        unlink_mock.assert_called_once()
        removed_keys = unlink_mock.call_args.args[1:]  # First arg is `self`
        # 1 shared `to` address + 5 different `_from` addresses
        self.assertEqual(len(removed_keys), 6)
        self.assertEqual(len(set(removed_keys)), 6)
        self.assertIsNone(safe_cache.get_cache_data("cache_path"))

    def test_bulk_create_rollback_does_not_invalidate(self):
        safe_address = Account.create().address
        safe_cache = self._set_transfers_cache(safe_address)
        internal_txs = self._build_internal_txs(safe_address, 3)

        with mock.patch.object(Redis, "unlink") as unlink_mock:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with self.assertRaises(ValueError):
                    with transaction.atomic():
                        InternalTx.objects.bulk_create(internal_txs)
                        raise ValueError("Rollback")

        self.assertEqual(callbacks, [])
        unlink_mock.assert_not_called()
        self.assertIsNotNone(safe_cache.get_cache_data("cache_path"))
