"""ROM I/O + header-based version detection.

Extracted from DWDDRandomizer's `qol_script.DigimonROM`, minus the QoL byte
patches — those stay with the randomizer.
"""
import os
from typing import Optional

from . import constants


class UnsupportedRomError(ValueError):
    pass


class UnknownRomError(ValueError):
    pass


def loadRom(fpath: str) -> bytearray:
    with open(fpath, "rb") as f:
        return bytearray(f.read())


def writeRom(rom_data: bytearray, fpath: str) -> None:
    with open(fpath, "wb") as f:
        f.write(rom_data)


def detectVersion(rom_data: bytearray, fpath: Optional[str] = None) -> str:
    """Match the 32-byte NDS header against known DWDD ROMs.

    Returns the version key (e.g. "DUSK_US"). Raises UnknownRomError if the
    header is unrecognized, UnsupportedRomError if it's known but not
    implemented in `constants.IMPLEMENTED_HEADERS`.
    """
    header_value = rom_data[:0x20].hex()
    rom_version = constants.HEADER_VALUES.get(header_value)
    if rom_version is None:
        name = os.path.basename(fpath) if fpath else "<rom>"
        raise UnknownRomError(f'Game not recognized. Please check your rom (file "{name}").')
    if rom_version not in constants.IMPLEMENTED_HEADERS:
        raise UnsupportedRomError(
            f"Support for {rom_version} not implemented yet. "
            f"Supported ROMs: {constants.IMPLEMENTED_HEADERS}"
        )
    return rom_version


class RomFile:
    """Convenience wrapper around (rom_data, version, source path)."""

    fpath: Optional[str]
    rom_data: bytearray
    version: str

    def __init__(self, fpath: Optional[str] = None, rom_data: Optional[bytearray] = None):
        if rom_data is None and fpath is None:
            raise ValueError("RomFile requires either fpath or rom_data")
        self.fpath = fpath
        self.rom_data = rom_data if rom_data is not None else loadRom(fpath)
        self.version = detectVersion(self.rom_data, fpath)

    def write(self, fpath: str) -> None:
        writeRom(self.rom_data, fpath)
