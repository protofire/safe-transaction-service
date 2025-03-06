# Enhanced Logging for Debugging Timeouts

This document describes the enhanced logging that has been added to help debug the 900-second timeout issue with the `index_new_proxies_task`.

## What has been added?

Comprehensive timing and diagnostic logging has been added to several key components:

1. **EthereumIndexer** - Added timing logs to track:
   - Overall execution time of the `start` method
   - Time spent processing almost updated and not updated addresses
   - Detailed timing for each `process_addresses` call
   - Detailed information about block ranges being processed

2. **EventsIndexer** - Added extensive logging for RPC calls:
   - Detailed timing for `eth_getLogs` operations, including:
     - Time for the first request batch
     - Time for remaining request batches
     - Total RPC call time
   - Information about address chunking for RPC calls
   - Information about log receipts found
   - Timing for decoding operations

3. **auto_adjust_block_limit** - Enhanced logging to better understand block processing limit adjustments:
   - Timing information for each block range process
   - Detailed information about block limit increases and decreases
   - More detailed reasons for each block limit adjustment

4. **ProxyFactoryIndexer** - Added detailed logging for:
   - Contract event initialization
   - Processing of log receipts
   - Creation of SafeContract objects
   - Information about each proxy contract found

## How to Use This Logging

With this enhanced logging, you should be able to identify:

1. Which part of the indexing process is taking the most time
2. Whether the task is hanging on a specific RPC call
3. How many blocks are being processed in each batch
4. How the block process limit is being adjusted
5. If there are issues with specific addresses or block ranges

## Key Log Patterns to Look For

1. **Hanging RPC Calls**: Look for `eth_getLogs` calls without corresponding completion logs
2. **Excessive Block Range**: Check if the block process limit is growing too large
3. **Slow Processing**: Look for operations taking more than a few seconds
4. **Block Limit Adjustments**: Watch how the system adjusts `block_process_limit` over time

## Recommended Configuration

To avoid timeouts, consider:

1. Setting a reasonable `ETH_EVENTS_BLOCK_PROCESS_LIMIT_MAX` (e.g., 5000 blocks)
2. Adjust `ETH_EVENTS_GET_LOGS_CONCURRENCY` based on RPC node capabilities
3. Consider reducing `ETH_EVENTS_QUERY_CHUNK_SIZE` if the RPC node is struggling

## Next Steps

After running the service with this enhanced logging:

1. Look for any RPC calls that don't complete
2. Check if the block processing limit grows too large
3. Check if specific addresses or block ranges consistently cause problems
4. Consider implementing pagination of large block ranges if needed 