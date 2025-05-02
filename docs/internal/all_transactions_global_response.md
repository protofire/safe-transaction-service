## Endpoint Response Documentation

**URL:** `GET /api/v2/stats/all-transactions/`

**Description:** Retrieves a list of executed transactions relevant to *any* Safe contract on the configured chain. Results are ordered chronologically by execution time (oldest first) and paginated using a keyset cursor based on `(timestamp, id)`.

### Query Parameters

| Parameter       | Type      | Required | Description                                                                                                                                   | Default Value |
| :-------------- | :-------- | :------- | :-------------------------------------------------------------------------------------------------------------------------------------------- | :------------ |
| `limit`         | `integer` | No       | Maximum number of transaction items to return in the `data` array. The actual number returned might be less due to transaction aggregation. | `100` (Max: `1000`) |
| `cursor`        | `string`  | No       | A base64 encoded cursor (`timestamp|id`) from a previous response's `nextCursor` field to fetch the subsequent page.                          | `null`        |
| `updated_after` | `string`  | No       | ISO 8601 timestamp. If provided, only returns transactions executed *at or after* this time.                                                 | `null` (No filter) |

### Top-Level Response Structure

The response is a JSON object with the following fields:

```json
{
  "nextCursor": "string | null",
  "data": [
    "Transaction Object (one of three types)"
  ]
}
```

| Field        | Type         | Description                                                                                                                                                              | Nullable |
| :----------- | :----------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :------- |
| `nextCursor` | `string`     | A base64 encoded string representing the cursor to fetch the next page of results. Format: `timestamp\|id`. Use this value in the `cursor` query parameter for the next request. | Yes      |
| `data`       | `array`      | An array containing transaction objects. Each object in the array will conform to one of the three transaction types detailed below (MULTISIG_TRANSACTION, MODULE_TRANSACTION, or ETHEREUM_TRANSACTION). | No       |

---

### Transaction Object Types

Each object within the `data` array represents a transaction and will conform to one of the following three structures, identifiable by the `txType` field.

#### 1. Multisig Transaction (`txType`: "MULTISIG_TRANSACTION")

This represents a transaction executed via the Safe's standard multisig logic.

| Field                    | Type         | Description                                                                                                | Nullable |
| :----------------------- | :----------- | :--------------------------------------------------------------------------------------------------------- | :------- |
| `type`                   | `string`     | **Value: "TRANSACTION"** (Indicates the overall category)                                                 | No       |
| `txType`                 | `string`     | **Value: "MULTISIG_TRANSACTION"**                                                                         | No       |
| `safe`                   | `string`     | Checksummed address of the Safe that executed the transaction.                                             | No       |
| `to`                     | `string`     | Checksummed address of the transaction destination. Can be the null address (`0x0...0`) for contract creation. | No       |
| `value`                  | `string`     | Value in Wei transferred by the transaction.                                                               | No       |
| `data`                   | `string`     | Hexadecimal string (0x-prefixed) of the transaction data.                                                  | Yes      |
| `operation`              | `integer`    | Safe operation type: `0` (CALL), `1` (DELEGATE_CALL), `2` (CREATE).                                       | No       |
| `gasToken`               | `string`     | Checksummed address of the token used for gas payment (0x0...0 for Ether).                                 | Yes      |
| `safeTxGas`              | `string`     | Gas limit configured for the Safe transaction execution.                                                   | No       |
| `baseGas`                | `string`     | Gas reserved for overhead costs within the Safe contract logic.                                            | No       |
| `gasPrice`               | `string`     | Gas price (Wei) configured for the transaction (legacy, relevant for non-EIP1559 txs).                     | No       |
| `refundReceiver`         | `string`     | Checksummed address designated to receive gas refunds.                                                     | Yes      |
| `nonce`                  | `integer`    | The nonce of the Safe transaction.                                                                         | No       |
| `executionDate`          | `string`     | ISO 8601 timestamp of when the transaction was executed (mined).                                           | Yes      |
| `submissionDate`         | `string`     | ISO 8601 timestamp of when the transaction proposal was first submitted to the service.                    | No       |
| `modified`               | `string`     | ISO 8601 timestamp of when the transaction record was last modified by the service.                        | No       |
| `blockNumber`            | `integer`    | Block number in which the transaction was mined.                                                           | Yes      |
| `transactionHash`        | `string`     | Hexadecimal string (0x-prefixed) of the Ethereum transaction hash.                                         | Yes      |
| `safeTxHash`             | `string`     | Hexadecimal string (0x-prefixed) of the unique Safe transaction hash.                                      | No       |
| `proposer`               | `string`     | Checksummed address of the owner/delegate who proposed the transaction.                                    | Yes      |
| `proposedByDelegate`     | `string`     | Checksummed address of the delegate who proposed the transaction (if proposed by a delegate).              | Yes      |
| `executor`               | `string`     | Checksummed address of the account that executed the transaction on-chain.                                 | Yes      |
| `isExecuted`             | `boolean`    | `true` if the transaction has been successfully executed on-chain.                                         | No       |
| `isSuccessful`           | `boolean`    | `true` if the execution was successful, `false` if it failed (reverted).                                   | Yes      |
| `ethGasPrice`            | `string`     | Effective gas price (Wei) of the Ethereum transaction.                                                     | Yes      |
| `maxFeePerGas`           | `string`     | Maximum fee per gas (Wei) for EIP-1559 transactions.                                                       | Yes      |
| `maxPriorityFeePerGas`   | `string`     | Maximum priority fee per gas (Wei) for EIP-1559 transactions.                                              | Yes      |
| `gasUsed`                | `integer`    | Actual gas units consumed by the Ethereum transaction execution.                                           | Yes      |
| `fee`                    | `string`     | Total fee (Wei) paid for the Ethereum transaction (`gasUsed` * `ethGasPrice`).                             | Yes      |
| `origin`                 | `string`     | String representation of the origin information (e.g., Safe app name) submitted with the proposal.         | Yes      |
| `dataDecoded`            | `object`     | Decoded representation of the `data` field if recognized, otherwise `null`. Structure depends on the ABI.    | Yes      |
| `confirmationsRequired`  | `integer`    | Number of owner confirmations required for the Safe at the time of execution.                              | Yes      |
| `confirmations`          | `array`      | List of confirmation objects provided for this transaction.                                                | Yes      |
| `trusted`                | `boolean`    | Indicates if the transaction details are trusted (usually true if submitted via the service).              | No       |
| `signatures`             | `string`     | Hexadecimal string (0x-prefixed) representing the packed signatures submitted with the proposal (might be null). | Yes      |
| `transfers`              | `array`      | List of asset transfers (Ether, ERC20, ERC721) associated with this transaction. See [Transfer Object Structure](#transfer-object-structure) below. | No       |

*Confirmation Object Structure (within `confirmations` array):*

| Field           | Type     | Description                                                         | Nullable |
| :-------------- | :------- | :------------------------------------------------------------------ | :------- |
| `owner`         | `string` | Checksummed address of the owner who provided the confirmation.     | No       |
| `submissionDate`| `string` | ISO 8601 timestamp when this confirmation was submitted.            | No       |
| `transactionHash`| `string` | The `safeTxHash` this confirmation relates to.                      | No       |
| `signature`     | `string` | Hexadecimal string (0x-prefixed) of the owner's signature.          | No       |
| `signatureType` | `string` | Type of signature ("CONTRACT_SIGNATURE", "APPROVED_HASH", "EOA"). | No       |

---

#### 2. Module Transaction (`txType`: "MODULE_TRANSACTION")

This represents a transaction executed by an enabled Safe Module.

| Field                 | Type      | Description                                                                                                | Nullable |
| :-------------------- | :-------- | :--------------------------------------------------------------------------------------------------------- | :------- |
| `type`                | `string`  | **Value: "TRANSACTION"** (Indicates the overall category)                                                 | No       |
| `txType`              | `string`  | **Value: "MODULE_TRANSACTION"**                                                                           | No       |
| `safe`                | `string`  | Checksummed address of the Safe associated with the module execution.                                      | No       |
| `module`              | `string`  | Checksummed address of the Module that executed the transaction.                                           | No       |
| `to`                  | `string`  | Checksummed address of the transaction destination called by the module.                                   | No       |
| `value`               | `string`  | Value in Wei transferred by the module transaction.                                                        | Yes      |
| `data`                | `string`  | Hexadecimal string (0x-prefixed) of the transaction data passed by the module.                             | Yes      |
| `operation`           | `integer` | Safe operation type used by the module: `0` (CALL), `1` (DELEGATE_CALL).                                   | No       |
| `executionDate`       | `string`  | ISO 8601 timestamp of when the transaction was executed (mined).                                           | No       |
| `blockNumber`         | `integer` | Block number in which the transaction was mined.                                                           | No       |
| `isSuccessful`        | `boolean` | `true` if the module's internal transaction execution was successful, `false` if it failed.                | No       |
| `transactionHash`     | `string`  | Hexadecimal string (0x-prefixed) of the Ethereum transaction hash that triggered the module execution.     | No       |
| `moduleTransactionId` | `string`  | Internal ID for the module transaction record (format `internal_tx_id`).                                 | No       |
| `failed`              | `boolean` | Deprecated alias for `not isSuccessful`.                                                                   | No       |
| `dataDecoded`         | `object`  | Decoded representation of the `data` field if recognized, otherwise `null`. Structure depends on the ABI.    | Yes      |
| `transfers`           | `array`   | List of asset transfers (Ether, ERC20, ERC721) associated with this transaction. See [Transfer Object Structure](#transfer-object-structure) below. | No       |

---

#### 3. Ethereum Transaction (`txType`: "ETHEREUM_TRANSACTION")

This usually represents an *incoming* transaction (Ether or Token transfer) directly to the Safe address, not initiated via Multisig or Module logic. It can also represent outgoing transfers initiated by owners not using the Safe standard mechanisms (less common).

| Field           | Type      | Description                                                                                                | Nullable |
| :-------------- | :-------- | :--------------------------------------------------------------------------------------------------------- | :------- |
| `type`          | `string`  | **Value: "TRANSACTION"** (Indicates the overall category)                                                 | No       |
| `txType`        | `string`  | **Value: "ETHEREUM_TRANSACTION"**                                                                         | No       |
| `executionDate` | `string`  | ISO 8601 timestamp of when the transaction was executed (mined).                                           | No       |
| `blockNumber`   | `integer` | Block number in which the transaction was mined.                                                           | No       |
| `transactionHash`| `string` | Hexadecimal string (0x-prefixed) of the Ethereum transaction hash.                                         | No       |
| `to`            | `string`  | Checksummed address of the transaction destination. Usually the Safe address for incoming transactions.      | Yes      |
| `from`          | `string`  | Checksummed address of the transaction sender (EOA or contract).                                           | No       |
| `data`          | `string`  | Hexadecimal string (0x-prefixed) of the transaction data (often `null` or `0x` for simple transfers).      | Yes      |
| `transfers`     | `array`   | List of asset transfers (Ether, ERC20, ERC721) associated with this transaction. See [Transfer Object Structure](#transfer-object-structure) below. | No       |

---

### Transfer Object Structure

The `transfers` array within each transaction object contains details about asset movements. Each object in this array represents a single transfer.

| Field           | Type      | Description                                                                                                                             | Nullable | Present For Types |
| :-------------- | :-------- | :-------------------------------------------------------------------------------------------------------------------------------------- | :------- | :---------------- |
| `type`          | `string`  | Type of transfer: "ETHER_TRANSFER", "ERC20_TRANSFER", "ERC721_TRANSFER".                                                             | No       | All               |
| `executionDate` | `string`  | ISO 8601 timestamp of when the transfer occurred (block timestamp).                                                                     | No       | All               |
| `blockNumber`   | `integer` | Block number in which the transfer occurred.                                                                                            | No       | All               |
| `transactionHash`| `string` | Hexadecimal string (0x-prefixed) of the parent Ethereum transaction hash.                                                              | No       | All               |
| `to`            | `string`  | Checksummed address of the recipient.                                                                                                   | No       | All               |
| `from`          | `string`  | Checksummed address of the sender.                                                                                                      | No       | All               |
| `value`         | `string`  | Amount transferred (in Wei for Ether, in atomic units for ERC20).                                                                       | Yes      | ETHER, ERC20      |
| `tokenId`       | `string`  | Unique identifier for the NFT transferred.                                                                                              | Yes      | ERC721            |
| `tokenAddress`  | `string`  | Checksummed address of the ERC20 or ERC721 contract.                                                                                    | Yes      | ERC20, ERC721     |
| `tokenInfo`     | `object`  | Information about the token (name, symbol, decimals, logo). See [Token Info Structure](#token-info-structure) below.                     | Yes      | ERC20, ERC721     |
| `transferId`    | `string`  | A unique identifier for the transfer, constructed internally (e.g., `e_{tx_hash}_{log_index}`, `i_{tx_hash}_{trace_address}`).         | No       | All               |

---

### Token Info Structure

The `tokenInfo` object nested within ERC20 and ERC721 transfers contains details about the token contract.

| Field     | Type      | Description                                            | Nullable |
| :-------- | :-------- | :----------------------------------------------------- | :------- |
| `type`    | `string`  | **Value: "ERC20" or "ERC721"**                         | No       |
| `address` | `string`  | Checksummed address of the token contract.             | No       |
| `name`    | `string`  | Name of the token (e.g., "Dai Stablecoin").            | No       |
| `symbol`  | `string`  | Symbol of the token (e.g., "DAI").                     | No       |
| `decimals`| `integer` | Number of decimal places for the token (usually 18).   | Yes      |
| `logoUri` | `string`  | URL pointing to an image/logo for the token.           | No       |

