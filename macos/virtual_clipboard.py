"""
macOS Virtual Clipboard - On-demand file transfer.

Unlike Windows, macOS Finder requires actual files on disk for copy/paste.
NSFilePromiseProvider only works for drag-and-drop, not clipboard paste.

This implementation uses a hybrid approach:
1. Creates lightweight placeholder files in a staging directory
2. Puts file URLs on pasteboard
3. When paste occurs, files are already there (downloaded in background)

For true on-demand (like iCloud), macOS would require a FUSE filesystem
or file provider extension, which is beyond the scope of this implementation.

Current behavior:
- Files are auto-downloaded when announced
- Placeholder approach for fast perceived response
"""
import os
import time
import logging
import threading
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Try to import macOS-specific modules
try:
    from AppKit import NSPasteboard, NSPasteboardTypeFileURL, NSURL, NSArray
    from Foundation import NSObject
    import objc
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False
    logger.warning("AppKit not available - virtual clipboard disabled")


@dataclass
class PendingFile:
    """Represents a file pending download"""
    name: str
    size: int
    checksum: str
    file_index: int
    transfer_id: str
    placeholder_path: Optional[Path] = None
    downloaded: bool = False


class MacVirtualClipboard:
    """
    Manages virtual files for macOS clipboard.

    Since Finder requires real files for paste, this class:
    1. Creates a staging directory for pending files
    2. Starts background downloads immediately
    3. Creates placeholder files that get replaced with real data
    """

    def __init__(self, staging_dir: Path = None):
        self.staging_dir = staging_dir or Path.home() / ".clipboard-sync" / "staging"
        self.staging_dir.mkdir(parents=True, exist_ok=True)

        self._pending_files: Dict[str, List[PendingFile]] = {}  # transfer_id -> files
        self._download_threads: Dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def set_virtual_files(
        self,
        files: List[Dict[str, Any]],
        transfer_id: str,
        download_callback: Callable[[str, int], Optional[bytes]]
    ) -> bool:
        """
        Set virtual files on the clipboard.

        This creates placeholder files and starts background downloads.

        Args:
            files: List of file info dicts
            transfer_id: Transfer ID for downloading
            download_callback: Function to get file data

        Returns:
            True if clipboard was set successfully
        """
        if not HAS_APPKIT:
            logger.error("AppKit not available")
            return False

        try:
            # Create staging subdirectory for this transfer
            transfer_dir = self.staging_dir / transfer_id[:8]
            transfer_dir.mkdir(parents=True, exist_ok=True)

            pending = []
            file_urls = []

            for f in files:
                # Create placeholder file
                placeholder_path = transfer_dir / f['name']

                # Create empty placeholder (or small file indicating pending)
                with open(placeholder_path, 'wb') as fp:
                    # Write minimal placeholder content
                    fp.write(b'')

                pf = PendingFile(
                    name=f['name'],
                    size=f['size'],
                    checksum=f['checksum'],
                    file_index=f['file_index'],
                    transfer_id=transfer_id,
                    placeholder_path=placeholder_path,
                    downloaded=False
                )
                pending.append(pf)
                file_urls.append(placeholder_path)

            # Store pending files
            with self._lock:
                self._pending_files[transfer_id] = pending

            # Set clipboard with placeholder URLs
            self._set_clipboard_files(file_urls)

            # Start background download
            download_thread = threading.Thread(
                target=self._download_files,
                args=(transfer_id, download_callback),
                daemon=True
            )
            download_thread.start()

            with self._lock:
                self._download_threads[transfer_id] = download_thread

            logger.info(f"Set {len(files)} virtual files on clipboard (downloading in background)")
            return True

        except Exception as e:
            logger.error(f"Failed to set virtual clipboard: {e}")
            return False

    def _set_clipboard_files(self, file_paths: List[Path]):
        """Set file URLs on the pasteboard"""
        pasteboard = NSPasteboard.generalPasteboard()
        pasteboard.clearContents()

        urls = []
        for path in file_paths:
            url = NSURL.fileURLWithPath_(str(path))
            urls.append(url)

        # Set as file URLs
        pasteboard.writeObjects_(NSArray.arrayWithArray_(urls))

    def _download_files(self, transfer_id: str, download_callback: Callable):
        """Download files in background and replace placeholders"""
        with self._lock:
            pending = self._pending_files.get(transfer_id, [])

        for pf in pending:
            try:
                logger.info(f"Downloading: {pf.name}")

                # Download file content
                data = download_callback(pf.transfer_id, pf.file_index)

                if data and pf.placeholder_path:
                    # Replace placeholder with actual content
                    with open(pf.placeholder_path, 'wb') as fp:
                        fp.write(data)

                    pf.downloaded = True
                    logger.info(f"Downloaded: {pf.name} ({len(data)} bytes)")
                else:
                    logger.error(f"Failed to download: {pf.name}")

            except Exception as e:
                logger.error(f"Download error for {pf.name}: {e}")

        # Update clipboard if all downloads complete
        with self._lock:
            if all(pf.downloaded for pf in pending):
                # Re-set clipboard with completed files
                completed_paths = [pf.placeholder_path for pf in pending if pf.placeholder_path]
                if completed_paths:
                    self._set_clipboard_files(completed_paths)
                    logger.info(f"All {len(completed_paths)} files downloaded and ready")

    def cleanup_old_transfers(self, max_age_hours: int = 24):
        """Clean up old staging directories"""
        try:
            now = time.time()
            max_age_seconds = max_age_hours * 3600

            for entry in self.staging_dir.iterdir():
                if entry.is_dir():
                    # Check age
                    age = now - entry.stat().st_mtime
                    if age > max_age_seconds:
                        # Remove old directory
                        import shutil
                        shutil.rmtree(entry, ignore_errors=True)
                        logger.info(f"Cleaned up old staging: {entry.name}")

        except Exception as e:
            logger.error(f"Cleanup error: {e}")

    def get_pending_status(self, transfer_id: str) -> Optional[Dict]:
        """Get status of pending downloads"""
        with self._lock:
            pending = self._pending_files.get(transfer_id)
            if not pending:
                return None

            downloaded = sum(1 for pf in pending if pf.downloaded)
            total = len(pending)

            return {
                'total': total,
                'downloaded': downloaded,
                'complete': downloaded == total,
                'files': [pf.name for pf in pending]
            }


# Global instance
_virtual_clipboard: Optional[MacVirtualClipboard] = None


def get_virtual_clipboard() -> MacVirtualClipboard:
    """Get or create the virtual clipboard manager"""
    global _virtual_clipboard
    if _virtual_clipboard is None:
        _virtual_clipboard = MacVirtualClipboard()
    return _virtual_clipboard


def set_virtual_clipboard(
    files: List[Dict[str, Any]],
    transfer_id: str,
    download_callback: Callable[[str, int], Optional[bytes]]
) -> bool:
    """
    Set virtual files on the macOS clipboard.

    Args:
        files: List of file info dicts with 'name', 'size', 'checksum', 'file_index'
        transfer_id: The transfer ID for downloading
        download_callback: Function to call when file content is needed

    Returns:
        True if successful
    """
    return get_virtual_clipboard().set_virtual_files(files, transfer_id, download_callback)
