"""
Tests for signal handler registration in the foreground agent.

Covers:
- SIGINT/SIGTERM always registered
- SIGBREAK registered only when the attribute exists (Windows-only), so
  'yank stop' sending CTRL_BREAK_EVENT runs cleanup instead of hard-killing
- The registered handler releases the singleton lock/PID pair
"""
import signal
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from yank import main as yank_main


@pytest.fixture
def foreground_args():
    return SimpleNamespace(port=9876, peer=None, no_security=False, verbose=False)


@pytest.fixture
def patched_foreground():
    """Stub out everything _run_foreground touches except signal registration."""
    app = MagicMock()
    pairing = MagicMock()
    pairing.is_paired.return_value = True

    with patch("yank.main.ensure_single_instance", return_value=True), \
            patch("yank.main.get_pairing_manager", return_value=pairing), \
            patch("yank.main.ClipboardSync", return_value=app), \
            patch("yank.main.release_singleton") as mock_release, \
            patch("yank.main.signal.signal") as mock_signal:
        yield SimpleNamespace(app=app, release=mock_release, signal=mock_signal)


def _registered(mock_signal):
    """Map of signal number -> handler passed to signal.signal()."""
    return {call.args[0]: call.args[1] for call in mock_signal.call_args_list}


class TestForegroundSignalRegistration:

    def test_sigint_and_sigterm_always_registered(self, patched_foreground, foreground_args):
        yank_main._run_foreground(foreground_args)

        handlers = _registered(patched_foreground.signal)
        assert signal.SIGINT in handlers
        assert signal.SIGTERM in handlers

    def test_sigbreak_registered_when_attribute_exists(
        self, patched_foreground, foreground_args, monkeypatch
    ):
        # SIGBREAK is Windows-only; simulate it so this runs on any host.
        monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)

        yank_main._run_foreground(foreground_args)

        handlers = _registered(patched_foreground.signal)
        assert signal.SIGBREAK in handlers
        # CTRL_BREAK_EVENT must run the same graceful shutdown as Ctrl-C.
        assert handlers[signal.SIGBREAK] is handlers[signal.SIGINT]

    def test_sigbreak_not_registered_when_attribute_missing(
        self, patched_foreground, foreground_args, monkeypatch
    ):
        monkeypatch.delattr(signal, "SIGBREAK", raising=False)

        yank_main._run_foreground(foreground_args)

        # Only SIGINT and SIGTERM on non-Windows hosts.
        assert set(_registered(patched_foreground.signal)) == {
            signal.SIGINT, signal.SIGTERM
        }

    def test_sigbreak_handler_stops_app_and_releases_singleton(
        self, patched_foreground, foreground_args, monkeypatch
    ):
        monkeypatch.setattr(signal, "SIGBREAK", 21, raising=False)

        yank_main._run_foreground(foreground_args)
        handler = _registered(patched_foreground.signal)[signal.SIGBREAK]

        patched_foreground.app.stop.reset_mock()
        patched_foreground.release.reset_mock()

        with pytest.raises(SystemExit) as exc:
            handler(signal.SIGBREAK, None)

        assert exc.value.code == 0
        patched_foreground.app.stop.assert_called_once()
        patched_foreground.release.assert_called_once()
