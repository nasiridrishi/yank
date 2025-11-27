"""
Windows Service Wrapper

Allows clipboard-sync to run as a Windows Service using pywin32.
Can also be installed via NSSM for simpler setup.
"""
import sys
import os
import logging
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    HAS_WIN32_SERVICE = True
except ImportError:
    HAS_WIN32_SERVICE = False
    print("pywin32 service support not available")

import config
from agent import SyncAgent
from windows.clipboard import WindowsClipboardMonitor

logger = logging.getLogger(__name__)


class ClipboardSyncService:
    """
    Windows Service class for Clipboard Sync
    """
    
    _svc_name_ = "ClipboardSync"
    _svc_display_name_ = "LAN Clipboard File Sync"
    _svc_description_ = "Syncs clipboard files between Windows and Mac over LAN"
    
    def __init__(self):
        self.agent = None
        self.clipboard_monitor = None
        self.running = False
    
    def start(self):
        """Start the service"""
        logger.info("Starting Clipboard Sync Service...")
        
        # Setup logging to file
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[logging.FileHandler(config.LOG_FILE)]
        )
        
        # Create components
        self.agent = SyncAgent(
            on_files_received=self._on_files_received,
            port=config.PORT
        )
        
        self.clipboard_monitor = WindowsClipboardMonitor(
            on_files_copied=self._on_files_copied,
            poll_interval=config.POLL_INTERVAL
        )
        
        # Start
        self.agent.start()
        self.clipboard_monitor.start()
        self.running = True
        
        logger.info("Clipboard Sync Service started")
    
    def stop(self):
        """Stop the service"""
        logger.info("Stopping Clipboard Sync Service...")
        self.running = False
        
        if self.clipboard_monitor:
            self.clipboard_monitor.stop()
        if self.agent:
            self.agent.stop()
        
        logger.info("Clipboard Sync Service stopped")
    
    def _on_files_copied(self, file_paths):
        """Handle files copied"""
        self.agent.send_files(file_paths)
    
    def _on_files_received(self, file_paths):
        """Handle files received"""
        self.clipboard_monitor.set_clipboard_files(file_paths)


if HAS_WIN32_SERVICE:
    class WindowsService(win32serviceutil.ServiceFramework):
        """
        pywin32 service framework wrapper
        """
        _svc_name_ = ClipboardSyncService._svc_name_
        _svc_display_name_ = ClipboardSyncService._svc_display_name_
        _svc_description_ = ClipboardSyncService._svc_description_
        
        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.service = ClipboardSyncService()
        
        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self.stop_event)
            self.service.stop()
        
        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, '')
            )
            self.service.start()
            
            # Wait for stop signal
            win32event.WaitForSingleObject(self.stop_event, win32event.INFINITE)


def install_service():
    """Install the Windows service"""
    if not HAS_WIN32_SERVICE:
        print("pywin32 service support not available")
        return
    
    print("Installing Clipboard Sync Service...")
    sys.argv = ['', 'install']
    win32serviceutil.HandleCommandLine(WindowsService)


def uninstall_service():
    """Uninstall the Windows service"""
    if not HAS_WIN32_SERVICE:
        print("pywin32 service support not available")
        return
    
    print("Uninstalling Clipboard Sync Service...")
    sys.argv = ['', 'remove']
    win32serviceutil.HandleCommandLine(WindowsService)


def print_nssm_instructions():
    """Print instructions for NSSM installation (simpler alternative)"""
    print("""
=== NSSM Installation (Recommended) ===

NSSM (Non-Sucking Service Manager) provides an easier way to run as a service.

1. Download NSSM from: https://nssm.cc/download

2. Extract and run as Admin:
   nssm install ClipboardSync

3. In the GUI, set:
   Path: C:\\Path\\To\\python.exe
   Startup directory: C:\\Path\\To\\clipboard-sync
   Arguments: -m windows.main

4. Click "Install service"

5. Start the service:
   nssm start ClipboardSync

To remove:
   nssm remove ClipboardSync confirm
""")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        if sys.argv[1] == 'install':
            install_service()
        elif sys.argv[1] == 'uninstall':
            uninstall_service()
        elif sys.argv[1] == 'nssm':
            print_nssm_instructions()
        else:
            print("Usage: python service.py [install|uninstall|nssm]")
    else:
        # Run directly (for testing)
        if HAS_WIN32_SERVICE:
            win32serviceutil.HandleCommandLine(WindowsService)
        else:
            print("Run 'python -m windows.main' instead")
