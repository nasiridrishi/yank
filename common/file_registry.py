"""
File Registry for tracking lazy file transfers.

Tracks:
- announced_transfers: Files we can serve to peers (sender side)
- pending_transfers: Files we're waiting to receive (receiver side)
- active_transfers: Transfers currently in progress

Features:
- Thread-safe access
- Auto-cleanup of expired transfers (TTL-based)
- Transfer state management
"""
import time
import threading
import logging
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass, field

from common.protocol import TransferMetadata, FileInfo

logger = logging.getLogger(__name__)


class TransferStatus(Enum):
    """Status of a transfer"""
    ANNOUNCED = "announced"      # Metadata sent, waiting for request
    PENDING = "pending"          # Metadata received, waiting for user action
    REQUESTING = "requesting"    # Request sent, waiting for chunks
    TRANSFERRING = "transferring"  # Chunks being sent/received
    COMPLETED = "completed"      # Transfer finished successfully
    FAILED = "failed"           # Transfer failed
    CANCELLED = "cancelled"     # Transfer cancelled by user
    EXPIRED = "expired"         # Transfer expired (TTL exceeded)


@dataclass
class TransferInfo:
    """Information about a transfer"""
    transfer_id: str
    metadata: TransferMetadata
    status: TransferStatus

    # Sender-side info
    source_paths: Dict[int, Path] = field(default_factory=dict)  # file_index -> path

    # Receiver-side info
    dest_dir: Optional[Path] = None
    downloaded_files: List[Path] = field(default_factory=list)

    # Progress tracking
    bytes_transferred: int = 0
    current_file_index: int = 0
    current_chunk_index: int = 0

    # Timing
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    completed_at: float = 0.0

    # Error info
    error_message: str = ""

    @property
    def is_expired(self) -> bool:
        """Check if transfer has expired"""
        if self.metadata.expires_at == 0:
            return False
        return time.time() > self.metadata.expires_at

    @property
    def is_complete(self) -> bool:
        """Check if transfer is complete"""
        return self.status in (TransferStatus.COMPLETED, TransferStatus.FAILED,
                               TransferStatus.CANCELLED, TransferStatus.EXPIRED)

    @property
    def progress_percent(self) -> float:
        """Get completion percentage"""
        if self.metadata.total_size == 0:
            return 100.0
        return (self.bytes_transferred / self.metadata.total_size) * 100

    def get_file_path(self, file_index: int) -> Optional[Path]:
        """Get source path for a file by index"""
        return self.source_paths.get(file_index)


class FileRegistry:
    """
    Registry for tracking file transfers.

    Thread-safe registry that tracks:
    - Announced transfers (files we can serve)
    - Pending transfers (files we're waiting for)
    """

    # Default transfer TTL: 5 minutes
    DEFAULT_TTL = 300

    def __init__(self, cleanup_interval: int = 60):
        self._transfers: Dict[str, TransferInfo] = {}
        self._lock = threading.RLock()
        self._cleanup_interval = cleanup_interval
        self._cleanup_timer: Optional[threading.Timer] = None
        self._callbacks: Dict[str, List[Callable]] = {
            'on_expired': [],
            'on_transfer_complete': [],
        }

        # Start cleanup timer
        self._schedule_cleanup()

    def _schedule_cleanup(self):
        """Schedule the next cleanup"""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()

        self._cleanup_timer = threading.Timer(self._cleanup_interval, self._do_cleanup)
        self._cleanup_timer.daemon = True
        self._cleanup_timer.start()

    def _do_cleanup(self):
        """Perform cleanup of expired transfers"""
        try:
            self.cleanup_expired()
        except Exception as e:
            logger.error(f"Cleanup error: {e}")
        finally:
            self._schedule_cleanup()

    def stop(self):
        """Stop the cleanup timer"""
        if self._cleanup_timer:
            self._cleanup_timer.cancel()
            self._cleanup_timer = None

    def register_callback(self, event: str, callback: Callable):
        """Register a callback for an event"""
        if event in self._callbacks:
            self._callbacks[event].append(callback)

    def _fire_callbacks(self, event: str, *args):
        """Fire callbacks for an event"""
        for callback in self._callbacks.get(event, []):
            try:
                callback(*args)
            except Exception as e:
                logger.error(f"Callback error for {event}: {e}")

    # ========== Sender-side operations ==========

    def register_announced(
        self,
        transfer_id: str,
        metadata: TransferMetadata,
        file_paths: List[Path]
    ) -> TransferInfo:
        """
        Register files that we've announced to peers (sender side).

        Args:
            transfer_id: Unique transfer identifier
            metadata: Transfer metadata
            file_paths: Source file paths (order must match metadata.files)

        Returns:
            TransferInfo for the registered transfer
        """
        with self._lock:
            # Build file_index -> path mapping
            source_paths = {}
            for i, path in enumerate(file_paths):
                path = Path(path)
                if path.is_dir():
                    # For directories, map each file inside
                    for j, file_info in enumerate(metadata.files):
                        if file_info.file_index >= i:
                            # Find matching file in directory
                            rel_path = file_info.relative_path
                            full_path = path.parent / rel_path
                            if full_path.exists():
                                source_paths[file_info.file_index] = full_path
                else:
                    source_paths[i] = path

            info = TransferInfo(
                transfer_id=transfer_id,
                metadata=metadata,
                status=TransferStatus.ANNOUNCED,
                source_paths=source_paths
            )

            self._transfers[transfer_id] = info
            logger.info(f"Registered announced transfer: {transfer_id} ({len(metadata.files)} files)")

            return info

    def get_file_for_transfer(self, transfer_id: str, file_index: int) -> Optional[Path]:
        """Get the source file path for serving a chunk request"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if not info:
                logger.warning(f"Transfer not found: {transfer_id}")
                return None

            if info.is_expired:
                logger.warning(f"Transfer expired: {transfer_id}")
                return None

            return info.get_file_path(file_index)

    # ========== Receiver-side operations ==========

    def register_pending(
        self,
        transfer_id: str,
        metadata: TransferMetadata
    ) -> TransferInfo:
        """
        Register a pending transfer that we've been notified about (receiver side).

        Args:
            transfer_id: Unique transfer identifier
            metadata: Transfer metadata from announcement

        Returns:
            TransferInfo for the registered transfer
        """
        with self._lock:
            info = TransferInfo(
                transfer_id=transfer_id,
                metadata=metadata,
                status=TransferStatus.PENDING
            )

            self._transfers[transfer_id] = info
            logger.info(f"Registered pending transfer: {transfer_id} ({len(metadata.files)} files, {metadata.total_size} bytes)")

            return info

    def start_transfer(self, transfer_id: str, dest_dir: Path) -> Optional[TransferInfo]:
        """
        Mark a pending transfer as starting (receiver side).

        Args:
            transfer_id: Transfer to start
            dest_dir: Destination directory for files

        Returns:
            Updated TransferInfo or None if not found
        """
        with self._lock:
            info = self._transfers.get(transfer_id)
            if not info:
                return None

            if info.is_expired:
                info.status = TransferStatus.EXPIRED
                return None

            info.status = TransferStatus.REQUESTING
            info.dest_dir = Path(dest_dir)
            info.started_at = time.time()

            return info

    def update_transfer_progress(
        self,
        transfer_id: str,
        bytes_transferred: int,
        file_index: int = None,
        chunk_index: int = None
    ):
        """Update transfer progress"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if info:
                info.bytes_transferred = bytes_transferred
                info.status = TransferStatus.TRANSFERRING
                if file_index is not None:
                    info.current_file_index = file_index
                if chunk_index is not None:
                    info.current_chunk_index = chunk_index

    def add_downloaded_file(self, transfer_id: str, file_path: Path):
        """Record a completed file download"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if info:
                info.downloaded_files.append(file_path)

    def complete_transfer(self, transfer_id: str) -> Optional[TransferInfo]:
        """Mark a transfer as completed"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if info:
                info.status = TransferStatus.COMPLETED
                info.completed_at = time.time()
                self._fire_callbacks('on_transfer_complete', info)
                logger.info(f"Transfer completed: {transfer_id}")
            return info

    def fail_transfer(self, transfer_id: str, error: str) -> Optional[TransferInfo]:
        """Mark a transfer as failed"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if info:
                info.status = TransferStatus.FAILED
                info.error_message = error
                info.completed_at = time.time()
                logger.error(f"Transfer failed: {transfer_id} - {error}")
            return info

    def cancel_transfer(self, transfer_id: str, reason: str = "") -> Optional[TransferInfo]:
        """Cancel a transfer"""
        with self._lock:
            info = self._transfers.get(transfer_id)
            if info and not info.is_complete:
                info.status = TransferStatus.CANCELLED
                info.error_message = reason
                info.completed_at = time.time()
                logger.info(f"Transfer cancelled: {transfer_id}")
            return info

    # ========== Query operations ==========

    def get_transfer(self, transfer_id: str) -> Optional[TransferInfo]:
        """Get transfer info by ID"""
        with self._lock:
            return self._transfers.get(transfer_id)

    def get_announced_transfers(self) -> List[TransferInfo]:
        """Get all announced transfers (sender side)"""
        with self._lock:
            return [
                t for t in self._transfers.values()
                if t.status == TransferStatus.ANNOUNCED and not t.is_expired
            ]

    def get_pending_transfers(self) -> List[TransferInfo]:
        """Get all pending transfers (receiver side)"""
        with self._lock:
            return [
                t for t in self._transfers.values()
                if t.status == TransferStatus.PENDING and not t.is_expired
            ]

    def get_active_transfers(self) -> List[TransferInfo]:
        """Get all transfers currently in progress"""
        with self._lock:
            return [
                t for t in self._transfers.values()
                if t.status in (TransferStatus.REQUESTING, TransferStatus.TRANSFERRING)
            ]

    def get_latest_pending(self) -> Optional[TransferInfo]:
        """Get the most recent pending transfer"""
        pending = self.get_pending_transfers()
        if not pending:
            return None
        return max(pending, key=lambda t: t.created_at)

    # ========== Cleanup operations ==========

    def cleanup_expired(self) -> int:
        """
        Clean up expired transfers.

        Returns:
            Number of transfers cleaned up
        """
        with self._lock:
            expired_ids = []

            for transfer_id, info in self._transfers.items():
                if info.is_expired and not info.is_complete:
                    info.status = TransferStatus.EXPIRED
                    expired_ids.append(transfer_id)
                    self._fire_callbacks('on_expired', info)

            for transfer_id in expired_ids:
                logger.info(f"Transfer expired: {transfer_id}")

            return len(expired_ids)

    def cleanup_completed(self, max_age: int = 3600) -> int:
        """
        Clean up completed transfers older than max_age seconds.

        Returns:
            Number of transfers cleaned up
        """
        with self._lock:
            now = time.time()
            to_remove = []

            for transfer_id, info in self._transfers.items():
                if info.is_complete and info.completed_at > 0:
                    if now - info.completed_at > max_age:
                        to_remove.append(transfer_id)

            for transfer_id in to_remove:
                del self._transfers[transfer_id]

            return len(to_remove)

    def clear_all(self):
        """Clear all transfers"""
        with self._lock:
            self._transfers.clear()

    def get_stats(self) -> dict:
        """Get registry statistics"""
        with self._lock:
            by_status = {}
            for info in self._transfers.values():
                status = info.status.value
                by_status[status] = by_status.get(status, 0) + 1

            return {
                'total': len(self._transfers),
                'by_status': by_status
            }


# Global registry instance
_registry: Optional[FileRegistry] = None


def get_registry() -> FileRegistry:
    """Get or create the global file registry"""
    global _registry
    if _registry is None:
        _registry = FileRegistry()
    return _registry


def shutdown_registry():
    """Shutdown the global registry"""
    global _registry
    if _registry:
        _registry.stop()
        _registry = None
