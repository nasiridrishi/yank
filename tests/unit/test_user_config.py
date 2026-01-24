"""
Unit tests for user_config.py - Configuration management
"""
import pytest
import json
from pathlib import Path

from yank.common.user_config import SyncConfig, ConfigManager


class TestSyncConfigValidation:
    """Tests for SyncConfig validation"""

    def test_default_config_is_valid(self):
        config = SyncConfig()
        errors = config.validate()
        assert errors == []

    def test_negative_file_size_invalid(self):
        config = SyncConfig(max_file_size_mb=-1)
        errors = config.validate()
        assert any("max_file_size_mb" in e for e in errors)

    def test_zero_file_size_invalid(self):
        config = SyncConfig(max_file_size_mb=0)
        errors = config.validate()
        assert any("max_file_size_mb" in e for e in errors)

    def test_negative_total_size_invalid(self):
        config = SyncConfig(max_total_size_mb=-10)
        errors = config.validate()
        assert any("max_total_size_mb" in e for e in errors)

    def test_negative_text_length_invalid(self):
        config = SyncConfig(min_text_length=-5)
        errors = config.validate()
        assert any("min_text_length" in e for e in errors)

    def test_sync_delay_too_high(self):
        config = SyncConfig(text_sync_delay=15.0)
        errors = config.validate()
        assert any("text_sync_delay" in e for e in errors)

    def test_sync_delay_negative(self):
        config = SyncConfig(text_sync_delay=-1.0)
        errors = config.validate()
        assert any("text_sync_delay" in e for e in errors)

    def test_file_size_exceeds_total(self):
        config = SyncConfig(max_file_size_mb=200, max_total_size_mb=100)
        errors = config.validate()
        assert any("exceed" in e.lower() for e in errors)

    def test_invalid_ignored_extensions_type(self):
        config = SyncConfig()
        config.ignored_extensions = "not_a_list"
        errors = config.validate()
        assert any("ignored_extensions" in e for e in errors)

    def test_valid_custom_config(self):
        config = SyncConfig(
            sync_files=False,
            sync_text=True,
            max_file_size_mb=50,
            max_total_size_mb=200,
            text_sync_delay=1.0,
            ignored_extensions=[".tmp", ".bak"]
        )
        errors = config.validate()
        assert errors == []


class TestSyncConfigSerialization:
    """Tests for SyncConfig serialization"""

    def test_to_dict(self):
        config = SyncConfig(sync_files=False, max_file_size_mb=50)
        data = config.to_dict()

        assert isinstance(data, dict)
        assert data["sync_files"] is False
        assert data["max_file_size_mb"] == 50

    def test_from_dict(self):
        data = {
            "sync_files": False,
            "max_file_size_mb": 75,
            "ignored_extensions": [".log"]
        }
        config = SyncConfig.from_dict(data)

        assert config.sync_files is False
        assert config.max_file_size_mb == 75
        assert config.ignored_extensions == [".log"]
        # Default values preserved for missing keys
        assert config.sync_text is True

    def test_from_dict_ignores_unknown_keys(self):
        data = {
            "sync_files": False,
            "unknown_key": "should be ignored"
        }
        config = SyncConfig.from_dict(data)
        assert config.sync_files is False
        assert not hasattr(config, "unknown_key")


class TestSyncConfigProperties:
    """Tests for SyncConfig computed properties"""

    def test_max_file_size_bytes(self):
        config = SyncConfig(max_file_size_mb=10)
        assert config.max_file_size == 10 * 1024 * 1024

    def test_max_total_size_bytes(self):
        config = SyncConfig(max_total_size_mb=100)
        assert config.max_total_size == 100 * 1024 * 1024

    def test_max_text_size_bytes(self):
        config = SyncConfig(max_text_size_mb=2)
        assert config.max_text_size == 2 * 1024 * 1024


class TestConfigManager:
    """Tests for ConfigManager"""

    def test_load_creates_default_if_missing(self, temp_dir):
        config_path = temp_dir / "config.json"
        assert not config_path.exists()

        manager = ConfigManager()
        manager._config = None  # Reset singleton state
        config = manager.load(config_path)

        assert config is not None
        assert config_path.exists()

    def test_load_existing_config(self, temp_dir):
        config_path = temp_dir / "config.json"
        config_path.write_text(json.dumps({
            "sync_files": False,
            "max_file_size_mb": 25
        }))

        manager = ConfigManager()
        manager._config = None
        config = manager.load(config_path)

        assert config.sync_files is False
        assert config.max_file_size_mb == 25

    def test_load_fixes_invalid_values(self, temp_dir):
        config_path = temp_dir / "config.json"
        config_path.write_text(json.dumps({
            "max_file_size_mb": -10  # Invalid
        }))

        manager = ConfigManager()
        manager._config = None
        config = manager.load(config_path)

        # Should be reset to default
        default = SyncConfig()
        assert config.max_file_size_mb == default.max_file_size_mb

    def test_save_and_load_roundtrip(self, temp_dir):
        config_path = temp_dir / "config.json"

        manager = ConfigManager()
        manager._config = SyncConfig(sync_text=False, max_file_size_mb=42)
        manager.save(config_path)

        # Load into new manager
        manager2 = ConfigManager()
        manager2._config = None
        config = manager2.load(config_path)

        assert config.sync_text is False
        assert config.max_file_size_mb == 42

    def test_set_valid_value(self, temp_dir):
        config_path = temp_dir / "config.json"

        manager = ConfigManager()
        manager._config = SyncConfig()
        manager.save(config_path)

        result = manager.set("sync_files", False)
        assert result is True
        assert manager._config.sync_files is False

    def test_set_unknown_key(self, temp_dir):
        manager = ConfigManager()
        manager._config = SyncConfig()

        result = manager.set("unknown_key", "value")
        assert result is False

    def test_reset(self, temp_dir):
        config_path = temp_dir / "config.json"

        manager = ConfigManager()
        manager._config = SyncConfig(sync_files=False, max_file_size_mb=25)
        manager.save(config_path)

        config = manager.reset()

        # Should be back to defaults
        default = SyncConfig()
        assert config.sync_files == default.sync_files
        assert config.max_file_size_mb == default.max_file_size_mb
