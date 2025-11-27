"""
Core Sync Agent - handles network communication between peers

This module provides the networking layer for clipboard sync.
It runs a server to receive files and a client to send files.

Security:
- Requires device pairing before accepting connections
- All messages encrypted with AES-256-GCM after pairing
"""
import socket
import threading
import logging
import time
import os
import hashlib
from typing import Optional, Callable, List
from pathlib import Path

import config
from common.protocol import (
    MessageType,
    MessageBuilder,
    MessageParser,
    TransferMetadata,
    pack_files,
    unpack_files
)
from common.discovery import start_discovery, stop_discovery, get_discovery
from common.pairing import get_pairing_manager, is_paired, get_encryption_key

logger = logging.getLogger(__name__)


class SyncAgent:
    """
    Main sync agent that handles:
    - Running a server to receive clipboard files
    - Sending clipboard files to peers
    - Peer discovery (via mDNS)
    - Encryption and authentication
    """

    def __init__(self,
                 on_files_received: Optional[Callable[[List[Path]], None]] = None,
                 port: int = config.PORT,
                 require_pairing: bool = True):
        """
        Initialize the sync agent

        Args:
            on_files_received: Callback when files are received from peer
            port: Port to listen on
            require_pairing: If True, reject connections from unpaired devices
        """
        self.port = port
        self.on_files_received = on_files_received
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

        # Pairing manager
        self._pairing_manager = get_pairing_manager()
    
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
