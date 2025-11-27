# LAN Clipboard File Sync

Cross-platform clipboard **file** synchronization between Windows and Mac over LAN.

Copy files on one machine → instantly paste on the other.

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
│          ▼          │                    │          ▼          │
│  ┌───────────────┐  │    TCP Socket      │  ┌───────────────┐  │
│  │   Sync Agent  │◄─┼────────────────────┼─►│   Sync Agent  │  │
│  └───────────────┘  │    Port 9876       │  └───────────────┘  │
│                     │                    │                     │
└─────────────────────┘                    └─────────────────────┘
```

## How It Works

1. **Clipboard Monitor** watches for file copies (Ctrl+C / Cmd+C on files) **and screenshots/images**
2. When files or images are detected, they're packaged with metadata + checksums
3. **Sync Agent** sends the package to the peer over TCP (LAN only)
4. Peer receives, verifies checksums, and injects into its clipboard
5. You can now paste (Ctrl+V / Cmd+V) on the other machine

**No cloud. No internet. LAN only.**

### Supported Content
- ✅ Files from Explorer/Finder
- ✅ Multiple files
- ✅ Folders (contents extracted)
- ✅ Screenshots (PrtScn, Cmd+Shift+4, Snipping Tool)
- ✅ Images copied from apps (browser, Photoshop, etc.)
- ✅ Image files (.png, .jpg, .gif, .bmp, .webp)

---

## Quick Start

### On Windows

```powershell
# Install dependencies
pip install pywin32 zeroconf Pillow

# Run (auto-discovers Mac on same network)
cd clipboard-sync
python -m windows.main

# Or specify Mac's IP directly
python -m windows.main --peer 192.168.1.50
```

### On macOS

```bash
# Install dependencies
pip install pyobjc zeroconf Pillow

# Run (auto-discovers Windows on same network)
cd clipboard-sync
python -m macos.main

# Or specify Windows IP directly
python -m macos.main --peer 192.168.1.100
```

---

## Usage

Once both sides are running:

1. **Windows → Mac**: Select file(s) in Explorer → Ctrl+C → Go to Mac → Cmd+V
2. **Mac → Windows**: Select file(s) in Finder → Cmd+C → Go to Windows → Ctrl+V

Console shows transfer status:
```
✓ Sent 3 file(s) to Mac (2.5MB)
✓ Received from Windows: document.pdf, image.png
  Ready to paste (Cmd+V)
```

---

## Run at Startup

### Windows (using NSSM - recommended)

1. Download [NSSM](https://nssm.cc/download)
2. Run as Admin:
   ```cmd
   nssm install ClipboardSync
   ```
3. Configure:
   - Path: `C:\Python311\python.exe`
   - Startup dir: `C:\path\to\clipboard-sync`
   - Arguments: `-m windows.main`
4. Start: `nssm start ClipboardSync`

### macOS (using launchd)

```bash
# Edit the plist to set your paths
nano macos/launchd.plist

# Install
cp macos/launchd.plist ~/Library/LaunchAgents/com.clipboard-sync.agent.plist
launchctl load ~/Library/LaunchAgents/com.clipboard-sync.agent.plist
```

---

## Configuration

Edit `config.py`:

```python
PORT = 9876                    # Network port
MAX_FILE_SIZE = 100 * 1024 * 1024   # 100MB per file
MAX_TOTAL_SIZE = 500 * 1024 * 1024  # 500MB total transfer
POLL_INTERVAL = 0.3            # Clipboard check frequency

# Manual peer (disable auto-discovery)
USE_AUTO_DISCOVERY = False
PEER_IP = "192.168.1.100"
```

---

## Firewall

Allow port **9876** TCP on both machines:

**Windows:**
```powershell
netsh advfirewall firewall add rule name="ClipboardSync" dir=in action=allow protocol=tcp localport=9876
```

**macOS:**
Usually no action needed (accepts incoming by default on home networks).

---

## Project Structure

```
clipboard-sync/
├── common/
│   ├── protocol.py      # Message serialization (binary protocol)
│   └── discovery.py     # mDNS/Bonjour peer discovery
├── windows/
│   ├── clipboard.py     # Win32 clipboard (CF_HDROP)
│   ├── service.py       # Windows Service wrapper
│   └── main.py          # Entry point
├── macos/
│   ├── clipboard.py     # NSPasteboard via PyObjC
│   ├── launchd.plist    # LaunchAgent config
│   └── main.py          # Entry point
├── config.py            # Settings
├── agent.py             # Core networking (TCP server/client)
└── requirements.txt
```

---

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "No peer available" | Check both machines are on same LAN subnet |
| "Connection refused" | Ensure firewall allows port 9876 |
| Files not appearing in clipboard | Check logs in `/tmp/clipboard-sync/` or `%TEMP%\clipboard-sync\` |
| Large files fail | Increase `MAX_TOTAL_SIZE` in config.py |

Enable verbose logging:
```bash
python -m windows.main --verbose
python -m macos.main --verbose
```

---

## Limitations

- **LAN only** - no internet/cloud support (by design)
- **Max ~500MB** per transfer (configurable)
- **No encryption** by default (add `ENCRYPTION_KEY` in config for AES)
- Folders are flattened (individual files extracted)
- Images are converted to PNG for cross-platform compatibility

---

## Works With Parsec

This is designed to complement Parsec which handles **text** clipboard sync.
This tool adds **file** clipboard sync that Parsec doesn't support.

Your workflow:
- Parsec: Display streaming + keyboard/mouse + text clipboard ✓
- This tool: File clipboard sync ✓
