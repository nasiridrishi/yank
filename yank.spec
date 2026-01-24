# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for Yank clipboard sync
Builds standalone executables for Windows, macOS, and Linux
"""
import sys
import platform

block_cipher = None

# Platform detection
PLATFORM = platform.system()

# Hidden imports needed by the application
hiddenimports = [
    'zeroconf',
    'zeroconf._utils.ipaddress',
    'zeroconf._handlers.answers',
    'cryptography',
    'cryptography.hazmat.primitives.ciphers.aead',
    'PIL',
    'PIL.Image',
    'PIL.PngImagePlugin',
    'PIL.JpegImagePlugin',
    'json',
    'hashlib',
    'threading',
    'socket',
    'struct',
    'logging',
]

# Platform-specific hidden imports
if PLATFORM == 'Windows':
    hiddenimports += [
        'win32clipboard',
        'win32con',
        'win32api',
        'win32gui',
        'pywintypes',
        'ctypes',
        'ctypes.wintypes',
    ]
elif PLATFORM == 'Darwin':
    hiddenimports += [
        'objc',
        'AppKit',
        'Foundation',
        'Cocoa',
        'PyObjCTools',
    ]
elif PLATFORM == 'Linux':
    hiddenimports += [
        'gi',
        'gi.repository.Gtk',
        'gi.repository.Gdk',
        'gi.repository.GdkPixbuf',
        'gi.repository.GLib',
    ]

a = Analysis(
    ['src/yank/main.py'],
    pathex=['src'],
    binaries=[],
    datas=[
        ('src/yank/config.py', 'yank'),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'pandas',
        'scipy',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# Platform-specific executable settings
if PLATFORM == 'Darwin':
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='yank',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
elif PLATFORM == 'Windows':
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='yank',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        icon=None,
    )
else:  # Linux
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name='yank',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
    )
