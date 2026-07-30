"""
Tests for encoding-safe console and log output (issue #12).

On Windows the background service's stdout/stderr are redirected to a file opened
with the locale codepage (cp1252 on most installs), and the logging FileHandler was
opened the same way. The app then writes arbitrary clipboard text and filenames to
those streams, so any emoji, CJK character or curly quote blew up the write.

Two separate failure modes are covered here:

* ``print()`` raises ``UnicodeEncodeError`` straight into its caller. Because the
  affected prints sit inside receive callbacks, that propagates into the protocol
  loop -- this is why the bug is not cosmetic.
* ``logging`` swallows the same error inside ``Handler.handleError`` but *drops the
  record* and dumps a traceback on stderr, so diagnostics are silently lost.

The fix is to keep every string literal that can reach an output stream pure ASCII,
and to open the log file as UTF-8 regardless of the machine's locale.
"""

import ast
import io
import logging
import sys
from pathlib import Path

import pytest

from yank import config
from yank.common.chunked_transfer import ProgressTracker

# Source tree under test. Resolved from the repo layout, falling back to the
# installed package location.
_SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "yank"
if not _SRC_ROOT.is_dir():  # pragma: no cover - only hit for non-editable installs
    import yank

    _SRC_ROOT = Path(yank.__file__).resolve().parent

# Methods whose arguments end up on an output stream.
_LOG_METHODS = frozenset(
    {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}
)


def _is_output_call(node):
    """True if ``node`` is a call that writes to a console or the log file.

    Matches ``print(...)``, ``<anything>.debug/info/warning/... (...)`` and
    ``<anything>.write(...)`` (covers ``sys.stdout.write``). Matching on the
    attribute name alone deliberately over-approximates: ``logger.info``,
    ``self.logger.info`` and ``logging.info`` all count, and a false positive
    only ever asks for one more ASCII string.
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "print"
    if isinstance(func, ast.Attribute):
        return func.attr in _LOG_METHODS or func.attr == "write"
    return False


def _non_ascii_string_literals(tree):
    """Yield (lineno, text) for non-ASCII str constants inside output calls.

    Walking the AST rather than grepping the file is the whole point: comments
    and docstrings are *allowed* to contain non-ASCII (source is decoded as
    UTF-8 regardless, and neither is ever written to a stream), so a plain text
    search would flag the box-drawing diagram in ``common/protocol.py`` and the
    ``--`` section rules in the service modules. Comments never appear in the
    AST at all, and a docstring is a bare ``Expr`` rather than a ``Call``
    argument, so both drop out naturally.

    f-strings are covered too: ``JoinedStr`` holds its literal chunks as plain
    ``Constant`` children, which ``ast.walk`` reaches.

    Known blind spot: a literal bound to a name first (``msg = "⚠ x"`` then
    ``print(msg)``) is not detected, since only constants lexically inside the
    call are inspected. No such indirection exists in the package today, and
    catching it would need dataflow analysis; the runtime cp1252 assertions in
    TestProgressBarIsAscii cover the rendered output for the one place that
    builds its string in a helper.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_output_call(node):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                if not child.value.isascii():
                    yield getattr(child, "lineno", node.lineno), child.value


def _python_sources():
    return sorted(p for p in _SRC_ROOT.rglob("*.py"))


class TestNoNonAsciiInOutputStrings:
    """Regression guard: non-ASCII must not creep back into printed/logged text."""

    def test_source_tree_is_scannable(self):
        """Sanity-check the scanner is actually pointed at the package."""
        sources = _python_sources()
        assert sources, f"no Python sources found under {_SRC_ROOT}"
        assert (_SRC_ROOT / "main.py") in sources

    def test_no_non_ascii_in_printed_or_logged_literals(self):
        offenders = []
        for path in _python_sources():
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for lineno, text in _non_ascii_string_literals(tree):
                bad = sorted({c for c in text if not c.isascii()})
                rendered = ", ".join(f"U+{ord(c):04X} {c!r}" for c in bad)
                offenders.append(f"{path.relative_to(_SRC_ROOT.parent)}:{lineno}: {rendered}")

        assert not offenders, (
            "Non-ASCII characters found in strings that reach an output stream.\n"
            "These raise UnicodeEncodeError on a cp1252 Windows console (see #12).\n"
            "Use the ASCII markers the rest of the CLI uses ('>>', 'X', '!', '[OK]').\n"
            + "\n".join(offenders)
        )

    def test_scanner_rejects_a_non_ascii_print(self):
        """The scanner must actually catch a bad literal (guards against a no-op test)."""
        tree = ast.parse('print("⚠ warning")\nlogger.info(f"café {x}")\n')
        found = list(_non_ascii_string_literals(tree))
        assert len(found) == 2

    def test_scanner_ignores_comments_and_docstrings(self):
        """Non-ASCII outside output calls is fine and must not be flagged."""
        source = (
            '"""Module docstring with a box: ┌─┐."""\n'
            "# section rule ─── and an em dash —\n"
            'BANNER = "█ not printed here"\n'
            'print("plain ascii")\n'
        )
        assert list(_non_ascii_string_literals(ast.parse(source))) == []


class TestProgressBarIsAscii:
    """The chunked-transfer progress bar used U+2588/U+2591 block characters."""

    @pytest.mark.parametrize("percent", [0, 1, 37, 99, 100])
    def test_progress_string_is_ascii(self, percent):
        tracker = ProgressTracker(total_bytes=1000)
        tracker.start("file.bin")
        tracker.update(percent * 10)

        rendered = tracker.get_progress_string()
        assert rendered.isascii(), f"non-ASCII in progress bar: {rendered!r}"

    def test_progress_bar_uses_hash_and_dash(self):
        """Must match the equivalent bar rendered in main.py's _on_transfer_progress."""
        tracker = ProgressTracker(total_bytes=100)
        tracker.start("file.bin")
        tracker.update(50)

        bar = tracker.get_progress_string().split("]")[0].split("[")[1]
        assert set(bar) <= {"#", "-"}, f"unexpected bar characters: {bar!r}"
        assert "#" in bar and "-" in bar

    def test_progress_string_survives_cp1252(self):
        """End-to-end: the rendered bar must encode on a cp1252 console."""
        tracker = ProgressTracker(total_bytes=100)
        tracker.start("file.bin")
        tracker.update(50)

        # Raises UnicodeEncodeError if the block characters ever come back.
        tracker.get_progress_string().encode("cp1252")


class TestLogFileHandlerEncoding:
    """The log FileHandler must be opened as UTF-8, not the locale codepage.

    Note on why these are mostly *source* assertions rather than runtime ones:
    ``logging.FileHandler`` with no ``encoding`` resolves it through
    ``io.text_encoding(None)``, i.e. to the locale encoding. macOS and modern
    Linux always report UTF-8, so ``handler.encoding`` reads back as ``'utf-8'``
    whether or not the argument was passed -- a runtime check cannot tell fixed
    from unfixed anywhere except an actual cp1252 Windows host. Asserting on the
    call site is therefore the only assertion that can fail in CI, so that is
    where the regression guard lives.
    """

    @staticmethod
    def _file_handler_calls():
        """Every ``logging.FileHandler(...)`` call site in main.py, as AST nodes."""
        source = (_SRC_ROOT / "main.py").read_text(encoding="utf-8")
        calls = []
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "FileHandler":
                calls.append(node)
        return source, calls

    def test_log_file_handler_is_constructed_with_utf8(self):
        """The actual regression guard: every FileHandler passes encoding='utf-8'."""
        _, calls = self._file_handler_calls()
        assert calls, "no logging.FileHandler call found in yank/main.py"

        for call in calls:
            kwargs = {kw.arg: kw for kw in call.keywords}
            assert "encoding" in kwargs, (
                f"FileHandler at main.py:{call.lineno} passes no encoding. On Windows "
                "that means the locale codepage (cp1252) and any emoji/CJK log record "
                "is dropped -- see #12."
            )
            value = kwargs["encoding"].value
            assert (
                isinstance(value, ast.Constant) and value.value == "utf-8"
            ), f"FileHandler at main.py:{call.lineno} must use encoding='utf-8'"

    def test_errors_kwarg_stays_behind_a_version_guard(self):
        """The 3.9+ 'errors' kwarg must not break Python 3.8.

        yank/main.py configures logging at import time, so an unsupported keyword
        would raise TypeError before the CLI can run at all on the declared floor.
        """
        source, calls = self._file_handler_calls()
        using_errors = [c.lineno for c in calls if any(kw.arg == "errors" for kw in c.keywords)]

        if using_errors:
            assert "sys.version_info >= (3, 9)" in source, (
                f"FileHandler(errors=...) at line(s) {using_errors} needs a "
                "sys.version_info >= (3, 9) guard; the kwarg does not exist on 3.8"
            )
            # ...and a plain 3.8-compatible fallback must exist alongside it.
            assert len(calls) >= 2, (
                "the version-guarded FileHandler needs a Python 3.8 fallback branch "
                "that omits 'errors'"
            )

    @staticmethod
    def _log_file_handlers():
        """Every FileHandler currently pointed at config.LOG_FILE.

        Importing yank.main is what configures logging; it is already imported by
        the time the suite runs, so inspect the live configuration rather than
        re-importing (which would re-run module-level platform setup).
        """
        import yank.main  # noqa: F401  - ensures logging.basicConfig has run

        target = str(Path(config.LOG_FILE).resolve())
        candidates = list(logging.getLogger().handlers)
        # pytest's logging plugin can swap out the root handlers, so also consult
        # the handler object main.py built.
        module_handler = getattr(yank.main, "_log_file_handler", None)
        if module_handler is not None:
            candidates.append(module_handler)

        return [
            h
            for h in candidates
            if isinstance(h, logging.FileHandler) and str(Path(h.baseFilename).resolve()) == target
        ]

    def test_log_file_handler_exists(self):
        assert self._log_file_handlers(), (
            f"no FileHandler configured for {config.LOG_FILE}; "
            "logging setup in yank/main.py may have changed"
        )

    def test_configured_handler_reports_utf8(self):
        """Smoke check on the live configuration.

        This cannot fail on a UTF-8-locale host even without the fix (see the
        class docstring); it exists to catch the handler being reconfigured to
        something actively wrong, e.g. an explicit non-UTF-8 encoding.
        """
        for handler in self._log_file_handlers():
            assert (
                handler.encoding == "utf-8"
            ), f"log FileHandler encoding is {handler.encoding!r}, expected 'utf-8'"
            if handler.stream is not None:
                assert handler.stream.encoding.lower().replace("-", "") == "utf8"

    @pytest.mark.skipif(
        sys.version_info < (3, 9),
        reason="FileHandler gained the 'errors' parameter in Python 3.9",
    )
    def test_configured_handler_replaces_undecodable_characters(self):
        """Lone surrogates from Windows filenames are rejected even by UTF-8."""
        for handler in self._log_file_handlers():
            assert handler.errors == "replace"

    def test_handler_kwargs_are_valid_on_this_interpreter(self, tmp_path):
        """Re-issue main.py's FileHandler call against a temp file.

        main.py builds the handler at import time, so an invalid keyword would be
        an immediate TypeError at startup rather than a deferred failure. Rebuild
        it here instead of reloading yank.main, which would re-run module-level
        platform setup and swap out class objects other tests hold references to.
        """
        log_path = tmp_path / "probe.log"
        if sys.version_info >= (3, 9):
            handler = logging.FileHandler(log_path, encoding="utf-8", errors="replace")
        else:  # pragma: no cover - only exercised on the 3.8 floor
            handler = logging.FileHandler(log_path, encoding="utf-8")
        try:
            assert handler.encoding == "utf-8"
        finally:
            handler.close()


class TestCp1252StreamRoundTrip:
    """Reproduce the underlying platform failure on any OS.

    io.TextIOWrapper with encoding='cp1252' behaves exactly like the redirected
    stdout/log file the Windows service opens, so these run on macOS and Linux.
    """

    # Content that cp1252 genuinely cannot represent. Note that curly quotes are
    # *not* in this list: U+201C/U+201D do exist in cp1252 (0x93/0x94). They still
    # break on other Windows codepages such as cp437, which is precisely why the
    # CLI's own markers must be ASCII rather than merely "cp1252-safe" -- see
    # test_curly_quotes_depend_on_the_active_codepage below.
    SAMPLES = [
        "emoji \U0001f600",
        "cjk 漢字",
        "warning sign ⚠",
        "block █░",
    ]

    @staticmethod
    def _stream(encoding, errors="strict"):
        return io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors=errors)

    @pytest.mark.parametrize("text", SAMPLES)
    def test_cp1252_stream_rejects_non_ascii(self, text):
        """Baseline: this is the exact production failure."""
        stream = self._stream("cp1252")
        with pytest.raises(UnicodeEncodeError):
            print(text, file=stream)
            stream.flush()

    @pytest.mark.parametrize("text", SAMPLES)
    def test_utf8_stream_accepts_non_ascii(self, text):
        """The configured encoding handles the same content fine."""
        stream = self._stream("utf-8")
        print(text, file=stream)
        stream.flush()

    def test_curly_quotes_depend_on_the_active_codepage(self):
        """Which characters survive depends on the machine's locale.

        cp1252 happens to include curly quotes, cp437 (still the default OEM
        console codepage in some locales) does not. Since the app cannot know
        the codepage of the console it is attached to, ASCII-only output is the
        only portable answer for CLI markers.
        """
        # TextIOWrapper encodes eagerly on write(), so these are real assertions.
        ok = self._stream("cp1252")
        ok.write("“hello”")  # fine here...
        ok.flush()

        bad = self._stream("cp437")
        with pytest.raises(UnicodeEncodeError):  # ...but not here
            bad.write("“hello”")
            bad.flush()

    def test_utf8_alone_still_rejects_lone_surrogates(self):
        """Why errors='replace' is warranted on top of encoding='utf-8'."""
        strict = self._stream("utf-8")
        with pytest.raises(UnicodeEncodeError):
            strict.write("windows \udcff filename")
            strict.flush()

        lenient = self._stream("utf-8", errors="replace")
        lenient.write("windows \udcff filename")
        lenient.flush()

    def test_file_handler_writes_non_ascii_record_through_configured_encoding(self, tmp_path):
        """A FileHandler built the way main.py builds it keeps the record.

        Without an explicit encoding this same record is silently dropped on a
        cp1252 machine: logging catches the UnicodeEncodeError in handleError,
        so nothing propagates but the diagnostic is lost.
        """
        log_path = tmp_path / "sync.log"
        handler = logging.FileHandler(log_path, encoding="utf-8")
        logger = logging.getLogger("yank.tests.encoding")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            logger.info("copied file \U0001f600 漢字.txt")
        finally:
            logger.removeHandler(handler)
            handler.close()

        contents = log_path.read_text(encoding="utf-8")
        assert "\U0001f600" in contents
        assert "漢字.txt" in contents

    def test_cp1252_file_handler_drops_the_record(self, tmp_path):
        """Documents the pre-fix behaviour the encoding argument prevents."""
        log_path = tmp_path / "sync-cp1252.log"
        handler = logging.FileHandler(log_path, encoding="cp1252")
        logger = logging.getLogger("yank.tests.encoding.cp1252")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

        previous = logging.raiseExceptions
        logging.raiseExceptions = False  # suppress the handleError traceback on stderr
        try:
            logger.info("copied file \U0001f600.txt")
        finally:
            logging.raiseExceptions = previous
            logger.removeHandler(handler)
            handler.close()

        assert log_path.read_text(encoding="cp1252") == ""
