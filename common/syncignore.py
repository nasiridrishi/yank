"""
.syncignore - Gitignore-style file filtering

Allows users to specify patterns for files/folders to ignore when syncing.
Supports standard gitignore patterns:
- Glob patterns: *.txt, *.exe
- Directory patterns: node_modules/, __pycache__/
- Negation: !important.txt
- Comments: # this is a comment
"""
import fnmatch
import logging
import re
from pathlib import Path
from typing import List, Set, Optional

logger = logging.getLogger(__name__)

# Default syncignore file location
SYNCIGNORE_FILE = Path(__file__).parent.parent / ".syncignore"

# Default patterns to ignore
DEFAULT_PATTERNS = """# Clipboard Sync Ignore File
# Add patterns here to exclude files from syncing
# Uses gitignore-style patterns

# System files
.DS_Store
Thumbs.db
desktop.ini
*.lnk

# Temporary files
*.tmp
*.temp
*.bak
*.swp
*.swo
*~
~$*

# Build artifacts
*.exe
*.dll
*.so
*.dylib
*.o
*.obj
*.class
*.pyc
*.pyo
__pycache__/

# IDE/Editor files
.idea/
.vscode/
*.sublime-*
.project
.settings/

# Version control
.git/
.svn/
.hg/

# Node modules (too large)
node_modules/

# Virtual environments
venv/
.venv/
env/
.env/

# Large media (uncomment if needed)
# *.mp4
# *.mov
# *.avi
# *.mkv

# Archives
# *.zip
# *.tar
# *.gz
# *.rar
# *.7z
"""


class SyncIgnore:
    """Handles .syncignore pattern matching"""

    _instance: Optional['SyncIgnore'] = None
    _patterns: List[str] = []
    _negations: List[str] = []
    _last_mtime: float = 0

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._patterns = []
            cls._instance._negations = []
            cls._instance._last_mtime = 0
        return cls._instance

    def __init__(self):
        self.load()

    def load(self, path: Path = None) -> None:
        """Load patterns from .syncignore file"""
        filepath = path or SYNCIGNORE_FILE

        # Check if file changed
        if filepath.exists():
            mtime = filepath.stat().st_mtime
            if mtime == self._last_mtime:
                return  # No change
            self._last_mtime = mtime

        self._patterns = []
        self._negations = []

        if not filepath.exists():
            # Create default file
            self._create_default(filepath)
            return

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()

                    # Skip empty lines and comments
                    if not line or line.startswith('#'):
                        continue

                    # Handle negation
                    if line.startswith('!'):
                        pattern = line[1:].strip()
                        if pattern:
                            self._negations.append(pattern)
                    else:
                        self._patterns.append(line)

            logger.info(f"Loaded {len(self._patterns)} ignore patterns, {len(self._negations)} negations")

        except Exception as e:
            logger.error(f"Failed to load .syncignore: {e}")

    def _create_default(self, path: Path) -> None:
        """Create default .syncignore file"""
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(DEFAULT_PATTERNS)
            logger.info(f"Created default .syncignore at {path}")
            self.load(path)
        except Exception as e:
            logger.error(f"Failed to create .syncignore: {e}")

    def should_ignore(self, filepath: Path) -> bool:
        """
        Check if a file should be ignored

        Args:
            filepath: Path to check (can be absolute or relative)

        Returns:
            True if file should be ignored, False otherwise
        """
        # Reload if file changed
        self.load()

        # Get filename and convert to string for matching
        filename = filepath.name
        filepath_str = str(filepath)

        # Check negations first (they override ignores)
        for pattern in self._negations:
            if self._match_pattern(filepath, pattern):
                return False

        # Check ignore patterns
        for pattern in self._patterns:
            if self._match_pattern(filepath, pattern):
                logger.debug(f"Ignoring {filename} (matches pattern: {pattern})")
                return True

        return False

    def _match_pattern(self, filepath: Path, pattern: str) -> bool:
        """
        Check if a filepath matches a pattern

        Supports:
        - Simple glob: *.txt
        - Directory glob: dir/
        - Path glob: dir/*.txt
        - Double star: **/*.txt
        """
        filename = filepath.name
        filepath_str = str(filepath).replace('\\', '/')

        # Directory pattern (ends with /)
        if pattern.endswith('/'):
            dir_pattern = pattern[:-1]
            # Check if any parent directory matches
            for parent in filepath.parents:
                if fnmatch.fnmatch(parent.name, dir_pattern):
                    return True
            # Also check if the path contains the directory
            if f"/{dir_pattern}/" in f"/{filepath_str}/" or filepath_str.startswith(f"{dir_pattern}/"):
                return True
            return False

        # Path pattern (contains /)
        if '/' in pattern:
            # Normalize both for comparison
            pattern_normalized = pattern.replace('\\', '/')

            # Handle ** (match any depth)
            if '**' in pattern_normalized:
                regex = self._glob_to_regex(pattern_normalized)
                return bool(re.match(regex, filepath_str))

            # Simple path match
            return fnmatch.fnmatch(filepath_str, f"*/{pattern_normalized}") or \
                   fnmatch.fnmatch(filepath_str, pattern_normalized)

        # Simple filename pattern
        return fnmatch.fnmatch(filename, pattern)

    def _glob_to_regex(self, pattern: str) -> str:
        """Convert a glob pattern with ** to regex"""
        # Escape special regex chars except * and ?
        pattern = re.escape(pattern)
        # Convert ** (escaped as \*\*) to match any path
        pattern = pattern.replace(r'\*\*', '.*')
        # Convert * (escaped as \*) to match within a segment
        pattern = pattern.replace(r'\*', '[^/]*')
        # Convert ? (escaped as \?) to match single char
        pattern = pattern.replace(r'\?', '.')
        return f".*{pattern}$"

    def filter_files(self, file_paths: List[Path]) -> List[Path]:
        """
        Filter a list of files, removing ignored ones

        Args:
            file_paths: List of file paths to filter

        Returns:
            List of files that should be synced
        """
        return [p for p in file_paths if not self.should_ignore(p)]

    def get_patterns(self) -> List[str]:
        """Get current ignore patterns"""
        return self._patterns.copy()

    def get_negations(self) -> List[str]:
        """Get current negation patterns"""
        return self._negations.copy()

    def add_pattern(self, pattern: str) -> bool:
        """Add a pattern to .syncignore"""
        try:
            with open(SYNCIGNORE_FILE, 'a', encoding='utf-8') as f:
                f.write(f"\n{pattern}")
            self._last_mtime = 0  # Force reload
            self.load()
            return True
        except Exception as e:
            logger.error(f"Failed to add pattern: {e}")
            return False


def get_syncignore() -> SyncIgnore:
    """Get the SyncIgnore instance"""
    return SyncIgnore()


def should_ignore(filepath: Path) -> bool:
    """Check if a file should be ignored"""
    return get_syncignore().should_ignore(filepath)


def filter_files(file_paths: List[Path]) -> List[Path]:
    """Filter files based on .syncignore patterns"""
    return get_syncignore().filter_files(file_paths)
