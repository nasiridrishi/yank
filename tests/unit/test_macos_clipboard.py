"""
Unit tests for the macOS clipboard monitor.

These tests NEVER touch the real system pasteboard: ``NSPasteboard`` is patched
out for a ``FakePasteboard`` before the monitor is constructed, so
``NSPasteboard.generalPasteboard()`` never runs and nothing is written to the
user's clipboard.

The main thing under test is loop prevention. ``MacClipboardMonitor`` keeps a
"last seen" hash so that content it injected into the pasteboard (because a peer
sent it) is not immediately detected as a fresh user copy and sent straight back.
That hash used to be a single field shared by the file-list path and the
image-data path, which meant an injected image file stored a file-list hash and
the next poll - taking the image branch - compared image bytes against it, found
no match, and echoed the image back to the peer.
"""
import sys
import hashlib
import threading

import pytest

pytest.importorskip("AppKit", reason="pyobjc is required for the macOS clipboard monitor")

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS clipboard monitor is darwin-only"
)

from AppKit import (  # noqa: E402
    NSFilenamesPboardType,
    NSPasteboardTypePNG,
    NSPasteboardTypeString,
    NSPasteboardTypeTIFF,
)

from yank.platform.macos import clipboard as mac_clipboard  # noqa: E402
from yank.platform.macos.clipboard import MacClipboardMonitor  # noqa: E402


# --------------------------------------------------------------------------
# Fake pasteboard
# --------------------------------------------------------------------------


class FakePasteboard:
    """In-memory stand-in for NSPasteboard.

    Mirrors the parts of the real semantics the monitor depends on: only
    ``clearContents`` bumps ``changeCount``, and reads return whatever the last
    write stored under that type.
    """

    def __init__(self):
        self._items = {}
        self._change_count = 0
        # Optional callable invoked with the name of each mutating method, used
        # to pause a write mid-flight in the concurrency test.
        self.on_write = None

    # -- reads --------------------------------------------------------------

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

    # -- writes -------------------------------------------------------------

    def _hook(self, name):
        if self.on_write is not None:
            self.on_write(name)

    def clearContents(self):  # noqa: N802
        self._hook("clearContents")
        self._items.clear()
        self._change_count += 1
        return self._change_count

    def setData_forType_(self, data, type_):  # noqa: N802
        self._hook("setData_forType_")
        self._items[type_] = bytes(data)
        return True

    def setPropertyList_forType_(self, plist, type_):  # noqa: N802
        self._hook("setPropertyList_forType_")
        self._items[type_] = [str(p) for p in plist]
        return True

    def setString_forType_(self, string, type_):  # noqa: N802
        self._hook("setString_forType_")
        self._items[type_] = str(string)
        return True

    # -- test helpers -------------------------------------------------------

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


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


@pytest.fixture
def pasteboard(monkeypatch):
    board = FakePasteboard()
    monkeypatch.setattr(mac_clipboard, "NSPasteboard", FakeNSPasteboardClass(board))
    return board


@pytest.fixture
def make_monitor(pasteboard, tmp_path):
    """Build a monitor wired to the fake pasteboard, plus its callback records."""
    created = {}

    def factory(**kwargs):
        kwargs.setdefault("sync_text", True)
        kwargs.setdefault("sync_files", True)
        kwargs.setdefault("sync_images", True)
        monitor = MacClipboardMonitor(
            on_files_copied=lambda paths: created["files"].append(list(paths)),
            on_text_copied=lambda text: created["text"].append(text),
            temp_dir=tmp_path / "spool",
            **kwargs,
        )
        assert monitor._pasteboard is pasteboard
        return monitor

    created["files"] = []
    created["text"] = []
    factory.files_copied = created["files"]
    factory.text_copied = created["text"]
    return factory


def image_bytes(seed: bytes, size: int = 256) -> bytes:
    """Deterministic pseudo-image payload (never decoded, only hashed/written)."""
    out = bytearray()
    chunk = seed
    while len(out) < size:
        chunk = hashlib.sha256(chunk).digest()
        out.extend(chunk)
    return bytes(out[:size])


IMAGE_A = image_bytes(b"a")
IMAGE_B = image_bytes(b"b")


# --------------------------------------------------------------------------
# The echo-loop regression
# --------------------------------------------------------------------------


class TestInjectedImageDoesNotEcho:
    """Regression: a received image must never be sent straight back."""

    def test_injected_image_file_is_not_echoed_when_only_images_sync(
        self, make_monitor, pasteboard, tmp_path
    ):
        # sync_files=False + sync_images=True is the exact configuration that
        # exposed the bug: set_clipboard_files() stored a FILE-LIST hash, but
        # the next poll skipped the file branch and hashed the image DATA.
        monitor = make_monitor(sync_files=False, sync_images=True)

        received = tmp_path / "from_peer.png"
        received.write_bytes(IMAGE_A)

        count_before = monitor._last_change_count
        monitor.set_clipboard_files([received])

        # The change-count guard alone would short-circuit the poll, so defeat
        # it deliberately: this simulates the poller having sampled the
        # pasteboard before set_clipboard_files() recorded the new count (the
        # race the injection lock closes), and also covers macOS bumping the
        # change count again after our write. The per-kind content hash is the
        # guard that has to catch this case.
        monitor._last_change_count = count_before

        monitor._check_clipboard()

        assert make_monitor.files_copied == [], "received image was echoed back to the peer"

    def test_injected_image_file_is_not_echoed_when_files_also_sync(
        self, make_monitor, tmp_path
    ):
        monitor = make_monitor(sync_files=True, sync_images=True)

        received = tmp_path / "from_peer.png"
        received.write_bytes(IMAGE_A)

        count_before = monitor._last_change_count
        monitor.set_clipboard_files([received])
        monitor._last_change_count = count_before

        monitor._check_clipboard()

        assert make_monitor.files_copied == []

    def test_injection_arms_both_the_file_and_image_guards(self, make_monitor, tmp_path):
        monitor = make_monitor()

        received = tmp_path / "from_peer.png"
        received.write_bytes(IMAGE_A)

        monitor.set_clipboard_files([received])

        # An image injection puts BOTH a filename and image data on the
        # pasteboard, so both guards must be armed - not just one of them.
        assert monitor._last_file_hash == monitor._hash_file_list([received])
        assert monitor._last_image_hash == hashlib.md5(IMAGE_A).hexdigest()

    def test_injected_non_png_image_is_not_echoed(self, make_monitor, pasteboard, tmp_path):
        """A JPEG is converted to PNG on the way to the pasteboard.

        The image guard therefore has to be armed with the hash of the
        *converted* bytes, not the bytes of the file on disk.
        """
        PIL_Image = pytest.importorskip("PIL.Image")

        monitor = make_monitor(sync_files=False, sync_images=True)

        received = tmp_path / "from_peer.jpg"
        PIL_Image.new("RGB", (8, 8), (10, 120, 200)).save(received, format="JPEG")

        count_before = monitor._last_change_count
        monitor.set_clipboard_files([received])

        on_board = pasteboard.dataForType_(NSPasteboardTypePNG)
        if on_board is None:
            pytest.skip("NSImage conversion unavailable in this environment")

        # Guard is armed from the converted PNG, which differs from the file.
        assert monitor._last_image_hash == hashlib.md5(on_board).hexdigest()
        assert monitor._last_image_hash != hashlib.md5(received.read_bytes()).hexdigest()

        monitor._last_change_count = count_before
        monitor._check_clipboard()

        assert make_monitor.files_copied == []

    def test_injected_plain_files_are_not_echoed(self, make_monitor, tmp_path):
        monitor = make_monitor()

        a = tmp_path / "a.txt"
        a.write_text("hello")
        b = tmp_path / "b.txt"
        b.write_text("world")

        count_before = monitor._last_change_count
        monitor.set_clipboard_files([a, b])
        monitor._last_change_count = count_before

        monitor._check_clipboard()

        assert make_monitor.files_copied == []

    def test_injected_text_is_not_echoed(self, make_monitor):
        monitor = make_monitor()

        count_before = monitor._last_change_count
        monitor.set_clipboard_text("from the peer")
        monitor._last_change_count = count_before

        monitor._check_clipboard()

        assert make_monitor.text_copied == []


# --------------------------------------------------------------------------
# ...but real copies must still be detected
# --------------------------------------------------------------------------


class TestGenuineCopiesStillFire:
    """A fix that suppresses everything is not a fix."""

    def test_new_image_fires_the_callback(self, make_monitor, pasteboard):
        monitor = make_monitor(sync_files=False, sync_images=True)

        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_A})
        monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 1
        [saved] = make_monitor.files_copied[0]
        assert saved.read_bytes() == IMAGE_A

    def test_new_image_fires_even_right_after_an_injection(
        self, make_monitor, pasteboard, tmp_path
    ):
        monitor = make_monitor(sync_files=False, sync_images=True)

        received = tmp_path / "from_peer.png"
        received.write_bytes(IMAGE_A)
        monitor.set_clipboard_files([received])

        # The user now copies a genuinely different image.
        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_B})
        monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 1
        [saved] = make_monitor.files_copied[0]
        assert saved.read_bytes() == IMAGE_B

    def test_tiff_only_image_fires_the_callback(self, make_monitor, pasteboard):
        monitor = make_monitor(sync_files=False, sync_images=True)

        pasteboard.user_copies(**{NSPasteboardTypeTIFF: IMAGE_A})
        monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 1

    def test_new_files_fire_the_callback(self, make_monitor, pasteboard, tmp_path):
        monitor = make_monitor()

        target = tmp_path / "doc.txt"
        target.write_text("x")
        pasteboard.user_copies(**{NSFilenamesPboardType: [str(target)]})
        monitor._check_clipboard()

        assert make_monitor.files_copied == [[target]]

    def test_new_text_fires_the_callback(self, make_monitor, pasteboard):
        monitor = make_monitor()

        pasteboard.user_copies(**{NSPasteboardTypeString: "typed by the user"})
        monitor._check_clipboard()

        assert make_monitor.text_copied == ["typed by the user"]

    def test_a_file_copy_does_not_unblock_an_image_and_vice_versa(
        self, make_monitor, pasteboard, tmp_path
    ):
        """The two hashes are independent - neither clobbers the other."""
        monitor = make_monitor()

        target = tmp_path / "doc.txt"
        target.write_text("x")

        pasteboard.user_copies(**{NSPasteboardTypeString: "text"})
        monitor._check_clipboard()

        pasteboard.user_copies(**{NSFilenamesPboardType: [str(target)]})
        monitor._check_clipboard()
        file_hash_after = monitor._last_file_hash

        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_A})
        monitor._check_clipboard()

        # Handling the image left the file hash intact...
        assert monitor._last_file_hash == file_hash_after
        # ...and produced a distinct image hash.
        assert monitor._last_image_hash == hashlib.md5(IMAGE_A).hexdigest()
        assert monitor._last_image_hash != monitor._last_file_hash
        assert len(make_monitor.files_copied) == 2


# --------------------------------------------------------------------------
# De-duplication of repeats
# --------------------------------------------------------------------------


class TestDeduplication:
    def test_same_image_seen_twice_fires_once(self, make_monitor, pasteboard):
        monitor = make_monitor(sync_files=False, sync_images=True)

        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_A})
        monitor._check_clipboard()
        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_A})
        monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 1

    def test_same_file_list_seen_twice_fires_once(self, make_monitor, pasteboard, tmp_path):
        monitor = make_monitor()

        target = tmp_path / "doc.txt"
        target.write_text("x")

        for _ in range(2):
            pasteboard.user_copies(**{NSFilenamesPboardType: [str(target)]})
            monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 1

    def test_images_sharing_a_4kb_prefix_are_treated_as_different(
        self, make_monitor, pasteboard
    ):
        """The hash used to cover only the first 4096 bytes."""
        monitor = make_monitor(sync_files=False, sync_images=True)

        prefix = image_bytes(b"shared-prefix", 8192)
        first = prefix + b"\x00" * 64
        second = prefix + b"\xff" * 64

        pasteboard.user_copies(**{NSPasteboardTypePNG: first})
        monitor._check_clipboard()
        pasteboard.user_copies(**{NSPasteboardTypePNG: second})
        monitor._check_clipboard()

        assert len(make_monitor.files_copied) == 2, "second image was hidden by a prefix-only hash"
        # Note: both spool files can share a name (the temp filename only has
        # second resolution), so assert on the payload of the latest write.
        assert make_monitor.files_copied[1][0].read_bytes() == second
        assert monitor._last_image_hash == hashlib.md5(second).hexdigest()


# --------------------------------------------------------------------------
# The write / changeCount race
# --------------------------------------------------------------------------


class TestInjectionIsAtomicAgainstPolling:
    def test_poll_cannot_interleave_with_an_in_flight_injection(
        self, make_monitor, pasteboard, tmp_path
    ):
        """The monitor thread must not observe a half-finished injection.

        ``clearContents()`` bumps ``changeCount`` immediately, but
        ``_last_change_count`` is only updated once the write completes. A poll
        landing in that window used to see the injected content as a fresh user
        copy; it now blocks on the injection lock instead.
        """
        monitor = make_monitor(sync_files=False, sync_images=True)

        received = tmp_path / "from_peer.png"
        received.write_bytes(IMAGE_A)

        write_in_flight = threading.Event()
        release_write = threading.Event()
        poll_finished = threading.Event()

        def pause_on_final_write(name):
            # setPropertyList_forType_ is the last write of an image injection:
            # by now the pasteboard holds the new content and a bumped
            # changeCount, but _last_change_count is still stale.
            if name == "setPropertyList_forType_":
                write_in_flight.set()
                release_write.wait(5)

        pasteboard.on_write = pause_on_final_write

        writer = threading.Thread(target=monitor.set_clipboard_files, args=([received],))
        writer.start()
        assert write_in_flight.wait(5), "injection never started"

        def poll():
            monitor._check_clipboard()
            poll_finished.set()

        poller = threading.Thread(target=poll)
        poller.start()
        try:
            assert not poll_finished.wait(0.5), "poll ran while the injection was mid-write"
        finally:
            release_write.set()
            writer.join(5)
            poller.join(5)

        assert poll_finished.is_set()
        assert make_monitor.files_copied == []
        assert monitor._last_change_count == pasteboard.changeCount()

    def test_setters_leave_change_count_bookkeeping_consistent(
        self, make_monitor, pasteboard, tmp_path
    ):
        monitor = make_monitor()

        target = tmp_path / "doc.txt"
        target.write_text("x")

        monitor.set_clipboard_files([target])
        assert monitor._last_change_count == pasteboard.changeCount()

        monitor.set_clipboard_text("hello")
        assert monitor._last_change_count == pasteboard.changeCount()

        image = tmp_path / "img.png"
        image.write_bytes(IMAGE_A)
        monitor.set_clipboard_files([image])
        assert monitor._last_change_count == pasteboard.changeCount()

        # And a poll right afterwards is a no-op.
        monitor._check_clipboard()
        assert make_monitor.files_copied == []
        assert make_monitor.text_copied == []


# --------------------------------------------------------------------------
# Feature toggles
# --------------------------------------------------------------------------


class TestFeatureToggles:
    def test_images_ignored_when_disabled(self, make_monitor, pasteboard):
        monitor = make_monitor(sync_images=False)

        pasteboard.user_copies(**{NSPasteboardTypePNG: IMAGE_A})
        monitor._check_clipboard()

        assert make_monitor.files_copied == []

    def test_files_ignored_when_disabled(self, make_monitor, pasteboard, tmp_path):
        monitor = make_monitor(sync_files=False)

        target = tmp_path / "doc.txt"
        target.write_text("x")
        pasteboard.user_copies(**{NSFilenamesPboardType: [str(target)]})
        monitor._check_clipboard()

        assert make_monitor.files_copied == []

    def test_text_ignored_when_disabled(self, make_monitor, pasteboard):
        monitor = make_monitor(sync_text=False)

        pasteboard.user_copies(**{NSPasteboardTypeString: "hi"})
        monitor._check_clipboard()

        assert make_monitor.text_copied == []
