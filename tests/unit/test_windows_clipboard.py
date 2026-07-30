"""
Unit tests for the Windows clipboard monitor.

These tests do NOT run any Win32 code. The module under test cannot even be
imported off Windows (``import win32clipboard`` and ``from ctypes import windll``
both fail, which flips ``HAS_WIN32`` to False and makes the constructor raise), so
every test installs fake pywin32 modules into ``sys.modules`` plus a fake
``ctypes.windll`` and then imports the module fresh. That exercises the module's
logic - polling, hashing, lock discipline - against an in-memory clipboard while
never touching a real one.
"""

import importlib
import io
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest

MODULE_NAME = "yank.platform.windows.clipboard"
PACKAGE_NAME = "yank.platform.windows"

# Real Win32 clipboard format ids used by the module under test
CF_HDROP = 15
CF_DIB = 8
CF_UNICODETEXT = 13


class FakeSequenceNumber:
    """Stand-in for user32.GetClipboardSequenceNumber."""

    def __init__(self, value: int = 1000):
        self.value = value
        self.call_count = 0

    def bump(self, by: int = 1) -> None:
        self.value += by

    def __call__(self) -> int:
        self.call_count += 1
        return self.value


class FakeWin32Clipboard:
    """
    Minimal in-memory stand-in for the win32clipboard module.

    Tracks open/close calls so tests can assert the monitor is not grabbing the
    global clipboard lock, and that it always releases it.
    """

    def __init__(self, png_format: int = 49999):
        self.contents: dict = {}
        self.png_format = png_format
        self.register_error = None
        self.open_error = None
        # Formats that are advertised but blow up on read (delayed rendering)
        self.get_errors: dict = {}
        self.calls: list = []
        self.open_count = 0
        self.close_count = 0
        self.is_open = False
        self.double_open = False

    def reset_counts(self) -> None:
        self.calls = []
        self.open_count = 0
        self.close_count = 0

    # --- win32clipboard API surface --------------------------------------
    def OpenClipboard(self, hwnd=None):
        self.calls.append("OpenClipboard")
        if self.open_error is not None:
            raise self.open_error
        if self.is_open:
            self.double_open = True
        self.is_open = True
        self.open_count += 1

    def CloseClipboard(self):
        self.calls.append("CloseClipboard")
        self.close_count += 1
        self.is_open = False

    def EmptyClipboard(self):
        self.calls.append("EmptyClipboard")
        self.contents.clear()

    def RegisterClipboardFormat(self, name):
        self.calls.append("RegisterClipboardFormat")
        if self.register_error is not None:
            raise self.register_error
        return self.png_format

    def IsClipboardFormatAvailable(self, fmt):
        return fmt in self.contents

    def GetClipboardData(self, fmt):
        if fmt in self.get_errors:
            raise self.get_errors[fmt]
        if fmt not in self.contents:
            raise RuntimeError(f"clipboard format {fmt} not available")
        return self.contents[fmt]

    def SetClipboardData(self, fmt, data):
        self.contents[fmt] = data

    def SetClipboardText(self, text, fmt=None):
        self.contents[CF_UNICODETEXT] = text


@pytest.fixture
def win_env(monkeypatch):
    """Import the Windows clipboard module against fake pywin32 bindings."""
    import ctypes

    clipboard = FakeWin32Clipboard()
    sequence = FakeSequenceNumber()

    win32con = ModuleType("win32con")
    win32con.CF_UNICODETEXT = CF_UNICODETEXT

    fakes = {
        "win32clipboard": clipboard,
        "win32con": win32con,
        "win32api": MagicMock(name="win32api"),
        "win32gui": MagicMock(name="win32gui"),
        "pythoncom": MagicMock(name="pythoncom"),
    }
    for name, fake in fakes.items():
        monkeypatch.setitem(sys.modules, name, fake)

    # ctypes.windll only exists on Windows
    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(user32=SimpleNamespace(GetClipboardSequenceNumber=sequence)),
        raising=False,
    )

    saved = {name: sys.modules.pop(name, None) for name in (MODULE_NAME, PACKAGE_NAME)}
    module = importlib.import_module(MODULE_NAME)
    assert module.HAS_WIN32, "fake pywin32 modules were not picked up"

    yield SimpleNamespace(module=module, clipboard=clipboard, sequence=sequence)

    for name, original in saved.items():
        sys.modules.pop(name, None)
        if original is not None:
            sys.modules[name] = original


def make_monitor(win_env, tmp_path, **kwargs):
    """Build a monitor and forget the clipboard calls made while constructing it."""
    kwargs.setdefault("temp_dir", tmp_path / "clipboard-sync")
    monitor = win_env.module.WindowsClipboardMonitor(**kwargs)
    win_env.clipboard.reset_counts()
    return monitor


def make_png(path, color=(10, 120, 200)):
    from PIL import Image

    Image.new("RGB", (8, 8), color).save(path, "PNG")
    return path


def png_bytes(color=(250, 20, 20)):
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, "PNG")
    return buffer.getvalue()


class TestSequenceNumberGate:
    """Issue #13: OpenClipboard() must not be called on every poll."""

    def test_unchanged_sequence_number_never_opens_the_clipboard(self, win_env, tmp_path):
        monitor = make_monitor(win_env, tmp_path)
        monitor._last_change_count = win_env.sequence.value

        for _ in range(5):
            monitor._check_clipboard()

        assert win_env.clipboard.open_count == 0
        assert win_env.clipboard.calls == []
        # The lock-free API is what got called instead
        assert win_env.sequence.call_count == 5

    def test_changed_sequence_number_opens_the_clipboard_once(self, win_env, tmp_path):
        received = []
        monitor = make_monitor(win_env, tmp_path, on_text_copied=received.append)
        monitor._last_change_count = win_env.sequence.value
        win_env.clipboard.contents[CF_UNICODETEXT] = "hello from the peer"

        win_env.sequence.bump()
        monitor._check_clipboard()
        # Two further polls with no further change must not re-open
        monitor._check_clipboard()
        monitor._check_clipboard()

        assert win_env.clipboard.open_count == 1
        assert win_env.clipboard.close_count == 1
        assert win_env.clipboard.is_open is False
        assert received == ["hello from the peer"]

    def test_each_new_change_opens_the_clipboard_again(self, win_env, tmp_path):
        monitor = make_monitor(win_env, tmp_path)
        monitor._last_change_count = win_env.sequence.value

        for _ in range(3):
            win_env.sequence.bump()
            monitor._check_clipboard()

        assert win_env.clipboard.open_count == 3
        assert win_env.clipboard.close_count == 3

    def test_falls_back_to_opening_when_sequence_number_is_unavailable(self, win_env, tmp_path):
        monitor = make_monitor(win_env, tmp_path)
        # GetClipboardSequenceNumber returns 0 on failure
        win_env.sequence.value = 0

        monitor._check_clipboard()
        monitor._check_clipboard()

        assert win_env.module._get_clipboard_sequence_number() is None
        assert win_env.clipboard.open_count == 2

    def test_falls_back_to_opening_when_sequence_number_raises(self, win_env, tmp_path):
        monitor = make_monitor(win_env, tmp_path)

        def boom():
            raise OSError("user32 unavailable")

        win_env.module.windll.user32.GetClipboardSequenceNumber = boom

        monitor._check_clipboard()

        assert win_env.clipboard.open_count == 1

    def test_a_failed_open_does_not_consume_the_change(self, win_env, tmp_path):
        """
        The sequence number must only be committed once the open succeeded.

        Committing it up front would drop the copy permanently whenever the
        source application is still holding the clipboard - which is precisely
        the contention this polling change is meant to be gentle about.
        """
        received = []
        monitor = make_monitor(win_env, tmp_path, on_text_copied=received.append)
        monitor._last_change_count = win_env.sequence.value
        text = "copied while another app held the lock"
        win_env.clipboard.contents[CF_UNICODETEXT] = text

        # The source app bumps the sequence number, then keeps the lock a while
        win_env.sequence.bump()
        win_env.clipboard.open_error = RuntimeError("clipboard busy")
        monitor._check_clipboard()
        assert received == []

        # It lets go: the copy must still be delivered, not silently swallowed
        win_env.clipboard.open_error = None
        monitor._check_clipboard()

        assert received == [text]

    def test_a_failed_open_recovers_on_a_later_poll(self, win_env, tmp_path):
        received = []
        monitor = make_monitor(win_env, tmp_path, on_text_copied=received.append)
        monitor._last_change_count = win_env.sequence.value
        win_env.clipboard.contents[CF_UNICODETEXT] = "eventually consistent"

        win_env.sequence.bump()
        win_env.clipboard.open_error = RuntimeError("clipboard busy")
        monitor._check_clipboard()
        win_env.clipboard.open_error = None

        for _ in range(10):
            monitor._check_clipboard()

        # Delivered exactly once across the recovery polls
        assert received == ["eventually consistent"]

    def test_a_handler_that_keeps_raising_does_not_reopen_every_poll(self, win_env, tmp_path):
        """
        The flip side: committing *after* dispatch would let one unreadable
        clipboard item re-take the global lock ~3x/second forever.
        """
        sent = []
        monitor = make_monitor(win_env, tmp_path, on_files_copied=sent.append)
        monitor._last_change_count = win_env.sequence.value
        win_env.clipboard.contents[CF_HDROP] = (str(tmp_path / "whatever.txt"),)
        win_env.clipboard.get_errors[CF_HDROP] = RuntimeError("delayed rendering failed")

        win_env.sequence.bump()
        for _ in range(5):
            monitor._check_clipboard()

        assert sent == []
        # One attempt for the one change, not one per poll
        assert win_env.clipboard.open_count == 1
        assert win_env.clipboard.close_count == 1

    def test_start_seeds_sequence_number_and_ignores_preexisting_content(self, win_env, tmp_path):
        sent = []
        win_env.clipboard.contents[CF_UNICODETEXT] = "copied before yank started"
        monitor = make_monitor(win_env, tmp_path, on_text_copied=sent.append, poll_interval=0.01)

        monitor.start()
        try:
            time.sleep(0.05)
        finally:
            monitor.stop()

        assert monitor._last_change_count == win_env.sequence.value
        # Content the user copied before the daemon existed is not broadcast,
        # and no poll took the global clipboard lock.
        assert sent == []
        assert win_env.clipboard.open_count == 0


class TestPngFormatRegistration:
    """Issue #13 (related): registration must never leak the clipboard lock."""

    def test_successful_registration_opens_and_closes(self, win_env, tmp_path):
        monitor = win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)

        assert monitor._cf_png == win_env.clipboard.png_format
        assert win_env.clipboard.open_count == 1
        assert win_env.clipboard.close_count == 1
        assert win_env.clipboard.is_open is False

    def test_registration_failure_still_closes_the_clipboard(self, win_env, tmp_path):
        win_env.clipboard.register_error = RuntimeError("RegisterClipboardFormat failed")

        monitor = win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)

        assert monitor._cf_png is None
        # The whole point: the lock is released even though registration threw
        assert win_env.clipboard.close_count == 1
        assert win_env.clipboard.is_open is False
        assert win_env.clipboard.calls == [
            "OpenClipboard",
            "RegisterClipboardFormat",
            "CloseClipboard",
        ]

    def test_registration_does_not_close_when_open_failed(self, win_env, tmp_path):
        win_env.clipboard.open_error = RuntimeError("clipboard busy")

        monitor = win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)

        assert monitor._cf_png is None
        assert win_env.clipboard.close_count == 0

    def test_keyboard_interrupt_is_not_swallowed(self, win_env, tmp_path):
        """The old bare `except:` also ate KeyboardInterrupt/SystemExit."""
        win_env.clipboard.register_error = KeyboardInterrupt()

        with pytest.raises(KeyboardInterrupt):
            win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)

        # ...and it still released the lock on the way out
        assert win_env.clipboard.close_count == 1
        assert win_env.clipboard.is_open is False

    def test_registration_is_per_instance_not_a_module_global(self, win_env, tmp_path):
        first = win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)
        win_env.clipboard.register_error = RuntimeError("nope")
        second = win_env.module.WindowsClipboardMonitor(temp_dir=tmp_path)

        assert first._cf_png == win_env.clipboard.png_format
        assert second._cf_png is None
        assert not hasattr(win_env.module, "CF_PNG")


class TestFileAndImageHashSplit:
    """Issue #16 (Windows half): a file-list hash must never gate an image."""

    def test_hashes_live_in_separate_fields(self, win_env, tmp_path):
        monitor = make_monitor(win_env, tmp_path)
        doc = tmp_path / "notes.txt"
        doc.write_text("hi")

        monitor.set_clipboard_files([doc])

        assert monitor._last_file_hash == monitor._hash_file_list([doc])
        assert monitor._last_image_hash is None
        assert not hasattr(monitor, "_last_clipboard_hash")

    def test_received_image_does_not_echo_back_when_sync_files_is_off(self, win_env, tmp_path):
        pytest.importorskip("PIL")
        sent = []
        monitor = make_monitor(
            win_env,
            tmp_path,
            on_files_copied=sent.append,
            sync_files=False,
            sync_images=True,
        )
        monitor._last_change_count = win_env.sequence.value
        image = make_png(tmp_path / "received.png")

        # Peer sends an image; we inject it into the clipboard
        monitor.set_clipboard_files([image])
        # The remembered hash is over the exact bytes the next poll will read back
        assert monitor._last_image_hash == monitor._hash_image_data(
            win_env.clipboard.contents[monitor._cf_png]
        )
        # Our own write bumps the sequence number, so the next poll does look
        win_env.sequence.bump()

        monitor._check_clipboard()

        assert sent == [], "received image was sent straight back to the peer"

    def test_received_image_does_not_echo_back_via_the_dib_fallback(self, win_env, tmp_path):
        """Same guarantee when the PNG format could not be registered."""
        pytest.importorskip("PIL")
        sent = []
        win_env.clipboard.register_error = RuntimeError("no PNG format")
        monitor = make_monitor(
            win_env,
            tmp_path,
            on_files_copied=sent.append,
            sync_files=False,
            sync_images=True,
        )
        assert monitor._cf_png is None
        monitor._last_change_count = win_env.sequence.value

        monitor.set_clipboard_files([make_png(tmp_path / "received.png")])
        win_env.sequence.bump()

        monitor._check_clipboard()

        # The image went out as CF_DIB only, so the recorded hash must be over the
        # DIB bytes rather than the PNG ones
        assert monitor._last_image_hash == monitor._hash_image_data(
            win_env.clipboard.contents[CF_DIB]
        )
        assert sent == []

    def test_a_genuinely_new_image_is_still_sent(self, win_env, tmp_path):
        pytest.importorskip("PIL")
        sent = []
        monitor = make_monitor(
            win_env,
            tmp_path,
            on_files_copied=sent.append,
            sync_files=False,
            sync_images=True,
        )
        monitor._last_change_count = win_env.sequence.value
        monitor.set_clipboard_files([make_png(tmp_path / "received.png")])

        # The user now copies a different image
        win_env.clipboard.contents[monitor._cf_png] = png_bytes()
        win_env.sequence.bump()

        monitor._check_clipboard()

        assert len(sent) == 1
        assert sent[0][0].suffix == ".png"
        assert sent[0][0].parent == monitor.temp_dir

    def test_received_files_do_not_echo_back(self, win_env, tmp_path):
        sent = []
        monitor = make_monitor(win_env, tmp_path, on_files_copied=sent.append)
        monitor._last_change_count = win_env.sequence.value
        doc = tmp_path / "notes.txt"
        doc.write_text("hi")

        monitor.set_clipboard_files([doc])
        # pywin32 hands back a tuple of paths for CF_HDROP, not the DROPFILES blob
        win_env.clipboard.contents[CF_HDROP] = (str(doc),)
        win_env.sequence.bump()

        monitor._check_clipboard()

        assert sent == []

    def test_new_files_are_sent(self, win_env, tmp_path):
        sent = []
        monitor = make_monitor(win_env, tmp_path, on_files_copied=sent.append)
        monitor._last_change_count = win_env.sequence.value
        doc = tmp_path / "notes.txt"
        doc.write_text("hi")

        win_env.clipboard.contents[CF_HDROP] = (str(doc),)
        win_env.sequence.bump()
        monitor._check_clipboard()

        assert sent == [[doc]]

    def test_injected_text_is_not_sent_back(self, win_env, tmp_path):
        sent = []
        monitor = make_monitor(win_env, tmp_path, on_text_copied=sent.append)
        monitor._last_change_count = win_env.sequence.value

        monitor.set_clipboard_text("from the peer")
        win_env.sequence.bump()
        monitor._check_clipboard()

        assert sent == []


class TestGetClipboardFiles:
    """Issue #17: the function must always hand back a list."""

    def test_returns_empty_list_when_hdrop_data_is_falsy(self, win_env):
        # CF_HDROP is advertised but holds nothing usable
        win_env.clipboard.contents[CF_HDROP] = ()

        result = win_env.module.get_clipboard_files()

        assert result == []
        assert isinstance(result, list)
        assert win_env.clipboard.close_count == 1
        assert win_env.clipboard.is_open is False

    def test_returns_empty_list_when_hdrop_is_absent(self, win_env):
        result = win_env.module.get_clipboard_files()

        assert result == []
        assert win_env.clipboard.close_count == 1

    def test_returns_only_existing_paths(self, win_env, tmp_path):
        existing = tmp_path / "there.txt"
        existing.write_text("x")
        win_env.clipboard.contents[CF_HDROP] = (str(existing), str(tmp_path / "gone.txt"))

        assert win_env.module.get_clipboard_files() == [existing]

    def test_returns_empty_list_when_the_clipboard_is_locked(self, win_env):
        win_env.clipboard.open_error = RuntimeError("clipboard busy")

        assert win_env.module.get_clipboard_files() == []
