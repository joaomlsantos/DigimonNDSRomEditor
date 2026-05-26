# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for the Digimon NDS ROM Editor (macOS).
#
# Build:   pyinstaller DigimonNDSRomEditor_macOS.spec
# Output:  dist/DigimonNDSRomEditor.app/  (macOS app bundle)
#
# Mirrors DigimonNDSRomEditor.spec; macOS-specific deltas:
#   - BUNDLE step replaces COLLECT to produce a .app instead of a folder.
#   - argv_emulation=True so files dropped on the .app are passed as argv
#     (matches DWDDRandomizer's macOS spec — useful even if the editor
#     doesn't consume argv yet, since it costs nothing to leave on).
#   - icon would be an .icns; commented out until an asset exists.
#   - target_arch=None builds for the host arch. Set to 'universal2' if/when
#     producing a fat binary for both Apple Silicon and Intel.
#   - codesign_identity / entitlements_file left None — sign at distribution
#     time, not build time.

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=[('public/editor.icns', 'public')],
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
    argv_emulation=True,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='public/editor.icns',
)
coll = BUNDLE(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='DigimonNDSRomEditor.app',
    icon='public/editor.icns',
)
