"""
Transfer Manager - Handles error recovery, cancellation, retry, and resume for file transfers.

Features:
- Cancel ongoing transfers
- Retry failed chunks with exponential backoff
- Resume interrupted transfers
- Save/restore transfer state for persistence
- Timeout handling with configurable limits
"""
import os
import json
import time
import threading
import logging
from pathlib import Path
from typing import Optional, Callable, Dict, List
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


class TransferState(Enum):
    """Transfer state for persistence"""
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TransferCheckpoint:
    """Checkpoint for resumable transfers"""
    transfer_id: str
    file_index: int
    bytes_transferred: int
    last_chunk_index: int
    state: str
    error_message: str = ""
    retry_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'TransferCheckpoint':
        return cls(**data)


class RetryPolicy:
    """Configurable retry policy with exponential backoff"""

    def __init__(
        self,
        max_retries: int = 3,
        initial_delay: float = 1.0,
        max_delay: float = 30.0,
        backoff_multiplier: float = 2.0
    ):
        self.max_retries = max_retries
        self.initial_delay = initial_delay
        self.max_delay = max_delay
        self.backoff_multiplier = backoff_multiplier

    def get_delay(self, attempt: int) -> float:
        """Get delay for given retry attempt (0-indexed)"""
        delay = self.initial_delay * (self.backoff_multiplier ** attempt)
        return min(delay, self.max_delay)

    def should_retry(self, attempt: int) -> bool:
        """Check if we should retry given current attempt count"""
        return attempt < self.max_retries


class TransferManager:
    """
    Manages file transfers with error recovery, cancellation, and resume support.

    Usage:
        manager = TransferManager(checkpoint_dir=Path("./checkpoints"))

        # Start a transfer (can be cancelled)
        manager.start_transfer(transfer_id, download_func)

        # Cancel if needed
        manager.cancel_transfer(transfer_id)

        # Resume after restart
        manager.resume_transfer(transfer_id, download_func)
    """

    def __init__(
        self,
        checkpoint_dir: Optional[Path] = None,
        retry_policy: Optional[RetryPolicy] = None,
        chunk_timeout: float = 30.0,
        transfer_timeout: float = 600.0  # 10 minutes
    ):
        self.checkpoint_dir = checkpoint_dir
        self.retry_policy = retry_policy or RetryPolicy()
        self.chunk_timeout = chunk_timeout
        self.transfer_timeout = transfer_timeout

        # Active transfers that can be cancelled
        self._active_transfers: Dict[str, threading.Event] = {}
        self._checkpoints: Dict[str, TransferCheckpoint] = {}
        self._lock = threading.Lock()

        # Callbacks
        self.on_transfer_cancelled: Optional[Callable[[str], None]] = None
        self.on_transfer_failed: Optional[Callable[[str, str], None]] = None
        self.on_transfer_resumed: Optional[Callable[[str], None]] = None
        self.on_retry: Optional[Callable[[str, int, str], None]] = None

        # Load existing checkpoints
        if self.checkpoint_dir:
            self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
            self._load_checkpoints()

    def _load_checkpoints(self):
        """Load saved checkpoints from disk"""
        if not self.checkpoint_dir:
            return

        checkpoint_file = self.checkpoint_dir / "transfer_checkpoints.json"
        if checkpoint_file.exists():
            try:
                with open(checkpoint_file, 'r') as f:
                    data = json.load(f)
                    for tid, cp_data in data.items():
                        self._checkpoints[tid] = TransferCheckpoint.from_dict(cp_data)
                logger.info(f"Loaded {len(self._checkpoints)} transfer checkpoints")
            except Exception as e:
                logger.error(f"Failed to load checkpoints: {e}")

    def _save_checkpoints(self):
        """Save checkpoints to disk"""
        if not self.checkpoint_dir:
            return

        checkpoint_file = self.checkpoint_dir / "transfer_checkpoints.json"
        try:
            data = {tid: cp.to_dict() for tid, cp in self._checkpoints.items()}
            with open(checkpoint_file, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save checkpoints: {e}")

    def start_transfer(self, transfer_id: str) -> threading.Event:
        """
        Register a transfer and return a cancellation event.

        The caller should check cancel_event.is_set() periodically
        during the transfer to handle cancellation.

        Returns:
            threading.Event that will be set if transfer is cancelled
        """
        with self._lock:
            cancel_event = threading.Event()
            self._active_transfers[transfer_id] = cancel_event

            # Initialize checkpoint
            self._checkpoints[transfer_id] = TransferCheckpoint(
                transfer_id=transfer_id,
                file_index=0,
                bytes_transferred=0,
                last_chunk_index=-1,
                state=TransferState.IN_PROGRESS.value
            )
            self._save_checkpoints()

            return cancel_event

    def cancel_transfer(self, transfer_id: str, reason: str = "User cancelled") -> bool:
        """
        Cancel an ongoing transfer.

        Returns:
            True if transfer was found and cancelled
        """
        with self._lock:
            if transfer_id in self._active_transfers:
                # Signal cancellation
                self._active_transfers[transfer_id].set()

                # Update checkpoint
                if transfer_id in self._checkpoints:
                    cp = self._checkpoints[transfer_id]
                    cp.state = TransferState.CANCELLED.value
                    cp.error_message = reason
                    cp.updated_at = time.time()
                    self._save_checkpoints()

                logger.info(f"Transfer cancelled: {transfer_id} - {reason}")

                if self.on_transfer_cancelled:
                    self.on_transfer_cancelled(transfer_id)

                return True

        return False

    def is_cancelled(self, transfer_id: str) -> bool:
        """Check if a transfer has been cancelled"""
        with self._lock:
            if transfer_id in self._active_transfers:
                return self._active_transfers[transfer_id].is_set()
        return False

    def update_progress(
        self,
        transfer_id: str,
        file_index: int,
        bytes_transferred: int,
        chunk_index: int
    ):
        """Update transfer progress checkpoint"""
        with self._lock:
            if transfer_id in self._checkpoints:
                cp = self._checkpoints[transfer_id]
                cp.file_index = file_index
                cp.bytes_transferred = bytes_transferred
                cp.last_chunk_index = chunk_index
                cp.updated_at = time.time()

                # Periodically save (every 10 chunks or 5 seconds)
                if chunk_index % 10 == 0:
                    self._save_checkpoints()

    def complete_transfer(self, transfer_id: str):
        """Mark a transfer as completed"""
        with self._lock:
            # Remove from active
            if transfer_id in self._active_transfers:
                del self._active_transfers[transfer_id]

            # Update checkpoint
            if transfer_id in self._checkpoints:
                cp = self._checkpoints[transfer_id]
                cp.state = TransferState.COMPLETED.value
                cp.updated_at = time.time()
                self._save_checkpoints()

                # Optionally remove completed checkpoints after some time
                # For now, keep them for debugging

    def fail_transfer(self, transfer_id: str, error: str):
        """Mark a transfer as failed"""
        with self._lock:
            # Remove from active
            if transfer_id in self._active_transfers:
                del self._active_transfers[transfer_id]

            # Update checkpoint
            if transfer_id in self._checkpoints:
                cp = self._checkpoints[transfer_id]
                cp.state = TransferState.FAILED.value
                cp.error_message = error
                cp.updated_at = time.time()
                self._save_checkpoints()

            if self.on_transfer_failed:
                self.on_transfer_failed(transfer_id, error)

    def get_checkpoint(self, transfer_id: str) -> Optional[TransferCheckpoint]:
        """Get checkpoint for a transfer (for resume)"""
        with self._lock:
            return self._checkpoints.get(transfer_id)

    def get_resumable_transfers(self) -> List[TransferCheckpoint]:
        """Get list of transfers that can be resumed"""
        with self._lock:
            return [
                cp for cp in self._checkpoints.values()
                if cp.state in (TransferState.IN_PROGRESS.value, TransferState.PAUSED.value)
            ]

    def can_resume(self, transfer_id: str) -> bool:
        """Check if a transfer can be resumed"""
        cp = self.get_checkpoint(transfer_id)
        if not cp:
            return False
        return cp.state in (TransferState.IN_PROGRESS.value, TransferState.PAUSED.value)

    def get_resume_offset(self, transfer_id: str, file_index: int) -> int:
        """Get byte offset to resume from for a specific file"""
        cp = self.get_checkpoint(transfer_id)
        if not cp or cp.file_index != file_index:
            return 0
        return cp.bytes_transferred

    def should_retry_chunk(self, transfer_id: str, error: str) -> tuple:
        """
        Check if we should retry a failed chunk.

        Returns:
            (should_retry: bool, delay: float, attempt: int)
        """
        with self._lock:
            cp = self._checkpoints.get(transfer_id)
            if not cp:
                return (False, 0, 0)

            attempt = cp.retry_count

            if self.retry_policy.should_retry(attempt):
                delay = self.retry_policy.get_delay(attempt)
                cp.retry_count += 1
                cp.updated_at = time.time()

                logger.info(f"Retry {attempt + 1}/{self.retry_policy.max_retries} for {transfer_id} in {delay:.1f}s")

                if self.on_retry:
                    self.on_retry(transfer_id, attempt + 1, error)

                return (True, delay, attempt + 1)

            return (False, 0, attempt)

    def reset_retry_count(self, transfer_id: str):
        """Reset retry count after successful chunk"""
        with self._lock:
            if transfer_id in self._checkpoints:
                self._checkpoints[transfer_id].retry_count = 0

    def cleanup_old_checkpoints(self, max_age_hours: int = 24):
        """Remove checkpoints older than max_age_hours"""
        with self._lock:
            now = time.time()
            max_age_seconds = max_age_hours * 3600

            to_remove = []
            for tid, cp in self._checkpoints.items():
                if cp.state in (TransferState.COMPLETED.value, TransferState.CANCELLED.value):
                    if now - cp.updated_at > max_age_seconds:
                        to_remove.append(tid)

            for tid in to_remove:
                del self._checkpoints[tid]

            if to_remove:
                self._save_checkpoints()
                logger.info(f"Cleaned up {len(to_remove)} old checkpoints")

    def clear_checkpoint(self, transfer_id: str):
        """Remove a specific checkpoint"""
        with self._lock:
            if transfer_id in self._checkpoints:
                del self._checkpoints[transfer_id]
                self._save_checkpoints()


# Global transfer manager instance
_manager: Optional[TransferManager] = None


def get_transfer_manager(checkpoint_dir: Optional[Path] = None) -> TransferManager:
    """Get or create the global transfer manager"""
    global _manager
    if _manager is None:
        _manager = TransferManager(checkpoint_dir=checkpoint_dir)
    return _manager
