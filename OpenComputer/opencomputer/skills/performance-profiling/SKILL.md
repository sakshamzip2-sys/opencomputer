---
name: performance-profiling
description: Use when investigating slow code, identifying hotspots, benchmarking optimizations, or diagnosing latency issues
---

# Performance Profiling

## When to use

- "It's slow" — measure before guessing
- Comparing two implementations
- Pre-launch latency check

## Steps

1. **Reproduce the slow case.** A profile of code that isn't slow tells you nothing. Lock in a benchmark first.
2. **Pick the right tool for the layer:**
   - Python: `cProfile` for CPU, `tracemalloc` for memory, `py-spy` for live processes.
   - SQL: `EXPLAIN ANALYZE` on the actual query with realistic data.
   - HTTP: browser DevTools waterfall + server-side flamegraph.
3. **Statistical significance.** Run 10+ samples; ignore the first (cold caches). Take median, not mean.
4. **Don't optimize what doesn't show up in the profile.** Hot path is usually 1-2 functions. The other 98% are noise.
5. **Verify after.** Same benchmark, side by side. If the gain is < 10x, the change might be neutral after caching warms.
6. **Document the trade-off.** Faster + more memory? Faster + less readable? Make the tax visible.

## Notes

- Big-O matters when N is big; constants matter when N is small. Both can be wrong.
- "Slow database" usually means a missing index, not a fundamental DB choice.
- Network is almost always the answer for "slow API endpoint" — start there.
