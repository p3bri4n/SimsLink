"""Shared pytest fixtures: synthetic DBPF file builder, temp Config, temp DB."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

import db as db_module
from config import Config

DBPF_MAGIC = b"DBPF"
_HEADER_SIZE = 96
_ENTRY_STRUCT = struct.Struct("<7I2H")


def write_dbpf(path: Path, resources: list[tuple[int, int, int, bytes]]) -> None:
    """Writes a minimal synthetic DBPF file for tests.

    `resources` is a list of (type, group, instance, data) tuples. Mirrors the
    layout package_parser.py reads — see its module docstring for the caveat
    about which DBPF index format this covers.
    """
    index_offset = _HEADER_SIZE
    index_size = len(resources) * _ENTRY_STRUCT.size
    data_offset = index_offset + index_size

    header = bytearray(_HEADER_SIZE)
    header[0:4] = DBPF_MAGIC
    struct.pack_into("<I", header, 0x24, len(resources))
    struct.pack_into("<I", header, 0x2C, index_size)
    struct.pack_into("<I", header, 0x40, index_offset)

    index = bytearray()
    blob = bytearray()
    cursor = data_offset
    for rtype, group, instance, data in resources:
        instance_hi = (instance >> 32) & 0xFFFFFFFF
        instance_lo = instance & 0xFFFFFFFF
        index += _ENTRY_STRUCT.pack(
            rtype, group, instance_hi, instance_lo, cursor, len(data), len(data), 0, 1
        )
        blob += data
        cursor += len(data)

    path.write_bytes(bytes(header) + bytes(index) + bytes(blob))


@pytest.fixture
def dbpf_writer():
    return write_dbpf


@pytest.fixture
def app_config(tmp_path) -> Config:
    game_dir = tmp_path / "game"
    user_dir = tmp_path / "sims4user"
    mods_dir = user_dir / "Mods"
    library_dir = tmp_path / "library"
    for d in (game_dir, mods_dir, library_dir):
        d.mkdir(parents=True, exist_ok=True)
    return Config(
        sims4_game_dir=game_dir,
        sims4_mods_dir=mods_dir,
        sims4_user_dir=user_dir,
        library_dir=library_dir,
        curseforge_api_key=None,
        download_watch_dir=tmp_path / "downloads",
        game_version=None,
    )


@pytest.fixture
def conn(tmp_path):
    connection = db_module.init_db(tmp_path / "simslink.sqlite3")
    yield connection
    connection.close()
