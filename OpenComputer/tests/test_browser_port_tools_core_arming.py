"""Unit tests for the arm-and-handle modules — dialog, file_chooser, downloads."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

import extensions.browser_control.tools_core.dialog as dialog_mod
import extensions.browser_control.tools_core.downloads as downloads_mod
import extensions.browser_control.tools_core.file_chooser as fc_mod
import pytest
from extensions.browser_control.tools_core.dialog import arm_dialog
from extensions.browser_control.tools_core.downloads import (
    DownloadSupersededError,
    arm_download,
    capture_download,
)
from extensions.browser_control.tools_core.file_chooser import arm_file_chooser


class _MockKeyboard:
    def __init__(self) -> None:
        self.pressed: list[str] = []

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class _MockPage:
    def __init__(self) -> None:
        self._listeners: dict[str, list[Any]] = {}
        self.keyboard = _MockKeyboard()

    def on(self, event: str, handler: Any) -> None:
        self._listeners.setdefault(event, []).append(handler)

    def remove_listener(self, event: str, handler: Any) -> None:
        try:
            self._listeners[event].remove(handler)
        except (KeyError, ValueError):
            pass

    off = remove_listener

    def fire(self, event: str, payload: Any) -> None:
        for h in list(self._listeners.get(event, [])):
            h(payload)


# ─── dialog ──────────────────────────────────────────────────────────


class _MockDialog:
    def __init__(self) -> None:
        self.accepted: list[str | None] = []
        self.dismissed = 0

    async def accept(self, prompt_text: str | None = None) -> None:
        self.accepted.append(prompt_text)

    async def dismiss(self) -> None:
        self.dismissed += 1


@pytest.mark.asyncio
async def test_arm_dialog_accept_round_trip() -> None:
    dialog_mod._reset_for_tests()
    page = _MockPage()
    dialog = _MockDialog()

    out = await arm_dialog(page, accept=True, prompt_text="hi")
    assert out["armed"] is True

    page.fire("dialog", dialog)
    await asyncio.sleep(0.02)
    assert dialog.accepted == ["hi"]


@pytest.mark.asyncio
async def test_arm_dialog_dismiss() -> None:
    dialog_mod._reset_for_tests()
    page = _MockPage()
    dialog = _MockDialog()
    await arm_dialog(page, accept=False)
    page.fire("dialog", dialog)
    await asyncio.sleep(0.02)
    assert dialog.dismissed == 1


@pytest.mark.asyncio
async def test_arm_dialog_silent_last_arm_wins() -> None:
    """A second arm bumps the id; the first listener becomes a silent no-op."""
    dialog_mod._reset_for_tests()
    page = _MockPage()
    d1 = _MockDialog()
    d2 = _MockDialog()
    await arm_dialog(page, accept=True, prompt_text="first")
    # second arm — bumps arm-id; first handler will see stale id and bail.
    await arm_dialog(page, accept=False)
    # Fire a single dialog event. Both listeners are still attached, but
    # only the most recent's arm-id matches; that one dismisses.
    page.fire("dialog", d1)
    await asyncio.sleep(0.05)
    # First arm's handler must NOT have fired accept (it's stale).
    assert d1.accepted == []
    # The second arm's handler ran dismiss on the same dialog object.
    assert d1.dismissed == 1
    # Unused dialog object is unaffected.
    assert d2.accepted == [] and d2.dismissed == 0


# ─── file chooser ────────────────────────────────────────────────────


class _MockFileChooser:
    def __init__(self) -> None:
        self.set_files_calls: list[list[str]] = []
        self._element = _MockElement()

    async def set_files(self, paths: list[str]) -> None:
        self.set_files_calls.append(paths)

    async def element(self) -> _MockElement:
        return self._element


class _MockElement:
    def __init__(self) -> None:
        self.evals: list[str] = []

    async def evaluate(self, js: str) -> None:
        self.evals.append(js)


@pytest.mark.asyncio
async def test_arm_file_chooser_with_real_file(tmp_path: Any) -> None:
    fc_mod._reset_for_tests()
    page = _MockPage()
    chooser = _MockFileChooser()
    f = tmp_path / "x.txt"
    f.write_text("hi")
    out = await arm_file_chooser(page, paths=[str(f)])
    assert out["armed"] is True

    page.fire("filechooser", chooser)
    await asyncio.sleep(0.02)
    assert chooser.set_files_calls == [[str(f)]]
    # Synthetic events were dispatched best-effort.
    assert any("dispatchEvent" in e for e in chooser._element.evals)


@pytest.mark.asyncio
async def test_arm_file_chooser_empty_paths_press_escape() -> None:
    """Empty paths is the explicit "dismiss the chooser via Escape" path."""
    fc_mod._reset_for_tests()
    page = _MockPage()
    chooser = _MockFileChooser()
    out = await arm_file_chooser(page, paths=[])
    assert out["paths"] == []
    page.fire("filechooser", chooser)
    await asyncio.sleep(0.02)
    # Nothing was uploaded.
    assert chooser.set_files_calls == []
    # And Escape was pressed to dismiss the dialog.
    assert "Escape" in page.keyboard.pressed


@pytest.mark.asyncio
async def test_arm_file_chooser_missing_file_raises(tmp_path: Any) -> None:
    fc_mod._reset_for_tests()
    page = _MockPage()
    bogus = tmp_path / "does-not-exist.txt"
    with pytest.raises(FileNotFoundError):
        await arm_file_chooser(page, paths=[str(bogus)])


# ─── downloads ───────────────────────────────────────────────────────


class _MockDownload:
    def __init__(self, url: str, suggested: str, payload: bytes = b"hello") -> None:
        self.url = url
        self.suggested_filename = suggested
        self._payload = payload

    async def save_as(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self._payload)


@pytest.mark.asyncio
async def test_arm_capture_round_trip(tmp_path: Any) -> None:
    downloads_mod._reset_for_tests()
    page = _MockPage()
    handle = await arm_download(page, timeout_ms=2000)
    download = _MockDownload("https://x.com/foo.bin", "foo.bin")
    page.fire("download", download)
    result = await capture_download(handle, out_dir=str(tmp_path))
    assert result.suggested_filename == "foo.bin"
    assert os.path.exists(result.path)
    with open(result.path, "rb") as fh:
        assert fh.read() == b"hello"


@pytest.mark.asyncio
async def test_double_arm_first_caller_sees_superseded(tmp_path: Any) -> None:
    """First arm + second arm + fire → first awaiter raises ``superseded``."""
    downloads_mod._reset_for_tests()
    page = _MockPage()
    handle1 = await arm_download(page, timeout_ms=1000)
    handle2 = await arm_download(page, timeout_ms=1000)

    download = _MockDownload("https://x.com/late.bin", "late.bin")
    # The single fire resolves both listener futures (both still attached).
    page.fire("download", download)

    # The newer arm (handle2) should succeed.
    r2 = await capture_download(handle2, out_dir=str(tmp_path))
    assert r2.path

    # The earlier arm (handle1) raises superseded because the
    # current_arm_id no longer matches.
    with pytest.raises(DownloadSupersededError):
        await capture_download(handle1, out_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_capture_download_timeout(tmp_path: Any) -> None:
    downloads_mod._reset_for_tests()
    page = _MockPage()
    handle = await arm_download(page, timeout_ms=100)
    with pytest.raises(TimeoutError):
        await capture_download(handle, out_dir=str(tmp_path))


@pytest.mark.asyncio
async def test_arm_file_chooser_silent_when_arm_replaced(tmp_path: Any) -> None:
    fc_mod._reset_for_tests()
    page = _MockPage()
    f = tmp_path / "y.txt"
    f.write_text("k")

    chooser = _MockFileChooser()
    await arm_file_chooser(page, paths=[str(f)])
    await arm_file_chooser(page, paths=[str(f)])
    # First listener is stale; only the second's id matches. Both
    # listeners are attached, both await the same future; whichever
    # reads `_current(page)` first sees arm 2 == arm 2 and proceeds
    # with set_files. The stale listener silently no-ops and the test
    # asserts no error surfaces.
    page.fire("filechooser", chooser)
    await asyncio.sleep(0.05)
    # At least one set_files call happened (the live arm).
    assert len(chooser.set_files_calls) >= 1


def test_temp_path_uses_sanitize() -> None:
    """``_build_temp_path`` should sanitize untrusted suggested name."""
    with tempfile.TemporaryDirectory() as td:
        p = downloads_mod._build_temp_path(td, "../../etc/passwd")
        # Sanitized → only the basename "passwd" survives the strip.
        assert p.startswith(td)
        assert "passwd" in os.path.basename(p)
        assert ".." not in os.path.basename(p)
