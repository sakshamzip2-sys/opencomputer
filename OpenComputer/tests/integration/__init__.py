"""Integration tests that spawn the real ``oc`` CLI binary via a pty.

These tests are opt-in. They:

  * require ``oc`` to be installed and on PATH,
  * use the user's REAL ``~/.opencomputer/`` profile home and live
    SessionDB (so the picker has something to render),
  * need a working pty subsystem (skipped on platforms without
    ``pty.openpty``).

Run with: ``pytest -m integration``. The default pytest config
deselects them via ``addopts = "-m 'not benchmark and not integration'"``.
"""
