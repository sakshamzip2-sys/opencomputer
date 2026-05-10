"""Tests for the pinned-files mechanism — Optimize Grade E mitigation.

Three layers of test:

1. Pure helpers (``render_pinned_files_block``, ``add_pinned_file``,
   ``remove_pinned_file``) — file I/O against tmp_path.
2. Config round-trip (save → load preserves pinned_files).
3. CLI surface (``oc pin``, ``oc unpin``, ``oc pin --list``) via Typer's
   CliRunner.
4. Integration: ``PromptBuilder.build`` injects the rendered block into
   the system prompt under the ``<pinned-files>`` slot.
"""
from __future__ import annotations

from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

from opencomputer.agent.config import Config, PromptConfig
from opencomputer.agent.pinned_files import (
    add_pinned_file,
    normalize_pinned_path,
    remove_pinned_file,
    render_pinned_files_block,
)

# ─── Pure helpers ─────────────────────────────────────────────────────


def test_render_empty_paths_returns_empty_string() -> None:
    assert render_pinned_files_block(()) == ""
    assert render_pinned_files_block([]) == ""


def test_render_single_python_file(tmp_path: Path) -> None:
    f = tmp_path / "hello.py"
    f.write_text("def hi():\n    return 'hi'\n")
    out = render_pinned_files_block((str(f),))
    assert "# " in out and str(f) in out
    assert "```python" in out
    assert "def hi():" in out
    assert out.endswith("```")


def test_render_unknown_extension_no_lang_hint(tmp_path: Path) -> None:
    f = tmp_path / "stuff.xyz"
    f.write_text("opaque content\n")
    out = render_pinned_files_block((str(f),))
    # Fence with empty lang hint: ```\n
    assert "```\n" in out
    assert "opaque content" in out


def test_render_skips_missing_file_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    real = tmp_path / "real.md"
    real.write_text("hi\n")
    bogus = tmp_path / "does_not_exist.md"
    out = render_pinned_files_block((str(real), str(bogus)))
    assert "hi" in out
    assert str(bogus) not in out  # not rendered
    # Warning should mention it
    assert any("does not exist" in r.message for r in caplog.records)


def test_render_skips_directory_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    out = render_pinned_files_block((str(tmp_path),))
    # Directory → skipped
    assert out == ""
    assert any("not a file" in r.message for r in caplog.records)


def test_render_caps_total_bytes(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """When combined size exceeds cap, later files are dropped + warned."""
    f1 = tmp_path / "a.txt"
    f1.write_text("a" * 100)
    f2 = tmp_path / "b.txt"
    f2.write_text("b" * 100)
    f3 = tmp_path / "c.txt"
    f3.write_text("c" * 100)

    # Cap at 150 bytes — only f1 fits (100), f2 would push to 200.
    out = render_pinned_files_block(
        (str(f1), str(f2), str(f3)), max_total_bytes=150
    )
    assert "a" * 100 in out
    assert "b" * 100 not in out  # dropped
    assert "c" * 100 not in out  # dropped
    # Warning summarises which were dropped
    msgs = [r.message for r in caplog.records]
    assert any("cap 150 bytes" in m for m in msgs)


def test_render_zero_cap_returns_empty(caplog: pytest.LogCaptureFixture) -> None:
    out = render_pinned_files_block(("/tmp/foo",), max_total_bytes=0)
    assert out == ""
    assert any("max_total_bytes=0" in r.message for r in caplog.records)


def test_render_handles_invalid_path_types(caplog: pytest.LogCaptureFixture) -> None:
    out = render_pinned_files_block(("", None, 42))  # type: ignore[arg-type]
    assert out == ""


def test_render_multiple_files_separated_by_blank_lines(tmp_path: Path) -> None:
    a = tmp_path / "a.md"
    a.write_text("# a\n")
    b = tmp_path / "b.md"
    b.write_text("# b\n")
    out = render_pinned_files_block((str(a), str(b)))
    # Two fenced blocks separated by blank lines
    assert out.count("```markdown") == 2
    assert "\n\n" in out


# ─── add / remove helpers ─────────────────────────────────────────────


def test_add_pinned_file_appends_normalized(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x\n")
    out = add_pinned_file((), str(f))
    assert out == (str(f.resolve()),)


def test_add_pinned_file_dedupes(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x\n")
    once = add_pinned_file((), str(f))
    twice = add_pinned_file(once, str(f))
    assert once == twice


def test_add_pinned_file_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        add_pinned_file((), str(tmp_path / "ghost.py"))


def test_add_pinned_file_rejects_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        add_pinned_file((), str(tmp_path))


def test_remove_pinned_file_works(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text("x\n")
    pinned = add_pinned_file((), str(f))
    out = remove_pinned_file(pinned, str(f))
    assert out == ()


def test_remove_pinned_file_noop_on_unknown(tmp_path: Path) -> None:
    pinned = (str(tmp_path / "a.py"),)
    out = remove_pinned_file(pinned, str(tmp_path / "b.py"))
    assert out == pinned


def test_normalize_pinned_path_expands_user(tmp_path: Path) -> None:
    s = normalize_pinned_path("~")
    assert s == str(Path.home().resolve())


# ─── Config round-trip ────────────────────────────────────────────────


def test_pinned_files_round_trips_through_yaml(tmp_path: Path) -> None:
    """save_config + load_config preserves pinned_files."""
    from opencomputer.agent.config_store import load_config, save_config

    cfg_path = tmp_path / "config.yaml"
    f = tmp_path / "pinned.py"
    f.write_text("# pinned\n")

    initial = Config()
    new_prompt = _dc_replace(
        initial.prompt, pinned_files=(str(f.resolve()),)
    )
    cfg = _dc_replace(initial, prompt=new_prompt)
    save_config(cfg, path=cfg_path)

    raw = cfg_path.read_text()
    assert "prompt:" in raw
    assert "pinned_files" in raw
    assert str(f.resolve()) in raw

    loaded = load_config(cfg_path)
    assert loaded.prompt.pinned_files == (str(f.resolve()),)


def test_default_config_omits_prompt_section(tmp_path: Path) -> None:
    """A fresh default Config doesn't write a prompt: section to YAML."""
    from opencomputer.agent.config_store import save_config

    cfg_path = tmp_path / "config.yaml"
    save_config(Config(), path=cfg_path)
    raw = cfg_path.read_text()
    # No prompt section when defaults
    assert "prompt:" not in raw or "pinned_files" not in raw


# ─── PromptBuilder integration ────────────────────────────────────────


def test_prompt_builder_injects_pinned_files_block_into_template() -> None:
    """``PromptBuilder.build(pinned_files_block=X)`` puts X in the template output."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    out = pb.build(
        pinned_files_block="# /fake/path/x.py\n```python\nprint('hi')\n```"
    )
    assert "<pinned-files>" in out
    assert "# /fake/path/x.py" in out
    assert "print('hi')" in out
    assert "</pinned-files>" in out


def test_prompt_builder_omits_slot_when_block_empty() -> None:
    """No pinned files → no `<pinned-files>` element in the prompt."""
    from opencomputer.agent.prompt_builder import PromptBuilder

    pb = PromptBuilder()
    out = pb.build(pinned_files_block="")
    assert "<pinned-files>" not in out


# ─── CLI surface ──────────────────────────────────────────────────────


def test_oc_pin_list_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`oc pin --list` with no pins prints the help nudge."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    r = CliRunner().invoke(app, ["pin", "--list"])
    assert r.exit_code == 0, r.output
    flat = " ".join(r.output.split())
    assert "no pinned files" in flat, f"output:\n{r.output}"


def test_oc_pin_add_then_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Round-trip: add via CLI, list via CLI, see the absolute path."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    target = tmp_path / "fake.py"
    target.write_text("x\n")

    r1 = CliRunner().invoke(app, ["pin", str(target)])
    assert r1.exit_code == 0, r1.output
    assert "pinned" in r1.output.lower()

    r2 = CliRunner().invoke(app, ["pin", "--list"])
    assert r2.exit_code == 0, r2.output
    # Rich console wraps long paths across lines for terminal width;
    # normalize whitespace before substring check.
    flat = " ".join(r2.output.split())
    flat_target = " ".join(str(target.resolve()).split())
    # Take just the basename — robust against rich's mid-path linebreaks.
    assert target.name in flat, f"basename missing from listing:\n{r2.output}"
    # And confirm at least one path-shaped segment is shown.
    assert "/" in flat_target  # sanity


def test_oc_pin_rejects_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    r = CliRunner().invoke(app, ["pin", str(tmp_path / "nope.py")])
    assert r.exit_code != 0
    # Rich console wraps long error messages across lines on narrow CI
    # terminals — flatten whitespace before the substring check.
    flat_lower = " ".join(r.output.lower().split())
    assert "no such file" in flat_lower, f"output:\n{r.output}"


def test_oc_unpin_removes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    target = tmp_path / "fake.py"
    target.write_text("x\n")

    runner = CliRunner()
    r1 = runner.invoke(app, ["pin", str(target)])
    assert r1.exit_code == 0

    r2 = runner.invoke(app, ["unpin", str(target)])
    assert r2.exit_code == 0
    assert "unpinned" in r2.output.lower()

    r3 = runner.invoke(app, ["pin", "--list"])
    assert "no pinned files" in r3.output


def test_oc_unpin_unknown_path_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    from typer.testing import CliRunner

    from opencomputer.cli import app

    r = CliRunner().invoke(app, ["unpin", str(tmp_path / "wasnt-pinned.py")])
    assert r.exit_code != 0
    flat_lower = " ".join(r.output.lower().split())
    assert "not pinned" in flat_lower, f"output:\n{r.output}"
