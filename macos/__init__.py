"""macOS-specific modules for clipboard sync"""
from .clipboard import MacClipboardMonitor, get_clipboard_files

__all__ = ['MacClipboardMonitor', 'get_clipboard_files']
