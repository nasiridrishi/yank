"""
SystemTray - System tray icon and menu for the drawer app.
"""

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from .signals import SyncSignals

logger = logging.getLogger(__name__)


def create_tray_icon(connected: bool = False, has_items: bool = False) -> QIcon:
    """
    Create a modern tray icon.

    Args:
        connected: If True, show connected state
        has_items: If True, show notification badge
    """
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Colors
    if connected:
        primary_color = QColor("#3b82f6")  # Blue when connected
    else:
        primary_color = QColor("#71717a")  # Gray when disconnected

    # Draw clipboard shape
    pen = QPen(primary_color, 4)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)

    # Clipboard body (rounded rectangle)
    clip_path = QPainterPath()
    clip_path.addRoundedRect(10, 14, 44, 42, 6, 6)
    painter.drawPath(clip_path)

    # Clipboard clip at top
    painter.setBrush(QBrush(primary_color))
    clip_top = QPainterPath()
    clip_top.addRoundedRect(22, 6, 20, 14, 4, 4)
    painter.drawPath(clip_top)

    # Inner lines (representing content)
    painter.setPen(QPen(primary_color, 3))
    painter.drawLine(18, 28, 46, 28)
    painter.drawLine(18, 38, 38, 38)
    painter.drawLine(18, 48, 42, 48)

    # Notification badge if has items
    if has_items:
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor("#ef4444")))
        painter.drawEllipse(44, 4, 16, 16)

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
        self._pending_count = 0

        self._setup_icon()
        self._setup_menu()
        self._connect_signals()

    def _setup_icon(self):
        """Set up the tray icon."""
        self.setIcon(create_tray_icon(connected=False))
        self.setToolTip("Yank - Clipboard Sync")

        # Click to toggle drawer
        self.activated.connect(self._on_activated)

    def _setup_menu(self):
        """Set up the context menu."""
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background-color: #27272a;
                border: 1px solid #3f3f46;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px 8px 16px;
                color: #fafafa;
                border-radius: 4px;
                margin: 2px 4px;
            }
            QMenu::item:selected {
                background-color: #3f3f46;
            }
            QMenu::item:disabled {
                color: #71717a;
            }
            QMenu::separator {
                height: 1px;
                background-color: #3f3f46;
                margin: 4px 8px;
            }
        """)

        # Show/Hide drawer
        self.show_action = QAction("Open Yank", menu)
        self.show_action.triggered.connect(self.drawer.show_drawer)
        menu.addAction(self.show_action)

        menu.addSeparator()

        # Connection status (display only)
        self.status_action = QAction("Not connected", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        # Settings (placeholder)
        settings_action = QAction("Preferences...", menu)
        settings_action.triggered.connect(self._show_settings)
        menu.addAction(settings_action)

        menu.addSeparator()

        # Quit
        quit_action = QAction("Quit Yank", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _connect_signals(self):
        """Connect to sync signals."""
        self.signals.files_announced.connect(self._on_files_announced)
        self.signals.transfer_complete.connect(self._on_transfer_complete)

    def _on_activated(self, reason):
        """Handle tray icon activation."""
        if reason == QSystemTrayIcon.Trigger:  # Single click
            self.drawer.toggle_drawer()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.drawer.show_drawer()

    def _on_files_announced(self, transfer_id: str, metadata):
        """Show notification when files are announced."""
        self._pending_count += 1
        self._update_icon()

        file_count = len(metadata.files)
        if file_count == 1:
            title = "File available"
            message = metadata.files[0].name
        else:
            title = f"{file_count} files available"
            names = [f.name for f in metadata.files[:2]]
            message = ", ".join(names)
            if file_count > 2:
                message += f" +{file_count - 2} more"

        self.showMessage(title, message, QSystemTrayIcon.Information, 3000)

    def _on_transfer_complete(self, transfer_id: str, success: bool, file_paths: list):
        """Update icon when transfer completes."""
        if self._pending_count > 0:
            self._pending_count -= 1
        self._update_icon()

    def _update_icon(self):
        """Update the tray icon based on state."""
        self.setIcon(create_tray_icon(connected=self._connected, has_items=self._pending_count > 0))

    def update_connection_status(self, connected: bool, peer_name: str):
        """Update the connection status."""
        self._connected = connected
        self._peer_name = peer_name
        self._update_icon()

        if connected:
            self.setToolTip(f"Yank - Connected to {peer_name}")
            self.status_action.setText(f"Connected to {peer_name}")
        else:
            self.setToolTip("Yank - Not connected")
            self.status_action.setText("Not connected")

    def _show_settings(self):
        """Show settings dialog."""
        # TODO: Implement settings dialog
        logger.info("Settings clicked - not implemented yet")
        self.showMessage(
            "Settings", "Settings dialog coming soon!", QSystemTrayIcon.Information, 2000
        )

    def _quit(self):
        """Quit the application."""
        self.app.quit()
