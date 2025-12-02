"""
Base classes for platform abstraction
"""
from abc import ABC, abstractmethod
from typing import List, Optional, Callable
from pathlib import Path
from dataclasses import dataclass


@dataclass
class PlatformInfo:
    """Information about a platform"""
    name: str
    display_name: str
    supports_virtual_clipboard: bool
    copy_shortcut: str
    paste_shortcut: str


class ClipboardMonitorBase(ABC):
    """
    Abstract base class for clipboard monitors

    All platform implementations must inherit from this class.
    """

    @abstractmethod
    def __init__(
        self,
        on_files_copied: Optional[Callable[[List[Path]], None]] = None,
        on_text_copied: Optional[Callable[[str], None]] = None,
        on_image_copied: Optional[Callable[[bytes], None]] = None,
        poll_interval: float = 0.3,
        enable_images: bool = True
    ):
        """
        Initialize clipboard monitor

        Args:
            on_files_copied: Callback when files are copied
            on_text_copied: Callback when text is copied
            on_image_copied: Callback when image is copied
            poll_interval: How often to check clipboard (seconds)
            enable_images: Whether to monitor images
        """
        pass

    @abstractmethod
    def start(self) -> None:
        """Start monitoring clipboard"""
        pass

    @abstractmethod
    def stop(self) -> None:
        """Stop monitoring clipboard"""
        pass

    @abstractmethod
    def set_clipboard_files(self, file_paths: List[Path]) -> bool:
        """
        Set files to clipboard

        Args:
            file_paths: List of file paths

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def set_clipboard_text(self, text: str) -> bool:
        """
        Set text to clipboard

        Args:
            text: Text to set

        Returns:
            True if successful
        """
        pass

    @abstractmethod
    def set_clipboard_image(self, image_data: bytes) -> bool:
        """
        Set image to clipboard

        Args:
            image_data: PNG image bytes

        Returns:
            True if successful
        """
        pass


__all__ = ['ClipboardMonitorBase', 'PlatformInfo']
