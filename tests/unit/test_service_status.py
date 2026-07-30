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

    def test_homebrew_running_wins_when_own_plist_is_not_loaded(self):
        """Both plists present but only the Homebrew agent is loaded."""
        self.mgr._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._plist_path.write_bytes(b"<plist/>")
        self.mgr._homebrew_plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                MagicMock(returncode=113, stdout="", stderr=""),   # com.yank.agent
                MagicMock(returncode=0, stdout="pid = 4242\n"),    # homebrew.mxcl.yank
            ]
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.RUNNING
        assert info.pid == 4242

    def test_status_survives_launchctl_timeout(self):
        self.mgr._plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.mgr._plist_path.write_bytes(b"<plist/>")

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = subprocess.TimeoutExpired(cmd="launchctl", timeout=10)
            info = self.mgr.get_status()

        assert info.status == ServiceStatus.STOPPED


# ── macOS: stop / uninstall / start act on the right label ───────────────


class _MacOSServiceTestBase:
    """Shared fixture: a MacOSServiceManager rooted in tmp_path.

    subprocess.run is patched out for every test in these classes so a real
    launchctl invocation can never escape and mutate the host's launchd state.
    """

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        with patch("yank.platform.macos.service.Path.home", return_value=tmp_path):
            from yank.platform.macos.service import MacOSServiceManager
            self.mgr = MacOSServiceManager()
        self.launch_agents = tmp_path / "Library" / "LaunchAgents"
        self.launch_agents.mkdir(parents=True)

        def _no_real_launchctl(*args, **kwargs):
            raise AssertionError(f"real subprocess call escaped: {args!r}")

        with patch("yank.platform.macos.service.subprocess.run") as mock_run:
            mock_run.side_effect = _no_real_launchctl
            self.run_mock = mock_run
            yield

    @staticmethod
    def _completed(returncode=0, stdout="", stderr=""):
        return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)

    def _install_own_plist(self):
        self.mgr._plist_path.write_bytes(b"<plist/>")

    def _install_homebrew_plist(self):
        self.mgr._homebrew_plist_path.write_bytes(b"<plist/>")


class TestMacOSStop(_MacOSServiceTestBase):
    """stop() must boot out the label that is actually loaded (issue #11)."""

    def test_stops_homebrew_label_when_only_homebrew_plist_exists(self):
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 777\n"),  # print homebrew.mxcl.yank
                self._completed(),                      # bootout
            ]
            ok, msg = self.mgr.stop()

        assert ok is True
        bootout_args = mock_lctl.call_args_list[-1][0]
        assert bootout_args[0] == "bootout"
        assert bootout_args[1].endswith("/homebrew.mxcl.yank")
        assert "brew services stop yank" in msg

    def test_stops_own_label_when_own_plist_is_loaded(self):
        self._install_own_plist()
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 12\n"),  # print com.yank.agent
                self._completed(),                     # bootout
            ]
            ok, msg = self.mgr.stop()

        assert (ok, msg) == (True, "Stopped")
        bootout_args = mock_lctl.call_args_list[-1][0]
        assert bootout_args[1].endswith("/com.yank.agent")

    def test_reports_failure_when_bootout_fails(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 12\n"),
                self._completed(returncode=1, stderr="Boot-out failed: 5: Input/output error\n"),
            ]
            ok, msg = self.mgr.stop()

        assert ok is False
        assert "Input/output error" in msg
        assert "com.yank.agent" in msg

    def test_reports_failure_when_bootout_fails_without_stderr(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 12\n"),
                self._completed(returncode=36, stderr=""),
            ]
            ok, msg = self.mgr.stop()

        assert ok is False
        assert "exit code 36" in msg

    def test_bootout_esrch_is_treated_as_already_stopped(self):
        """The agent can exit between the status probe and the bootout."""
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 12\n"),
                self._completed(returncode=3, stderr="Boot-out failed: 3: No such process\n"),
            ]
            ok, msg = self.mgr.stop()

        assert (ok, msg) == (True, "Not running")

    def test_reports_failure_when_bootout_times_out(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(stdout="pid = 12\n"),
                subprocess.TimeoutExpired(cmd="launchctl", timeout=10),
            ]
            ok, msg = self.mgr.stop()

        assert ok is False
        assert "timed out" in msg

    def test_not_running_does_not_bootout(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = self._completed(returncode=113)
            ok, msg = self.mgr.stop()

        assert (ok, msg) == (True, "Not running")
        assert all(call[0][0] == "print" for call in mock_lctl.call_args_list)

    def test_nothing_installed_is_not_running(self):
        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            ok, msg = self.mgr.stop()

        assert (ok, msg) == (True, "Not running")
        mock_lctl.assert_not_called()


class TestMacOSUninstall(_MacOSServiceTestBase):
    """uninstall() must not silently claim success for a brew-managed agent."""

    def test_homebrew_only_is_reported_not_deleted(self):
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            ok, msg = self.mgr.uninstall()

        assert ok is False
        assert "brew services stop yank" in msg
        # Homebrew's plist is left for brew to manage
        assert self.mgr._homebrew_plist_path.exists()
        mock_lctl.assert_not_called()

    def test_own_plist_removed_and_homebrew_reported(self):
        self._install_own_plist()
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = self._completed()
            ok, msg = self.mgr.uninstall()

        assert ok is True
        assert not self.mgr._plist_path.exists()
        assert self.mgr._homebrew_plist_path.exists()
        assert "brew services stop yank" in msg
        bootout_args = mock_lctl.call_args_list[-1][0]
        assert bootout_args[0] == "bootout"
        assert bootout_args[1].endswith("/com.yank.agent")

    def test_own_plist_only(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = self._completed()
            ok, msg = self.mgr.uninstall()

        assert (ok, msg) == (True, "Uninstalled")
        assert not self.mgr._plist_path.exists()

    def test_nothing_installed(self):
        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            ok, msg = self.mgr.uninstall()

        assert (ok, msg) == (True, "Not installed")
        mock_lctl.assert_not_called()

    def test_bootout_timeout_does_not_block_plist_removal(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = subprocess.TimeoutExpired(cmd="launchctl", timeout=10)
            ok, msg = self.mgr.uninstall()

        assert ok is True
        assert not self.mgr._plist_path.exists()


class TestMacOSStart(_MacOSServiceTestBase):
    """start() error handling and Homebrew awareness (issues #11, #15)."""

    def test_bootstrap_timeout_returns_error_not_traceback(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(returncode=113),  # print: not loaded
                subprocess.TimeoutExpired(cmd="launchctl", timeout=10),  # bootstrap
            ]
            ok, msg = self.mgr.start()

        assert ok is False
        assert "bootstrap timed out" in msg

    def test_kickstart_timeout_returns_error(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(returncode=113),  # print: not loaded
                self._completed(returncode=5),    # bootstrap fails
                subprocess.TimeoutExpired(cmd="launchctl", timeout=30),  # kickstart
            ]
            ok, msg = self.mgr.start()

        assert ok is False
        assert "kickstart timed out" in msg

    def test_already_running_under_homebrew_does_not_install(self):
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl, \
             patch.object(self.mgr, "install") as mock_install:
            mock_lctl.return_value = self._completed(stdout="pid = 31337\n")
            ok, msg = self.mgr.start()

        assert ok is True
        assert "31337" in msg
        mock_install.assert_not_called()
        assert not self.mgr._plist_path.exists()

    def test_stopped_homebrew_agent_is_bootstrapped_not_duplicated(self):
        """Never install a second RunAtLoad agent alongside Homebrew's."""
        self._install_homebrew_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl, \
             patch.object(self.mgr, "install") as mock_install:
            mock_lctl.side_effect = [
                self._completed(returncode=113),  # print: not loaded
                self._completed(),                # bootstrap
            ]
            ok, msg = self.mgr.start()

        assert (ok, msg) == (True, "Started")
        mock_install.assert_not_called()
        bootstrap_args = mock_lctl.call_args_list[-1][0]
        assert bootstrap_args[0] == "bootstrap"
        assert bootstrap_args[2] == str(self.mgr._homebrew_plist_path)

    def test_installs_own_plist_when_nothing_is_installed(self):
        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.return_value = self._completed()
            ok, msg = self.mgr.start()

        assert (ok, msg) == (True, "Started")
        assert self.mgr._plist_path.exists()
        bootstrap_args = mock_lctl.call_args_list[-1][0]
        assert bootstrap_args[0] == "bootstrap"
        assert bootstrap_args[2] == str(self.mgr._plist_path)

    def test_bootstrap_failure_is_reported(self):
        self._install_own_plist()

        with patch.object(self.mgr, "_launchctl") as mock_lctl:
            mock_lctl.side_effect = [
                self._completed(returncode=113),                      # print
                self._completed(returncode=5, stderr="Bootstrap failed\n"),
                self._completed(returncode=5, stderr="Bad request\n"),  # kickstart
            ]
            ok, msg = self.mgr.start()

        assert ok is False
        assert "Bad request" in msg


class TestMacOSLaunchctlWrapper(_MacOSServiceTestBase):
    """_launchctl must honour its `check` argument (issue #15)."""

    def test_check_is_passed_through_to_subprocess(self):
        self.run_mock.side_effect = None
        self.run_mock.return_value = self._completed()

        self.mgr._launchctl("print", "gui/501/com.yank.agent", check=False)
        assert self.run_mock.call_args.kwargs["check"] is False

        self.mgr._launchctl("print", "gui/501/com.yank.agent")
        assert self.run_mock.call_args.kwargs["check"] is True

    def test_check_true_raises_on_failure(self):
        def fake_run(cmd, **kwargs):
            # Mimic subprocess.run's own contract for check=True
            if kwargs.get("check"):
                raise subprocess.CalledProcessError(1, cmd)
            return self._completed(returncode=1)

        self.run_mock.side_effect = fake_run

        with pytest.raises(subprocess.CalledProcessError):
            self.mgr._launchctl("bootout", "gui/501/com.yank.agent")

        # check=False keeps the old behaviour: inspect returncode yourself
        assert self.mgr._launchctl(
            "bootout", "gui/501/com.yank.agent", check=False
        ).returncode == 1

    def test_timeout_is_passed_through(self):
        self.run_mock.side_effect = None
        self.run_mock.return_value = self._completed()

        self.mgr._launchctl("kickstart", "-k", "gui/501/com.yank.agent", timeout=30)
        assert self.run_mock.call_args.kwargs["timeout"] == 30


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
