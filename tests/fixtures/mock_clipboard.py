"""
Mock clipboard for cross-platform testing

Provides a fake clipboard implementation that can be used in tests
without requiring actual clipboard access.
"""
from pathlib import Path
from typing import Optional, Callable, List


class MockClipboardMonitor:
    """
    Mock clipboard monitor for testing.

    Simulates clipboard operations without requiring actual system clipboard access.
    """

    def __init__(
        self,
        on_files_copied: Optional[Callable[[List[Path]], None]] = None,
        on_text_copied: Optional[Callable[[str], None]] = None,
        on_image_copied: Optional[Callable[[bytes], None]] = None,
    ):
        self.on_files_copied = on_files_copied
        self.on_text_copied = on_text_copied
        self.on_image_copied = on_image_copied

        self._running = False
        self._clipboard_text: Optional[str] = None
        self._clipboard_files: Optional[List[Path]] = None
        self._clipboard_image: Optional[bytes] = None

    def start(self):
        """Start the mock monitor"""
        self._running = True

    def stop(self):
        """Stop the mock monitor"""
        self._running = False

    def set_clipboard_text(self, text: str) -> bool:
        """Set text to clipboard"""
        self._clipboard_text = text
        self._clipboard_files = None
        self._clipboard_image = None
        return True

    def set_clipboard_files(self, files: List[Path]) -> bool:
        """Set files to clipboard"""
        self._clipboard_files = files
        self._clipboard_text = None
        self._clipboard_image = None
        return True

    def set_clipboard_image(self, image_data: bytes) -> bool:
        """Set image to clipboard"""
        self._clipboard_image = image_data
        self._clipboard_text = None
        self._clipboard_files = None
        return True

    def get_clipboard_text(self) -> Optional[str]:
        """Get text from clipboard"""
        return self._clipboard_text

    def get_clipboard_files(self) -> Optional[List[Path]]:
        """Get files from clipboard"""
        return self._clipboard_files

    def get_clipboard_image(self) -> Optional[bytes]:
        """Get image from clipboard"""
        return self._clipboard_image

    # Simulation methods for testing

    def simulate_text_copy(self, text: str):
        """Simulate user copying text"""
        self._clipboard_text = text
        if self.on_text_copied:
            self.on_text_copied(text)

    def simulate_file_copy(self, files: List[Path]):
        """Simulate user copying files"""
        self._clipboard_files = files
        if self.on_files_copied:
            self.on_files_copied(files)

    def simulate_image_copy(self, image_data: bytes):
        """Simulate user copying an image"""
        self._clipboard_image = image_data
        if self.on_image_copied:
            self.on_image_copied(image_data)
