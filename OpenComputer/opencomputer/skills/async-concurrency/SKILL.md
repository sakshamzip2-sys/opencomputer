---
name: async-concurrency
description: Use when writing async/await code, taskgroups, race conditions, deadlocks, or concurrent execution patterns
---

# Async / Concurrency

## When to use

- Adding async I/O to a previously sync codebase
- Debugging "it works locally but hangs in production"
- Reviewing concurrent code for race conditions

## Steps

1. **Async all the way down.** Mixing sync + async (a blocking `requests.get` inside an async handler) blocks the entire loop.
2. **Use TaskGroups for fan-out.** Python 3.11+: `asyncio.TaskGroup` — exception in one cancels siblings, no orphan tasks.
3. **Bounded concurrency.** Unbounded `asyncio.gather` with N=100k = hammered downstream. Wrap with a `Semaphore(K)`.
4. **Cancellation discipline.** When parent cancels, child must cleanup. Use `try/finally` or `async with` for cleanup.
5. **Race conditions live in shared state.** Two tasks reading-modifying-writing the same dict = corrupt state. Serialize via `Lock` or single-owner pattern.
6. **Deadlocks.** Always acquire locks in the same order. If you're holding lock A and need lock B, and another task does the reverse, you deadlock.
7. **Backpressure.** Producer faster than consumer = unbounded queue = OOM. Bounded queues + drop-or-block policy.

## Notes

- `time.sleep()` blocks the loop. Use `await asyncio.sleep()`.
- Don't create new event loops inside async code (`asyncio.run()` from inside `async def` is wrong).
- Threads + async coexist; CPU-bound work belongs in `run_in_executor`.
- Async exceptions can be silently swallowed if you don't `await` the task — always `await` or hand to a TaskGroup.
