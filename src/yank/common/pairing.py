"""
Device Pairing Module for clipboard-sync

Handles PIN-based pairing between devices:
1. Device A generates a 6-digit PIN and waits for connection
2. Device B connects and sends the PIN
3. If PIN matches, both devices exchange keys and store the pairing
4. Future connections use the shared key for encryption
"""
import os
import json
import socket
import struct
import secrets
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional, Tuple, Dict
from dataclasses import dataclass, asdict
from datetime import datetime

from yank.common.crypto import generate_key, generate_pin, KEY_SIZE

logger = logging.getLogger(__name__)

# Pairing protocol message types
PAIR_REQUEST = 0x01
PAIR_CHALLENGE = 0x02
PAIR_RESPONSE = 0x03
PAIR_SUCCESS = 0x04
PAIR_FAILURE = 0x05

# Config file location
def get_config_dir() -> Path:
    """Get the config directory for storing pairing info"""
    if os.name == 'nt':  # Windows
        config_dir = Path(os.environ.get('APPDATA', Path.home())) / 'clipboard-sync'
    else:  # macOS/Linux
        config_dir = Path.home() / '.clipboard-sync'

    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_pairing_file() -> Path:
    """Get the path to the pairing config file"""
    return get_config_dir() / 'pairing.json'


@dataclass
class PairedDevice:
    """Information about a paired device"""
    device_id: str          # Unique device identifier
    device_name: str        # Human-readable name
    shared_key: str         # Hex-encoded shared encryption key
    paired_at: str          # ISO timestamp
    last_seen: str = ""     # Last successful connection

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> 'PairedDevice':
        return cls(**data)


class PairingManager:
    """Manages device pairing and key storage"""

    def __init__(self):
        self._paired_device: Optional[PairedDevice] = None
        self._encryption_key: Optional[bytes] = None
        self._load_pairing()

    def _load_pairing(self):
        """Load pairing info from disk"""
        pairing_file = get_pairing_file()

        if pairing_file.exists():
            try:
                with open(pairing_file, 'r') as f:
                    data = json.load(f)

                if data.get('paired_device'):
                    self._paired_device = PairedDevice.from_dict(data['paired_device'])
                    self._encryption_key = bytes.fromhex(self._paired_device.shared_key)
                    logger.info(f"Loaded pairing with device: {self._paired_device.device_name}")
            except Exception as e:
                logger.error(f"Failed to load pairing: {e}")

    def _save_pairing(self):
        """Save pairing info to disk"""
        pairing_file = get_pairing_file()

        data = {
            'paired_device': self._paired_device.to_dict() if self._paired_device else None
        }

        try:
            with open(pairing_file, 'w') as f:
                json.dump(data, f, indent=2)

            # Set restrictive permissions on Unix
            if os.name != 'nt':
                os.chmod(pairing_file, 0o600)

        except Exception as e:
            logger.error(f"Failed to save pairing: {e}")

    def is_paired(self) -> bool:
        """Check if we have a paired device"""
        return self._paired_device is not None

    def get_encryption_key(self) -> Optional[bytes]:
        """Get the encryption key for the paired device"""
        return self._encryption_key

    def get_paired_device(self) -> Optional[PairedDevice]:
        """Get info about the paired device"""
        return self._paired_device

    def set_pairing(self, device_id: str, device_name: str, shared_key: bytes):
        """Store a new pairing"""
        self._paired_device = PairedDevice(
            device_id=device_id,
            device_name=device_name,
            shared_key=shared_key.hex(),
            paired_at=datetime.now().isoformat(),
            last_seen=datetime.now().isoformat()
        )
        self._encryption_key = shared_key
        self._save_pairing()
        logger.info(f"Paired with device: {device_name}")

    def update_last_seen(self):
        """Update the last seen timestamp"""
        if self._paired_device:
            self._paired_device.last_seen = datetime.now().isoformat()
            self._save_pairing()

    def clear_pairing(self):
        """Remove the current pairing"""
        self._paired_device = None
        self._encryption_key = None
        self._save_pairing()
        logger.info("Pairing cleared")

    def verify_device(self, device_id: str) -> bool:
        """Verify if a device ID matches our paired device"""
        if not self._paired_device:
            return False
        return self._paired_device.device_id == device_id


def get_device_id() -> str:
    """Generate a unique device ID based on machine characteristics"""
    import platform

    # Combine hostname and platform info for a semi-stable ID
    info = f"{platform.node()}-{platform.system()}-{platform.machine()}"
    return hashlib.sha256(info.encode()).hexdigest()[:16]


def get_device_name() -> str:
    """Get a human-readable device name"""
    import platform
    return f"{platform.node()} ({platform.system()})"


class PairingServer:
    """Server side of pairing - generates PIN and waits for connection"""

    def __init__(self, port: int = 9877):
        self.port = port
        self.pin: Optional[str] = None
        self._server_socket: Optional[socket.socket] = None
        self._running = False

    def start_pairing(self, timeout: int = 120) -> Tuple[bool, str]:
        """
        Start the pairing process as server (PIN displayer)

        Args:
            timeout: Seconds to wait for client connection

        Returns:
            (success, message) tuple
        """
        self.pin = generate_pin()

        # Generate our part of the shared key
        our_key_part = generate_key()

        try:
            self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._server_socket.bind(('0.0.0.0', self.port))
            self._server_socket.listen(1)
            self._server_socket.settimeout(timeout)

            # Get local IP addresses
            local_ips = self._get_local_ips()

            print(f"\n{'='*50}")
            print(f"  PAIRING MODE")
            print(f"{'='*50}")
            print(f"\n  PIN: {self.pin}")
            print(f"\n  On the other device, run:")
            print(f"")
            for ip in local_ips:
                print(f"    ./run.sh join {ip} {self.pin}")
            print(f"")
            print(f"  Waiting for connection... (timeout: {timeout}s)")
            print(f"{'='*50}\n")

            self._running = True

            # Wait for connection
            client_socket, addr = self._server_socket.accept()
            logger.info(f"Pairing connection from {addr}")

            try:
                client_socket.settimeout(30)

                # Receive pairing request with PIN
                data = client_socket.recv(1024)
                if len(data) < 7:
                    return False, "Invalid pairing request"

                msg_type = data[0]
                if msg_type != PAIR_REQUEST:
                    return False, "Unexpected message type"

                pin_len = data[1]
                received_pin = data[2:2+pin_len].decode('utf-8')
                their_device_id = data[2+pin_len:2+pin_len+16].decode('utf-8')
                name_len = data[2+pin_len+16]
                their_device_name = data[2+pin_len+17:2+pin_len+17+name_len].decode('utf-8')
                their_key_part = data[2+pin_len+17+name_len:2+pin_len+17+name_len+KEY_SIZE]

                # Verify PIN
                if received_pin != self.pin:
                    # Send failure
                    client_socket.sendall(bytes([PAIR_FAILURE]) + b"Invalid PIN")
                    return False, "Invalid PIN entered"

                # PIN correct - send our key part and device info
                our_device_id = get_device_id().encode('utf-8')
                our_device_name = get_device_name().encode('utf-8')

                response = bytes([PAIR_SUCCESS])
                response += bytes([len(our_device_id)]) + our_device_id
                response += bytes([len(our_device_name)]) + our_device_name
                response += our_key_part

                client_socket.sendall(response)

                # Combine key parts (XOR for simplicity, both contribute entropy)
                shared_key = bytes(a ^ b for a, b in zip(our_key_part, their_key_part))

                # Store pairing
                manager = PairingManager()
                manager.set_pairing(their_device_id, their_device_name, shared_key)

                return True, f"Successfully paired with {their_device_name}"

            finally:
                client_socket.close()

        except socket.timeout:
            return False, "Pairing timed out - no device connected"
        except Exception as e:
            logger.error(f"Pairing error: {e}")
            return False, f"Pairing failed: {e}"
        finally:
            self._running = False
            if self._server_socket:
                self._server_socket.close()

    def _get_local_ips(self) -> list:
        """Get list of local IP addresses for display"""
        ips = []
        try:
            # Get all network interfaces
            hostname = socket.gethostname()
            # Try to get all IPs associated with hostname
            try:
                for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
                    ip = info[4][0]
                    if not ip.startswith('127.'):
                        ips.append(ip)
            except socket.gaierror:
                pass

            # Also try connecting to external address to find primary IP
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                s.connect(('8.8.8.8', 80))
                primary_ip = s.getsockname()[0]
                s.close()
                if primary_ip not in ips:
                    ips.insert(0, primary_ip)
            except:
                pass

            # Remove duplicates while preserving order
            seen = set()
            unique_ips = []
            for ip in ips:
                if ip not in seen:
                    seen.add(ip)
                    unique_ips.append(ip)
            ips = unique_ips

        except Exception as e:
            logger.debug(f"Error getting local IPs: {e}")

        # Fallback if no IPs found
        if not ips:
            ips = ['<your-ip>']

        return ips

    def stop(self):
        """Stop the pairing server"""
        self._running = False
        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass


class PairingClient:
    """Client side of pairing - enters PIN and connects to server"""

    def __init__(self, host: str, port: int = 9877):
        self.host = host
        self.port = port

    def pair_with_pin(self, pin: str) -> Tuple[bool, str]:
        """
        Connect to pairing server and complete pairing with PIN

        Args:
            pin: 6-digit PIN displayed on server

        Returns:
            (success, message) tuple
        """
        # Generate our part of the shared key
        our_key_part = generate_key()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30)
            sock.connect((self.host, self.port))

            try:
                # Send pairing request with PIN and our info
                our_device_id = get_device_id().encode('utf-8')
                our_device_name = get_device_name().encode('utf-8')
                pin_bytes = pin.encode('utf-8')

                request = bytes([PAIR_REQUEST])
                request += bytes([len(pin_bytes)]) + pin_bytes
                request += our_device_id[:16].ljust(16, b'\0')  # Fixed 16 bytes
                request += bytes([len(our_device_name)]) + our_device_name
                request += our_key_part

                sock.sendall(request)

                # Receive response
                data = sock.recv(1024)
                if not data:
                    return False, "No response from server"

                msg_type = data[0]

                if msg_type == PAIR_FAILURE:
                    error_msg = data[1:].decode('utf-8')
                    return False, f"Pairing rejected: {error_msg}"

                if msg_type != PAIR_SUCCESS:
                    return False, "Unexpected response"

                # Parse server response
                offset = 1
                their_id_len = data[offset]
                offset += 1
                their_device_id = data[offset:offset+their_id_len].decode('utf-8')
                offset += their_id_len

                their_name_len = data[offset]
                offset += 1
                their_device_name = data[offset:offset+their_name_len].decode('utf-8')
                offset += their_name_len

                their_key_part = data[offset:offset+KEY_SIZE]

                # Combine key parts
                shared_key = bytes(a ^ b for a, b in zip(our_key_part, their_key_part))

                # Store pairing
                manager = PairingManager()
                manager.set_pairing(their_device_id, their_device_name, shared_key)

                return True, f"Successfully paired with {their_device_name}"

            finally:
                sock.close()

        except ConnectionRefusedError:
            return False, f"Could not connect to {self.host}:{self.port} - is pairing mode active?"
        except socket.timeout:
            return False, "Connection timed out"
        except Exception as e:
            logger.error(f"Pairing error: {e}")
            return False, f"Pairing failed: {e}"


# Global pairing manager instance
_pairing_manager: Optional[PairingManager] = None


def get_pairing_manager() -> PairingManager:
    """Get the global pairing manager instance"""
    global _pairing_manager
    if _pairing_manager is None:
        _pairing_manager = PairingManager()
    return _pairing_manager


def is_paired() -> bool:
    """Check if device is paired"""
    return get_pairing_manager().is_paired()


def get_encryption_key() -> Optional[bytes]:
    """Get encryption key if paired"""
    return get_pairing_manager().get_encryption_key()


def require_pairing() -> bool:
    """Check if pairing is required (returns True if NOT paired)"""
    return not is_paired()
