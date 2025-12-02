"""
SystemTray - System tray icon and menu for the drawer app.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .signals import SyncSignals

logger = logging.getLogger(__name__)


def create_default_icon(connected: bool = False) -> QIcon:
    """
    Create a simple default icon programmatically.

    Args:
        connected: If True, use green accent; otherwise gray
    """
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Draw a clipboard shape
    color = QColor("#4aff4a") if connected else QColor("#888888")
    painter.setBrush(color)
    painter.setPen(Qt.NoPen)

    # Clipboard body
    painter.drawRoundedRect(8, 12, 48, 44, 4, 4)

    # Clipboard clip at top
    painter.drawRoundedRect(20, 4, 24, 12, 3, 3)

    # Inner area (white)
    painter.setBrush(QColor("#ffffff"))
    painter.drawRect(14, 20, 36, 30)

    # Lines representing content
    painter.setBrush(color)
    painter.drawRect(18, 26, 28, 3)
    painter.drawRect(18, 33, 20, 3)
    painter.drawRect(18, 40, 24, 3)

    painter.end()

    return QIcon(pixmap)


class SystemTray(QSystemTrayIcon):
    """
    System tray icon with menu for the drawer app.

    Features:
    - Tray icon with connection status indicator
    - Click to toggle drawer
    - Context menu with common actions
    - Notifications for new items
    """

    def __init__(self, app: QApplication, drawer, signals: SyncSignals, parent=None):
        super().__init__(parent)

        self.app = app
        self.drawer = drawer
        self.signals = signals

        self._connected = False
        self._peer_name = ""

        self._setup_icon()
        self._setup_menu()
        self._connect_signals()

    def _setup_icon(self):
        """Set up the tray icon."""
        self.setIcon(create_default_icon(connected=False))
        self.setToolTip("Yank - Clipboard Sync")

        # Click to toggle drawer
        self.activated.connect(self._on_activated)

    def _setup_menu(self):
        """Set up the context menu."""
        menu = QMenu()

        # Show/Hide drawer
        self.show_action = QAction("Show Drawer", menu)
        self.show_action.triggered.connect(self.drawer.toggle_drawer)
        menu.addAction(self.show_action)

        menu.addSeparator()

        # Connection status (display only)
        self.status_action = QAction("Not connected", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        # Settings (placeholder for now)
        settings_action = QAction("Settings...", menu)
        settings_action.triggered.connect(self._show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        # Quit
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _connect_signals(self):
        """Connect to sync signals."""
        # New files announced - show notification
        self.signals.files_announced.connect(self._on_files_announced)

    def _on_activated(self, reason):
        """Handle tray icon activation."""
        if reason == QSystemTrayIcon.Trigger:  # Single click
            self.drawer.toggle_drawer()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.drawer.show_drawer()

    def _on_files_announced(self, transfer_id: str, metadata):
        """Show notification when files are announced."""
        file_count = len(metadata.files)
        if file_count == 1:
            title = "File available"
            message = f"{metadata.files[0].name}"
        else:
            title = f"{file_count} files available"
            message = ", ".join(f.name for f in metadata.files[:3])
            if file_count > 3:
                message += f" +{file_count - 3} more"

        self.showMessage(title, message, QSystemTrayIcon.Information, 3000)

    def update_connection_status(self, connected: bool, peer_name: str):
        """Update the connection status."""
        self._connected = connected
        self._peer_name = peer_name

        # Update icon
        self.setIcon(create_default_icon(connected=connected))

        # Update tooltip
        if connected:
            self.setToolTip(f"Yank - Connected to {peer_name}")
            self.status_action.setText(f"Connected to {peer_name}")
        else:
            self.setToolTip("Yank - Not connected")
            self.status_action.setText("Not connected")

    def _show_settings(self):
        """Show settings dialog (placeholder)."""
        # TODO: Implement settings dialog
        logger.info("Settings clicked - not implemented yet")

    def _quit(self):
        """Quit the application."""
        self.app.quit()
