"""
Windows Clipboard Monitoring

Uses pywin32 to monitor the Windows clipboard for:
- File copies (CF_HDROP) - files from Explorer
- Image data (CF_DIB/CF_DIBV5/PNG) - screenshots, copied images

When files or images are copied, this module detects it and triggers a callback.
"""
import os
import io
import time
import logging
import threading
import tempfile
import hashlib
from typing import Optional, Callable, List, Tuple
from pathlib import Path
from datetime import datetime

# Windows-specific imports
try:
    import win32clipboard
    import win32con
    import win32api
    import win32gui
    import pythoncom
    from ctypes import windll, create_unicode_buffer, sizeof, byref
    from ctypes.wintypes import UINT
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False
    print("WARNING: pywin32 not installed. Run: pip install pywin32")

# For image handling
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("WARNING: Pillow not installed. Image clipboard support disabled. Run: pip install Pillow")

logger = logging.getLogger(__name__)

# Clipboard formats
CF_HDROP = 15  # File list format
CF_DIB = 8     # Device Independent Bitmap
CF_DIBV5 = 17  # DIB v5 (with alpha)
CF_PNG = None  # Will be registered dynamically


class WindowsClipboardMonitor:
    """
    Monitor Windows clipboard for file copies, images, and text

    Supports:
    - Files (CF_HDROP) - from Explorer
    - Images (CF_DIB, CF_DIBV5, PNG) - screenshots, copied images
    - Text (CF_UNICODETEXT) - copied text

    Uses polling (checking clipboard periodically) rather than
    clipboard viewer chain, which is more reliable and simpler.
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
        if not HAS_WIN32:
            raise RuntimeError("pywin32 is required for Windows clipboard monitoring")

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
        self._last_clipboard_hash: Optional[str] = None
        self._last_text_hash: Optional[str] = None
        self._last_change_count: int = 0
        self._lock = threading.Lock()

        # Track files/text we've received to avoid loops
        self._received_files: set = set()
        self._received_files_lock = threading.Lock()
        self._received_text_hash: Optional[str] = None
        self._received_text_lock = threading.Lock()

        # Register PNG clipboard format
        global CF_PNG
        try:
            win32clipboard.OpenClipboard()
            CF_PNG = win32clipboard.RegisterClipboardFormat("PNG")
            win32clipboard.CloseClipboard()
        except:
            CF_PNG = None
    
    def start(self):
        """Start monitoring the clipboard"""
        if self._running:
            return
        
        self._running = True
        self._monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._monitor_thread.start()
        logger.info("Windows clipboard monitor started (files + images)")
    
    def stop(self):
        """Stop monitoring the clipboard"""
        self._running = False
        if self._monitor_thread:
            self._monitor_thread.join(timeout=2)
        logger.info("Windows clipboard monitor stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop"""
        # Initialize COM for this thread
        pythoncom.CoInitialize()
        
        try:
            while self._running:
                try:
                    self._check_clipboard()
                except Exception as e:
                    logger.debug(f"Clipboard check error: {e}")
                
                time.sleep(self.poll_interval)
        finally:
            pythoncom.CoUninitialize()
    
    def _check_clipboard(self):
        """Check clipboard for file, image, or text changes"""
        try:
            win32clipboard.OpenClipboard()

            try:
                # Priority: Files first, then images, then text
                if self.sync_files and win32clipboard.IsClipboardFormatAvailable(CF_HDROP):
                    self._handle_files()
                elif self.sync_images and HAS_PIL and (
                        win32clipboard.IsClipboardFormatAvailable(CF_DIB) or
                        win32clipboard.IsClipboardFormatAvailable(CF_DIBV5) or
                        (CF_PNG and win32clipboard.IsClipboardFormatAvailable(CF_PNG))):
                    self._handle_image()
                elif self.sync_text and win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    self._handle_text()

            finally:
                win32clipboard.CloseClipboard()

        except Exception as e:
            # Clipboard might be locked by another app
            logger.debug(f"Could not access clipboard: {e}")
    
    def _handle_files(self):
        """Handle file clipboard data"""
        data = win32clipboard.GetClipboardData(CF_HDROP)
        
        if not data:
            return
        
        # data is a tuple of file paths
        file_paths = [Path(f) for f in data]
        
        # Create hash of file list to detect changes
        clipboard_hash = self._hash_file_list(file_paths)
        
        # Check if this is new
        with self._lock:
            if clipboard_hash == self._last_clipboard_hash:
                return
            self._last_clipboard_hash = clipboard_hash
        
        # Check if these are files we just received (avoid loops)
        with self._received_files_lock:
            if all(str(p).lower() in self._received_files for p in file_paths):
                logger.debug("Skipping received files to avoid loop")
                return
        
        # Filter to only existing files
        valid_files = [p for p in file_paths if p.exists()]
        
        if valid_files:
            logger.info(f"Detected {len(valid_files)} file(s) copied to clipboard")
            
            if self.on_files_copied:
                self.on_files_copied(valid_files)
    
    def _handle_image(self):
        """Handle image clipboard data (screenshots, copied images)"""
        if not HAS_PIL:
            return
        
        image_data = None
        image_format = None
        
        # Try PNG first (best quality, supports transparency)
        if CF_PNG and win32clipboard.IsClipboardFormatAvailable(CF_PNG):
            try:
                image_data = win32clipboard.GetClipboardData(CF_PNG)
                image_format = 'png'
            except:
                pass
        
        # Fall back to DIB
        if not image_data and win32clipboard.IsClipboardFormatAvailable(CF_DIB):
            try:
                image_data = win32clipboard.GetClipboardData(CF_DIB)
                image_format = 'dib'
            except:
                pass
        
        if not image_data:
            return
        
        # Create hash to detect changes
        data_hash = hashlib.md5(image_data[:4096] if len(image_data) > 4096 else image_data).hexdigest()
        
        with self._lock:
            if data_hash == self._last_clipboard_hash:
                return
            self._last_clipboard_hash = data_hash
        
        # Convert to image file
        try:
            if image_format == 'png':
                img = Image.open(io.BytesIO(image_data))
            else:  # DIB
                img = self._dib_to_image(image_data)
            
            if img is None:
                return
            
            # Save to temp file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"clipboard_image_{timestamp}.png"
            filepath = self.temp_dir / filename
            
            img.save(filepath, 'PNG')
            
            logger.info(f"Detected image in clipboard, saved to {filename}")
            
            # Track to avoid loops
            with self._received_files_lock:
                self._received_files.add(str(filepath).lower())
            
            if self.on_files_copied:
                self.on_files_copied([filepath])
                
        except Exception as e:
            logger.error(f"Failed to process clipboard image: {e}")
    
    def _dib_to_image(self, dib_data: bytes) -> Optional[Image.Image]:
        """Convert DIB (Device Independent Bitmap) data to PIL Image"""
        try:
            # DIB header structure
            # BITMAPINFOHEADER: 40 bytes
            # biSize (4), biWidth (4), biHeight (4), biPlanes (2), biBitCount (2),
            # biCompression (4), biSizeImage (4), biXPelsPerMeter (4), biYPelsPerMeter (4),
            # biClrUsed (4), biClrImportant (4)
            
            import struct
            
            header_size = struct.unpack('<I', dib_data[0:4])[0]
            width = struct.unpack('<i', dib_data[4:8])[0]
            height = struct.unpack('<i', dib_data[8:12])[0]
            bit_count = struct.unpack('<H', dib_data[14:16])[0]
            
            # Height can be negative (top-down DIB)
            flip = height > 0
            height = abs(height)
            
            # Calculate pixel data offset
            if bit_count <= 8:
                # Has color table
                colors = 1 << bit_count
                pixel_offset = header_size + colors * 4
            else:
                pixel_offset = header_size
            
            pixel_data = dib_data[pixel_offset:]
            
            # Create image based on bit depth
            if bit_count == 32:
                img = Image.frombytes('RGBA', (width, height), pixel_data, 'raw', 'BGRA')
            elif bit_count == 24:
                # Use PIL's raw decoder for fast BGR to RGB conversion
                # Rows are padded to 4 bytes in DIB format
                row_size = ((width * 3 + 3) // 4) * 4
                # PIL can handle padded rows with the raw decoder's stride parameter
                img = Image.frombytes('RGB', (width, height), pixel_data, 'raw', 'BGR', row_size, -1 if flip else 1)
                flip = False  # Already handled by stride direction
            else:
                # For other bit depths, use a simpler approach
                # Create BMP in memory and let PIL handle it
                bmp_header = b'BM' + struct.pack('<I', 14 + len(dib_data)) + b'\x00\x00\x00\x00' + struct.pack('<I', 14 + header_size)
                bmp_data = bmp_header + dib_data
                img = Image.open(io.BytesIO(bmp_data))
            
            if flip and bit_count in (24, 32):
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            
            return img
            
        except Exception as e:
            logger.error(f"DIB conversion error: {e}")
            return None
    
    def _handle_text(self):
        """Handle text clipboard data"""
        try:
            text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
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
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32con.CF_UNICODETEXT)
                logger.info(f"Set text in clipboard ({len(text)} chars)")
            finally:
                win32clipboard.CloseClipboard()
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
            
            # Clean old entries (keep last 100)
            if len(self._received_files) > 100:
                self._received_files = set(list(self._received_files)[-50:])
        
        # Update our hash to avoid re-sending
        with self._lock:
            self._last_clipboard_hash = self._hash_file_list(file_paths)
        
        # Check if it's a single image file - put as image data too
        if len(file_paths) == 1 and HAS_PIL:
            ext = file_paths[0].suffix.lower()
            if ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp'):
                self._set_clipboard_image_file(file_paths[0])
                return
        
        try:
            # Build DROPFILES structure for CF_HDROP
            file_list = '\0'.join(str(p) for p in file_paths) + '\0\0'
            
            import struct
            
            # Calculate offset (size of DROPFILES structure)
            dropfiles_size = 20  # 4 + 8 + 4 + 4 bytes
            
            # Build the structure
            dropfiles = struct.pack('IiiII', dropfiles_size, 0, 0, 0, 1)
            
            # Combine structure and file list (in Unicode)
            data = dropfiles + file_list.encode('utf-16-le')
            
            # Set clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(CF_HDROP, data)
                logger.info(f"Set {len(file_paths)} file(s) in clipboard")
            finally:
                win32clipboard.CloseClipboard()
                
        except Exception as e:
            logger.error(f"Failed to set clipboard files: {e}")
    
    def _set_clipboard_image_file(self, image_path: Path):
        """Set an image file in clipboard as both file and image data"""
        try:
            img = Image.open(image_path)
            
            # Convert to BMP for clipboard (DIB)
            output = io.BytesIO()
            
            # Handle transparency - convert RGBA to RGB with white background
            if img.mode == 'RGBA':
                background = Image.new('RGB', img.size, (255, 255, 255))
                background.paste(img, mask=img.split()[3])
                img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            img.save(output, 'BMP')
            bmp_data = output.getvalue()
            
            # Skip BMP file header (14 bytes) to get DIB
            dib_data = bmp_data[14:]
            
            # Also prepare PNG data for apps that support it
            png_output = io.BytesIO()
            Image.open(image_path).save(png_output, 'PNG')
            png_data = png_output.getvalue()
            
            # Set clipboard with multiple formats
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                
                # Set DIB (most compatible)
                win32clipboard.SetClipboardData(CF_DIB, dib_data)
                
                # Set PNG (better quality)
                if CF_PNG:
                    win32clipboard.SetClipboardData(CF_PNG, png_data)
                
                # Also set as file for file-based paste
                import struct
                dropfiles_size = 20
                file_list = str(image_path) + '\0\0'
                dropfiles = struct.pack('IiiII', dropfiles_size, 0, 0, 0, 1)
                hdrop_data = dropfiles + file_list.encode('utf-16-le')
                win32clipboard.SetClipboardData(CF_HDROP, hdrop_data)
                
                logger.info(f"Set image in clipboard: {image_path.name}")
            finally:
                win32clipboard.CloseClipboard()
                
        except Exception as e:
            logger.error(f"Failed to set clipboard image: {e}")
            # Fall back to file-only
            self._set_clipboard_files_only(file_paths=[image_path])
    
    def _set_clipboard_files_only(self, file_paths: List[Path]):
        """Set files in clipboard (without image conversion)"""
        try:
            import struct
            file_list = '\0'.join(str(p) for p in file_paths) + '\0\0'
            dropfiles_size = 20
            dropfiles = struct.pack('IiiII', dropfiles_size, 0, 0, 0, 1)
            data = dropfiles + file_list.encode('utf-16-le')
            
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(CF_HDROP, data)
            finally:
                win32clipboard.CloseClipboard()
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
        download_callback
    ) -> bool:
        """
        Set virtual files on clipboard (true on-demand transfer).

        When user pastes, the download_callback will be called to get file data.

        Args:
            files: List of file info dicts with 'name', 'size', 'checksum', 'file_index'
            transfer_id: Transfer ID for the download
            download_callback: Callable[[str, int], Optional[bytes]] to get file data

        Returns:
            True if successful
        """
        try:
            from windows.virtual_clipboard import set_virtual_clipboard

            # Track to avoid loops
            with self._received_files_lock:
                for f in files:
                    self._received_files.add(f['name'].lower())

            success = set_virtual_clipboard(files, transfer_id, download_callback)

            if success:
                logger.info(f"Set {len(files)} virtual file(s) on clipboard")

            return success

        except ImportError as e:
            logger.warning(f"Virtual clipboard not available: {e}")
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
    if not HAS_WIN32:
        return []
    
    try:
        win32clipboard.OpenClipboard()
        try:
            if not win32clipboard.IsClipboardFormatAvailable(CF_HDROP):
                return []
            
            data = win32clipboard.GetClipboardData(CF_HDROP)
            if data:
                return [Path(f) for f in data if Path(f).exists()]
        finally:
            win32clipboard.CloseClipboard()
    except Exception as e:
        logger.debug(f"Could not get clipboard files: {e}")
    
    return []
