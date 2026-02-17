# Yank - LAN Clipboard Sync

Cross-platform clipboard synchronization between Windows, macOS, and Linux over LAN.

Copy files or text on one machine, instantly paste on the other.

**Features:**
- Secure PIN pairing with AES-256-GCM encryption
- Lazy file transfer (instant copy, download on paste)
- Text, files, and images
- No cloud. No internet. LAN only.

## Installation

### Download

Download the executable for your platform from [Releases](https://github.com/nasiridrishi/yank/releases):

| Platform | Download |
|----------|----------|
| macOS | `yank-macos` |
| Windows | `yank-windows.exe` |
| Linux | `yank-linux` |

### Install

**macOS:**
```bash
chmod +x yank-macos
mv yank-macos /usr/local/bin/yank
```

**Linux:**
```bash
chmod +x yank-linux
sudo mv yank-linux /usr/local/bin/yank
```

**Windows:**
Move `yank-windows.exe` to a folder in your PATH, or run directly.

### Linux: GTK3 Dependency

Linux requires GTK3 system libraries:

```bash
# Ubuntu/Debian
sudo apt install libgtk-3-0

# Fedora/RHEL
sudo dnf install gtk3

# Arch
sudo pacman -S gtk3
```

---

## Quick Start

### 1. Pair Devices (One-Time)

**On machine A:**
```bash
yank pair
```
Shows a 6-digit PIN and your IP.

**On machine B:**
```bash
yank join 192.168.1.x 123456
```
Replace with actual IP and PIN.

That's it. The service starts automatically and survives reboots. Copy on one machine, paste on the other.

---

## Usage

| Action | Windows | macOS | Linux |
|--------|---------|-------|-------|
| Copy files | Ctrl+C | Cmd+C | Ctrl+C |
| Copy text | Ctrl+C | Cmd+C | Ctrl+C |
| Paste | Ctrl+V | Cmd+V | Ctrl+V |

### Large Files (Lazy Transfer)

Files over 10MB use lazy transfer:
1. **Copy** - Only metadata sent (instant, even for 100GB)
2. **Paste** - File downloads on-demand

---

## Commands

```bash
# Pairing (one-time)
yank pair                   # Show PIN for pairing (auto-starts service)
yank join <IP> <PIN>        # Pair with another device (auto-starts service)
yank unpair                 # Remove pairing and uninstall service

# Service management
yank status                 # Show service and pairing status
yank stop                   # Stop the service
yank start                  # Start the service
yank logs                   # View last 50 log lines
yank logs -f                # Follow logs in real-time

# Configuration
yank config                 # Show configuration
yank config --set KEY VAL   # Change setting
yank --help                 # Show all options
```

---

## Configuration

Settings are in `~/.yank/config.json` (created on first run):

```json
{
  "sync_files": true,
  "sync_text": true,
  "sync_images": true,
  "max_file_size_mb": 100,
  "max_total_size_mb": 500
}
```

### Ignore Patterns

Create `.syncignore` in your home directory:
```
*.log
*.tmp
node_modules/
.git/
```

---

## Firewall

Allow port **9876** TCP:

**Windows (Admin PowerShell):**
```powershell
netsh advfirewall firewall add rule name="Yank" dir=in action=allow protocol=tcp localport=9876
```

**Linux:**
```bash
sudo ufw allow 9876/tcp
```

**macOS:** Usually no action needed.

---

## Security

- **PIN Pairing**: 6-digit PIN prevents unauthorized connections
- **AES-256-GCM**: All data encrypted
- **LAN Only**: Never touches the internet

---

## Building from Source

For developers who want to build the executable:

```bash
# Clone and setup
git clone https://github.com/nasiridrishi/yank.git
cd yank
python -m venv venv
source venv/bin/activate  # or .\venv\Scripts\activate on Windows
pip install -e ".[dev,macos]"  # or [dev,windows] or [dev,linux]

# Build standalone executable
./build.sh      # macOS/Linux
.\build.ps1     # Windows

# Output: dist/yank (or dist/yank.exe)
```

---

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
