"""
User-Friendly Error Messages

Provides clear, actionable error messages for common Yank errors.
Each error includes a description and a suggestion for how to resolve it.
"""
from enum import Enum
from dataclasses import dataclass
from typing import Optional


@dataclass
class YankError:
    """User-friendly error with message and suggestion"""
    message: str
    suggestion: str
    code: str = ""

    def __str__(self) -> str:
        result = f"Error: {self.message}"
        if self.suggestion:
            result += f"\n  Suggestion: {self.suggestion}"
        return result

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "suggestion": self.suggestion
        }


class ErrorCode(Enum):
    """Error codes for categorization"""
    # Network errors
    PEER_OFFLINE = "peer_offline"
    CONNECTION_REFUSED = "connection_refused"
    CONNECTION_TIMEOUT = "connection_timeout"
    NETWORK_UNREACHABLE = "network_unreachable"

    # Authentication errors
    AUTH_FAILED = "auth_failed"
    NOT_PAIRED = "not_paired"
    PAIRING_EXPIRED = "pairing_expired"

    # Transfer errors
    CHECKSUM_MISMATCH = "checksum_mismatch"
    TRANSFER_TIMEOUT = "transfer_timeout"
    TRANSFER_CANCELLED = "transfer_cancelled"
    FILE_NOT_FOUND = "file_not_found"
    FILE_TOO_LARGE = "file_too_large"

    # Resource errors
    DISK_FULL = "disk_full"
    PERMISSION_DENIED = "permission_denied"
    PORT_IN_USE = "port_in_use"

    # Configuration errors
    INVALID_CONFIG = "invalid_config"
    MISSING_DEPENDENCY = "missing_dependency"

    # Protocol errors
    PROTOCOL_ERROR = "protocol_error"
    MESSAGE_TOO_LARGE = "message_too_large"
    BUFFER_OVERFLOW = "buffer_overflow"

    # General
    UNKNOWN = "unknown"


# Error messages with user-friendly suggestions
ERROR_MESSAGES = {
    ErrorCode.PEER_OFFLINE: YankError(
        code="peer_offline",
        message="Peer device is not responding",
        suggestion="Make sure Yank is running on the other device and both devices are on the same network"
    ),

    ErrorCode.CONNECTION_REFUSED: YankError(
        code="connection_refused",
        message="Connection was refused by the peer",
        suggestion="Check if Yank is running on the peer device. You may need to re-pair the devices"
    ),

    ErrorCode.CONNECTION_TIMEOUT: YankError(
        code="connection_timeout",
        message="Connection timed out while trying to reach peer",
        suggestion="Check your network connection and firewall settings (port 9876 must be open)"
    ),

    ErrorCode.NETWORK_UNREACHABLE: YankError(
        code="network_unreachable",
        message="Cannot reach the peer device on the network",
        suggestion="Verify both devices are connected to the same local network"
    ),

    ErrorCode.AUTH_FAILED: YankError(
        code="auth_failed",
        message="Authentication with peer failed",
        suggestion="Try re-pairing the devices using './run.sh pair' and './run.sh join'"
    ),

    ErrorCode.NOT_PAIRED: YankError(
        code="not_paired",
        message="Not paired with a peer device",
        suggestion="Pair with another device first: run './run.sh pair' on one device, then './run.sh join <IP> <PIN>' on the other"
    ),

    ErrorCode.PAIRING_EXPIRED: YankError(
        code="pairing_expired",
        message="Pairing information has expired or is invalid",
        suggestion="Re-pair the devices using './run.sh unpair' followed by './run.sh pair'"
    ),

    ErrorCode.CHECKSUM_MISMATCH: YankError(
        code="checksum_mismatch",
        message="File integrity check failed - data may be corrupted",
        suggestion="Try copying the file again. If the problem persists, check your network connection"
    ),

    ErrorCode.TRANSFER_TIMEOUT: YankError(
        code="transfer_timeout",
        message="File transfer timed out",
        suggestion="Check your network connection. For large files, ensure the connection is stable"
    ),

    ErrorCode.TRANSFER_CANCELLED: YankError(
        code="transfer_cancelled",
        message="File transfer was cancelled",
        suggestion="Copy the files again to restart the transfer"
    ),

    ErrorCode.FILE_NOT_FOUND: YankError(
        code="file_not_found",
        message="The requested file was not found",
        suggestion="The file may have been moved or deleted. Try copying it again"
    ),

    ErrorCode.FILE_TOO_LARGE: YankError(
        code="file_too_large",
        message="File exceeds the maximum allowed size",
        suggestion="Adjust the max file size in config: './run.sh config --set max_file_size <MB>'"
    ),

    ErrorCode.DISK_FULL: YankError(
        code="disk_full",
        message="Not enough disk space to receive the file",
        suggestion="Free up disk space and try again"
    ),

    ErrorCode.PERMISSION_DENIED: YankError(
        code="permission_denied",
        message="Permission denied when accessing file or directory",
        suggestion="Check file permissions. On macOS, you may need to grant Full Disk Access in System Preferences"
    ),

    ErrorCode.PORT_IN_USE: YankError(
        code="port_in_use",
        message="Port 9876 is already in use",
        suggestion="Another instance of Yank may be running. Use './run.sh status' to check"
    ),

    ErrorCode.INVALID_CONFIG: YankError(
        code="invalid_config",
        message="Configuration file contains invalid values",
        suggestion="Run './run.sh config --reset' to restore default settings"
    ),

    ErrorCode.MISSING_DEPENDENCY: YankError(
        code="missing_dependency",
        message="A required dependency is not installed",
        suggestion="Run the setup script: './setup.sh' (macOS/Linux) or '.\\setup.ps1' (Windows)"
    ),

    ErrorCode.PROTOCOL_ERROR: YankError(
        code="protocol_error",
        message="Communication protocol error with peer",
        suggestion="Ensure both devices are running the same version of Yank"
    ),

    ErrorCode.MESSAGE_TOO_LARGE: YankError(
        code="message_too_large",
        message="Received message exceeds maximum allowed size",
        suggestion="This may indicate a protocol mismatch or corrupted data. Restart both Yank instances"
    ),

    ErrorCode.BUFFER_OVERFLOW: YankError(
        code="buffer_overflow",
        message="Network buffer overflow - too much data pending",
        suggestion="Check your network connection. The peer may be sending data too fast"
    ),

    ErrorCode.UNKNOWN: YankError(
        code="unknown",
        message="An unexpected error occurred",
        suggestion="Check the logs at logs/clipboard-sync.log for more details"
    ),
}


def get_error(code: ErrorCode) -> YankError:
    """Get user-friendly error for a given error code"""
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES[ErrorCode.UNKNOWN])


def get_error_from_exception(exc: Exception) -> YankError:
    """Map common exceptions to user-friendly errors"""
    exc_str = str(exc).lower()
    exc_type = type(exc).__name__

    # Connection errors
    if "connection refused" in exc_str:
        return get_error(ErrorCode.CONNECTION_REFUSED)
    if "timed out" in exc_str or "timeout" in exc_str:
        return get_error(ErrorCode.CONNECTION_TIMEOUT)
    if "network is unreachable" in exc_str:
        return get_error(ErrorCode.NETWORK_UNREACHABLE)

    # File errors
    if "no such file" in exc_str or "not found" in exc_str:
        return get_error(ErrorCode.FILE_NOT_FOUND)
    if "permission denied" in exc_str:
        return get_error(ErrorCode.PERMISSION_DENIED)
    if "no space left" in exc_str or "disk full" in exc_str:
        return get_error(ErrorCode.DISK_FULL)

    # Socket errors
    if "address already in use" in exc_str:
        return get_error(ErrorCode.PORT_IN_USE)

    # Authentication
    if "auth" in exc_str and ("fail" in exc_str or "invalid" in exc_str):
        return get_error(ErrorCode.AUTH_FAILED)

    # Checksum
    if "checksum" in exc_str or "integrity" in exc_str:
        return get_error(ErrorCode.CHECKSUM_MISMATCH)

    # Default
    error = get_error(ErrorCode.UNKNOWN)
    # Include original exception message for debugging
    return YankError(
        code=error.code,
        message=f"{error.message}: {exc_type}",
        suggestion=error.suggestion
    )


def format_error(code: ErrorCode, details: Optional[str] = None) -> str:
    """Format error message for display"""
    error = get_error(code)
    result = str(error)
    if details:
        result = f"{result}\n  Details: {details}"
    return result
