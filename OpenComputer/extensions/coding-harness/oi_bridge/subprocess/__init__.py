# ruff: noqa: N999  -- directory 'oi-capability' has a hyphen (required by plugin manifest)
"""Subprocess package — parent-side process management and JSON-RPC client.

The only file permitted to `import interpreter` is server.py, which
runs INSIDE the OI venv as a child process — never in this process.
"""
