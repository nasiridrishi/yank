"""
Clipboard Sync - Cross-Platform Entry Point

Automatically detects the operating system and uses the appropriate
clipboard implementation.

Commands:
    python -m main              Start the sync agent
    python -m main pair         Start pairing mode (display PIN)
    python -m main join IP PIN  Join/pair with another device
    python -m main unpair       Remove pairing
    python -m main status       Show pairing status
"""
import sys
import os
import platform
import logging
import signal
import argparse
from pathlib import Path

import config
from agent import SyncAgent
from common.pairing import (
    PairingServer, PairingClient,
    get_pairing_manager, get_device_name
)

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
    """

    def __init__(self, peer_ip: str = None, port: int = config.PORT, require_pairing: bool = True):
        self.peer_ip = peer_ip
        self.port = port
        self.require_pairing = require_pairing

        self.agent: SyncAgent = None
        self.clipboard_monitor: ClipboardMonitor = None
        self._running = False

    def start(self):
        """Start the clipboard sync"""
        logger.info(f"Starting {PLATFORM_NAME} Clipboard Sync...")

        # Create sync agent
        self.agent = SyncAgent(
            on_files_received=self._on_files_received,
            port=self.port,
            require_pairing=self.require_pairing
        )

        # Set peer if provided
        if self.peer_ip:
            self.agent.set_peer(self.peer_ip, self.port)

        # Create clipboard monitor (platform-specific implementation)
        self.clipboard_monitor = ClipboardMonitor(
            on_files_copied=self._on_files_copied,
            poll_interval=config.POLL_INTERVAL
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
        print(f"  LAN Clipboard File Sync - {PLATFORM_NAME}")
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
        print("="*50)
        print(f"\nCopy files ({copy_shortcut}) to sync them to your {other_platform}.")
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
        logger.info(f"Sending {len(file_paths)} file(s) to peer...")

        # Calculate total size
        total_size = sum(
            f.stat().st_size if f.is_file() else 0
            for f in file_paths
        )

        if total_size > config.MAX_TOTAL_SIZE:
            logger.warning(f"Total size ({total_size / 1024 / 1024:.1f}MB) exceeds limit")
            print(f"⚠ Files too large ({total_size / 1024 / 1024:.1f}MB). Max: {config.MAX_TOTAL_SIZE / 1024 / 1024:.0f}MB")
            return

        # Send files
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        success = self.agent.send_files(file_paths)

        if success:
            print(f"✓ Sent {len(file_paths)} file(s) to {other_platform} ({total_size / 1024:.1f}KB)")
        else:
            print(f"✗ Failed to send files. Check if {other_platform} is running.")

    def _on_files_received(self, file_paths: list):
        """Called when files are received from peer"""
        logger.info(f"Received {len(file_paths)} file(s) from peer")

        # Set in clipboard
        self.clipboard_monitor.set_clipboard_files(file_paths)

        # Notify user
        other_platform = "Mac" if PLATFORM == "Windows" else "Windows"
        paste_shortcut = "Ctrl+V" if PLATFORM == "Windows" else "Cmd+V"

        filenames = [p.name for p in file_paths[:3]]
        if len(file_paths) > 3:
            filenames.append(f"... +{len(file_paths) - 3} more")

        print(f"✓ Received from {other_platform}: {', '.join(filenames)}")
        print(f"  Ready to paste ({paste_shortcut})")

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


def cmd_start(args):
    """Start the clipboard sync agent"""
    manager = get_pairing_manager()

    if not manager.is_paired():
        print("\n[WARNING] No device paired!")
        print(f"Run '{RUN_SCRIPT} pair' first to pair with another device.")
        print("Or use '--no-security' to run without pairing (not recommended).\n")

        if not args.no_security:
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
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    app.start()
    app.run_forever()


def main():
    parser = argparse.ArgumentParser(
        description=f'LAN Clipboard File Sync - {PLATFORM_NAME}',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""
Commands:
  start       Start the clipboard sync agent (default)
  pair        Enter pairing mode - displays PIN for other device
  join        Pair with another device using IP and PIN
  unpair      Remove current pairing
  status      Show pairing status

Examples:
  python -m main                          Start syncing
  python -m main pair                     Show PIN for pairing
  python -m main join 192.168.1.5 123456  Pair with device
  python -m main status                   Check pairing status
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

    # Unpair command
    subparsers.add_parser('unpair', help='Remove current pairing')

    # Status command
    subparsers.add_parser('status', help='Show pairing status')

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
    elif args.command == 'pair':
        cmd_pair(args)
    elif args.command == 'join':
        cmd_join(args)
    elif args.command == 'unpair':
        cmd_unpair(args)
    elif args.command == 'status':
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
