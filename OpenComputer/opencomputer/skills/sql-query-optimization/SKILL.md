---
name: sql-query-optimization
description: Use when investigating slow SQL queries, missing indexes, EXPLAIN plans, query rewrites, or N+1 problems
---

# SQL Query Optimization

## When to use

- Endpoint flagged slow and the slow call is a query
- New report query before merge
- "Why is this query 100x slower in production?"

## Steps

1. **Run `EXPLAIN ANALYZE`.** Not `EXPLAIN`. ANALYZE actually executes — gives real timings.
2. **Read it bottom-up.** Inner-most node first. Look for `Seq Scan` on big tables (missing index) or `Sort` (could be served by an index).
3. **Cardinality reality check.** Postgres' estimate vs actual rows. >10× off = stats are stale (`ANALYZE`) or query needs rewriting.
4. **Add the right index, not all indexes.** Indexes cost on writes; pick by query profile. Composite order matches WHERE order.
5. **Eliminate N+1s.** ORM lazy-loading is the #1 source. Use `JOIN` / `IN (subquery)` / `prefetch_related`.
6. **Avoid functions on indexed columns in WHERE.** `WHERE LOWER(email) = ?` defeats the index unless you have a functional index.
7. **LIMIT pages cursor-not-offset.** `OFFSET 100000` reads + discards 100000 rows. `WHERE id > last_seen_id LIMIT N` is constant time.

## Notes

- Triggers and views can hide cost — `EXPLAIN` shows the real plan including them.
- Prepared statement plans can go stale. `DEALLOCATE` and let it re-plan if data shape changed.
- `IS NULL` matches don't use B-tree indexes by default; use a partial index or NULLS FIRST/LAST sort.
