# Skill: SQL

## Query patterns

- Use CTEs (`WITH name AS (...)`) to break complex queries into named, readable steps. Avoid deeply nested subqueries.
- Use window functions (`ROW_NUMBER()`, `RANK()`, `LAG()`, `LEAD()`, `SUM() OVER (...)`) instead of self-joins for running totals, rankings, and adjacent-row comparisons.
- `GROUP BY` on the minimal necessary columns. Include all non-aggregated `SELECT` columns in `GROUP BY`.
- Filter early: apply `WHERE` before `JOIN` when possible, so the engine works on a smaller set.
- `EXPLAIN` / `EXPLAIN ANALYZE` before optimising — confirm the slow step before adding indexes.

## Aggregations

- `COUNT(*)` counts all rows including NULLs; `COUNT(col)` skips NULLs. Be explicit.
- `SUM` / `AVG` over NULL-containing columns: `COALESCE(col, 0)` if zero-filling makes sense; leave NULL if absence of data has meaning.
- `DISTINCT` inside aggregates (`COUNT(DISTINCT col)`) is expensive on large tables.

## Window functions — practical examples

```sql
-- Row number within a group (deduplicate: keep latest per user)
SELECT *
FROM (
    SELECT *,
        ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
    FROM orders
) ranked
WHERE rn = 1;

-- Running total
SELECT date, amount,
    SUM(amount) OVER (ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS running_total
FROM daily_sales;

-- Compare to previous row
SELECT date, amount,
    LAG(amount, 1) OVER (ORDER BY date) AS prev_amount,
    amount - LAG(amount, 1) OVER (ORDER BY date) AS change
FROM daily_sales;

-- Percentile rank
SELECT name, score,
    PERCENT_RANK() OVER (ORDER BY score) AS percentile,
    NTILE(4) OVER (ORDER BY score) AS quartile
FROM results;
```

## Recursive CTEs

```sql
-- Traverse a tree / hierarchy (e.g. org chart, category tree)
WITH RECURSIVE org AS (
    -- Anchor: start with top-level nodes
    SELECT id, name, parent_id, 0 AS depth, name AS path
    FROM employees
    WHERE parent_id IS NULL

    UNION ALL

    -- Recursive step: join children to parent
    SELECT e.id, e.name, e.parent_id, org.depth + 1,
           org.path || ' > ' || e.name
    FROM employees e
    JOIN org ON e.parent_id = org.id
)
SELECT * FROM org ORDER BY path;

-- Generate a date series (PostgreSQL)
WITH RECURSIVE dates AS (
    SELECT '2024-01-01'::date AS d
    UNION ALL
    SELECT d + 1 FROM dates WHERE d < '2024-01-31'
)
SELECT d FROM dates;
-- Simpler in PostgreSQL: SELECT generate_series('2024-01-01', '2024-01-31', '1 day')
```

## Joins

- Prefer explicit `JOIN ... ON` over implicit comma joins in `FROM`.
- `INNER JOIN` by default; `LEFT JOIN` when the right side may be absent; `CROSS JOIN` only when you mean every combination.
- Check for accidental fan-out: joining on a non-unique key multiplies rows. Verify join cardinality before writing the full query.

```sql
-- Detect fan-out before joining
SELECT COUNT(*) FROM orders o JOIN order_items i ON o.id = i.order_id;
-- If this is >> COUNT(*) FROM orders, you have a one-to-many relationship

-- LATERAL join — like a correlated subquery but as a table
SELECT u.id, u.name, latest.created_at AS last_order
FROM users u
LEFT JOIN LATERAL (
    SELECT created_at FROM orders WHERE user_id = u.id ORDER BY created_at DESC LIMIT 1
) latest ON true;
```

## Indexes

- Index foreign keys and columns that appear in `WHERE`, `JOIN ON`, and `ORDER BY` of frequent queries.
- Composite indexes: most-selective column first; column order matters for which queries can use the index.
- Partial indexes to index only the interesting subset:
  ```sql
  CREATE INDEX ON orders (customer_id) WHERE status = 'pending';
  ```
- Covering indexes include all columns a query needs, eliminating the table lookup:
  ```sql
  CREATE INDEX ON orders (customer_id) INCLUDE (total, created_at);
  ```
- Unused indexes cost write performance — check `pg_stat_user_indexes` (PostgreSQL) periodically.
- Expression indexes for queries on computed values:
  ```sql
  CREATE INDEX ON users (lower(email));  -- supports: WHERE lower(email) = 'alice@example.com'
  ```

## Safe updates and deletes

```sql
-- Always wrap destructive statements in a transaction and verify before committing
BEGIN;
DELETE FROM orders WHERE status = 'cancelled' AND created_at < '2023-01-01';
SELECT COUNT(*) FROM orders;  -- verify row counts look right
-- COMMIT;  -- or ROLLBACK if something is off

-- Test DELETE predicates with SELECT first
SELECT COUNT(*) FROM orders WHERE status = 'cancelled' AND created_at < '2023-01-01';

-- Batch large deletes to avoid table-level lock escalation
DO $$
DECLARE deleted int;
BEGIN
    LOOP
        DELETE FROM events WHERE id IN (
            SELECT id FROM events WHERE created_at < NOW() - INTERVAL '1 year' LIMIT 1000
        );
        GET DIAGNOSTICS deleted = ROW_COUNT;
        EXIT WHEN deleted = 0;
        PERFORM pg_sleep(0.1);  -- brief pause between batches
    END LOOP;
END $$;
```

## Schema and migrations

- Add `NOT NULL` columns with a `DEFAULT` in a single `ALTER TABLE` to avoid rewriting the table twice (PostgreSQL 11+).
- Zero-downtime migration pattern for large tables:
  1. Add nullable column (instant, no rewrite).
  2. Backfill in batches: `UPDATE table SET col = default WHERE col IS NULL AND id BETWEEN x AND y`.
  3. Add `NOT NULL` constraint: `ALTER TABLE ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID`, then `VALIDATE CONSTRAINT`.
- Name constraints explicitly (`CONSTRAINT fk_orders_customer FOREIGN KEY ...`) so error messages and `\d` output are readable.
- Use `TIMESTAMP WITH TIME ZONE` (not `TIMESTAMP`) for event times. Store in UTC, display in local time in the application.

## Transaction isolation

| Level | Dirty read | Non-repeatable read | Phantom read | Use when |
|---|---|---|---|---|
| READ UNCOMMITTED | possible | possible | possible | Rare; avoid |
| READ COMMITTED (default) | no | possible | possible | Most OLTP |
| REPEATABLE READ | no | no | possible (not in PG) | Reports that need consistent snapshot |
| SERIALIZABLE | no | no | no | Financial: balance checks, inventory |

```sql
-- Set isolation level for a transaction
BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ;
...
COMMIT;
```

## JSON in PostgreSQL

```sql
-- Store JSON; jsonb is binary-indexed, preferred over json
ALTER TABLE events ADD COLUMN metadata jsonb;

-- Query JSON fields
SELECT * FROM events WHERE metadata->>'source' = 'api';
SELECT * FROM events WHERE (metadata->>'retries')::int > 3;
SELECT * FROM events WHERE metadata @> '{"status": "failed"}';  -- contains

-- Extract nested value
SELECT metadata #>> '{user,email}' FROM events;

-- Build JSON in a query
SELECT json_build_object('id', id, 'name', name) FROM users;

-- Aggregate into JSON array
SELECT json_agg(row_to_json(u)) FROM users u WHERE active;

-- Index a JSON field
CREATE INDEX ON events ((metadata->>'source'));
CREATE INDEX ON events USING GIN (metadata);  -- full jsonb containment queries
```

## Reading query plans

```sql
EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT)
SELECT ...;
```

- **Seq Scan**: reads the whole table — expected for small tables or when fetching most rows. Bad for selective queries on large tables.
- **Index Scan**: uses an index to find rows — good for selective predicates.
- **Index Only Scan**: satisfies the query entirely from the index (covering index) — fastest.
- **Nested Loop**: works well when the outer side is small; scales poorly with large inputs.
- **Hash Join**: builds a hash table of the smaller side; efficient for larger joins.
- **Sort**: watch for sorts that spill to disk (`Sort Method: external merge`).
- Look at `actual rows` vs `rows=` estimate — large discrepancies mean stale statistics; run `ANALYZE tablename`.
