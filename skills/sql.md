# Skill: SQL

## Query patterns

- Use CTEs (`WITH name AS (...)`) to break complex queries into named, readable steps. Avoid deeply nested subqueries.
- Use window functions (`ROW_NUMBER()`, `RANK()`, `LAG()`, `LEAD()`, `SUM() OVER (...)`) instead of self-joins for running totals, rankings, and adjacent-row comparisons.
- `GROUP BY` on the minimal necessary columns. Include all non-aggregated `SELECT` columns in `GROUP BY` unless the database infers it.
- Filter early: apply `WHERE` before `JOIN` and `HAVING` when possible, so the engine works on a smaller set.
- `EXPLAIN` or `EXPLAIN ANALYZE` before optimising — confirm the slow step before adding indexes.

## Aggregations

- `COUNT(*)` counts all rows including NULLs; `COUNT(col)` skips NULLs. Be explicit.
- `SUM` / `AVG` over NULL-containing columns: `COALESCE(col, 0)` if zero-filling makes sense; leave NULL if absence of data has meaning.
- `DISTINCT` inside aggregates (`COUNT(DISTINCT col)`) is expensive on large tables — consider approximate counts (`HLL`, `APPROX_COUNT_DISTINCT`) where exact precision is not required.

## Joins

- Prefer explicit `JOIN ... ON` over implicit comma joins in `FROM`.
- `INNER JOIN` by default; `LEFT JOIN` when the right side may be absent; `CROSS JOIN` only when you mean every combination.
- Check for accidental fan-out: joining on a non-unique key multiplies rows. Verify join cardinality with a `SELECT COUNT(*)` on the join condition first.

## Indexes

- Index foreign keys and columns that appear in `WHERE`, `JOIN ON`, and `ORDER BY` of frequent queries.
- Composite indexes: most-selective column first; column order matters.
- Partial indexes (where supported) to index only the interesting subset: `CREATE INDEX ON orders (customer_id) WHERE status = 'pending'`.
- Covering indexes include all columns a query needs, eliminating the table lookup entirely.
- Unused indexes cost write performance — check `pg_stat_user_indexes` or equivalent periodically.

## Safe updates and deletes

- Always wrap destructive statements in a transaction. `BEGIN; DELETE ...; SELECT COUNT(*) FROM ...;` — verify, then `COMMIT` or `ROLLBACK`.
- Test `DELETE` / `UPDATE` predicates with a `SELECT` first.
- For large deletes, batch in chunks (`WHERE id BETWEEN X AND Y`) to avoid table-level lock escalation.
- Add a `LIMIT` to `DELETE` in MySQL/MariaDB when batching.

## Schema and migrations

- Add `NOT NULL` columns with a `DEFAULT` in a single `ALTER TABLE` statement to avoid rewriting the table twice (Postgres 11+).
- For zero-downtime migrations on live tables: add nullable column → backfill in batches → add `NOT NULL` constraint.
- Name constraints explicitly (`CONSTRAINT fk_orders_customer FOREIGN KEY ...`) so error messages are readable.
- Use `TIMESTAMP WITH TIME ZONE` (not `TIMESTAMP`) for event times.
