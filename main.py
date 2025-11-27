"""
Clipboard Sync - Cross-Platform Entry Point

Automatically detects the operating system and uses the appropriate
clipboard implementation.

Features:
- Instant file announcements (metadata only - works even for 100GB!)
- Chunked streaming for large files (memory efficient)
- Text clipboard sync
- Automatic peer discovery

Commands:
    python -m main              Start the sync agent
    python -m main stop         Stop running instance
    python -m main pair         Start pairing mode (display PIN)
    python -m main join IP PIN  Join/pair with another device
    python -m main unpair       Remove pairing
    python -m main status       Show pairing status
    python -m main config       Show/edit configuration
"""
import sys
import os
import platform
import logging
import signal
import argparse
import threading
from pathlib import Path

import config
from agent import SyncAgent
from common.protocol import TransferMetadata
from common.pairing import (
    PairingServer, PairingClient,
    get_pairing_manager, get_device_name
)
from common.singleton import ensure_single_instance, release_singleton, get_existing_instance_pid
from common.user_config import get_config, get_config_manager, print_config, format_size
from common.syncignore import get_syncignore, filter_files
from common.chunked_transfer import format_bytes

# Detect OS and import appropriate clipboard module
PLATFORM = platform.system()

if PLATFORM == 'Windows':
    from windows.clipboard import WindowsClipboardMonitor as ClipboardMonitor
    PLATFORM_NAME = "Windows"
    RUN_SCRIPT = "run.ps1"
elif PLATFORM == 'Darwin':
    from macos.clipboard import MacClipboardMonitor as ClipboardMonitor
    PLATFORM_NAME = "macOS"
    RUN_SCRIPT = "run.sh"
else:
    print(f"Unsupported platform: {PLATFORM}")
    print("This application only supports Windows and macOS.")
    sys.exit(1)

# Setup logging
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL),
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.LOG_FILE)
    ]
)
logger = logging.getLogger(__name__)


class ClipboardSync:
    """
    Main application class - cross-platform

    Features:
    - Lazy file transfer: Instant copy (metadata only), download on paste
    - Chunked streaming for large files
    - Automatic progress tracking
    """

    # Files larger than this use lazy transfer (10MB default)
    LAZY_TRANSFER_THRESHOLD = 10 * 1024 * 1024

    def __init__(self, peer_ip: str = None, port: int = config.PORT, require_pairing: bool = True):
        self.peer_ip = peer_ip
        self.port = port
        self.require_pairing = require_pairing

        # Load user config
        self.user_config = get_config()

        self.agent: SyncAgent = None
        self.clipboard_monitor: ClipboardMonitor = None
        self._running = False

        # Track pending transfers
        self._pending_transfer_id: str = None
        self._last_progress_update: float = 0

    def start(self):
        """Start the clipboard sync"""
        logger.info(f"Starting {PLATFORM_NAME} Clipboard Sync...")

        # Create sync agent with all callbacks
        self.agent = SyncAgent(
            on_files_received=self._on_files_received,
            on_text_received=self._on_text_received,
            on_files_announced=self._on_files_announced,
            on_transfer_progress=self._on_transfer_progress,
            port=self.port,
            require_pairing=self.require_pairing
        )

        # Set peer if provided
        if self.peer_ip:
            self.agent.set_peer(self.peer_ip, self.port)

        # Create clipboard monitor (platform-specific implementation)
        self.clipboard_monitor = ClipboardMonitor(
            on_files_copied=self._on_files_copied,
            on_text_copied=self._on_text_copied,
            poll_interval=config.POLL_INTERVAL,
            sync_text=self.user_config.sync_text,
            sync_files=self.user_config.sync_files,
            sync_images=self.user_config.sync_images
        )

        # Start components
        self.agent.start()
        self.clipboard_monitor.start()

        self._running = True
        logger.info(f"{PLATFORM_NAME} Clipboard Sync started successfully")

        # Print status
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows PC"
        copy_shortcut = "Ctrl+C" if PLATFORM == "Windows" else "Cmd+C"
        paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"

        print("\n" + "="*50)
        print(f"  LAN Clipboard Sync - {PLATFORM_NAME}")
        print("="*50)
        print(f"  Listening on port: {self.port}")
        if self.peer_ip:
            print(f"  Peer: {self.peer_ip}:{self.port}")
        else:
            print("  Peer: Auto-discovery enabled")
        if self.require_pairing:
            manager = get_pairing_manager()
            if manager.is_paired():
                print(f"  Security: ENCRYPTED (paired with {manager.get_paired_device().device_name})")
            else:
                print("  Security: ENABLED (no device paired)")
        else:
            print("  Security: DISABLED")

        # Show sync settings
        print("\n  Sync Settings:")
        print(f"    Files:  {'ON' if self.user_config.sync_files else 'OFF'}")
        print(f"    Text:   {'ON' if self.user_config.sync_text else 'OFF'}")
        print(f"    Images: {'ON' if self.user_config.sync_images else 'OFF'}")

        print("="*50)
        print(f"\nCopy files or text ({copy_shortcut}) to sync to your {other_platform}.")
        print("Press Ctrl+C to stop.\n")

    def stop(self):
        """Stop the clipboard sync"""
        logger.info(f"Stopping {PLATFORM_NAME} Clipboard Sync...")
        self._running = False

        if self.clipboard_monitor:
            self.clipboard_monitor.stop()

        if self.agent:
            self.agent.stop()

        logger.info(f"{PLATFORM_NAME} Clipboard Sync stopped")

    def _on_files_copied(self, file_paths: list):
        """Called when files are copied to clipboard"""
        if not self.user_config.sync_files:
            return

        # Filter files based on .syncignore
        original_count = len(file_paths)
        file_paths = filter_files(file_paths)

        if len(file_paths) < original_count:
            ignored = original_count - len(file_paths)
            logger.info(f"Filtered {ignored} file(s) based on .syncignore")

        if not file_paths:
            return

        # Filter by extension from config
        filtered_paths = []
        for p in file_paths:
            if p.suffix.lower() not in self.user_config.ignored_extensions:
                filtered_paths.append(p)
        file_paths = filtered_paths

        if not file_paths:
            return

        # Calculate total size
        total_size = sum(
            f.stat().st_size if f.is_file() else 0
            for f in file_paths
        )

        # Check against config limits
        max_total = self.user_config.max_total_size

        if total_size > max_total:
            logger.warning(f"Total size ({total_size / 1024 / 1024:.1f}MB) exceeds limit")
            print(f"! Files too large ({format_size(total_size)}). Max: {format_size(max_total)}")
            return

        # Check individual file sizes
        for f in file_paths:
            if f.is_file() and f.stat().st_size > self.user_config.max_file_size:
                logger.warning(f"File {f.name} exceeds max file size")
                print(f"! File '{f.name}' too large. Max: {format_size(self.user_config.max_file_size)}")
                return

        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"

        # Use lazy transfer for large files, direct transfer for small files
        if total_size > self.LAZY_TRANSFER_THRESHOLD:
            # Lazy transfer: announce only (instant!)
            logger.info(f"Announcing {len(file_paths)} file(s) ({format_bytes(total_size)})...")
            transfer_id = self.agent.announce_files(file_paths)

            if transfer_id:
                print(f">> Announced {len(file_paths)} file(s) to {other_platform} ({format_bytes(total_size)})")
                print(f"   Files ready for download on {other_platform}")
            else:
                print(f"X Failed to announce files. Check if {other_platform} is running.")
        else:
            # Direct transfer for small files (legacy behavior)
            logger.info(f"Sending {len(file_paths)} file(s) to peer...")
            success = self.agent.send_files(file_paths)

            if success:
                print(f">> Sent {len(file_paths)} file(s) to {other_platform} ({format_size(total_size)})")
            else:
                print(f"X Failed to send files. Check if {other_platform} is running.")

    def _on_files_received(self, file_paths: list):
        """Called when files are received from peer (small files via direct transfer)"""
        logger.info(f"Received {len(file_paths)} file(s) from peer")

        # Set in clipboard
        self.clipboard_monitor.set_clipboard_files(file_paths)

        # Notify user
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"

        filenames = [p.name for p in file_paths[:3]]
        if len(file_paths) > 3:
            filenames.append(f"... +{len(file_paths) - 3} more")

        print(f">> Received from {other_platform}: {', '.join(filenames)}")
        print(f"   Ready to paste ({paste_shortcut})")

    def _on_text_copied(self, text: str):
        """Called when text is copied to clipboard"""
        if not self.user_config.sync_text:
            return

        # Check minimum length
        if len(text) < self.user_config.min_text_length:
            logger.debug(f"Text too short ({len(text)} chars), ignoring")
            return

        # Check size limit
        text_size = len(text.encode('utf-8'))
        if text_size > self.user_config.max_text_size:
            logger.warning(f"Text too large ({format_size(text_size)}), ignoring")
            print(f"⚠ Text too large ({format_size(text_size)}). Max: {format_size(self.user_config.max_text_size)}")
            return

        logger.info(f"Sending text to peer ({len(text)} chars)...")

        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        success = self.agent.send_text(text)

        if success:
            preview = text[:50] + "..." if len(text) > 50 else text
            preview = preview.replace('\n', ' ').replace('\r', '')
            print(f">> Sent text to {other_platform} ({len(text)} chars): {preview}")
        else:
            print(f"X Failed to send text. Check if {other_platform} is running.")

    def _on_text_received(self, text: str):
        """Called when text is received from peer"""
        logger.info(f"Received text from peer ({len(text)} chars)")

        # Set in clipboard
        self.clipboard_monitor.set_clipboard_text(text)

        # Notify user
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"

        preview = text[:50] + "..." if len(text) > 50 else text
        preview = preview.replace('\n', ' ').replace('\r', '')

        print(f">> Received text from {other_platform} ({len(text)} chars): {preview}")
        print(f"   Ready to paste ({paste_shortcut})")

    def _on_files_announced(self, transfer_id: str, metadata: TransferMetadata):
        """
        Called when files are announced by peer (lazy transfer).

        On Windows: Uses virtual clipboard for true on-demand (download on paste via IDataObject)
        On macOS: Uses placeholder-based approach (downloads in background as placeholders)
        """
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"

        # Show what files are available
        print(f"\n<< Files announced from {other_platform}:")
        for f in metadata.files[:5]:
            print(f"   - {f.name} ({format_bytes(f.size)})")
        if len(metadata.files) > 5:
            print(f"   ... +{len(metadata.files) - 5} more")
        print(f"   Total: {format_bytes(metadata.total_size)}")

        # Store for potential later use
        self._pending_transfer_id = transfer_id

        # Try to use virtual clipboard (both Windows and macOS)
        use_virtual = self._try_set_virtual_clipboard(transfer_id, metadata)
        if use_virtual:
            if PLATFORM == "Windows":
                print(f"\n   Ready to paste ({paste_shortcut}) - download will start when you paste")
            else:
                print(f"\n   Ready to paste ({paste_shortcut}) - downloading in background...")
            return

        # Fall back to auto-download
        print(f"\n   Downloading files...")
        download_thread = threading.Thread(
            target=self._download_announced_files,
            args=(transfer_id, metadata),
            daemon=True
        )
        download_thread.start()

    def _try_set_virtual_clipboard(self, transfer_id: str, metadata: TransferMetadata) -> bool:
        """
        Try to set virtual files on clipboard.

        Windows: Uses IDataObject with CFSTR_FILEDESCRIPTOR/CFSTR_FILECONTENTS
                 for true on-demand download when paste occurs.
        macOS: Uses placeholder files that download in background since
               NSFilePromiseProvider doesn't work with Finder copy/paste.

        Returns True if successful, False to fall back to auto-download.
        """
        try:
            # Prepare file info for virtual clipboard
            files = [
                {
                    'name': f.name,
                    'size': f.size,
                    'checksum': f.checksum,
                    'file_index': f.file_index
                }
                for f in metadata.files
            ]

            # Create download callback that fetches file content on-demand
            def download_callback(tid: str, file_index: int):
                return self.agent.download_single_file(tid, file_index)

            # Try to set virtual clipboard
            success = self.clipboard_monitor.set_virtual_clipboard_files(
                files,
                transfer_id,
                download_callback
            )

            return success

        except Exception as e:
            logger.warning(f"Virtual clipboard failed, using auto-download: {e}")
            return False

    def _download_announced_files(self, transfer_id: str, metadata: TransferMetadata):
        """Download announced files in background thread"""
        try:
            # Request the transfer
            downloaded_files = self.agent.request_transfer(transfer_id)

            if downloaded_files:
                # Set in clipboard
                self.clipboard_monitor.set_clipboard_files(downloaded_files)

                paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"
                print(f"\n>> Downloaded {len(downloaded_files)} file(s)")
                print(f"   Ready to paste ({paste_shortcut})")
            else:
                print(f"\nX Download failed. Files may have expired or peer went offline.")

        except Exception as e:
            logger.error(f"Download error: {e}")
            print(f"\nX Download error: {e}")

    def _on_transfer_progress(self, transfer_id: str, bytes_done: int, bytes_total: int, current_file: str):
        """Called during file transfer to show progress"""
        import time

        # Rate limit progress updates to avoid flooding console
        now = time.time()
        if now - self._last_progress_update < 0.5:
            return
        self._last_progress_update = now

        percent = (bytes_done / bytes_total * 100) if bytes_total > 0 else 0
        done_str = format_bytes(bytes_done)
        total_str = format_bytes(bytes_total)

        # Progress bar
        bar_width = 20
        filled = int(bar_width * percent / 100)
        bar = '#' * filled + '-' * (bar_width - filled)

        # Print on same line (carriage return)
        file_display = current_file[:30] + "..." if len(current_file) > 33 else current_file
        sys.stdout.write(f"\r   [{bar}] {percent:.1f}% ({done_str}/{total_str}) {file_display}")
        sys.stdout.flush()

        if bytes_done >= bytes_total:
            print()  # New line when complete

    def run_forever(self):
        """Run until interrupted"""
        try:
            while self._running:
                import time
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()


def cmd_pair(args):
    """Start pairing mode - display PIN and wait for connection"""
    print(f"\nDevice: {get_device_name()}")

    server = PairingServer(port=9877)

    def signal_handler(sig, frame):
        print("\nCancelled.")
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    success, message = server.start_pairing(timeout=args.timeout)

    if success:
        print(f"\n[OK] {message}")
        print(f"\nYou can now run '{RUN_SCRIPT} start' to begin syncing.")
    else:
        print(f"\n[FAILED] {message}")
        sys.exit(1)


def cmd_join(args):
    """Join/pair with another device using its IP and PIN"""
    if not args.host:
        print("[ERROR] Host IP is required. Usage: join <IP> <PIN>")
        sys.exit(1)

    if not args.pin:
        print("[ERROR] PIN is required. Usage: join <IP> <PIN>")
        sys.exit(1)

    print(f"\nConnecting to {args.host}...")

    client = PairingClient(args.host, port=9877)
    success, message = client.pair_with_pin(args.pin)

    if success:
        print(f"\n[OK] {message}")
        print(f"\nYou can now run '{RUN_SCRIPT} start' to begin syncing.")
    else:
        print(f"\n[FAILED] {message}")
        sys.exit(1)


def cmd_unpair(args):
    """Remove the current pairing"""
    manager = get_pairing_manager()

    if not manager.is_paired():
        print("\nNo device is currently paired.")
        return

    paired = manager.get_paired_device()
    print(f"\nCurrently paired with: {paired.device_name}")
    print(f"Paired at: {paired.paired_at}")

    confirm = input("\nAre you sure you want to unpair? (y/N): ")
    if confirm.lower() == 'y':
        manager.clear_pairing()
        print("\n[OK] Pairing removed.")
    else:
        print("\nCancelled.")


def cmd_status(args):
    """Show pairing status"""
    manager = get_pairing_manager()

    print(f"\n{'='*50}")
    print("  Clipboard Sync - Status")
    print(f"{'='*50}")
    print(f"\n  This device: {get_device_name()}")

    if manager.is_paired():
        paired = manager.get_paired_device()
        print(f"\n  Pairing Status: PAIRED")
        print(f"  Paired Device:  {paired.device_name}")
        print(f"  Device ID:      {paired.device_id}")
        print(f"  Paired At:      {paired.paired_at}")
        print(f"  Last Seen:      {paired.last_seen or 'Never'}")
        print(f"\n  Encryption: ENABLED (AES-256-GCM)")
    else:
        print(f"\n  Pairing Status: NOT PAIRED")
        print(f"\n  To pair with another device:")
        print(f"    1. Run '{RUN_SCRIPT} pair' on this device")
        print(f"    2. Run '{RUN_SCRIPT} join <IP> <PIN>' on the other device")

    print(f"\n{'='*50}\n")


def cmd_config(args):
    """Show or modify configuration"""
    config_mgr = get_config_manager()
    user_cfg = config_mgr.get()

    if args.show:
        print_config()
        return

    if args.reset:
        config_mgr.reset()
        print("[OK] Configuration reset to defaults.")
        print_config()
        return

    # Handle setting a value
    if args.set:
        key, value = args.set
        # Convert value to appropriate type
        if value.lower() in ('true', 'on', 'yes', '1'):
            value = True
        elif value.lower() in ('false', 'off', 'no', '0'):
            value = False
        elif value.isdigit():
            value = int(value)
        elif value.replace('.', '').isdigit():
            value = float(value)

        if config_mgr.set(key, value):
            print(f"[OK] Set {key} = {value}")
        else:
            print(f"[ERROR] Unknown config key: {key}")
            print("\nAvailable keys:")
            for k in vars(user_cfg):
                if not k.startswith('_'):
                    print(f"  - {k}")
        return

    # Default: show config
    print_config()

    # Show syncignore info
    syncignore = get_syncignore()
    patterns = syncignore.get_patterns()
    print(f"\n  .syncignore: {len(patterns)} patterns loaded")
    print(f"  Edit .syncignore to exclude file types from syncing.\n")


def cmd_stop(args):
    """Stop the running clipboard sync instance"""
    pid = get_existing_instance_pid()

    if not pid:
        print("\nNo running instance found.")
        return

    print(f"\nStopping clipboard-sync (PID {pid})...")

    try:
        if os.name == 'nt':
            # Windows - send SIGTERM equivalent
            import ctypes
            PROCESS_TERMINATE = 0x0001
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid)
            if handle:
                kernel32.TerminateProcess(handle, 0)
                kernel32.CloseHandle(handle)
                print(f"[OK] Stopped process {pid}")
            else:
                print(f"[ERROR] Could not stop process {pid} (access denied)")
        else:
            # Unix - send SIGTERM
            os.kill(pid, signal.SIGTERM)
            print(f"[OK] Sent stop signal to process {pid}")
    except ProcessLookupError:
        print(f"[WARN] Process {pid} not found (may have already exited)")
    except PermissionError:
        print(f"[ERROR] Permission denied. Try running with sudo/admin.")
    except Exception as e:
        print(f"[ERROR] Failed to stop process: {e}")


def cmd_start(args):
    """Start the clipboard sync agent"""
    # Check for existing instance
    if not ensure_single_instance("clipboard-sync", args.port):
        existing_pid = get_existing_instance_pid()
        print(f"\n[ERROR] Another instance is already running (PID {existing_pid})")
        print(f"Stop the existing instance first, or use '{RUN_SCRIPT} stop'")
        sys.exit(1)

    manager = get_pairing_manager()

    if not manager.is_paired():
        print("\n[WARNING] No device paired!")
        print(f"Run '{RUN_SCRIPT} pair' first to pair with another device.")
        print("Or use '--no-security' to run without pairing (not recommended).\n")

        if not args.no_security:
            release_singleton()
            sys.exit(1)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    app = ClipboardSync(
        peer_ip=args.peer,
        port=args.port,
        require_pairing=not args.no_security
    )

    # Handle signals
    def signal_handler(sig, frame):
        print("\nShutting down...")
        app.stop()
        release_singleton()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        app.start()
        app.run_forever()
    finally:
        release_singleton()


def main():
    parser = argparse.ArgumentParser(
        description=f'LAN Clipboard Sync - {PLATFORM_NAME}',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Commands:
  start       Start the clipboard sync agent (default)
  stop        Stop the running instance
  pair        Enter pairing mode - displays PIN for other device
  join        Pair with another device using IP and PIN
  unpair      Remove current pairing
  status      Show pairing status
  config      Show/edit configuration

Examples:
  python -m main                          Start syncing
  python -m main pair                     Show PIN for pairing
  python -m main join 192.168.1.5 123456  Pair with device
  python -m main status                   Check pairing status
  python -m main config                   View configuration
  python -m main config --set sync_text false  Disable text sync
"""
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # Start command (default)
    start_parser = subparsers.add_parser('start', help='Start clipboard sync')
    start_parser.add_argument('-p', '--peer', type=str, help='Peer IP address')
    start_parser.add_argument('--port', type=int, default=config.PORT, help=f'Port (default: {config.PORT})')
    start_parser.add_argument('-v', '--verbose', action='store_true', help='Verbose logging')
    start_parser.add_argument('--no-security', action='store_true', help='Disable pairing requirement (not recommended)')

    # Pair command
    pair_parser = subparsers.add_parser('pair', help='Enter pairing mode')
    pair_parser.add_argument('--timeout', type=int, default=120, help='Pairing timeout in seconds (default: 120)')

    # Join command
    join_parser = subparsers.add_parser('join', help='Join/pair with another device')
    join_parser.add_argument('host', nargs='?', help='IP address of device to pair with')
    join_parser.add_argument('pin', nargs='?', help='PIN displayed on other device')

    # Stop command
    subparsers.add_parser('stop', help='Stop running instance')

    # Unpair command
    subparsers.add_parser('unpair', help='Remove current pairing')

    # Status command
    subparsers.add_parser('status', help='Show pairing status')

    # Config command
    config_parser = subparsers.add_parser('config', help='Show/edit configuration')
    config_parser.add_argument('--show', action='store_true', help='Show current configuration')
    config_parser.add_argument('--reset', action='store_true', help='Reset to default configuration')
    config_parser.add_argument('--set', nargs=2, metavar=('KEY', 'VALUE'), help='Set a configuration value')

    args = parser.parse_args()

    # Default to start if no command
    if args.command is None:
        args.command = 'start'
        args.peer = None
        args.port = config.PORT
        args.verbose = False
        args.no_security = False

    # Route to command handler
    if args.command == 'start':
        cmd_start(args)
    elif args.command == 'stop':
        cmd_stop(args)
    elif args.command == 'pair':
        cmd_pair(args)
    elif args.command == 'join':
        cmd_join(args)
    elif args.command == 'unpair':
        cmd_unpair(args)
    elif args.command == 'status':
        cmd_status(args)
    elif args.command == 'config':
        cmd_config(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
