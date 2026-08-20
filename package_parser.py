"""DBPF (.package) header and resource-index reader.

Targets DBPF 2.1 / index format 7 — the standard, uncompressed-index layout
used by The Sims 3 and The Sims 4 tooling (as documented by the modding
community, e.g. Sims4Tools/s4pi; DBPF is not officially documented by EA).
The optional "constant field" index compression that some tools can opt into
is not handled here — this has not been validated against real game files
(none are available in this environment); revisit if real .package fixtures
turn up index entries that don't parse cleanly.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

MAGIC = b"DBPF"
HEADER_SIZE = 96

# The Sims 4 String Table resource type — a de facto standard within the
# modding community, not documented by EA.
RESOURCE_TYPE_STBL = 0x220557DA

_ENTRY_STRUCT = struct.Struct("<7I2H")
INDEX_ENTRY_SIZE = _ENTRY_STRUCT.size


class DbpfError(Exception):
    """Raised when a file isn't a valid/readable DBPF package."""


@dataclass(frozen=True)
class ResourceEntry:
    type: int
    group: int
    instance: int
    offset: int
    file_size: int
    mem_size: int
    compressed: bool

    @property
    def is_stbl(self) -> bool:
        return self.type == RESOURCE_TYPE_STBL


@dataclass(frozen=True)
class PackageInfo:
    path: Path
    resources: list[ResourceEntry]

    @property
    def is_stbl_only(self) -> bool:
        return len(self.resources) > 0 and all(r.is_stbl for r in self.resources)

    @property
    def stbl_keys(self) -> set[tuple[int, int]]:
        """(group, instance) pairs for this package's STBL resources."""
        return {(r.group, r.instance) for r in self.resources if r.is_stbl}


@dataclass(frozen=True)
class _Header:
    index_entry_count: int
    index_offset: int


def _parse_header(data: bytes) -> _Header:
    if data[:4] != MAGIC:
        raise DbpfError(f"Not a DBPF file (bad magic: {data[:4]!r})")
    (index_entry_count,) = struct.unpack_from("<I", data, 0x24)
    (index_offset,) = struct.unpack_from("<I", data, 0x40)
    return _Header(index_entry_count=index_entry_count, index_offset=index_offset)


def _parse_index(index_bytes: bytes, entry_count: int) -> list[ResourceEntry]:
    entries = []
    for i in range(entry_count):
        start = i * INDEX_ENTRY_SIZE
        chunk = index_bytes[start : start + INDEX_ENTRY_SIZE]
        if len(chunk) < INDEX_ENTRY_SIZE:
            raise DbpfError("Truncated index table")
        (
            rtype,
            group,
            instance_hi,
            instance_lo,
            offset,
            file_size,
            mem_size,
            compression_type,
            _committed,
        ) = _ENTRY_STRUCT.unpack(chunk)
        entries.append(
            ResourceEntry(
                type=rtype,
                group=group,
                instance=(instance_hi << 32) | instance_lo,
                offset=offset,
                file_size=file_size,
                mem_size=mem_size,
                compressed=compression_type != 0,
            )
        )
    return entries


def read_package(path: Path) -> PackageInfo:
    with path.open("rb") as f:
        header_bytes = f.read(HEADER_SIZE)
        if len(header_bytes) < HEADER_SIZE:
            raise DbpfError(f"{path}: file too small to be a valid DBPF package")
        header = _parse_header(header_bytes)
        f.seek(header.index_offset)
        index_bytes = f.read(header.index_entry_count * INDEX_ENTRY_SIZE)
    resources = _parse_index(index_bytes, header.index_entry_count)
    return PackageInfo(path=path, resources=resources)


def matching_stbl_keys(a: PackageInfo, b: PackageInfo) -> set[tuple[int, int]]:
    """(group, instance) STBL keys present in both packages — a strong signal
    that one is a translation of the other. Used by dependencies.py."""
    return a.stbl_keys & b.stbl_keys
