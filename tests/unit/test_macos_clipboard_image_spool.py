"""
Regression tests for the macOS clipboard image spool filename.

These tests NEVER touch the real system pasteboard: ``NSPasteboard`` is patched
out for a ``FakePasteboard`` before the monitor is constructed, so
``NSPasteboard.generalPasteboard()`` never runs and nothing is written to the
user's clipboard.

``_handle_image()`` used to name its spool file from a second-resolution
timestamp, so two images copied inside the same second landed on one path and
the second silently overwrote the first. That matters for the lazy (>10 MB)
transfer path in ``main.py``: FILE_ANNOUNCE sends only metadata and the file is
read back from disk when the peer pulls it, so the peer could receive the wrong
image bytes - or bytes whose size and checksum no longer matched what was
announced.

The clock is frozen in these tests so "within the same second" is guaranteed
rather than left to chance.
"""
import sys
import hashlib
from datetime import datetime

import pytest

pytest.importorskip("AppKit", reason="pyobjc is required for the macOS clipboard monitor")

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS clipboard monitor is darwin-only"
)

from AppKit import (  # noqa: E402
    NSPasteboardTypePNG,
    NSPasteboardTypeTIFF,
)

from yank.platform.macos import clipboard as mac_clipboard  # noqa: E402
from yank.platform.macos.clipboard import MacClipboardMonitor  # noqa: E402


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakePasteboard:
    """In-memory stand-in for NSPasteboard.

    Only the reads ``_handle_image()`` performs are needed here; a "copy" is
    modelled as replacing the contents and bumping ``changeCount``.
    """

    def __init__(self):
        self._items = {}
        self._change_count = 0

    def changeCount(self):  # noqa: N802 - Cocoa naming
        return self._change_count

    def types(self):
        return list(self._items.keys())

    def dataForType_(self, type_):  # noqa: N802
        return self._items.get(type_)

    def propertyListForType_(self, type_):  # noqa: N802
        return self._items.get(type_)

    def stringForType_(self, type_):  # noqa: N802
        return self._items.get(type_)

    def pasteboardItems(self):  # noqa: N802
        return []

    def user_copies(self, **by_type):
        """Simulate some other app putting content on the pasteboard."""
        self._items.clear()
        self._items.update(by_type)
        self._change_count += 1


class FakeNSPasteboardClass:
    """Replaces the NSPasteboard *class* so generalPasteboard() is never called."""

    def __init__(self, board):
        self._board = board

    def generalPasteboard(self):  # noqa: N802
        return self._board


FROZEN_NOW = datetime(2026, 7, 30, 14, 30, 22)
FROZEN_STAMP = FROZEN_NOW.strftime("%Y%m%d_%H%M%S")


class FrozenDatetime:
    """Pins ``datetime.now()`` so every spool file lands in the same second."""

    @staticmethod
    def now():
        return FROZEN_NOW


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def pasteboard(monkeypatch):
    board = FakePasteboard()
    monkeypatch.setattr(mac_clipboard, "NSPasteboard", FakeNSPasteboardClass(board))
    return board


@pytest.fixture
def frozen_clock(monkeypatch):
    monkeypatch.setattr(mac_clipboard, "datetime", FrozenDatetime)


@pytest.fixture
def spool_dir(tmp_path):
    return tmp_path / "spool"


@pytest.fixture
def monitor(pasteboard, frozen_clock, spool_dir):
    """A monitor wired to the fake pasteboard, recording its callbacks."""
    saved = []
    mon = MacClipboardMonitor(
        on_files_copied=lambda paths: saved.append(list(paths)),
        temp_dir=spool_dir,
        sync_files=False,
        sync_images=True,
        sync_text=False,
    )
    assert mon._pasteboard is pasteboard
    mon.saved = saved
    return mon


def image_bytes(seed: bytes, size: int = 4096) -> bytes:
    """Deterministic pseudo-image payload (never decoded, only hashed/written)."""
    out = bytearray()
    chunk = seed
    while len(out) < size:
        chunk = hashlib.sha256(chunk).digest()
        out.extend(chunk)
    return bytes(out[:size])


IMAGE_A = image_bytes(b"a")
IMAGE_B = image_bytes(b"b", size=8192)  # deliberately a different size


def copy_png(pasteboard, monitor, payload):
    pasteboard.user_copies(**{NSPasteboardTypePNG: payload})
    monitor._check_clipboard()


# --------------------------------------------------------------------------
# The collision
# --------------------------------------------------------------------------


class TestSpoolFilesDoNotCollide:
    def test_two_images_in_one_second_get_distinct_paths(self, monitor, pasteboard):
        copy_png(pasteboard, monitor, IMAGE_A)
        copy_png(pasteboard, monitor, IMAGE_B)

        assert len(monitor.saved) == 2, "second image was not detected"
        [first] = monitor.saved[0]
        [second] = monitor.saved[1]

        assert first != second, "both images were spooled to the same path"

    def test_the_first_image_is_not_overwritten(self, monitor, pasteboard):
        copy_png(pasteboard, monitor, IMAGE_A)
        [first] = monitor.saved[0]
        assert first.read_bytes() == IMAGE_A

        copy_png(pasteboard, monitor, IMAGE_B)
        [second] = monitor.saved[1]

        # The whole point: announcing the first image and pulling it later must
        # still yield the first image.
        assert first.read_bytes() == IMAGE_A, "second copy clobbered the first image"
        assert second.read_bytes() == IMAGE_B

    def test_announced_size_and_checksum_stay_valid(self, monitor, pasteboard):
        """What the lazy transfer path actually depends on.

        FILE_ANNOUNCE captures size/checksum up front; the bytes are read from
        disk later. Both must still describe the file when the peer pulls it.
        """
        copy_png(pasteboard, monitor, IMAGE_A)
        [announced] = monitor.saved[0]

        announced_size = announced.stat().st_size
        announced_checksum = hashlib.sha256(announced.read_bytes()).hexdigest()

        copy_png(pasteboard, monitor, IMAGE_B)

        assert announced.stat().st_size == announced_size
        assert hashlib.sha256(announced.read_bytes()).hexdigest() == announced_checksum
        assert announced_size == len(IMAGE_A)

    def test_a_burst_of_images_all_survive(self, monitor, pasteboard, spool_dir):
        payloads = [image_bytes(f"burst-{i}".encode()) for i in range(6)]

        for payload in payloads:
            copy_png(pasteboard, monitor, payload)

        assert len(monitor.saved) == len(payloads)

        paths = [entry[0] for entry in monitor.saved]
        assert len({str(p) for p in paths}) == len(payloads), "spool paths collided"

        for path, payload in zip(paths, payloads):
            assert path.read_bytes() == payload

        # Nothing was lost or left behind in the spool directory either.
        assert sorted(p.name for p in spool_dir.iterdir()) == sorted(p.name for p in paths)

    def test_tiff_fallback_writes_also_get_distinct_paths(self, monitor, pasteboard):
        """The TIFF branch writes through a different code path to the same name."""
        pasteboard.user_copies(**{NSPasteboardTypeTIFF: IMAGE_A})
        monitor._check_clipboard()
        pasteboard.user_copies(**{NSPasteboardTypeTIFF: IMAGE_B})
        monitor._check_clipboard()

        assert len(monitor.saved) == 2
        [first] = monitor.saved[0]
        [second] = monitor.saved[1]
        assert first != second
        assert first.exists() and second.exists()


# --------------------------------------------------------------------------
# ...without breaking how the file is named or where it goes
# --------------------------------------------------------------------------


class TestSpoolNaming:
    def test_name_keeps_its_prefix_timestamp_and_extension(
        self, monitor, pasteboard, spool_dir
    ):
        copy_png(pasteboard, monitor, IMAGE_A)
        [path] = monitor.saved[0]

        assert path.parent == spool_dir
        assert path.name.startswith(f"clipboard_image_{FROZEN_STAMP}_")
        assert path.suffix == ".png"

    def test_unique_part_is_what_differs(self, monitor, pasteboard):
        copy_png(pasteboard, monitor, IMAGE_A)
        copy_png(pasteboard, monitor, IMAGE_B)

        [first] = monitor.saved[0]
        [second] = monitor.saved[1]

        prefix = f"clipboard_image_{FROZEN_STAMP}_"
        assert first.name.startswith(prefix)
        assert second.name.startswith(prefix)
        # Same second, same prefix - only the unique suffix separates them.
        assert first.stem != second.stem
