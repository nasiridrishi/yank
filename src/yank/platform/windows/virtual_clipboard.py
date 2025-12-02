"""
Windows Virtual Clipboard - True on-demand file transfer.

Implements IDataObject with CFSTR_FILEDESCRIPTOR and CFSTR_FILECONTENTS
to provide virtual files that trigger downloads when pasted.

When Explorer pastes, it:
1. Reads CFSTR_FILEDESCRIPTOR to get file metadata
2. For each file, reads CFSTR_FILECONTENTS via IStream
3. Our IStream.Read() triggers the actual download from peer

This provides true iCloud-like behavior where:
- Copy is instant (just metadata)
- Download happens only when you paste
"""
import struct
import threading
import logging
import time
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Try to import Windows-specific modules
try:
    import pythoncom
    import pywintypes
    import win32con
    import win32clipboard
    from win32com.server.util import wrap as WrapObject
    from win32com.server.exception import COMException
    import winerror
    HAS_PYWIN32 = True
except ImportError:
    HAS_PYWIN32 = False
    logger.warning("pywin32 not available - virtual clipboard disabled")


# Windows constants
MAX_PATH = 260
CFSTR_FILEDESCRIPTOR_W = "FileGroupDescriptorW"
CFSTR_FILECONTENTS = "FileContents"

# FILEDESCRIPTOR flags
FD_CLSID = 0x00000001
FD_SIZEPOINT = 0x00000002
FD_ATTRIBUTES = 0x00000004
FD_CREATETIME = 0x00000008
FD_ACCESSTIME = 0x00000010
FD_WRITESTIME = 0x00000020
FD_FILESIZE = 0x00000040
FD_PROGRESSUI = 0x00004000  # Show progress during paste
FD_UNICODE = 0x80000000

# File attributes
FILE_ATTRIBUTE_NORMAL = 0x00000080
FILE_ATTRIBUTE_ARCHIVE = 0x00000020


@dataclass
class VirtualFile:
    """Represents a virtual file to be placed on clipboard"""
    name: str
    size: int
    checksum: str
    file_index: int
    transfer_id: str


def register_clipboard_formats():
    """Register custom clipboard formats"""
    if not HAS_PYWIN32:
        return None, None

    try:
        cf_filedescriptor = win32clipboard.RegisterClipboardFormat(CFSTR_FILEDESCRIPTOR_W)
        cf_filecontents = win32clipboard.RegisterClipboardFormat(CFSTR_FILECONTENTS)
        return cf_filedescriptor, cf_filecontents
    except Exception as e:
        logger.error(f"Failed to register clipboard formats: {e}")
        return None, None


def pack_filedescriptor_w(files: List[VirtualFile]) -> bytes:
    """
    Pack FILEGROUPDESCRIPTORW structure.

    Layout:
    - UINT cItems (4 bytes)
    - FILEDESCRIPTORW[cItems] (each 592 bytes for Unicode)

    FILEDESCRIPTORW layout (592 bytes):
    - DWORD dwFlags (4)
    - CLSID clsid (16)
    - SIZEL sizel (8)
    - POINTL pointl (8)
    - DWORD dwFileAttributes (4)
    - FILETIME ftCreationTime (8)
    - FILETIME ftLastAccessTime (8)
    - FILETIME ftLastWriteTime (8)
    - DWORD nFileSizeHigh (4)
    - DWORD nFileSizeLow (4)
    - WCHAR cFileName[MAX_PATH] (520)
    """
    # Header: number of files
    data = struct.pack('<I', len(files))

    for vf in files:
        # Flags: we provide file size
        flags = FD_FILESIZE | FD_ATTRIBUTES | FD_PROGRESSUI | FD_UNICODE

        # CLSID (16 bytes of zeros)
        clsid = b'\x00' * 16

        # SIZEL (8 bytes)
        sizel = struct.pack('<II', 0, 0)

        # POINTL (8 bytes)
        pointl = struct.pack('<ii', 0, 0)

        # File attributes
        attrs = FILE_ATTRIBUTE_NORMAL | FILE_ATTRIBUTE_ARCHIVE

        # FILETIME (8 bytes each) - use current time
        # FILETIME is 100-nanosecond intervals since Jan 1, 1601
        # For simplicity, use zeros (Windows will use current time)
        filetime = struct.pack('<Q', 0)

        # File size (high and low DWORD)
        size_high = (vf.size >> 32) & 0xFFFFFFFF
        size_low = vf.size & 0xFFFFFFFF

        # Filename (Unicode, MAX_PATH characters = 520 bytes)
        filename_bytes = vf.name.encode('utf-16-le')
        filename_padded = filename_bytes[:518] + b'\x00\x00'  # Ensure null termination
        filename_padded = filename_padded.ljust(520, b'\x00')

        # Pack FILEDESCRIPTORW
        fd = struct.pack('<I', flags)  # dwFlags
        fd += clsid  # clsid (16 bytes)
        fd += sizel  # sizel (8 bytes)
        fd += pointl  # pointl (8 bytes)
        fd += struct.pack('<I', attrs)  # dwFileAttributes
        fd += filetime  # ftCreationTime
        fd += filetime  # ftLastAccessTime
        fd += filetime  # ftLastWriteTime
        fd += struct.pack('<I', size_high)  # nFileSizeHigh
        fd += struct.pack('<I', size_low)  # nFileSizeLow
        fd += filename_padded  # cFileName

        data += fd

    return data


class VirtualFileStream:
    """
    Implements IStream interface for virtual file content.

    When Explorer reads from this stream, we trigger the actual
    download from the peer device.
    """

    _public_methods_ = ['Read', 'Write', 'Seek', 'SetSize', 'CopyTo',
                        'Commit', 'Revert', 'LockRegion', 'UnlockRegion',
                        'Stat', 'Clone']
    _com_interfaces_ = [pythoncom.IID_IStream] if HAS_PYWIN32 else []

    def __init__(
        self,
        virtual_file: VirtualFile,
        download_callback: Callable[[str, int], Optional[bytes]]
    ):
        """
        Initialize the virtual stream.

        Args:
            virtual_file: The virtual file metadata
            download_callback: Function to call to get file data
                              Args: (transfer_id, file_index)
                              Returns: File bytes or None on failure
        """
        self.virtual_file = virtual_file
        self.download_callback = download_callback
        self._position = 0
        self._data: Optional[bytes] = None
        self._lock = threading.Lock()
        self._download_started = False
        self._download_complete = False
        self._download_error: Optional[str] = None

    def _ensure_downloaded(self):
        """Ensure file data is downloaded (lazy load)"""
        with self._lock:
            if self._data is not None:
                return True

            if self._download_error:
                return False

            if not self._download_started:
                self._download_started = True
                logger.info(f"Starting download for: {self.virtual_file.name}")

                try:
                    self._data = self.download_callback(
                        self.virtual_file.transfer_id,
                        self.virtual_file.file_index
                    )
                    if self._data is None:
                        self._download_error = "Download failed"
                        return False
                    self._download_complete = True
                    logger.info(f"Download complete: {self.virtual_file.name} ({len(self._data)} bytes)")
                    return True
                except Exception as e:
                    self._download_error = str(e)
                    logger.error(f"Download error: {e}")
                    return False

            return self._download_complete

    def Read(self, num_bytes):
        """Read bytes from the stream - triggers download if needed"""
        if not self._ensure_downloaded():
            raise COMException(desc=self._download_error or "Download failed",
                             hresult=winerror.E_FAIL)

        with self._lock:
            if self._position >= len(self._data):
                return b''

            end_pos = min(self._position + num_bytes, len(self._data))
            result = self._data[self._position:end_pos]
            self._position = end_pos
            return result

    def Write(self, data):
        """Write not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def Seek(self, offset, origin):
        """Seek within the stream"""
        if not self._ensure_downloaded():
            raise COMException(hresult=winerror.E_FAIL)

        with self._lock:
            if origin == 0:  # STREAM_SEEK_SET
                new_pos = offset
            elif origin == 1:  # STREAM_SEEK_CUR
                new_pos = self._position + offset
            elif origin == 2:  # STREAM_SEEK_END
                new_pos = len(self._data) + offset
            else:
                raise COMException(hresult=winerror.E_INVALIDARG)

            if new_pos < 0:
                new_pos = 0
            if new_pos > len(self._data):
                new_pos = len(self._data)

            self._position = new_pos
            return self._position

    def SetSize(self, size):
        """SetSize not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def CopyTo(self, stream, count):
        """Copy to another stream"""
        data = self.Read(count)
        stream.Write(data)
        return len(data), len(data)

    def Commit(self, flags):
        """Commit not supported"""
        pass

    def Revert(self):
        """Revert not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def LockRegion(self, offset, count, lock_type):
        """LockRegion not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def UnlockRegion(self, offset, count, lock_type):
        """UnlockRegion not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def Stat(self, flags):
        """Return stream statistics"""
        if not self._ensure_downloaded():
            size = self.virtual_file.size
        else:
            size = len(self._data)

        # Return STATSTG structure as tuple
        # (pwcsName, type, cbSize, mtime, ctime, atime, grfMode, grfLocksSupported, clsid, grfStateBits, reserved)
        return (
            self.virtual_file.name,  # pwcsName
            2,  # STGTY_STREAM
            size,  # cbSize
            pywintypes.Time(0),  # mtime
            pywintypes.Time(0),  # ctime
            pywintypes.Time(0),  # atime
            0,  # grfMode
            0,  # grfLocksSupported
            pythoncom.IID_NULL,  # clsid
            0,  # grfStateBits
            0   # reserved
        )

    def Clone(self):
        """Clone the stream"""
        raise COMException(hresult=winerror.E_NOTIMPL)


class VirtualFileDataObject:
    """
    Implements IDataObject for virtual files.

    Provides CFSTR_FILEDESCRIPTOR for file metadata and
    CFSTR_FILECONTENTS (as IStream) for file data.
    """

    _public_methods_ = ['GetData', 'GetDataHere', 'QueryGetData', 'GetCanonicalFormatEtc',
                        'SetData', 'EnumFormatEtc', 'DAdvise', 'DUnadvise', 'EnumDAdvise']
    _com_interfaces_ = [pythoncom.IID_IDataObject] if HAS_PYWIN32 else []

    def __init__(
        self,
        virtual_files: List[VirtualFile],
        download_callback: Callable[[str, int], Optional[bytes]]
    ):
        """
        Initialize the data object.

        Args:
            virtual_files: List of virtual files to expose
            download_callback: Function to download file content
        """
        self.virtual_files = virtual_files
        self.download_callback = download_callback
        self._streams: Dict[int, VirtualFileStream] = {}

        # Register clipboard formats
        self.cf_filedescriptor, self.cf_filecontents = register_clipboard_formats()

        # Define supported formats
        self.supported_formats = []

        if self.cf_filedescriptor:
            # FILEDESCRIPTOR format
            self.supported_formats.append((
                self.cf_filedescriptor,
                None,
                pythoncom.DVASPECT_CONTENT,
                -1,
                pythoncom.TYMED_HGLOBAL
            ))

        if self.cf_filecontents:
            # FILECONTENTS format (one per file, using lindex)
            for i, vf in enumerate(virtual_files):
                self.supported_formats.append((
                    self.cf_filecontents,
                    None,
                    pythoncom.DVASPECT_CONTENT,
                    i,  # lindex = file index
                    pythoncom.TYMED_ISTREAM
                ))

    def GetData(self, formatetc):
        """Return data for the requested format"""
        cf, target, aspect, lindex, tymed = formatetc

        # Handle FILEDESCRIPTOR
        if cf == self.cf_filedescriptor and tymed & pythoncom.TYMED_HGLOBAL:
            data = pack_filedescriptor_w(self.virtual_files)
            return (pythoncom.TYMED_HGLOBAL, data, None)

        # Handle FILECONTENTS
        if cf == self.cf_filecontents and tymed & pythoncom.TYMED_ISTREAM:
            if 0 <= lindex < len(self.virtual_files):
                # Get or create stream for this file
                if lindex not in self._streams:
                    self._streams[lindex] = VirtualFileStream(
                        self.virtual_files[lindex],
                        self.download_callback
                    )

                # Wrap as COM object and return
                stream = WrapObject(self._streams[lindex], pythoncom.IID_IStream)
                return (pythoncom.TYMED_ISTREAM, stream, None)

        raise COMException(hresult=winerror.DV_E_FORMATETC)

    def GetDataHere(self, formatetc, medium):
        """GetDataHere not supported"""
        raise COMException(hresult=winerror.E_NOTIMPL)

    def QueryGetData(self, formatetc):
        """Check if format is supported"""
        cf, target, aspect, lindex, tymed = formatetc

        # Check FILEDESCRIPTOR
        if cf == self.cf_filedescriptor and tymed & pythoncom.TYMED_HGLOBAL:
            return None  # S_OK

        # Check FILECONTENTS
        if cf == self.cf_filecontents and tymed & pythoncom.TYMED_ISTREAM:
            if 0 <= lindex < len(self.virtual_files):
                return None  # S_OK

        raise COMException(hresult=winerror.DV_E_FORMATETC)

    def GetCanonicalFormatEtc(self, formatetc):
        """Return canonical format"""
        raise COMException(hresult=winerror.DATA_S_SAMEFORMATETC)

    def SetData(self, formatetc, medium, release):
        """SetData - accept but ignore (required for drag-drop helper)"""
        return None  # S_OK

    def EnumFormatEtc(self, direction):
        """Enumerate supported formats"""
        if direction != pythoncom.DATADIR_GET:
            raise COMException(hresult=winerror.E_NOTIMPL)

        # Create format list
        formats = []

        if self.cf_filedescriptor:
            formats.append((
                self.cf_filedescriptor,
                None,
                pythoncom.DVASPECT_CONTENT,
                -1,
                pythoncom.TYMED_HGLOBAL
            ))

        if self.cf_filecontents:
            for i in range(len(self.virtual_files)):
                formats.append((
                    self.cf_filecontents,
                    None,
                    pythoncom.DVASPECT_CONTENT,
                    i,
                    pythoncom.TYMED_ISTREAM
                ))

        from win32com.server.util import NewEnum
        return NewEnum(formats, iid=pythoncom.IID_IEnumFORMATETC)

    def DAdvise(self, formatetc, flags, sink):
        """DAdvise not supported"""
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)

    def DUnadvise(self, connection):
        """DUnadvise not supported"""
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)

    def EnumDAdvise(self):
        """EnumDAdvise not supported"""
        raise COMException(hresult=winerror.OLE_E_ADVISENOTSUPPORTED)


# Keep global reference to prevent garbage collection
_active_clipboard_data = {
    'data_object': None,
    'wrapped': None,
    'download_callback': None
}


def set_virtual_clipboard(
    files: List[Dict[str, Any]],
    transfer_id: str,
    download_callback: Callable[[str, int], Optional[bytes]]
) -> bool:
    """
    Set virtual files on the Windows clipboard.

    Args:
        files: List of file info dicts with 'name', 'size', 'checksum', 'file_index'
        transfer_id: The transfer ID for downloading
        download_callback: Function to call when file content is needed

    Returns:
        True if successful
    """
    global _active_clipboard_data

    if not HAS_PYWIN32:
        logger.error("pywin32 not available")
        return False

    try:
        # Create virtual file list
        virtual_files = [
            VirtualFile(
                name=f['name'],
                size=f['size'],
                checksum=f['checksum'],
                file_index=f['file_index'],
                transfer_id=transfer_id
            )
            for f in files
        ]

        # Create data object
        data_object = VirtualFileDataObject(virtual_files, download_callback)

        # Wrap as COM object
        wrapped = WrapObject(data_object, pythoncom.IID_IDataObject)

        # IMPORTANT: Keep references to prevent garbage collection!
        # Without this, Explorer's paste will fail because the objects are gone
        _active_clipboard_data['data_object'] = data_object
        _active_clipboard_data['wrapped'] = wrapped
        _active_clipboard_data['download_callback'] = download_callback

        # Set on clipboard (must be done from main thread or STA)
        pythoncom.OleInitialize()
        pythoncom.OleSetClipboard(wrapped)

        logger.info(f"Set {len(files)} virtual files on clipboard")
        return True

    except Exception as e:
        logger.error(f"Failed to set virtual clipboard: {e}")
        return False


def clear_virtual_clipboard():
    """Clear virtual files from clipboard"""
    global _active_clipboard_data

    if not HAS_PYWIN32:
        return

    try:
        pythoncom.OleSetClipboard(None)

        # Clear references
        _active_clipboard_data['data_object'] = None
        _active_clipboard_data['wrapped'] = None
        _active_clipboard_data['download_callback'] = None
    except Exception as e:
        logger.error(f"Failed to clear clipboard: {e}")
