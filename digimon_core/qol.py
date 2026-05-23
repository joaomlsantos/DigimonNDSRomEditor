"""QoL byte-patches applied at save time.

Each patch is a small idempotent mutation of the output ROM bytes. They are
applied *after* `RomSession.serialize_all()` writes the user-edited model
state, so they sit on top of normal edits — never the other way around.

Parameter-style patches (movement speed, scan rate) write absolute values
directly into the target ARM immediate byte — no multiplier semantics. The
defaults in this dataclass match the vanilla Dusk/Dawn US bytes; the session
overwrites them with whatever's actually in the loaded ROM at parse time so
the editor displays the real current value.

Wild-encounter rates and farm EXP gains live in the regular editors (Wild
Encounters, Farm Terrains), since they're direct model fields — no QoL
multiplier needed.

Ported from DWDDRandomizer's `qol_script.py`. Logic kept faithful; defaults
shift to "all off, vanilla parameters" since the editor is data-editing-first
and QoL is opt-in here.
"""
from __future__ import annotations

import binascii
from dataclasses import dataclass

from . import constants


@dataclass
class QolSettings:
    # boolean toggles
    fast_text: bool = False
    fast_movement: bool = False
    expand_player_name: bool = False
    fast_scan: bool = False
    unlock_exclusive_areas: bool = False
    improve_battle_performance: bool = False
    # parameters — only consulted when the matching toggle is on. Defaults
    # are the vanilla Dusk/Dawn US byte values; RomSession overwrites them
    # with what's actually in the loaded ROM so the editor shows the real
    # current value (1..255 — single-byte ARM immediate at the offset).
    movement_speed: int = 2
    scan_rate: int = 15


def apply_qol_patches(rom_data: bytearray, version: str, settings: QolSettings) -> None:
    """Apply every enabled patch in-place onto rom_data."""
    if settings.fast_text:
        _apply_text_speed(rom_data, version)
    if settings.fast_movement:
        _apply_movement_speed(rom_data, version, settings.movement_speed)
    if settings.expand_player_name:
        _apply_player_name(rom_data, version)
    if settings.fast_scan:
        _apply_scan_rate(rom_data, version, settings.scan_rate)
    if settings.unlock_exclusive_areas:
        _apply_exclusive_areas(rom_data, version)
    if settings.improve_battle_performance:
        _apply_battle_performance(rom_data, version)


def _apply_text_speed(rom: bytearray, version: str) -> None:
    offset = constants.TEXT_SPEED_OFFSET[version]
    rom[offset:offset + 4] = binascii.unhexlify("030010e3")


def _apply_movement_speed(rom: bytearray, version: str, new_speed: int) -> None:
    offset = constants.MOVEMENT_SPEED_OFFSET[version]
    # The byte at this offset is the immediate of a MOV r1, #imm ARM instruction
    # (vanilla 0x02); only the low byte is meaningful — anything wider would
    # corrupt the opcode/rotate bytes. Clamp 1..255 to match the widget cap.
    rom[offset] = max(1, min(255, new_speed))


def _apply_player_name(rom: bytearray, version: str) -> None:
    for offset, value in constants.PLAYERNAME_EXTENSION_ADDRESSES[version].items():
        _write_le(rom, value, offset, 4)


def _apply_scan_rate(rom: bytearray, version: str, new_rate: int) -> None:
    offset = constants.BASE_SCAN_RATE_OFFSET[version]
    # Single byte at the ARM immediate of an RSB instruction (vanilla 0x0F).
    rom[offset] = max(1, min(255, new_rate))


def _apply_exclusive_areas(rom: bytearray, version: str) -> None:
    for offset, value, _description in constants.VERSION_EXCLUSIVE_AREA_UNLOCKS[version]:
        _write_le(rom, value, offset, 2)


def _apply_battle_performance(rom: bytearray, version: str) -> None:
    for addr, new_delay in constants.BATTLE_FRAME_ADDRESSES[version].items():
        rom[addr] = new_delay


def _write_le(rom: bytearray, value: int, offset: int, width: int) -> None:
    rom[offset:offset + width] = int(value).to_bytes(width, byteorder="little")
