"""
Service Manager Abstraction

Provides a cross-platform interface for managing Yank as a background service
with auto-start on login. Platform-specific implementations handle:
- macOS: LaunchAgent (launchctl)
- Linux: systemd user service (systemctl --user)
- Windows: Task Scheduler + detached process
"""
import os
import sys
import shutil
import signal
import logging
import subprocess
from abc import ABC, abstractmethod
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Tuple, List

logger = logging.getLogger(__name__)


class ServiceStatus(Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    NOT_INSTALLED = "not_installed"
    UNKNOWN = "unknown"


@dataclass
class ServiceInfo:
    status: ServiceStatus
    pid: Optional[int] = None
    enabled: bool = False  # auto-start on login


class ServiceManager(ABC):
    """Abstract base for platform service managers."""

    SERVICE_LABEL = "com.yank.agent"

    def get_binary_path(self) -> str:
        """Detect the yank binary path.

        Priority:
        1. PyInstaller frozen exe (sys._MEIPASS parent)
        2. shutil.which('yank')
        3. sys.executable (python interpreter for dev)
        """
        # PyInstaller frozen executable
        if getattr(sys, 'frozen', False):
            return sys.executable

        # Installed as 'yank' command (pip, homebrew, apt)
        which = shutil.which('yank')
        if which:
            return which

        # Development: use python interpreter with -m yank
        return sys.executable

    def get_service_args(self) -> List[str]:
        """Return the command-line args the service should invoke."""
        binary = self.get_binary_path()

        if getattr(sys, 'frozen', False) or shutil.which('yank'):
            return [binary, 'start', '--foreground']
        else:
            # Development mode: python -m yank start --foreground
            return [binary, '-m', 'yank', 'start', '--foreground']

    @abstractmethod
    def install(self) -> Tuple[bool, str]:
        """Install the service for auto-start on login."""
        ...

    @abstractmethod
    def uninstall(self) -> Tuple[bool, str]:
        """Remove the service."""
        ...

    @abstractmethod
    def start(self) -> Tuple[bool, str]:
        """Start the service."""
        ...

    @abstractmethod
    def stop(self) -> Tuple[bool, str]:
        """Stop the service."""
        ...

    @abstractmethod
    def get_status(self) -> ServiceInfo:
        """Get current service status."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if this service manager can be used on this system."""
        ...

    def get_log_path(self) -> Optional[str]:
        """Return the log file path, if applicable."""
        return None

    def get_log_command(self, lines: int = 50) -> Optional[List[str]]:
        """Return command to show last N log lines, or None."""
        return None

    def get_log_follow_command(self) -> Optional[List[str]]:
        """Return command to follow logs, or None."""
        return None

    def install_and_start(self) -> Tuple[bool, str]:
        """Install service and start it. Called after pair/join."""
        # Check if already installed with correct binary
        info = self.get_status()
        if info.status == ServiceStatus.RUNNING:
            if self._needs_reinstall():
                self.stop()
                self.uninstall()
            else:
                return True, "Already running"

        ok, msg = self.install()
        if not ok:
            return False, msg

        ok, msg = self.start()
        if not ok:
            return False, f"Installed but failed to start: {msg}"

        return True, "Service installed and started"

    def stop_and_uninstall(self) -> Tuple[bool, str]:
        """Stop service and uninstall. Called by unpair."""
        info = self.get_status()

        if info.status == ServiceStatus.RUNNING:
            self.stop()

        if info.status != ServiceStatus.NOT_INSTALLED:
            return self.uninstall()

        return True, "Not installed"

    def _needs_reinstall(self) -> bool:
        """Override in subclass to detect stale binary paths in service config."""
        return False


class FallbackServiceManager(ServiceManager):
    """Fallback when no platform service manager is available.

    Spawns a detached subprocess for start(). No auto-start on login.
    """

    def is_available(self) -> bool:
        return True

    def install(self) -> Tuple[bool, str]:
        return True, "No service manager available; no auto-start configured"

    def uninstall(self) -> Tuple[bool, str]:
        return True, "Nothing to uninstall"

    def start(self) -> Tuple[bool, str]:
        args = self.get_service_args()
        try:
            if os.name == 'nt':
                # Windows: detached process
                CREATE_NEW_PROCESS_GROUP = 0x00000200
                DETACHED_PROCESS = 0x00000008
                subprocess.Popen(
                    args,
                    creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                )
            else:
                # Unix: double fork via subprocess
                subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                )
            return True, "Started (no auto-start on login)"
        except Exception as e:
            return False, str(e)

    def stop(self) -> Tuple[bool, str]:
        from yank.common.singleton import get_existing_instance_pid
        pid = get_existing_instance_pid()
        if not pid:
            return True, "Not running"

        try:
            if os.name == 'nt':
                import ctypes
                PROCESS_TERMINATE = 0x0001
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
                if handle:
                    kernel32.TerminateProcess(handle, 0)
                    kernel32.CloseHandle(handle)
                else:
                    return False, f"Could not open process {pid}"
            else:
                os.kill(pid, signal.SIGTERM)
            return True, f"Stopped (PID {pid})"
        except ProcessLookupError:
            return True, "Already stopped"
        except PermissionError:
            return False, "Permission denied"
        except Exception as e:
            return False, str(e)

    def get_status(self) -> ServiceInfo:
        from yank.common.singleton import get_existing_instance_pid
        pid = get_existing_instance_pid()
        if pid:
            return ServiceInfo(status=ServiceStatus.RUNNING, pid=pid, enabled=False)
        return ServiceInfo(status=ServiceStatus.STOPPED, enabled=False)


def get_service_manager() -> ServiceManager:
    """Factory: detect platform and return the appropriate ServiceManager."""
    system = sys.platform

    if system == 'darwin':
        try:
            from yank.platform.macos.service import MacOSServiceManager
            mgr = MacOSServiceManager()
            if mgr.is_available():
                return mgr
        except ImportError:
            pass
    elif system.startswith('linux'):
        try:
            from yank.platform.linux.service import LinuxServiceManager
            mgr = LinuxServiceManager()
            if mgr.is_available():
                return mgr
        except ImportError:
            pass
    elif system == 'win32':
        try:
            from yank.platform.windows.service import WindowsServiceManager
            mgr = WindowsServiceManager()
            if mgr.is_available():
                return mgr
        except ImportError:
            pass

    return FallbackServiceManager()
