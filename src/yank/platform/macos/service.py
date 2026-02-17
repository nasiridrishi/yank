"""
macOS LaunchAgent Service Manager

Manages Yank as a LaunchAgent via launchctl for auto-start on login
and crash recovery.
"""
import os
import plistlib
import subprocess
import logging
from pathlib import Path
from typing import Tuple, Optional, List

from yank.common.service_manager import ServiceManager, ServiceInfo, ServiceStatus

logger = logging.getLogger(__name__)


class MacOSServiceManager(ServiceManager):

    PLIST_NAME = "com.yank.agent.plist"

    def __init__(self):
        self._plist_path = Path.home() / "Library" / "LaunchAgents" / self.PLIST_NAME
        self._log_dir = Path.home() / "Library" / "Logs" / "Yank"
        self._log_path = self._log_dir / "yank.log"
        self._uid = os.getuid()

    def is_available(self) -> bool:
        return os.path.isfile("/bin/launchctl")

    def get_log_path(self) -> Optional[str]:
        return str(self._log_path)

    def get_log_command(self, lines: int = 50) -> Optional[List[str]]:
        return ["tail", "-n", str(lines), str(self._log_path)]

    def get_log_follow_command(self) -> Optional[List[str]]:
        return ["tail", "-f", str(self._log_path)]

    # ── install / uninstall ──────────────────────────────────────────

    def install(self) -> Tuple[bool, str]:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            self._plist_path.parent.mkdir(parents=True, exist_ok=True)

            args = self.get_service_args()
            plist = {
                "Label": self.SERVICE_LABEL,
                "ProgramArguments": args,
                "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False},
                "StandardOutPath": str(self._log_path),
                "StandardErrorPath": str(self._log_path),
                "EnvironmentVariables": {
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
                },
            }

            with open(self._plist_path, "wb") as f:
                plistlib.dump(plist, f)

            return True, f"Installed {self._plist_path}"
        except Exception as e:
            return False, str(e)

    def uninstall(self) -> Tuple[bool, str]:
        try:
            # Bootout first (ignoring errors if not loaded)
            self._launchctl("bootout", f"gui/{self._uid}/{self.SERVICE_LABEL}", check=False)

            if self._plist_path.exists():
                self._plist_path.unlink()

            return True, "Uninstalled"
        except Exception as e:
            return False, str(e)

    # ── start / stop ─────────────────────────────────────────────────

    def start(self) -> Tuple[bool, str]:
        if not self._plist_path.exists():
            ok, msg = self.install()
            if not ok:
                return False, msg

        # Check if already loaded
        info = self.get_status()
        if info.status == ServiceStatus.RUNNING:
            if self._needs_reinstall():
                self.stop()
                self.install()
            else:
                return True, f"Already running (PID {info.pid})"

        # Bootstrap (load) the agent
        result = self._launchctl("bootstrap", f"gui/{self._uid}", str(self._plist_path), check=False)
        if result.returncode != 0:
            # May already be loaded — try kickstart (with longer timeout)
            try:
                result = self._launchctl(
                    "kickstart", "-k", f"gui/{self._uid}/{self.SERVICE_LABEL}",
                    check=False, timeout=30,
                )
            except subprocess.TimeoutExpired:
                return False, "launchctl kickstart timed out"
            if result.returncode != 0:
                return False, f"launchctl failed: {result.stderr.strip()}"

        return True, "Started"

    def stop(self) -> Tuple[bool, str]:
        info = self.get_status()
        if info.status != ServiceStatus.RUNNING:
            return True, "Not running"

        # Must bootout to prevent KeepAlive from restarting the process
        self._launchctl("bootout", f"gui/{self._uid}/{self.SERVICE_LABEL}", check=False)

        return True, "Stopped"

    # ── status ───────────────────────────────────────────────────────

    def get_status(self) -> ServiceInfo:
        if not self._plist_path.exists():
            return ServiceInfo(status=ServiceStatus.NOT_INSTALLED)

        result = self._launchctl("print", f"gui/{self._uid}/{self.SERVICE_LABEL}", check=False)
        if result.returncode != 0:
            return ServiceInfo(status=ServiceStatus.STOPPED, enabled=True)

        # Parse PID from launchctl print output
        pid = self._parse_pid(result.stdout)
        if pid and pid > 0:
            return ServiceInfo(status=ServiceStatus.RUNNING, pid=pid, enabled=True)

        return ServiceInfo(status=ServiceStatus.STOPPED, enabled=True)

    # ── internal helpers ─────────────────────────────────────────────

    def _needs_reinstall(self) -> bool:
        """Check if plist ProgramArguments matches current binary."""
        try:
            if not self._plist_path.exists():
                return False
            with open(self._plist_path, "rb") as f:
                plist = plistlib.load(f)
            installed_args = plist.get("ProgramArguments", [])
            return installed_args != self.get_service_args()
        except Exception:
            return True

    def _parse_pid(self, output: str) -> Optional[int]:
        """Extract PID from launchctl print output."""
        for line in output.splitlines():
            line = line.strip()
            if line.startswith("pid ="):
                try:
                    return int(line.split("=")[1].strip())
                except (ValueError, IndexError):
                    pass
        return None

    @staticmethod
    def _launchctl(*args, check: bool = True, timeout: int = 10) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
