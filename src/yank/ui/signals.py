"""
Qt Signals for thread-safe communication between SyncAgent and UI.

The SyncAgent runs in a background thread, while the UI runs in the
main Qt thread. These signals bridge the two safely.
"""

from PySide6.QtCore import QObject, Signal


class SyncSignals(QObject):
    """
    Signal hub for sync-related events.

    All signals are thread-safe and can be emitted from any thread.
    """

    # Files announced by peer (lazy transfer)
    # Args: transfer_id (str), metadata (TransferMetadata)
    files_announced = Signal(str, object)

    # Files received directly (small files, already downloaded)
    # Args: transfer_id (str), file_paths (list)
    files_received = Signal(str, list)

    # Transfer progress update
    # Args: transfer_id (str), bytes_done (int), bytes_total (int), current_file (str)
    transfer_progress = Signal(str, int, int, str)

    # Transfer completed
    # Args: transfer_id (str), success (bool), file_paths (list)
    transfer_complete = Signal(str, bool, list)

    # Transfer cancelled
    # Args: transfer_id (str), reason (str)
    transfer_cancelled = Signal(str, str)

    # Transfer error
    # Args: transfer_id (str), error (str)
    transfer_error = Signal(str, str)

    # Connection status changed
    # Args: connected (bool), peer_name (str)
    connection_changed = Signal(bool, str)

    # Download requested by user (from UI to agent)
    # Args: transfer_id (str)
    download_requested = Signal(str)

    # Download to location requested
    # Args: transfer_id (str), destination (str)
    download_to_requested = Signal(str, str)

    # Cancel transfer requested
    # Args: transfer_id (str)
    cancel_requested = Signal(str)

    # Dismiss item from list
    # Args: transfer_id (str)
    dismiss_requested = Signal(str)
