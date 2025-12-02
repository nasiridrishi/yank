"""
DrawerWindow - Slide-down drawer window for file blueprints.

Native Explorer/Finder-like design for familiarity.
"""

import logging
import platform
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFileIconProvider,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from yank.common.chunked_transfer import format_bytes
from yank.common.protocol import TransferMetadata

from .signals import SyncSignals

logger = logging.getLogger(__name__)

# Detect system theme (simplified)
IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"


def get_file_icon(filename: str) -> QIcon:
    """Get system file icon for a filename."""
    provider = QFileIconProvider()
    # Create a temporary file info to get the icon
    suffix = Path(filename).suffix
    if suffix:
        # Get icon based on file type
        file_info = provider.icon(QFileIconProvider.File)
        return file_info
    return provider.icon(QFileIconProvider.File)


def format_time_ago(timestamp: float) -> str:
    """Format timestamp as relative time."""
    now = time.time()
    diff = now - timestamp

    if diff < 60:
        return "Just now"
    elif diff < 3600:
        mins = int(diff / 60)
        return f"{mins}m ago"
    elif diff < 86400:
        hours = int(diff / 3600)
        return f"{hours}h ago"
    else:
        days = int(diff / 86400)
        return f"{days}d ago"


class FileRowWidget(QFrame):
    """
    A single file row in Explorer/Finder style.

    Layout: [Icon] [Filename + Size] [Status] [Time]
    """

    def __init__(
        self, transfer_id: str, metadata: TransferMetadata, signals: SyncSignals, parent=None
    ):
        super().__init__(parent)
        self.transfer_id = transfer_id
        self.metadata = metadata
        self.signals = signals
        self.status = "pending"
        self.timestamp = time.time()
        self._selected = False

        self.setMouseTracking(True)
        self._setup_ui()
        self._apply_style()

    def _setup_ui(self):
        self.setFixedHeight(52)
        self.setCursor(Qt.PointingHandCursor)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)

        # File icon
        icon_label = QLabel()
        icon_label.setFixedSize(32, 32)

        # Get appropriate icon
        file_count = len(self.metadata.files)
        if file_count == 1:
            # Single file - show file icon
            icon = QApplication.style().standardIcon(QStyle.SP_FileIcon)
        else:
            # Multiple files - show folder icon
            icon = QApplication.style().standardIcon(QStyle.SP_DirIcon)

        icon_label.setPixmap(icon.pixmap(32, 32))
        layout.addWidget(icon_label)

        # File info (name + details)
        info_layout = QVBoxLayout()
        info_layout.setSpacing(2)
        info_layout.setContentsMargins(0, 0, 0, 0)

        # Filename
        if file_count == 1:
            name = self.metadata.files[0].name
            if len(name) > 40:
                name = name[:37] + "..."
        else:
            name = f"{file_count} items"

        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-size: 13px; font-weight: 500;")
        info_layout.addWidget(self.name_label)

        # Size and file list preview
        size_text = format_bytes(self.metadata.total_size)
        if file_count > 1:
            preview = ", ".join(f.name for f in self.metadata.files[:2])
            if file_count > 2:
                preview += f", +{file_count - 2} more"
            if len(preview) > 45:
                preview = preview[:42] + "..."
            size_text = f"{size_text} - {preview}"

        self.detail_label = QLabel(size_text)
        self.detail_label.setStyleSheet("font-size: 11px; color: #666666;")
        info_layout.addWidget(self.detail_label)

        layout.addLayout(info_layout, 1)

        # Status indicator
        self.status_label = QLabel()
        self.status_label.setFixedWidth(90)
        self.status_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._update_status_label()
        layout.addWidget(self.status_label)

        # Progress bar (hidden initially, overlays status when downloading)
        self.progress_bar = QProgressBar()
        self.progress_bar.setFixedSize(80, 4)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #e0e0e0;
                border-radius: 2px;
            }
            QProgressBar::chunk {
                background-color: #0078d4;
                border-radius: 2px;
            }
        """)

        # Time
        self.time_label = QLabel(format_time_ago(self.timestamp))
        self.time_label.setFixedWidth(50)
        self.time_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.time_label.setStyleSheet("font-size: 11px; color: #888888;")
        layout.addWidget(self.time_label)

    def _apply_style(self):
        """Apply native-like styling."""
        if self._selected:
            bg = "#0078d4" if IS_WINDOWS else "#0063cc"
            self.name_label.setStyleSheet("font-size: 13px; font-weight: 500; color: white;")
            self.detail_label.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.8);")
            self.time_label.setStyleSheet("font-size: 11px; color: rgba(255,255,255,0.8);")
        else:
            bg = "#ffffff"
            self.name_label.setStyleSheet("font-size: 13px; font-weight: 500; color: #1a1a1a;")
            self.detail_label.setStyleSheet("font-size: 11px; color: #666666;")
            self.time_label.setStyleSheet("font-size: 11px; color: #888888;")

        self.setStyleSheet(f"""
            FileRowWidget {{
                background-color: {bg};
                border-bottom: 1px solid #e5e5e5;
            }}
            FileRowWidget:hover {{
                background-color: {"#e5f3ff" if not self._selected else bg};
            }}
        """)

    def _update_status_label(self):
        """Update the status text and style."""
        if self.status == "pending":
            self.status_label.setText("Download")
            self.status_label.setStyleSheet("font-size: 12px; color: #0078d4; font-weight: 500;")
            self.progress_bar.setVisible(False)
        elif self.status == "downloading":
            self.status_label.setText("")
            self.progress_bar.setVisible(True)
        elif self.status == "completed":
            self.status_label.setText("Ready")
            self.status_label.setStyleSheet("font-size: 12px; color: #107c10; font-weight: 500;")
            self.progress_bar.setVisible(False)
        elif self.status == "failed":
            self.status_label.setText("Failed")
            self.status_label.setStyleSheet("font-size: 12px; color: #d83b01; font-weight: 500;")
            self.progress_bar.setVisible(False)

    def set_status(self, status: str, message: str = None):
        """Update status."""
        self.status = status
        self._update_status_label()

        if status == "pending":
            self.setCursor(Qt.PointingHandCursor)
        elif status == "downloading":
            self.setCursor(Qt.ArrowCursor)
        elif status == "completed":
            self.setCursor(Qt.ArrowCursor)
        elif status == "failed":
            self.setCursor(Qt.PointingHandCursor)  # Can retry

    def set_progress(self, bytes_done: int, bytes_total: int, current_file: str):
        """Update download progress."""
        if bytes_total > 0:
            percent = int(bytes_done / bytes_total * 100)
            self.progress_bar.setValue(percent)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.status == "pending":
                self.set_status("downloading")
                self.signals.download_requested.emit(self.transfer_id)
            elif self.status == "failed":
                # Retry
                self.set_status("downloading")
                self.signals.download_requested.emit(self.transfer_id)
        super().mousePressEvent(event)

    def enterEvent(self, event):
        self._apply_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._apply_style()
        super().leaveEvent(event)


class DrawerWindow(QWidget):
    """
    Native-feeling drawer window with Explorer/Finder-like file list.
    """

    DRAWER_WIDTH = 400
    DRAWER_HEIGHT = 380
    ANIMATION_DURATION = 200

    def __init__(self, signals: SyncSignals, parent=None):
        super().__init__(parent)
        self.signals = signals
        self._transfers: Dict[str, FileRowWidget] = {}
        self._items: Dict[str, QListWidgetItem] = {}

        self._setup_window()
        self._setup_ui()
        self._setup_animation()

    def _setup_window(self):
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        screen = QApplication.primaryScreen()
        screen_geo = screen.availableGeometry()

        x = screen_geo.x() + (screen_geo.width() - self.DRAWER_WIDTH) // 2
        y = screen_geo.y() - self.DRAWER_HEIGHT

        self.setGeometry(x, y, self.DRAWER_WIDTH, self.DRAWER_HEIGHT)
        self._hidden_y = y
        self._visible_y = screen_geo.y()

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Main container with shadow
        container = QFrame()
        container.setObjectName("container")
        container.setStyleSheet("""
            #container {
                background-color: #ffffff;
                border: 1px solid #d0d0d0;
                border-top: none;
                border-bottom-left-radius: 8px;
                border-bottom-right-radius: 8px;
            }
        """)

        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Header bar
        header = QFrame()
        header.setFixedHeight(44)
        header.setStyleSheet("""
            QFrame {
                background-color: #f8f8f8;
                border-bottom: 1px solid #e0e0e0;
            }
        """)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(14, 0, 14, 0)

        # Title
        title = QLabel("Clipboard")
        title.setStyleSheet("""
            font-size: 13px;
            font-weight: 600;
            color: #1a1a1a;
        """)
        header_layout.addWidget(title)

        header_layout.addStretch()

        # Connection status
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setStyleSheet("""
            background-color: #888888;
            border-radius: 4px;
        """)
        header_layout.addWidget(self.status_dot)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("""
            font-size: 12px;
            color: #666666;
            margin-left: 6px;
        """)
        header_layout.addWidget(self.status_label)

        container_layout.addWidget(header)

        # File list
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget {
                background-color: #ffffff;
                border: none;
                outline: none;
            }
            QListWidget::item {
                padding: 0px;
                border: none;
            }
            QListWidget::item:selected {
                background-color: transparent;
            }
            QScrollBar:vertical {
                background-color: #f5f5f5;
                width: 8px;
                border: none;
            }
            QScrollBar::handle:vertical {
                background-color: #c0c0c0;
                border-radius: 4px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #a0a0a0;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
        """)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)

        container_layout.addWidget(self.list_widget, 1)

        # Empty state
        self.empty_widget = QWidget()
        self.empty_widget.setStyleSheet("background-color: #ffffff;")
        empty_layout = QVBoxLayout(self.empty_widget)
        empty_layout.setAlignment(Qt.AlignCenter)

        # Empty icon
        empty_icon = QLabel()
        empty_icon.setPixmap(QApplication.style().standardIcon(QStyle.SP_FileIcon).pixmap(48, 48))
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_icon.setStyleSheet("opacity: 0.3;")
        empty_layout.addWidget(empty_icon)

        empty_title = QLabel("No files")
        empty_title.setStyleSheet("""
            font-size: 14px;
            font-weight: 500;
            color: #666666;
            margin-top: 12px;
        """)
        empty_title.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_title)

        empty_desc = QLabel("Files from your paired device\nwill appear here")
        empty_desc.setStyleSheet("""
            font-size: 12px;
            color: #888888;
            margin-top: 4px;
        """)
        empty_desc.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_desc)

        container_layout.addWidget(self.empty_widget)

        # Bottom drag handle
        handle = QFrame()
        handle.setFixedHeight(16)
        handle.setStyleSheet("background-color: #f8f8f8;")
        handle_layout = QHBoxLayout(handle)
        handle_layout.setContentsMargins(0, 4, 0, 8)
        handle_layout.setAlignment(Qt.AlignCenter)

        drag_bar = QLabel()
        drag_bar.setFixedSize(36, 4)
        drag_bar.setStyleSheet("""
            background-color: #d0d0d0;
            border-radius: 2px;
        """)
        handle_layout.addWidget(drag_bar)

        container_layout.addWidget(handle)

        main_layout.addWidget(container)
        self._update_empty_state()

    def _setup_animation(self):
        self._animation = QPropertyAnimation(self, b"pos")
        self._animation.setDuration(self.ANIMATION_DURATION)
        self._animation.setEasingCurve(QEasingCurve.OutCubic)

    def show_drawer(self):
        if self._animation.state() == QPropertyAnimation.Running:
            return
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(self.x(), self._visible_y))
        self._animation.start()
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_drawer(self):
        if self._animation.state() == QPropertyAnimation.Running:
            return
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QPoint(self.x(), self._hidden_y))
        self._animation.finished.connect(self._on_hide_complete)
        self._animation.start()

    def _on_hide_complete(self):
        try:
            self._animation.finished.disconnect(self._on_hide_complete)
        except RuntimeError:
            pass
        self.hide()

    def toggle_drawer(self):
        if self.isVisible() and self.y() >= self._visible_y:
            self.hide_drawer()
        else:
            self.show_drawer()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        QTimer.singleShot(150, self._check_focus)

    def _check_focus(self):
        if not self.isActiveWindow():
            self.hide_drawer()

    def _update_empty_state(self):
        has_items = self.list_widget.count() > 0
        self.empty_widget.setVisible(not has_items)
        self.list_widget.setVisible(has_items)

    # ========== Public Slots ==========

    def add_announced_files(self, transfer_id: str, metadata: TransferMetadata):
        """Add announced files (pending download)."""
        logger.info(f"Adding announced files to drawer: {transfer_id}")
        self._add_transfer(transfer_id, metadata, "pending")

    def add_received_files(self, transfer_id: str, file_paths: list):
        """Add received files (already downloaded)."""
        logger.info(f"Adding received files to drawer: {transfer_id}")

        from yank.common.protocol import FileInfo, TransferMetadata

        files = []
        total_size = 0
        for i, path in enumerate(file_paths):
            size = path.stat().st_size if path.exists() else 0
            total_size += size
            files.append(
                FileInfo(
                    name=path.name, size=size, checksum="", file_index=i, relative_path=str(path)
                )
            )

        metadata = TransferMetadata(
            transfer_id=transfer_id,
            files=files,
            total_size=total_size,
            timestamp=time.time(),
            source_os=platform.system().lower(),
            chunk_size=1024 * 1024,
            expires_at=0,
        )

        self._add_transfer(transfer_id, metadata, "completed")

    def _add_transfer(self, transfer_id: str, metadata: TransferMetadata, status: str):
        """Add a transfer to the list."""
        widget = FileRowWidget(transfer_id, metadata, self.signals)
        widget.set_status(status)

        list_item = QListWidgetItem(self.list_widget)
        list_item.setData(Qt.UserRole, transfer_id)
        list_item.setSizeHint(QSize(self.DRAWER_WIDTH - 20, 52))

        self.list_widget.insertItem(0, list_item)
        self.list_widget.setItemWidget(list_item, widget)

        self._transfers[transfer_id] = widget
        self._items[transfer_id] = list_item
        self._update_empty_state()

        if not self.isVisible():
            self.show_drawer()

    def update_transfer_progress(
        self, transfer_id: str, bytes_done: int, bytes_total: int, current_file: str
    ):
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            widget.set_status("downloading")
            widget.set_progress(bytes_done, bytes_total, current_file)

    def mark_transfer_complete(self, transfer_id: str, success: bool, file_paths: list):
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            widget.set_status("completed" if success else "failed")

    def update_connection_status(self, connected: bool, peer_name: str):
        if connected:
            self.status_dot.setStyleSheet("""
                background-color: #107c10;
                border-radius: 4px;
            """)
            self.status_label.setText(peer_name)
            self.status_label.setStyleSheet("""
                font-size: 12px;
                color: #107c10;
                margin-left: 6px;
            """)
        else:
            self.status_dot.setStyleSheet("""
                background-color: #888888;
                border-radius: 4px;
            """)
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet("""
                font-size: 12px;
                color: #666666;
                margin-left: 6px;
            """)

    # ========== Context Menu ==========

    def _show_context_menu(self, pos):
        item = self.list_widget.itemAt(pos)
        if not item:
            return

        transfer_id = item.data(Qt.UserRole)
        if not transfer_id or transfer_id not in self._transfers:
            return

        widget = self._transfers[transfer_id]

        menu = QMenu(self)
        # Use native styling

        if widget.status == "pending":
            download_action = menu.addAction("Download")
            download_action.triggered.connect(lambda: self._start_download(transfer_id))

            menu.addSeparator()

        if widget.status == "completed":
            # Could add "Open" or "Show in Folder" here
            open_action = menu.addAction("Open")
            open_action.setEnabled(False)  # TODO: implement

            show_action = menu.addAction("Show in Folder")
            show_action.setEnabled(False)  # TODO: implement

            menu.addSeparator()

        remove_action = menu.addAction("Remove")
        remove_action.triggered.connect(lambda: self._remove_transfer(transfer_id))

        menu.exec(self.list_widget.mapToGlobal(pos))

    def _start_download(self, transfer_id: str):
        if transfer_id in self._transfers:
            widget = self._transfers[transfer_id]
            widget.set_status("downloading")
            self.signals.download_requested.emit(transfer_id)

    def _remove_transfer(self, transfer_id: str):
        if transfer_id not in self._transfers:
            return

        if transfer_id in self._items:
            item = self._items[transfer_id]
            row = self.list_widget.row(item)
            if row >= 0:
                self.list_widget.takeItem(row)
            del self._items[transfer_id]

        del self._transfers[transfer_id]
        self._update_empty_state()
        self.signals.dismiss_requested.emit(transfer_id)
