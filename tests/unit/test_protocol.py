"""
Unit tests for protocol.py - Message building and parsing
"""
import pytest
import json
import struct
import hashlib
from pathlib import Path

from yank.common.protocol import (
    MessageType, MessageBuilder, MessageParser, TransferMetadata, FileInfo,
    MessageFlags, MAX_MESSAGE_SIZE, MAX_BUFFER_SIZE, _safe_json_parse
)


class TestSafeJsonParse:
    """Tests for the _safe_json_parse function"""

    def test_valid_json(self):
        data = b'{"key": "value", "number": 42}'
        success, result = _safe_json_parse(data)
        assert success is True
        assert result == {"key": "value", "number": 42}

    def test_valid_json_with_expected_keys(self):
        data = b'{"name": "test", "size": 100}'
        success, result = _safe_json_parse(data, expected_keys=["name", "size"])
        assert success is True
        assert result["name"] == "test"

    def test_missing_expected_keys(self):
        data = b'{"name": "test"}'
        success, result = _safe_json_parse(data, expected_keys=["name", "size"])
        assert success is False
        assert "Missing keys" in result

    def test_invalid_json(self):
        data = b'{"invalid": json'
        success, result = _safe_json_parse(data)
        assert success is False
        assert "Invalid JSON" in result

    def test_invalid_utf8(self):
        data = b'\xff\xfe invalid utf8'
        success, result = _safe_json_parse(data)
        assert success is False
        assert "Invalid" in result


class TestMessageBuilder:
    """Tests for MessageBuilder"""

    def test_build_text_transfer(self, sample_text):
        msg = MessageBuilder.build_text_transfer(sample_text, key=None)
        assert msg is not None
        assert len(msg) > 4  # At least length header

        # Extract message type (5th byte, after 4-byte length)
        msg_type = msg[4]
        assert msg_type == MessageType.TEXT_TRANSFER

    def test_build_text_transfer_encrypted(self, sample_text, encryption_key):
        msg = MessageBuilder.build_text_transfer(sample_text, key=encryption_key)
        assert msg is not None

        # First byte after length should be encryption flag
        assert msg[4] == MessageFlags.ENCRYPTED

    def test_build_ping(self):
        msg = MessageBuilder.build_ping()
        assert msg is not None
        assert msg[4] == MessageType.PING

    def test_build_pong(self):
        msg = MessageBuilder.build_pong()
        assert msg is not None
        assert msg[4] == MessageType.PONG


class TestMessageParser:
    """Tests for MessageParser"""

    def test_parse_text_transfer(self, sample_text):
        msg = MessageBuilder.build_text_transfer(sample_text, key=None)
        # Extract payload (skip 4-byte length + 1-byte type)
        payload = msg[5:]

        parsed_text = MessageParser.parse_text_transfer(payload)
        assert parsed_text == sample_text

    def test_feed_and_parse(self, sample_text):
        parser = MessageParser()
        msg = MessageBuilder.build_text_transfer(sample_text, key=None)

        success = parser.feed(msg)
        assert success is True

        result = parser.parse_one()
        assert result is not None
        msg_type, payload = result
        assert msg_type == MessageType.TEXT_TRANSFER

    def test_partial_message(self, sample_text):
        parser = MessageParser()
        msg = MessageBuilder.build_text_transfer(sample_text, key=None)

        # Feed only half
        half = len(msg) // 2
        parser.feed(msg[:half])

        # Should return None (incomplete)
        result = parser.parse_one()
        assert result is None

        # Feed rest
        parser.feed(msg[half:])

        # Now should parse
        result = parser.parse_one()
        assert result is not None

    def test_buffer_overflow_protection(self):
        parser = MessageParser()

        # Try to feed more than MAX_BUFFER_SIZE
        huge_data = b"X" * (MAX_BUFFER_SIZE + 1)
        success = parser.feed(huge_data)
        assert success is False

    def test_message_too_large_rejection(self):
        parser = MessageParser()

        # Create a fake message header claiming huge size
        fake_header = struct.pack('>I', MAX_MESSAGE_SIZE + 1000)
        fake_header += bytes([MessageType.TEXT_TRANSFER])
        fake_header += b"X" * 100  # Some payload

        parser.feed(fake_header)
        result = parser.parse_one()

        # Should return error
        assert result is not None
        msg_type, payload = result
        assert msg_type == MessageType.ERROR

    def test_encrypted_message_without_key(self, sample_text, encryption_key):
        parser = MessageParser()  # No key set
        msg = MessageBuilder.build_text_transfer(sample_text, key=encryption_key)

        parser.feed(msg)
        result = parser.parse_one()

        # Should return error (no key to decrypt)
        assert result is not None
        msg_type, _ = result
        assert msg_type == MessageType.ERROR

    def test_encrypted_message_roundtrip(self, sample_text, encryption_key):
        parser = MessageParser(key=encryption_key)
        msg = MessageBuilder.build_text_transfer(sample_text, key=encryption_key)

        parser.feed(msg)
        result = parser.parse_one()

        assert result is not None
        msg_type, payload = result
        assert msg_type == MessageType.TEXT_TRANSFER

        parsed_text = MessageParser.parse_text_transfer(payload)
        assert parsed_text == sample_text


class TestProtocolConstants:
    """Test protocol constants and limits"""

    def test_max_message_size_reasonable(self):
        # Should be at least 10MB
        assert MAX_MESSAGE_SIZE >= 10 * 1024 * 1024

    def test_max_buffer_size_larger_than_message(self):
        # Buffer should be able to hold at least one max-size message
        assert MAX_BUFFER_SIZE >= MAX_MESSAGE_SIZE

    def test_message_type_values(self):
        # Verify message types are integers
        assert isinstance(MessageType.PING, int)
        assert isinstance(MessageType.PONG, int)
        assert isinstance(MessageType.TEXT_TRANSFER, int)
        assert isinstance(MessageType.FILE_TRANSFER, int)
