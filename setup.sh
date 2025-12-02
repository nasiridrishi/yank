#!/bin/bash
# Automated setup script for Yank - LAN Clipboard Sync
# Supports macOS and Linux

set -e  # Exit on error

echo "=================================="
echo "  Yank - LAN Clipboard Sync"
echo "  Automated Setup"
echo "=================================="
echo ""

# Detect OS
OS_TYPE="$(uname -s)"
if [ "$OS_TYPE" = "Darwin" ]; then
    PLATFORM="macos"
    PLATFORM_NAME="macOS"
elif [ "$OS_TYPE" = "Linux" ]; then
    PLATFORM="linux"
    PLATFORM_NAME="Linux"
else
    echo "❌ Error: Unsupported platform: $OS_TYPE"
    echo "This script supports macOS and Linux only."
    exit 1
fi

echo "[OK] Detected platform: $PLATFORM_NAME"
echo ""

# Check Python version
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python 3 not found"
    echo "Please install Python 3.8 or higher"
    exit 1
fi

PYTHON_VERSION=$(python3 --version | cut -d' ' -f2)
echo "[OK] Found Python $PYTHON_VERSION"
echo ""

# Linux-specific: Check for GTK3 dependencies
if [ "$PLATFORM" = "linux" ]; then
    echo "Checking Linux system dependencies..."

    # Check if GTK3 is installed
    if ! pkg-config --exists gtk+-3.0 2>/dev/null; then
        echo "[WARNING] GTK3 not found"
        echo ""
        echo "Please install GTK3 system packages:"
        echo ""
        echo "  Ubuntu/Debian:"
        echo "    sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0"
        echo ""
        echo "  Fedora/RHEL:"
        echo "    sudo dnf install python3-gobject gtk3"
        echo ""
        echo "  Arch:"
        echo "    sudo pacman -S python-gobject gtk3"
        echo ""
        read -p "Continue anyway? [y/N] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    else
        echo "[OK] GTK3 system dependencies found"
    fi
    echo ""
fi

# Create virtual environment
if [ -d "venv" ]; then
    echo "[OK] Virtual environment already exists"
else
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "[OK] Virtual environment created"
fi
echo ""

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip, setuptools, and wheel..."
python -m pip install --upgrade pip setuptools wheel -q
echo "[OK] Build tools upgraded"
echo ""

# Install package with platform-specific dependencies
echo "Installing Yank with $PLATFORM_NAME dependencies..."
pip install -e ".[$PLATFORM]"
echo ""
echo "[OK] Yank installed successfully!"
echo ""

# Verify installation
echo "Verifying installation..."
if python -c "from yank.platform import get_platform_info; print('[OK] Platform detection:', get_platform_info().display_name)" 2>/dev/null; then
    echo "[OK] Installation verified"
else
    echo "[WARNING] Could not verify installation"
fi
echo ""

echo "=================================="
echo "  Setup Complete!"
echo "=================================="
echo ""
echo "Next steps:"
echo ""
echo "1. Activate the virtual environment:"
echo "   source venv/bin/activate"
echo ""
echo "2. Pair with another device:"
echo "   ./run.sh pair"
echo ""
echo "3. Start syncing:"
echo "   ./run.sh start"
echo ""
echo "4. View help:"
echo "   ./run.sh help"
echo ""
