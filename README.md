# Yank - LAN Clipboard Sync

Cross-platform clipboard synchronization between Windows, macOS, and Linux over LAN.

Copy files or text on one machine, instantly paste on the other.

**Features:**
- Secure PIN pairing with AES-256-GCM encryption
- Lazy file transfer (instant copy, download on paste)
- Text, files, and images
- No cloud. No internet. LAN only.

## Architecture

```
┌─────────────────────┐                    ┌─────────────────────┐
│   Windows Client    │                    │     Mac Client      │
│                     │                    │                     │
│  ┌───────────────┐  │                    │  ┌───────────────┐  │
│  │   Clipboard   │  │                    │  │   Clipboard   │  │
│  │   Monitor     │  │                    │  │   Monitor     │  │
│  └───────┬───────┘  │                    │  └───────┬───────┘  │
│          │          │                    │          │          │
│          ▼          │    Encrypted TCP   │          ▼          │
│  ┌───────────────┐  │    (AES-256-GCM)   │  ┌───────────────┐  │
│  │   Sync Agent  │◄─┼────────────────────┼─►│   Sync Agent  │  │
│  └───────────────┘  │    Port 9876       │  └───────────────┘  │
│                     │                    │                     │
└─────────────────────┘                    └─────────────────────┘
```

## Installation

### Requirements

- Python 3.8+ (Python 3.10+ recommended)
- **Linux only:** GTK3 system packages (see below)

### Linux System Dependencies

Install GTK3 before running setup:

```bash
# Ubuntu/Debian
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0

# Fedora/RHEL
sudo dnf install python3-gobject gtk3

# Arch
sudo pacman -S python-gobject gtk3
```

### Setup

**One-command install (recommended):**

```bash
# macOS/Linux
./setup.sh

# Windows
.\setup.ps1
```

**Manual install:**

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Linux/macOS
# .\venv\Scripts\activate  # Windows

# Upgrade pip
python -m pip install --upgrade pip setuptools wheel

# Install with platform-specific dependencies
pip install -e ".[windows]"  # Windows
pip install -e ".[macos]"    # macOS
pip install -e ".[linux]"    # Linux
```

---

## Quick Start

### 1. Pair Devices (One-Time Setup)

**On the first machine:**
```bash
# Windows
.\run.ps1 pair

# macOS/Linux
./run.sh pair
```
This displays a 6-digit PIN.

**On the second machine:**
```bash
# Windows
.\run.ps1 join 192.168.1.x 123456

# macOS/Linux
./run.sh join 192.168.1.x 123456
```

### 2. Start Syncing

**Windows:**
```powershell
.\run.ps1 start
# or just: .\run.ps1
```

**macOS/Linux:**
```bash
./run.sh start
# or just: ./run.sh
```

---

## Usage

Once both sides are running:

| Action | Windows | Mac | Linux |
|--------|---------|-----|-------|
| Copy files | Select in Explorer, Ctrl+C | Select in Finder, Cmd+C | Select in file manager, Ctrl+C |
| Copy text | Ctrl+C | Cmd+C | Ctrl+C |
| Paste | Ctrl+V | Cmd+V | Ctrl+V |

### Lazy Transfer (Large Files)

For files over 10MB, Yank uses lazy transfer:
1. **Copy** - Only metadata is sent (instant, even for 100GB!)
2. **Paste** - File downloads on-demand

Console output:
```
Announced 3 file(s) to Mac (2.5 GB)
Files ready for download on Mac

Files announced from Windows:
  - big_video.mp4 (2.5 GB)
  Ready to paste (Cmd+V) - download will start when you paste
```

---

## Commands

```bash
# Start syncing (default)
./run.sh start

# Pair with another device
./run.sh pair              # Show PIN
./run.sh join <IP> <PIN>   # Connect with PIN

# Manage pairing
./run.sh unpair            # Remove pairing
./run.sh status            # Show pairing status

# Configuration
./run.sh config            # Show current config
./run.sh config --set sync_text false
./run.sh config --reset
```

---

## Configuration

### User Config (`sync_config.json`)

Created automatically on first run:

```json
{
  "sync_files": true,
  "sync_text": true,
  "sync_images": true,
  "max_file_size": 104857600,
  "max_total_size": 524288000,
  "ignored_extensions": [".tmp", ".temp", ".bak"]
}
```

### Ignore Files (`.syncignore`)

Like `.gitignore` for clipboard sync:

```
# Ignore patterns
*.log
*.tmp
node_modules/
.git/
```

### App Config (`config.py`)

```python
PORT = 9876                        # Network port
MAX_FILE_SIZE = 100 * 1024 * 1024  # 100MB per file
POLL_INTERVAL = 0.3                # Clipboard check frequency
USE_AUTO_DISCOVERY = True          # mDNS peer discovery
```

---

## Security

- **PIN Pairing**: 6-digit PIN verification prevents unauthorized connections
- **AES-256-GCM**: All transfers encrypted with a derived key
- **Challenge-Response**: Mutual authentication on each connection
- **LAN Only**: No internet exposure by design

To disable security (not recommended):
```bash
./run.sh start --no-security
```

---

## Firewall

Allow port **9876** TCP:

**Windows (PowerShell as Admin):**
```powershell
netsh advfirewall firewall add rule name="Yank" dir=in action=allow protocol=tcp localport=9876
```

**macOS:**
Usually no action needed on home networks.

**Linux:**
```bash
# UFW (Ubuntu/Debian)
sudo ufw allow 9876/tcp

# firewalld (Fedora/RHEL/CentOS)
sudo firewall-cmd --permanent --add-port=9876/tcp
sudo firewall-cmd --reload

# iptables (manual)
sudo iptables -A INPUT -p tcp --dport 9876 -j ACCEPT
```

---

**Check status:**
```bash
./run.sh status
```

---

## Supported Content

- Files from Explorer/Finder
- Multiple files and folders
- Text clipboard
- Screenshots (PrtScn, Cmd+Shift+4)
- Images from apps (browser, Photoshop, etc.)
- Image files (.png, .jpg, .gif, .webp)

---

## Limitations

- **LAN only** - no internet/cloud (by design)
- **Max 500MB** per transfer (configurable)
- Folders are flattened (files extracted)
- Images converted to PNG for compatibility

