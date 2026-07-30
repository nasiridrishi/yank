"""
Windows Service Manager

Manages Yank using Task Scheduler for auto-start on login and a detached
process for the running service. Replaces the old pywin32 Windows Service
approach which ran in Session 0 and could not access the user's clipboard.
"""
import codecs
import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, List, Optional, Tuple

from yank.common.service_manager import ServiceInfo, ServiceManager, ServiceStatus

logger = logging.getLogger(__name__)

# ElementTree refuses str input that still carries an encoding declaration
# ("Unicode strings with encoding declaration are not supported"), and schtasks
# emits `<?xml version="1.0" encoding="UTF-16"?>`, so strip it after decoding.
_XML_DECLARATION_RE = re.compile(r"^\s*<\?xml.*?\?>", re.DOTALL)

# Byte-order mark left behind by some decoders; harmless to strip.
_BOM_CHAR = chr(0xFEFF)


class WindowsServiceManager(ServiceManager):

    TASK_NAME = "YankClipboardSync"

    # Process creation flags (winbase.h)
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    DETACHED_PROCESS = 0x00000008
    CREATE_NO_WINDOW = 0x08000000

    # Process access rights / control events / wait results
    PROCESS_TERMINATE = 0x0001
    SYNCHRONIZE = 0x00100000
    CTRL_BREAK_EVENT = 1
    WAIT_OBJECT_0 = 0x00000000

    # How long stop() waits for a clean shutdown before force-killing, and how
    # long it waits for the kill itself to land (so a caller that restarts
    # immediately does not race the singleton lock).
    GRACEFUL_STOP_TIMEOUT_MS = 5000
    FORCE_STOP_TIMEOUT_MS = 2000

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
                logger.info("Scheduled task points at a stale binary; reinstalling")
                ok, msg = self.stop()
                if not ok:
                    return False, f"Could not stop the running agent to reinstall: {msg}"
                ok, msg = self.install()
                if not ok:
                    return False, msg
            else:
                return True, f"Already running (PID {info.pid})"

        if not self._is_task_installed():
            ok, msg = self.install()
            if not ok:
                return False, msg

        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            args = self.get_service_args()

            # The child prints arbitrary clipboard text, so the log must not be
            # bound to the locale codepage (cp1252). Two separate things decide
            # that: this handle's encoding (what *we* would write) and the
            # child's own sys.stdout encoding, which Python derives from the
            # environment because the stream is a file, not a console — hence
            # PYTHONIOENCODING below. A PyInstaller-frozen child may run with an
            # isolated config that ignores PYTHON* vars; there the runtime hook
            # (hooks/hook-yank-console.py) is the remaining line of defence.
            env = dict(os.environ)
            env.setdefault("PYTHONIOENCODING", "utf-8:replace")

            # CREATE_NEW_PROCESS_GROUP makes the child's PID double as its
            # process-group id, which is what stop() addresses CTRL_BREAK to.
            creationflags = (
                self.DETACHED_PROCESS | self.CREATE_NEW_PROCESS_GROUP | self.CREATE_NO_WINDOW
            )

            # CreateProcess duplicates the handle into the child, so closing our
            # copy as soon as Popen returns is safe and keeps `yank start` from
            # leaking a file handle for the rest of its (short) life.
            with open(self._log_path, "a", encoding="utf-8", errors="replace") as log_handle:
                subprocess.Popen(
                    args,
                    creationflags=creationflags,
                    stdout=log_handle,
                    stderr=log_handle,
                    stdin=subprocess.DEVNULL,
                    env=env,
                )
            return True, "Started"
        except Exception as e:
            return False, str(e)

    def stop(self) -> Tuple[bool, str]:
        """Stop the agent, preferring a clean shutdown over a hard kill.

        A hard TerminateProcess leaves sockets and worker threads unclosed and
        skips release_singleton(), stranding the lock/PID pair in %TEMP% until
        stale-PID detection notices. main._run_foreground() installs signal
        handlers that stop the agent and release the lock, so we first try to
        deliver a console control event to the child's process group.

        That attempt is best-effort: GenerateConsoleCtrlEvent only reaches
        processes attached to *our* console, and start() launches the agent with
        DETACHED_PROCESS — it has no console at all. Task Scheduler's ONLOGON
        copy is equally detached. Delivery therefore fails on the common path,
        and the child would have to handle SIGBREAK (which is what CPython maps
        CTRL_BREAK onto) for it to shut down cleanly even when it lands. The
        TerminateProcess fallback below is not a corner case; it is the norm.
        """
        from yank.common.singleton import get_existing_instance_pid
        pid = get_existing_instance_pid()
        if not pid:
            return True, "Not running"

        if pid < 0:
            # Port is in use but the owning PID is unknown — nothing to signal.
            return False, "Running but PID unknown; stop manually or kill the process on port 9876"

        try:
            kernel32 = self._get_kernel32()
            handle = kernel32.OpenProcess(self.PROCESS_TERMINATE | self.SYNCHRONIZE, False, pid)
            if not handle:
                return False, f"Could not open process {pid}"

            try:
                if self._send_ctrl_break(kernel32, pid):
                    if self._wait_for_exit(kernel32, handle, self.GRACEFUL_STOP_TIMEOUT_MS):
                        return True, f"Stopped gracefully (PID {pid})"
                    logger.debug("PID %s did not exit after CTRL_BREAK; terminating", pid)

                kernel32.TerminateProcess(handle, 0)
                self._wait_for_exit(kernel32, handle, self.FORCE_STOP_TIMEOUT_MS)
                return True, f"Stopped (PID {pid})"
            finally:
                kernel32.CloseHandle(handle)
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
        """True when the installed task launches something other than we would.

        The macOS manager does the same by comparing plist ProgramArguments;
        here the equivalent source of truth is the task's Exec action.

        Unlike the macOS version this returns *False* when the task cannot be
        read or parsed. start() reacts to True by stopping and reinstalling, so
        treating an unreadable query as "stale" would turn every start into a
        stop/reinstall cycle.
        """
        installed = self._query_task_exec()
        if installed is None:
            return False

        command, arguments = installed
        installed_line = self._canonical_command_line(command, arguments)
        expected_line = self._canonical_command_line(*self._expected_exec())
        if installed_line == expected_line:
            return False

        logger.info(
            "Scheduled task runs %r but should run %r; reinstalling",
            installed_line, expected_line,
        )
        return True

    def _expected_exec(self) -> Tuple[str, str]:
        """The (command, arguments) install() would register right now."""
        args = self.get_service_args()
        return args[0], " ".join(args[1:])

    def _query_task_exec(self) -> Optional[Tuple[str, str]]:
        """Return (command, arguments) from the installed task, or None."""
        try:
            result = subprocess.run(
                ["schtasks", "/Query", "/TN", self.TASK_NAME, "/XML"],
                capture_output=True, timeout=10,
            )
        except Exception as e:
            logger.debug("schtasks query failed: %s", e)
            return None

        if result.returncode != 0:
            return None

        text = self._decode_schtasks_xml(result.stdout)
        if not text:
            return None

        try:
            root = ET.fromstring(text)
        except Exception as e:
            # ParseError for malformed XML, ValueError if an encoding
            # declaration survived the strip above. Either way we do not know
            # what is installed, so _needs_reinstall() must not guess.
            logger.debug("Could not parse schtasks XML: %s", e)
            return None

        # Task Scheduler XML lives in the
        # http://schemas.microsoft.com/windows/2004/02/mit/task namespace;
        # `{*}` matches any (or no) namespace so we do not depend on the exact
        # schema version schtasks emits.
        exec_el = root.find(".//{*}Actions/{*}Exec")
        if exec_el is None:
            exec_el = root.find(".//{*}Exec")
        if exec_el is None:
            return None

        command = exec_el.findtext("{*}Command")
        if command is None:
            return None

        return command, exec_el.findtext("{*}Arguments") or ""

    @staticmethod
    def _decode_schtasks_xml(raw: Any) -> Optional[str]:
        """Decode schtasks /XML output (UTF-16 with BOM in practice) to str."""
        if not raw:
            return None

        if isinstance(raw, (bytes, bytearray)):
            data = bytes(raw)
            if data.startswith(codecs.BOM_UTF16_LE) or data.startswith(codecs.BOM_UTF16_BE):
                text = data.decode("utf-16", errors="replace")
            elif data.startswith(codecs.BOM_UTF8):
                text = data.decode("utf-8-sig", errors="replace")
            elif b"\x00" in data[:64]:
                # UTF-16 without a BOM (seen when output is piped).
                text = data.decode("utf-16-le", errors="replace")
            else:
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = data.decode("cp1252", errors="replace")
        else:
            text = str(raw)

        text = _XML_DECLARATION_RE.sub("", text.lstrip(_BOM_CHAR), count=1).strip()
        return text or None

    @staticmethod
    def _canonical_command_line(command: str, arguments: str = "") -> str:
        """Flatten an Exec action into one comparable string.

        Command and Arguments are joined rather than compared field by field:
        we hand schtasks a single /TR string and it decides where to split it,
        so a task whose whole command line landed in <Command> must still
        compare equal — otherwise every start() would trigger a reinstall.

        Quotes are dropped, whitespace collapsed, separators unified and case
        folded, since Windows paths are case-insensitive and accept either
        slash. Both sides go through this, so the folding cannot hide a real
        difference in the binary path or the flags.
        """
        line = f"{command} {arguments}".replace('"', " ")
        return " ".join(line.split()).replace("/", "\\").lower()

    @staticmethod
    def _get_kernel32() -> Any:
        """Return a kernel32 binding with the prototypes stop() relies on.

        Isolated in a method so the import stays lazy (this module must import
        on non-Windows hosts, where ctypes.windll does not exist) and so tests
        can substitute a mock.

        A private WinDLL instance is used rather than the shared
        ctypes.windll.kernel32 so declaring argtypes/restype here cannot change
        how other modules (e.g. common/singleton.py) call the same functions.
        HANDLEs must be pointer-sized: with the default int prototypes a handle
        would round-trip through a 32-bit c_int.
        """
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]

        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GenerateConsoleCtrlEvent.argtypes = [ctypes.c_uint32, ctypes.c_uint32]
        kernel32.GenerateConsoleCtrlEvent.restype = ctypes.c_int
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int

        return kernel32

    def _send_ctrl_break(self, kernel32: Any, pid: int) -> bool:
        """Send CTRL_BREAK_EVENT to the process group whose id is `pid`.

        Returns False when the event could not be delivered, which is the usual
        outcome for a detached child (see stop()).
        """
        try:
            return bool(kernel32.GenerateConsoleCtrlEvent(self.CTRL_BREAK_EVENT, pid))
        except Exception as e:
            logger.debug("GenerateConsoleCtrlEvent failed for PID %s: %s", pid, e)
            return False

    def _wait_for_exit(self, kernel32: Any, handle: Any, timeout_ms: int) -> bool:
        """Wait up to timeout_ms for the process to exit. True if it did."""
        try:
            return bool(
                kernel32.WaitForSingleObject(handle, timeout_ms) == self.WAIT_OBJECT_0
            )
        except Exception as e:
            logger.debug("WaitForSingleObject failed: %s", e)
            return False
