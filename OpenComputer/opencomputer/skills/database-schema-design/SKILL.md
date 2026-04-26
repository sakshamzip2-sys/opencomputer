---
name: database-schema-design
description: Use when designing tables, columns, indexes, foreign keys, normalization, or planning database migrations
---

# Database Schema Design

## When to use

- Adding a new table or feature that touches multiple tables
- Reviewing a migration before merge
- Diagnosing slow queries that point to schema problems

## Steps

1. **Write the queries first.** Before any DDL, list the read patterns: "find all X where Y, joined to Z". Schema falls out of that.
2. **Pick the right key.** Surrogate (UUID/serial) for stability; natural key only when the business owns it.
3. **Index the WHERE/JOIN/ORDER columns.** Not the SELECT columns. Composite indexes match query order, not field order in the table.
4. **Foreign keys with explicit ON DELETE.** Default CASCADE is rarely what you want; default RESTRICT lets you catch dependencies. Pick one consciously.
5. **Normalize until it hurts, denormalize until it works.** Default to 3NF; denormalize only when you have a profile showing the join is the cost.
6. **Migrations are forward-only.** Backfill in batches; never lock big tables in a transaction.
7. **Soft deletes are a schema decision.** Add `deleted_at TIMESTAMP NULL` consistently and add it to every WHERE clause, or don't soft-delete.

## Notes

- `NULL` and `NOT NULL` are part of the schema, not optional. Decide explicitly.
- TIMESTAMPTZ > TIMESTAMP. Always.
- Don't store JSON for fields you'll query — that's a sign you should split the table.
