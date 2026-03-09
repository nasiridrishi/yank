# PyInstaller runtime hook
# Runs before any application code when the frozen Windows exe starts.
# Since yank.spec builds with console=False (GUI subsystem), Windows never
# creates a console automatically. This hook attaches to the parent process's
# existing console (cmd.exe / PowerShell) so interactive output still works.
# When launched by Task Scheduler (no parent console), AttachConsole(-1) returns
# False and the process runs silently — which is exactly what we want.
import sys

if sys.platform == 'win32':
    import ctypes
    if ctypes.windll.kernel32.AttachConsole(-1):
        # Reopen standard streams so print() and logging output reach the console
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace')
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace')
        sys.stdin  = open('CONIN$',  'r', encoding='utf-8', errors='replace')
