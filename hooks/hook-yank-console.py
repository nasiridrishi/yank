# Runtime hook: runs before any app code when the frozen Windows exe starts.
# yank.spec builds with console=False (GUI subsystem), so Windows never creates
# a console automatically and sys.stdout/stderr/stdin start as None.
#
# - Interactive use (launched from cmd.exe / PowerShell): attach to the parent's
#   existing console so print() and logging output are visible.
# - Background use (Task Scheduler ONLOGON, detached Popen child): no parent
#   console exists. AttachConsole(-1) returns False. We bind the standard
#   streams to os.devnull so any print()/sys.stdout.write() calls in the app
#   do not crash with AttributeError. File logging (FileHandler) still works.
import sys

if sys.platform == 'win32':
    import os
    import ctypes

    if ctypes.windll.kernel32.AttachConsole(-1):
        sys.stdout = open('CONOUT$', 'w', encoding='utf-8', errors='replace')
        sys.stderr = open('CONOUT$', 'w', encoding='utf-8', errors='replace')
        sys.stdin  = open('CONIN$',  'r', encoding='utf-8', errors='replace')
    else:
        # No parent console — bind to devnull so print()/stdout writes don't crash.
        devnull_out = open(os.devnull, 'w', encoding='utf-8', errors='replace')
        devnull_in  = open(os.devnull, 'r', encoding='utf-8', errors='replace')
        if sys.stdout is None:
            sys.stdout = devnull_out
        if sys.stderr is None:
            sys.stderr = devnull_out
        if sys.stdin is None:
            sys.stdin = devnull_in
