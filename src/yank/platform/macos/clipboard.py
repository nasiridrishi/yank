"""
macOS Clipboard Monitoring

Uses PyObjC to monitor the macOS pasteboard (clipboard) for:
- File copies (NSFilenamesPboardType) - files from Finder
- Image data (NSPasteboardTypePNG, TIFF) - screenshots, copied images

When files or images are copied, this module detects it and triggers a callback.
"""
import os
import io
import time
import logging
import threading
import tempfile
import hashlib
from typing import Optional, Callable, List
from pathlib import Path
from urllib.parse import unquote, urlparse
from datetime import datetime

# macOS-specific imports
try:
    from AppKit import (
        NSPasteboard, NSPasteboardTypeFileURL, NSURL, NSFilenamesPboardType,
        NSPasteboardTypePNG, NSPasteboardTypeTIFF, NSImage, NSBitmapImageRep,
        NSPNGFileType, NSPasteboardTypeString
    )
    from Foundation import NSArray, NSData, NSString
    HAS_APPKIT = True
except ImportError:
    HAS_APPKIT = False
    print("WARNING: pyobjc not installed. Run: pip install pyobjc")

# For image handling
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("WARNING: Pillow not installed for image processing. Run: pip install Pillow")

logger = logging.getLogger(__name__)


class MacClipboardMonitor:
    """
    Monitor macOS pasteboard for file copies, images, and text

    Supports:
    - Files (NSFilenamesPboardType) - from Finder
    - Images (NSPasteboardTypePNG, TIFF) - screenshots, copied images
    - Text (NSPasteboardTypeString) - copied text

    Uses polling to check the pasteboard change count.
    """

    def __init__(self,
                 on_files_copied: Optional[Callable[[List[Path]], None]] = None,
                 on_text_copied: Optional[Callable[[str], None]] = None,
                 poll_interval: float = 0.3,
                 temp_dir: Path = None,
                 sync_text: bool = True,
                 sync_files: bool = True,
                 sync_images: bool = True):
        """
        Initialize clipboard monitor

        Args:
            on_files_copied: Callback when files/images are copied to clipboard
            on_text_copied: Callback when text is copied to clipboard
            poll_interval: Seconds between clipboard checks
            temp_dir: Directory to save temporary image files
            sync_text: Whether to sync text clipboard
            sync_files: Whether to sync files
            sync_images: Whether to sync images
        """
        if not HAS_APPKIT:
            raise RuntimeError("pyobjc is required for macOS clipboard monitoring")

        self.on_files_copied = on_files_copied
        self.on_text_copied = on_text_copied
        self.poll_interval = poll_interval
        self.temp_dir = temp_dir or Path(tempfile.gettempdir()) / 'clipboard-sync'
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        # Feature toggles
        self.sync_text = sync_text
        self.sync_files = sync_files
        self.sync_images = sync_images

        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._last_change_count: int = 0
        self._last_content_hash: Optional[str] = None
        self._last_text_hash: Optional[str] = None
        self._lock = threading.Lock()

        # Track files/text we've received to avoid loops
        self._received_files: set = set()
        self._received_files_lock = threading.Lock()
        self._received_text_hash: Optional[str] = None
        self._received_text_lock = threading.Lock()

        # Get pasteboard reference
        self._pasteboard = NSPasteboard.generalPasteboard()
    
    def start(self):
        """Start monitoring the clipboard"""
        if self._running:
            return
        
        # Initialize change count
        self._last_change_count = self._pasteboard.changeCount()
        
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("macOS clipboard monitor started (files + images)")
    
    def stop(self):
        """Stop monitoring the clipboard"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        logger.info("macOS clipboard monitor stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        while self._running:
            try:
                self._check_clipboard()
            except Exception as e:
                logger.debug(f"Clipboard check error: {e}")
            
            time.sleep(self.poll_interval)
    
    def _check_clipboard(self):
        """Check clipboard for file, image, or text changes"""
        # Check if clipboard changed
        current_count = self._pasteboard.changeCount()

        if current_count == self._last_change_count:
            return

        # Get available types
        types = self._pasteboard.types()

        # Re-check change count - if clipboard changed during read, skip this cycle
        # to avoid processing inconsistent data
        post_read_count = self._pasteboard.changeCount()
        if post_read_count != current_count:
            self._last_change_count = post_read_count
            return

        self._last_change_count = current_count

        # Priority: Files first, then images, then text
        if self.sync_files and NSFilenamesPboardType in types:
            self._handle_files()
        elif self.sync_images and (NSPasteboardTypePNG in types or NSPasteboardTypeTIFF in types):
            self._handle_image()
        elif self.sync_text and NSPasteboardTypeString in types:
            self._handle_text()
    
    def _handle_files(self):
        """Handle file clipboard data"""
        file_paths = self._get_files_from_pasteboard()
        
        if not file_paths:
            return
        
        # Create hash to detect duplicates
        file_hash = self._hash_file_list(file_paths)
        
        with self._lock:
            if file_hash == self._last_content_hash:
                return
            self._last_content_hash = file_hash
        
        # Check if these are files we just received (avoid loops)
        with self._received_files_lock:
            if all(str(p).lower() in self._received_files for p in file_paths):
                logger.debug("Skipping received files to avoid loop")
                return
        
        logger.info(f"Detected {len(file_paths)} file(s) copied to clipboard")
        
        if self.on_files_copied:
            self.on_files_copied(file_paths)
    
    def _handle_image(self):
        """Handle image clipboard data (screenshots, copied images)"""
        try:
            image_data = None
            
            # Try PNG first
            if NSPasteboardTypePNG in self._pasteboard.types():
                image_data = self._pasteboard.dataForType_(NSPasteboardTypePNG)
            # Fall back to TIFF
            elif NSPasteboardTypeTIFF in self._pasteboard.types():
                image_data = self._pasteboard.dataForType_(NSPasteboardTypeTIFF)
            
            if not image_data:
                return
            
            # Get bytes for hashing
            data_bytes = bytes(image_data)
            
            # Create hash to detect changes
            data_hash = hashlib.md5(data_bytes[:4096] if len(data_bytes) > 4096 else data_bytes).hexdigest()
            
            with self._lock:
                if data_hash == self._last_content_hash:
                    return
                self._last_content_hash = data_hash
            
            # Save to temp file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"clipboard_image_{timestamp}.png"
            filepath = self.temp_dir / filename
            
            # Convert TIFF to PNG if needed
            if NSPasteboardTypeTIFF in self._pasteboard.types() and NSPasteboardTypePNG not in self._pasteboard.types():
                # Use NSImage to convert
                ns_image = NSImage.alloc().initWithData_(image_data)
                if ns_image:
                    tiff_data = ns_image.TIFFRepresentation()
                    bitmap = NSBitmapImageRep.imageRepWithData_(tiff_data)
                    png_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
                    png_data.writeToFile_atomically_(str(filepath), True)
                else:
                    # Fall back to raw write
                    with open(filepath, 'wb') as f:
                        f.write(data_bytes)
            else:
                # Already PNG
                with open(filepath, 'wb') as f:
                    f.write(data_bytes)
            
            logger.info(f"Detected image in clipboard, saved to {filename}")
            
            # Track to avoid loops
            with self._received_files_lock:
                self._received_files.add(str(filepath).lower())
            
            if self.on_files_copied:
                self.on_files_copied([filepath])
                
        except Exception as e:
            logger.error(f"Failed to process clipboard image: {e}")
    
    def _get_files_from_pasteboard(self) -> List[Path]:
        """Get files from the pasteboard"""
        file_paths = []
        
        # Try NSFilenamesPboardType first (older but more reliable)
        filenames = self._pasteboard.propertyListForType_(NSFilenamesPboardType)
        if filenames:
            for filename in filenames:
                path = Path(filename)
                if path.exists():
                    file_paths.append(path)
            return file_paths
        
        # Try file URLs
        types = self._pasteboard.types()
        
        if NSPasteboardTypeFileURL in types:
            # Get all items
            items = self._pasteboard.pasteboardItems()
            
            for item in items:
                url_string = item.stringForType_(NSPasteboardTypeFileURL)
                if url_string:
                    # Parse the file URL
                    path = self._url_to_path(url_string)
                    if path and path.exists():
                        file_paths.append(path)
        
        return file_paths
    
    def _url_to_path(self, url_string: str) -> Optional[Path]:
        """Convert a file:// URL to a Path"""
        try:
            if url_string.startswith('file://'):
                # Parse and decode the URL
                parsed = urlparse(url_string)
                path_str = unquote(parsed.path)
                return Path(path_str)
        except Exception as e:
            logger.debug(f"Error parsing URL {url_string}: {e}")
        return None
    
    def _handle_text(self):
        """Handle text clipboard data"""
        try:
            text = self._pasteboard.stringForType_(NSPasteboardTypeString)
        except:
            return

        if not text or not text.strip():
            return

        # Create hash to detect changes
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()

        with self._lock:
            if text_hash == self._last_text_hash:
                return
            self._last_text_hash = text_hash

        # Check if this is text we just received (avoid loops)
        with self._received_text_lock:
            if text_hash == self._received_text_hash:
                logger.debug("Skipping received text to avoid loop")
                return

        logger.info(f"Detected text in clipboard ({len(text)} chars)")

        if self.on_text_copied:
            self.on_text_copied(text)

    def _hash_file_list(self, file_paths: List[Path]) -> str:
        """Create a hash of file list for change detection"""
        content = '|'.join(sorted(str(p) for p in file_paths))
        return hashlib.md5(content.encode()).hexdigest()

    def set_clipboard_text(self, text: str):
        """
        Set text in the clipboard

        Args:
            text: Text to put in clipboard
        """
        if not text:
            return

        # Track this text to avoid loops
        text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()
        with self._received_text_lock:
            self._received_text_hash = text_hash

        # Update our hash to avoid re-sending
        with self._lock:
            self._last_text_hash = text_hash

        try:
            self._pasteboard.clearContents()
            self._pasteboard.setString_forType_(text, NSPasteboardTypeString)
            self._last_change_count = self._pasteboard.changeCount()
            logger.info(f"Set text in clipboard ({len(text)} chars)")
        except Exception as e:
            logger.error(f"Failed to set clipboard text: {e}")

    def set_clipboard_files(self, file_paths: List[Path]):
        """
        Set files in the clipboard (for pasting)
        
        Args:
            file_paths: List of file paths to put in clipboard
        """
        if not file_paths:
            return
        
        # Track these files to avoid loops
        with self._received_files_lock:
            for p in file_paths:
                self._received_files.add(str(p).lower())
            
            # Clean old entries
            if len(self._received_files) > 100:
                self._received_files = set(list(self._received_files)[-50:])
        
        # Update our hash to avoid re-sending
        with self._lock:
            self._last_content_hash = self._hash_file_list(file_paths)
        
        # Check if it's a single image file - put as image data too
        if len(file_paths) == 1:
            ext = file_paths[0].suffix.lower()
            if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.tiff', '.webp'):
                self._set_clipboard_image_file(file_paths[0])
                return
        
        try:
            # Clear and prepare pasteboard
            self._pasteboard.clearContents()
            
            # Convert paths to filenames
            filenames = [str(p) for p in file_paths]
            
            # Set as filenames (for Finder paste)
            self._pasteboard.setPropertyList_forType_(
                NSArray.arrayWithArray_(filenames),
                NSFilenamesPboardType
            )
            
            # Update change count tracking
            self._last_change_count = self._pasteboard.changeCount()
            
            logger.info(f"Set {len(file_paths)} file(s) in clipboard")
            
        except Exception as e:
            logger.error(f"Failed to set clipboard files: {e}")
    
    def _set_clipboard_image_file(self, image_path: Path):
        """Set an image file in clipboard as both file and image data"""
        try:
            # Read image data
            with open(image_path, 'rb') as f:
                image_data = f.read()
            
            # Determine format and convert to PNG if needed
            ext = image_path.suffix.lower()
            
            if ext == '.png':
                png_data = image_data
            else:
                # Use NSImage to convert to PNG
                ns_data = NSData.dataWithBytes_length_(image_data, len(image_data))
                ns_image = NSImage.alloc().initWithData_(ns_data)
                if ns_image:
                    tiff_data = ns_image.TIFFRepresentation()
                    bitmap = NSBitmapImageRep.imageRepWithData_(tiff_data)
                    png_ns_data = bitmap.representationUsingType_properties_(NSPNGFileType, None)
                    png_data = bytes(png_ns_data)
                else:
                    # Fallback: just set as file
                    self._set_clipboard_files_only([image_path])
                    return
            
            # Clear and prepare pasteboard
            self._pasteboard.clearContents()
            
            # Set PNG data for image paste
            png_ns_data = NSData.dataWithBytes_length_(png_data, len(png_data))
            self._pasteboard.setData_forType_(png_ns_data, NSPasteboardTypePNG)
            
            # Also set as file for file-based paste
            self._pasteboard.setPropertyList_forType_(
                NSArray.arrayWithArray_([str(image_path)]),
                NSFilenamesPboardType
            )
            
            # Update change count tracking
            self._last_change_count = self._pasteboard.changeCount()
            
            logger.info(f"Set image in clipboard: {image_path.name}")
            
        except Exception as e:
            logger.error(f"Failed to set clipboard image: {e}")
            # Fall back to file-only
            self._set_clipboard_files_only([image_path])
    
    def _set_clipboard_files_only(self, file_paths: List[Path]):
        """Set files in clipboard (without image conversion)"""
        try:
            self._pasteboard.clearContents()
            filenames = [str(p) for p in file_paths]
            self._pasteboard.setPropertyList_forType_(
                NSArray.arrayWithArray_(filenames),
                NSFilenamesPboardType
            )
            self._last_change_count = self._pasteboard.changeCount()
        except Exception as e:
            logger.error(f"Failed to set clipboard files: {e}")
    
    def clear_received_files(self):
        """Clear the received files tracking set"""
        with self._received_files_lock:
            self._received_files.clear()

    def set_virtual_clipboard_files(
        self,
        files: List[dict],
        transfer_id: str,
        download_callback: Callable[[str, int], Optional[bytes]]
    ) -> bool:
        """
        Set virtual files on the clipboard for on-demand download.

        On macOS, this uses a placeholder approach since NSFilePromiseProvider
        doesn't work with Finder copy/paste (only drag-drop).

        Args:
            files: List of file info dicts with 'name', 'size', 'checksum', 'file_index'
            transfer_id: The transfer ID for downloading
            download_callback: Function to call when file content is needed

        Returns:
            True if successful
        """
        try:
            from macos.virtual_clipboard import set_virtual_clipboard

            # Track files to avoid loops
            with self._received_files_lock:
                for f in files:
                    # Mark the staging path as received
                    staging_path = Path.home() / ".clipboard-sync" / "staging" / transfer_id[:8] / f['name']
                    self._received_files.add(str(staging_path).lower())

            # Update hash to avoid re-sending
            with self._lock:
                file_names = '|'.join(sorted(f['name'] for f in files))
                self._last_content_hash = hashlib.md5(file_names.encode()).hexdigest()

            result = set_virtual_clipboard(files, transfer_id, download_callback)

            if result:
                # Update change count after setting clipboard
                self._last_change_count = self._pasteboard.changeCount()

            return result

        except ImportError:
            logger.error("Virtual clipboard module not available")
            return False
        except Exception as e:
            logger.error(f"Failed to set virtual clipboard: {e}")
            return False


def get_clipboard_files() -> List[Path]:
    """
    Get current files from clipboard
    
    Returns:
        List of file paths in clipboard, or empty list if no files
    """
    if not HAS_APPKIT:
        return []
    
    try:
        pasteboard = NSPasteboard.generalPasteboard()
        
        # Try NSFilenamesPboardType
        filenames = pasteboard.propertyListForType_(NSFilenamesPboardType)
        if filenames:
            return [Path(f) for f in filenames if Path(f).exists()]
        
    except Exception as e:
        logger.debug(f"Could not get clipboard files: {e}")
    
    return []
