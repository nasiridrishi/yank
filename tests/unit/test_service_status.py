"""
Tests for service status detection across platforms.

Covers:
- macOS: Homebrew-managed plist detection
- Linux: Package-provided systemd unit detection
- Self-healing: auto-install when paired but service missing
"""
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest

from yank.common.service_manager import ServiceInfo, ServiceStatus


# ── macOS: Homebrew plist detection ──────────────────────────────────────


class TestMacOSHomebrewDetection:
    """MacOSServiceManager.get_status() should detect Homebrew-managed plist."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with patch("yank.platform.macos.service.Path.home", return_value=tmp_path):
            from yank.platform.macos.service import MacOSServiceManager
            self.mgr = MacOSServiceManager()
            self.launch_agents = tmp_path / "Library" / "LaunchAgents"
            self.launch_agents.mkdir(parents=True)

    def test_no_plist_returns_not_installed(self):
        info = self.mgr.get_status()
        assert info.status == ServiceStatus.NOT_INSTALLED

    def test_yank_plist_exists_but_not_loaded(self):
        self.mgr._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = MagicMock(returncode=113, stdout="", stderr="")
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.STOPPED
        assert info.enabled is True

    def test_homebrew_plist_exists_and_running(self):
        """If only the Homebrew plist exists and is loaded, report RUNNING."""
        # No yank plist, only homebrew plist
        self.mgr._homebrew_plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._homebrew_plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = MagicMock(
                returncode=0,
                stdout="pid = 12345\n",
            )
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.RUNNING
        assert info.pid == 12345
        # launchctl was called with homebrew label
        mock_lctl.assert_called_once()
        call_args = mock_lctl.call_args[0]
        assert "homebrew.mxcl.yank" in call_args[1]

    def test_homebrew_plist_exists_but_stopped(self):
        self.mgr._homebrew_plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._homebrew_plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = MagicMock(returncode=113, stdout="", stderr="")
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.STOPPED

    def test_yank_plist_takes_priority_over_homebrew(self):
        """If both plists exist, the yank plist is checked (not homebrew)."""
        self.mgr._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._plist_path.write_bytes(b"<plist/>")
        self.mgr._homebrew_plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = MagicMock(
                returncode=0,
                stdout="pid = 99\n",
            )
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.RUNNING
        assert info.pid == 99
        # Should query yank label, not homebrew
        call_args = mock_lctl.call_args[0]
        assert "com.yank.agent" in call_args[1]


# ── Linux: package-provided unit detection ───────────────────────────────


class TestLinuxPackageUnitDetection:
    """LinuxServiceManager.get_status() should detect /usr/lib/systemd/user unit."""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path / ".config")}):
            from yank.platform.linux.service import LinuxServiceManager
            self.mgr = LinuxServiceManager()
            # Override system unit path to tmp for testing
            self.mgr._system_unit_path = tmp_path / "usr" / "lib" / "systemd" / "user" / "yank.service"
            self.mgr._unit_path = tmp_path / ".config" / "systemd" / "user" / "yank.service"

    def test_no_unit_returns_not_installed(self):
        info = self.mgr.get_status()
        assert info.status == ServiceStatus.NOT_INSTALLED

    def test_user_unit_exists(self):
        self.mgr._unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._unit_path.write_text("[Unit]\n")

        with patch.object(self.mgr, "_systemctl") as mock_sctl:
            mock_sctl.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=active\nMainPID=555\nUnitFileState=enabled\n",
            )
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.RUNNING
        assert info.pid == 555

    def test_system_unit_only(self):
        """Package-provided unit at /usr/lib/systemd/user/ is sufficient."""
        self.mgr._system_unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._system_unit_path.write_text("[Unit]\n")

        with patch.object(self.mgr, "_systemctl") as mock_sctl:
            mock_sctl.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=inactive\nMainPID=0\nUnitFileState=enabled\n",
            )
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.STOPPED
        assert info.enabled is True

    def test_start_skips_install_when_system_unit_exists(self):
        """start() should not call install() if system unit exists."""
        self.mgr._system_unit_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._system_unit_path.write_text("[Unit]\n")

        with patch.object(self.mgr, "_systemctl") as mock_sctl, \
             patch.object(self.mgr, "install") as mock_install:
            mock_sctl.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=inactive\nMainPID=0\nUnitFileState=enabled\n",
            )
            self.mgr.start()

        mock_install.assert_not_called()

    def test_start_installs_when_no_unit_exists(self):
        """start() should call install() when neither unit exists."""
        with patch.object(self.mgr, "_systemctl") as mock_sctl, \
             patch.object(self.mgr, "install", return_value=(True, "ok")) as mock_install:
            mock_sctl.return_value = MagicMock(
                returncode=0,
                stdout="ActiveState=inactive\nMainPID=0\nUnitFileState=disabled\n",
            )
            self.mgr.start()

        mock_install.assert_called_once()


# ── Self-healing: cmd_status auto-installs ───────────────────────────────


class TestCmdStatusSelfHealing:
    """cmd_status() should auto-install service when paired but not installed."""

    def test_auto_installs_when_paired_but_not_installed(self):
        mock_svc = MagicMock()
        mock_svc.get_status.side_effect = [
            ServiceInfo(status=ServiceStatus.NOT_INSTALLED),
            ServiceInfo(status=ServiceStatus.STOPPED, enabled=True),
        ]
        mock_svc.install.return_value = (True, "Installed")

        mock_pairing = MagicMock()
        mock_pairing.is_paired.return_value = True
        mock_pairing.get_paired_device.return_value = MagicMock(device_name="TestPC")

        args = MagicMock()

        with patch("yank.main.get_pairing_manager", return_value=mock_pairing), \
             patch("yank.main.get_service_manager", return_value=mock_svc), \
             patch("builtins.print") as mock_print:
            from yank.main import cmd_status
            cmd_status(args)

        mock_svc.install.assert_called_once()
        # Should print the auto-install message
        printed = " ".join(str(c) for c in mock_print.call_args_list)
        assert "auto-installed" in printed

    def test_no_auto_install_when_not_paired(self):
        mock_svc = MagicMock()
        mock_svc.get_status.return_value = ServiceInfo(status=ServiceStatus.NOT_INSTALLED)

        mock_pairing = MagicMock()
        mock_pairing.is_paired.return_value = False

        args = MagicMock()

        with patch("yank.main.get_pairing_manager", return_value=mock_pairing), \
             patch("yank.main.get_service_manager", return_value=mock_svc), \
             patch("builtins.print"):
            from yank.main import cmd_status
            cmd_status(args)

        mock_svc.install.assert_not_called()

    def test_no_auto_install_when_already_installed(self):
        mock_svc = MagicMock()
        mock_svc.get_status.return_value = ServiceInfo(
            status=ServiceStatus.STOPPED, enabled=True
        )

        mock_pairing = MagicMock()
        mock_pairing.is_paired.return_value = True
        mock_pairing.get_paired_device.return_value = MagicMock(device_name="TestPC")

        args = MagicMock()

        with patch("yank.main.get_pairing_manager", return_value=mock_pairing), \
             patch("yank.main.get_service_manager", return_value=mock_svc), \
             patch("builtins.print"):
            from yank.main import cmd_status
            cmd_status(args)

        mock_svc.install.assert_not_called()
