"""
Singleton lock to ensure only one instance of clipboard-sync runs at a time.

Uses a combination of:
- PID file to track the running process
- File locking to prevent race conditions
- Port binding check as backup validation
"""
import os
import sys
import socket
import logging
import subprocess
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class SingletonLock:
    """
    Ensures only one instance of the application runs at a time.

    Usage:
        lock = SingletonLock()
        if not lock.acquire():
            print("Another instance is already running")
            sys.exit(1)

        # ... run application ...

        lock.release()  # Called automatically on normal exit
    """

    def __init__(self, app_name: str = "clipboard-sync", port: int = 9876):
        self.app_name = app_name
        self.port = port
        self._lock_file: Optional[Path] = None
        self._pid_file: Optional[Path] = None
        self._lock_fd = None
        self._acquired = False

        # Determine lock file location
        if os.name == 'nt':  # Windows
            lock_dir = Path(os.environ.get('TEMP', Path.home()))
        else:  # macOS/Linux
            lock_dir = Path('/tmp')

        self._lock_file = lock_dir / f"{app_name}.lock"
        self._pid_file = lock_dir / f"{app_name}.pid"

    def acquire(self) -> bool:
        """
        Try to acquire the singleton lock.

        Returns:
            True if lock acquired (no other instance running)
            False if another instance is already running
        """
        # First check if port is already in use (quick check)
        if self._is_port_in_use():
            existing_pid = self._read_pid_file()
            if existing_pid and self._is_process_running(existing_pid):
                logger.warning(f"Another instance (PID {existing_pid}) is already running on port {self.port}")
                return False
            else:
                logger.warning(f"Port {self.port} is in use but PID file is stale. Another application may be using this port.")
                return False

        # Try to acquire file lock
        try:
            if os.name == 'nt':
                # Windows: Use msvcrt for file locking
                import msvcrt
                self._lock_fd = open(self._lock_file, 'w')
                try:
                    msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
                except IOError:
                    self._lock_fd.close()
                    self._lock_fd = None
                    existing_pid = self._read_pid_file()
                    logger.warning(f"Another instance (PID {existing_pid}) is already running (lock held)")
                    return False
            else:
                # Unix: Use fcntl for file locking
                import fcntl
                self._lock_fd = open(self._lock_file, 'w')
                try:
                    fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except IOError:
                    self._lock_fd.close()
                    self._lock_fd = None
                    existing_pid = self._read_pid_file()
                    logger.warning(f"Another instance (PID {existing_pid}) is already running (lock held)")
                    return False

            # Write our PID to the lock file
            pid_str = str(os.getpid())
            self._lock_fd.write(pid_str)
            self._lock_fd.flush()

            # Also write to a separate .pid file (not locked, always readable)
            try:
                self._pid_file.write_text(pid_str)
            except Exception as e:
                logger.debug(f"Could not write .pid file: {e}")

            self._acquired = True
            logger.debug(f"Acquired singleton lock (PID {os.getpid()})")
            return True

        except Exception as e:
            logger.error(f"Error acquiring singleton lock: {e}")
            if self._lock_fd:
                self._lock_fd.close()
                self._lock_fd = None
            return False

    def release(self):
        """Release the singleton lock."""
        if not self._acquired:
            return

        try:
            if self._lock_fd:
                if os.name == 'nt':
                    import msvcrt
                    try:
                        msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_UNLCK, 1)
                    except:
                        pass
                else:
                    import fcntl
                    try:
                        fcntl.flock(self._lock_fd.fileno(), fcntl.LOCK_UN)
                    except:
                        pass

                self._lock_fd.close()
                self._lock_fd = None

            # Remove lock file
            if self._lock_file and self._lock_file.exists():
                try:
                    self._lock_file.unlink()
                except:
                    pass

            # Remove pid file
            if self._pid_file and self._pid_file.exists():
                try:
                    self._pid_file.unlink()
                except:
                    pass

            self._acquired = False
            logger.debug("Released singleton lock")

        except Exception as e:
            logger.error(f"Error releasing singleton lock: {e}")

    def _is_port_in_use(self) -> bool:
        """Check if the application port is already in use."""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(('127.0.0.1', self.port))
            sock.close()
            return result == 0
        except:
            return False

    def _read_pid_file(self) -> Optional[int]:
        """Read PID from .pid file first, then fall back to .lock file."""
        # Try the separate .pid file first (always readable, even on Windows)
        try:
            if self._pid_file and self._pid_file.exists():
                content = self._pid_file.read_text().strip()
                if content:
                    return int(content)
        except:
            pass

        # Fall back to the .lock file (may fail on Windows if locked)
        try:
            if self._lock_file and self._lock_file.exists():
                content = self._lock_file.read_text().strip()
                if content:
                    return int(content)
        except:
            pass
        return None

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with given PID is running."""
        try:
            if os.name == 'nt':
                # Windows
                import ctypes
                kernel32 = ctypes.windll.kernel32
                PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                # Unix - send signal 0 to check if process exists
                os.kill(pid, 0)
                return True
        except (OSError, ProcessLookupError):
            return False
        except Exception:
            return False

    def _find_pid_by_port(self) -> Optional[int]:
        """Find the PID of the process listening on our port.

        Returns the PID if found, or None.
        """
        try:
            if os.name == 'nt':
                # Windows: parse netstat output
                output = subprocess.check_output(
                    ['netstat', '-a', '-n', '-o'],
                    text=True, timeout=5,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)
                )
                for line in output.splitlines():
                    parts = line.split()
                    if len(parts) >= 5 and 'LISTENING' in parts:
                        local_addr = parts[1]
                        if local_addr.endswith(f':{self.port}'):
                            pid = int(parts[-1])
                            if pid > 0:
                                return pid
            else:
                # Unix: use lsof
                output = subprocess.check_output(
                    ['lsof', '-ti', f':{self.port}'],
                    text=True, timeout=5,
                    stderr=subprocess.DEVNULL
                )
                for line in output.strip().splitlines():
                    line = line.strip()
                    if line.isdigit():
                        return int(line)
        except (subprocess.SubprocessError, FileNotFoundError, ValueError):
            pass
        return None

    def get_existing_pid(self) -> Optional[int]:
        """Get PID of existing running instance, if any.

        Returns:
            int > 0: PID of the running instance
            -1: Instance is running (port in use) but PID is unknown
            None: No instance running
        """
        pid = self._read_pid_file()
        if pid and self._is_process_running(pid):
            return pid

        # PID file unreadable or stale — check if the port is in use
        if self._is_port_in_use():
            found = self._find_pid_by_port()
            if found:
                return found
            return -1  # running but PID unknown

        return None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("Another instance is already running")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()
        return False


# Global singleton instance
_singleton_lock: Optional[SingletonLock] = None


def ensure_single_instance(app_name: str = "clipboard-sync", port: int = 9876) -> bool:
    """
    Ensure only one instance of the application is running.

    Call this at the start of your application.

    Returns:
        True if this is the only instance
        False if another instance is already running
    """
    global _singleton_lock

    if _singleton_lock is not None:
        # Already acquired
        return True

    _singleton_lock = SingletonLock(app_name, port)
    return _singleton_lock.acquire()


def release_singleton():
    """Release the singleton lock. Call this on application shutdown."""
    global _singleton_lock

    if _singleton_lock:
        _singleton_lock.release()
        _singleton_lock = None


def get_existing_instance_pid() -> Optional[int]:
    """Get PID of existing running instance, if any."""
    lock = SingletonLock()
    return lock.get_existing_pid()
