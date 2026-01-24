"""
Linux Clipboard Monitor using GTK3

Works on both X11 and Wayland display servers
"""
import hashlib
import logging
import time
import threading
from pathlib import Path
from typing import Optional, Callable, List
from urllib.parse import urlparse, unquote

try:
    import gi
    gi.require_version('Gtk', '3.0')
    from gi.repository import Gtk, Gdk, GLib, GdkPixbuf
    HAS_GTK = True
except (ImportError, ValueError) as e:
    HAS_GTK = False
    print(f"GTK3 not available: {e}")
    print("Install: sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0")

from yank import config

logger = logging.getLogger(__name__)


class LinuxClipboardMonitor:
    """
    Monitor Linux clipboard using GTK3

    Supports:
    - Files (via text/uri-list)
    - Text
    - Images (via GdkPixbuf)
    """

    def __init__(
        self,
        on_files_copied: Optional[Callable[[List[Path]], None]] = None,
        on_text_copied: Optional[Callable[[str], None]] = None,
        on_image_copied: Optional[Callable[[bytes], None]] = None,
        poll_interval: float = 0.3,
        enable_images: bool = True
    ):
        if not HAS_GTK:
            raise RuntimeError("GTK3 not available. Cannot monitor clipboard.")

        self.on_files_copied = on_files_copied
        self.on_text_copied = on_text_copied
        self.on_image_copied = on_image_copied
        self.poll_interval = poll_interval
        self.enable_images = enable_images

        self._running = False
        self._thread = None
        self._last_content_hash = None

        # GTK clipboard
        display = Gdk.Display.get_default()
        if not display:
            raise RuntimeError("No display found. Make sure DISPLAY is set.")

        self._clipboard = Gtk.Clipboard.get_default(display)

        logger.info("Linux clipboard monitor initialized (GTK3)")

    def start(self):
        """Start monitoring clipboard"""
        if self._running:
            logger.warning("Clipboard monitor already running")
            return

        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

        logger.info("Linux clipboard monitor started (files + images)")

    def stop(self):
        """Stop monitoring clipboard"""
        if not self._running:
            return

        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

        logger.info("Linux clipboard monitor stopped")

    def _monitor_loop(self):
        """Main monitoring loop using GTK main context"""
        GLib.timeout_add(int(self.poll_interval * 1000), self._check_clipboard_timeout)

        context = GLib.MainContext.default()
        while self._running:
            # Use non-blocking iteration to respect poll_interval contract
            had_events = context.iteration(False)
            if not had_events:
                time.sleep(0.05)  # Short sleep when no events to avoid busy-waiting

    def _check_clipboard_timeout(self):
        """Called periodically by GTK timeout"""
        if not self._running:
            return False

        try:
            self._check_clipboard()
        except Exception as e:
            logger.error(f"Error checking clipboard: {e}")

        return True

    def _check_clipboard(self):
        """Check clipboard for changes"""
        # Check for files first
        if self._check_files():
            return

        # Check for images
        if self.enable_images and self._check_images():
            return

        # Check for text
        self._check_text()

    def _check_files(self) -> bool:
        """Check for files in clipboard"""
        if not self.on_files_copied:
            return False

        # GTK uses text/uri-list for files
        uris = self._clipboard.wait_for_uris()
        if not uris:
            return False

        # Convert file:// URIs to paths
        file_paths = []
        for uri in uris:
            try:
                parsed = urlparse(uri)
                if parsed.scheme == 'file':
                    path = unquote(parsed.path)
                    file_paths.append(Path(path))
            except Exception as e:
                logger.warning(f"Failed to parse URI {uri}: {e}")

        if not file_paths:
            return False

        # Check if this is new content (use MD5 for deterministic hashing across sessions)
        content = '|'.join(sorted(str(p) for p in file_paths))
        content_hash = hashlib.md5(content.encode()).hexdigest()
        if content_hash == self._last_content_hash:
            return False

        self._last_content_hash = content_hash

        logger.info(f"Detected {len(file_paths)} file(s) copied to clipboard")
        self.on_files_copied(file_paths)
        return True

    def _check_images(self) -> bool:
        """Check for images in clipboard"""
        if not self.on_image_copied:
            return False

        pixbuf = self._clipboard.wait_for_image()
        if not pixbuf:
            return False

        # Convert pixbuf to PNG bytes
        success, png_bytes = pixbuf.save_to_bufferv('png', [], [])
        if not success:
            logger.warning("Failed to convert image to PNG")
            return False

        # Check if this is new content (use MD5 for deterministic hashing)
        sample = png_bytes[:1024] if len(png_bytes) > 1024 else png_bytes
        content_hash = hashlib.md5(sample).hexdigest()
        if content_hash == self._last_content_hash:
            return False

        self._last_content_hash = content_hash

        logger.info(f"Detected image in clipboard ({len(png_bytes)} bytes)")
        self.on_image_copied(png_bytes)
        return True

    def _check_text(self) -> bool:
        """Check for text in clipboard"""
        if not self.on_text_copied:
            return False

        text = self._clipboard.wait_for_text()
        if not text or not text.strip():
            return False

        # Check if this is new content (use MD5 for deterministic hashing)
        content_hash = hashlib.md5(text.encode()).hexdigest()
        if content_hash == self._last_content_hash:
            return False

        self._last_content_hash = content_hash

        logger.info(f"Detected text in clipboard ({len(text)} chars)")
        self.on_text_copied(text)
        return True

    def set_clipboard_files(self, file_paths: List[Path]) -> bool:
        """
        Set files to clipboard

        Args:
            file_paths: List of file paths to set

        Returns:
            True if successful
        """
        try:
            # Convert paths to file:// URIs
            uris = [f"file://{path.absolute()}" for path in file_paths]

            # GTK clipboard expects SelectionData
            self._clipboard.set_text('\n'.join(uris), -1)
            self._clipboard.store()

            logger.info(f"Set {len(file_paths)} file(s) to clipboard")
            return True
        except Exception as e:
            logger.error(f"Failed to set clipboard files: {e}")
            return False

    def set_clipboard_text(self, text: str) -> bool:
        """
        Set text to clipboard

        Args:
            text: Text to set

        Returns:
            True if successful
        """
        try:
            self._clipboard.set_text(text, -1)
            self._clipboard.store()

            logger.info(f"Set text to clipboard ({len(text)} chars)")
            return True
        except Exception as e:
            logger.error(f"Failed to set clipboard text: {e}")
            return False

    def set_clipboard_image(self, image_data: bytes) -> bool:
        """
        Set image to clipboard

        Args:
            image_data: PNG image bytes

        Returns:
            True if successful
        """
        try:
            # Load PNG bytes into pixbuf
            loader = GdkPixbuf.PixbufLoader.new_with_type('png')
            loader.write(image_data)
            loader.close()

            pixbuf = loader.get_pixbuf()
            if not pixbuf:
                logger.error("Failed to load image")
                return False

            self._clipboard.set_image(pixbuf)
            self._clipboard.store()

            logger.info(f"Set image to clipboard ({len(image_data)} bytes)")
            return True
        except Exception as e:
            logger.error(f"Failed to set clipboard image: {e}")
            return False
