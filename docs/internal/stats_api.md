# Internal Stats API Endpoints

This document describes internal API endpoints intended for statistics gathering and analysis.
These endpoints are subject to change and are not part of the public stable API.

## Global All Transactions

Retrieves all executed transactions across all Safes, ordered chronologically (oldest first) using keyset pagination.

**Endpoint:**

```
GET /api/v2/stats/all-transactions/
```

**Activation:**

This endpoint is only active if the `ENABLE_STATS_API` environment variable (or setting) is set to `True`.

**Authentication:**

Requires a valid API key sent in the `X-Internal-API-Key` header. Valid keys are defined in the `INTERNAL_API_KEYS` environment variable (or setting), which should be a comma-separated list.

**Query Parameters:**

*   `limit` (integer, optional): Number of items to retrieve per page. Defaults to `100` (or `settings.REST_FRAMEWORK['PAGE_SIZE']`), maximum `1000`.
*   `updated_after` (string, optional): Filters results to include only transactions with a timestamp greater than or equal to the provided value. Format: ISO 8601 (e.g., `2024-07-19T10:00:00Z`).
*   `cursor` (string, optional): An opaque cursor string (Base64 encoded `timestamp|id`) obtained from the `next_cursor` field of a previous response. Used to fetch the next page of results.

**Response Format (Success: 200 OK):**

```json
{
  "next_cursor": "BASE64_ENCODED_TIMESTAMP_PIPE_ID_STRING_OR_NULL",
  "data": [
    {
      // Transaction object (schema matches the public 
      // /api/v2/safes/{address}/all-transactions/ response items)
      "type": "MULTISIG_TRANSACTION", 
      // ... other fields
    },
    {
      "type": "ETHEREUM_TRANSACTION",
      // ... other fields
    },
    // ... more transactions
  ]
}
```

*   `next_cursor`: Contains the cursor string to use in the `cursor` query parameter to fetch the next page. It is `null` if there are no more pages.
*   `data`: An array of transaction objects. The structure of each object depends on its type (`MULTISIG_TRANSACTION`, `MODULE_TRANSACTION`, `ETHEREUM_TRANSACTION`) and matches the schema of the public per-safe endpoint.

**Error Responses:**

*   `403 Forbidden`: If `ENABLE_STATS_API` is `False`, or if the `X-Internal-API-Key` header is missing or contains an invalid key.
*   `400 Bad Request`: If `limit`, `updated_after`, or `cursor` parameters have an invalid format. 