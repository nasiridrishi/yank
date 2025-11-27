"""
Configuration for LAN Clipboard File Sync
"""
import os
from pathlib import Path

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
