"""
Core Sync Agent - handles network communication between peers

This module provides the networking layer for clipboard sync.
It runs a server to receive files and a client to send files.

Security:
- Requires device pairing before accepting connections
- All messages encrypted with AES-256-GCM after pairing

Lazy Transfer:
- Files are announced with metadata only (instant)
- Actual transfer happens on-demand when peer requests
- Large files are streamed in chunks (memory efficient)
"""
import socket
import threading
import logging
import time
import os
import hashlib
import uuid
from typing import Optional, Callable, List, Dict
from pathlib import Path

import config
from common.protocol import (
    MessageType,
    MessageBuilder,
    MessageParser,
    TransferMetadata,
    ChunkInfo,
    pack_files,
    unpack_files
)
from common.discovery import start_discovery, stop_discovery, get_discovery
from common.pairing import get_pairing_manager, is_paired, get_encryption_key
from common.file_registry import FileRegistry, TransferStatus
from common.chunked_transfer import (
    ChunkedFileReader,
    ChunkedFileWriter,
    ProgressTracker,
    create_file_metadata,
    format_bytes,
    DEFAULT_CHUNK_SIZE
)
from common.transfer_manager import get_transfer_manager, TransferManager

logger = logging.getLogger(__name__)


class SyncAgent:
    """
    Main sync agent that handles:
    - Running a server to receive clipboard files and text
    - Sending clipboard files and text to peers
    - Peer discovery (via mDNS)
    - Encryption and authentication
    """

    def __init__(self,
                 on_files_received: Optional[Callable[[List[Path]], None]] = None,
                 on_text_received: Optional[Callable[[str], None]] = None,
                 on_files_announced: Optional[Callable[[str, TransferMetadata], None]] = None,
                 on_transfer_progress: Optional[Callable[[str, int, int, str], None]] = None,
                 port: int = config.PORT,
                 require_pairing: bool = True):
        """
        Initialize the sync agent

        Args:
            on_files_received: Callback when files are received from peer (legacy/small files)
            on_text_received: Callback when text is received from peer
            on_files_announced: Callback when files are announced (lazy transfer)
                               Args: (transfer_id, metadata)
            on_transfer_progress: Callback for transfer progress updates
                                 Args: (transfer_id, bytes_done, bytes_total, current_file)
            port: Port to listen on
            require_pairing: If True, reject connections from unpaired devices
        """
        self.port = port
        self.on_files_received = on_files_received
        self.on_text_received = on_text_received
        self.on_files_announced = on_files_announced
        self.on_transfer_progress = on_transfer_progress
        self.require_pairing = require_pairing

        self._server_socket: Optional[socket.socket] = None
        self._running = False
        self._server_thread: Optional[threading.Thread] = None
        self._peer_ip: Optional[str] = config.PEER_IP
        self._peer_port: int = port
        self._lock = threading.Lock()

        # Track last sent to avoid loops
        self._last_sent_hash: Optional[str] = None
        self._last_sent_time: float = 0
        self._last_sent_text_hash: Optional[str] = None
        self._last_sent_text_time: float = 0

        # Pairing manager
        self._pairing_manager = get_pairing_manager()

        # File registry for lazy transfers (each agent gets its own instance)
        self._registry = FileRegistry()

        # Transfer manager for error recovery
        self._transfer_manager = get_transfer_manager(checkpoint_dir=config.TEMP_DIR / "checkpoints")

        # Active file writers for receiving chunks
        self._active_writers: Dict[str, Dict[int, ChunkedFileWriter]] = {}  # transfer_id -> {file_index -> writer}
    
    def start(self):
        """Start the sync agent (server + discovery)"""
        if self._running:
            return
        
        self._running = True
        
        # Start server
        self._start_server()
        
        # Start peer discovery
        if config.USE_AUTO_DISCOVERY:
            start_discovery(
                port=self.port,
                on_peer_found=self._on_peer_discovered,
                on_peer_lost=self._on_peer_lost
            )
        
        logger.info(f"Sync agent started on port {self.port}")
    
    def stop(self):
        """Stop the sync agent"""
        self._running = False
        
        # Stop discovery
        stop_discovery()
        
        # Stop server
        if self._server_socket:
            try:
                self._server_socket.close()
            except:
                pass
        
        if self._server_thread:
            self._server_thread.join(timeout=2)
        
        logger.info("Sync agent stopped")
    
    def _start_server(self):
        """Start the server socket"""
        self._server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_socket.bind(('0.0.0.0', self.port))
        self._server_socket.listen(5)
        self._server_socket.settimeout(1.0)  # For clean shutdown
        
        self._server_thread = threading.Thread(target=self._server_loop, daemon=True)
        self._server_thread.start()
    
    def _server_loop(self):
        """Main server loop accepting connections"""
        while self._running:
            try:
                client_socket, addr = self._server_socket.accept()
                logger.debug(f"Connection from {addr}")
                
                # Handle in separate thread
                handler = threading.Thread(
                    target=self._handle_client,
                    args=(client_socket, addr),
                    daemon=True
                )
                handler.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self._running:
                    logger.error(f"Server error: {e}")
    
    def _handle_client(self, client_socket: socket.socket, addr: tuple):
        """Handle incoming connection with authentication"""
        encryption_key = self._pairing_manager.get_encryption_key()

        # Check if pairing is required
        if self.require_pairing and not self._pairing_manager.is_paired():
            logger.warning(f"Rejecting connection from {addr} - not paired")
            try:
                client_socket.sendall(MessageBuilder.build_auth_failure("Device not paired"))
            except:
                pass
            client_socket.close()
            return

        # Perform challenge-response authentication
        if self.require_pairing and encryption_key:
            if not self._authenticate_client(client_socket, addr, encryption_key):
                client_socket.close()
                return

        # Create parser with encryption key
        parser = MessageParser(key=encryption_key)

        try:
            client_socket.settimeout(30.0)

            while True:
                data = client_socket.recv(config.BUFFER_SIZE)
                if not data:
                    break

                parser.feed(data)

                while True:
                    result = parser.parse_one()
                    if result is None:
                        break

                    msg_type, payload = result
                    self._handle_message(client_socket, msg_type, payload, encryption_key)

        except socket.timeout:
            logger.warning(f"Connection timeout from {addr}")
        except Exception as e:
            logger.error(f"Error handling client {addr}: {e}")
        finally:
            client_socket.close()

    def _authenticate_client(self, client_socket: socket.socket, addr: tuple, key: bytes) -> bool:
        """
        Authenticate a client using challenge-response

        Returns True if authenticated, False otherwise
        """
        try:
            # Generate random challenge
            challenge = os.urandom(32)

            # Send challenge
            client_socket.sendall(MessageBuilder.build_auth_challenge(challenge))

            # Receive response
            client_socket.settimeout(10.0)
            data = client_socket.recv(config.BUFFER_SIZE)

            if not data:
                logger.warning(f"No auth response from {addr}")
                return False

            parser = MessageParser()
            parser.feed(data)
            result = parser.parse_one()

            if not result:
                logger.warning(f"Invalid auth response from {addr}")
                return False

            msg_type, payload = result

            if msg_type != MessageType.AUTH_RESPONSE:
                logger.warning(f"Unexpected message type {msg_type} from {addr}")
                client_socket.sendall(MessageBuilder.build_auth_failure("Invalid response"))
                return False

            # Verify response: should be HMAC(challenge, key)
            expected = hashlib.sha256(challenge + key).digest()

            if payload != expected:
                logger.warning(f"Auth failed from {addr} - invalid response")
                client_socket.sendall(MessageBuilder.build_auth_failure("Authentication failed"))
                return False

            # Send success
            client_socket.sendall(MessageBuilder.build_auth_success())
            logger.info(f"Authenticated connection from {addr}")
            self._pairing_manager.update_last_seen()
            return True

        except socket.timeout:
            logger.warning(f"Auth timeout from {addr}")
            return False
        except Exception as e:
            logger.error(f"Auth error from {addr}: {e}")
            return False
    
    def _handle_message(self, client_socket: socket.socket, msg_type: int, payload: bytes, key: bytes = None):
        """Handle a received message"""
        if msg_type == MessageType.PING:
            client_socket.sendall(MessageBuilder.build_pong(key))

        elif msg_type == MessageType.FILE_TRANSFER:
            try:
                metadata, file_data = MessageParser.parse_file_transfer(payload)

                # Unpack files to temp directory
                dest_dir = config.TEMP_DIR / f"recv_{int(time.time() * 1000)}"
                extracted_paths = unpack_files(metadata, file_data, dest_dir)

                logger.info(f"Received {len(extracted_paths)} files from peer")

                # Send ACK
                client_socket.sendall(MessageBuilder.build_ack(True, "Files received", key))

                # Callback to inject into clipboard
                if self.on_files_received:
                    self.on_files_received(extracted_paths)

            except Exception as e:
                logger.error(f"Error processing file transfer: {e}")
                client_socket.sendall(MessageBuilder.build_ack(False, str(e), key))

        elif msg_type == MessageType.TEXT_TRANSFER:
            try:
                text = MessageParser.parse_text_transfer(payload)
                logger.info(f"Received text from peer ({len(text)} chars)")

                # Send ACK
                client_socket.sendall(MessageBuilder.build_text_ack(True, "Text received", key))

                # Callback to inject into clipboard
                if self.on_text_received:
                    self.on_text_received(text)

            except Exception as e:
                logger.error(f"Error processing text transfer: {e}")
                client_socket.sendall(MessageBuilder.build_text_ack(False, str(e), key))

        # ========== Lazy Transfer Message Handlers ==========

        elif msg_type == MessageType.FILE_ANNOUNCE:
            self._handle_file_announce(client_socket, payload, key)

        elif msg_type == MessageType.FILE_REQUEST:
            self._handle_file_request(client_socket, payload, key)

        elif msg_type == MessageType.FILE_CHUNK:
            self._handle_file_chunk(client_socket, payload, key)

        elif msg_type == MessageType.FILE_CHUNK_ACK:
            # Just log it - used for flow control
            ack = MessageParser.parse_file_chunk_ack(payload)
            logger.debug(f"Chunk ACK: {ack['transfer_id']} file {ack['file_index']} chunk {ack['chunk_index']}")

        elif msg_type == MessageType.TRANSFER_COMPLETE:
            self._handle_transfer_complete(client_socket, payload, key)

        elif msg_type == MessageType.TRANSFER_CANCEL:
            self._handle_transfer_cancel(payload)

        elif msg_type == MessageType.TRANSFER_ERROR:
            self._handle_transfer_error(payload)

        elif msg_type == MessageType.ERROR:
            error_msg = MessageParser.parse_error(payload)
            logger.error(f"Received error from peer: {error_msg}")
    
    def _on_peer_discovered(self, ip: str, port: int):
        """Called when a peer is discovered"""
        with self._lock:
            self._peer_ip = ip
            self._peer_port = port
        logger.info(f"Peer discovered: {ip}:{port}")
    
    def _on_peer_lost(self, name: str):
        """Called when a peer is lost"""
        # Could clear peer info, but we keep it in case it comes back
        logger.info(f"Peer lost: {name}")
    
    def set_peer(self, ip: str, port: int = None):
        """Manually set peer address"""
        with self._lock:
            self._peer_ip = ip
            self._peer_port = port or self.port
        logger.info(f"Peer set to {ip}:{self._peer_port}")
    
    def send_files(self, file_paths: List[Path]) -> bool:
        """
        Send files to the connected peer

        Args:
            file_paths: List of file paths to send

        Returns:
            True if successful, False otherwise
        """
        # Check if paired
        encryption_key = self._pairing_manager.get_encryption_key()
        if self.require_pairing and not self._pairing_manager.is_paired():
            logger.error("Cannot send files - not paired with any device")
            return False

        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            # Try to get from discovery
            if config.USE_AUTO_DISCOVERY:
                discovery = get_discovery()
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip, peer_port = peer
                else:
                    logger.warning("No peer available to send files")
                    return False
            else:
                logger.warning("No peer configured")
                return False

        try:
            # Pack files
            metadata, file_data = pack_files(file_paths)

            # Check size limit
            if metadata.total_size > config.MAX_TOTAL_SIZE:
                logger.error(f"Total size {metadata.total_size} exceeds limit {config.MAX_TOTAL_SIZE}")
                return False

            # Create hash to detect loops
            content_hash = hashlib.md5(file_data[:1024]).hexdigest()

            # Avoid sending duplicates
            current_time = time.time()
            if content_hash == self._last_sent_hash and current_time - self._last_sent_time < 2:
                logger.debug("Skipping duplicate send")
                return True

            # Build message (encrypted if we have a key)
            message = MessageBuilder.build_file_transfer(metadata, file_data, encryption_key)

            # Connect and send
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30.0)

            try:
                sock.connect((peer_ip, peer_port))

                # Authenticate with server if pairing enabled
                if self.require_pairing and encryption_key:
                    if not self._authenticate_with_server(sock, encryption_key):
                        logger.error("Authentication with peer failed")
                        return False

                # Send in chunks
                total_sent = 0
                while total_sent < len(message):
                    sent = sock.send(message[total_sent:total_sent + config.BUFFER_SIZE])
                    if sent == 0:
                        raise RuntimeError("Socket connection broken")
                    total_sent += sent

                # Wait for ACK
                parser = MessageParser(key=encryption_key)
                while True:
                    data = sock.recv(config.BUFFER_SIZE)
                    if not data:
                        break

                    parser.feed(data)
                    result = parser.parse_one()
                    if result:
                        msg_type, payload = result
                        if msg_type == MessageType.FILE_ACK:
                            ack = MessageParser.parse_ack(payload)
                            if ack['success']:
                                logger.info(f"Files sent successfully ({metadata.total_size} bytes)")
                                self._last_sent_hash = content_hash
                                self._last_sent_time = current_time
                                return True
                            else:
                                logger.error(f"Peer rejected files: {ack['message']}")
                                return False
                        elif msg_type == MessageType.AUTH_FAILURE:
                            logger.error(f"Authentication rejected: {payload.decode('utf-8', errors='ignore')}")
                            return False
                        break

            finally:
                sock.close()

        except socket.timeout:
            logger.error("Timeout sending files to peer")
            return False
        except ConnectionRefusedError:
            logger.error(f"Connection refused by {peer_ip}:{peer_port}")
            return False
        except Exception as e:
            logger.error(f"Error sending files: {e}")
            return False

        return False

    def send_text(self, text: str) -> bool:
        """
        Send text to the connected peer

        Args:
            text: Text string to send

        Returns:
            True if successful, False otherwise
        """
        # Check if paired
        encryption_key = self._pairing_manager.get_encryption_key()
        if self.require_pairing and not self._pairing_manager.is_paired():
            logger.error("Cannot send text - not paired with any device")
            return False

        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            # Try to get from discovery
            if config.USE_AUTO_DISCOVERY:
                discovery = get_discovery()
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip, peer_port = peer
                else:
                    logger.warning("No peer available to send text")
                    return False
            else:
                logger.warning("No peer configured")
                return False

        try:
            # Create hash to detect loops
            text_hash = hashlib.md5(text.encode('utf-8')).hexdigest()

            # Avoid sending duplicates
            current_time = time.time()
            if text_hash == self._last_sent_text_hash and current_time - self._last_sent_text_time < 2:
                logger.debug("Skipping duplicate text send")
                return True

            # Build message (encrypted if we have a key)
            message = MessageBuilder.build_text_transfer(text, encryption_key)

            # Connect and send
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30.0)

            try:
                sock.connect((peer_ip, peer_port))

                # Authenticate with server if pairing enabled
                if self.require_pairing and encryption_key:
                    if not self._authenticate_with_server(sock, encryption_key):
                        logger.error("Authentication with peer failed")
                        return False

                # Send message
                sock.sendall(message)

                # Wait for ACK
                parser = MessageParser(key=encryption_key)
                while True:
                    data = sock.recv(config.BUFFER_SIZE)
                    if not data:
                        break

                    parser.feed(data)
                    result = parser.parse_one()
                    if result:
                        msg_type, payload = result
                        if msg_type == MessageType.TEXT_ACK:
                            ack = MessageParser.parse_text_ack(payload)
                            if ack['success']:
                                logger.info(f"Text sent successfully ({len(text)} chars)")
                                self._last_sent_text_hash = text_hash
                                self._last_sent_text_time = current_time
                                return True
                            else:
                                logger.error(f"Peer rejected text: {ack['message']}")
                                return False
                        elif msg_type == MessageType.AUTH_FAILURE:
                            logger.error(f"Authentication rejected: {payload.decode('utf-8', errors='ignore')}")
                            return False
                        break

            finally:
                sock.close()

        except socket.timeout:
            logger.error("Timeout sending text to peer")
            return False
        except ConnectionRefusedError:
            logger.error(f"Connection refused by {peer_ip}:{peer_port}")
            return False
        except Exception as e:
            logger.error(f"Error sending text: {e}")
            return False

        return False

    def _authenticate_with_server(self, sock: socket.socket, key: bytes) -> bool:
        """
        Authenticate with the server using challenge-response

        Returns True if authenticated, False otherwise
        """
        try:
            # Receive challenge
            sock.settimeout(10.0)
            data = sock.recv(config.BUFFER_SIZE)

            if not data:
                logger.warning("No challenge from server")
                return False

            parser = MessageParser()
            parser.feed(data)
            result = parser.parse_one()

            if not result:
                logger.warning("Invalid challenge from server")
                return False

            msg_type, payload = result

            if msg_type == MessageType.AUTH_FAILURE:
                logger.warning(f"Server rejected connection: {payload.decode('utf-8', errors='ignore')}")
                return False

            if msg_type != MessageType.AUTH_CHALLENGE:
                logger.warning(f"Unexpected message type {msg_type}")
                return False

            # Compute response: SHA256(challenge + key)
            response = hashlib.sha256(payload + key).digest()

            # Send response
            sock.sendall(MessageBuilder.build_auth_response(response))

            # Wait for success/failure
            data = sock.recv(config.BUFFER_SIZE)
            if not data:
                return False

            parser = MessageParser()
            parser.feed(data)
            result = parser.parse_one()

            if result and result[0] == MessageType.AUTH_SUCCESS:
                logger.debug("Authentication successful")
                self._pairing_manager.update_last_seen()
                return True

            if result and result[0] == MessageType.AUTH_FAILURE:
                logger.warning(f"Auth failed: {result[1].decode('utf-8', errors='ignore')}")

            return False

        except socket.timeout:
            logger.warning("Auth timeout")
            return False
        except Exception as e:
            logger.error(f"Auth error: {e}")
            return False
    
    def ping_peer(self) -> bool:
        """Ping the peer to check connectivity"""
        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            return False

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5.0)
            sock.connect((peer_ip, peer_port))
            sock.sendall(MessageBuilder.build_ping())

            data = sock.recv(config.BUFFER_SIZE)
            sock.close()

            parser = MessageParser()
            parser.feed(data)
            result = parser.parse_one()

            if result and result[0] == MessageType.PONG:
                return True

        except Exception as e:
            logger.debug(f"Ping failed: {e}")

        return False

    # ========== Lazy Transfer Methods ==========

    def announce_files(self, file_paths: List[Path], expiry_seconds: int = 300) -> Optional[str]:
        """
        Announce files available for transfer (metadata only, no file data sent).

        This is instant even for 100GB of files because only metadata is sent.
        The peer will request actual file data when they paste.

        Args:
            file_paths: List of file paths to announce
            expiry_seconds: How long the transfer offer is valid (default 5 minutes)

        Returns:
            transfer_id if successful, None otherwise
        """
        # Check if paired
        encryption_key = self._pairing_manager.get_encryption_key()
        if self.require_pairing and not self._pairing_manager.is_paired():
            logger.error("Cannot announce files - not paired with any device")
            return None

        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            if config.USE_AUTO_DISCOVERY:
                discovery = get_discovery()
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip, peer_port = peer
                else:
                    logger.warning("No peer available to announce files")
                    return None
            else:
                logger.warning("No peer configured")
                return None

        try:
            # Generate transfer ID
            transfer_id = str(uuid.uuid4())

            # Create metadata (fast - only reads file sizes and calculates checksums)
            metadata = create_file_metadata(
                file_paths,
                transfer_id,
                expiry_seconds=expiry_seconds
            )

            # Register in our registry (so we can serve chunks later)
            self._registry.register_announced(transfer_id, metadata, file_paths)

            # Build and send announce message
            message = MessageBuilder.build_file_announce(metadata, encryption_key)

            # Connect and send
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(30.0)

            try:
                sock.connect((peer_ip, peer_port))

                # Authenticate if required
                if self.require_pairing and encryption_key:
                    if not self._authenticate_with_server(sock, encryption_key):
                        logger.error("Authentication with peer failed")
                        return None

                sock.sendall(message)
                logger.info(f"Announced {len(metadata.files)} files ({format_bytes(metadata.total_size)}) - transfer_id: {transfer_id}")

                return transfer_id

            finally:
                sock.close()

        except Exception as e:
            logger.error(f"Error announcing files: {e}")
            return None

    def request_transfer(self, transfer_id: str, dest_dir: Path = None) -> Optional[List[Path]]:
        """
        Request and download files from a pending transfer.

        Features:
        - Cancellation support (check cancel_transfer())
        - Retry logic for failed chunks
        - Resume support for interrupted transfers
        - Timeout handling

        Args:
            transfer_id: The transfer ID from FILE_ANNOUNCE
            dest_dir: Destination directory (defaults to temp dir)

        Returns:
            List of downloaded file paths, or None on failure
        """
        # Get transfer info from registry
        transfer_info = self._registry.get_transfer(transfer_id)
        if not transfer_info:
            logger.error(f"Transfer not found: {transfer_id}")
            return None

        if transfer_info.is_expired:
            logger.error(f"Transfer expired: {transfer_id}")
            self._registry.fail_transfer(transfer_id, "Transfer expired")
            return None

        # Set up destination
        if not dest_dir:
            dest_dir = config.TEMP_DIR / f"recv_{int(time.time() * 1000)}"
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Mark transfer as starting
        self._registry.start_transfer(transfer_id, dest_dir)

        # Register with transfer manager for cancellation support
        cancel_event = self._transfer_manager.start_transfer(transfer_id)

        # Check if we can resume from a previous attempt
        resume_offset = 0
        checkpoint = self._transfer_manager.get_checkpoint(transfer_id)
        if checkpoint and checkpoint.bytes_transferred > 0:
            resume_offset = checkpoint.bytes_transferred
            logger.info(f"Resuming transfer from {format_bytes(resume_offset)}")

        encryption_key = self._pairing_manager.get_encryption_key()

        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            if config.USE_AUTO_DISCOVERY:
                discovery = get_discovery()
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip, peer_port = peer

        if not peer_ip:
            logger.error("No peer available for download")
            self._registry.fail_transfer(transfer_id, "No peer available")
            self._transfer_manager.fail_transfer(transfer_id, "No peer available")
            return None

        downloaded_files = []
        metadata = transfer_info.metadata

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(self._transfer_manager.chunk_timeout)
            sock.connect((peer_ip, peer_port))

            # Authenticate if required
            if self.require_pairing and encryption_key:
                if not self._authenticate_with_server(sock, encryption_key):
                    logger.error("Authentication with peer failed")
                    self._registry.fail_transfer(transfer_id, "Authentication failed")
                    self._transfer_manager.fail_transfer(transfer_id, "Authentication failed")
                    return None

            parser = MessageParser(key=encryption_key)
            total_bytes_received = resume_offset

            # Request each file
            for file_info in metadata.files:
                # Check for cancellation
                if cancel_event.is_set():
                    logger.info(f"Transfer cancelled: {transfer_id}")
                    self._registry.cancel_transfer(transfer_id, "User cancelled")
                    return None

                logger.info(f"Requesting file: {file_info.name} ({format_bytes(file_info.size)})")

                # Determine destination path
                if file_info.relative_path and '/' in file_info.relative_path:
                    file_dest = dest_dir / file_info.relative_path
                else:
                    file_dest = dest_dir / file_info.name

                # Create writer for this file
                writer = ChunkedFileWriter(file_dest, file_info.size, file_info.checksum)

                # Calculate offset for this file (for resume)
                file_offset = 0
                if checkpoint and checkpoint.file_index == file_info.file_index:
                    # Resume this specific file
                    file_offset = checkpoint.bytes_transferred - sum(
                        f.size for f in metadata.files[:file_info.file_index]
                    )
                    file_offset = max(0, file_offset)

                # Send request (with offset for resume)
                request_msg = MessageBuilder.build_file_request(
                    transfer_id,
                    file_info.file_index,
                    file_offset,
                    encryption_key
                )
                sock.sendall(request_msg)

                # Receive chunks with retry support
                file_bytes_received = file_offset
                consecutive_errors = 0

                while file_bytes_received < file_info.size:
                    # Check for cancellation
                    if cancel_event.is_set():
                        logger.info(f"Transfer cancelled during file: {file_info.name}")
                        writer.cleanup()
                        self._registry.cancel_transfer(transfer_id, "User cancelled")
                        return None

                    try:
                        data = sock.recv(config.BUFFER_SIZE)
                        if not data:
                            raise RuntimeError("Connection closed during transfer")

                        parser.feed(data)
                        consecutive_errors = 0  # Reset on successful receive
                        self._transfer_manager.reset_retry_count(transfer_id)

                        while True:
                            result = parser.parse_one()
                            if result is None:
                                break

                            msg_type, payload = result

                            if msg_type == MessageType.FILE_CHUNK:
                                chunk_info, chunk_data = MessageParser.parse_file_chunk(payload)

                                # Write chunk
                                if not writer.write_chunk(chunk_info.offset, chunk_data, chunk_info.checksum):
                                    raise RuntimeError(f"Checksum mismatch at offset {chunk_info.offset}")

                                file_bytes_received += len(chunk_data)
                                total_bytes_received += len(chunk_data)

                                # Update progress in registry and transfer manager
                                self._registry.update_transfer_progress(
                                    transfer_id,
                                    total_bytes_received,
                                    file_info.file_index
                                )
                                self._transfer_manager.update_progress(
                                    transfer_id,
                                    file_info.file_index,
                                    total_bytes_received,
                                    chunk_info.chunk_index
                                )

                                if self.on_transfer_progress:
                                    self.on_transfer_progress(
                                        transfer_id,
                                        total_bytes_received,
                                        metadata.total_size,
                                        file_info.name
                                    )

                                # Send ACK for flow control
                                ack_msg = MessageBuilder.build_file_chunk_ack(
                                    transfer_id,
                                    file_info.file_index,
                                    chunk_info.chunk_index,
                                    encryption_key
                                )
                                sock.sendall(ack_msg)

                                if chunk_info.is_last:
                                    break

                            elif msg_type == MessageType.TRANSFER_ERROR:
                                error_info = MessageParser.parse_transfer_error(payload)
                                raise RuntimeError(f"Peer error: {error_info['error']}")

                    except socket.timeout:
                        consecutive_errors += 1
                        should_retry, delay, attempt = self._transfer_manager.should_retry_chunk(
                            transfer_id, "Timeout waiting for chunk"
                        )
                        if should_retry:
                            logger.warning(f"Timeout, retrying in {delay:.1f}s (attempt {attempt})")
                            time.sleep(delay)
                            continue
                        else:
                            raise RuntimeError(f"Transfer timeout after {attempt} retries")

                    except Exception as e:
                        consecutive_errors += 1
                        should_retry, delay, attempt = self._transfer_manager.should_retry_chunk(
                            transfer_id, str(e)
                        )
                        if should_retry and consecutive_errors < 5:
                            logger.warning(f"Error: {e}, retrying in {delay:.1f}s (attempt {attempt})")
                            time.sleep(delay)
                            continue
                        else:
                            raise

                # Finalize file
                final_path = writer.finalize()
                downloaded_files.append(final_path)
                self._registry.add_downloaded_file(transfer_id, final_path)
                logger.info(f"Downloaded: {final_path}")

            # Send completion
            complete_msg = MessageBuilder.build_transfer_complete(transfer_id, encryption_key)
            sock.sendall(complete_msg)

            # Mark complete
            self._registry.complete_transfer(transfer_id)
            self._transfer_manager.complete_transfer(transfer_id)
            logger.info(f"Transfer complete: {len(downloaded_files)} files")

            return downloaded_files

        except Exception as e:
            error_msg = str(e)
            logger.error(f"Error during transfer: {error_msg}")
            self._registry.fail_transfer(transfer_id, error_msg)
            self._transfer_manager.fail_transfer(transfer_id, error_msg)

            # Send cancel message to peer
            if sock:
                try:
                    cancel_msg = MessageBuilder.build_transfer_cancel(transfer_id, error_msg, encryption_key)
                    sock.sendall(cancel_msg)
                except:
                    pass

            return None

        finally:
            if sock:
                try:
                    sock.close()
                except:
                    pass

    def cancel_transfer(self, transfer_id: str, reason: str = "User cancelled") -> bool:
        """
        Cancel an ongoing transfer.

        Args:
            transfer_id: The transfer to cancel
            reason: Reason for cancellation

        Returns:
            True if transfer was found and cancel signal sent
        """
        # Signal cancellation through transfer manager
        cancelled = self._transfer_manager.cancel_transfer(transfer_id, reason)

        if cancelled:
            # Also update registry
            self._registry.cancel_transfer(transfer_id, reason)

            # Try to notify peer
            encryption_key = self._pairing_manager.get_encryption_key()
            try:
                with self._lock:
                    peer_ip = self._peer_ip
                    peer_port = self._peer_port

                if peer_ip:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(5.0)
                    sock.connect((peer_ip, peer_port))

                    if self.require_pairing and encryption_key:
                        self._authenticate_with_server(sock, encryption_key)

                    cancel_msg = MessageBuilder.build_transfer_cancel(transfer_id, reason, encryption_key)
                    sock.sendall(cancel_msg)
                    sock.close()
            except Exception as e:
                logger.debug(f"Could not notify peer of cancellation: {e}")

        return cancelled

    def get_resumable_transfers(self) -> list:
        """Get list of transfers that can be resumed"""
        return self._transfer_manager.get_resumable_transfers()

    def download_single_file(self, transfer_id: str, file_index: int) -> Optional[bytes]:
        """
        Download a single file and return its content as bytes.

        This is used by the virtual clipboard to download files on-demand.

        Args:
            transfer_id: The transfer ID
            file_index: Index of the file to download

        Returns:
            File bytes or None on failure
        """
        # Get transfer info
        transfer_info = self._registry.get_transfer(transfer_id)
        if not transfer_info:
            logger.error(f"Transfer not found: {transfer_id}")
            return None

        if transfer_info.is_expired:
            logger.error(f"Transfer expired: {transfer_id}")
            return None

        # Find the file info
        file_info = None
        for f in transfer_info.metadata.files:
            if f.file_index == file_index:
                file_info = f
                break

        if not file_info:
            logger.error(f"File index {file_index} not found in transfer {transfer_id}")
            return None

        encryption_key = self._pairing_manager.get_encryption_key()

        with self._lock:
            peer_ip = self._peer_ip
            peer_port = self._peer_port

        if not peer_ip:
            if config.USE_AUTO_DISCOVERY:
                discovery = get_discovery()
                peer = discovery.get_first_peer()
                if peer:
                    peer_ip, peer_port = peer

        if not peer_ip:
            logger.error("No peer available")
            return None

        sock = None
        file_data = bytearray()

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(60.0)
            sock.connect((peer_ip, peer_port))

            # Authenticate
            if self.require_pairing and encryption_key:
                if not self._authenticate_with_server(sock, encryption_key):
                    logger.error("Authentication failed")
                    return None

            parser = MessageParser(key=encryption_key)

            # Request the file
            logger.info(f"Downloading: {file_info.name} ({format_bytes(file_info.size)})")

            request_msg = MessageBuilder.build_file_request(
                transfer_id,
                file_index,
                0,
                encryption_key
            )
            sock.sendall(request_msg)

            # Receive chunks
            bytes_received = 0
            while bytes_received < file_info.size:
                data = sock.recv(config.BUFFER_SIZE)
                if not data:
                    raise RuntimeError("Connection closed")

                parser.feed(data)

                while True:
                    result = parser.parse_one()
                    if result is None:
                        break

                    msg_type, payload = result

                    if msg_type == MessageType.FILE_CHUNK:
                        chunk_info, chunk_data = MessageParser.parse_file_chunk(payload)

                        # Verify chunk checksum
                        actual_checksum = hashlib.md5(chunk_data).hexdigest()
                        if actual_checksum != chunk_info.checksum:
                            raise RuntimeError("Chunk checksum mismatch")

                        # Append data at correct offset
                        if chunk_info.offset == len(file_data):
                            file_data.extend(chunk_data)
                        else:
                            # Handle out-of-order (shouldn't happen)
                            if chunk_info.offset > len(file_data):
                                file_data.extend(b'\x00' * (chunk_info.offset - len(file_data)))
                            file_data[chunk_info.offset:chunk_info.offset + len(chunk_data)] = chunk_data

                        bytes_received += len(chunk_data)

                        # Progress callback
                        if self.on_transfer_progress:
                            self.on_transfer_progress(
                                transfer_id,
                                bytes_received,
                                file_info.size,
                                file_info.name
                            )

                        # Send ACK
                        ack_msg = MessageBuilder.build_file_chunk_ack(
                            transfer_id,
                            file_index,
                            chunk_info.chunk_index,
                            encryption_key
                        )
                        sock.sendall(ack_msg)

                        if chunk_info.is_last:
                            break

                    elif msg_type == MessageType.TRANSFER_ERROR:
                        error = MessageParser.parse_transfer_error(payload)
                        raise RuntimeError(f"Peer error: {error['error']}")

            # Verify final checksum
            final_checksum = hashlib.md5(file_data).hexdigest()
            if final_checksum != file_info.checksum:
                logger.error("File checksum mismatch")
                return None

            logger.info(f"Downloaded: {file_info.name}")
            return bytes(file_data)

        except Exception as e:
            logger.error(f"Download error: {e}")
            return None

        finally:
            if sock:
                try:
                    sock.close()
                except:
                    pass

    def _handle_file_announce(self, client_socket: socket.socket, payload: bytes, key: bytes = None):
        """Handle FILE_ANNOUNCE - peer is offering files"""
        try:
            metadata = MessageParser.parse_file_announce(payload)
            transfer_id = metadata.transfer_id

            logger.info(f"Files announced: {len(metadata.files)} files ({format_bytes(metadata.total_size)})")
            for f in metadata.files:
                logger.info(f"  - {f.name} ({format_bytes(f.size)})")

            # Register as pending
            self._registry.register_pending(transfer_id, metadata)

            # Notify callback
            if self.on_files_announced:
                self.on_files_announced(transfer_id, metadata)

        except Exception as e:
            logger.error(f"Error handling file announce: {e}")

    def _handle_file_request(self, client_socket: socket.socket, payload: bytes, key: bytes = None):
        """Handle FILE_REQUEST - peer wants to download a file"""
        try:
            request = MessageParser.parse_file_request(payload)
            transfer_id = request['transfer_id']
            file_index = request['file_index']
            offset = request.get('offset', 0)

            logger.info(f"File request: transfer={transfer_id}, file={file_index}, offset={offset}")

            # Get file path from registry
            file_path = self._registry.get_file_for_transfer(transfer_id, file_index)
            if not file_path:
                error_msg = MessageBuilder.build_transfer_error(transfer_id, "File not found", key)
                client_socket.sendall(error_msg)
                return

            if not file_path.exists():
                error_msg = MessageBuilder.build_transfer_error(transfer_id, "File no longer exists", key)
                client_socket.sendall(error_msg)
                return

            # Stream file in chunks
            transfer_info = self._registry.get_transfer(transfer_id)
            chunk_size = transfer_info.metadata.chunk_size if transfer_info else DEFAULT_CHUNK_SIZE

            reader = ChunkedFileReader(file_path, chunk_size=chunk_size)

            for chunk_index, chunk_offset, data, checksum, is_last in reader.read_chunks(offset):
                chunk_info = ChunkInfo(
                    transfer_id=transfer_id,
                    file_index=file_index,
                    chunk_index=chunk_index,
                    offset=chunk_offset,
                    size=len(data),
                    checksum=checksum,
                    is_last=is_last
                )

                chunk_msg = MessageBuilder.build_file_chunk(chunk_info, data, key)
                client_socket.sendall(chunk_msg)

                logger.debug(f"Sent chunk {chunk_index} ({len(data)} bytes, last={is_last})")

            logger.info(f"Finished streaming file {file_index} for transfer {transfer_id}")

        except Exception as e:
            logger.error(f"Error handling file request: {e}")
            try:
                error_msg = MessageBuilder.build_transfer_error(
                    request.get('transfer_id', ''),
                    str(e),
                    key
                )
                client_socket.sendall(error_msg)
            except:
                pass

    def _handle_file_chunk(self, client_socket: socket.socket, payload: bytes, key: bytes = None):
        """Handle FILE_CHUNK - receiving a chunk of file data"""
        try:
            chunk_info, chunk_data = MessageParser.parse_file_chunk(payload)
            transfer_id = chunk_info.transfer_id
            file_index = chunk_info.file_index

            # Get or create writer for this file
            if transfer_id not in self._active_writers:
                self._active_writers[transfer_id] = {}

            writers = self._active_writers[transfer_id]

            if file_index not in writers:
                # Need to get file info from registry
                transfer_info = self._registry.get_transfer(transfer_id)
                if not transfer_info:
                    logger.error(f"No transfer info for {transfer_id}")
                    return

                file_info = None
                for f in transfer_info.metadata.files:
                    if f.file_index == file_index:
                        file_info = f
                        break

                if not file_info:
                    logger.error(f"No file info for index {file_index}")
                    return

                # Create writer
                dest_dir = transfer_info.dest_dir or config.TEMP_DIR
                if file_info.relative_path and '/' in file_info.relative_path:
                    file_dest = dest_dir / file_info.relative_path
                else:
                    file_dest = dest_dir / file_info.name

                writers[file_index] = ChunkedFileWriter(
                    file_dest,
                    file_info.size,
                    file_info.checksum
                )

            writer = writers[file_index]

            # Write chunk
            if not writer.write_chunk(chunk_info.offset, chunk_data, chunk_info.checksum):
                logger.error(f"Failed to write chunk at offset {chunk_info.offset}")
                return

            # Send ACK
            ack_msg = MessageBuilder.build_file_chunk_ack(
                transfer_id,
                file_index,
                chunk_info.chunk_index,
                key
            )
            client_socket.sendall(ack_msg)

            # If last chunk, finalize
            if chunk_info.is_last:
                try:
                    final_path = writer.finalize()
                    self._registry.add_downloaded_file(transfer_id, final_path)
                    del writers[file_index]
                    logger.info(f"File complete: {final_path}")
                except Exception as e:
                    logger.error(f"Error finalizing file: {e}")

        except Exception as e:
            logger.error(f"Error handling file chunk: {e}")

    def _handle_transfer_complete(self, client_socket: socket.socket, payload: bytes, key: bytes = None):
        """Handle TRANSFER_COMPLETE"""
        try:
            info = MessageParser.parse_transfer_complete(payload)
            transfer_id = info['transfer_id']

            self._registry.complete_transfer(transfer_id)
            logger.info(f"Transfer completed by peer: {transfer_id}")

            # Clean up writers
            if transfer_id in self._active_writers:
                del self._active_writers[transfer_id]

        except Exception as e:
            logger.error(f"Error handling transfer complete: {e}")

    def _handle_transfer_cancel(self, payload: bytes):
        """Handle TRANSFER_CANCEL"""
        try:
            info = MessageParser.parse_transfer_cancel(payload)
            transfer_id = info['transfer_id']
            reason = info.get('reason', 'Unknown')

            self._registry.cancel_transfer(transfer_id, reason)
            logger.info(f"Transfer cancelled: {transfer_id} - {reason}")

            # Clean up writers
            if transfer_id in self._active_writers:
                for writer in self._active_writers[transfer_id].values():
                    writer.cleanup()
                del self._active_writers[transfer_id]

        except Exception as e:
            logger.error(f"Error handling transfer cancel: {e}")

    def _handle_transfer_error(self, payload: bytes):
        """Handle TRANSFER_ERROR"""
        try:
            info = MessageParser.parse_transfer_error(payload)
            transfer_id = info['transfer_id']
            error = info.get('error', 'Unknown error')

            self._registry.fail_transfer(transfer_id, error)
            logger.error(f"Transfer error: {transfer_id} - {error}")

            # Clean up writers
            if transfer_id in self._active_writers:
                for writer in self._active_writers[transfer_id].values():
                    writer.cleanup()
                del self._active_writers[transfer_id]

        except Exception as e:
            logger.error(f"Error handling transfer error: {e}")
