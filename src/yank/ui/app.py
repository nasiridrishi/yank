"""
DrawerApp - Main Qt application for the drawer UI

This module manages the Qt application lifecycle and integrates with
the existing SyncAgent for clipboard synchronization.
"""

import logging
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import QApplication

from yank import config
from yank.agent import SyncAgent
from yank.common.pairing import get_pairing_manager
from yank.common.protocol import TransferMetadata

from .drawer import DrawerWindow
from .signals import SyncSignals
from .tray import SystemTray

logger = logging.getLogger(__name__)


class SyncAgentWorker(QThread):
    """
    Worker thread that runs the SyncAgent.

    The SyncAgent uses blocking socket operations, so we run it
    in a separate thread to keep the Qt UI responsive.
    """

    def __init__(self, agent: SyncAgent, parent=None):
        super().__init__(parent)
        self.agent = agent
        self._running = False

    def run(self):
        """Start the agent and run until stopped."""
        self._running = True
        self.agent.start()

        # Keep thread alive while agent is running
        while self._running:
            self.msleep(100)

        self.agent.stop()

    def stop(self):
        """Signal the worker to stop."""
        self._running = False
        self.wait(5000)  # Wait up to 5 seconds


class DrawerApp(QObject):
    """
    Main application class for the drawer UI.

    Manages:
    - Qt application lifecycle
    - SyncAgent in background thread
    - System tray icon
    - Drawer window
    - Signal routing between sync agent and UI
    """

    def __init__(self, peer_ip: str = None, port: int = config.PORT, require_pairing: bool = True):
        super().__init__()

        self.peer_ip = peer_ip
        self.port = port
        self.require_pairing = require_pairing

        # Qt application (created in start())
        self._app: Optional[QApplication] = None

        # Signal bridge for thread-safe communication
        self.signals = SyncSignals()

        # UI components (created in start())
        self._tray: Optional[SystemTray] = None
        self._drawer: Optional[DrawerWindow] = None

        # Sync agent and worker thread
        self._agent: Optional[SyncAgent] = None
        self._agent_worker: Optional[SyncAgentWorker] = None

        # Pairing manager
        self._pairing_manager = get_pairing_manager()

    def start(self) -> int:
        """
        Start the drawer application.

        Returns:
            Exit code from Qt application
        """
        # Clean up old temp files
        config.cleanup_old_temp_files()

        # Create Qt application
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setApplicationName("Yank")
        self._app.setQuitOnLastWindowClosed(False)  # Keep running in tray

        # Create sync agent with callbacks that emit signals
        self._agent = SyncAgent(
            on_files_received=self._on_files_received,
            on_text_received=self._on_text_received,
            on_files_announced=self._on_files_announced,
            on_transfer_progress=self._on_transfer_progress,
            port=self.port,
            require_pairing=self.require_pairing,
        )

        if self.peer_ip:
            self._agent.set_peer(self.peer_ip, self.port)

        # Create UI components
        self._drawer = DrawerWindow(self.signals)
        self._tray = SystemTray(self._app, self._drawer, self.signals)

        # Connect signals to UI updates
        self._connect_signals()

        # Start sync agent in background thread
        self._agent_worker = SyncAgentWorker(self._agent)
        self._agent_worker.start()

        # Show tray icon
        self._tray.show()

        # Update connection status
        self._update_status()

        # Start periodic connection check
        self._connection_timer = QTimer()
        self._connection_timer.timeout.connect(self._check_connection)
        self._connection_timer.start(5000)  # Check every 5 seconds

        logger.info("Drawer app started")

        # Run Qt event loop
        return self._app.exec()

    def stop(self):
        """Stop the application."""
        logger.info("Stopping drawer app...")

        # Stop agent worker
        if self._agent_worker:
            self._agent_worker.stop()

        # Quit Qt app
        if self._app:
            self._app.quit()

        logger.info("Drawer app stopped")

    def _connect_signals(self):
        """Connect sync signals to UI slots."""
        # File announcements -> add to drawer
        self.signals.files_announced.connect(self._drawer.add_announced_files)

        # Files received directly -> add to drawer as completed
        self.signals.files_received.connect(self._drawer.add_received_files)

        # Transfer progress -> update progress bar
        self.signals.transfer_progress.connect(self._drawer.update_transfer_progress)

        # Transfer complete -> update item state
        self.signals.transfer_complete.connect(self._drawer.mark_transfer_complete)

        # Connection status -> update tray icon AND drawer
        self.signals.connection_changed.connect(self._tray.update_connection_status)
        self.signals.connection_changed.connect(self._drawer.update_connection_status)

        # Download requests from UI -> agent
        self.signals.download_requested.connect(self._handle_download_request)

    def _update_status(self):
        """Update connection status in UI based on pairing."""
        is_paired = self._pairing_manager.is_paired()
        if is_paired:
            paired_device = self._pairing_manager.get_paired_device()
            device_name = paired_device.device_name if paired_device else "Unknown"
            self.signals.connection_changed.emit(True, device_name)
        else:
            self.signals.connection_changed.emit(False, "")

    def _check_connection(self):
        """Periodically check if peer is reachable."""
        if not self._agent:
            return

        # Check pairing status
        is_paired = self._pairing_manager.is_paired()
        if not is_paired:
            self.signals.connection_changed.emit(False, "")
            return

        paired_device = self._pairing_manager.get_paired_device()
        device_name = paired_device.device_name if paired_device else "Unknown"

        # Check if we have a peer IP (either from discovery or manual setting)
        peer_ip = None
        with self._agent._lock:
            peer_ip = self._agent._peer_ip

        if not peer_ip:
            # Try to get from discovery
            from yank.common.discovery import get_discovery

            discovery = get_discovery()
            if discovery:
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip = peer[0]

        if peer_ip:
            self.signals.connection_changed.emit(True, device_name)
        else:
            self.signals.connection_changed.emit(False, "")

    # ========== Callbacks from SyncAgent (run in agent thread) ==========

    def _on_files_received(self, file_paths: list):
        """Called when files are received (small files, direct transfer)."""
        # For small files, they're already downloaded
        # Show them in the drawer as completed
        import uuid

        transfer_id = str(uuid.uuid4())
        logger.info(f"=== FILES RECEIVED ===")
        logger.info(f"Count: {len(file_paths)}")
        for p in file_paths:
            logger.info(f"  - {p}")
        logger.info(f"Emitting files_received signal with transfer_id: {transfer_id}")
        self.signals.files_received.emit(transfer_id, file_paths)

    def _on_text_received(self, text: str):
        """Called when text is received."""
        logger.info(f"Received text ({len(text)} chars)")
        # Text sync can remain automatic (no drawer needed)

    def _on_files_announced(self, transfer_id: str, metadata: TransferMetadata):
        """Called when files are announced (lazy transfer)."""
        logger.info(f"=== FILES ANNOUNCED ===")
        logger.info(f"Transfer ID: {transfer_id}")
        logger.info(f"File count: {len(metadata.files)}")
        for f in metadata.files:
            logger.info(f"  - {f.name} ({f.size} bytes)")
        logger.info(f"Emitting files_announced signal")
        self.signals.files_announced.emit(transfer_id, metadata)

    def _on_transfer_progress(
        self, transfer_id: str, bytes_done: int, bytes_total: int, current_file: str
    ):
        """Called during file transfer."""
        # Emit signal to update UI (thread-safe)
        self.signals.transfer_progress.emit(transfer_id, bytes_done, bytes_total, current_file)

    # ========== UI Action Handlers ==========

    def _handle_download_request(self, transfer_id: str):
        """Handle download request from UI."""

        # Run download in a thread to avoid blocking UI
        def do_download():
            try:
                downloaded = self._agent.request_transfer(transfer_id)
                if downloaded:
                    self.signals.transfer_complete.emit(transfer_id, True, downloaded)
                else:
                    self.signals.transfer_complete.emit(transfer_id, False, [])
            except Exception as e:
                logger.error(f"Download failed: {e}")
                self.signals.transfer_complete.emit(transfer_id, False, [])

        thread = threading.Thread(target=do_download, daemon=True)
        thread.start()
