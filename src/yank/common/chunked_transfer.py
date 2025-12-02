"""
Chunked file transfer utilities for memory-efficient large file handling.

Provides:
- ChunkedFileReader: Read files in chunks without loading entire file into memory
- ChunkedFileWriter: Write chunks to temp file, verify checksum, atomic move
- ProgressTracker: Track transfer progress with speed and ETA calculation
"""
import os
import time
import shutil
import hashlib
import logging
import threading
from pathlib import Path
from typing import Iterator, Callable, Optional, List
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Default chunk size: 1MB
DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass
class TransferStats:
    """Statistics for a transfer in progress"""
    bytes_transferred: int = 0
    bytes_total: int = 0
    start_time: float = 0.0
    last_update_time: float = 0.0
    last_bytes: int = 0
    speed_bps: float = 0.0  # Bytes per second
    eta_seconds: float = 0.0

    def update(self, bytes_transferred: int):
        """Update stats with new byte count"""
        now = time.time()
        self.bytes_transferred = bytes_transferred

        # Calculate speed (smoothed over last interval)
        if self.last_update_time > 0:
            time_delta = now - self.last_update_time
            if time_delta > 0.1:  # Update at most every 100ms
                bytes_delta = bytes_transferred - self.last_bytes
                self.speed_bps = bytes_delta / time_delta
                self.last_update_time = now
                self.last_bytes = bytes_transferred

                # Calculate ETA
                remaining = self.bytes_total - bytes_transferred
                if self.speed_bps > 0:
                    self.eta_seconds = remaining / self.speed_bps
                else:
                    self.eta_seconds = 0
        else:
            self.last_update_time = now
            self.last_bytes = bytes_transferred

    @property
    def percent(self) -> float:
        """Get completion percentage"""
        if self.bytes_total == 0:
            return 0.0
        return (self.bytes_transferred / self.bytes_total) * 100

    @property
    def elapsed_seconds(self) -> float:
        """Get elapsed time since start"""
        if self.start_time == 0:
            return 0.0
        return time.time() - self.start_time


class ChunkedFileReader:
    """
    Read a file in chunks for memory-efficient streaming.

    Usage:
        reader = ChunkedFileReader(filepath, chunk_size=1024*1024)
        for chunk_info, data in reader.read_chunks():
            # chunk_info contains offset, size, checksum, is_last
            # data is the raw bytes
            send_chunk(chunk_info, data)
    """

    def __init__(self, filepath: Path, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.filepath = Path(filepath)
        self.chunk_size = chunk_size
        self.file_size = self.filepath.stat().st_size
        self.total_chunks = (self.file_size + chunk_size - 1) // chunk_size

    def read_chunks(self, start_offset: int = 0) -> Iterator[tuple]:
        """
        Yield (chunk_index, offset, data, checksum, is_last) tuples.

        Args:
            start_offset: Byte offset to start reading from (for resume)
        """
        chunk_index = start_offset // self.chunk_size
        current_offset = start_offset

        with open(self.filepath, 'rb') as f:
            f.seek(start_offset)

            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break

                checksum = hashlib.md5(data).hexdigest()
                is_last = (current_offset + len(data)) >= self.file_size

                yield (chunk_index, current_offset, data, checksum, is_last)

                chunk_index += 1
                current_offset += len(data)

    def get_file_checksum(self) -> str:
        """Calculate full file MD5 checksum"""
        hasher = hashlib.md5()
        with open(self.filepath, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        return hasher.hexdigest()


class ChunkedFileWriter:
    """
    Write file chunks to a temporary file, then atomically move to destination.

    Usage:
        writer = ChunkedFileWriter(dest_path, expected_size, expected_checksum)
        for chunk in receive_chunks():
            writer.write_chunk(chunk.offset, chunk.data, chunk.checksum)
            if chunk.is_last:
                final_path = writer.finalize()
    """

    def __init__(self, dest_path: Path, expected_size: int, expected_checksum: str):
        self.dest_path = Path(dest_path)
        self.expected_size = expected_size
        self.expected_checksum = expected_checksum

        # Create temp file in same directory for atomic move
        self.temp_path = self.dest_path.parent / f".{self.dest_path.name}.tmp"
        self.dest_path.parent.mkdir(parents=True, exist_ok=True)

        self.bytes_written = 0
        self.hasher = hashlib.md5()
        self._file = None
        self._lock = threading.Lock()

    def _ensure_file_open(self):
        """Ensure the temp file is open for writing"""
        if self._file is None:
            self._file = open(self.temp_path, 'wb')

    def write_chunk(self, offset: int, data: bytes, chunk_checksum: str) -> bool:
        """
        Write a chunk to the file.

        Args:
            offset: Byte offset in the file
            data: Chunk data
            chunk_checksum: Expected MD5 of this chunk

        Returns:
            True if chunk was written successfully
        """
        # Verify chunk checksum
        actual_checksum = hashlib.md5(data).hexdigest()
        if actual_checksum != chunk_checksum:
            logger.error(f"Chunk checksum mismatch at offset {offset}")
            return False

        with self._lock:
            self._ensure_file_open()

            # Seek to offset (for out-of-order chunks or resume)
            self._file.seek(offset)
            self._file.write(data)

            # Update total bytes (track highest offset + size)
            end_pos = offset + len(data)
            if end_pos > self.bytes_written:
                self.bytes_written = end_pos

        return True

    def finalize(self) -> Path:
        """
        Close temp file, verify checksum, and move to final destination.

        Returns:
            Final file path

        Raises:
            ValueError: If checksum doesn't match
        """
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

        # Verify file size
        actual_size = self.temp_path.stat().st_size
        if actual_size != self.expected_size:
            self.cleanup()
            raise ValueError(f"Size mismatch: expected {self.expected_size}, got {actual_size}")

        # Verify full file checksum
        hasher = hashlib.md5()
        with open(self.temp_path, 'rb') as f:
            for chunk in iter(lambda: f.read(65536), b''):
                hasher.update(chunk)
        actual_checksum = hasher.hexdigest()

        if actual_checksum != self.expected_checksum:
            self.cleanup()
            raise ValueError(f"Checksum mismatch: expected {self.expected_checksum}, got {actual_checksum}")

        # Handle destination collision
        final_path = self._get_unique_path(self.dest_path)

        # Atomic move
        shutil.move(str(self.temp_path), str(final_path))
        logger.info(f"File written successfully: {final_path}")

        return final_path

    def _get_unique_path(self, path: Path) -> Path:
        """Get a unique path if file already exists"""
        if not path.exists():
            return path

        stem = path.stem
        suffix = path.suffix
        counter = 1

        while path.exists():
            path = path.parent / f"{stem}_{counter}{suffix}"
            counter += 1

        return path

    def cleanup(self):
        """Clean up temp file on error"""
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None

        if self.temp_path.exists():
            try:
                self.temp_path.unlink()
            except Exception as e:
                logger.error(f"Failed to cleanup temp file: {e}")

    def get_progress(self) -> float:
        """Get completion percentage"""
        if self.expected_size == 0:
            return 100.0
        return (self.bytes_written / self.expected_size) * 100


class ProgressTracker:
    """
    Track and display transfer progress.

    Usage:
        tracker = ProgressTracker(total_size, callback=update_ui)
        tracker.start()
        for chunk in chunks:
            tracker.update(chunk.size)
        tracker.finish()
    """

    def __init__(
        self,
        total_bytes: int,
        total_files: int = 1,
        callback: Optional[Callable[[TransferStats], None]] = None,
        update_interval: float = 0.1
    ):
        self.total_bytes = total_bytes
        self.total_files = total_files
        self.callback = callback
        self.update_interval = update_interval

        self.stats = TransferStats(bytes_total=total_bytes)
        self.current_file_index = 0
        self.current_file_name = ""
        self.files_completed = 0

        self._lock = threading.Lock()
        self._last_callback_time = 0.0

    def start(self, file_name: str = ""):
        """Start tracking progress"""
        with self._lock:
            self.stats.start_time = time.time()
            self.stats.last_update_time = 0
            self.current_file_name = file_name
            self._invoke_callback()

    def update(self, bytes_added: int):
        """Update progress with bytes transferred"""
        with self._lock:
            self.stats.update(self.stats.bytes_transferred + bytes_added)

            # Rate-limit callbacks
            now = time.time()
            if now - self._last_callback_time >= self.update_interval:
                self._invoke_callback()
                self._last_callback_time = now

    def set_bytes(self, total_bytes: int):
        """Set absolute byte count (for chunk-based updates)"""
        with self._lock:
            self.stats.update(total_bytes)

            now = time.time()
            if now - self._last_callback_time >= self.update_interval:
                self._invoke_callback()
                self._last_callback_time = now

    def next_file(self, file_name: str):
        """Move to next file in multi-file transfer"""
        with self._lock:
            self.files_completed += 1
            self.current_file_index += 1
            self.current_file_name = file_name
            self._invoke_callback()

    def finish(self):
        """Mark transfer as complete"""
        with self._lock:
            self.stats.bytes_transferred = self.stats.bytes_total
            self.files_completed = self.total_files
            self._invoke_callback()

    def _invoke_callback(self):
        """Invoke the progress callback if set"""
        if self.callback:
            try:
                self.callback(self.stats)
            except Exception as e:
                logger.error(f"Progress callback error: {e}")

    def get_progress_string(self) -> str:
        """Get a formatted progress string"""
        percent = self.stats.percent
        transferred = format_bytes(self.stats.bytes_transferred)
        total = format_bytes(self.stats.bytes_total)
        speed = format_bytes(self.stats.speed_bps) + "/s"
        eta = format_time(self.stats.eta_seconds)

        # Progress bar
        bar_width = 20
        filled = int(bar_width * percent / 100)
        bar = '█' * filled + '░' * (bar_width - filled)

        return f"[{bar}] {percent:.1f}% ({transferred}/{total}) - {speed} - ETA: {eta}"


def format_bytes(size: float) -> str:
    """Format byte size as human-readable string"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(size) < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def format_time(seconds: float) -> str:
    """Format seconds as human-readable time"""
    if seconds <= 0:
        return "calculating..."
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        mins = int(seconds / 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds / 3600)
        mins = int((seconds % 3600) / 60)
        return f"{hours}h {mins}m"


def create_file_metadata(
    file_paths: List[Path],
    transfer_id: str,
    expiry_seconds: int = 300,
    chunk_size: int = DEFAULT_CHUNK_SIZE
) -> 'TransferMetadata':
    """
    Create TransferMetadata for a list of files without reading file contents.

    This is much faster than pack_files() as it only reads file metadata,
    not the actual data.
    """
    import platform
    from yank.common.protocol import FileInfo, TransferMetadata, calculate_checksum

    files_info = []
    total_size = 0
    file_index = 0

    for filepath in file_paths:
        filepath = Path(filepath)

        if filepath.is_dir():
            # For directories, collect all files
            for subpath in filepath.rglob('*'):
                if subpath.is_file():
                    rel_path = subpath.relative_to(filepath.parent)
                    file_size = subpath.stat().st_size
                    checksum = calculate_checksum(subpath)

                    files_info.append(FileInfo(
                        name=subpath.name,
                        size=file_size,
                        checksum=checksum,
                        is_directory=False,
                        relative_path=str(rel_path),
                        file_index=file_index
                    ))
                    total_size += file_size
                    file_index += 1
        else:
            # Single file
            file_size = filepath.stat().st_size
            checksum = calculate_checksum(filepath)

            files_info.append(FileInfo(
                name=filepath.name,
                size=file_size,
                checksum=checksum,
                is_directory=False,
                relative_path=filepath.name,
                file_index=file_index
            ))
            total_size += file_size
            file_index += 1

    now = time.time()

    return TransferMetadata(
        files=files_info,
        total_size=total_size,
        timestamp=now,
        source_os='windows' if platform.system() == 'Windows' else 'macos',
        transfer_id=transfer_id,
        expires_at=now + expiry_seconds,
        chunk_size=chunk_size
    )
