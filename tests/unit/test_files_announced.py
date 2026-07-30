"""
Regression tests for the file-announcement path after the dead virtual-clipboard
code was removed (issue #9).

The virtual-clipboard modules imported a non-existent top-level package
(`from macos.virtual_clipboard import ...`), so the ImportError was swallowed and
the code never ran. It has been deleted rather than repaired, because the staged
filename was peer-supplied and unsanitized -- a peer could announce a file named
`../../pairing.json` and overwrite the shared AES key store.

These tests pin the resulting contract:
  * `_on_files_announced` ALWAYS spawns the background download thread.
  * No virtual-clipboard entry points survive on any monitor or module.
"""
import importlib.util
import sys
import tempfile
from dataclasses import fields
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Importing yank.main runs logging.basicConfig() with a FileHandler at module
# scope. Point that at a throwaway file first so importing this test module
# never opens (or appends to) the real shared log.
if "yank.main" not in sys.modules:
    from yank import config as _yank_config

    _yank_config.LOG_FILE = Path(tempfile.mkdtemp(prefix="yank_test_log_")) / "test.log"

from yank.common.protocol import FileInfo, TransferMetadata  # noqa: E402
from yank.main import ClipboardSync  # noqa: E402
from yank.platform.base import PlatformInfo  # noqa: E402


def make_metadata(count: int = 2) -> TransferMetadata:
    """Build announcement metadata without touching the filesystem."""
    files = [
        FileInfo(
            name=f"file{i}.bin",
            size=1024 * (i + 1),
            checksum=f"checksum{i}",
            file_index=i,
        )
        for i in range(count)
    ]
    return TransferMetadata(
        files=files,
        total_size=sum(f.size for f in files),
        timestamp=0.0,
        source_os="macos",
        transfer_id="abcdef1234567890",
    )


@pytest.fixture
def sync():
    """
    A ClipboardSync with no __init__ side effects.

    `_on_files_announced` only needs `_pending_transfer_id` and the bound
    `_download_announced_files`, so bypassing __init__ avoids loading user
    config, binding sockets, or starting an agent.
    """
    instance = ClipboardSync.__new__(ClipboardSync)
    instance.agent = MagicMock()
    instance.clipboard_monitor = MagicMock()
    instance._pending_transfer_id = None
    return instance


class TestOnFilesAnnouncedAlwaysDownloads:
    """_on_files_announced must always take the auto-download path."""

    @pytest.mark.parametrize("platform_name", ["Darwin", "Windows", "Linux"])
    def test_starts_download_thread_on_every_platform(self, sync, platform_name):
        metadata = make_metadata()

        with patch("yank.main.threading.Thread") as mock_thread:
            with patch("yank.main.PLATFORM", platform_name):
                sync._on_files_announced("abcdef1234567890", metadata)

        mock_thread.assert_called_once()
        kwargs = mock_thread.call_args.kwargs
        assert kwargs["target"] == sync._download_announced_files
        assert kwargs["args"] == ("abcdef1234567890", metadata)
        assert kwargs["daemon"] is True
        mock_thread.return_value.start.assert_called_once()

        # The clipboard monitor must not be asked to stage anything itself.
        assert sync.clipboard_monitor.mock_calls == []

    def test_records_pending_transfer_id(self, sync):
        metadata = make_metadata()

        with patch("yank.main.threading.Thread"):
            sync._on_files_announced("transfer-42", metadata)

        assert sync._pending_transfer_id == "transfer-42"

    def test_announcement_output_is_ascii_and_honest(self, sync, capsys):
        """
        Only the download path remains, so the banner must not promise the
        clipboard is ready to paste before the download has finished.
        """
        metadata = make_metadata(count=7)

        with patch("yank.main.threading.Thread"):
            sync._on_files_announced("abcdef1234567890", metadata)

        out = capsys.readouterr().out

        # Windows consoles are cp1252 - printed output must stay ASCII.
        out.encode("ascii")

        assert "Files announced" in out
        assert "Downloading files" in out
        assert "... +2 more" in out  # 7 files, only 5 listed
        assert "Ready to paste" not in out

    def test_download_thread_is_not_awaited(self, sync):
        """The announcement handler must return without joining the thread."""
        with patch("yank.main.threading.Thread") as mock_thread:
            sync._on_files_announced("abcdef1234567890", make_metadata())

        mock_thread.return_value.join.assert_not_called()


class TestVirtualClipboardRemoved:
    """No virtual-clipboard entry point may survive."""

    def test_clipboard_sync_has_no_try_set_virtual_clipboard(self):
        assert not hasattr(ClipboardSync, "_try_set_virtual_clipboard")

    def test_macos_monitor_has_no_virtual_clipboard_method(self):
        from yank.platform.macos.clipboard import MacClipboardMonitor

        assert not hasattr(MacClipboardMonitor, "set_virtual_clipboard_files")

    def test_windows_monitor_has_no_virtual_clipboard_method(self):
        from yank.platform.windows.clipboard import WindowsClipboardMonitor

        assert not hasattr(WindowsClipboardMonitor, "set_virtual_clipboard_files")

    @pytest.mark.parametrize(
        "module",
        [
            "yank.platform.macos.virtual_clipboard",
            "yank.platform.windows.virtual_clipboard",
        ],
    )
    def test_virtual_clipboard_modules_are_gone(self, module):
        assert importlib.util.find_spec(module) is None

    def test_platform_info_has_no_virtual_clipboard_flag(self):
        assert "supports_virtual_clipboard" not in {f.name for f in fields(PlatformInfo)}

    def test_platform_info_still_constructs(self):
        """The three call sites in platform/__init__.py must still be valid."""
        from yank.platform import get_platform_info

        info = get_platform_info()
        assert info.name in {"windows", "macos", "linux"}
        assert info.copy_shortcut and info.paste_shortcut
