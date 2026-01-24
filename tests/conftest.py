"""
Global test fixtures for Yank tests
"""
import pytest
import tempfile
import shutil
from pathlib import Path
from typing import Generator


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create a temporary directory for test files"""
    path = Path(tempfile.mkdtemp(prefix="yank_test_"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def sample_file(temp_dir: Path) -> Path:
    """Create a sample text file for testing"""
    file_path = temp_dir / "sample.txt"
    file_path.write_text("Hello, World! This is a test file.")
    return file_path


@pytest.fixture
def large_sample_file(temp_dir: Path) -> Path:
    """Create a larger file for chunked transfer testing"""
    file_path = temp_dir / "large_sample.bin"
    # Create 2MB file with random-ish data
    data = b"X" * (2 * 1024 * 1024)
    file_path.write_bytes(data)
    return file_path


@pytest.fixture
def sample_image_bytes() -> bytes:
    """Minimal valid PNG image for testing"""
    # 1x1 transparent PNG
    return bytes([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,  # PNG signature
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52,  # IHDR chunk
        0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4,
        0x89, 0x00, 0x00, 0x00, 0x0A, 0x49, 0x44, 0x41,  # IDAT chunk
        0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
        0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00,
        0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE,  # IEND chunk
        0x42, 0x60, 0x82
    ])


@pytest.fixture
def encryption_key() -> bytes:
    """Sample 32-byte encryption key"""
    return b"0123456789abcdef0123456789abcdef"


@pytest.fixture
def sample_text() -> str:
    """Sample text for text transfer testing"""
    return "Hello, this is a clipboard sync test message!"
