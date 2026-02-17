"""
Linux systemd User Service Manager

Manages Yank as a systemd user service for auto-start on login
and crash recovery.
"""
import os
import subprocess
import logging
from pathlib import Path
from typing import Tuple, Optional, List

from yank.common.service_manager import ServiceManager, ServiceInfo, ServiceStatus

logger = logging.getLogger(__name__)

UNIT_TEMPLATE = """\
[Unit]
Description=Yank - LAN Clipboard Sync
After=graphical-session.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
{environment_lines}

[Install]
WantedBy=default.target
"""


class LinuxServiceManager(ServiceManager):

    UNIT_NAME = "yank.service"

    def __init__(self):
        config_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        self._unit_path = config_dir / "systemd" / "user" / self.UNIT_NAME

    def is_available(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "--user", "show-environment"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_log_command(self, lines: int = 50) -> Optional[List[str]]:
        return ["journalctl", "--user-unit", self.UNIT_NAME, "-n", str(lines), "--no-pager"]

    def get_log_follow_command(self) -> Optional[List[str]]:
        return ["journalctl", "--user-unit", self.UNIT_NAME, "-f"]

    # ── install / uninstall ──────────────────────────────────────────

    def install(self) -> Tuple[bool, str]:
        try:
            self._unit_path.parent.mkdir(parents=True, exist_ok=True)

            args = self.get_service_args()
            exec_start = " ".join(args)

            # Capture display-related env vars so the service can access
            # the user's display and clipboard.
            env_keys = [
                "DISPLAY",
                "WAYLAND_DISPLAY",
                "XDG_RUNTIME_DIR",
                "DBUS_SESSION_BUS_ADDRESS",
            ]
            env_lines = []
            for key in env_keys:
                val = os.environ.get(key)
                if val:
                    env_lines.append(f"Environment={key}={val}")

            environment_lines = "\n".join(env_lines)

            unit_content = UNIT_TEMPLATE.format(
                exec_start=exec_start,
                environment_lines=environment_lines,
            )

            self._unit_path.write_text(unit_content)

            self._systemctl("daemon-reload")
            self._systemctl("enable", self.UNIT_NAME)

            return True, f"Installed {self._unit_path}"
        except Exception as e:
            return False, str(e)

    def uninstall(self) -> Tuple[bool, str]:
        try:
            self._systemctl("disable", self.UNIT_NAME, check=False)

            if self._unit_path.exists():
                self._unit_path.unlink()

            self._systemctl("daemon-reload", check=False)

            return True, "Uninstalled"
        except Exception as e:
            return False, str(e)

    # ── start / stop ─────────────────────────────────────────────────

    def start(self) -> Tuple[bool, str]:
        if not self._unit_path.exists():
            ok, msg = self.install()
            if not ok:
                return False, msg

        info = self.get_status()
        if info.status == ServiceStatus.RUNNING:
            if self._needs_reinstall():
                self.stop()
                self.install()
            else:
                return True, f"Already running (PID {info.pid})"

        result = self._systemctl("start", self.UNIT_NAME, check=False)
        if result.returncode != 0:
            return False, f"systemctl start failed: {result.stderr.strip()}"

        return True, "Started"

    def stop(self) -> Tuple[bool, str]:
        info = self.get_status()
        if info.status != ServiceStatus.RUNNING:
            return True, "Not running"

        result = self._systemctl("stop", self.UNIT_NAME, check=False)
        if result.returncode != 0:
            return False, f"systemctl stop failed: {result.stderr.strip()}"

        return True, "Stopped"

    # ── status ───────────────────────────────────────────────────────

    def get_status(self) -> ServiceInfo:
        if not self._unit_path.exists():
            return ServiceInfo(status=ServiceStatus.NOT_INSTALLED)

        result = self._systemctl(
            "show", self.UNIT_NAME,
            "--property=ActiveState,MainPID,UnitFileState",
            check=False,
        )
        if result.returncode != 0:
            return ServiceInfo(status=ServiceStatus.UNKNOWN)

        props = {}
        for line in result.stdout.strip().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip()

        active = props.get("ActiveState", "")
        pid_str = props.get("MainPID", "0")
        unit_state = props.get("UnitFileState", "")

        pid = int(pid_str) if pid_str.isdigit() and int(pid_str) > 0 else None
        enabled = unit_state == "enabled"

        if active == "active":
            return ServiceInfo(status=ServiceStatus.RUNNING, pid=pid, enabled=enabled)
        elif active in ("inactive", "failed"):
            return ServiceInfo(status=ServiceStatus.STOPPED, enabled=enabled)
        else:
            return ServiceInfo(status=ServiceStatus.UNKNOWN, enabled=enabled)

    # ── internal helpers ─────────────────────────────────────────────

    def _needs_reinstall(self) -> bool:
        try:
            if not self._unit_path.exists():
                return False
            content = self._unit_path.read_text()
            expected_exec = " ".join(self.get_service_args())
            return f"ExecStart={expected_exec}" not in content
        except Exception:
            return True

    @staticmethod
    def _systemctl(*args, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["systemctl", "--user", *args],
            capture_output=True,
            text=True,
            timeout=10,
        )
