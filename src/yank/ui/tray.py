"""
SystemTray - System tray icon for the drawer app.
Simple, native-looking tray with minimal menu.
"""

import logging
import platform

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QStyle, QSystemTrayIcon

from .signals import SyncSignals

logger = logging.getLogger(__name__)

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def create_tray_icon(connected: bool = False, has_pending: bool = False) -> QIcon:
    """
    Create a simple tray icon.
    Uses system colors for native feel.
    """
    size = 32 if IS_MAC else 16
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)

    # Simple clipboard shape
    color = QColor("#107c10") if connected else QColor("#666666")
    painter.setPen(Qt.NoPen)
    painter.setBrush(color)

    # Scale factors
    s = size / 16.0

    # Clipboard body
    painter.drawRoundedRect(
        int(2 * s), int(4 * s), int(12 * s), int(11 * s), int(2 * s), int(2 * s)
    )

    # Clip at top
    painter.drawRoundedRect(int(5 * s), int(1 * s), int(6 * s), int(4 * s), int(1 * s), int(1 * s))

    # Inner white area
    painter.setBrush(QColor("#ffffff"))
    painter.drawRect(int(4 * s), int(6 * s), int(8 * s), int(7 * s))

    # Content lines
    painter.setBrush(color)
    painter.drawRect(int(5 * s), int(8 * s), int(6 * s), int(1 * s))
    painter.drawRect(int(5 * s), int(10 * s), int(4 * s), int(1 * s))

    # Notification dot
    if has_pending:
        painter.setBrush(QColor("#d83b01"))
        painter.drawEllipse(int(10 * s), int(1 * s), int(5 * s), int(5 * s))

    painter.end()
    return QIcon(pixmap)


class SystemTray(QSystemTrayIcon):
    """
    System tray icon with simple native menu.
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
        self.setIcon(create_tray_icon(connected=False))
        self.setToolTip("Yank - Clipboard Sync")
        self.activated.connect(self._on_activated)

    def _setup_menu(self):
        menu = QMenu()

        # Open drawer
        open_action = QAction("Open Yank", menu)
        open_action.triggered.connect(self.drawer.show_drawer)
        menu.addAction(open_action)

        menu.addSeparator()

        # Status (disabled, just for display)
        self.status_action = QAction("Not connected", menu)
        self.status_action.setEnabled(False)
        menu.addAction(self.status_action)

        menu.addSeparator()

        # Quit
        quit_action = QAction("Quit", menu)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)

        self.setContextMenu(menu)

    def _connect_signals(self):
        self.signals.files_announced.connect(self._on_files_announced)
        self.signals.files_received.connect(self._on_files_received)
        self.signals.transfer_complete.connect(self._on_transfer_complete)

    def _on_activated(self, reason):
        if reason == QSystemTrayIcon.Trigger:
            self.drawer.toggle_drawer()
        elif reason == QSystemTrayIcon.DoubleClick:
            self.drawer.show_drawer()

    def _on_files_announced(self, transfer_id: str, metadata):
        self._pending_count += 1
        self._update_icon()

        # Show notification
        count = len(metadata.files)
        if count == 1:
            title = "File available"
            msg = metadata.files[0].name
        else:
            title = f"{count} files available"
            msg = ", ".join(f.name for f in metadata.files[:2])
            if count > 2:
                msg += f" +{count - 2} more"

        self.showMessage(title, msg, QSystemTrayIcon.Information, 3000)

    def _on_files_received(self, transfer_id: str, file_paths: list):
        # Show notification for received files
        count = len(file_paths)
        if count == 1:
            title = "File received"
            msg = file_paths[0].name
        else:
            title = f"{count} files received"
            msg = ", ".join(p.name for p in file_paths[:2])
            if count > 2:
                msg += f" +{count - 2} more"

        self.showMessage(title, msg, QSystemTrayIcon.Information, 3000)

    def _on_transfer_complete(self, transfer_id: str, success: bool, file_paths: list):
        if self._pending_count > 0:
            self._pending_count -= 1
        self._update_icon()

    def _update_icon(self):
        self.setIcon(
            create_tray_icon(connected=self._connected, has_pending=self._pending_count > 0)
        )

    def update_connection_status(self, connected: bool, peer_name: str):
        self._connected = connected
        self._peer_name = peer_name
        self._update_icon()

        if connected:
            self.setToolTip(f"Yank - {peer_name}")
            self.status_action.setText(f"Connected to {peer_name}")
        else:
            self.setToolTip("Yank - Not connected")
            self.status_action.setText("Not connected")

    def _quit(self):
        self.app.quit()
