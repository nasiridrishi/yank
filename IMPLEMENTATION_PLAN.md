# Lazy File Transfer Implementation Plan

## Overview

Implement iCloud-like clipboard sync where:
1. Copying files sends only **metadata** (instant, even for 100GB)
2. Actual file transfer happens **on-demand** when pasting
3. Large files are transferred via **chunked streaming** (memory efficient)
4. User sees **progress** during transfer

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    PHASE 1: COPY (Device A)                     │
├─────────────────────────────────────────────────────────────────┤
│  1. User copies files (Ctrl+C)                                  │
│  2. Clipboard monitor detects files                             │
│  3. Generate transfer_id (UUID)                                 │
│  4. Calculate metadata (name, size, checksum for each file)     │
│  5. Send FILE_ANNOUNCE message to peer (metadata only!)         │
│  6. Register files in FileRegistry (ready to serve on request)  │
│                                                                 │
│  Time: ~100ms even for 1TB of files                             │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│              PHASE 2: RECEIVE ANNOUNCE (Device B)               │
├─────────────────────────────────────────────────────────────────┤
│  1. Receive FILE_ANNOUNCE with metadata                         │
│  2. Store in PendingTransfers registry                          │
│  3. Set up "virtual files" in clipboard:                        │
│     - Windows: Custom IDataObject with CFSTR_FILEDESCRIPTOR     │
│     - macOS: Placeholder files + NSFilePresenter                │
│  4. User sees "files ready to paste" immediately                │
│                                                                 │
│  Time: ~50ms                                                    │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼ (User pastes - Ctrl+V)
┌─────────────────────────────────────────────────────────────────┐
│                 PHASE 3: PASTE TRIGGERS DOWNLOAD                │
├─────────────────────────────────────────────────────────────────┤
│  1. OS requests file content (IStream::Read / NSFilePresenter)  │
│  2. Send FILE_REQUEST to Device A                               │
│  3. Device A reads file in chunks (1MB each)                    │
│  4. Stream FILE_CHUNK messages to Device B                      │
│  5. Device B writes chunks to temp file, updates progress       │
│  6. On complete: verify checksum, move to final location        │
│  7. Paste operation completes with real file                    │
│                                                                 │
│  Shows: "Downloading: 45% (450MB/1GB) - document.pdf"           │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Phases

### Phase 1: Protocol & Core Infrastructure (Foundation)
**Goal:** New message types and core utilities for chunked transfer

#### Tasks:
1.1. **New Message Types in protocol.py**
     - FILE_ANNOUNCE (0x14) - metadata only
     - FILE_REQUEST (0x15) - request specific file
     - FILE_CHUNK (0x16) - chunk of file data
     - FILE_COMPLETE (0x17) - transfer complete
     - TRANSFER_CANCEL (0x18) - cancel transfer
     - TRANSFER_ERROR (0x19) - error during transfer

1.2. **Data Structures**
     - TransferMetadata (enhanced) - transfer_id, expiry
     - ChunkInfo - offset, size, checksum
     - TransferProgress - bytes_sent, bytes_total, speed, eta

1.3. **Chunked File Reader utility**
     - Read file in configurable chunks (default 1MB)
     - Yield chunks with offset and checksum
     - Memory efficient (never loads full file)

1.4. **Chunked File Writer utility**
     - Write chunks to temp file
     - Track progress
     - Verify final checksum
     - Atomic move to destination

---

### Phase 2: File Registry System
**Goal:** Track announced files (sender) and pending transfers (receiver)

#### Tasks:
2.1. **FileRegistry class (common/file_registry.py)**
     - announced_transfers: files we can serve (sender side)
     - pending_transfers: files we're waiting for (receiver side)
     - Auto-cleanup expired transfers (TTL: 5 minutes)
     - Thread-safe access

2.2. **TransferInfo dataclass**
     - transfer_id: str (UUID)
     - files: List[FileMetadata]
     - source_paths: List[Path] (sender only)
     - announced_at: float
     - expires_at: float
     - status: pending/transferring/complete/failed

2.3. **Registry Operations**
     - register_announced(transfer_id, files, paths)
     - register_pending(transfer_id, metadata)
     - get_transfer(transfer_id)
     - mark_expired()
     - cleanup()

---

### Phase 3: Sender Side - Announce & Serve
**Goal:** Send metadata on copy, serve chunks on request

#### Tasks:
3.1. **Update agent.py - Announce capability**
     - New method: announce_files(file_paths) -> transfer_id
     - Calculates metadata without reading file contents
     - Sends FILE_ANNOUNCE message
     - Registers in FileRegistry

3.2. **Update agent.py - Serve chunks**
     - Handle FILE_REQUEST message
     - Look up transfer in FileRegistry
     - Stream FILE_CHUNK messages
     - Send FILE_COMPLETE when done

3.3. **Update main.py - Use announce instead of send**
     - _on_files_copied() calls announce_files() not send_files()
     - Keep send_files() for backward compatibility / small files

3.4. **Chunk streaming implementation**
     - Read file in 1MB chunks
     - Send each chunk with offset
     - Handle backpressure (wait for ACK every N chunks)
     - Resume support (start from offset)

---

### Phase 4: Receiver Side - Basic (Blocking Download)
**Goal:** Request and receive files when needed

#### Tasks:
4.1. **Handle FILE_ANNOUNCE**
     - Parse metadata
     - Store in pending_transfers
     - Trigger callback: on_files_announced(transfer_id, metadata)

4.2. **Request files method**
     - request_transfer(transfer_id) -> downloads all files
     - Sends FILE_REQUEST for each file
     - Receives and writes chunks
     - Shows progress
     - Returns list of downloaded file paths

4.3. **Progress tracking**
     - ProgressTracker class
     - Callbacks: on_progress(bytes_done, bytes_total, speed, eta)
     - Console progress bar

4.4. **Integrate with main.py**
     - on_files_announced() -> start download immediately (for now)
     - Show progress during download
     - Put completed files in clipboard

---

### Phase 5: Windows Virtual Clipboard
**Goal:** True on-demand - paste triggers download

#### Tasks:
5.1. **Research & prototype IDataObject**
     - Study pywin32 IDataObject implementation
     - Create minimal working example
     - Test with Explorer paste

5.2. **Implement VirtualFileDataObject class**
     - Implements IDataObject interface
     - GetData(CFSTR_FILEDESCRIPTOR) -> returns file metadata
     - GetData(CFSTR_FILECONTENTS) -> returns IStream

5.3. **Implement VirtualFileStream class**
     - Implements IStream interface
     - Read() triggers FILE_REQUEST to peer
     - Blocks until chunk received, returns data
     - Handles progress UI

5.4. **Integration**
     - on_files_announced() -> set VirtualFileDataObject in clipboard
     - User pastes -> IStream.Read() called -> triggers download
     - File appears after download complete

---

### Phase 6: macOS Virtual Clipboard
**Goal:** True on-demand for macOS

#### Tasks:
6.1. **Research NSFilePresenter approach**
     - Study PyObjC NSFilePresenter
     - Test placeholder file approach
     - Verify Finder triggers relinquishPresentedItemToReader

6.2. **Implement FilePromisePresenter class**
     - Conforms to NSFilePresenter protocol
     - Creates placeholder file
     - relinquishPresentedItemToReader triggers download

6.3. **Placeholder file management**
     - Create zero-byte placeholder in temp directory
     - Register with NSFileCoordinator
     - Put file URL on pasteboard

6.4. **Download on access**
     - When relinquishPresentedItemToReader called
     - Send FILE_REQUEST to peer
     - Write data to placeholder file
     - Call completion handler

---

### Phase 7: Progress UI & User Experience
**Goal:** Beautiful progress indication

#### Tasks:
7.1. **Console progress bar**
     - [████████░░░░░░░░] 52% (520MB/1GB) - document.pdf
     - Show speed: 25 MB/s
     - Show ETA: ~20s remaining

7.2. **Multi-file progress**
     - Overall progress: 3/10 files
     - Current file progress
     - Total bytes progress

7.3. **Notifications**
     - "Receiving files from Mac..." (start)
     - "Files ready to paste" (complete)
     - "Transfer failed: Device offline" (error)

---

### Phase 8: Error Handling & Edge Cases
**Goal:** Robust error handling

#### Tasks:
8.1. **Source device offline**
     - Timeout waiting for chunks (30s)
     - Retry logic (3 attempts)
     - Clear error message to user

8.2. **Transfer cancellation**
     - User can cancel (Ctrl+C during progress)
     - Send TRANSFER_CANCEL message
     - Clean up partial files

8.3. **Resume interrupted transfers**
     - Save progress to disk
     - On reconnect, resume from last chunk
     - Verify partial file checksum

8.4. **Multiple concurrent transfers**
     - Queue management
     - One active transfer at a time (configurable)
     - Cancel old transfer if new files copied

8.5. **Expiry handling**
     - Transfers expire after 5 minutes
     - Clear from registry
     - Show "Transfer expired" if paste attempted

---

## File Changes Summary

### New Files:
- `common/file_registry.py` - Transfer tracking
- `common/chunked_transfer.py` - Chunk read/write utilities
- `common/progress.py` - Progress tracking
- `windows/virtual_clipboard.py` - IDataObject implementation
- `macos/virtual_clipboard.py` - NSFilePresenter implementation

### Modified Files:
- `common/protocol.py` - New message types
- `agent.py` - Announce, request, chunk handling
- `main.py` - New flow integration
- `windows/clipboard.py` - Virtual file support
- `macos/clipboard.py` - Virtual file support
- `common/user_config.py` - New settings (chunk_size, transfer_timeout)

---

## Configuration Additions

```json
{
  "chunk_size": 1048576,        // 1MB chunks
  "transfer_timeout": 30,       // seconds to wait for peer
  "transfer_expiry": 300,       // 5 minutes
  "max_concurrent_transfers": 1,
  "auto_download_threshold": 10485760,  // 10MB - auto-download if smaller
  "show_progress": true
}
```

---

## Testing Plan

1. **Small file (<10MB)** - Should work instantly
2. **Large file (1GB)** - Should stream with progress
3. **Multiple files** - Should handle sequentially
4. **Directory with many files** - Should work
5. **Cancel mid-transfer** - Should clean up
6. **Source goes offline** - Should timeout gracefully
7. **Resume after disconnect** - Should continue
8. **Paste before download** - Should trigger download

---

## Implementation Status

### Completed Phases:
- Phase 1 (Protocol) - New message types added to protocol.py
- Phase 2 (Registry) - FileRegistry class with auto-cleanup
- Phase 3 (Sender) - announce_files() and chunk streaming in agent.py
- Phase 4 (Receiver) - request_transfer() with auto-download on announce
- Phase 7 (Progress UI) - Console progress bar with speed/ETA

### Pending Phases:
- Phase 5 (Windows Virtual Clipboard) - True on-demand paste trigger
- Phase 6 (macOS Virtual Clipboard) - True on-demand paste trigger
- Phase 8 (Error handling) - Resume, retry, better error messages

### Current Behavior:
1. Files < 10MB: Sent immediately (legacy behavior)
2. Files >= 10MB: Announced with metadata only (instant!)
   - Receiver auto-downloads in background
   - Progress bar shown during download
   - Files placed in clipboard when complete

### Future Enhancement (Phase 5/6):
True on-demand transfer where download only starts when user pastes,
not when files are announced. Requires platform-specific clipboard APIs.

---

## Rollback Plan

Keep existing send_files() working for:
- Text clipboard (always instant)
- Small files under threshold
- Fallback if virtual clipboard fails

New lazy transfer is opt-in via config until stable.
