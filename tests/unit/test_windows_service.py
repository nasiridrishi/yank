"""
Tests for the Windows service manager (yank.platform.windows.service).

These run on any host. The module under test touches Windows-only APIs
(schtasks, kernel32, detached Popen) at call time, never at import time, so
every one of them is mocked here:

- subprocess.run       -> canned `schtasks /Query /XML` output
- subprocess.Popen     -> never actually spawns the agent
- _get_kernel32()      -> MagicMock (ctypes.windll does not exist off Windows)
- builtins.open        -> the service log is never created

Nothing in this file installs, starts, or stops a real service.
"""
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from yank.common.service_manager import ServiceInfo, ServiceStatus
from yank.platform.windows.service import WindowsServiceManager

EXE = r"C:\Program Files\Yank\yank.exe"
SERVICE_ARGS = [EXE, "start", "--foreground"]

TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Date>2026-01-01T12:00:00</Date>
    <Author>DESKTOP-ABC\\user</Author>
    <URI>\\YankClipboardSync</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>DESKTOP-ABC\\user</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-21-1111111111-2222222222-3333333333-1001</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <StartWhenAvailable>false</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>{arguments}
    </Exec>
  </Actions>
</Task>
"""


def task_xml(command=EXE, arguments="start --foreground", quoted=True, encoding="utf-16"):
    """Build schtasks-style XML. Returns bytes (or str when encoding is None)."""
    command_text = f'"{command}"' if quoted else command
    args_text = f"\n      <Arguments>{arguments}</Arguments>" if arguments is not None else ""
    xml = TASK_XML.format(command=command_text, arguments=args_text)
    return xml if encoding is None else xml.encode(encoding)


def completed(stdout, returncode=0):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = b""
    return result


def make_kernel32(open_handle=0x1234, ctrl_break=1, wait_results=(0,)):
    kernel32 = MagicMock(name="kernel32")
    kernel32.OpenProcess.return_value = open_handle
    kernel32.GenerateConsoleCtrlEvent.return_value = ctrl_break
    kernel32.WaitForSingleObject.side_effect = list(wait_results)
    kernel32.TerminateProcess.return_value = 1
    kernel32.CloseHandle.return_value = 1
    return kernel32


def call_names(mock_obj):
    """Ordered list of child-method names called on a MagicMock."""
    return [name for name, _, _ in mock_obj.mock_calls]


@pytest.fixture
def mgr():
    manager = WindowsServiceManager()
    with patch.object(WindowsServiceManager, "get_service_args", return_value=list(SERVICE_ARGS)):
        yield manager


# ── module import ────────────────────────────────────────────────────────


class TestImportsOffWindows:
    def test_module_imports_and_instantiates(self):
        """No Windows-only import may run at import/construction time."""
        manager = WindowsServiceManager()
        assert manager.TASK_NAME == "YankClipboardSync"
        assert manager.get_log_path().endswith("yank.log")


# ── _needs_reinstall: schtasks /Query /XML parsing ───────────────────────


class TestNeedsReinstall:
    """#14: _needs_reinstall() was hardcoded False, so the reinstall branch in
    start() was dead code and upgrades kept launching the old exe path."""

    def test_queries_task_xml_as_bytes(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml())
            mgr._needs_reinstall()

        run.assert_called_once()
        assert run.call_args[0][0] == [
            "schtasks", "/Query", "/TN", "YankClipboardSync", "/XML",
        ]
        # text=True would mangle schtasks' UTF-16 output.
        assert run.call_args[1].get("text") is not True

    def test_matching_task_does_not_need_reinstall(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml())
            assert mgr._needs_reinstall() is False

    def test_stale_binary_path_needs_reinstall(self, mgr):
        stale = task_xml(command=r"C:\Users\user\AppData\Local\Yank\old\yank.exe")
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(stale)
            assert mgr._needs_reinstall() is True

    def test_stale_arguments_need_reinstall(self, mgr):
        """A task installed in dev mode (python -m yank) vs. a frozen exe."""
        stale = task_xml(command=r"C:\Python312\python.exe", arguments="-m yank start --foreground")
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(stale)
            assert mgr._needs_reinstall() is True

    def test_same_binary_different_flags_needs_reinstall(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml(arguments="start"))
            assert mgr._needs_reinstall() is True

    def test_quoting_case_and_separators_are_normalized(self, mgr):
        """Windows paths are case-insensitive and accept either separator, and
        schtasks may or may not report the quotes we passed via /TR."""
        equivalent = task_xml(
            command="c:/PROGRAM FILES/Yank/YANK.EXE",
            quoted=False,
            arguments="start   --foreground",
        )
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(equivalent)
            assert mgr._needs_reinstall() is False

    def test_unsplit_command_line_is_not_treated_as_stale(self, mgr):
        """schtasks decides where to split the /TR string. If it keeps the whole
        line in <Command>, that must still compare equal — otherwise every
        start() would stop and reinstall the service."""
        unsplit = task_xml(
            command=f'"{EXE}" start --foreground', quoted=False, arguments=None
        )
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(unsplit)
            assert mgr._needs_reinstall() is False

    def test_utf16_without_bom_is_decoded(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml(encoding="utf-16-le"))
            assert mgr._needs_reinstall() is False

    def test_utf8_output_is_decoded(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml(encoding="utf-8"))
            assert mgr._needs_reinstall() is False

    def test_str_output_with_encoding_declaration_is_handled(self, mgr):
        """ElementTree rejects str input carrying an encoding declaration; the
        declaration must be stripped rather than blowing up."""
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml(encoding=None))
            assert mgr._needs_reinstall() is False

    # — unknown state must never mean "reinstall" —

    def test_unparseable_output_returns_false(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(b"\x00\x01 not xml at all <<<")
            assert mgr._needs_reinstall() is False

    def test_truncated_xml_returns_false(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(b"<Task><Actions><Exec><Command>c:\\yank.exe")
            assert mgr._needs_reinstall() is False

    def test_empty_output_returns_false(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(b"")
            assert mgr._needs_reinstall() is False

    def test_query_failure_returns_false(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(b"ERROR: cannot find the file specified.", returncode=1)
            assert mgr._needs_reinstall() is False

    def test_schtasks_missing_returns_false(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run", side_effect=FileNotFoundError):
            assert mgr._needs_reinstall() is False

    def test_non_exec_action_returns_false(self, mgr):
        """A task rewritten to a ComHandler action has no Command to compare."""
        xml = (
            '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
            "<Actions><ComHandler><ClassId>{0}</ClassId></ComHandler></Actions></Task>"
        ).encode("utf-16")
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(xml)
            assert mgr._needs_reinstall() is False

    def test_missing_arguments_element_compares_as_empty(self, mgr):
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(task_xml(arguments=None))
            # Installed task takes no arguments, we expect "start --foreground".
            assert mgr._needs_reinstall() is True

    def test_namespaceless_xml_still_parses(self, mgr):
        xml = (
            "<Task><Actions><Exec>"
            f"<Command>{EXE}</Command><Arguments>start --foreground</Arguments>"
            "</Exec></Actions></Task>"
        ).encode("utf-16")
        with patch("yank.platform.windows.service.subprocess.run") as run:
            run.return_value = completed(xml)
            assert mgr._needs_reinstall() is False


# ── stop(): graceful first, hard kill as fallback ────────────────────────


class TestStop:
    """#14: stop() used to go straight to TerminateProcess, so the agent never
    closed its sockets/threads and never ran release_singleton()."""

    def _stop_with(self, mgr, kernel32, pid=4321):
        with patch("yank.common.singleton.get_existing_instance_pid", return_value=pid), \
                patch.object(WindowsServiceManager, "_get_kernel32", return_value=kernel32):
            return mgr.stop()

    def test_not_running_is_a_noop(self, mgr):
        kernel32 = make_kernel32()
        ok, msg = self._stop_with(mgr, kernel32, pid=None)
        assert (ok, msg) == (True, "Not running")
        assert kernel32.mock_calls == []

    def test_unknown_pid_does_not_signal_anything(self, mgr):
        kernel32 = make_kernel32()
        ok, msg = self._stop_with(mgr, kernel32, pid=-1)
        assert ok is False
        assert "PID unknown" in msg
        assert kernel32.mock_calls == []

    def test_graceful_ctrl_break_avoids_terminate(self, mgr):
        kernel32 = make_kernel32(ctrl_break=1, wait_results=(0,))
        ok, msg = self._stop_with(mgr, kernel32)

        assert ok is True
        assert msg == "Stopped gracefully (PID 4321)"
        kernel32.TerminateProcess.assert_not_called()
        assert call_names(kernel32) == [
            "OpenProcess", "GenerateConsoleCtrlEvent", "WaitForSingleObject", "CloseHandle",
        ]

    def test_ctrl_break_targets_the_process_group(self, mgr):
        kernel32 = make_kernel32()
        self._stop_with(mgr, kernel32)
        kernel32.GenerateConsoleCtrlEvent.assert_called_once_with(
            WindowsServiceManager.CTRL_BREAK_EVENT, 4321
        )

    def test_falls_back_to_terminate_when_process_ignores_ctrl_break(self, mgr):
        WAIT_TIMEOUT = 0x102
        kernel32 = make_kernel32(ctrl_break=1, wait_results=(WAIT_TIMEOUT, 0))
        ok, msg = self._stop_with(mgr, kernel32)

        assert ok is True
        assert msg == "Stopped (PID 4321)"
        assert call_names(kernel32) == [
            "OpenProcess",
            "GenerateConsoleCtrlEvent",
            "WaitForSingleObject",
            "TerminateProcess",
            "WaitForSingleObject",
            "CloseHandle",
        ]

    def test_undeliverable_ctrl_break_terminates_without_waiting(self, mgr):
        """The detached child has no console, so delivery usually fails —
        do not burn the graceful timeout in that case."""
        kernel32 = make_kernel32(ctrl_break=0, wait_results=(0,))
        ok, msg = self._stop_with(mgr, kernel32)

        assert (ok, msg) == (True, "Stopped (PID 4321)")
        assert call_names(kernel32) == [
            "OpenProcess", "GenerateConsoleCtrlEvent", "TerminateProcess",
            "WaitForSingleObject", "CloseHandle",
        ]

    def test_ctrl_break_raising_falls_back_to_terminate(self, mgr):
        kernel32 = make_kernel32(wait_results=(0,))
        kernel32.GenerateConsoleCtrlEvent.side_effect = OSError("no console")
        ok, _ = self._stop_with(mgr, kernel32)

        assert ok is True
        kernel32.TerminateProcess.assert_called_once()

    def test_graceful_timeout_is_bounded(self, mgr):
        kernel32 = make_kernel32(wait_results=(0,))
        self._stop_with(mgr, kernel32)
        handle, timeout = kernel32.WaitForSingleObject.call_args[0]
        assert handle == 0x1234
        assert timeout == WindowsServiceManager.GRACEFUL_STOP_TIMEOUT_MS

    def test_open_process_failure_is_reported(self, mgr):
        kernel32 = make_kernel32(open_handle=0)
        ok, msg = self._stop_with(mgr, kernel32)

        assert ok is False
        assert msg == "Could not open process 4321"
        kernel32.TerminateProcess.assert_not_called()
        kernel32.CloseHandle.assert_not_called()

    def test_handle_is_closed_even_when_terminate_raises(self, mgr):
        kernel32 = make_kernel32(ctrl_break=0)
        kernel32.TerminateProcess.side_effect = OSError("access denied")
        ok, msg = self._stop_with(mgr, kernel32)

        assert ok is False
        assert "access denied" in msg
        kernel32.CloseHandle.assert_called_once_with(0x1234)

    def test_kernel32_unavailable_is_reported(self, mgr):
        with patch("yank.common.singleton.get_existing_instance_pid", return_value=99), \
                patch.object(
                    WindowsServiceManager, "_get_kernel32", side_effect=AttributeError("windll")
                ):
            ok, msg = mgr.stop()
        assert ok is False
        assert "windll" in msg


# ── start(): log encoding, handle lifetime, reinstall branch ─────────────


class _StartHarness:
    """Patches everything start() would otherwise do for real."""

    def __init__(self, status=ServiceStatus.STOPPED, needs_reinstall=False):
        self.status = status
        self.needs_reinstall = needs_reinstall
        self.open_mock = mock_open()

    def __enter__(self):
        info = ServiceInfo(status=self.status, pid=777, enabled=True)
        self._patches = [
            patch.object(WindowsServiceManager, "get_status", return_value=info),
            patch.object(WindowsServiceManager, "_is_task_installed", return_value=True),
            patch.object(
                WindowsServiceManager, "_needs_reinstall", return_value=self.needs_reinstall
            ),
            patch.object(WindowsServiceManager, "stop", return_value=(True, "Stopped")),
            patch.object(WindowsServiceManager, "install", return_value=(True, "Created")),
            patch.object(Path, "mkdir"),
            patch("yank.platform.windows.service.subprocess.Popen"),
            patch("builtins.open", self.open_mock),
        ]
        started = [p.start() for p in self._patches]
        self.get_status, self.is_installed, self.needs, self.stop, self.install = started[:5]
        self.popen = started[6]
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


class TestStartLogHandle:
    """Part of #12: the detached child's stdout/stderr log was opened with the
    locale codepage, so emoji/CJK clipboard text raised UnicodeEncodeError."""

    def test_log_is_opened_as_utf8_with_replacement(self, mgr):
        with _StartHarness() as h:
            ok, msg = mgr.start()

        assert (ok, msg) == (True, "Started")
        args, kwargs = h.open_mock.call_args
        assert args[1] == "a"
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"

    def test_child_environment_forces_utf8_stdio(self, mgr):
        """The parent's open() encoding does not reach the child; the child
        picks its stdout encoding from the environment."""
        with _StartHarness() as h:
            mgr.start()

        env = h.popen.call_args[1]["env"]
        assert env["PYTHONIOENCODING"] == "utf-8:replace"
        assert "PATH" in env or "Path" in env  # inherited, not replaced

    def test_log_handle_is_closed_in_the_parent(self, mgr):
        with _StartHarness() as h:
            mgr.start()

        handle = h.open_mock.return_value
        assert h.popen.call_args[1]["stdout"] is handle
        assert h.popen.call_args[1]["stderr"] is handle
        handle.__exit__.assert_called()

    def test_child_keeps_its_own_process_group(self, mgr):
        """stop() delivers CTRL_BREAK to the group, so the flag must stay set."""
        with _StartHarness() as h:
            mgr.start()

        flags = h.popen.call_args[1]["creationflags"]
        assert flags & WindowsServiceManager.CREATE_NEW_PROCESS_GROUP
        assert flags & WindowsServiceManager.DETACHED_PROCESS


class TestStartReinstallBranch:
    def test_running_with_current_binary_is_left_alone(self, mgr):
        with _StartHarness(status=ServiceStatus.RUNNING, needs_reinstall=False) as h:
            ok, msg = mgr.start()

        assert (ok, msg) == (True, "Already running (PID 777)")
        h.popen.assert_not_called()
        h.install.assert_not_called()

    def test_running_with_stale_binary_is_restarted(self, mgr):
        with _StartHarness(status=ServiceStatus.RUNNING, needs_reinstall=True) as h:
            ok, msg = mgr.start()

        assert (ok, msg) == (True, "Started")
        h.stop.assert_called_once()
        h.install.assert_called_once()
        h.popen.assert_called_once()

    def test_reinstall_aborts_when_the_old_process_cannot_be_stopped(self, mgr):
        with _StartHarness(status=ServiceStatus.RUNNING, needs_reinstall=True) as h:
            h.stop.return_value = (False, "Could not open process 777")
            ok, msg = mgr.start()

        assert ok is False
        assert "Could not open process 777" in msg
        h.popen.assert_not_called()
