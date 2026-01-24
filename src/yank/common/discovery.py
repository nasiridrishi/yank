"""
Peer discovery using mDNS/Zeroconf (Bonjour)

Allows automatic discovery of clipboard-sync peers on the LAN
"""
import os
import socket
import logging
import threading
import time
from typing import Optional, Callable, Dict, Tuple
from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf, ServiceStateChange

from yank import config

logger = logging.getLogger(__name__)

# Environment variable for manual peer IP fallback
PEER_IP_ENV_VAR = "YANK_PEER_IP"

# Peer cache TTL in seconds
PEER_CACHE_TTL = 60.0


class PeerDiscovery:
    """
    Handles peer discovery and advertisement using mDNS
    """
    
    def __init__(self, on_peer_found: Optional[Callable[[str, int], None]] = None,
                 on_peer_lost: Optional[Callable[[str], None]] = None):
        """
        Initialize peer discovery

        Args:
            on_peer_found: Callback when a peer is discovered (ip, port)
            on_peer_lost: Callback when a peer disappears (name)
        """
        self.zeroconf: Optional[Zeroconf] = None
        self.browser: Optional[ServiceBrowser] = None
        self.service_info: Optional[ServiceInfo] = None
        self.on_peer_found = on_peer_found
        self.on_peer_lost = on_peer_lost
        self.discovered_peers: Dict[str, tuple] = {}  # name -> (ip, port)
        self._peer_timestamps: Dict[str, float] = {}  # name -> discovery time (for TTL)
        self._running = False
        self._lock = threading.Lock()
        self._peer_found_event = threading.Event()  # For blocking get_first_peer
    
    def _get_local_ip(self) -> str:
        """Get the local IP address"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Doesn't actually connect, just determines the local interface
            s.connect(('8.8.8.8', 80))
            return s.getsockname()[0]
        except Exception:
            return '127.0.0.1'
        finally:
            s.close()
    
    def _on_service_state_change(self, zeroconf: Zeroconf, service_type: str,
                                  name: str, state_change: ServiceStateChange):
        """Handle service state changes"""
        if state_change == ServiceStateChange.Added:
            info = zeroconf.get_service_info(service_type, name)
            if info:
                self._handle_service_found(name, info)
        elif state_change == ServiceStateChange.Removed:
            self._handle_service_lost(name)
    
    def _handle_service_found(self, name: str, info: ServiceInfo):
        """Handle a discovered peer"""
        if not info.addresses:
            return

        # Get IP and port
        ip = socket.inet_ntoa(info.addresses[0])
        port = info.port

        # Skip if it's us
        local_ip = self._get_local_ip()
        if ip == local_ip:
            return

        with self._lock:
            is_new = name not in self.discovered_peers
            self.discovered_peers[name] = (ip, port)
            self._peer_timestamps[name] = time.time()

            if is_new:
                logger.info(f"Discovered peer: {name} at {ip}:{port}")
                self._peer_found_event.set()  # Signal waiting threads

                if self.on_peer_found:
                    self.on_peer_found(ip, port)
    
    def _handle_service_lost(self, name: str):
        """Handle a lost peer"""
        with self._lock:
            if name in self.discovered_peers:
                del self.discovered_peers[name]
                logger.info(f"Lost peer: {name}")
                
                if self.on_peer_lost:
                    self.on_peer_lost(name)
    
    def start(self, port: int = config.PORT):
        """
        Start peer discovery and advertisement
        
        Args:
            port: Port the service is running on
        """
        if self._running:
            return
        
        self.zeroconf = Zeroconf()
        
        # Register our service
        local_ip = self._get_local_ip()
        hostname = socket.gethostname()
        service_name = f"clipboard-sync-{hostname}.{config.SERVICE_NAME}"
        
        self.service_info = ServiceInfo(
            config.SERVICE_NAME,
            service_name,
            addresses=[socket.inet_aton(local_ip)],
            port=port,
            properties={'version': '1.0'},
        )
        
        try:
            self.zeroconf.register_service(self.service_info)
            logger.info(f"Registered service: {service_name} at {local_ip}:{port}")
        except Exception as e:
            logger.error(f"Failed to register service: {e}")
        
        # Browse for other peers
        self.browser = ServiceBrowser(
            self.zeroconf,
            config.SERVICE_NAME,
            handlers=[self._on_service_state_change]
        )
        
        self._running = True
        logger.info("Peer discovery started")
    
    def stop(self):
        """Stop peer discovery"""
        if not self._running:
            return
        
        if self.service_info and self.zeroconf:
            self.zeroconf.unregister_service(self.service_info)
        
        if self.browser:
            self.browser.cancel()
        
        if self.zeroconf:
            self.zeroconf.close()
        
        self._running = False
        logger.info("Peer discovery stopped")
    
    def get_peers(self) -> Dict[str, tuple]:
        """Get currently discovered peers (with cache cleanup)"""
        self._cleanup_stale_peers()
        with self._lock:
            return dict(self.discovered_peers)

    def _cleanup_stale_peers(self):
        """Remove peers that haven't been seen recently"""
        now = time.time()
        with self._lock:
            stale = [
                name for name, ts in self._peer_timestamps.items()
                if now - ts > PEER_CACHE_TTL
            ]
            for name in stale:
                if name in self.discovered_peers:
                    del self.discovered_peers[name]
                del self._peer_timestamps[name]
                logger.debug(f"Expired stale peer: {name}")

    def get_first_peer(self, timeout: float = 5.0) -> Optional[Tuple[str, int]]:
        """
        Get the first discovered peer (ip, port).

        Args:
            timeout: Maximum seconds to wait for a peer (default 5s).
                     Set to 0 for non-blocking check.

        Returns:
            Tuple of (ip, port) or None if no peer found
        """
        # First check for manual override via environment variable
        manual_peer = self._get_manual_peer()
        if manual_peer:
            return manual_peer

        # Clean up stale peers
        self._cleanup_stale_peers()

        # Check existing peers
        with self._lock:
            if self.discovered_peers:
                return list(self.discovered_peers.values())[0]

        # If no timeout, return immediately
        if timeout <= 0:
            return None

        # Wait for a peer to be discovered
        self._peer_found_event.clear()
        if self._peer_found_event.wait(timeout=timeout):
            with self._lock:
                if self.discovered_peers:
                    return list(self.discovered_peers.values())[0]

        logger.debug(f"No peer found within {timeout}s timeout")
        return None

    def _get_manual_peer(self) -> Optional[Tuple[str, int]]:
        """
        Get peer from YANK_PEER_IP environment variable.

        Format: IP or IP:PORT (default port is config.PORT)
        """
        peer_ip = os.environ.get(PEER_IP_ENV_VAR)
        if not peer_ip:
            return None

        try:
            if ':' in peer_ip:
                ip, port_str = peer_ip.rsplit(':', 1)
                port = int(port_str)
            else:
                ip = peer_ip
                port = config.PORT

            # Basic validation
            socket.inet_aton(ip)  # Validates IP format
            logger.info(f"Using manual peer from {PEER_IP_ENV_VAR}: {ip}:{port}")
            return (ip, port)
        except (ValueError, socket.error) as e:
            logger.warning(f"Invalid {PEER_IP_ENV_VAR} value '{peer_ip}': {e}")
            return None


# Singleton instance for easy access
_discovery: Optional[PeerDiscovery] = None


def get_discovery() -> PeerDiscovery:
    """Get the singleton discovery instance"""
    global _discovery
    if _discovery is None:
        _discovery = PeerDiscovery()
    return _discovery


def start_discovery(port: int = config.PORT,
                   on_peer_found: Optional[Callable[[str, int], None]] = None,
                   on_peer_lost: Optional[Callable[[str], None]] = None) -> PeerDiscovery:
    """
    Convenience function to start peer discovery
    
    Returns: PeerDiscovery instance
    """
    discovery = get_discovery()
    discovery.on_peer_found = on_peer_found
    discovery.on_peer_lost = on_peer_lost
    discovery.start(port)
    return discovery


def stop_discovery():
    """Stop peer discovery"""
    global _discovery
    if _discovery:
        _discovery.stop()
        _discovery = None
