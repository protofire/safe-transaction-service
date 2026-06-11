# Migration Risk Analysis: v5.42.1 → v6.4.0

**New migrations introduced:** 1 (only `0101` — migrations 0096–0100 already present in v5.42.1)

---

## 0101 — `multisigtransaction_payment`

**File:** `safe_transaction_service/history/migrations/0101_multisigtransaction_payment.py`

**Operation:** `AddField` — adds a nullable `Uint256Field(blank=True, default=None, null=True)` named `payment` to the `MultiSigTransaction` model.

**Risk: LOW**

Adding a nullable column with a default of `None` is a metadata-only change in PostgreSQL 11+. It does **not** rewrite the table and acquires only a brief `ACCESS EXCLUSIVE` lock to update the schema catalog — typically milliseconds even on large tables.

No backfill is performed in this migration. The `payment` value is populated at indexing time from `ExecutionSuccess`/`ExecutionFailure` events going forward.

**Recommended approach:** Apply during normal deployment with no maintenance window required.

---

## Previously Migrated (already in v5.42.1, for reference)

These migrations were introduced between v5.39.0 and v5.42.1 and are already applied in any deployment running `v5.42.1`. They are **not re-run** and listed here only for historical context.

| Migration                                       | Risk                  | Notes                                                                                                                                                  |
| ----------------------------------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `0096_internaltxdecoded_safe_address`           | Low                   | Added `safe_address` column to `internaltxdecoded`; backfilled only unprocessed records (~30s).                                                        |
| `0097_internaltxdecoded_safe_address_processed` | **High (historical)** | Backfilled `safe_address` for ALL historical processed records via JOIN on `history_internaltx._from`. Took 5–10 min on large tables. Already applied. |
| `0098_internaltxdecoded_setup_idx`              | Low                   | Added partial index `history_decoded_setup_idx`. Already applied.                                                                                      |
| `0099_alter_ethereumtx`                         | Medium (historical)   | Field type changes on `EthereumTx` + index renames. Already applied.                                                                                   |
| `0100_safelaststatus_module_guard`              | Low                   | Added nullable `module_guard` columns. Already applied.                                                                                                |

---

## Summary

| Risk Level | Count |
| ---------- | ----- |
| High       | 0     |
| Medium     | 0     |
| Low/Safe   | 1     |

**No maintenance window required** for the v6.4.0 migration. The single new migration (`0101`) is a safe nullable `ADD COLUMN` with no backfill.
