# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for the Digimon NDS ROM Editor (Linux).
#
# Build:   pyinstaller DigimonNDSRomEditor_linux.spec
# Output:  dist/DigimonNDSRomEditor/DigimonNDSRomEditor  (+ supporting files)
#
# Mirrors DigimonNDSRomEditor.spec; Linux-specific deltas:
#   - icon would be a PNG (Linux desktop entries reference image files
#     rather than the Windows .ico/.exe resource scheme), commented out
#     until an asset exists.
#   - no `version=` line (that's a Windows PE resource, not a Linux thing).

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'PySide6.QtNetwork',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebChannel',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DRender',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtSensors',
        'PySide6.QtSerialPort',
        'PySide6.QtBluetooth',
        'PySide6.QtPositioning',
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DigimonNDSRomEditor',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='public/editor.png',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DigimonNDSRomEditor',
)
