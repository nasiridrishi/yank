"""
Protocol for clipboard file transfer

Message Format (unencrypted):
┌──────────────┬──────────────┬─────────────────┬──────────────┐
│ Header (4B)  │ Type (1B)    │ Metadata (JSON) │ File Data    │
│ Total Length │ MSG_TYPE     │ Variable Length │ Binary       │
└──────────────┴──────────────┴─────────────────┴──────────────┘

Message Format (encrypted):
┌──────────────┬──────────────┬──────────────────────────────────┐
│ Header (4B)  │ Flags (1B)   │ Encrypted Payload                │
│ Total Length │ ENCRYPTED    │ (nonce + ciphertext + tag)       │
└──────────────┴──────────────┴──────────────────────────────────┘
"""
import json
import struct
import hashlib
import logging
from dataclasses import dataclass, asdict
from typing import List, Optional
from pathlib import Path
import io

logger = logging.getLogger(__name__)


class MessageType:
    """Message types for the protocol"""
    PING = 0x01
    PONG = 0x02
    FILE_TRANSFER = 0x10
    FILE_ACK = 0x11
    CLIPBOARD_CLEAR = 0x20
    AUTH_CHALLENGE = 0x30    # Server sends challenge
    AUTH_RESPONSE = 0x31     # Client responds to challenge
    AUTH_SUCCESS = 0x32      # Authentication successful
    AUTH_FAILURE = 0x33      # Authentication failed
    ERROR = 0xFF


class MessageFlags:
    """Flags for message header"""
    NONE = 0x00
    ENCRYPTED = 0x01  # Payload is encrypted


@dataclass
class FileInfo:
    """Metadata for a single file"""
    name: str
    size: int
    checksum: str  # MD5 for quick verification
    is_directory: bool = False
    relative_path: str = ""  # For preserving folder structure
    
    def to_dict(self):
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(**data)


@dataclass
class TransferMetadata:
    """Metadata for a clipboard transfer"""
    files: List[FileInfo]
    total_size: int
    timestamp: float
    source_os: str  # 'windows' or 'macos'
    
    def to_dict(self):
        return {
            'files': [f.to_dict() for f in self.files],
            'total_size': self.total_size,
            'timestamp': self.timestamp,
            'source_os': self.source_os
        }
    
    @classmethod
    def from_dict(cls, data: dict):
        return cls(
            files=[FileInfo.from_dict(f) for f in data['files']],
            total_size=data['total_size'],
            timestamp=data['timestamp'],
            source_os=data['source_os']
        )


def calculate_checksum(filepath: Path) -> str:
    """Calculate MD5 checksum of a file"""
    hasher = hashlib.md5()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def calculate_checksum_bytes(data: bytes) -> str:
    """Calculate MD5 checksum of bytes"""
    return hashlib.md5(data).hexdigest()


class MessageBuilder:
    """Build protocol messages"""

    @staticmethod
    def _encrypt_message(message: bytes, key: bytes) -> bytes:
        """Encrypt a message and wrap with encrypted header"""
        from common.crypto import encrypt

        # Original message is: [4 bytes len][1 byte type][payload]
        # We encrypt everything after the length header

        original_len = struct.unpack('>I', message[:4])[0]
        content = message[4:]  # type + payload

        # Encrypt content
        encrypted = encrypt(content, key)

        # Build new message with encrypted flag
        # Format: [4 bytes len][1 byte ENCRYPTED flag][encrypted data]
        new_content = bytes([MessageFlags.ENCRYPTED]) + encrypted
        return struct.pack('>I', len(new_content)) + new_content

    @staticmethod
    def build_ping(key: bytes = None) -> bytes:
        """Build a ping message"""
        message = struct.pack('>IB', 1, MessageType.PING)
        if key:
            return MessageBuilder._encrypt_message(message, key)
        return message

    @staticmethod
    def build_pong(key: bytes = None) -> bytes:
        """Build a pong message"""
        message = struct.pack('>IB', 1, MessageType.PONG)
        if key:
            return MessageBuilder._encrypt_message(message, key)
        return message
    
    @staticmethod
    def build_file_transfer(metadata: TransferMetadata, file_data: bytes, key: bytes = None) -> bytes:
        """
        Build a file transfer message

        Format:
        - 4 bytes: total message length (excluding this header)
        - 1 byte: message type
        - 4 bytes: metadata JSON length
        - N bytes: metadata JSON
        - M bytes: file data
        """
        metadata_json = json.dumps(metadata.to_dict()).encode('utf-8')
        metadata_len = len(metadata_json)

        # Message content (excluding the 4-byte length header)
        content = struct.pack('>BI', MessageType.FILE_TRANSFER, metadata_len)
        content += metadata_json
        content += file_data

        # Prepend total length
        message = struct.pack('>I', len(content)) + content

        if key:
            return MessageBuilder._encrypt_message(message, key)
        return message

    @staticmethod
    def build_ack(success: bool, message: str = "", key: bytes = None) -> bytes:
        """Build an acknowledgment message"""
        ack_data = json.dumps({'success': success, 'message': message}).encode('utf-8')
        content = struct.pack('>B', MessageType.FILE_ACK) + ack_data
        msg = struct.pack('>I', len(content)) + content

        if key:
            return MessageBuilder._encrypt_message(msg, key)
        return msg

    @staticmethod
    def build_error(error_message: str, key: bytes = None) -> bytes:
        """Build an error message"""
        error_data = error_message.encode('utf-8')
        content = struct.pack('>B', MessageType.ERROR) + error_data
        msg = struct.pack('>I', len(content)) + content

        if key:
            return MessageBuilder._encrypt_message(msg, key)
        return msg

    @staticmethod
    def build_auth_challenge(challenge: bytes) -> bytes:
        """Build an authentication challenge message"""
        content = struct.pack('>B', MessageType.AUTH_CHALLENGE) + challenge
        return struct.pack('>I', len(content)) + content

    @staticmethod
    def build_auth_response(response: bytes) -> bytes:
        """Build an authentication response message"""
        content = struct.pack('>B', MessageType.AUTH_RESPONSE) + response
        return struct.pack('>I', len(content)) + content

    @staticmethod
    def build_auth_success() -> bytes:
        """Build an authentication success message"""
        return struct.pack('>IB', 1, MessageType.AUTH_SUCCESS)

    @staticmethod
    def build_auth_failure(reason: str = "") -> bytes:
        """Build an authentication failure message"""
        reason_data = reason.encode('utf-8')
        content = struct.pack('>B', MessageType.AUTH_FAILURE) + reason_data
        return struct.pack('>I', len(content)) + content


class MessageParser:
    """Parse protocol messages"""

    def __init__(self, key: bytes = None):
        self.buffer = bytearray()
        self.key = key  # Encryption key for decryption

    def set_key(self, key: bytes):
        """Set the encryption key for decryption"""
        self.key = key

    def feed(self, data: bytes):
        """Feed data into the parser buffer"""
        self.buffer.extend(data)

    def parse_one(self) -> Optional[tuple]:
        """
        Try to parse one complete message from buffer

        Returns: (message_type, payload) or None if incomplete
        """
        # Need at least 5 bytes (4 length + 1 type/flag)
        if len(self.buffer) < 5:
            return None

        # Read message length
        msg_len = struct.unpack('>I', self.buffer[:4])[0]

        # Check if we have the full message
        total_needed = 4 + msg_len
        if len(self.buffer) < total_needed:
            return None

        # Check if encrypted
        first_byte = self.buffer[4]

        if first_byte == MessageFlags.ENCRYPTED:
            # Encrypted message
            encrypted_data = bytes(self.buffer[5:total_needed])

            # Remove from buffer first
            del self.buffer[:total_needed]

            if not self.key:
                logger.warning("Received encrypted message but no key set")
                return (MessageType.ERROR, b"No encryption key")

            try:
                from common.crypto import decrypt
                decrypted = decrypt(encrypted_data, self.key)

                # Decrypted data is: [1 byte type][payload]
                msg_type = decrypted[0]
                payload = decrypted[1:]

                return (msg_type, payload)

            except Exception as e:
                logger.error(f"Decryption failed: {e}")
                return (MessageType.ERROR, f"Decryption failed: {e}".encode())

        else:
            # Unencrypted message
            msg_type = first_byte
            payload = bytes(self.buffer[5:total_needed])

            # Remove from buffer
            del self.buffer[:total_needed]

            return (msg_type, payload)
    
    @staticmethod
    def parse_file_transfer(payload: bytes) -> tuple:
        """
        Parse a file transfer payload
        
        Returns: (TransferMetadata, file_data)
        """
        # First 4 bytes are metadata length
        metadata_len = struct.unpack('>I', payload[:4])[0]
        
        # Extract metadata JSON
        metadata_json = payload[4:4+metadata_len].decode('utf-8')
        metadata = TransferMetadata.from_dict(json.loads(metadata_json))
        
        # Rest is file data
        file_data = payload[4+metadata_len:]
        
        return (metadata, file_data)
    
    @staticmethod
    def parse_ack(payload: bytes) -> dict:
        """Parse an acknowledgment payload"""
        return json.loads(payload.decode('utf-8'))
    
    @staticmethod
    def parse_error(payload: bytes) -> str:
        """Parse an error payload"""
        return payload.decode('utf-8')


def pack_files(file_paths: List[Path], base_path: Optional[Path] = None) -> tuple:
    """
    Pack multiple files into a single binary blob with metadata
    
    Returns: (TransferMetadata, packed_bytes)
    """
    import time
    import platform
    
    files_info = []
    data_stream = io.BytesIO()
    total_size = 0
    
    for filepath in file_paths:
        filepath = Path(filepath)
        
        if filepath.is_dir():
            # For directories, we'll pack all contents
            for subpath in filepath.rglob('*'):
                if subpath.is_file():
                    rel_path = subpath.relative_to(filepath.parent)
                    file_size = subpath.stat().st_size
                    checksum = calculate_checksum(subpath)
                    
                    files_info.append(FileInfo(
                        name=subpath.name,
                        size=file_size,
                        checksum=checksum,
                        is_directory=False,
                        relative_path=str(rel_path)
                    ))
                    
                    with open(subpath, 'rb') as f:
                        data_stream.write(f.read())
                    total_size += file_size
        else:
            # Single file
            file_size = filepath.stat().st_size
            checksum = calculate_checksum(filepath)
            
            rel_path = filepath.name
            if base_path:
                rel_path = str(filepath.relative_to(base_path))
            
            files_info.append(FileInfo(
                name=filepath.name,
                size=file_size,
                checksum=checksum,
                is_directory=False,
                relative_path=rel_path
            ))
            
            with open(filepath, 'rb') as f:
                data_stream.write(f.read())
            total_size += file_size
    
    metadata = TransferMetadata(
        files=files_info,
        total_size=total_size,
        timestamp=time.time(),
        source_os='windows' if platform.system() == 'Windows' else 'macos'
    )
    
    return (metadata, data_stream.getvalue())


def unpack_files(metadata: TransferMetadata, data: bytes, dest_dir: Path) -> List[Path]:
    """
    Unpack files from binary blob to destination directory
    
    Returns: List of extracted file paths
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    
    extracted_paths = []
    offset = 0
    
    for file_info in metadata.files:
        # Determine destination path
        if file_info.relative_path and '/' in file_info.relative_path:
            dest_path = dest_dir / file_info.relative_path
            dest_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            dest_path = dest_dir / file_info.name
        
        # Handle name collisions
        if dest_path.exists():
            stem = dest_path.stem
            suffix = dest_path.suffix
            counter = 1
            while dest_path.exists():
                dest_path = dest_path.parent / f"{stem}_{counter}{suffix}"
                counter += 1
        
        # Extract file data
        file_data = data[offset:offset + file_info.size]
        offset += file_info.size
        
        # Verify checksum
        actual_checksum = calculate_checksum_bytes(file_data)
        if actual_checksum != file_info.checksum:
            raise ValueError(f"Checksum mismatch for {file_info.name}")
        
        # Write file
        with open(dest_path, 'wb') as f:
            f.write(file_data)
        
        extracted_paths.append(dest_path)
    
    return extracted_paths
