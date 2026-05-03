"""Tests for static adapter validation."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_registry():
    from extensions.adapter_runner import clear_registry_for_tests

    clear_registry_for_tests()
    yield
    clear_registry_for_tests()


def test_validate_well_formed_adapter(tmp_path: Path):
    from extensions.adapter_runner._validation import validate_adapter_file

    f = tmp_path / "good.py"
    f.write_text(
        '''
from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="good",
    name="thing",
    description="good adapter",
    domain="example.com",
    strategy=Strategy.PUBLIC,
    columns=["a"],
)
async def run(args, ctx):
    return [{"a": 1}]
'''
    )
    result = validate_adapter_file(f)
    assert result.ok, f"expected ok, got errors {result.errors}"
    assert result.spec is not None
    assert result.spec.site == "good"


def test_validate_missing_decorator(tmp_path: Path):
    from extensions.adapter_runner._validation import validate_adapter_file

    f = tmp_path / "no_dec.py"
    f.write_text("async def run(args, ctx): return []\n")
    result = validate_adapter_file(f, skip_import=True)
    assert not result.ok
    assert any("@adapter" in e for e in result.errors)


def test_validate_sync_run_rejected(tmp_path: Path):
    from extensions.adapter_runner._validation import validate_adapter_file

    f = tmp_path / "sync.py"
    f.write_text(
        '''
from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="s",
    name="n",
    description="d",
    domain="e.com",
    strategy=Strategy.PUBLIC,
)
def run(args, ctx):
    return []
'''
    )
    result = validate_adapter_file(f, skip_import=True)
    assert not result.ok
    assert any("async def" in e for e in result.errors)


def test_validate_syntax_error(tmp_path: Path):
    from extensions.adapter_runner._validation import validate_adapter_file

    f = tmp_path / "bad.py"
    f.write_text("def def def\n")
    result = validate_adapter_file(f)
    assert not result.ok
    assert any("syntax" in e.lower() for e in result.errors)


def test_validate_warns_on_no_columns(tmp_path: Path):
    from extensions.adapter_runner._validation import validate_adapter_file

    f = tmp_path / "no_cols.py"
    f.write_text(
        '''
from extensions.adapter_runner import adapter, Strategy

@adapter(
    site="x",
    name="y",
    description="d",
    domain="e.com",
    strategy=Strategy.PUBLIC,
)
async def run(args, ctx):
    return []
'''
    )
    result = validate_adapter_file(f)
    assert result.ok
    assert any("columns" in w for w in result.warnings)
