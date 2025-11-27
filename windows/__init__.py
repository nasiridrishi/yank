"""Windows-specific modules for clipboard sync"""
from .clipboard import WindowsClipboardMonitor, get_clipboard_files

__all__ = ['WindowsClipboardMonitor', 'get_clipboard_files']
