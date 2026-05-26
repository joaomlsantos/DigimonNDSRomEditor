# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller build spec for the Digimon NDS ROM Editor (Windows).
#
# Build:   pyinstaller DigimonNDSRomEditor.spec
# Output:  dist/DigimonNDSRomEditor/DigimonNDSRomEditor.exe  (+ supporting files)
#
# Layout mirrors DWDDRandomizer.spec — one-folder COLLECT (not onefile) so the
# bundled PySide6 platform plugins load reliably and so cold-start isn't paying
# the onefile self-extract tax every launch.

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    # Bundle the app icon so `QApplication.setWindowIcon` can find it at
    # runtime — the `icon=` field below only stamps the EXE's file icon
    # (what Explorer renders for the file), not the window/taskbar icon.
    datas=[('public/editor.ico', 'public')],
    # PySide6 hooks ship with PyInstaller; nothing else in the project pulls
    # in dynamic imports that the static analyser would miss.
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Trim Qt modules the editor never touches — keeps the dist smaller.
    # If a future feature pulls one of these in (e.g. QtMultimedia for sound
    # previews) drop it from this list.
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
    # GUI app — no console window on Windows.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='public/editor.ico',
    version='version_info.txt',
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
