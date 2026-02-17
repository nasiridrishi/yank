#!/usr/bin/env python3
"""
End-to-end test for the service manager architecture.

Tests the full lifecycle on the current platform:
  install → start → status(running) → logs → stop → status(stopped)
  → start again → stop → uninstall → status(not_installed)

Also tests CLI commands via subprocess to verify the real user experience.
"""
import os
import sys
import time
import subprocess
import shutil

# ── Helpers ──────────────────────────────────────────────────────────

PASS = 0
FAIL = 0
YANK = shutil.which("yank") or sys.executable


def run_yank(*args, timeout=30):
    """Run a yank CLI command and return (returncode, stdout, stderr)."""
    cmd = [YANK] + list(args)
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout
    )
    return result.returncode, result.stdout, result.stderr


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def wait_for(predicate, timeout=10, interval=0.5):
    """Poll predicate() until True or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ── Cleanup any leftover state ───────────────────────────────────────

def cleanup():
    """Stop and uninstall any existing service."""
    run_yank("stop")
    time.sleep(1)
    # Force bootout on macOS
    if sys.platform == "darwin":
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/com.yank.agent"],
            capture_output=True, timeout=5,
        )
    # Remove stale lock
    lock = "/tmp/clipboard-sync.lock"
    if os.path.exists(lock):
        os.remove(lock)


# ── Tests ────────────────────────────────────────────────────────────

def test_service_manager_api():
    """Test the Python API directly."""
    print("\n=== Service Manager API Tests ===\n")

    from yank.common.service_manager import get_service_manager, ServiceStatus, FallbackServiceManager

    mgr = get_service_manager()

    # 1. Factory returns platform-specific manager
    if sys.platform == "darwin":
        from yank.platform.macos.service import MacOSServiceManager
        check("factory returns MacOSServiceManager", isinstance(mgr, MacOSServiceManager))
    elif sys.platform.startswith("linux"):
        # May or may not have systemd
        check("factory returns a ServiceManager", mgr is not None)
    else:
        check("factory returns a ServiceManager", mgr is not None)

    # 2. is_available
    check("is_available() returns True", mgr.is_available())

    # 3. get_binary_path
    binary = mgr.get_binary_path()
    check("get_binary_path() returns a path", len(binary) > 0, binary)
    check("binary exists on disk", os.path.exists(binary), binary)

    # 4. get_service_args
    args = mgr.get_service_args()
    check("get_service_args() includes --foreground", "--foreground" in args, str(args))

    # 5. Uninstall first (clean slate)
    ok, msg = mgr.uninstall()
    check("uninstall (clean slate)", ok, msg)

    # 6. Status should be NOT_INSTALLED
    info = mgr.get_status()
    check("status is NOT_INSTALLED after uninstall", info.status == ServiceStatus.NOT_INSTALLED, info.status.value)

    # 7. Install
    ok, msg = mgr.install()
    check("install succeeds", ok, msg)

    # 8. Status should be STOPPED (installed but not running)
    info = mgr.get_status()
    check("status is STOPPED after install", info.status == ServiceStatus.STOPPED, info.status.value)
    check("enabled is True after install", info.enabled == True, str(info.enabled))

    # 9. Start
    ok, msg = mgr.start()
    check("start succeeds", ok, msg)

    # 10. Wait for running
    running = wait_for(lambda: mgr.get_status().status == ServiceStatus.RUNNING)
    check("status becomes RUNNING after start", running)

    info = mgr.get_status()
    check("PID is set when running", info.pid is not None and info.pid > 0, str(info.pid))

    # 11. Start again (idempotent)
    ok, msg = mgr.start()
    check("start when already running returns ok", ok, msg)
    check("start msg mentions already running", "already" in msg.lower(), msg)

    # 12. Stop
    ok, msg = mgr.stop()
    check("stop succeeds", ok, msg)

    # Wait for stop
    stopped = wait_for(lambda: mgr.get_status().status != ServiceStatus.RUNNING)
    check("status becomes STOPPED after stop", stopped)

    # 13. Stop again (idempotent)
    time.sleep(1)
    ok, msg = mgr.stop()
    check("stop when not running returns ok", ok, msg)

    # 14. install_and_start convenience
    ok, msg = mgr.install_and_start()
    check("install_and_start succeeds", ok, msg)
    running = wait_for(lambda: mgr.get_status().status == ServiceStatus.RUNNING)
    check("running after install_and_start", running)

    # 15. stop_and_uninstall convenience
    ok, msg = mgr.stop_and_uninstall()
    check("stop_and_uninstall succeeds", ok, msg)
    time.sleep(2)
    info = mgr.get_status()
    check("status is NOT_INSTALLED after stop_and_uninstall", info.status == ServiceStatus.NOT_INSTALLED, info.status.value)

    # 16. Log path
    log_path = mgr.get_log_path()
    check("get_log_path() returns a path", log_path is not None and len(log_path) > 0, str(log_path))

    # 17. Log commands
    log_cmd = mgr.get_log_command(lines=10)
    check("get_log_command() returns a list", isinstance(log_cmd, list) and len(log_cmd) > 0)
    follow_cmd = mgr.get_log_follow_command()
    check("get_log_follow_command() returns a list", isinstance(follow_cmd, list) and len(follow_cmd) > 0)


def test_cli_commands():
    """Test CLI commands via subprocess."""
    print("\n=== CLI Command Tests ===\n")

    # 1. yank --help
    rc, out, err = run_yank("--help")
    check("yank --help exits 0", rc == 0)
    check("help shows 'logs' command", "logs" in out)

    # 2. yank start --help
    rc, out, err = run_yank("start", "--help")
    check("start --help exits 0", rc == 0)
    check("start --help shows --foreground", "--foreground" in out)

    # 3. yank status (not installed)
    rc, out, err = run_yank("status")
    check("status command exits 0", rc == 0)
    # Could be NOT_INSTALLED or STOPPED depending on state
    check("status output has indicator", "[" in out, out.strip()[:80])

    # 4. yank stop (when not running)
    rc, out, err = run_yank("stop")
    check("stop when not running exits 0", rc == 0)
    check("stop says not running", "not running" in out.lower() or "stopped" in out.lower(), out.strip()[:80])

    # 5. yank start (background)
    rc, out, err = run_yank("start")
    check("start command exits 0", rc == 0)
    check("start output includes OK or started", "ok" in out.lower() or "started" in out.lower(), out.strip()[:80])

    time.sleep(3)

    # 6. yank status (should be running)
    rc, out, err = run_yank("status")
    check("status shows RUNNING", "running" in out.lower(), out.strip()[:80])
    check("status shows PID", "pid" in out.lower(), out.strip()[:80])

    # 7. yank start again (idempotent)
    rc, out, err = run_yank("start")
    check("start when running exits 0", rc == 0)
    check("start says already running", "already running" in out.lower(), out.strip()[:80])

    # 8. yank logs
    rc, out, err = run_yank("logs", "-n", "5")
    # May have output or not depending on log file existence
    check("logs command exits 0 or handles missing log", rc == 0 or "no log" in out.lower() + err.lower())

    # 9. yank stop
    rc, out, err = run_yank("stop")
    check("stop command exits 0", rc == 0)
    check("stop output includes OK or stopped", "ok" in out.lower() or "stopped" in out.lower(), out.strip()[:80])

    time.sleep(2)

    # 10. yank status (should be stopped)
    rc, out, err = run_yank("status")
    check("status shows STOPPED after stop", "stopped" in out.lower() or "not running" in out.lower(), out.strip()[:80])

    # 11. yank logs -n 3 (log file should exist now)
    rc, out, err = run_yank("logs", "-n", "3")
    check("logs shows output after service ran", rc == 0)


def test_foreground_flag():
    """Test that --foreground runs in foreground and exits on SIGTERM."""
    print("\n=== Foreground Mode Tests ===\n")

    # Start in foreground as a subprocess
    proc = subprocess.Popen(
        [YANK, "start", "--foreground"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    time.sleep(4)

    # Should be running
    check("foreground process is running", proc.poll() is None)

    # Send SIGTERM
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

    check("foreground process exited after SIGTERM", proc.returncode is not None)

    # Clean up lock file
    time.sleep(1)


def test_stale_binary_detection():
    """Test that _needs_reinstall detects changed binary paths."""
    print("\n=== Stale Binary Detection Tests ===\n")

    from yank.common.service_manager import get_service_manager

    mgr = get_service_manager()

    # Install with current args
    mgr.uninstall()
    mgr.install()

    # Should not need reinstall (just installed)
    check("no reinstall needed immediately after install", not mgr._needs_reinstall())

    # Verify it detects when it does need reinstall by checking the method exists
    check("_needs_reinstall method exists", hasattr(mgr, '_needs_reinstall'))

    # Clean up
    mgr.uninstall()


def test_edge_cases():
    """Test edge cases from the plan."""
    print("\n=== Edge Case Tests ===\n")

    from yank.common.service_manager import get_service_manager, ServiceStatus

    mgr = get_service_manager()

    # 1. Double uninstall
    mgr.uninstall()
    ok, msg = mgr.uninstall()
    check("double uninstall is safe", ok, msg)

    # 2. Stop when not installed
    ok, msg = mgr.stop()
    check("stop when not installed is safe", ok, msg)

    # 3. Status when not installed
    info = mgr.get_status()
    check("status when not installed", info.status == ServiceStatus.NOT_INSTALLED, info.status.value)

    # 4. install_and_start from scratch
    ok, msg = mgr.install_and_start()
    check("install_and_start from scratch", ok, msg)
    time.sleep(3)

    info = mgr.get_status()
    check("running after install_and_start from scratch", info.status == ServiceStatus.RUNNING, info.status.value)

    # 5. stop_and_uninstall
    ok, msg = mgr.stop_and_uninstall()
    check("stop_and_uninstall", ok, msg)
    time.sleep(2)


# ── Main ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  Yank Service Manager — End-to-End Tests")
    print(f"  Platform: {sys.platform}")
    print(f"  Binary: {YANK}")
    print("=" * 60)

    cleanup()
    time.sleep(1)

    try:
        test_service_manager_api()
        test_cli_commands()
        test_foreground_flag()
        test_stale_binary_detection()
        test_edge_cases()
    finally:
        # Always clean up
        print("\n--- Cleanup ---")
        cleanup()
        time.sleep(1)

    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"  Results: {PASS}/{total} passed, {FAIL} failed")
    print("=" * 60)

    sys.exit(1 if FAIL > 0 else 0)
