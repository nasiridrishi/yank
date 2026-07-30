"""
Unit tests for SyncAgent._handle_message ACK framing.

Regression coverage for the double-ACK protocol desync: _handle_message used to
send a success ACK *before* invoking the clipboard callback and then a second
failure ACK from the except block if that callback raised. The peer received two
frames for one message; the extra frame stayed in its parse buffer and was
misread as the start of the next message, corrupting the whole connection.

These tests never touch the network. The agent is constructed directly (never via
start()) with the pairing manager and transfer manager patched out, and
_handle_message is driven with a mock socket.
"""
import json
import struct
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from yank import config
from yank.agent import SyncAgent
from yank.common.protocol import (
    FileInfo,
    MessageBuilder,
    MessageParser,
    MessageType,
    TransferMetadata,
    calculate_checksum_bytes,
)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def make_agent(**kwargs) -> SyncAgent:
    """
    Build a SyncAgent without any network, disk or singleton side effects.

    SyncAgent.__init__ only reaches out for the pairing manager and the global
    transfer manager, so patching those two is enough to keep construction inert.
    start() is deliberately never called - no socket is ever bound.
    """
    with patch("yank.agent.get_pairing_manager", return_value=MagicMock()), \
         patch("yank.agent.get_transfer_manager", return_value=MagicMock()):
        return SyncAgent(require_pairing=False, **kwargs)


def sent_bytes(sock: MagicMock) -> bytes:
    """Concatenate everything the handler wrote to the socket."""
    return b"".join(call.args[0] for call in sock.sendall.call_args_list)


def parse_all(data: bytes, key=None):
    """
    Feed bytes into a fresh MessageParser and drain it.

    Returns (messages, leftover_bytes). leftover_bytes is what a real peer would
    carry into its next read - it must be empty, otherwise the connection is
    desynced.
    """
    parser = MessageParser(key=key)
    parser.feed(data)
    messages = []
    while True:
        result = parser.parse_one()
        if result is None:
            break
        messages.append(result)
    return messages, bytes(parser.buffer)


def split_frame(frame: bytes, key=None):
    """Turn a full wire frame into the (msg_type, payload) pair _handle_message takes."""
    messages, leftover = parse_all(frame, key=key)
    assert len(messages) == 1
    assert leftover == b""
    return messages[0]


def build_file_transfer_message(payload_bytes: bytes = b"hello files", key=None):
    """Build a complete FILE_TRANSFER frame for a single in-memory file."""
    metadata = TransferMetadata(
        files=[
            FileInfo(
                name="note.txt",
                size=len(payload_bytes),
                checksum=calculate_checksum_bytes(payload_bytes),
                relative_path="note.txt",
                file_index=0,
            )
        ],
        total_size=len(payload_bytes),
        timestamp=0.0,
        source_os="macos",
    )
    return MessageBuilder.build_file_transfer(metadata, payload_bytes, key)


@pytest.fixture
def agent():
    a = make_agent()
    yield a
    a._registry.stop()  # cancel the registry's background cleanup timer


@pytest.fixture
def sock():
    return MagicMock()


@pytest.fixture(autouse=True)
def isolated_temp_dir(monkeypatch, temp_dir: Path):
    """Keep unpack_files out of the shared /tmp/clipboard-sync directory."""
    monkeypatch.setattr(config, "TEMP_DIR", temp_dir)
    return temp_dir


# --------------------------------------------------------------------------
# FILE_TRANSFER
# --------------------------------------------------------------------------

class TestFileTransferAck:

    def test_callback_raising_still_sends_exactly_one_frame(self, sock, temp_dir):
        """The regression: a throwing on_files_received must not trigger a second ACK."""
        callback = MagicMock(side_effect=RuntimeError("clipboard is on fire"))
        a = make_agent(on_files_received=callback)
        try:
            msg_type, payload = split_frame(build_file_transfer_message())
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert callback.call_count == 1
        assert sock.sendall.call_count == 1

        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.FILE_ACK
        # The bytes genuinely arrived and unpacked, so the ACK stays successful -
        # a local clipboard failure is not the sender's problem.
        assert MessageParser.parse_ack(messages[0][1])["success"] is True

    def test_callback_exception_is_logged(self, sock, caplog):
        callback = MagicMock(side_effect=RuntimeError("clipboard is on fire"))
        a = make_agent(on_files_received=callback)
        try:
            msg_type, payload = split_frame(build_file_transfer_message())
            with caplog.at_level("ERROR"):
                a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert "clipboard is on fire" in caplog.text

    def test_happy_path_sends_one_success_ack(self, sock, temp_dir):
        received = []
        a = make_agent(on_files_received=received.append)
        try:
            msg_type, payload = split_frame(build_file_transfer_message(b"payload data"))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.FILE_ACK
        assert MessageParser.parse_ack(messages[0][1])["success"] is True

        # And the files really landed on disk
        assert len(received) == 1
        assert [p.read_bytes() for p in received[0]] == [b"payload data"]

    def test_no_callback_registered_still_sends_one_ack(self, agent, sock):
        msg_type, payload = split_frame(build_file_transfer_message())
        agent._handle_message(sock, msg_type, payload)

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock))
        assert messages[0][0] == MessageType.FILE_ACK
        assert leftover == b""

    def test_malformed_payload_sends_one_failure_ack(self, sock):
        callback = MagicMock()
        a = make_agent(on_files_received=callback)
        try:
            # metadata length says 10 bytes, but the bytes are not JSON
            bad_payload = struct.pack(">I", 10) + b"not-json!!"
            a._handle_message(sock, MessageType.FILE_TRANSFER, bad_payload)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        callback.assert_not_called()

        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.FILE_ACK
        assert MessageParser.parse_ack(messages[0][1])["success"] is False

    def test_encrypted_ack_is_a_single_frame(self, sock, encryption_key):
        callback = MagicMock(side_effect=UnicodeEncodeError("charmap", "x", 0, 1, "boom"))
        a = make_agent(on_files_received=callback)
        try:
            frame = build_file_transfer_message(key=encryption_key)
            msg_type, payload = split_frame(frame, key=encryption_key)
            a._handle_message(sock, msg_type, payload, encryption_key)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock), key=encryption_key)
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.FILE_ACK


# --------------------------------------------------------------------------
# TEXT_TRANSFER
# --------------------------------------------------------------------------

class TestTextTransferAck:

    def test_callback_raising_still_sends_exactly_one_frame(self, sock):
        callback = MagicMock(side_effect=RuntimeError("clipboard is on fire"))
        a = make_agent(on_text_received=callback)
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer("hi"))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert callback.call_count == 1
        assert sock.sendall.call_count == 1

        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.TEXT_ACK
        assert MessageParser.parse_text_ack(messages[0][1])["success"] is True

    def test_unicode_encode_error_in_callback_sends_one_frame(self, sock):
        """
        The concrete Windows failure mode (issue #12): printing received text to a
        cp1252 console raises UnicodeEncodeError inside the callback.
        """
        callback = MagicMock(
            side_effect=UnicodeEncodeError("charmap", "\u2013", 0, 1, "undefined")
        )
        a = make_agent(on_text_received=callback)
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer("en dash \u2013"))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""

    def test_callback_exception_is_logged(self, sock, caplog):
        callback = MagicMock(side_effect=RuntimeError("clipboard is on fire"))
        a = make_agent(on_text_received=callback)
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer("hi"))
            with caplog.at_level("ERROR"):
                a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert "clipboard is on fire" in caplog.text

    def test_happy_path_sends_one_success_ack(self, sock, sample_text):
        received = []
        a = make_agent(on_text_received=received.append)
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer(sample_text))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert received == [sample_text]
        assert sock.sendall.call_count == 1

        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.TEXT_ACK
        assert MessageParser.parse_text_ack(messages[0][1])["success"] is True

    def test_empty_text_still_reaches_the_callback(self, sock):
        """Empty string is falsy but valid - the guard must be an is-None check."""
        received = []
        a = make_agent(on_text_received=received.append)
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer(""))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert received == [""]
        assert sock.sendall.call_count == 1

    def test_malformed_payload_sends_one_failure_ack(self, sock):
        callback = MagicMock()
        a = make_agent(on_text_received=callback)
        try:
            # length header says 4 bytes of text, but they are not valid UTF-8
            bad_payload = struct.pack(">I", 4) + b"\xff\xfe\xfd\xfc"
            a._handle_message(sock, MessageType.TEXT_TRANSFER, bad_payload)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        callback.assert_not_called()

        messages, leftover = parse_all(sent_bytes(sock))
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.TEXT_ACK
        assert MessageParser.parse_text_ack(messages[0][1])["success"] is False

    def test_encrypted_ack_is_a_single_frame(self, sock, encryption_key):
        callback = MagicMock(side_effect=RuntimeError("boom"))
        a = make_agent(on_text_received=callback)
        try:
            frame = MessageBuilder.build_text_transfer("secret", encryption_key)
            msg_type, payload = split_frame(frame, key=encryption_key)
            a._handle_message(sock, msg_type, payload, encryption_key)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock), key=encryption_key)
        assert len(messages) == 1
        assert leftover == b""
        assert messages[0][0] == MessageType.TEXT_ACK


# --------------------------------------------------------------------------
# The desync condition itself
# --------------------------------------------------------------------------

class TestPeerParserStaysInSync:
    """
    The actual failure the double ACK caused: leftover bytes in the peer's parse
    buffer get misread as the head of the next message. Simulate the peer by
    feeding the handler's output plus a following message into one parser.
    """

    # A follow-on frame the peer should read cleanly. TEXT_TRANSFER is used rather
    # than PING because MessageType.PING (0x01) collides with
    # MessageFlags.ENCRYPTED (0x01) in the wire format.
    NEXT_FRAME = MessageBuilder.build_text_transfer("next message")

    def test_peer_reads_next_message_correctly_after_failing_text_callback(self, sock):
        a = make_agent(on_text_received=MagicMock(side_effect=RuntimeError("boom")))
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer("hi"))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        wire = sent_bytes(sock) + self.NEXT_FRAME
        messages, leftover = parse_all(wire)

        assert [m[0] for m in messages] == [MessageType.TEXT_ACK, MessageType.TEXT_TRANSFER]
        assert MessageParser.parse_text_transfer(messages[1][1]) == "next message"
        assert leftover == b""

    def test_peer_reads_next_message_correctly_after_failing_file_callback(self, sock):
        a = make_agent(on_files_received=MagicMock(side_effect=RuntimeError("boom")))
        try:
            msg_type, payload = split_frame(build_file_transfer_message())
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        wire = sent_bytes(sock) + self.NEXT_FRAME
        messages, leftover = parse_all(wire)

        assert [m[0] for m in messages] == [MessageType.FILE_ACK, MessageType.TEXT_TRANSFER]
        assert MessageParser.parse_text_transfer(messages[1][1]) == "next message"
        assert leftover == b""

    def test_send_text_treats_the_single_ack_as_success(self, sock):
        """
        End-to-end shape check against the real sender loop's expectation:
        send_text() looks for one TEXT_ACK with success=True.
        """
        a = make_agent(on_text_received=MagicMock(side_effect=RuntimeError("boom")))
        try:
            msg_type, payload = split_frame(MessageBuilder.build_text_transfer("hi"))
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        parser = MessageParser()
        parser.feed(sent_bytes(sock))
        result = parser.parse_one()

        assert result is not None
        assert result[0] == MessageType.TEXT_ACK
        assert json.loads(result[1].decode("utf-8"))["success"] is True
        assert parser.parse_one() is None


# --------------------------------------------------------------------------
# Other _handle_message branches must not double-send either
# --------------------------------------------------------------------------

class TestOtherBranchesSingleFrame:

    def test_ping_sends_exactly_one_pong(self, agent, sock):
        agent._handle_message(sock, MessageType.PING, b"")

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock))
        assert [m[0] for m in messages] == [MessageType.PONG]
        assert leftover == b""

    def test_file_announce_sends_nothing(self, sock):
        announced = []
        a = make_agent(on_files_announced=lambda tid, md: announced.append(tid))
        try:
            metadata = TransferMetadata(
                files=[FileInfo(name="a.bin", size=1, checksum="x")],
                total_size=1,
                timestamp=0.0,
                source_os="macos",
                transfer_id="tid-1",
            )
            frame = MessageBuilder.build_file_announce(metadata)
            msg_type, payload = split_frame(frame)
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert announced == ["tid-1"]
        assert sock.sendall.call_count == 0

    def test_failing_announce_callback_sends_nothing(self, sock):
        a = make_agent(on_files_announced=MagicMock(side_effect=RuntimeError("boom")))
        try:
            metadata = TransferMetadata(
                files=[FileInfo(name="a.bin", size=1, checksum="x")],
                total_size=1,
                timestamp=0.0,
                source_os="macos",
                transfer_id="tid-2",
            )
            frame = MessageBuilder.build_file_announce(metadata)
            msg_type, payload = split_frame(frame)
            a._handle_message(sock, msg_type, payload)
        finally:
            a._registry.stop()

        assert sock.sendall.call_count == 0

    def test_unknown_file_request_sends_one_error_frame(self, agent, sock):
        frame = MessageBuilder.build_file_request("no-such-transfer", 0, 0)
        msg_type, payload = split_frame(frame)
        agent._handle_message(sock, msg_type, payload)

        assert sock.sendall.call_count == 1
        messages, leftover = parse_all(sent_bytes(sock))
        assert [m[0] for m in messages] == [MessageType.TRANSFER_ERROR]
        assert leftover == b""
