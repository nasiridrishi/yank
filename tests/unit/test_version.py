"""
Tests for the single source of truth for the project version.

``src/yank/__init__.py`` owns the version number.  ``pyproject.toml`` declares
``dynamic = ["version"]`` and points setuptools at that attribute, so the
distribution metadata, ``pip show``, and ``yank --version`` are all derived from
one literal and cannot drift apart (see issue #18).

Covers:
- ``yank.__version__`` is importable, non-empty and release-shaped
- ``pyproject.toml`` really is wired to the attribute and declares no static
  version of its own
- the build backend resolves that wiring to the same value
- installed distribution metadata agrees with the attribute
- the version still resolves when no distribution metadata exists at all
  (the frozen PyInstaller case)
- no other module in the package re-declares a version
"""

import importlib
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import pytest

import yank

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC_DIR = REPO_ROOT / "src" / "yank"

# The attribute pyproject is expected to read the version from.
VERSION_ATTR = "yank.__version__"


def _load_pyproject():
    """Parse pyproject.toml into a dict, or return None if no TOML parser.

    tomllib is stdlib on 3.11+; tomli is the 3.8-3.10 backport.  Neither is a
    declared dev dependency, so callers must handle None by scanning lines --
    the project floor is Python 3.8.
    """
    try:
        import tomllib  # type: ignore[import-not-found]
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            return None
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _section_lines(table):
    """Yield the raw lines belonging to one top-level TOML table.

    A deliberately small stand-in for a TOML parser, used only on interpreters
    where neither tomllib nor tomli is importable.
    """
    current = None
    for raw in PYPROJECT.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip()
            continue
        if current == table and line and not line.startswith("#"):
            yield line


def _project_name():
    """The distribution name from pyproject -- not the same as the import name."""
    data = _load_pyproject()
    if data is not None:
        return data["project"]["name"]
    for line in _section_lines("project"):  # pragma: no cover - no TOML parser
        match = re.match(r"""name\s*=\s*["'](.+?)["']""", line)
        if match:
            return match.group(1)
    raise AssertionError("pyproject [project] declares no name")  # pragma: no cover


DIST_NAME = _project_name()


class TestVersionAttribute:
    """The attribute itself must be usable by anything that imports yank."""

    def test_version_is_importable_and_non_empty(self):
        assert isinstance(yank.__version__, str)
        assert yank.__version__.strip()

    def test_version_is_release_shaped(self):
        # e.g. 1.0.3, 1.2, 2.0.0rc1 -- rejects placeholders like "unknown"/"0".
        assert re.match(
            r"^\d+\.\d+(\.\d+)?([._-]?[a-zA-Z0-9]+)*$", yank.__version__
        ), f"__version__ = {yank.__version__!r} does not look like a release version"

    def test_version_is_exported(self):
        assert "__version__" in yank.__all__


class TestPyprojectWiring:
    """pyproject.toml must derive its version from yank.__version__."""

    def test_version_is_declared_dynamic(self):
        data = _load_pyproject()
        if data is None:  # pragma: no cover - only without tomllib/tomli
            match = re.search(r"dynamic\s*=\s*\[([^\]]*)\]", " ".join(_section_lines("project")))
            assert match, "pyproject [project] declares no dynamic fields"
            assert "version" in match.group(1)
            return
        assert "version" in data["project"].get(
            "dynamic", []
        ), 'pyproject [project] must declare dynamic = ["version"]'

    def test_no_static_version_in_pyproject(self):
        """A static [project] version would immediately re-open issue #18."""
        data = _load_pyproject()
        if data is None:  # pragma: no cover - only without tomllib/tomli
            assert not [ln for ln in _section_lines("project") if re.match(r"version\s*=", ln)]
            return
        assert "version" not in data["project"], (
            "pyproject [project] declares a hardcoded version; it must stay dynamic "
            "so src/yank/__init__.py remains the single source of truth"
        )

    def test_dynamic_version_points_at_the_package_attribute(self):
        data = _load_pyproject()
        if data is None:  # pragma: no cover - only without tomllib/tomli
            assert VERSION_ATTR in " ".join(_section_lines("tool.setuptools.dynamic"))
            return
        assert data["tool"]["setuptools"]["dynamic"]["version"] == {"attr": VERSION_ATTR}


class TestBuildBackend:
    """The wiring is only real if the build backend resolves it."""

    def test_built_metadata_version_matches_attribute(self, tmp_path, monkeypatch):
        """Build the wheel metadata and check the version setuptools computed.

        This is what ends up in the sdist/wheel, `pip show`, the GitHub release
        artifacts, and any downstream packaging -- so it must equal the literal
        in src/yank/__init__.py.
        """
        build_meta = pytest.importorskip("setuptools.build_meta")

        monkeypatch.chdir(REPO_ROOT)
        out_dir = tmp_path / "metadata"
        out_dir.mkdir()
        dist_info = build_meta.prepare_metadata_for_build_wheel(str(out_dir))

        metadata = (out_dir / dist_info / "METADATA").read_text(encoding="utf-8")
        built_version = re.search(r"^Version:\s*(.+)$", metadata, re.M)
        built_name = re.search(r"^Name:\s*(.+)$", metadata, re.M)

        assert built_version, metadata
        assert built_version.group(1).strip() == yank.__version__, (
            f"setuptools built version {built_version.group(1)!r} but "
            f"yank.__version__ is {yank.__version__!r}"
        )
        assert built_name and built_name.group(1).strip() == DIST_NAME


class TestInstalledMetadata:
    """When the package is pip-installed, its metadata must match the attribute."""

    def test_distribution_metadata_matches_attribute(self):
        try:
            declared = importlib.metadata.version(DIST_NAME)
        except importlib.metadata.PackageNotFoundError:
            pytest.skip(f"{DIST_NAME} is not installed in this environment")
        assert declared == yank.__version__, (
            f"installed metadata says {declared!r} but yank.__version__ is "
            f"{yank.__version__!r}; reinstall the package after bumping the version"
        )

    def test_metadata_lookup_needs_the_distribution_name(self):
        """Guards the footgun that makes metadata lookups silently fall back.

        ``importlib.metadata.version("yank")`` never resolves for this project:
        the import name is ``yank`` but the distribution is ``DIST_NAME``.
        """
        if DIST_NAME.replace("-", "_").lower() == "yank":
            pytest.skip("distribution name now matches the import name")
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("yank")


class TestFrozenBuild:
    """PyInstaller binaries carry no distribution metadata at all."""

    def test_version_resolves_without_distribution_metadata(self, monkeypatch):
        expected = yank.__version__

        def _no_metadata(*args, **kwargs):
            raise importlib.metadata.PackageNotFoundError("frozen build: no metadata")

        monkeypatch.setattr(importlib.metadata, "version", _no_metadata)
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEItest", raising=False)

        try:
            reloaded = importlib.reload(yank)
            assert reloaded.__version__ == expected
            assert reloaded.__version__.strip()
        finally:
            monkeypatch.undo()
            importlib.reload(yank)

    def test_importing_yank_never_needs_metadata(self, monkeypatch):
        """Importing the package must not raise just because metadata is gone."""

        def _explode(*args, **kwargs):
            raise AssertionError("yank/__init__.py must not query package metadata")

        monkeypatch.setattr(importlib.metadata, "version", _explode)
        monkeypatch.setattr(importlib.metadata, "distribution", _explode)
        try:
            importlib.reload(yank)
        finally:
            monkeypatch.undo()
            importlib.reload(yank)


class TestNoDuplicateVersions:
    """Nothing else in the package may declare a version of its own."""

    def test_only_init_declares_a_version(self):
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in SRC_DIR.rglob("*.py")
            if path.name != "__init__.py"
            and re.search(r"^__version__\s*=", path.read_text(encoding="utf-8"), re.M)
        ]
        assert not offenders, f"version declared outside src/yank/__init__.py: {offenders}"

    def test_no_module_hardcodes_the_current_version(self):
        literal = re.compile(r"""["']""" + re.escape(yank.__version__) + r"""["']""")
        offenders = [
            str(path.relative_to(REPO_ROOT))
            for path in SRC_DIR.rglob("*.py")
            if path != SRC_DIR / "__init__.py" and literal.search(path.read_text(encoding="utf-8"))
        ]
        assert not offenders, f"version string hardcoded outside __init__.py: {offenders}"


class TestCommandLine:
    """`yank --version` is what users and release automation actually see."""

    def test_cli_reports_the_package_version(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; sys.argv = ['yank', '--version']; "
                "from yank.main import main; main()",
            ],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout + result.stderr
        assert result.returncode == 0, output
        assert yank.__version__ in output, output
