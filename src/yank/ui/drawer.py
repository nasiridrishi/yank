"""
DrawerWindow - Slide-down drawer window for file blueprints.

This window slides down from the top of the screen when activated,
showing announced files from other devices.
"""

import logging
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import Property, QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QAction, QCursor, QIcon, QScreen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from yank.common.chunked_transfer import format_bytes
from yank.common.protocol import TransferMetadata

from .signals import SyncSignals

logger = logging.getLogger(__name__)


class FileItemWidget(QFrame):
    """
    Custom widget for displaying a file blueprint in the list.

    Shows:
    - File name
    - File size
    - Source device
    - Download progress (when downloading)
    - Status indicator
    """

    def __init__(self, file_info: dict, transfer_id: str, source_device: str, parent=None):
        super().__init__(parent)
        self.file_info = file_info
        self.transfer_id = transfer_id
        self.source_device = source_device
        self.status = "pending"  # pending, downloading, completed, failed

        self._setup_ui()

    def _setup_ui(self):
        """Set up the widget UI."""
        self.setFrameStyle(QFrame.StyledPanel)
        self.setStyleSheet("""
            FileItemWidget {
                background-color: #2d2d2d;
                border-radius: 6px;
                padding: 8px;
                margin: 2px;
            }
            FileItemWidget:hover {
                background-color: #3d3d3d;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)

        # Top row: filename and size
        top_row = QHBoxLayout()

        self.name_label = QLabel(self.file_info.get("name", "Unknown"))
        self.name_label.setStyleSheet("font-weight: bold; color: #ffffff;")
        top_row.addWidget(self.name_label)

        top_row.addStretch()

        size = self.file_info.get("size", 0)
        self.size_label = QLabel(format_bytes(size))
        self.size_label.setStyleSheet("color: #888888;")
        top_row.addWidget(self.size_label)

        layout.addLayout(top_row)

        # Bottom row: source device and status
        bottom_row = QHBoxLayout()

        self.source_label = QLabel(f"from {self.source_device}")
        self.source_label.setStyleSheet("color: #666666; font-size: 11px;")
        bottom_row.addWidget(self.source_label)

        bottom_row.addStretch()

        self.status_label = QLabel("Ready to download")
        self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
        bottom_row.addWidget(self.status_label)

        layout.addLayout(bottom_row)

        # Progress bar (hidden by default)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #1a1a1a;
                height: 4px;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background-color: #4a9eff;
                border-radius: 2px;
            }
        """)
        layout.addWidget(self.progress_bar)

    def set_status(self, status: str, message: str = None):
        """Update the status display."""
        self.status = status

        if status == "pending":
            self.status_label.setText("Ready to download")
            self.status_label.setStyleSheet("color: #888888; font-size: 11px;")
            self.progress_bar.setVisible(False)
        elif status == "downloading":
            self.status_label.setText(message or "Downloading...")
            self.status_label.setStyleSheet("color: #4a9eff; font-size: 11px;")
            self.progress_bar.setVisible(True)
        elif status == "completed":
            self.status_label.setText("Downloaded")
            self.status_label.setStyleSheet("color: #4aff4a; font-size: 11px;")
            self.progress_bar.setVisible(False)
        elif status == "failed":
            self.status_label.setText(message or "Failed")
            self.status_label.setStyleSheet("color: #ff4a4a; font-size: 11px;")
            self.progress_bar.setVisible(False)

    def set_progress(self, percent: int):
        """Update the progress bar."""
        self.progress_bar.setValue(percent)


class TransferItemWidget(QFrame):
    """
    Widget for displaying a transfer (group of files) in the list.
    """

    def __init__(self, transfer_id: str, metadata: TransferMetadata, parent=None):
        super().__init__(parent)
        self.transfer_id = transfer_id
        self.metadata = metadata
        self.status = "pending"

        self._setup_ui()

    def _setup_ui(self):
        """Set up the widget UI."""
        self.setFrameStyle(QFrame.StyledPanel)
        self.setStyleSheet("""
            TransferItemWidget {
                background-color: #2d2d2d;
                border-radius: 8px;
                margin: 4px;
            }
            TransferItemWidget:hover {
                background-color: #353535;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(6)

        # Header row
        header = QHBoxLayout()

        # File count and total size
        file_count = len(self.metadata.files)
        if file_count == 1:
            title = self.metadata.files[0].name
        else:
            title = f"{file_count} files"

        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-weight: bold; font-size: 14px; color: #ffffff;")
        header.addWidget(self.title_label)

        header.addStretch()

        self.size_label = QLabel(format_bytes(self.metadata.total_size))
        self.size_label.setStyleSheet("color: #888888; font-size: 13px;")
        header.addWidget(self.size_label)

        layout.addLayout(header)

        # File list (for multi-file transfers)
        if file_count > 1:
            files_shown = min(3, file_count)
            for i in range(files_shown):
                f = self.metadata.files[i]
                file_label = QLabel(f"  - {f.name}")
                file_label.setStyleSheet("color: #888888; font-size: 12px;")
                layout.addWidget(file_label)

            if file_count > 3:
                more_label = QLabel(f"  ... +{file_count - 3} more")
                more_label.setStyleSheet("color: #666666; font-size: 12px;")
                layout.addWidget(more_label)

        # Status row
        status_row = QHBoxLayout()

        # Source (we don't have this info yet, placeholder)
        self.source_label = QLabel("from paired device")
        self.source_label.setStyleSheet("color: #666666; font-size: 11px;")
        status_row.addWidget(self.source_label)

        status_row.addStretch()

        self.status_label = QLabel("Click to download")
        self.status_label.setStyleSheet("color: #4a9eff; font-size: 11px;")
        status_row.addWidget(self.status_label)

        layout.addLayout(status_row)

        # Progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(4)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #1a1a1a;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background-color: #4a9eff;
                border-radius: 2px;
            }
        """)
        layout.addWidget(self.progress_bar)

    def set_status(self, status: str, message: str = None):
        """Update status display."""
        self.status = status

        if status == "pending":
            self.status_label.setText("Click to download")
            self.status_label.setStyleSheet("color: #4a9eff; font-size: 11px;")
            self.progress_bar.setVisible(False)
        elif status == "downloading":
            self.status_label.setText(message or "Downloading...")
            self.status_label.setStyleSheet("color: #4a9eff; font-size: 11px;")
            self.progress_bar.setVisible(True)
        elif status == "completed":
            self.status_label.setText("Downloaded")
            self.status_label.setStyleSheet("color: #4aff4a; font-size: 11px;")
            self.progress_bar.setVisible(False)
        elif status == "failed":
            self.status_label.setText(message or "Failed")
            self.status_label.setStyleSheet("color: #ff4a4a; font-size: 11px;")
            self.progress_bar.setVisible(False)

    def set_progress(self, bytes_done: int, bytes_total: int, current_file: str):
        """Update progress."""
        if bytes_total > 0:
            percent = int(bytes_done / bytes_total * 100)
            self.progress_bar.setValue(percent)
            self.status_label.setText(f"{percent}% - {current_file}")


class DrawerWindow(QWidget):
    """
    The main drawer window that slides down from the top of the screen.
    """

    DRAWER_WIDTH = 420
    DRAWER_HEIGHT = 400
    ANIMATION_DURATION = 250  # ms

    def __init__(self, signals: SyncSignals, parent=None):
        super().__init__(parent)
        self.signals = signals

        # Track transfers
        self._transfers: Dict[str, TransferItemWidget] = {}

        self._setup_window()
        self._setup_ui()
        self._setup_animation()

    def _setup_window(self):
        """Configure window properties."""
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool  # Don't show in taskbar
        )
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Position at top center of primary screen
        screen = QApplication.primaryScreen()
        screen_geo = screen.availableGeometry()

        x = screen_geo.x() + (screen_geo.width() - self.DRAWER_WIDTH) // 2
        y = screen_geo.y() - self.DRAWER_HEIGHT  # Start hidden above screen

        self.setGeometry(x, y, self.DRAWER_WIDTH, self.DRAWER_HEIGHT)
        self._hidden_y = y
        self._visible_y = screen_geo.y()

    def _setup_ui(self):
        """Set up the drawer UI."""
        # Main container with rounded corners
        self.setStyleSheet("""
            DrawerWindow {
                background-color: #1e1e1e;
                border-bottom-left-radius: 12px;
                border-bottom-right-radius: 12px;
            }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 12)
        layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setStyleSheet("""
            QFrame {
                background-color: #252525;
                border-bottom: 1px solid #333333;
            }
        """)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 12, 16, 12)

        title = QLabel("Clipboard")
        title.setStyleSheet("font-size: 14px; font-weight: bold; color: #ffffff;")
        header_layout.addWidget(title)

        header_layout.addStretch()

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("font-size: 12px; color: #888888;")
        header_layout.addWidget(self.status_label)

        layout.addWidget(header)

        # Content area - list of transfers
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                border: none;
                padding: 8px;
            }
            QListWidget::item {
                background-color: transparent;
                border: none;
                padding: 0px;
            }
            QListWidget::item:selected {
                background-color: transparent;
            }
        """)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list_widget.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)

        layout.addWidget(self.list_widget)

        # Empty state
        self.empty_label = QLabel("No items from other devices")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setStyleSheet("color: #666666; font-size: 13px; padding: 40px;")
        layout.addWidget(self.empty_label)

        self._update_empty_state()

    def _setup_animation(self):
        """Set up slide animation."""
        self._animation = QPropertyAnimation(self, b"pos")
        self._animation.setDuration(self.ANIMATION_DURATION)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def show_drawer(self):
        """Slide the drawer down into view."""
        if self._animation.state() == QPropertyAnimation.Running:
            return

        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(self.x(), self._visible_y))
        self._animation.start()
        self.show()
        self.activateWindow()

    def hide_drawer(self):
        """Slide the drawer up out of view."""
        if self._animation.state() == QPropertyAnimation.Running:
            return

        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(self.x(), self._hidden_y))
        self._animation.finished.connect(self._on_hide_complete)
        self._animation.start()

    def _on_hide_complete(self):
        """Called when hide animation completes."""
        self._animation.finished.disconnect(self._on_hide_complete)
        self.hide()

    def toggle_drawer(self):
        """Toggle drawer visibility."""
        if self.isVisible() and self.y() >= self._visible_y:
            self.hide_drawer()
        else:
            self.show_drawer()

    def focusOutEvent(self, event):
        """Hide drawer when it loses focus."""
        super().focusOutEvent(event)
        # Small delay to allow clicking on items
        QTimer.singleShot(100, self._check_focus)

    def _check_focus(self):
        """Check if we should hide the drawer."""
        if not self.isActiveWindow():
            self.hide_drawer()

    def _update_empty_state(self):
        """Show/hide empty state based on content."""
        has_items = self.list_widget.count() > 0
        self.empty_label.setVisible(not has_items)
        self.list_widget.setVisible(has_items)

    # ========== Slots for SyncSignals ==========

    def add_announced_files(self, transfer_id: str, metadata: TransferMetadata):
        """Add a new transfer to the list."""
        logger.info(f"Adding transfer to drawer: {transfer_id}")

        # Create item widget
        item_widget = TransferItemWidget(transfer_id, metadata)

        # Create list item
        list_item = QListWidgetItem(self.list_widget)
        list_item.setData(Qt.UserRole, transfer_id)
        list_item.setSizeHint(item_widget.sizeHint())

        self.list_widget.addItem(list_item)
        self.list_widget.setItemWidget(list_item, item_widget)

        self._transfers[transfer_id] = item_widget
        self._update_empty_state()

        # Show drawer if hidden
        if not self.isVisible():
            self.show_drawer()

    def update_transfer_progress(
        self, transfer_id: str, bytes_done: int, bytes_total: int, current_file: str
    ):
        """Update progress for a transfer."""
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            widget.set_status("downloading")
            widget.set_progress(bytes_done, bytes_total, current_file)

    def mark_transfer_complete(self, transfer_id: str, success: bool, file_paths: list):
        """Mark a transfer as complete."""
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            if success:
                widget.set_status("completed")
            else:
                widget.set_status("failed", "Download failed")

    def update_connection_status(self, connected: bool, peer_name: str):
        """Update the connection status display."""
        if connected:
            self.status_label.setText(f"Connected to {peer_name}")
            self.status_label.setStyleSheet("font-size: 12px; color: #4aff4a;")
        else:
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet("font-size: 12px; color: #888888;")

    # ========== User Actions ==========

    def _on_item_double_clicked(self, item: QListWidgetItem):
        """Handle double-click on an item."""
        transfer_id = item.data(Qt.UserRole)
        if transfer_id and transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            if widget.status == "pending":
                widget.set_status("downloading", "Starting...")
                self.signals.download_requested.emit(transfer_id)

    def _show_context_menu(self, pos):
        """Show context menu for an item."""
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        transfer_id = item.data(Qt.UserRole)
        if not transfer_id or transfer_id not in self._transfers:
            return

        widget = self._transfers[transfer_id]

        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #2d2d2d;
                border: 1px solid #3d3d3d;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 16px;
                color: #ffffff;
            }
            QMenu::item:selected {
                background-color: #3d3d3d;
            }
        """)

        if widget.status == "pending":
            download_action = menu.addAction("Download")
            download_action.triggered.connect(lambda: self._start_download(transfer_id))

            download_to_action = menu.addAction("Download to...")
            download_to_action.triggered.connect(lambda: self._download_to(transfer_id))

        menu.addSeparator()

        dismiss_action = menu.addAction("Dismiss")
        dismiss_action.triggered.connect(lambda: self._dismiss_transfer(transfer_id))

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _start_download(self, transfer_id: str):
        """Start downloading a transfer."""
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            widget.set_status("downloading", "Starting...")
            self.signals.download_requested.emit(transfer_id)

    def _download_to(self, transfer_id: str):
        """Download to a specific location."""
        from PySide6.QtWidgets import QFileDialog

        folder = QFileDialog.getExistingDirectory(self, "Download to", str(Path.home()))
        if folder:
            self.signals.download_to_requested.emit(transfer_id, folder)

    def _dismiss_transfer(self, transfer_id: str):
        """Remove a transfer from the list."""
        if transfer_id not in self._transfers:
            return

        # Find and remove the list item
        for i in range(self.list_widget.count()):
            item = self.list_widget.item(i)
            if item.data(Qt.UserRole) == transfer_id:
                self.list_widget.takeItem(i)
                break

        del self._transfers[transfer_id]
        self._update_empty_state()

        self.signals.dismiss_requested.emit(transfer_id)
