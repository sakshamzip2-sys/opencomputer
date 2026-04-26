"""Benchmarks for OpenComputer agent loop quality.

Tests in this directory are opt-in via the ``benchmark`` pytest marker —
they exercise the full agent loop end-to-end against a real LLM provider
and are NOT run on every PR. Use ``pytest -m benchmark`` to invoke.
"""
