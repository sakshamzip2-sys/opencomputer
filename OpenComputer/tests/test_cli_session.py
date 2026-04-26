"""Tests for `opencomputer session` CLI (G.33 / Tier 4).

Covers list, show, fork, resume against a tmp_path-rooted SessionDB.
The CLI uses ``_home()`` to find sessions.db; we monkey-patch
``OPENCOMPUTER_HOME_ROOT`` so each test gets an isolated profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from opencomputer.agent.state import SessionDB
from opencomputer.cli_session import session_app
from plugin_sdk.core import Message

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point _home() at a fresh tmp_path so each test sees an empty DB."""
    monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
    return tmp_path


def _seed(home: Path, session_id: str, *, msgs: int = 3) -> SessionDB:
    """Drop a session + ``msgs`` messages into the active profile DB."""
    db = SessionDB(home / "sessions.db")
    db.create_session(
        session_id, platform="cli", model="claude-opus-4-7", title="Demo session"
    )
    for i in range(msgs):
        role = "user" if i % 2 == 0 else "assistant"
        db.append_message(
            session_id, Message(role=role, content=f"message {i}")
        )
    return db


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


class TestList:
    def test_empty_profile_shows_hint(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["list"])
        assert result.exit_code == 0
        assert "no sessions" in result.stdout.lower()

    def test_lists_seeded_session(self, isolated_home: Path) -> None:
        _seed(isolated_home, "abc123def456", msgs=2)
        result = runner.invoke(session_app, ["list"])
        assert result.exit_code == 0
        # The id may render with line-break wrapping in Rich's table
        # output, so check character-by-character (drop whitespace +
        # ANSI noise).
        flat = "".join(c for c in result.stdout if c.isalnum())
        assert "abc123def456" in flat

    def test_limit_clamps(self, isolated_home: Path) -> None:
        # Limit above max should fail validation (typer min/max).
        result = runner.invoke(session_app, ["list", "--limit", "5000"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestShow:
    def test_unknown_session_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["show", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_shows_metadata_and_head_preview(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "demo-id", msgs=4)
        result = runner.invoke(session_app, ["show", "demo-id"])
        assert result.exit_code == 0
        assert "demo-id" in result.stdout
        assert "Demo session" in result.stdout
        assert "claude-opus-4-7" in result.stdout
        # Default head=5; we seeded 4 messages — all should preview.
        assert "message 0" in result.stdout
        assert "message 3" in result.stdout

    def test_head_zero_skips_preview(self, isolated_home: Path) -> None:
        _seed(isolated_home, "demo-id", msgs=3)
        result = runner.invoke(session_app, ["show", "demo-id", "--head", "0"])
        assert result.exit_code == 0
        assert "Demo session" in result.stdout
        assert "message 0" not in result.stdout


# ---------------------------------------------------------------------------
# fork
# ---------------------------------------------------------------------------


class TestFork:
    def test_unknown_source_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["fork", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_fork_clones_session_and_messages(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "src-id", msgs=4)
        result = runner.invoke(session_app, ["fork", "src-id"])
        assert result.exit_code == 0
        assert "forked" in result.stdout.lower()
        # The new id is a fresh UUID hex — verify by listing.
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        ids = [s["id"] for s in sessions]
        # Both source + fork should be present.
        assert "src-id" in ids
        new_ids = [i for i in ids if i != "src-id"]
        assert len(new_ids) == 1
        # The forked session should have all 4 messages.
        forked = new_ids[0]
        msgs = db.get_messages(forked)
        assert len(msgs) == 4
        assert msgs[0].content == "message 0"
        assert msgs[3].content == "message 3"

    def test_fork_uses_explicit_title(self, isolated_home: Path) -> None:
        _seed(isolated_home, "src-id", msgs=1)
        runner.invoke(
            session_app,
            ["fork", "src-id", "--title", "what-if branch"],
        )
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        forked = next(s for s in sessions if s["id"] != "src-id")
        assert forked["title"] == "what-if branch"

    def test_fork_default_title_appends_suffix(
        self, isolated_home: Path
    ) -> None:
        _seed(isolated_home, "src-id", msgs=1)
        runner.invoke(session_app, ["fork", "src-id"])
        db = SessionDB(isolated_home / "sessions.db")
        sessions = db.list_sessions(limit=10)
        forked = next(s for s in sessions if s["id"] != "src-id")
        assert "(fork)" in (forked["title"] or "")


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


class TestResume:
    def test_unknown_session_returns_error(self, isolated_home: Path) -> None:
        result = runner.invoke(session_app, ["resume", "nonexistent"])
        assert result.exit_code == 1
        assert "not found" in result.stdout.lower()

    def test_prints_chat_resume_command(self, isolated_home: Path) -> None:
        _seed(isolated_home, "abc-resumable", msgs=2)
        result = runner.invoke(session_app, ["resume", "abc-resumable"])
        assert result.exit_code == 0
        # The exact resume command should be visible (modulo Rich
        # wrapping). Check the pieces individually.
        assert "opencomputer chat" in result.stdout
        assert "--resume" in result.stdout
        assert "abc-resumable" in result.stdout


# ---------------------------------------------------------------------------
# Round 2B P-12: --label / --agent / --search filters
# ---------------------------------------------------------------------------


def _seed_titled(
    home: Path,
    session_id: str,
    title: str,
    *,
    messages: list[tuple[str, str]] | None = None,
) -> None:
    """Drop a session with a custom title + arbitrary message texts."""
    db = SessionDB(home / "sessions.db")
    db.create_session(
        session_id, platform="cli", model="claude-opus-4-7", title=title
    )
    if messages:
        for role, content in messages:
            db.append_message(session_id, Message(role=role, content=content))


def _ids_in_output(output: str, candidates: list[str]) -> set[str]:
    """Return which candidate ids appear in *output* (Rich-table-safe)."""
    flat = "".join(c for c in output if c.isalnum())
    return {cid for cid in candidates if cid.replace("-", "") in flat}


class TestListFilters:
    """P-12: --label / --agent / --search filters for session list."""

    # --- --label ------------------------------------------------------

    def test_label_substring_match(self, isolated_home: Path) -> None:
        _seed_titled(isolated_home, "id-alpha", "Stock research notes")
        _seed_titled(isolated_home, "id-beta", "Cooking ideas")
        _seed_titled(isolated_home, "id-gamma", "Vacation planning")
        result = runner.invoke(session_app, ["list", "--label", "stock"])
        assert result.exit_code == 0
        seen = _ids_in_output(result.stdout, ["id-alpha", "id-beta", "id-gamma"])
        assert seen == {"id-alpha"}

    def test_label_case_insensitive(self, isolated_home: Path) -> None:
        _seed_titled(isolated_home, "id-mixed", "Q1 Report Draft")
        result = runner.invoke(session_app, ["list", "--label", "REPORT"])
        assert result.exit_code == 0
        assert "id-mixed" in "".join(c for c in result.stdout if c.isalnum() or c == "-")

    def test_label_no_match_returns_empty(self, isolated_home: Path) -> None:
        _seed_titled(isolated_home, "id-x", "alpha")
        _seed_titled(isolated_home, "id-y", "beta")
        result = runner.invoke(session_app, ["list", "--label", "zeta"])
        assert result.exit_code == 0
        assert "no sessions match" in result.stdout.lower()
        assert "id-x" not in result.stdout
        assert "id-y" not in result.stdout

    # --- --agent ------------------------------------------------------

    def test_agent_switches_profile_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--agent <name>`` reads sessions from that profile's DB."""
        # Arrange a two-profile world. Active profile = "default" (root).
        # Named profile = "coder" under <root>/profiles/coder/.
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
        # Seed the default profile.
        _seed_titled(tmp_path, "default-sess", "default profile work")
        # Seed the coder profile.
        coder_dir = tmp_path / "profiles" / "coder"
        coder_dir.mkdir(parents=True, exist_ok=True)
        _seed_titled(coder_dir, "coder-sess", "coder profile work")

        # Without --agent we see the default profile's session.
        result_default = runner.invoke(session_app, ["list"])
        assert result_default.exit_code == 0
        seen = _ids_in_output(
            result_default.stdout, ["default-sess", "coder-sess"]
        )
        assert "default-sess" in seen
        assert "coder-sess" not in seen

        # With --agent coder we see only the coder profile's session.
        result_coder = runner.invoke(session_app, ["list", "--agent", "coder"])
        assert result_coder.exit_code == 0
        seen2 = _ids_in_output(
            result_coder.stdout, ["default-sess", "coder-sess"]
        )
        assert "coder-sess" in seen2
        assert "default-sess" not in seen2

    def test_agent_invalid_profile_name_errors(
        self, isolated_home: Path
    ) -> None:
        # Uppercase / reserved names are rejected by validate_profile_name.
        result = runner.invoke(session_app, ["list", "--agent", "BADNAME"])
        assert result.exit_code == 1
        assert "error" in result.stdout.lower()

    # --- --search -----------------------------------------------------

    def test_search_returns_sessions_with_matches(
        self, isolated_home: Path
    ) -> None:
        _seed_titled(
            isolated_home,
            "match-sess",
            "any title",
            messages=[("user", "tell me about pancakes please")],
        )
        _seed_titled(
            isolated_home,
            "miss-sess",
            "another title",
            messages=[("user", "we should buy waffles")],
        )
        result = runner.invoke(session_app, ["list", "--search", "pancakes"])
        assert result.exit_code == 0
        seen = _ids_in_output(result.stdout, ["match-sess", "miss-sess"])
        assert seen == {"match-sess"}

    def test_search_no_match_returns_empty(self, isolated_home: Path) -> None:
        _seed_titled(
            isolated_home,
            "id-1",
            "title",
            messages=[("user", "hello world")],
        )
        result = runner.invoke(
            session_app, ["list", "--search", "zzz_no_such_token"]
        )
        assert result.exit_code == 0
        assert "no sessions match" in result.stdout.lower()

    def test_search_dedupes_multiple_message_matches(
        self, isolated_home: Path
    ) -> None:
        # The same session has multiple matching messages — should
        # appear ONCE in the output, not once per matched message.
        _seed_titled(
            isolated_home,
            "dup-sess",
            "title",
            messages=[
                ("user", "pancakes are great"),
                ("assistant", "pancakes are indeed great"),
                ("user", "more pancakes"),
            ],
        )
        result = runner.invoke(session_app, ["list", "--search", "pancakes"])
        assert result.exit_code == 0
        flat = "".join(c for c in result.stdout if c.isalnum() or c == "-")
        # 3 matching messages → only 1 row in the table.
        assert flat.count("dup-sess") == 1

    # --- FTS5 special-char inputs ------------------------------------

    def test_search_handles_colon_literally(self, isolated_home: Path) -> None:
        # `:` is the FTS5 column qualifier — without phrase wrapping
        # this query would be parsed as "column a, term b" and fail.
        _seed_titled(
            isolated_home,
            "colon-sess",
            "x",
            messages=[("user", "the ratio is a:b for the test")],
        )
        result = runner.invoke(session_app, ["list", "--search", "a:b"])
        assert result.exit_code == 0, result.stdout
        assert "colon-sess" in "".join(
            c for c in result.stdout if c.isalnum() or c == "-"
        )

    def test_search_handles_double_quote(self, isolated_home: Path) -> None:
        # Embedded `"` must be escaped as `""` by _escape_fts5.
        _seed_titled(
            isolated_home,
            "quote-sess",
            "x",
            messages=[("user", 'the literal a"b appears here')],
        )
        result = runner.invoke(session_app, ["list", "--search", 'a"b'])
        assert result.exit_code == 0, result.stdout
        assert "quote-sess" in "".join(
            c for c in result.stdout if c.isalnum() or c == "-"
        )

    def test_search_handles_asterisk(self, isolated_home: Path) -> None:
        # `*` is the FTS5 prefix operator — phrase wrapping makes it
        # literal so we don't accidentally do a prefix search.
        _seed_titled(
            isolated_home,
            "star-sess",
            "x",
            messages=[("user", "see commit a*b for details")],
        )
        result = runner.invoke(session_app, ["list", "--search", "a*b"])
        assert result.exit_code == 0, result.stdout
        # The FTS5 tokenizer may strip `*` from indexed text, so we
        # only assert exit_code success — i.e. the query did not
        # syntax-error. That's the load-bearing property of escaping.

    # --- Combinations -------------------------------------------------

    def test_label_plus_search_intersection(self, isolated_home: Path) -> None:
        _seed_titled(
            isolated_home,
            "both-sess",
            "alpha report",
            messages=[("user", "discussing widgets")],
        )
        _seed_titled(
            isolated_home,
            "label-only",
            "alpha summary",
            messages=[("user", "discussing gadgets")],  # no widgets
        )
        _seed_titled(
            isolated_home,
            "search-only",
            "beta report",
            messages=[("user", "discussing widgets")],  # title lacks alpha
        )
        result = runner.invoke(
            session_app,
            ["list", "--label", "alpha", "--search", "widgets"],
        )
        assert result.exit_code == 0
        seen = _ids_in_output(
            result.stdout, ["both-sess", "label-only", "search-only"]
        )
        assert seen == {"both-sess"}

    def test_agent_plus_search_combine(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OPENCOMPUTER_HOME_ROOT", str(tmp_path))
        monkeypatch.setenv("OPENCOMPUTER_HOME", str(tmp_path))
        # Seed coder profile with two sessions, only one matching the search.
        coder_dir = tmp_path / "profiles" / "coder"
        coder_dir.mkdir(parents=True, exist_ok=True)
        _seed_titled(
            coder_dir,
            "coder-other",
            "x",
            messages=[("user", "discussing pandas dataframes")],
        )
        _seed_titled(
            coder_dir,
            "coder-match",
            "x",
            messages=[("user", "matplotlib syntax help")],
        )
        # Default profile also contains "matplotlib" — should NOT leak in.
        _seed_titled(
            tmp_path,
            "default-match",
            "x",
            messages=[("user", "matplotlib question")],
        )
        result = runner.invoke(
            session_app,
            ["list", "--agent", "coder", "--search", "matplotlib"],
        )
        assert result.exit_code == 0
        seen = _ids_in_output(
            result.stdout,
            ["coder-other", "coder-match", "default-match"],
        )
        assert seen == {"coder-match"}


# ---------------------------------------------------------------------------
# Direct unit test for the FTS5 escaping helper
# ---------------------------------------------------------------------------


class TestEscapeFts5:
    """Targeted unit test for _escape_fts5 — the load-bearing helper."""

    def test_wraps_in_quotes(self) -> None:
        from opencomputer.cli_session import _escape_fts5

        assert _escape_fts5("hello") == '"hello"'

    def test_doubles_internal_quotes(self) -> None:
        from opencomputer.cli_session import _escape_fts5

        assert _escape_fts5('a"b') == '"a""b"'

    def test_passes_special_chars_inside_quotes(self) -> None:
        from opencomputer.cli_session import _escape_fts5

        # `:` `*` `(` `)` survive untouched inside the phrase wrapper —
        # FTS5 sees them as literal text, not operators.
        for ch in (":", "*", "(", ")", "AND", "NOT"):
            wrapped = _escape_fts5(f"x{ch}y")
            assert wrapped.startswith('"') and wrapped.endswith('"')
            assert ch in wrapped
