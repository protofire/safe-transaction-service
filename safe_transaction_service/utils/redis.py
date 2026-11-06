import copyreg
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache

from django.conf import settings
from django.db import transaction

from redis import ConnectionPool, Redis

logger = logging.getLogger(__name__)


@cache
def get_redis() -> Redis:
    logger.info("Opening connection to Redis")

    # Encode memoryview for redis when using pickle
    copyreg.pickle(memoryview, lambda val: (memoryview, (bytes(val),)))

    connection_pool = ConnectionPool(
        max_connections=settings.REDIS_POOL_MAX_CONNECTIONS
    ).from_url(
        settings.REDIS_URL,
        socket_connect_timeout=settings.REDIS_CONNECTION_TIMEOUT_SECONDS,
        socket_timeout=settings.REDIS_TIMEOUT_SECONDS,
        health_check_interval=30,
    )
    return Redis(connection_pool=connection_pool)


# Max number of keys sent in a single `UNLINK` command
UNLINK_CHUNK_SIZE = 1_000

# Keys collected by `defer_unlink`. `None` when not inside a `defer_unlink` block
_deferred_unlink_keys: ContextVar[set[str] | None] = ContextVar(
    "deferred_unlink_keys", default=None
)


def _unlink(keys: Iterable[str]) -> None:
    keys = list(keys)
    redis = get_redis()
    for i in range(0, len(keys), UNLINK_CHUNK_SIZE):
        redis.unlink(*keys[i : i + UNLINK_CHUNK_SIZE])


def unlink_keys(keys: Iterable[str]) -> None:
    """
    Remove the provided keys from Redis using as few round trips as possible.
    If called inside a `defer_unlink` block, keys will be removed when the block's
    database transaction is committed.

    :param keys:
    """
    keys = set(keys)
    if not keys:
        return None

    if (deferred_keys := _deferred_unlink_keys.get()) is not None:
        deferred_keys.update(keys)
    else:
        _unlink(keys)


@contextmanager
def defer_unlink() -> Iterator[None]:
    """
    Collect the keys passed to `unlink_keys` inside the block and remove them once,
    with no duplicates, after the current database transaction is committed (or right
    away if there's no transaction). If the transaction is rolled back nothing is removed.
    Nested blocks are handled by the outermost one.
    """
    if _deferred_unlink_keys.get() is not None:
        yield
        return

    keys: set[str] = set()
    token = _deferred_unlink_keys.set(keys)
    try:
        yield
    finally:
        _deferred_unlink_keys.reset(token)
        if keys:

            def unlink_deferred_keys():
                _unlink(keys)

            # `robust`: data is already committed, a Redis error must not break the caller
            transaction.on_commit(unlink_deferred_keys, robust=True)
