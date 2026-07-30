"""
Yank - LAN Clipboard Sync

Cross-platform clipboard synchronization for Windows, macOS, and Linux
"""

# ---------------------------------------------------------------------------
# SINGLE SOURCE OF TRUTH FOR THE PROJECT VERSION.
#
# pyproject.toml declares ``dynamic = ["version"]`` and reads this attribute at
# build time (``[tool.setuptools.dynamic] version = {attr = "yank.__version__"}``),
# so the sdist/wheel metadata, ``pip show``, and ``yank --version`` can never
# disagree.  Bump it here and nowhere else; release tags (``vX.Y.Z``) should
# match, since the release workflow derives package versions from the tag name.
#
# Deliberately a plain literal rather than an ``importlib.metadata`` lookup:
# Yank ships as a PyInstaller binary that carries no distribution metadata, so a
# metadata lookup would fall back on every released build (and the import name
# ``yank`` differs from the distribution name ``yank-clipboard-sync``, which
# makes such lookups easy to get silently wrong).
# ---------------------------------------------------------------------------
__version__ = "1.0.3"

__author__ = "Nasir Idrishi"
__license__ = "MIT"

__all__ = ['__version__']
