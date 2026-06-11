# Release Changes: v5.42.1 → v6.4.0

**Rollout branch:** `development-6.4.0`
**Upstream commits merged:** 90
**Custom Protofire commits preserved:** 9

---

## New Environment Variables

| Variable                                 | Default                                       | Description                                                                                                                                                                                        | Required? |
| ---------------------------------------- | --------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| `DB_LOCK_TIMEOUT`                        | `5000` (ms)                                   | Kills a statement waiting on a lock. Prevents a slow transaction holding a row lock from cascading into an outage. Must be lower than `DB_STATEMENT_TIMEOUT`.                                      | Optional  |
| `DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT` | `30000` (ms)                                  | Kills a connection inside a transaction that is idle (e.g. app crash mid-request, network drop). Prevents locks held indefinitely.                                                                 | Optional  |
| `ETH_ALLOW_EMPTY_TRANSACTION_DATA`       | `false`                                       | Allow transactions with null data/input fields. Required for some networks like Tempo.                                                                                                             | Optional  |
| `ETH_REINDEX_MAX_RETRIES`                | `5`                                           | Number of consecutive failures of the same block range during reindex before giving up.                                                                                                            | Optional  |
| `EVENTS_QUEUE_TOPIC_EXCHANGE_NAME`       | `safe-transaction-service-events-with-topics` | New RabbitMQ topic exchange. Messages are routed with key `{chainId}.{type}.{address}`. The legacy fanout exchange is automatically bridged — existing consumers continue working without changes. | Optional  |

## Changed Defaults (potentially breaking)

| Variable                              | Old Default     | New Default                       | Impact                                                                                                                                                                                   |
| ------------------------------------- | --------------- | --------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `EVENTS_QUEUE_EXCHANGE_NAME`          | `amq.fanout`    | `safe-transaction-service-events` | **Medium** — if your RabbitMQ broker binds consumers to the exact exchange name `amq.fanout`, they will stop receiving events. Update the broker binding or set this env var explicitly. |
| `EVENTS_QUEUE_POOL_CONNECTIONS_LIMIT` | `0` (unlimited) | `20`                              | Low — reduces connection pool to RabbitMQ. Increase if you see pool exhaustion under high event load.                                                                                    |

## Breaking Changes

### 1. Python 3.13 Required

`pyproject.toml` sets `requires-python = ">=3.13"`. CI/CD runners and the Docker base image must be upgraded before installing dependencies.

### 2. Dependency Manager: `pip` → `uv`

`requirements.txt` has been deleted upstream. All dependencies are now in `pyproject.toml` + `uv.lock`.

- **Dockerfile** now uses `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv` and runs `uv sync --no-dev --frozen --no-install-project` instead of `pip install -r requirements.txt`.
- The custom `safe-eth-py` fork (`git+https://github.com/protofire/safe-eth-py.git@v7.20.0`) must be ported to a `[tool.uv.sources]` entry in `pyproject.toml` (or dropped if upstream `safe-eth-py==7.21.0` includes the needed changes).

### 3. `safe-eth-py` Fork — Action Required

The fork currently pins `git+https://github.com/protofire/safe-eth-py.git@v7.20.0` via `requirements.txt`. Upstream now specifies `safe-eth-py[django]==7.21.0` via `pyproject.toml`. You must decide:
**Decision:** Using the custom fork — it contains network configurations not present in the upstream package. Dependency is pinned to `safe-eth-py[django]==7.20.0` with a `[tool.uv.sources]` override in `pyproject.toml`:

```toml
[tool.uv.sources]
safe-eth-py = { git = "https://github.com/protofire/safe-eth-py.git", tag = "v7.20.0" }
```

**Note:** `uv.lock` must be regenerated (`uv lock`) after deploying this change.

### 4. `EVENTS_QUEUE_EXCHANGE_NAME` Default Changed

Default changed from `amq.fanout` to `safe-transaction-service-events`. If your RabbitMQ deployment binds to the old exchange name `amq.fanout`, set this explicitly:

```
EVENTS_QUEUE_EXCHANGE_NAME=amq.fanout
```

or update your broker configuration to match the new default.

### 5. v1 Owners/Modules Endpoint — 200-Result Hard Cap

`GET /api/v1/owners/{address}/safes/` and `GET /api/v1/modules/{address}/safes/` now return **at most 200 results**. Any client managing an owner/module with >200 Safes will receive truncated data without any error or pagination signal. Migrate those clients to the v2 paginated endpoint.

### 6. PostgreSQL 18 in docker-compose

The development/CI `docker-compose.yml` now references `postgres:18-alpine` (was 16). Production PostgreSQL is not forced to upgrade, but test your deployment against PostgreSQL 18 before any scheduled infra upgrade.

## Build Changes

| File                    | Change                                                                                                                                                                                                                      |
| ----------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `docker/web/Dockerfile` | Replaced `pip install -r requirements.txt` with `uv sync --no-dev --frozen --no-install-project`. Copies `uv` binary from `ghcr.io/astral-sh/uv:latest`. Copies `pyproject.toml` + `uv.lock` instead of `requirements.txt`. |
| `requirements.txt`      | **Deleted** — replaced by `pyproject.toml` + `uv.lock`                                                                                                                                                                      |
| `requirements-dev.txt`  | **Deleted** — replaced by `[dependency-groups].dev` in `pyproject.toml`                                                                                                                                                     |
| `requirements-test.txt` | **Deleted** — included in dev group                                                                                                                                                                                         |
| `pyproject.toml`        | Now contains full project metadata, all dependencies, and `uv` configuration                                                                                                                                                |
| `docker-compose.yml`    | PostgreSQL image: `postgres:16-alpine` → `postgres:18-alpine`                                                                                                                                                               |

### Key Dependency Version Changes

| Package               | v5.42.1              | v6.4.0                               |
| --------------------- | -------------------- | ------------------------------------ |
| Python                | 3.11+                | **3.13** (hard requirement)          |
| Django                | ~5.2.11              | >=5.2.13,<5.3                        |
| Celery                | ~5.5.x               | 5.6.3                                |
| `psycopg`             | ~3.2.x               | 3.3.4                                |
| `redis` client        | ~6.x                 | **7.4.0** (major bump)               |
| `web3`                | ~7.14.1              | 7.16.0                               |
| `safe-eth-py`         | 7.19.0 (fork@7.20.0) | 7.21.0                               |
| `gunicorn`            | ~23.x                | 25.3.0 (gevent)                      |
| `orjson`              | not present          | 3.11.9 (new, replaces stdlib `json`) |
| `djangorestframework` | ~3.16.x              | 3.17.1                               |

## Action Items Before Deployment

- [ ] **Upgrade Python runtime to 3.13** in CI/CD and Docker base image
- [ ] **Update Dockerfile** to use `uv sync` pattern (see Build Changes above)
- [x] **`safe-eth-py` fork** — using `protofire/safe-eth-py@v7.20.0` via `[tool.uv.sources]` (contains custom network configs). Run `uv lock` to regenerate `uv.lock` before building.
- [ ] **Update CI/CD pipeline** — replace `pip install -r requirements.txt` with `uv sync --no-dev --frozen --no-install-project`
- [ ] **Verify `EVENTS_QUEUE_EXCHANGE_NAME`** — confirm RabbitMQ bindings match new default or set explicitly
- [ ] **Run migration 0101** in a maintenance window (see `migration-risks.md`)
- [ ] **Audit v1 owners/modules clients** — confirm no consumer hits the 200-result truncation
- [ ] **Set new env vars** (especially `DB_LOCK_TIMEOUT`, `DB_IDLE_IN_TRANSACTION_SESSION_TIMEOUT`) in your secrets/config store
- [ ] **Validate "temp" block limit patch** (`b23e33c7`) — decide if still needed before carrying forward
