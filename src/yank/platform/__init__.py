"""
Platform abstraction layer with auto-detection

Automatically detects the platform and provides the appropriate clipboard monitor.
"""
import sys
from typing import Type

from .base import ClipboardMonitorBase, PlatformInfo

_PLATFORM = sys.platform

if _PLATFORM == "win32":
    from .windows import WindowsClipboardMonitor
    _clipboard_monitor_class = WindowsClipboardMonitor
    _platform_info = PlatformInfo(
        name="windows",
        display_name="Windows",
        supports_virtual_clipboard=False,
        copy_shortcut="Ctrl+C",
        paste_shortcut="Ctrl+V",
    )
elif _PLATFORM == "darwin":
    from .macos import MacClipboardMonitor
    _clipboard_monitor_class = MacClipboardMonitor
    _platform_info = PlatformInfo(
        name="macos",
        display_name="macOS",
        supports_virtual_clipboard=True,
        copy_shortcut="Cmd+C",
        paste_shortcut="Cmd+V",
    )
elif _PLATFORM.startswith("linux"):
    from .linux import LinuxClipboardMonitor
    _clipboard_monitor_class = LinuxClipboardMonitor
    _platform_info = PlatformInfo(
        name="linux",
        display_name="Linux",
        supports_virtual_clipboard=True,
        copy_shortcut="Ctrl+C",
        paste_shortcut="Ctrl+V",
    )
else:
    raise RuntimeError(f"Unsupported platform: {_PLATFORM}")


ClipboardMonitor = _clipboard_monitor_class


def get_platform_info() -> PlatformInfo:
    """Get information about the current platform"""
    return _platform_info


def get_clipboard_monitor_class() -> Type[ClipboardMonitorBase]:
    """Get the clipboard monitor class for the current platform"""
    return _clipboard_monitor_class


__all__ = [
    'ClipboardMonitor',
    'ClipboardMonitorBase',
    'PlatformInfo',
    'get_platform_info',
    'get_clipboard_monitor_class',
]
