"""Tests for cli_ui.busy_indicator."""

from opencomputer.cli_ui.busy_indicator import STYLES, BusyIndicator, _wcswidth


def test_all_styles_registered() -> None:
    for name in ("kawaii", "minimal", "dots", "wings", "none"):
        assert name in STYLES


def test_each_style_has_uniform_width() -> None:
    for name, frames in STYLES.items():
        if name == "none":
            continue  # only one frame
        widths = {_wcswidth(f) for f in frames}
        assert len(widths) == 1, f"{name} has non-uniform widths {widths}"


def test_indicator_cycles_frames() -> None:
    bi = BusyIndicator(style="dots")
    seen = {bi.next_frame() for _ in range(len(STYLES["dots"]) * 2)}
    # Strip the trailing pad spaces before comparing — STYLES values are padded.
    assert {f.rstrip() for f in seen} == {f.rstrip() for f in STYLES["dots"]}


def test_unknown_style_falls_back_to_kawaii() -> None:
    bi = BusyIndicator(style="not-a-real-style")
    assert bi.style == "kawaii"


def test_none_style_renders_empty() -> None:
    bi = BusyIndicator(style="none")
    assert bi.next_frame() == ""


def test_reset_restarts_cycle() -> None:
    bi = BusyIndicator(style="minimal")
    f1 = bi.next_frame()
    bi.next_frame()
    bi.reset()
    f3 = bi.next_frame()
    assert f1 == f3
