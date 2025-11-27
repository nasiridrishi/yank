"""
Local Simulation Test for Clipboard Sync

This simulates two agents on localhost to test the lazy file transfer system:
1. Agent A (sender) announces files
2. Agent B (receiver) receives announcement and downloads

Run with: python test_simulation.py
"""
import os
import sys
import time
import tempfile
import threading
import hashlib
import logging
from pathlib import Path

# Force UTF-8 output on Windows
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Enable debug logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

from agent import SyncAgent
from common.protocol import TransferMetadata
from common.chunked_transfer import format_bytes


class SimulationTest:
    """Simulates two agents communicating locally"""

    def __init__(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="clipboard_sync_test_"))
        # Use high ports to avoid conflicts with running instances
        self.sender_port = 19876
        self.receiver_port = 19877

        # Results
        self.announced_transfer_id = None
        self.announced_metadata = None
        self.received_files = []
        self.download_complete = threading.Event()
        self.announcement_received = threading.Event()

        # Agents
        self.sender_agent = None
        self.receiver_agent = None

    def setup(self):
        """Create test files and agents"""
        print("\n" + "=" * 60)
        print("  CLIPBOARD SYNC - LOCAL SIMULATION TEST")
        print("=" * 60)

        # Create test files of various sizes
        self.test_files = []

        # Small file
        small_file = self.test_dir / "small_test.txt"
        small_file.write_text("Hello from clipboard sync!" * 100)
        self.test_files.append(small_file)

        # Medium file (~500KB)
        medium_file = self.test_dir / "medium_test.bin"
        medium_file.write_bytes(os.urandom(500 * 1024))
        self.test_files.append(medium_file)

        # Larger file (~2MB) to test chunking
        large_file = self.test_dir / "large_test.bin"
        large_file.write_bytes(os.urandom(2 * 1024 * 1024))
        self.test_files.append(large_file)

        print(f"\n[Setup] Created test files in: {self.test_dir}")
        for f in self.test_files:
            print(f"  - {f.name}: {format_bytes(f.stat().st_size)}")

        # Create sender agent
        self.sender_agent = SyncAgent(
            on_files_received=lambda x: None,
            on_text_received=lambda x: None,
            port=self.sender_port,
            require_pairing=False  # Disable security for test
        )

        # Create receiver agent with callbacks
        self.receiver_agent = SyncAgent(
            on_files_received=self._on_files_received,
            on_text_received=lambda x: None,
            on_files_announced=self._on_files_announced,
            on_transfer_progress=self._on_progress,
            port=self.receiver_port,
            require_pairing=False
        )

        print(f"\n[Setup] Sender agent on port {self.sender_port}")
        print(f"[Setup] Receiver agent on port {self.receiver_port}")

    def _on_files_announced(self, transfer_id: str, metadata: TransferMetadata):
        """Called when receiver gets file announcement"""
        self.announced_transfer_id = transfer_id
        self.announced_metadata = metadata
        print(f"\n[Receiver] Files announced! Transfer ID: {transfer_id[:8]}...")
        print(f"[Receiver] Files available:")
        for f in metadata.files:
            print(f"  - {f.name}: {format_bytes(f.size)}")
        print(f"[Receiver] Total: {format_bytes(metadata.total_size)}")
        self.announcement_received.set()

    def _on_files_received(self, file_paths: list):
        """Called when files are received (direct transfer)"""
        self.received_files = file_paths
        print(f"\n[Receiver] Files received: {[p.name for p in file_paths]}")

    def _on_progress(self, transfer_id: str, bytes_done: int, bytes_total: int, current_file: str):
        """Show download progress"""
        percent = (bytes_done / bytes_total * 100) if bytes_total > 0 else 0
        bar_width = 30
        filled = int(bar_width * percent / 100)
        bar = '#' * filled + '-' * (bar_width - filled)
        sys.stdout.write(f"\r[Progress] [{bar}] {percent:.1f}% - {current_file[:20]}")
        sys.stdout.flush()
        if bytes_done >= bytes_total:
            print()

    def start_agents(self):
        """Start both agents"""
        print("\n[Test] Starting agents...")

        self.sender_agent.start()
        self.receiver_agent.start()

        # Point them at each other
        self.sender_agent.set_peer("127.0.0.1", self.receiver_port)
        self.receiver_agent.set_peer("127.0.0.1", self.sender_port)

        time.sleep(0.5)  # Let them initialize
        print("[Test] Agents started and connected")

    def stop_agents(self):
        """Stop both agents"""
        if self.sender_agent:
            self.sender_agent.stop()
        if self.receiver_agent:
            self.receiver_agent.stop()

    def test_lazy_transfer(self) -> bool:
        """Test the lazy file transfer flow"""
        print("\n" + "-" * 60)
        print("  TEST: Lazy File Transfer (Announce -> Request -> Download)")
        print("-" * 60)

        # Step 1: Sender announces files
        print("\n[Step 1] Sender announcing files...")
        transfer_id = self.sender_agent.announce_files(self.test_files)

        if not transfer_id:
            print("[FAIL] Failed to announce files")
            return False

        print(f"[Step 1] Announced with transfer ID: {transfer_id[:8]}...")

        # Step 2: Wait for receiver to get announcement
        print("\n[Step 2] Waiting for receiver to get announcement...")
        if not self.announcement_received.wait(timeout=5):
            print("[FAIL] Receiver didn't get announcement")
            return False

        print("[Step 2] Receiver got announcement!")

        # Allow time for connections to settle
        time.sleep(0.5)

        # Verify sender's registry has the transfer
        print(f"[Debug] Sender registry ID: {id(self.sender_agent._registry)}")
        print(f"[Debug] Receiver registry ID: {id(self.receiver_agent._registry)}")
        print(f"[Debug] Transfer ID announced by sender: {transfer_id[:8]}...")
        print(f"[Debug] Transfer ID received by receiver: {self.announced_transfer_id[:8]}...")

        sender_transfer = self.sender_agent._registry.get_transfer(transfer_id)  # Use sender's transfer_id
        if sender_transfer:
            print(f"[Debug] Sender registry has transfer: {sender_transfer.status}")
            print(f"[Debug] Source paths: {sender_transfer.source_paths}")
        else:
            print(f"[Debug] Sender registry does NOT have transfer {transfer_id}")
            # Check what transfers sender has
            all_transfers = list(self.sender_agent._registry._transfers.items())
            print(f"[Debug] Sender registry has {len(all_transfers)} transfers:")
            for tid, t in all_transfers:
                print(f"[Debug]   - {tid[:8]}... status={t.status}, paths={t.source_paths}")

        # Diagnostic: Check if sender's server is actually accepting connections
        import socket as sock_module
        print(f"\n[Debug] Testing connection to sender's server at 127.0.0.1:{self.sender_port}...")
        try:
            test_sock = sock_module.socket(sock_module.AF_INET, sock_module.SOCK_STREAM)
            test_sock.settimeout(2.0)
            test_sock.connect(('127.0.0.1', self.sender_port))
            print(f"[Debug] Connection to sender successful!")
            test_sock.close()
        except Exception as e:
            print(f"[Debug] Connection to sender FAILED: {e}")

        # Step 3: Receiver requests the transfer
        print("\n[Step 3] Receiver requesting file download...")
        downloaded_files = self.receiver_agent.request_transfer(self.announced_transfer_id)

        if not downloaded_files:
            print("[FAIL] Download failed")
            return False

        print(f"\n[Step 3] Downloaded {len(downloaded_files)} file(s)")

        # Step 4: Verify files
        print("\n[Step 4] Verifying downloaded files...")
        success = True

        for orig_file in self.test_files:
            # Find matching downloaded file
            downloaded = None
            for df in downloaded_files:
                if df.name == orig_file.name:
                    downloaded = df
                    break

            if not downloaded:
                print(f"  [FAIL] {orig_file.name}: Not found in downloads")
                success = False
                continue

            # Compare checksums
            orig_hash = hashlib.md5(orig_file.read_bytes()).hexdigest()
            dl_hash = hashlib.md5(downloaded.read_bytes()).hexdigest()

            if orig_hash == dl_hash:
                print(f"  [OK] {orig_file.name}: Checksum match ({format_bytes(downloaded.stat().st_size)})")
            else:
                print(f"  [FAIL] {orig_file.name}: Checksum mismatch!")
                success = False

        return success

    def test_single_file_download(self) -> bool:
        """Test downloading a single file (virtual clipboard callback)"""
        print("\n" + "-" * 60)
        print("  TEST: Single File Download (Virtual Clipboard Callback)")
        print("-" * 60)

        # Clear event BEFORE announcing to avoid race condition
        self.announcement_received.clear()

        # Announce files
        print("\n[Step 1] Announcing files...")
        transfer_id = self.sender_agent.announce_files(self.test_files)

        if not transfer_id:
            print("[FAIL] Failed to announce files")
            return False

        # Wait for announcement callback
        if not self.announcement_received.wait(timeout=5):
            print("[FAIL] Receiver didn't get announcement")
            return False

        # Re-set peer to prevent auto-discovery from overwriting with real Mac
        self.receiver_agent.set_peer("127.0.0.1", self.sender_port)

        # Download just the first file (simulates virtual clipboard callback)
        print("\n[Step 2] Downloading single file (index 0)...")
        file_data = self.receiver_agent.download_single_file(self.announced_transfer_id, 0)

        if not file_data:
            print("[FAIL] Single file download failed")
            return False

        # Verify content
        orig_data = self.test_files[0].read_bytes()
        if file_data == orig_data:
            print(f"  [OK] Single file download verified ({format_bytes(len(file_data))})")
            return True
        else:
            print(f"  [FAIL] Content mismatch!")
            return False

    def test_text_sync(self) -> bool:
        """Test text clipboard sync"""
        print("\n" + "-" * 60)
        print("  TEST: Text Clipboard Sync")
        print("-" * 60)

        test_text = "Hello from clipboard sync test! 🎉 Special chars: äöü"
        received_text = None
        text_received = threading.Event()

        def on_text(text):
            nonlocal received_text
            received_text = text
            text_received.set()

        # Temporarily set callback
        orig_callback = self.receiver_agent.on_text_received
        self.receiver_agent.on_text_received = on_text

        print(f"\n[Step 1] Sending text: '{test_text[:30]}...'")
        success = self.sender_agent.send_text(test_text)

        if not success:
            print("[FAIL] Failed to send text")
            self.receiver_agent.on_text_received = orig_callback
            return False

        print("[Step 2] Waiting for receiver...")
        if not text_received.wait(timeout=5):
            print("[FAIL] Text not received")
            self.receiver_agent.on_text_received = orig_callback
            return False

        # Restore callback
        self.receiver_agent.on_text_received = orig_callback

        if received_text == test_text:
            print(f"[OK] Text received and verified!")
            return True
        else:
            print(f"[FAIL] Text mismatch!")
            print(f"  Expected: {test_text}")
            print(f"  Got: {received_text}")
            return False

    def cleanup(self):
        """Clean up test files"""
        import shutil
        try:
            shutil.rmtree(self.test_dir, ignore_errors=True)
            print(f"\n[Cleanup] Removed test directory")
        except:
            pass

    def run_all_tests(self):
        """Run all simulation tests"""
        results = {}

        try:
            self.setup()
            self.start_agents()

            # Run tests
            results['text_sync'] = self.test_text_sync()
            results['lazy_transfer'] = self.test_lazy_transfer()
            results['single_file_download'] = self.test_single_file_download()

        except Exception as e:
            print(f"\n[ERROR] Test exception: {e}")
            import traceback
            traceback.print_exc()
        finally:
            self.stop_agents()
            self.cleanup()

        # Summary
        print("\n" + "=" * 60)
        print("  TEST RESULTS")
        print("=" * 60)

        all_passed = True
        for test_name, passed in results.items():
            status = "PASS" if passed else "FAIL"
            symbol = "✓" if passed else "✗"
            print(f"  {symbol} {test_name}: {status}")
            if not passed:
                all_passed = False

        print("=" * 60)

        if all_passed:
            print("\n  All tests PASSED!\n")
        else:
            print("\n  Some tests FAILED!\n")

        return all_passed


def main():
    test = SimulationTest()
    success = test.run_all_tests()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
