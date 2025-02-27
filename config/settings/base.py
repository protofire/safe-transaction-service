"""
Base settings to build other settings files upon.
"""

from pathlib import Path

import environ
from corsheaders.defaults import default_headers as default_cors_headers

from safe_transaction_service import __version__

from ..gunicorn import (
    gunicorn_request_timeout,
    gunicorn_worker_connections,
    gunicorn_workers,
)

ROOT_DIR = Path(__file__).resolve(strict=True).parent.parent.parent
APPS_DIR = ROOT_DIR / "safe_transaction_service"

env = environ.Env()

READ_DOT_ENV_FILE = env.bool("DJANGO_READ_DOT_ENV_FILE", default=False)
DOT_ENV_FILE = env("DJANGO_DOT_ENV_FILE", default=None)
if READ_DOT_ENV_FILE or DOT_ENV_FILE:
    DOT_ENV_FILE = DOT_ENV_FILE or ".env"
    # OS environment variables take precedence over variables from .env
    env.read_env(str(ROOT_DIR / DOT_ENV_FILE))

# GENERAL
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#debug
DEBUG = env.bool("DEBUG", False)
# Local time zone. Choices are
# http://en.wikipedia.org/wiki/List_of_tz_zones_by_name
# though not all of them may be available with every OS.
# In Windows, this must be set to your system time zone.
TIME_ZONE = "UTC"
# https://docs.djangoproject.com/en/dev/ref/settings/#language-code
LANGUAGE_CODE = "en-us"
# https://docs.djangoproject.com/en/dev/ref/settings/#site-id
SITE_ID = 1
# https://docs.djangoproject.com/en/dev/ref/settings/#use-i18n
USE_I18N = True
# https://docs.djangoproject.com/en/dev/ref/settings/#use-tz
USE_TZ = True
# https://docs.djangoproject.com/en/3.2/ref/settings/#force-script-name
FORCE_SCRIPT_NAME = env("FORCE_SCRIPT_NAME", default=None)

# SSO
SSO_ENABLED = False

# Enable analytics endpoints
ENABLE_ANALYTICS = env("ENABLE_ANALYTICS", default=False)

# GUNICORN
GUNICORN_REQUEST_TIMEOUT = gunicorn_request_timeout
GUNICORN_WORKER_CONNECTIONS = gunicorn_worker_connections
GUNICORN_WORKERS = gunicorn_workers

# DATABASES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
DB_STATEMENT_TIMEOUT = env.int("DB_STATEMENT_TIMEOUT", 60_000)
DATABASES = {
    "default": env.db("DATABASE_URL"),
}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
DATABASES["default"]["ENGINE"] = "django_db_geventpool.backends.postgresql_psycopg2"
DATABASES["default"]["CONN_MAX_AGE"] = 0
DB_MAX_CONNS = env.int("DB_MAX_CONNS", default=50)
DATABASES["default"]["OPTIONS"] = {
    # https://github.com/jneight/django-db-geventpool#settings
    "MAX_CONNS": DB_MAX_CONNS,
    "REUSE_CONNS": env.int("DB_REUSE_CONNS", default=DB_MAX_CONNS),
    "options": f"-c statement_timeout={DB_STATEMENT_TIMEOUT}",
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# URLS
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#root-urlconf
ROOT_URLCONF = "config.urls"
# https://docs.djangoproject.com/en/dev/ref/settings/#wsgi-application
WSGI_APPLICATION = "config.wsgi.application"

# APPS
# ------------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.sites",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 'django.contrib.humanize', # Handy template tags
]
THIRD_PARTY_APPS = [
    "django_extensions",
    "corsheaders",
    "rest_framework",
    "django_s3_storage",
    "rest_framework.authtoken",
    "drf_spectacular",
]
LOCAL_APPS = [
    "safe_transaction_service.account_abstraction.apps.AccountAbstractionConfig",
    "safe_transaction_service.analytics.apps.AnalyticsConfig",
    "safe_transaction_service.contracts.apps.ContractsConfig",
    "safe_transaction_service.events.apps.EventsConfig",
    "safe_transaction_service.history.apps.HistoryConfig",
    "safe_transaction_service.notifications.apps.NotificationsConfig",
    "safe_transaction_service.safe_messages.apps.SafeMessagesConfig",
    "safe_transaction_service.tokens.apps.TokensConfig",
]
# https://docs.djangoproject.com/en/dev/ref/settings/#installed-apps
INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

# MIDDLEWARE
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#middleware
MIDDLEWARE = [
    "safe_transaction_service.utils.loggers.LoggingMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.http.ConditionalGetMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

# STATIC
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#static-root
STATIC_ROOT = str(ROOT_DIR / "staticfiles")

# https://docs.djangoproject.com/en/dev/ref/settings/#static-url
STATIC_URL = "static/"
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#std:setting-STATICFILES_DIRS
STATICFILES_DIRS = [
    str(APPS_DIR / "static"),
]
# https://docs.djangoproject.com/en/dev/ref/contrib/staticfiles/#staticfiles-finders
STATICFILES_FINDERS = [
    "django.contrib.staticfiles.finders.FileSystemFinder",
    "django.contrib.staticfiles.finders.AppDirectoriesFinder",
]

# MEDIA
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#media-root
MEDIA_ROOT = str(APPS_DIR / "media")
# https://docs.djangoproject.com/en/dev/ref/settings/#media-url
MEDIA_URL = "/media/"

# TEMPLATES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#templates
TEMPLATES = [
    {
        # https://docs.djangoproject.com/en/dev/ref/settings/#std:setting-TEMPLATES-BACKEND
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        # https://docs.djangoproject.com/en/dev/ref/settings/#template-dirs
        "DIRS": [
            str(APPS_DIR / "templates"),
        ],
        "OPTIONS": {
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-debug
            "debug": DEBUG,
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-loaders
            # https://docs.djangoproject.com/en/dev/ref/templates/api/#loader-types
            "loaders": [
                "django.template.loaders.filesystem.Loader",
                "django.template.loaders.app_directories.Loader",
            ],
            # https://docs.djangoproject.com/en/dev/ref/settings/#template-context-processors
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.i18n",
                "django.template.context_processors.media",
                "django.template.context_processors.static",
                "django.template.context_processors.tz",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]


# CORS
# ------------------------------------------------------------------------------
CORS_ALLOW_ALL_ORIGINS = True
CORS_ALLOW_HEADERS = list(default_cors_headers) + [
    "if-match",
    "if-modified-since",
    "if-none-match",
]
CORS_EXPOSE_HEADERS = ["etag"]

# FIXTURES
# ------------------------------------------------------------------------------
# https://docs.djangoproject.com/en/dev/ref/settings/#fixture-dirs
FIXTURE_DIRS = (str(APPS_DIR / "fixtures"),)

# ADMIN
# ------------------------------------------------------------------------------
# Django Admin URL regex.
ADMIN_URL = "admin/"

# Celery
# ------------------------------------------------------------------------------
INSTALLED_APPS += [
    "django_celery_beat",
]

# http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-broker_url
CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="django://")
# https://docs.celeryproject.org/en/stable/userguide/optimizing.html#broker-connection-pools
# https://docs.celeryq.dev/en/latest/userguide/optimizing.html#broker-connection-pools
# Configured to 0 due to connection issues https://github.com/celery/celery/issues/4355
CELERY_BROKER_POOL_LIMIT = env.int("CELERY_BROKER_POOL_LIMIT", default=0)
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#broker-heartbeat
CELERY_BROKER_HEARTBEAT = env.int("CELERY_BROKER_HEARTBEAT", default=120)

# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-broker_connection_max_retries
CELERY_BROKER_CONNECTION_MAX_RETRIES = (
    value
    if (value := env.int("CELERY_BROKER_CONNECTION_MAX_RETRIES", default=-1)) > 0
    else None
)
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#broker-channel-error-retry
CELERY_BROKER_CHANNEL_ERROR_RETRY = env.bool(
    "CELERY_BROKER_CHANNEL_ERROR_RETRY", default=True
)
# https://docs.celeryq.dev/en/stable/userguide/configuration.html#broker-connection-retry-on-startup
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = env.bool(
    "CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP", default=True
)
# https://docs.celeryq.dev/en/latest/userguide/configuration.html#task-result-backend-settings
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default=None)
# http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-accept_content
CELERY_ACCEPT_CONTENT = ["json"]
# http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-task_serializer
CELERY_TASK_SERIALIZER = "json"
# http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-result_serializer
CELERY_RESULT_SERIALIZER = "json"
# We are not interested in keeping results of tasks
CELERY_IGNORE_RESULT = True
# http://docs.celeryproject.org/en/latest/userguide/configuration.html#std:setting-task_always_eager
CELERY_ALWAYS_EAGER = False
# https://docs.celeryproject.org/en/latest/userguide/configuration.html#task-default-priority
# Higher = more priority on RabbitMQ, opposite on Redis ¯\_(ツ)_/¯
CELERY_TASK_DEFAULT_PRIORITY = 5
# https://docs.celeryproject.org/en/stable/userguide/configuration.html#task-queue-max-priority
CELERY_TASK_QUEUE_MAX_PRIORITY = 10
# https://docs.celeryproject.org/en/latest/userguide/configuration.html#broker-transport-options
CELERY_BROKER_TRANSPORT_OPTIONS = {}

# https://docs.celeryq.dev/en/stable/userguide/configuration.html#std-setting-task_routes
CELERY_ROUTES = (
    [
        (
            "safe_transaction_service.history.tasks.retry_get_metadata_task",
            {"queue": "tokens", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.events.tasks.send_event_to_queue_task",
            {"queue": "webhooks", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.history.tasks.reindex_mastercopies_last_hours_task",
            {"queue": "indexing"},
        ),
        (
            "safe_transaction_service.history.tasks.reindex_erc20_erc721_last_hours_task",
            {"queue": "indexing"},
        ),
        (
            "safe_transaction_service.history.tasks.process_decoded_internal_txs_for_safe_task",
            {"queue": "processing", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.history.tasks.process_decoded_internal_txs_task",
            {"queue": "processing", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.history.tasks.*",
            {"queue": "indexing", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.contracts.tasks.*",
            {"queue": "contracts", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.notifications.tasks.*",
            {"queue": "notifications", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.tokens.tasks.*",
            {"queue": "tokens", "delivery_mode": "transient"},
        ),
        (
            "safe_transaction_service.analytics.tasks.*",
            {"queue": "contracts", "delivery_mode": "transient"},
        ),
    ],
)


# Django REST Framework
# ------------------------------------------------------------------------------
REST_FRAMEWORK = {
    "PAGE_SIZE": 10,
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.LimitOffsetPagination",
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.AllowAny",),
    "DEFAULT_RENDERER_CLASSES": (
        "djangorestframework_camel_case.render.CamelCaseJSONRenderer",
    ),
    "DEFAULT_PARSER_CLASSES": (
        "djangorestframework_camel_case.parser.CamelCaseJSONParser",
    ),
    "DEFAULT_AUTHENTICATION_CLASSES": (
        # 'rest_framework.authentication.BasicAuthentication',
        "rest_framework.authentication.SessionAuthentication",
        "rest_framework.authentication.TokenAuthentication",
    ),
    "DEFAULT_VERSIONING_CLASS": "rest_framework.versioning.NamespaceVersioning",
    "ALLOWED_VERSIONS": ["v1", "v2"],
    "EXCEPTION_HANDLER": "safe_transaction_service.history.exceptions.custom_exception_handler",
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}

# INDEXER LOG LEVEL
ERC20_721_INDEXER_LOG_LEVEL = (
    env("ERC20_INDEXER_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
PROXY_FACTORY_INDEXER_LOG_LEVEL = (
    env("PROXY_FACTORY_INDEXER_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
SAFE_EVENTS_INDEXER_LOG_LEVEL = (
    env("SAFE_EVENTS_INDEXER_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
INTERNAL_TX_INDEXER_LOG_LEVEL = (
    env("INTERNAL_TX_INDEXER_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
# API LOG LEVEL
API_LOG_LEVEL = env("API_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
BALANCES_API_LOG_LEVEL = (
    env("BALANCES_API_LOG_LEVEL", default="WARNING") if not DEBUG else "DEBUG"
)
MESSAGES_API_LOG_LEVEL = (
    env("MESSAGES_API_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
ALL_TRANSACTIONS_API_LOG_LEVEL = (
    env("ALL_TRANSACTIONS_API_LOG_LEVEL", default="INFO") if not DEBUG else "DEBUG"
)
COLLECTIBLES_API_LOG_LEVEL = (
    env("COLLECTIBLES_API_LOG_LEVEL", default="WARNING") if not DEBUG else "DEBUG"
)


# LOGGING
# ------------------------------------------------------------------------------
# See: https://docs.djangoproject.com/en/dev/ref/settings/#logging
# A sample logging configuration. The only tangible logging
# performed by this configuration is to send an email to
# the site admins bon every HTTP 500 error when DEBUG=False.
# See https://docs.djangoproject.com/en/dev/topics/logging for
# more details on how to customize your logging configuration.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "require_debug_false": {"()": "django.utils.log.RequireDebugFalse"},
        "ignore_succeeded_none": {
            "()": "safe_transaction_service.utils.loggers.IgnoreSucceededNone"
        },
    },
    "formatters": {
        "short": {"format": "%(asctime)s %(message)s"},
        "verbose": {
            "format": "%(asctime)s [%(levelname)s] [%(processName)s] %(message)s"
        },
        "celery_verbose": {
            "class": "safe_transaction_service.utils.celery.PatchedCeleryFormatter",
            "format": "%(asctime)s [%(levelname)s] [%(task_id)s/%(task_name)s] %(message)s",
            # 'format': '%(asctime)s [%(levelname)s] [%(processName)s] [%(task_id)s/%(task_name)s] %(message)s'
        },
    },
    "handlers": {
        "mail_admins": {
            "level": "ERROR",
            "filters": ["require_debug_false"],
            "class": "django.utils.log.AdminEmailHandler",
        },
        "console": {
            "level": "DEBUG",
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
        "console_short": {
            "class": "logging.StreamHandler",
            "formatter": "short",
        },
        "celery_console": {
            "level": "DEBUG",
            "filters": [] if DEBUG else ["ignore_succeeded_none"],
            "class": "logging.StreamHandler",
            "formatter": "celery_verbose",
        },
    },
    "loggers": {
        "": {
            "handlers": ["console"],
            "level": "INFO",
        },
        "web3.providers": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
        "django.geventpool": {
            "level": "DEBUG" if DEBUG else "WARNING",
        },
        "LoggingMiddleware": {
            "handlers": ["console_short"],
            "level": "INFO",
            "propagate": False,
        },
        "safe_transaction_service": {
            "level": "DEBUG" if DEBUG else "INFO",
            "handlers": ["console"],
            "propagate": False,
        },
        # BALANCES LOG
        "safe_transaction_service.history.views.SafeBalanceView": {
            "level": BALANCES_API_LOG_LEVEL,
        },
        "safe_transaction_service.history.services.balance_service": {
            "level": BALANCES_API_LOG_LEVEL,
        },
        "safe_transaction_service.history.views_v2.SafeBalanceView": {
            "level": BALANCES_API_LOG_LEVEL,
        },
        # COLLECTIBLES LOG
        "safe_transaction_service.history.views_v2.SafeCollectiblesView": {
            "level": COLLECTIBLES_API_LOG_LEVEL,
        },
        "safe_transaction_service.history.services.collectibles_service": {
            "level": COLLECTIBLES_API_LOG_LEVEL,
        },
        # ALL-TRANSACTIONS LOG
        "safe_transaction_service.history.views.AllTransactionsListView": {
            "level": ALL_TRANSACTIONS_API_LOG_LEVEL,
        },
        "safe_transaction_service.history.services.transaction_service": {
            "level": ALL_TRANSACTIONS_API_LOG_LEVEL,
        },
        # MESSAGES_API_LOG_LEVEL: NO LOGS FOR NOW
        # ERC20_721_INDEXER_LOG_LEVEL
        "safe_transaction_service.history.indexers.erc20_events_indexer": {
            "level": ERC20_721_INDEXER_LOG_LEVEL,
        },
        "safe_transaction_service.history.tasks.index_erc20_events_task": {
            "level": ERC20_721_INDEXER_LOG_LEVEL,
        },
        # PROXY_FACTORY_INDEXER_LOG_LEVEL
        "safe_transaction_service.history.indexers.proxy_factory_indexer": {
            "level": PROXY_FACTORY_INDEXER_LOG_LEVEL,
        },
        "safe_transaction_service.history.tasks.index_new_proxies_task": {
            "level": PROXY_FACTORY_INDEXER_LOG_LEVEL,
        },
        # SAFE_EVENTS_INDEXER_LOG_LEVEL
        "safe_transaction_service.history.indexers.safe_events_indexer": {
            "level": SAFE_EVENTS_INDEXER_LOG_LEVEL,
        },
        "safe_transaction_service.history.tasks.index_safe_events_task": {
            "level": SAFE_EVENTS_INDEXER_LOG_LEVEL,
        },
        # INTERNAL_TX_INDEXER_LOG_LEVEL
        "safe_transaction_service.history.indexers.internal_tx_indexer": {
            "level": INTERNAL_TX_INDEXER_LOG_LEVEL,
        },
        "safe_transaction_service.history.tasks.index_internal_txs_task": {
            "level": INTERNAL_TX_INDEXER_LOG_LEVEL,
        },
        "celery": {
            "handlers": ["console"],
            "level": "DEBUG" if DEBUG else "INFO",
            "propagate": False,  # If not it will be out for the root logger too
        },
        "celery.worker.strategy": {  # All the "Received task..."
            "handlers": ["console"],
            "level": "INFO" if DEBUG else "WARNING",
            "propagate": False,  # If not it will be out for the root logger too
        },
        "django.request": {
            "handlers": ["mail_admins"],
            "level": "ERROR",
            "propagate": True,
        },
        "django.security.DisallowedHost": {
            "level": "ERROR",
            "handlers": ["console", "mail_admins"],
            "propagate": True,
        },
        "pika": {
            "propagate": True if DEBUG else False,
        },
    },
}

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

# Ethereum RPC
# ------------------------------------------------------------------------------
ETHEREUM_NODE_URL = env("ETHEREUM_NODE_URL", default=None)
HIDE_ETHEREUM_RPC = env.bool(
    "HIDE_ETHEREUM_RPC", default=False
)  # Don't expose the Ethereum RPC URL

# Ethereum 4337 Bundler RPC
# ------------------------------------------------------------------------------
ETHEREUM_4337_BUNDLER_URL = env("ETHEREUM_4337_BUNDLER_URL", default=None)
ETHEREUM_4337_SUPPORTED_ENTRY_POINTS = env.list(
    "ETHEREUM_4337_SUPPORTED_ENTRY_POINTS",
    default=["0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789"],
)
ETHEREUM_4337_SUPPORTED_SAFE_MODULES = env.list(
    "ETHEREUM_4337_SUPPORTED_SAFE_MODULES",
    default=["0xa581c4A4DB7175302464fF3C06380BC3270b4037"],
)

# Tracing indexing configuration (not useful for L2 indexing)
# ------------------------------------------------------------------------------
ETHEREUM_TRACING_NODE_URL = env("ETHEREUM_TRACING_NODE_URL", default=None)
ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT = env.int(
    "ETH_INTERNAL_TXS_BLOCK_PROCESS_LIMIT", default=10_000
)
ETH_INTERNAL_TXS_BLOCKS_TO_REINDEX_AGAIN = env.int(
    "ETH_INTERNAL_TXS_BLOCKS_TO_REINDEX_AGAIN", default=1
)
ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS = env.int(
    "ETH_INTERNAL_TXS_NUMBER_TRACE_BLOCKS", default=10
)  # Use `trace_block` for last `number_trace_blocks` blocks indexing
ETH_INTERNAL_NO_FILTER = env.bool(
    "ETH_INTERNAL_NO_FILTER", default=False
)  # Don't use `trace_filter`, only `trace_block` and `trace_transaction`
ETH_INTERNAL_TRACE_TXS_BATCH_SIZE = env.int(
    "ETH_INTERNAL_TRACE_TXS_BATCH_SIZE", default=0
)  # Number of `trace_transaction` calls allowed in the same RPC batch call, as results can be quite big
ETH_INTERNAL_TX_DECODED_PROCESS_BATCH = env.int(
    "ETH_INTERNAL_TX_DECODED_PROCESS_BATCH", default=500
)  # Number of InternalTxDecoded to process together. Keep it low to be memory friendly

# Event indexing configuration (L2 and ERC20/721)
# ------------------------------------------------------------------------------
ETH_L2_NETWORK = env.bool(
    "ETH_L2_NETWORK", default=not ETHEREUM_TRACING_NODE_URL
)  # Use L2 event indexing
ETH_EVENTS_BLOCK_PROCESS_LIMIT = env.int(
    "ETH_EVENTS_BLOCK_PROCESS_LIMIT", default=50
)  # Initial number of blocks to process together when searching for events. It will be auto increased. 0 == no limit.
ETH_EVENTS_BLOCK_PROCESS_LIMIT_MAX = env.int(
    "ETH_EVENTS_BLOCK_PROCESS_LIMIT_MAX", default=0
)  # Maximum number of blocks to process together when searching for events. 0 == no limit.
ETH_EVENTS_BLOCKS_TO_REINDEX_AGAIN = env.int(
    "ETH_EVENTS_BLOCKS_TO_REINDEX_AGAIN", default=2
)  # Blocks to reindex again every indexer run when service is synced. Useful for RPCs not reliable
ETH_EVENTS_GET_LOGS_CONCURRENCY = env.int(
    "ETH_EVENTS_GET_LOGS_CONCURRENCY", default=20
)  # Number of concurrent requests to `getLogs`
ETH_EVENTS_QUERY_CHUNK_SIZE = env.int(
    "ETH_EVENTS_QUERY_CHUNK_SIZE", default=1_000
)  # Number of addresses to use as `getLogs` parameter. `0 == no limit`. By testing `1_000` looks like a good default
ETH_EVENTS_UPDATED_BLOCK_BEHIND = env.int(
    "ETH_EVENTS_UPDATED_BLOCK_BEHIND", default=24 * 60 * 60 // 15
)  # Number of blocks to consider an address 'almost updated'.
ETH_REORG_BLOCKS_BATCH = env.int(
    "ETH_REORG_BLOCKS_BATCH", default=250
)  # Number of blocks to be checked in the same batch for reorgs
ETH_REORG_BLOCKS = env.int(
    "ETH_REORG_BLOCKS", default=200 if ETH_L2_NETWORK else 10
)  # Number of blocks from the current block number needed to consider a block valid/stable
ETH_ERC20_LOAD_ADDRESSES_CHUNK_SIZE = env.int(
    "ETH_ERC20_LOAD_ADDRESSES_CHUNK_SIZE", default=500_000
)  # Load Safe addresses for the ERC20 indexer with a database iterator with the defined `chunk_size`

# Events processing
# ------------------------------------------------------------------------------
PROCESSING_ENABLE_OUT_OF_ORDER_CHECK = env.bool(
    "PROCESSING_ENABLE_OUT_OF_ORDER_CHECK", default=True
)  # Enable out of order check, in case some transactions appear after a reindex so Safes don't get corrupt. Disabling it can speed up processing

# Tokens
# ------------------------------------------------------------------------------
TOKENS_LOGO_BASE_URI = env.str(
    "TOKENS_LOGO_BASE_URI", default="https://tokens-logo.localhost/"
)  # Used if AWS_S3_PUBLIC_URL is not defined
TOKENS_LOGO_EXTENSION = env.str("TOKENS_LOGO_EXTENSION", default=".png")
TOKENS_ENS_IMAGE_URL = env.str(
    "TOKENS_ENS_IMAGE_URL",
    default="https://safe-transaction-assets.safe.global/tokens/logos/ENS.png",
)
TOKENS_ERC20_GET_BALANCES_BATCH = env.int(
    "TOKENS_ERC20_GET_BALANCES_BATCH", default=2_000
)  # Number of tokens to get balances from in the same request. From 2_500 some nodes raise HTTP 413

# ENS
# ------------------------------------------------------------------------------
# See https://docs.ens.domains/web/subgraph for more details

ENS_SUBGRAPH_URL = env.str("ENS_SUBGRAPH_URL", default=None)
ENS_SUBGRAPH_API_KEY = env.str("ENS_SUBGRAPH_API_KEY", default=None)
ENS_SUBGRAPH_ID = env.str("ENS_SUBGRAPH_ID", default=None)

# Notifications
# ------------------------------------------------------------------------------
SLACK_API_WEBHOOK = env("SLACK_API_WEBHOOK", default=None)

# Notifications
NOTIFICATIONS_FIREBASE_CREDENTIALS_PATH = env(
    "NOTIFICATIONS_FIREBASE_CREDENTIALS_PATH", default=None
)
NOTIFICATIONS_DUPLICATED_EXPIRATION_TIME_SECONDS = env.int(
    "NOTIFICATIONS_DUPLICATED_EXPIRATION_TIME_SECONDS", default=60 * 60 * 2  # 2 hours
)  # Don't allow the same notification to be sent during the expiration time due to reorgs and reindexing

if NOTIFICATIONS_FIREBASE_CREDENTIALS_PATH:
    import json

    NOTIFICATIONS_FIREBASE_AUTH_CREDENTIALS = json.load(
        environ.Path(NOTIFICATIONS_FIREBASE_CREDENTIALS_PATH).file(
            "firebase-credentials.json"
        )
    )

# Events
# ------------------------------------------------------------------------------
EVENTS_QUEUE_URL = env("EVENTS_QUEUE_URL", default=None)
EVENTS_QUEUE_EXCHANGE_NAME = env("EVENTS_QUEUE_EXCHANGE_NAME", default="amq.fanout")
EVENTS_QUEUE_POOL_CONNECTIONS_LIMIT = env.int(
    "EVENTS_QUEUE_POOL_CONNECTIONS_LIMIT", default=0
)

# Events and notifications
# ------------------------------------------------------------------------------
DISABLE_NOTIFICATIONS_AND_EVENTS = env.bool(
    "DISABLE_NOTIFICATIONS_AND_EVENTS", default=False
)  # Increases indexing speed for initial sync by disabling sending notifications and events to the queue

# Cache
CACHE_ALL_TXS_VIEW = env.int(
    "CACHE_ALL_TXS_VIEW", default=10 * 60
)  # 10 minutes. 0 is disabled

# AWS S3 https://github.com/etianen/django-s3-storage
# ------------------------------------------------------------------------------
# AWS_QUERYSTRING_AUTH = False  # Remove query parameter authentication from generated URLs
AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID", default=None)
AWS_S3_PUBLIC_URL = env(
    "AWS_S3_PUBLIC_URL", default=None
)  # Set custom domain for file urls (like cloudfront)
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY", default=None)
AWS_S3_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME", default=None)
AWS_S3_FILE_OVERWRITE = True
AWS_S3_USE_THREADS = False  # Threading not compatible with gevent
AWS_CONFIGURED = bool(
    AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY and AWS_S3_BUCKET_NAME
)

ETHERSCAN_API_KEY = env("ETHERSCAN_API_KEY", default=None)
IPFS_GATEWAY = env("IPFS_GATEWAY", default="https://ipfs.io/ipfs/")

# Shell Plus
# ------------------------------------------------------------------------------
SHELL_PLUS_PRINT_SQL_TRUNCATE = env.int("SHELL_PLUS_PRINT_SQL_TRUNCATE", default=10_000)

# Endpoints
TX_SERVICE_ALL_TXS_ENDPOINT_LIMIT_TRANSFERS = env.int(
    "TX_SERVICE_ALL_TXS_ENDPOINT_LIMIT_TRANSFERS", default=1_000
)  # Don't return more than 1_000 transfers

# Compression level – an integer from 0 to 9. 0 means not compression
CACHE_ALL_TXS_COMPRESSION_LEVEL = env.int("CACHE_ALL_TXS_COMPRESSION_LEVEL", default=0)
CACHE_VIEW_DEFAULT_TIMEOUT = env.int(
    "CACHE_VIEW_DEFAULT_TIMEOUT", default=0
)  # 0 will disable the cache

# Contracts reindex batch configuration
# ------------------------------------------------------------------------------
# The following configuration prevents overwhelming third-party data sources by controlling the rate of requests.
# Defines the batch size to limit the number of reindex contracts tasks sent to Celery concurrently.
REINDEX_CONTRACTS_METADATA_BATCH = env.int(
    "REINDEX_CONTRACTS_METADATA_BATCH", default=100
)
# Defines the delay countdown between batches of reindex contract tasks.
REINDEX_CONTRACTS_METADATA_COUNTDOWN = env.int(
    "REINDEX_CONTRACTS_METADATA_COUNTDOWN", default=0
)

# DRF ESPECTACULAR
SPECTACULAR_SETTINGS = {
    "TITLE": "Safe Transaction Service",
    "DESCRIPTION": "API to keep track of transactions sent via Safe smart contracts",
    "VERSION": __version__,
    "SWAGGER_UI_FAVICON_HREF": "static/safe/favicon.png",
    "OAS_VERSION": "3.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v[0-9]",
    "DEFAULT_GENERATOR_CLASS": "safe_transaction_service.utils.swagger.IgnoreVersionSchemaGenerator",
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.contrib.djangorestframework_camel_case.camelize_serializer_fields"
    ],
    "SORT_OPERATION_PARAMETERS": False,
}
