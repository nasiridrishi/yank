"""
DrawerWindow - Slide-down drawer window for file blueprints.

This window slides down from the top of the screen when activated,
showing announced files from other devices.
"""

import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from PySide6.QtCore import (
    Property,
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QCursor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPainterPath,
    QPen,
    QScreen,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from yank.common.chunked_transfer import format_bytes
from yank.common.protocol import TransferMetadata

from .signals import SyncSignals

logger = logging.getLogger(__name__)


# File type icons (simple unicode representations)
FILE_ICONS = {
    # Documents
    ".pdf": "PDF",
    ".doc": "DOC",
    ".docx": "DOC",
    ".txt": "TXT",
    ".rtf": "RTF",
    ".md": "MD",
    # Spreadsheets
    ".xls": "XLS",
    ".xlsx": "XLS",
    ".csv": "CSV",
    # Images
    ".png": "IMG",
    ".jpg": "IMG",
    ".jpeg": "IMG",
    ".gif": "GIF",
    ".svg": "SVG",
    ".webp": "IMG",
    # Videos
    ".mp4": "VID",
    ".mov": "VID",
    ".avi": "VID",
    ".mkv": "VID",
    # Audio
    ".mp3": "MP3",
    ".wav": "WAV",
    ".flac": "AUD",
    # Archives
    ".zip": "ZIP",
    ".rar": "RAR",
    ".7z": "7Z",
    ".tar": "TAR",
    ".gz": "GZ",
    # Code
    ".py": "PY",
    ".js": "JS",
    ".ts": "TS",
    ".html": "HTM",
    ".css": "CSS",
    ".json": "JSN",
    ".xml": "XML",
    # Executables
    ".exe": "EXE",
    ".app": "APP",
    ".dmg": "DMG",
    ".msi": "MSI",
}


def get_file_type_label(filename: str) -> str:
    """Get a short label for the file type."""
    ext = Path(filename).suffix.lower()
    return FILE_ICONS.get(ext, "FILE")


def get_file_type_color(filename: str) -> str:
    """Get a color for the file type badge."""
    ext = Path(filename).suffix.lower()

    # Color categories
    if ext in [".pdf"]:
        return "#e74c3c"  # Red
    elif ext in [".doc", ".docx", ".txt", ".rtf", ".md"]:
        return "#3498db"  # Blue
    elif ext in [".xls", ".xlsx", ".csv"]:
        return "#27ae60"  # Green
    elif ext in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"]:
        return "#9b59b6"  # Purple
    elif ext in [".mp4", ".mov", ".avi", ".mkv"]:
        return "#e67e22"  # Orange
    elif ext in [".mp3", ".wav", ".flac"]:
        return "#1abc9c"  # Teal
    elif ext in [".zip", ".rar", ".7z", ".tar", ".gz"]:
        return "#f39c12"  # Yellow
    elif ext in [".py", ".js", ".ts", ".html", ".css", ".json", ".xml"]:
        return "#2ecc71"  # Light green
    elif ext in [".exe", ".app", ".dmg", ".msi"]:
        return "#95a5a6"  # Gray
    else:
        return "#7f8c8d"  # Default gray


class FileTypeBadge(QWidget):
    """A small badge showing the file type."""

    def __init__(self, filename: str, parent=None):
        super().__init__(parent)
        self.label = get_file_type_label(filename)
        self.color = get_file_type_color(filename)
        self.setFixedSize(36, 36)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Draw rounded rectangle background
        path = QPainterPath()
        path.addRoundedRect(0, 0, 36, 36, 6, 6)
        painter.fillPath(path, QColor(self.color))

        # Draw text
        painter.setPen(QColor("#ffffff"))
        font = QFont()
        font.setPixelSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(self.rect(), Qt.AlignCenter, self.label)


class TransferItemWidget(QFrame):
    """
    Widget for displaying a transfer (group of files) in the list.
    Modern card-style design with file type badges.
    """

    def __init__(
        self, transfer_id: str, metadata: TransferMetadata, signals: SyncSignals, parent=None
    ):
        super().__init__(parent)
        self.transfer_id = transfer_id
        self.metadata = metadata
        self.signals = signals
        self.status = "pending"
        self._hover = False
        self.timestamp = datetime.now()

        self.setMouseTracking(True)
        self._setup_ui()

    def _setup_ui(self):
        """Set up the widget UI."""
        self.setFixedHeight(90)
        self.setCursor(Qt.PointingHandCursor)
        self._update_style()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        # File type badge
        file_count = len(self.metadata.files)
        if file_count == 1:
            badge = FileTypeBadge(self.metadata.files[0].name)
        else:
            # For multiple files, show a folder-like badge
            badge = QWidget()
            badge.setFixedSize(36, 36)
            badge_layout = QVBoxLayout(badge)
            badge_layout.setContentsMargins(0, 0, 0, 0)
            badge_label = QLabel(str(file_count))
            badge_label.setAlignment(Qt.AlignCenter)
            badge_label.setStyleSheet("""
                background-color: #5865f2;
                color: white;
                font-weight: bold;
                font-size: 14px;
                border-radius: 6px;
            """)
            badge_label.setFixedSize(36, 36)
            badge_layout.addWidget(badge_label)

        layout.addWidget(badge)

        # Content area
        content = QVBoxLayout()
        content.setSpacing(4)

        # Title row
        title_row = QHBoxLayout()
        title_row.setSpacing(8)

        if file_count == 1:
            title_text = self.metadata.files[0].name
            # Truncate long names
            if len(title_text) > 35:
                title_text = title_text[:32] + "..."
        else:
            title_text = f"{file_count} files"

        self.title_label = QLabel(title_text)
        self.title_label.setStyleSheet("""
            font-weight: 600;
            font-size: 13px;
            color: #e4e4e7;
        """)
        title_row.addWidget(self.title_label)
        title_row.addStretch()

        self.size_label = QLabel(format_bytes(self.metadata.total_size))
        self.size_label.setStyleSheet("""
            font-size: 12px;
            color: #71717a;
            font-weight: 500;
        """)
        title_row.addWidget(self.size_label)

        content.addLayout(title_row)

        # File list preview (for multi-file)
        if file_count > 1:
            files_text = ", ".join(f.name for f in self.metadata.files[:3])
            if file_count > 3:
                files_text += f" +{file_count - 3} more"
            if len(files_text) > 50:
                files_text = files_text[:47] + "..."

            files_label = QLabel(files_text)
            files_label.setStyleSheet("""
                font-size: 11px;
                color: #52525b;
            """)
            content.addWidget(files_label)

        # Bottom row: status and time
        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(8)

        self.status_label = QLabel("Ready to download")
        self.status_label.setStyleSheet("""
            font-size: 11px;
            color: #3b82f6;
            font-weight: 500;
        """)
        bottom_row.addWidget(self.status_label)

        bottom_row.addStretch()

        self.time_label = QLabel("just now")
        self.time_label.setStyleSheet("""
            font-size: 11px;
            color: #52525b;
        """)
        bottom_row.addWidget(self.time_label)

        content.addLayout(bottom_row)

        # Progress bar (hidden by default)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(3)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #27272a;
                border-radius: 1px;
            }
            QProgressBar::chunk {
                background-color: #3b82f6;
                border-radius: 1px;
            }
        """)
        content.addWidget(self.progress_bar)

        layout.addLayout(content, 1)

    def _update_style(self):
        """Update the widget style based on state."""
        if self.status == "completed":
            bg = "#162415" if self._hover else "#0f1f0e"
            border = "#22c55e"
        elif self.status == "failed":
            bg = "#2a1515" if self._hover else "#1f0f0f"
            border = "#ef4444"
        elif self.status == "downloading":
            bg = "#1a1a2e" if self._hover else "#151525"
            border = "#3b82f6"
        else:
            bg = "#27272a" if self._hover else "#1f1f23"
            border = "#3f3f46" if self._hover else "#27272a"

        self.setStyleSheet(f"""
            TransferItemWidget {{
                background-color: {bg};
                border: 1px solid {border};
                border-radius: 10px;
            }}
        """)

    def enterEvent(self, event):
        self._hover = True
        self._update_style()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._update_style()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.status == "pending":
            self.set_status("downloading", "Starting...")
            self.signals.download_requested.emit(self.transfer_id)
        super().mousePressEvent(event)

    def set_status(self, status: str, message: str = None):
        """Update status display."""
        self.status = status
        self._update_style()

        if status == "pending":
            self.status_label.setText("Ready to download")
            self.status_label.setStyleSheet("font-size: 11px; color: #3b82f6; font-weight: 500;")
            self.progress_bar.setVisible(False)
            self.setCursor(Qt.PointingHandCursor)
        elif status == "downloading":
            self.status_label.setText(message or "Downloading...")
            self.status_label.setStyleSheet("font-size: 11px; color: #3b82f6; font-weight: 500;")
            self.progress_bar.setVisible(True)
            self.setCursor(Qt.ArrowCursor)
        elif status == "completed":
            self.status_label.setText("Downloaded")
            self.status_label.setStyleSheet("font-size: 11px; color: #22c55e; font-weight: 500;")
            self.progress_bar.setVisible(False)
            self.setCursor(Qt.ArrowCursor)
        elif status == "failed":
            self.status_label.setText(message or "Failed - click to retry")
            self.status_label.setStyleSheet("font-size: 11px; color: #ef4444; font-weight: 500;")
            self.progress_bar.setVisible(False)
            self.setCursor(Qt.PointingHandCursor)

    def set_progress(self, bytes_done: int, bytes_total: int, current_file: str):
        """Update progress."""
        if bytes_total > 0:
            percent = int(bytes_done / bytes_total * 100)
            self.progress_bar.setValue(percent)

            # Show speed estimate
            done_str = format_bytes(bytes_done)
            total_str = format_bytes(bytes_total)
            self.status_label.setText(f"{percent}% - {done_str} / {total_str}")


class DrawerWindow(QWidget):
    """
    The main drawer window that slides down from the top of the screen.
    Modern dark theme with smooth animations.
    """

    DRAWER_WIDTH = 380
    DRAWER_HEIGHT = 450
    ANIMATION_DURATION = 200

    def __init__(self, signals: SyncSignals, parent=None):
        super().__init__(parent)
        self.signals = signals

        # Track transfers
        self._transfers: Dict[str, TransferItemWidget] = {}
        self._items: Dict[str, QListWidgetItem] = {}

        self._setup_window()
        self._setup_ui()
        self._setup_animation()

    def _setup_window(self):
        """Configure window properties."""
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)

        # Position at top center of primary screen
        screen = QApplication.primaryScreen()
        screen_geo = screen.availableGeometry()

        x = screen_geo.x() + (screen_geo.width() - self.DRAWER_WIDTH) // 2
        y = screen_geo.y() - self.DRAWER_HEIGHT

        self.setGeometry(x, y, self.DRAWER_WIDTH, self.DRAWER_HEIGHT)
        self._hidden_y = y
        self._visible_y = screen_geo.y()

    def _setup_ui(self):
        """Set up the drawer UI."""
        # Main container
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Container widget for styling
        container = QFrame()
        container.setObjectName("drawerContainer")
        container.setStyleSheet("""
            #drawerContainer {
                background-color: #18181b;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
                border: 1px solid #27272a;
                border-top: none;
            }
        """)

        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)

        # Header
        header = QFrame()
        header.setFixedHeight(52)
        header.setStyleSheet("""
            QFrame {
                background-color: #18181b;
                border-bottom: 1px solid #27272a;
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
            }
        """)

        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 0, 16, 0)

        # App icon/title
        title = QLabel("Yank")
        title.setStyleSheet("""
            font-size: 15px;
            font-weight: 700;
            color: #fafafa;
            letter-spacing: -0.3px;
        """)
        header_layout.addWidget(title)

        header_layout.addStretch()

        # Connection status indicator
        self.status_dot = QLabel()
        self.status_dot.setFixedSize(8, 8)
        self.status_dot.setStyleSheet("""
            background-color: #71717a;
            border-radius: 4px;
        """)
        header_layout.addWidget(self.status_dot)

        self.status_label = QLabel("Not connected")
        self.status_label.setStyleSheet("""
            font-size: 12px;
            color: #71717a;
            margin-left: 6px;
        """)
        header_layout.addWidget(self.status_label)

        container_layout.addWidget(header)

        # Content area
        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(8)

        # List widget
        self.list_widget = QListWidget()
        self.list_widget.setStyleSheet("""
            QListWidget {
                background-color: transparent;
                border: none;
                outline: none;
            }
            QListWidget::item {
                background-color: transparent;
                border: none;
                padding: 4px 0px;
            }
            QListWidget::item:selected {
                background-color: transparent;
            }
            QListWidget::item:hover {
                background-color: transparent;
            }
        """)
        self.list_widget.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list_widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_widget.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.list_widget.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._show_context_menu)

        # Scrollbar styling
        self.list_widget.verticalScrollBar().setStyleSheet("""
            QScrollBar:vertical {
                background-color: transparent;
                width: 6px;
                margin: 0px;
            }
            QScrollBar::handle:vertical {
                background-color: #3f3f46;
                border-radius: 3px;
                min-height: 30px;
            }
            QScrollBar::handle:vertical:hover {
                background-color: #52525b;
            }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
                height: 0px;
            }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {
                background-color: transparent;
            }
        """)

        content_layout.addWidget(self.list_widget)

        # Empty state
        self.empty_widget = QWidget()
        empty_layout = QVBoxLayout(self.empty_widget)
        empty_layout.setAlignment(Qt.AlignCenter)

        empty_icon = QLabel("()")
        empty_icon.setStyleSheet("""
            font-size: 32px;
            color: #3f3f46;
        """)
        empty_icon.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_icon)

        empty_title = QLabel("No items yet")
        empty_title.setStyleSheet("""
            font-size: 14px;
            font-weight: 600;
            color: #71717a;
            margin-top: 8px;
        """)
        empty_title.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_title)

        empty_desc = QLabel("Files copied on your paired device\nwill appear here")
        empty_desc.setStyleSheet("""
            font-size: 12px;
            color: #52525b;
            margin-top: 4px;
        """)
        empty_desc.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_desc)

        content_layout.addWidget(self.empty_widget)

        container_layout.addWidget(content_widget, 1)

        # Footer with drag handle
        footer = QFrame()
        footer.setFixedHeight(20)
        footer.setStyleSheet("background-color: transparent;")

        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 0, 0, 8)
        footer_layout.setAlignment(Qt.AlignCenter)

        drag_handle = QLabel()
        drag_handle.setFixedSize(40, 4)
        drag_handle.setStyleSheet("""
            background-color: #3f3f46;
            border-radius: 2px;
        """)
        footer_layout.addWidget(drag_handle)

        container_layout.addWidget(footer)

        main_layout.addWidget(container)

        # Add shadow effect
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(30)
        shadow.setXOffset(0)
        shadow.setYOffset(4)
        shadow.setColor(QColor(0, 0, 0, 80))
        container.setGraphicsEffect(shadow)

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
        self.raise_()
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
        try:
            self._animation.finished.disconnect(self._on_hide_complete)
        except RuntimeError:
            pass
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
        QTimer.singleShot(150, self._check_focus)

    def _check_focus(self):
        """Check if we should hide the drawer."""
        if not self.isActiveWindow():
            self.hide_drawer()

    def _update_empty_state(self):
        """Show/hide empty state based on content."""
        has_items = self.list_widget.count() > 0
        self.empty_widget.setVisible(not has_items)
        self.list_widget.setVisible(has_items)

    # ========== Slots for SyncSignals ==========

    def add_announced_files(self, transfer_id: str, metadata: TransferMetadata):
        """Add a new transfer to the list."""
        logger.info(f"Adding transfer to drawer: {transfer_id}")

        # Create item widget
        item_widget = TransferItemWidget(transfer_id, metadata, self.signals)

        # Create list item
        list_item = QListWidgetItem(self.list_widget)
        list_item.setData(Qt.UserRole, transfer_id)
        list_item.setSizeHint(QSize(self.DRAWER_WIDTH - 40, 98))

        self.list_widget.insertItem(0, list_item)  # Add at top
        self.list_widget.setItemWidget(list_item, item_widget)

        self._transfers[transfer_id] = item_widget
        self._items[transfer_id] = list_item
        self._update_empty_state()

        # Show drawer if hidden
        if not self.isVisible():
            self.show_drawer()

    def add_received_files(self, transfer_id: str, file_paths: list):
        """Add received files (already downloaded) to the list."""
        logger.info(f"Adding received files to drawer: {transfer_id}")

        # Create a fake metadata-like structure for the widget
        from yank.common.protocol import FileMetadata, TransferMetadata

        files = []
        total_size = 0
        for i, path in enumerate(file_paths):
            size = path.stat().st_size if path.exists() else 0
            total_size += size
            files.append(
                FileMetadata(
                    name=path.name, size=size, checksum="", file_index=i, relative_path=str(path)
                )
            )

        metadata = TransferMetadata(
            transfer_id=transfer_id,
            files=files,
            total_size=total_size,
            chunk_size=1024 * 1024,
            expires_at=0,
        )

        # Create item widget
        item_widget = TransferItemWidget(transfer_id, metadata, self.signals)
        item_widget.set_status("completed")  # Already downloaded

        # Create list item
        list_item = QListWidgetItem(self.list_widget)
        list_item.setData(Qt.UserRole, transfer_id)
        list_item.setSizeHint(QSize(self.DRAWER_WIDTH - 40, 98))

        self.list_widget.insertItem(0, list_item)  # Add at top
        self.list_widget.setItemWidget(list_item, item_widget)

        self._transfers[transfer_id] = item_widget
        self._items[transfer_id] = list_item
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
            self.status_dot.setStyleSheet("""
                background-color: #22c55e;
                border-radius: 4px;
            """)
            self.status_label.setText(peer_name)
            self.status_label.setStyleSheet("""
                font-size: 12px;
                color: #a1a1aa;
                margin-left: 6px;
            """)
        else:
            self.status_dot.setStyleSheet("""
                background-color: #71717a;
                border-radius: 4px;
            """)
            self.status_label.setText("Not connected")
            self.status_label.setStyleSheet("""
                font-size: 12px;
                color: #71717a;
                margin-left: 6px;
            """)

    # ========== Context Menu ==========

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
                background-color: #27272a;
                border: 1px solid #3f3f46;
                border-radius: 8px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 16px;
                color: #fafafa;
                border-radius: 4px;
                margin: 2px 4px;
            }
            QMenu::item:selected {
                background-color: #3f3f46;
            }
            QMenu::separator {
                height: 1px;
                background-color: #3f3f46;
                margin: 4px 8px;
            }
        """)

        if widget.status == "pending":
            download_action = menu.addAction("Download")
            download_action.triggered.connect(lambda: self._start_download(transfer_id))

            download_to_action = menu.addAction("Download to...")
            download_to_action.triggered.connect(lambda: self._download_to(transfer_id))

            menu.addSeparator()

        if widget.status == "completed":
            # Could add "Open" or "Show in folder" actions here
            pass

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

        if transfer_id in self._items:
            item = self._items[transfer_id]
            row = self.list_widget.row(item)
            if row >= 0:
                self.list_widget.takeItem(row)
            del self._items[transfer_id]

        del self._transfers[transfer_id]
        self._update_empty_state()

        self.signals.dismiss_requested.emit(transfer_id)
