# Skill: SQL

## Core rules

- Use CTEs (`WITH name AS (...)`) to break complex queries into readable steps. Avoid deep nested subqueries.
- Use window functions (`ROW_NUMBER()`, `SUM() OVER (...)`, `LAG()`) instead of self-joins for rankings, running totals, and adjacent-row comparisons.
- Use explicit `JOIN ... ON`, never comma joins. `INNER` by default; `LEFT` when the right side may be absent.
- Wrap every `DELETE`/`UPDATE` in a transaction, and run it as a `SELECT COUNT(*)` first to check the row count.
- `EXPLAIN ANALYZE` before adding indexes — confirm the slow step first.
- `COUNT(*)` counts NULLs; `COUNT(col)` skips them. Be explicit.
- Store event times as `TIMESTAMP WITH TIME ZONE` in UTC.

## Safe destructive changes

```sql
-- Verify the predicate BEFORE deleting
SELECT COUNT(*) FROM orders WHERE status = 'cancelled' AND created_at < '2023-01-01';

BEGIN;
DELETE FROM orders WHERE status = 'cancelled' AND created_at < '2023-01-01';
-- inspect, then COMMIT; or ROLLBACK;
```

Batch very large deletes (e.g. `LIMIT 1000` in a loop) to avoid long locks.

## Window functions (common uses)

```sql
-- Keep the latest row per user
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
  FROM orders
) t WHERE rn = 1;

-- Running total
SELECT date, SUM(amount) OVER (ORDER BY date) AS running_total FROM sales;
```

## Joins — watch cardinality

- Joining on a non-unique key multiplies rows (fan-out). Check counts before trusting a joined result.

## Indexes

- Index foreign keys and columns used in `WHERE`, `JOIN ON`, and `ORDER BY` of frequent queries.
- Composite index: put the most selective column first. Order determines which queries can use it.
- Unused indexes slow writes — remove them.

## Reading a query plan

- **Seq Scan** — whole table; fine for small tables, bad for selective queries on big ones.
- **Index Scan / Index Only Scan** — good; the latter is fastest (covering index).
- Compare `actual rows` vs the `rows=` estimate; a big gap means stale stats — run `ANALYZE`.
