---
name: api-design
description: Use when designing REST or RPC endpoints, OpenAPI schemas, request/response contracts, or HTTP integrations
---

# API Design

## When to use

- Adding a new endpoint to a service
- Reviewing an OpenAPI spec
- Refactoring a chatty integration

## Steps

1. **Resources, not actions.** `POST /orders` not `POST /createOrder`. Verbs are HTTP methods.
2. **Idempotency for writes.** Every POST/PUT should accept an idempotency key (header or body). Retries must not double-charge.
3. **Pagination from day one.** `limit` + `cursor` (opaque), not `offset` (slow at scale).
4. **Errors with structure.** `{error: {code, message, details}}` — never plain text. Codes are a stable contract; messages are not.
5. **Field versions, not URL versions.** `/v2/orders` is fine; better is to add fields and deprecate gracefully via response headers.
6. **Compression + ETags.** Enable gzip; respect `If-None-Match`. Free wins for free.
7. **Document the failure modes.** What HTTP status when X? What does a 429 retry-after look like? What does a 503 mean for clients?

## Notes

- Don't return secrets in error responses.
- Accept ISO-8601 only for dates. No epoch ms unless every consumer is yours.
- Validate input with a schema (pydantic, JSON Schema). Reject early with 400.
