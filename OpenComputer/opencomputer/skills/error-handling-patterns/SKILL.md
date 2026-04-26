---
name: error-handling-patterns
description: Use when writing exception handlers, retry logic, fallback paths, or designing fault-tolerant code
---

# Error Handling Patterns

## When to use

- Adding `try/except` to handle a real failure mode
- Reviewing error handling in a PR
- Diagnosing a "swallowed exception" bug

## Steps

1. **Catch specific, not broad.** `except ConnectionError` not `except Exception`. Broad excepts hide bugs that aren't network bugs.
2. **Re-raise unless you have a recovery.** If you can't recover, log and `raise`. Logging-and-swallowing is the worst pattern.
3. **Fail closed for security.** Auth check failures → deny. Default to refusal, not allowance.
4. **Retry only idempotent operations.** Read: yes. POST without idempotency key: no. Use exponential backoff with jitter.
5. **Fallback only when you have a real fallback.** Stale cache OK; making up an empty result and pretending success is not.
6. **Error context.** Include the input that caused the failure (sanitized for secrets). "ConnectionError" tells nothing; "ConnectionError to api.x.com after 3 retries" is debuggable.
7. **Boundary handling.** Catch at module boundaries, not deep inside. The deeper a try, the harder it is to reason about.

## Notes

- `finally` for cleanup, not for "always succeed" semantics.
- A bare `except:` (no class) catches `KeyboardInterrupt` too. Don't.
- An exception is data — log it with structured fields, not as a raw string.
