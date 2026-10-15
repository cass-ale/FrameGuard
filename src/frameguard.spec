# -*- mode: python ; coding: utf-8 -*-
r"""
FrameGuard Desktop -- PyInstaller Spec File (Windows .exe + macOS .app)

Key principles:
- NO UPX (causes python312.dll 0xC0000005 crashes on Windows)
- Standard PyInstaller hooks collect only the PySide6 modules the app imports
- Folder mode output, wrapped in BUNDLE() on macOS
- All paths resolved from SPECPATH, never the shell's CWD

Do not run PyInstaller directly. Use the build scripts, which also handle the
clean virtualenv, version validation, icon generation, tool staging, code
signing and the smoke test:

    Windows:  build_installer.bat
    macOS:    ./build_mac.sh

Output:
    Windows:  dist\FrameGuard\FrameGuard.exe  ->  Output\FrameGuardInstaller.exe
    macOS:    local cache/dist/FrameGuard.app ->  Output/FrameGuard.dmg
"""

import sys
import os

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
APP_NAME = "FrameGuard"
APP_VERSION = "0.6.2"
ENTRY_POINT = "main.py"
# This is an opaque identifier, not branding, but it IS the app's macOS
# identity (bundle identity, .fg UTI ownership, preferences domain), so
# changing it makes macOS treat FrameGuard as a different app. It carried
# a historical "meta.tpgxr" reverse-DNS until 2026-10-14, when it was
# migrated to the publisher's own domain before the first public release,
# at the developer's direction. The .fg UTI and the LaunchAgent label
# migrated with it; tests/test_macos.py pins the current values.
BUNDLE_ID = "com.caprilaria.frameguard"

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

# SPECPATH is already the directory containing the .spec file; taking dirname()
# of it pointed pathex one level too high.
spec_dir = os.path.abspath(SPECPATH)


def _asset(*parts):
    """Absolute path to a repo asset, independent of the shell's CWD."""
    return os.path.join(spec_dir, *parts)


# macOS needs .icns; Windows needs .ico. Resolved absolutely so building from
# another directory doesn't silently drop the icon.
ICON_FILE = _asset("assets", "frameguard.icns" if IS_MAC else "frameguard.ico")
if not os.path.isfile(ICON_FILE):
    if IS_MAC:
        raise SystemExit(
            f"Missing {ICON_FILE}.\n"
            f"Generate it first:  python assets/make_icns.py"
        )
    print(f"[spec] WARNING: icon not found at {ICON_FILE}; building without one.")
    ICON_FILE = None

# ---------------------------------------------------------------------------
# Windows Version Info
# ---------------------------------------------------------------------------
version_info = None
if sys.platform == "win32":
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo,
        StringTable, StringStruct, VarFileInfo, VarStruct,
    )
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=(0, 6, 2, 0),
            prodvers=(0, 6, 2, 0),
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
        ),
        kids=[
            StringFileInfo([
                StringTable('040904B0', [
                    StringStruct('CompanyName', 'Capri la Ria'),
                    StringStruct('FileDescription', 'FrameGuard Desktop - Video QA Tool'),
                    StringStruct('FileVersion', '0.6.2.0'),
                    StringStruct('InternalName', 'FrameGuard'),
                    StringStruct('OriginalFilename', 'FrameGuard.exe'),
                    StringStruct('ProductName', 'FrameGuard Desktop'),
                    StringStruct('ProductVersion', '0.6.2.0'),
                    StringStruct('LegalCopyright', 'Copyright © 2026 Cassian Aléria'),
                ])
            ]),
            VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
        ]
    )

# ---------------------------------------------------------------------------
# Data Files
# ---------------------------------------------------------------------------
datas = [
    (_asset("assets"), "assets"),    # Application icons and logos
    (_asset("presets"), "presets"),  # Bundled scan presets (4K30/4K60/1080p30/VR360)
]
# Written by the build scripts; lets the About dialog report when this build
# was made. Optional so the spec still works for an ad-hoc local build.
_stamp = os.environ.get("FRAMEGUARD_BUILD_STAMP_PATH")
if _stamp:
    _stamp = os.path.abspath(_stamp)
else:
    _stamp = _asset("frameguard", "_build_stamp.json")
if os.path.isfile(_stamp):
    datas.append((_stamp, "frameguard"))

# fpdf2 font data
from PyInstaller.utils.hooks import collect_data_files
fpdf_datas = collect_data_files('fpdf')
datas.extend(fpdf_datas)

# ---------------------------------------------------------------------------
# Binaries
# ---------------------------------------------------------------------------
binaries = []

# psutil platform-specific extensions
from PyInstaller.utils.hooks import collect_dynamic_libs
psutil_libs = collect_dynamic_libs('psutil')
binaries.extend(psutil_libs)

# ---------------------------------------------------------------------------
# Hidden Imports
# ---------------------------------------------------------------------------
hidden_imports = [
    # FrameGuard package - ALL modules explicitly listed
    'frameguard',
    'frameguard.gui',
    'frameguard.core',
    'frameguard.tools',
    'frameguard.deltaupdate',
    'frameguard.design',
    'frameguard.icons',
    'frameguard.manifest',
    'frameguard.updatesource',
    'frameguard.theme',
    'frameguard.worker',
    'frameguard.report',
    'frameguard.watchfolder',
    'frameguard.updater',
    'frameguard.presets',
    
    # Dependencies
    'fpdf',
    'fpdf.enums',
    'fpdf.fonts',
    'fpdf.image_parsing',
    'psutil',
    
    # Standard library (dynamically imported by our code)
    'multiprocessing',
    'json',
    'csv',
    'hashlib',
    'subprocess',
    'threading',
    'urllib',
    'urllib.request',
    'pathlib',
    'shutil',
    'tempfile',
    'webbrowser',
    'ctypes',
    'logging',
    'signal',
    'locale',
    'encodings',
    'encodings.utf_8',
    'encodings.ascii',
    'encodings.latin_1',
    'encodings.cp1252',
]

# Platform-specific
if sys.platform == 'win32':
    hidden_imports.extend([
        'psutil._pswindows',
        'ctypes.wintypes',
        'multiprocessing.freeze_support',
    ])

# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
a = Analysis(
    [ENTRY_POINT],
    pathex=[spec_dir],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden_imports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim fat — modules we definitely don't use
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'PIL', 'Pillow',
        'tkinter', '_tkinter',
        'test', 'tests',
        'IPython', 'notebook', 'jupyter',
        'setuptools', 'pkg_resources', 'pip',
    ],
    noarchive=False,
    optimize=0,  # Don't optimize — preserves tracebacks for debugging
)

# ---------------------------------------------------------------------------
# PYZ (compressed Python modules archive)
# ---------------------------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ---------------------------------------------------------------------------
# EXE
# ---------------------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # Folder mode for Inno Setup / .app BUNDLE
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # CRITICAL: UPX corrupts python312.dll
    console=False,      # Windowed — no console
    disable_windowed_traceback=False,  # Show traceback dialog on crash
    # Left False deliberately: FrameGuard installs a QEvent.FileOpen filter
    # instead, which also handles a .fg opened while the app is already
    # running. argv_emulation only covers the cold-launch case.
    argv_emulation=False,
    # The release builder exports the native host architecture explicitly.
    # FrameGuard ships separate arm64 and Intel artifacts; universal2 is not a
    # supported release target.
    target_arch=os.environ.get("FRAMEGUARD_TARGET_ARCH") or None,
    # Signing identity comes from the environment so build_mac.sh and direct
    # PyInstaller runs agree. Ad-hoc ("-") for internal distribution; a real
    # Developer ID for Gatekeeper-clean builds. None here means "unsigned":
    # build_mac.sh signs after staging so the signature covers ffmpeg too.
    codesign_identity=os.environ.get("FRAMEGUARD_CODESIGN_IDENTITY") or None,
    entitlements_file=(
        _asset("entitlements.plist")
        if os.path.isfile(_asset("entitlements.plist")) else None
    ),
    icon=ICON_FILE,
    version=version_info,
)

# ---------------------------------------------------------------------------
# COLLECT (folder mode output → dist/FrameGuard/)
# ---------------------------------------------------------------------------
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,          # CRITICAL: No UPX on any collected files
    upx_exclude=[],
    name=APP_NAME,
)

# ---------------------------------------------------------------------------
# BUNDLE (macOS .app)
#
# Without this PyInstaller emits dist/FrameGuard/FrameGuard — a bare Unix
# binary with no Info.plist, no dock icon, no file associations and nothing to
# code-sign. build_mac.sh writes the .app to a local cache because DriveFS
# cannot preserve Qt framework symlinks; Windows retains dist/FrameGuard.
# ---------------------------------------------------------------------------
if IS_MAC:
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=ICON_FILE,
        bundle_identifier=BUNDLE_ID,
        version=APP_VERSION,
        info_plist={
            "CFBundleName": APP_NAME,
            "CFBundleDisplayName": "FrameGuard Desktop",
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundleIdentifier": BUNDLE_ID,
            "CFBundlePackageType": "APPL",
            "NSHumanReadableCopyright": "Copyright © 2026 Cassian Aléria",

            # Retina. Without this the whole UI renders at 1x and looks soft.
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "12.0",
            "LSApplicationCategoryType": "public.app-category.video",

            # Qt draws its own UI; let the system light/dark setting through
            # rather than forcing Aqua.
            "NSRequiresAquaSystemAppearance": False,

            # --- .fg preset association (macOS equivalent of the Windows
            # registry entries in installer.iss). Finder needs BOTH the
            # exported UTI and the document type to show our icon.
            "UTExportedTypeDeclarations": [{
                "UTTypeIdentifier": "com.caprilaria.frameguard.preset",
                "UTTypeDescription": "FrameGuard Scan Preset",
                "UTTypeConformsTo": ["public.json", "public.data"],
                "UTTypeIconFile": "frameguard.icns",
                "UTTypeTagSpecification": {
                    "public.filename-extension": ["fg"],
                    "public.mime-type": ["application/x-frameguard-preset"],
                },
            }],
            "CFBundleDocumentTypes": [{
                "CFBundleTypeName": "FrameGuard Scan Preset",
                "CFBundleTypeExtensions": ["fg"],
                "CFBundleTypeIconFile": "frameguard.icns",
                "CFBundleTypeRole": "Editor",
                "LSHandlerRank": "Owner",
                "LSItemContentTypes": ["com.caprilaria.frameguard.preset"],
            }],
        },
    )
