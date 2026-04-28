"""Tests for paste-fold helper."""
from opencomputer.cli_ui.paste_folder import LINE_THRESHOLD, PasteFolder


def test_short_text_passes_through():
    pf = PasteFolder()
    short = "one\ntwo\nthree"  # 3 lines, under threshold
    out, bid = pf.fold(short)
    assert out == short
    assert bid is None


def test_at_threshold_passes_through():
    pf = PasteFolder()
    text = "\n".join(["x"] * LINE_THRESHOLD)  # exactly threshold lines
    out, bid = pf.fold(text)
    assert out == text
    assert bid is None


def test_above_threshold_folds():
    pf = PasteFolder()
    text = "\n".join(["x"] * (LINE_THRESHOLD + 1))
    out, bid = pf.fold(text)
    assert bid == 1
    assert out == f"[Pasted text #1 +{LINE_THRESHOLD} lines]"


def test_counter_increments():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    out1, bid1 = pf.fold(text)
    out2, bid2 = pf.fold(text + "\nextra")
    assert bid1 == 1
    assert bid2 == 2
    assert "#1" in out1
    assert "#2" in out2


def test_extra_lines_count():
    """A 187-line paste shows '+186 lines' (lines beyond the first)."""
    pf = PasteFolder()
    text = "\n".join(["x"] * 187)
    out, _ = pf.fold(text)
    assert "+186 lines" in out


def test_trailing_newline_doesnt_inflate():
    """Trailing newline shouldn't count as an extra line."""
    pf = PasteFolder()
    text = "\n".join(["x"] * 10) + "\n"  # 10 visible + trailing \n
    out, _ = pf.fold(text)
    assert "+9 lines" in out


def test_expand_all_basic():
    pf = PasteFolder()
    full = "\n".join([f"line{i}" for i in range(20)])
    placeholder, _ = pf.fold(full)
    assert pf.expand_all(f"hello {placeholder} world") == f"hello {full} world"


def test_expand_all_unknown_id_left_alone():
    pf = PasteFolder()
    out = pf.expand_all("hello [Pasted text #999 +50 lines] world")
    assert out == "hello [Pasted text #999 +50 lines] world"


def test_expand_all_multiple_placeholders():
    pf = PasteFolder()
    big1 = "\n".join(["a"] * 10)
    big2 = "\n".join(["b"] * 10)
    p1, _ = pf.fold(big1)
    p2, _ = pf.fold(big2)
    out = pf.expand_all(f"{p1} and {p2}")
    assert out == f"{big1} and {big2}"


def test_is_same_as_last_initially_false():
    pf = PasteFolder()
    assert pf.is_same_as_last("anything") is False


def test_is_same_as_last_after_fold():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    pf.fold(text)
    assert pf.is_same_as_last(text) is True
    assert pf.is_same_as_last(text + "modified") is False


def test_is_same_as_last_tracks_most_recent():
    pf = PasteFolder()
    a = "\n".join(["a"] * 10)
    b = "\n".join(["b"] * 10)
    pf.fold(a)
    pf.fold(b)
    # Latest fold was b, so a should NOT match
    assert pf.is_same_as_last(b) is True
    assert pf.is_same_as_last(a) is False


def test_placeholder_for():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    placeholder, bid = pf.fold(text)
    assert pf.placeholder_for(bid) == placeholder
    assert pf.placeholder_for(999) is None


def test_placeholder_for_last():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    placeholder, _ = pf.fold(text)
    assert pf.placeholder_for_last() == placeholder


def test_placeholder_for_last_none_when_no_folds():
    pf = PasteFolder()
    assert pf.placeholder_for_last() is None


def test_has_active_fold():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    placeholder, _ = pf.fold(text)
    assert pf.has_active_fold(f"hi {placeholder}")
    assert not pf.has_active_fold("hi just text")
    # Unknown id placeholder doesn't count as active
    assert not pf.has_active_fold("hi [Pasted text #999 +50 lines]")


def test_clear_resets():
    pf = PasteFolder()
    text = "\n".join(["x"] * 10)
    pf.fold(text)
    pf.clear()
    assert pf._counter == 0
    assert pf._blobs == {}
    assert pf.placeholder_for_last() is None


def test_custom_threshold():
    pf = PasteFolder(threshold=2)
    out, bid = pf.fold("one\ntwo\nthree")  # 3 lines > 2
    assert bid == 1
    assert "#1" in out


def test_empty_string():
    pf = PasteFolder()
    out, bid = pf.fold("")
    assert out == ""
    assert bid is None


def test_single_line_no_fold():
    pf = PasteFolder()
    out, bid = pf.fold("just one line")
    assert out == "just one line"
    assert bid is None
