#!/bin/bash
# Build standalone executable for Yank clipboard sync
# Usage: ./build.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=== Yank Build Script ==="
echo ""

# Detect platform
case "$(uname -s)" in
    Darwin)  PLATFORM="macOS" ;;
    Linux)   PLATFORM="Linux" ;;
    MINGW*|CYGWIN*|MSYS*) PLATFORM="Windows" ;;
    *)       PLATFORM="Unknown" ;;
esac

echo "Platform: $PLATFORM"
echo ""

# Check for virtual environment
if [ -d "venv" ]; then
    echo "Activating virtual environment..."
    source venv/bin/activate
else
    echo "Warning: No virtual environment found. Using system Python."
fi

# Check for PyInstaller
if ! python -c "import PyInstaller" 2>/dev/null; then
    echo "Installing PyInstaller..."
    pip install pyinstaller
fi

# Clean previous builds
echo "Cleaning previous builds..."
rm -rf build/ dist/

# Build
echo ""
echo "Building standalone executable..."
python -m PyInstaller yank.spec --clean

# Show result
echo ""
echo "=== Build Complete ==="
echo ""
if [ -f "dist/yank" ]; then
    ls -lh dist/yank
    echo ""
    echo "Executable created at: dist/yank"
    echo ""
    echo "To run: ./dist/yank start"
    echo "For help: ./dist/yank --help"
elif [ -f "dist/yank.exe" ]; then
    ls -lh dist/yank.exe
    echo ""
    echo "Executable created at: dist\\yank.exe"
fi
