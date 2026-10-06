-- Row counts and total size (table + indexes + TOAST) of the tables affected by
-- WHITELISTED_SAFES. Compare a whitelist deployment against a stock one on the same
-- network, see docs/whitelist-benchmark.md
--
--   psql "$DATABASE_URL" -f scripts/whitelist_db_stats.sql
SELECT
    t.table_name,
    (xpath('/row/count/text()',
           query_to_xml(format('SELECT count(*) FROM %I', t.table_name), false, true, '')))[1]::text::bigint AS rows,
    pg_size_pretty(pg_total_relation_size(t.table_name::regclass)) AS total_size,
    pg_total_relation_size(t.table_name::regclass) AS total_bytes
FROM (
    VALUES
        ('history_ethereumblock'),
        ('history_ethereumtx'),
        ('history_internaltx'),
        ('history_internaltxdecoded'),
        ('history_erc20transfer'),
        ('history_erc721transfer'),
        ('history_saferelevanttransaction'),
        ('history_safecontract'),
        ('history_safestatus'),
        ('history_safelaststatus'),
        ('history_multisigtransaction'),
        ('history_moduletransaction')
) AS t(table_name)
ORDER BY total_bytes DESC;
