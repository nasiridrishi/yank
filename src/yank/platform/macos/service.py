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
    HOMEBREW_PLIST_NAME = "homebrew.mxcl.yank.plist"
    HOMEBREW_LABEL = "homebrew.mxcl.yank"

    # launchctl reports POSIX errno values; ESRCH means "no such service loaded"
    _ESRCH = 3

    def __init__(self):
        self._plist_path = Path.home() / "Library" / "LaunchAgents" / self.PLIST_NAME
        self._homebrew_plist_path = Path.home() / "Library" / "LaunchAgents" / self.HOMEBREW_PLIST_NAME
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
        """Remove our own LaunchAgent.

        A Homebrew-managed agent is deliberately left alone: deleting
        ``homebrew.mxcl.yank.plist`` behind brew's back desyncs
        ``brew services list``, so we report it instead of guessing.
        """
        try:
            own_installed = self._plist_path.exists()
            homebrew_installed = self._homebrew_plist_path.exists()

            if own_installed:
                # Bootout first (ignoring errors if not loaded)
                try:
                    self._launchctl(
                        "bootout", f"gui/{self._uid}/{self.SERVICE_LABEL}", check=False
                    )
                except subprocess.TimeoutExpired:
                    logger.warning("launchctl bootout timed out for %s", self.SERVICE_LABEL)
                self._plist_path.unlink()

            if homebrew_installed:
                brew_note = (
                    f"{self.HOMEBREW_LABEL} is managed by Homebrew and was left in place - "
                    "run 'brew services stop yank' to remove it"
                )
                logger.warning("uninstall: %s", brew_note)
                if own_installed:
                    return True, f"Uninstalled {self.SERVICE_LABEL}. Note: {brew_note}"
                return False, f"Not uninstalled: {brew_note}"

            if not own_installed:
                return True, "Not installed"

            return True, "Uninstalled"
        except Exception as e:
            return False, str(e)

    # ── start / stop ─────────────────────────────────────────────────

    def start(self) -> Tuple[bool, str]:
        label, info = self._resolve()

        if info.status == ServiceStatus.RUNNING:
            if label == self.HOMEBREW_LABEL:
                return True, f"Already running (PID {info.pid}, managed by Homebrew)"
            if not self._needs_reinstall():
                return True, f"Already running (PID {info.pid})"
            # Stale binary path in our plist — reload it.
            ok, msg = self.stop()
            if not ok:
                return False, msg
            ok, msg = self.install()
            if not ok:
                return False, msg
            label = self.SERVICE_LABEL
        elif label != self.HOMEBREW_LABEL:
            # Nothing installed, or only our own plist: make sure it exists.
            # A Homebrew-managed agent is loaded as-is so we never end up with
            # two RunAtLoad agents racing for the same port.
            if not self._plist_path.exists():
                ok, msg = self.install()
                if not ok:
                    return False, msg
            label = self.SERVICE_LABEL

        plist_path = self._plist_for(label)

        # Bootstrap (load) the agent
        try:
            result = self._launchctl(
                "bootstrap", f"gui/{self._uid}", str(plist_path), check=False
            )
        except subprocess.TimeoutExpired:
            return False, "launchctl bootstrap timed out"

        if result.returncode != 0:
            # May already be loaded — try kickstart (with longer timeout)
            try:
                result = self._launchctl(
                    "kickstart", "-k", f"gui/{self._uid}/{label}",
                    check=False, timeout=30,
                )
            except subprocess.TimeoutExpired:
                return False, "launchctl kickstart timed out"
            if result.returncode != 0:
                return False, f"launchctl failed: {self._stderr(result)}"

        return True, "Started"

    def stop(self) -> Tuple[bool, str]:
        label, info = self._resolve()
        if label is None or info.status != ServiceStatus.RUNNING:
            return True, "Not running"

        # Must bootout to prevent KeepAlive from restarting the process.
        # Act on whichever label is actually loaded — a Homebrew install runs
        # under homebrew.mxcl.yank, not com.yank.agent.
        try:
            result = self._launchctl("bootout", f"gui/{self._uid}/{label}", check=False)
        except subprocess.TimeoutExpired:
            return False, "launchctl bootout timed out"

        if result.returncode == self._ESRCH:
            # Raced with the agent exiting on its own — it is gone either way.
            return True, "Not running"

        if result.returncode != 0:
            return False, f"launchctl bootout {label} failed: {self._stderr(result)}"

        if label == self.HOMEBREW_LABEL:
            return True, (
                "Stopped homebrew.mxcl.yank. Run 'brew services stop yank' "
                "so it stays stopped after a reboot."
            )

        return True, "Stopped"

    # ── status ───────────────────────────────────────────────────────

    def get_status(self) -> ServiceInfo:
        _label, info = self._resolve()
        return info

    # ── internal helpers ─────────────────────────────────────────────

    def _resolve(self) -> Tuple[Optional[str], ServiceInfo]:
        """Work out which LaunchAgent label actually governs Yank.

        Yank can be installed by us (``com.yank.agent``) or by Homebrew
        (``homebrew.mxcl.yank``), and both plists can be present at once.
        Whichever label is actually loaded and running wins, so that
        start()/stop() act on the agent the user can see; ours breaks ties.

        Returns ``(label, info)``. ``label`` is None only when nothing is
        installed at all; otherwise it is the label to act on.

        Caveat: if both agents were somehow running at once, the tie-break
        picks ours, so stop() would boot out ours and leave the Homebrew one
        running while reporting "Stopped". That state is largely unreachable —
        singleton.py makes the loser of the port-9876 bind exit — and it is no
        worse than the old behaviour, which never looked at the Homebrew label
        at all.
        """
        candidates: List[str] = []
        if self._plist_path.exists():
            candidates.append(self.SERVICE_LABEL)
        if self._homebrew_plist_path.exists():
            candidates.append(self.HOMEBREW_LABEL)

        if not candidates:
            return None, ServiceInfo(status=ServiceStatus.NOT_INSTALLED)

        for label in candidates:
            info = self._probe(label)
            if info is not None and info.status == ServiceStatus.RUNNING:
                return label, info

        # Installed, but nothing is loaded and running.
        return candidates[0], ServiceInfo(status=ServiceStatus.STOPPED, enabled=True)

    def _probe(self, label: str) -> Optional[ServiceInfo]:
        """Query launchctl for one label. Returns None when it is not loaded."""
        try:
            result = self._launchctl("print", f"gui/{self._uid}/{label}", check=False)
        except subprocess.TimeoutExpired:
            logger.warning("launchctl print timed out for %s", label)
            return None

        if result.returncode != 0:
            return None

        # Parse PID from launchctl print output
        pid = self._parse_pid(result.stdout)
        if pid and pid > 0:
            return ServiceInfo(status=ServiceStatus.RUNNING, pid=pid, enabled=True)

        return ServiceInfo(status=ServiceStatus.STOPPED, enabled=True)

    def _plist_for(self, label: str) -> Path:
        if label == self.HOMEBREW_LABEL:
            return self._homebrew_plist_path
        return self._plist_path

    @staticmethod
    def _stderr(result: subprocess.CompletedProcess) -> str:
        """Human-readable failure detail from a launchctl result."""
        err = (result.stderr or "").strip()
        return err or f"exit code {result.returncode}"

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
        """Run launchctl.

        With ``check=True`` a non-zero exit raises ``CalledProcessError``; every
        caller here passes ``check=False`` and inspects ``returncode`` itself,
        because launchctl uses non-zero exits for ordinary states such as
        "service not loaded".
        """
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
