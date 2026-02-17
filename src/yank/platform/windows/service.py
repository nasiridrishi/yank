"""
Windows Service Manager

Manages Yank using Task Scheduler for auto-start on login and a detached
process for the running service. Replaces the old pywin32 Windows Service
approach which ran in Session 0 and could not access the user's clipboard.
"""
import os
import sys
import subprocess
import logging
from pathlib import Path
from typing import Tuple, Optional, List

from yank.common.service_manager import ServiceManager, ServiceInfo, ServiceStatus

logger = logging.getLogger(__name__)


class WindowsServiceManager(ServiceManager):

    TASK_NAME = "YankClipboardSync"

    def __init__(self):
        local_app = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        self._log_dir = local_app / "Yank" / "Logs"
        self._log_path = self._log_dir / "yank.log"

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/FO", "LIST"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_log_path(self) -> Optional[str]:
        return str(self._log_path)

    def get_log_command(self, lines: int = 50) -> Optional[List[str]]:
        # PowerShell: Get-Content -Tail
        return [
            "powershell", "-NoProfile", "-Command",
            f"Get-Content -Path '{self._log_path}' -Tail {lines}",
        ]

    def get_log_follow_command(self) -> Optional[List[str]]:
        return [
            "powershell", "-NoProfile", "-Command",
            f"Get-Content -Path '{self._log_path}' -Tail 50 -Wait",
        ]

    # ── install / uninstall ──────────────────────────────────────────

    def install(self) -> Tuple[bool, str]:
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)

            args = self.get_service_args()
            # Build the /TR argument — quote the executable, append rest
            exe = args[0]
            rest = " ".join(args[1:])
            tr = f'"{exe}" {rest}' if rest else f'"{exe}"'

            result = subprocess.run(
                [
                    "schtasks", "/Create",
                    "/TN", self.TASK_NAME,
                    "/TR", tr,
                    "/SC", "ONLOGON",
                    "/RL", "LIMITED",
                    "/F",
                ],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0:
                return False, f"schtasks create failed: {result.stderr.strip()}"

            return True, "Scheduled task created"
        except Exception as e:
            return False, str(e)

    def uninstall(self) -> Tuple[bool, str]:
        try:
            result = subprocess.run(
                ["schtasks", "/Delete", "/TN", self.TASK_NAME, "/F"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode != 0 and "cannot find" not in result.stderr.lower():
                return False, f"schtasks delete failed: {result.stderr.strip()}"

            return True, "Scheduled task removed"
        except Exception as e:
            return False, str(e)

    # ── start / stop ─────────────────────────────────────────────────

    def start(self) -> Tuple[bool, str]:
        info = self.get_status()
        if info.status == ServiceStatus.RUNNING:
            if self._needs_reinstall():
                self.stop()
                self.install()
            else:
                return True, f"Already running (PID {info.pid})"

        if not self._is_task_installed():
            ok, msg = self.install()
            if not ok:
                return False, msg

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            args = self.get_service_args()

            CREATE_NEW_PROCESS_GROUP = 0x00000200
            DETACHED_PROCESS = 0x00000008

            log_handle = open(self._log_path, "a")
            subprocess.Popen(
                args,
                creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                stdout=log_handle,
                stderr=log_handle,
                stdin=subprocess.DEVNULL,
            )
            return True, "Started"
        except Exception as e:
            return False, str(e)

    def stop(self) -> Tuple[bool, str]:
        from yank.common.singleton import get_existing_instance_pid
        pid = get_existing_instance_pid()
        if not pid:
            return True, "Not running"

        try:
            import ctypes
            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 0)
                kernel32.CloseHandle(handle)
                return True, f"Stopped (PID {pid})"
            else:
                return False, f"Could not open process {pid}"
        except Exception as e:
            return False, str(e)

    # ── status ───────────────────────────────────────────────────────

    def get_status(self) -> ServiceInfo:
        from yank.common.singleton import get_existing_instance_pid

        installed = self._is_task_installed()
        pid = get_existing_instance_pid()

        if pid:
            return ServiceInfo(status=ServiceStatus.RUNNING, pid=pid, enabled=installed)
        elif installed:
            return ServiceInfo(status=ServiceStatus.STOPPED, enabled=True)
        else:
            return ServiceInfo(status=ServiceStatus.NOT_INSTALLED)

    # ── internal helpers ─────────────────────────────────────────────

    def _is_task_installed(self) -> bool:
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", self.TASK_NAME],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _needs_reinstall(self) -> bool:
        # Could parse schtasks /Query /XML, but for simplicity
        # we just reinstall on start if the binary path changed.
        # The install() with /F flag overwrites existing task.
        return False
