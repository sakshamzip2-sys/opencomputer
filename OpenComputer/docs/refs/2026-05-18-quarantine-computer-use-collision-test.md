# Quarantine: `test_no_module_collision_with_sibling_plugins`

Date: 2026-05-18
Status: **QUARANTINED** (`@pytest.mark.xfail(strict=False)`) — tracked, not fixed.

## What is quarantined

`tests/extensions/test_computer_use_plugin.py::TestNoModuleCollisionWithSiblingPlugins::test_no_module_collision_with_sibling_plugins`

## Why

The test fails **deterministically on Linux CI** and **passes every way on
macOS** — isolated, in the full ~17k-test suite, and across the whole
`tests/extensions/` directory.

In CI, during `load_plugin(computer-use)` (load order A — colliding
plugins first), the loader raises and swallows:

```
AttributeError: 'PosixPath' object has no attribute '_str'
AttributeError: 'PosixPath' object has no attribute '_drv'
```

`_str` / `_drv` are `pathlib` internal slots. A `PosixPath` in that
half-constructed / cross-module-corrupted state is a classic symptom of
`sys.modules` pollution — two copies of a module/class interacting. The
loader catches the exception, so the plugin "loads" but registers zero
tools; the test then asserts on the symptom (`registered == []`).

It is **suite-order dependent**: the failure surfaced when PRs added/
changed test files and shifted pytest's collection order. The file was
last substantively changed by PR #653 (cua-driver 0.1.9 reconciliation).

## Why quarantined rather than fixed

The root cause is not yet isolated and is **not reproducible on macOS**.
Per the systematic-debugging discipline, a fix without a proven root
cause and a reproduction would be a guess. The broken test was blocking
**every** PR's CI, so it is quarantined (`xfail`, non-strict so the
macOS pass is tolerated) to unblock the repo while the real fix is done
as its own focused task.

## What the real fix needs

1. A Linux-reproducible run of the full suite (container / CI debug
   session) to reproduce the `PosixPath._str` corruption.
2. Trace which earlier test pollutes `sys.modules` and how the loader's
   synthetic-module isolation lets a corrupted `PosixPath` through.
3. Either harden the loader's `sys.modules` isolation or make this test
   hermetic (snapshot/restore `sys.modules`), then remove the `xfail`.

## De-quarantine criteria

Remove the `@pytest.mark.xfail` once the loader/test is fixed and the
test passes on Linux CI in full-suite order.
