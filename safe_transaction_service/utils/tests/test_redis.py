from unittest import mock

from django.test import TestCase

from redis import Redis

from ..redis import defer_unlink, get_redis, unlink_keys


class TestRedis(TestCase):
    def test_unlink_keys(self):
        redis = get_redis()
        redis.set("test-unlink-a", 1)
        redis.set("test-unlink-b", 1)
        with mock.patch.object(
            Redis, "unlink", autospec=True, side_effect=Redis.unlink
        ) as unlink_mock:
            unlink_keys(["test-unlink-a", "test-unlink-b", "test-unlink-a"])
        unlink_mock.assert_called_once()
        self.assertFalse(redis.exists("test-unlink-a", "test-unlink-b"))

        with mock.patch.object(Redis, "unlink") as unlink_mock:
            unlink_keys([])
        unlink_mock.assert_not_called()

    @mock.patch("safe_transaction_service.utils.redis.UNLINK_CHUNK_SIZE", 2)
    def test_unlink_keys_chunks(self):
        with mock.patch.object(Redis, "unlink") as unlink_mock:
            unlink_keys(["a", "b", "c", "d", "e"])
        self.assertEqual(unlink_mock.call_count, 3)

    def test_defer_unlink(self):
        with mock.patch.object(Redis, "unlink") as unlink_mock:
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with defer_unlink():
                    unlink_keys(["a", "b"])
                    with defer_unlink():  # Nested blocks are flushed by the outermost
                        unlink_keys(["b", "c"])
                    unlink_mock.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        unlink_mock.assert_called_once()
        self.assertCountEqual(unlink_mock.call_args.args, ["a", "b", "c"])

    def test_defer_unlink_robust(self):
        # A Redis error after commit must not break the caller, data is already stored
        with mock.patch.object(Redis, "unlink", side_effect=ConnectionError):
            with self.captureOnCommitCallbacks(execute=True) as callbacks:
                with defer_unlink():
                    unlink_keys(["a"])
        self.assertEqual(len(callbacks), 1)
