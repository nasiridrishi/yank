"""
Configuration for LAN Clipboard File Sync
"""
import os
import sys
import time
import shutil
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Network Settings
PORT = 9876
BUFFER_SIZE = 65536  # 64KB chunks for file transfer
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB max per file
MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 500MB max total transfer

# Peer Discovery
USE_AUTO_DISCOVERY = True  # Use mDNS/Bonjour to find peers
SERVICE_NAME = "_clipboard-sync._tcp.local."
PEER_IP = None  # Set manually if auto-discovery disabled (e.g., "192.168.1.100")

# Paths
if os.name == 'nt':  # Windows
    TEMP_DIR = Path(os.environ.get('TEMP', 'C:/Temp')) / 'clipboard-sync'
else:  # macOS/Linux
    TEMP_DIR = Path('/tmp/clipboard-sync')

TEMP_DIR.mkdir(parents=True, exist_ok=True)

# Clipboard Polling
POLL_INTERVAL = 0.3  # seconds between clipboard checks

# Logging
LOG_LEVEL = "INFO"
LOG_FILE = TEMP_DIR / "clipboard-sync.log"

# Security (optional)
ENCRYPTION_KEY = None  # Set to enable AES encryption (32 bytes)

# Cleanup settings
TEMP_FILE_MAX_AGE_HOURS = 1  # Delete received files older than this


def get_data_dir() -> Path:
    """Platform-specific directory for user data (config, syncignore).

    Returns a persistent directory that works correctly even when the
    application is packaged with PyInstaller (where __file__ resolves
    to a temporary _MEI* directory).
    """
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Application Support'
    else:
        base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
    d = base / 'Yank'
    d.mkdir(parents=True, exist_ok=True)
    return d


def cleanup_old_temp_files():
    """
    Clean up old received files from temp directory.
    Called on startup to prevent disk space buildup.
    """
    try:
        max_age_seconds = TEMP_FILE_MAX_AGE_HOURS * 3600
        now = time.time()
        cleaned_count = 0
        cleaned_size = 0

        for item in TEMP_DIR.iterdir():
            # Only clean recv_* directories
            if item.is_dir() and item.name.startswith('recv_'):
                try:
                    # Check age based on directory modification time
                    age = now - item.stat().st_mtime
                    if age > max_age_seconds:
                        # Calculate size before deletion
                        size = sum(f.stat().st_size for f in item.rglob('*') if f.is_file())
                        shutil.rmtree(item)
                        cleaned_count += 1
                        cleaned_size += size
                except Exception as e:
                    logger.debug(f"Could not clean {item}: {e}")

        if cleaned_count > 0:
            size_mb = cleaned_size / (1024 * 1024)
            logger.info(f"Cleaned up {cleaned_count} old transfer(s), freed {size_mb:.1f} MB")

    except Exception as e:
        logger.debug(f"Cleanup error: {e}")
