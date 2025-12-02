"""
User Configuration Management

Manages user-editable settings stored in a JSON file.
Settings can be toggled without modifying code.
"""
import json
import logging
from pathlib import Path
from typing import Any, Optional
from dataclasses import dataclass, asdict, field

logger = logging.getLogger(__name__)

# Default config file location (same directory as the app)
CONFIG_FILE = Path(__file__).parent.parent / "config.json"

# Conversion constant
MB = 1024 * 1024


@dataclass
class SyncConfig:
    """User configuration for clipboard sync"""

    # Feature toggles
    sync_files: bool = True
    sync_text: bool = True
    sync_images: bool = True

    # Size limits (in MB for user-friendliness)
    max_text_size_mb: int = 1  # 1MB max text
    max_file_size_mb: int = 100  # 100MB per file
    max_total_size_mb: int = 500  # 500MB total transfer

    # Behavior
    auto_discovery: bool = True
    show_notifications: bool = True

    # Text sync options
    min_text_length: int = 1  # Minimum chars to sync (avoid accidental single char)
    text_sync_delay: float = 0.5  # Seconds to wait before syncing text (debounce)

    # Ignored patterns (in addition to .syncignore)
    ignored_extensions: list = field(default_factory=lambda: [
        ".tmp", ".temp", ".bak", ".swp", ".lock"
    ])

    # Properties to get sizes in bytes (for internal use)
    @property
    def max_text_size(self) -> int:
        return self.max_text_size_mb * MB

    @property
    def max_file_size(self) -> int:
        return self.max_file_size_mb * MB

    @property
    def max_total_size(self) -> int:
        return self.max_total_size_mb * MB

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'SyncConfig':
        """Create config from dict, using defaults for missing keys"""
        defaults = cls()
        for key, value in data.items():
            if hasattr(defaults, key):
                setattr(defaults, key, value)
        return defaults


class ConfigManager:
    """Manages loading, saving, and accessing user configuration"""

    _instance: Optional['ConfigManager'] = None
    _config: Optional[SyncConfig] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if self._config is None:
            self.load()

    def load(self, config_path: Path = None) -> SyncConfig:
        """Load configuration from file"""
        path = config_path or CONFIG_FILE

        if path.exists():
            try:
                with open(path, 'r') as f:
                    data = json.load(f)
                self._config = SyncConfig.from_dict(data)
                logger.info(f"Loaded config from {path}")
            except Exception as e:
                logger.warning(f"Failed to load config: {e}, using defaults")
                self._config = SyncConfig()
        else:
            logger.info("No config file found, using defaults")
            self._config = SyncConfig()
            # Save defaults
            self.save(path)

        return self._config

    def save(self, config_path: Path = None) -> bool:
        """Save configuration to file"""
        path = config_path or CONFIG_FILE

        try:
            with open(path, 'w') as f:
                json.dump(self._config.to_dict(), f, indent=2)
            logger.info(f"Saved config to {path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            return False

    def get(self) -> SyncConfig:
        """Get current configuration"""
        if self._config is None:
            self.load()
        return self._config

    def set(self, key: str, value: Any) -> bool:
        """Set a configuration value"""
        if not hasattr(self._config, key):
            logger.error(f"Unknown config key: {key}")
            return False

        setattr(self._config, key, value)
        return self.save()

    def reset(self) -> SyncConfig:
        """Reset to default configuration"""
        self._config = SyncConfig()
        self.save()
        return self._config


def get_config() -> SyncConfig:
    """Get the current user configuration"""
    return ConfigManager().get()


def get_config_manager() -> ConfigManager:
    """Get the configuration manager instance"""
    return ConfigManager()


def format_size(size_bytes: int) -> str:
    """Format bytes as human-readable string"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


def print_config():
    """Print current configuration in a readable format"""
    config = get_config()

    print("\n" + "=" * 50)
    print("  Clipboard Sync - Configuration")
    print("=" * 50)

    print("\n  Feature Toggles:")
    print(f"    File Sync:     {'ON' if config.sync_files else 'OFF'}")
    print(f"    Text Sync:     {'ON' if config.sync_text else 'OFF'}")
    print(f"    Image Sync:    {'ON' if config.sync_images else 'OFF'}")
    print(f"    Auto-Discovery: {'ON' if config.auto_discovery else 'OFF'}")

    print("\n  Size Limits:")
    print(f"    Max Text Size:  {config.max_text_size_mb} MB")
    print(f"    Max File Size:  {config.max_file_size_mb} MB")
    print(f"    Max Total Size: {config.max_total_size_mb} MB")

    print("\n  Text Sync Options:")
    print(f"    Min Text Length: {config.min_text_length} chars")
    print(f"    Sync Delay:      {config.text_sync_delay}s")

    print("\n  Ignored Extensions:")
    print(f"    {', '.join(config.ignored_extensions)}")

    print(f"\n  Config File: {CONFIG_FILE}")
    print("=" * 50 + "\n")
