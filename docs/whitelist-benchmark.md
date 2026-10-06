# Whitelist indexing benchmark

`WHITELISTED_SAFES` limits L2 event and ERC20/721 indexing to a set of Safes. This
benchmark compares stock and whitelist indexing on a local Ganache chain as the number
of Safes on chain (N) grows, with the number of whitelisted Safes (W) fixed.

**It shows relative scaling only, not production numbers.** Ganache has no provider
limits, no latency and almost no unrelated activity, so absolute savings on a real
network can't be derived from it. Those are measured per network during rollout (see
the per-network rollout verification in the implementation plan) with
`scripts/whitelist_db_stats.sql`.

## What is measured

For every N (default `20,100,500`):

1. N Safes are deployed (L2 singletons 1.3.0, 1.4.1 and 1.5.0 in turn). Each one
   executes a Safe transaction, and every second one receives an ERC20 transfer.
2. The block range is padded with empty blocks to the same span for every N, so the
   number of block windows (`eth_getLogs` rounds) doesn't depend on N.
3. The range is indexed twice from a clean indexing state, through the JSON-RPC
   logging proxy (`scripts/rpc_logging_proxy.py`, started in-process):
   - **stock**: empty whitelist,
   - **whitelist**: the first W Safes.

   Both runs use `SafeEventsIndexer` and `Erc20EventsIndexer` with a fixed block range
   (50 blocks, no auto adjust) and `ETH_EVENTS_QUERY_CHUNK_SIZE=10`, below N, so the
   stock ERC20 indexer queries every `Transfer` event (broad mode) while the whitelist
   one queries filtered chunks. Then the decoded transactions are processed.

Per run: RPC calls (total, `eth_getLogs`, transaction/receipt fetches), response
bytes, stored rows for the `history_*` tables listed in the SQL script, and wall time.

## Running it

```bash
# Services: db, redis, rabbitmq and ganache. Stop the app containers, the test suite
# flushes the shared Redis:
docker compose stop web indexer-worker scheduler nginx

WHITELIST_BENCHMARK=1 \
WHITELIST_BENCHMARK_N=20,100,500 \
WHITELIST_BENCHMARK_W=5 \
WHITELIST_BENCHMARK_OUT=/tmp/bench \
  pytest safe_transaction_service/history/tests/benchmarks/test_whitelist_benchmark.py -s

docker compose start web indexer-worker scheduler nginx
```

The tables are printed and written to `/tmp/bench.md` and `/tmp/bench.csv`. N=500
deploys about 1,750 transactions, so the run takes a few minutes. The benchmark is
skipped in the normal test suite.

## Reading the results

The report ends with the growth of every metric from the smallest to the largest N:

```
Growth N=20 -> N=500 (x25 Safes):
- rpc_calls: stock x.., whitelist x..
...
```

**Go/no-go:** the whitelist run's cost (RPC calls, response bytes and rows) must stay
roughly flat as N grows, while the stock run grows with N. If the whitelist run grows
with N, stop and revisit the design.

Expected shape:

- Stock transaction fetches, rows and response bytes grow linearly with N.
- Whitelist transaction fetches and rows stay constant (W Safes). Response bytes may
  grow slightly: the stock-compatible `eth_getLogs` responses are small but not empty.
- `eth_getLogs` calls are constant in both modes for a given span. The whitelist mode
  makes `2 × ceil((W + factories) / chunk size)`-ish calls per window; that's the cost
  of filtering by address on the node.

## Real networks

On a deployed service, compare row counts and table sizes of a whitelist deployment
against a stock one (or against the same network before enabling the whitelist):

```bash
psql "$DATABASE_URL" -f scripts/whitelist_db_stats.sql
```
