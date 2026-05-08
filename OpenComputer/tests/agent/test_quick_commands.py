"""Tests for agent.quick_commands."""

from pathlib import Path

import pytest

from opencomputer.agent.quick_commands import (
    QuickCommandError,
    QuickCommands,
    QuickResult,
)


def _write(p: Path, body: str) -> None:
    p.write_text(body, encoding="utf-8")


def test_loads_from_yaml(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    _write(cfg, """
quick_commands:
  echo:
    type: exec
    command: echo hello
  ll:
    type: alias
    target: /tools
""")
    qc = QuickCommands.load(cfg)
    assert "echo" in qc
    assert "ll" in qc
    assert qc["echo"].type == "exec"
    assert qc["ll"].type == "alias"


def test_loads_missing_file_yields_empty(tmp_path: Path) -> None:
    qc = QuickCommands.load(tmp_path / "missing.yaml")
    assert qc.commands == {}


def test_exec_runs_subprocess(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  ok:
    type: exec
    command: echo from-quick
""")
    qc = QuickCommands.load(cfg)
    res = qc.run("ok", "")
    assert isinstance(res, QuickResult)
    assert "from-quick" in res.output


def test_exec_timeout_kills_long_command(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  forever:
    type: exec
    command: sleep 60
""")
    qc = QuickCommands.load(cfg, timeout=0.5)
    res = qc.run("forever", "")
    assert res is not None
    assert res.timed_out is True


def test_alias_recurses_through_dispatcher(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  greet:
    type: alias
    target: /usage
""")
    seen: list[tuple[str, str, int]] = []

    def fake_dispatcher(name: str, args: str, depth: int) -> QuickResult:
        seen.append((name, args, depth))
        return QuickResult(
            output="dispatched", timed_out=False, depth=depth
        )

    qc = QuickCommands.load(cfg, dispatcher=fake_dispatcher)
    res = qc.run("greet", "extra args")
    assert seen == [("usage", "extra args", 1)]
    assert res is not None
    assert res.depth == 1


def test_alias_loop_capped(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  a:
    type: alias
    target: /b
  b:
    type: alias
    target: /a
""")
    qc_holder: dict[str, QuickCommands] = {}

    def dispatcher(name: str, args: str, depth: int) -> QuickResult:
        result = qc_holder["qc"].run(name, args, _depth=depth)
        return result if result else QuickResult(output="", timed_out=False)

    qc = QuickCommands.load(cfg, dispatcher=dispatcher)
    qc_holder["qc"] = qc
    with pytest.raises(QuickCommandError, match="alias loop"):
        qc.run("a", "")


def test_unknown_returns_none() -> None:
    qc = QuickCommands(commands={})
    assert qc.run("nope", "") is None


def test_alias_without_target_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  bad:
    type: alias
""")

    def dispatcher(name: str, args: str, depth: int) -> QuickResult:
        return QuickResult(output="", timed_out=False)

    qc = QuickCommands.load(cfg, dispatcher=dispatcher)
    with pytest.raises(QuickCommandError, match="no target"):
        qc.run("bad", "")


def test_alias_without_dispatcher_raises(tmp_path: Path) -> None:
    cfg = tmp_path / "c.yaml"
    _write(cfg, """
quick_commands:
  hello:
    type: alias
    target: /world
""")
    qc = QuickCommands.load(cfg)  # no dispatcher
    with pytest.raises(QuickCommandError, match="dispatcher"):
        qc.run("hello", "")
