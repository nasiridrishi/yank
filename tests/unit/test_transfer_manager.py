"""
Unit tests for transfer_manager.py - Transfer state management
"""
import pytest
import time
from pathlib import Path

from yank.common.transfer_manager import (
    TransferManager, TransferCheckpoint, TransferState, RetryPolicy,
    get_transfer_manager, shutdown_transfer_manager
)


class TestRetryPolicy:
    """Tests for RetryPolicy"""

    def test_default_policy(self):
        policy = RetryPolicy()
        assert policy.max_retries == 3
        assert policy.initial_delay == 1.0
        assert policy.backoff_multiplier == 2.0

    def test_should_retry(self):
        policy = RetryPolicy(max_retries=3)
        assert policy.should_retry(0) is True
        assert policy.should_retry(1) is True
        assert policy.should_retry(2) is True
        assert policy.should_retry(3) is False

    def test_exponential_backoff(self):
        policy = RetryPolicy(initial_delay=1.0, backoff_multiplier=2.0)
        assert policy.get_delay(0) == 1.0
        assert policy.get_delay(1) == 2.0
        assert policy.get_delay(2) == 4.0
        assert policy.get_delay(3) == 8.0

    def test_max_delay_cap(self):
        policy = RetryPolicy(initial_delay=1.0, max_delay=5.0, backoff_multiplier=2.0)
        assert policy.get_delay(5) == 5.0  # Capped at max


class TestTransferCheckpoint:
    """Tests for TransferCheckpoint"""

    def test_creation(self):
        cp = TransferCheckpoint(
            transfer_id="test-123",
            file_index=0,
            bytes_transferred=1000,
            last_chunk_index=5,
            state=TransferState.IN_PROGRESS.value
        )
        assert cp.transfer_id == "test-123"
        assert cp.bytes_transferred == 1000

    def test_to_dict(self):
        cp = TransferCheckpoint(
            transfer_id="test-456",
            file_index=1,
            bytes_transferred=5000,
            last_chunk_index=10,
            state=TransferState.COMPLETED.value
        )
        data = cp.to_dict()
        assert data["transfer_id"] == "test-456"
        assert data["state"] == "completed"

    def test_from_dict(self):
        data = {
            "transfer_id": "test-789",
            "file_index": 2,
            "bytes_transferred": 10000,
            "last_chunk_index": 20,
            "state": "in_progress",
            "retry_count": 1,
            "created_at": 1234567890.0,
            "updated_at": 1234567891.0
        }
        cp = TransferCheckpoint.from_dict(data)
        assert cp.transfer_id == "test-789"
        assert cp.retry_count == 1


class TestTransferManager:
    """Tests for TransferManager"""

    def test_start_transfer(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        cancel_event = manager.start_transfer("transfer-001")

        assert cancel_event is not None
        assert not cancel_event.is_set()

        cp = manager.get_checkpoint("transfer-001")
        assert cp is not None
        assert cp.state == TransferState.IN_PROGRESS.value

    def test_cancel_transfer(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        cancel_event = manager.start_transfer("transfer-002")

        result = manager.cancel_transfer("transfer-002", "Test cancellation")
        assert result is True
        assert cancel_event.is_set()

        cp = manager.get_checkpoint("transfer-002")
        assert cp.state == TransferState.CANCELLED.value
        assert cp.error_message == "Test cancellation"

    def test_is_cancelled(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-003")

        assert manager.is_cancelled("transfer-003") is False
        manager.cancel_transfer("transfer-003")
        assert manager.is_cancelled("transfer-003") is True

    def test_update_progress(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-004")

        manager.update_progress("transfer-004", file_index=0, bytes_transferred=5000, chunk_index=5)

        cp = manager.get_checkpoint("transfer-004")
        assert cp.bytes_transferred == 5000
        assert cp.last_chunk_index == 5

    def test_complete_transfer(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-005")
        manager.complete_transfer("transfer-005")

        cp = manager.get_checkpoint("transfer-005")
        assert cp.state == TransferState.COMPLETED.value

    def test_fail_transfer(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-006")
        manager.fail_transfer("transfer-006", "Network error")

        cp = manager.get_checkpoint("transfer-006")
        assert cp.state == TransferState.FAILED.value
        assert cp.error_message == "Network error"

    def test_should_retry_chunk(self, temp_dir):
        manager = TransferManager(
            checkpoint_dir=temp_dir,
            retry_policy=RetryPolicy(max_retries=3)
        )
        manager.start_transfer("transfer-007")

        # First retry
        should_retry, delay, attempt = manager.should_retry_chunk("transfer-007", "error")
        assert should_retry is True
        assert attempt == 1

        # Second retry
        should_retry, delay, attempt = manager.should_retry_chunk("transfer-007", "error")
        assert should_retry is True
        assert attempt == 2

        # Third retry
        should_retry, delay, attempt = manager.should_retry_chunk("transfer-007", "error")
        assert should_retry is True
        assert attempt == 3

        # No more retries
        should_retry, delay, attempt = manager.should_retry_chunk("transfer-007", "error")
        assert should_retry is False

    def test_reset_retry_count(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-008")
        manager.should_retry_chunk("transfer-008", "error")
        manager.should_retry_chunk("transfer-008", "error")

        manager.reset_retry_count("transfer-008")

        cp = manager.get_checkpoint("transfer-008")
        assert cp.retry_count == 0

    def test_checkpoint_persistence(self, temp_dir):
        # Create manager and start transfer
        manager1 = TransferManager(checkpoint_dir=temp_dir)
        manager1.start_transfer("transfer-009")
        manager1.update_progress("transfer-009", 1, 10000, 10)

        # Create new manager - should load checkpoints
        manager2 = TransferManager(checkpoint_dir=temp_dir)
        cp = manager2.get_checkpoint("transfer-009")

        assert cp is not None
        assert cp.bytes_transferred == 10000

    def test_cleanup_old_checkpoints(self, temp_dir):
        manager = TransferManager(checkpoint_dir=temp_dir)
        manager.start_transfer("transfer-010")
        manager.complete_transfer("transfer-010")

        # Manually set old timestamp
        manager._checkpoints["transfer-010"].updated_at = time.time() - 100000

        manager.cleanup_old_checkpoints(max_age_hours=1)

        assert manager.get_checkpoint("transfer-010") is None


class TestGlobalTransferManager:
    """Tests for global transfer manager functions"""

    def test_get_transfer_manager_singleton(self, temp_dir):
        # Reset global state
        import yank.common.transfer_manager as tm
        tm._manager = None

        m1 = get_transfer_manager(temp_dir)
        m2 = get_transfer_manager()

        assert m1 is m2

    def test_shutdown_transfer_manager(self, temp_dir):
        import yank.common.transfer_manager as tm
        tm._manager = None

        manager = get_transfer_manager(temp_dir)
        manager.start_transfer("shutdown-test")

        shutdown_transfer_manager()

        assert tm._manager is None
