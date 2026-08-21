"""Dependency resolution (required/optional/translation) and translation-mod
detection.

No dedicated "translation" relation exists in the CurseForge API — only
embeddedLibrary/incompatible/optionalDependency/requiredDependency — so
translation detection always combines multiple weak signals, never a single
automatic match:
  1. description keywords + a CurseForge URL pointing at an installed mod
     (strongest signal)
  2. name/slug markers like [FR], _VF, "- French Translation" (weak,
     pre-filter only)
  3. STBL Group/Instance ID comparison via package_parser.py (strong
     technical confirmation, on-demand only — never a full-library scan)

Every suggested link requires explicit user confirmation: detection
functions only ever produce confidence='suggested' rows; confirm_dependency()
is the only path to 'confirmed' for an auto-detected link.

This module only depends on package_parser.py (a "leaf" module) so that
mod_manager.py can depend on it (to block enable() on unresolved required
dependencies) without a circular import.
"""

from __future__ import annotations

import difflib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import package_parser

DEPENDENCY_TYPES = ("required", "optional", "translation")
CONFIDENCE_LEVELS = ("confirmed", "suggested")

_TRANSLATION_KEYWORDS = ("traduction", "translation", "übersetzung", "traducción")
_CURSEFORGE_URL_RE = re.compile(r"https?://(?:www\.)?curseforge\.com/sims4/mods/([a-z0-9-]+)", re.IGNORECASE)
_NAME_MARKER_RE = re.compile(
    r"\[\s*(?:fr|french|vf|traduction)\s*\]"
    r"|\(\s*(?:fr|french|vf|traduction)\s*\)"
    r"|[-_]\s*(?:french\s*translation|traduction(?:\s*fran[çc]aise)?|vf)\b",
    re.IGNORECASE,
)
_NAME_MATCH_CUTOFF = 0.7
_SMALL_PACKAGE_MAX_BYTES = 2_000_000  # STBL-only translation packages are typically tiny


class DependencyError(Exception):
    pass


class UnresolvedRequiredDependencyError(DependencyError):
    def __init__(self, mod_id: str, missing: list["DependencyLink"]) -> None:
        self.mod_id = mod_id
        self.missing = missing
        names = ", ".join(
            link.depends_on_mod_id or f"curseforge:{link.depends_on_curseforge_id}" for link in missing
        )
        super().__init__(f"Cannot enable '{mod_id}': unresolved required dependency on {names}")


@dataclass(frozen=True)
class DependencyLink:
    id: int
    mod_id: str
    depends_on_mod_id: str | None
    depends_on_curseforge_id: int | None
    dependency_type: str
    confidence: str
    mandatory: bool


@dataclass(frozen=True)
class DetectionSignal:
    source_mod_id: str
    method: str  # 'description' | 'name_heuristic' | 'stbl_comparison'
    strength: str  # 'weak' | 'strong'


# --- CRUD + resolution -------------------------------------------------------


def add_dependency(
    mod_id: str,
    *,
    conn: sqlite3.Connection,
    dependency_type: str,
    depends_on_mod_id: str | None = None,
    depends_on_curseforge_id: int | None = None,
    confidence: str = "confirmed",
    mandatory: bool = True,
) -> int:
    if dependency_type not in DEPENDENCY_TYPES:
        raise DependencyError(f"Invalid dependency_type: {dependency_type}")
    if confidence not in CONFIDENCE_LEVELS:
        raise DependencyError(f"Invalid confidence: {confidence}")
    if depends_on_mod_id is None and depends_on_curseforge_id is None:
        raise DependencyError("Must specify depends_on_mod_id or depends_on_curseforge_id")

    cursor = conn.execute(
        "INSERT INTO dependencies "
        "(mod_id, depends_on_mod_id, depends_on_curseforge_id, dependency_type, confidence, mandatory) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (mod_id, depends_on_mod_id, depends_on_curseforge_id, dependency_type, confidence, int(mandatory)),
    )
    conn.commit()
    return cursor.lastrowid


def confirm_dependency(dependency_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE dependencies SET confidence = 'confirmed' WHERE id = ?", (dependency_id,))
    conn.commit()


def reject_dependency(dependency_id: int, conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM dependencies WHERE id = ?", (dependency_id,))
    conn.commit()


def _row_to_link(row: sqlite3.Row) -> DependencyLink:
    return DependencyLink(
        id=row["id"],
        mod_id=row["mod_id"],
        depends_on_mod_id=row["depends_on_mod_id"],
        depends_on_curseforge_id=row["depends_on_curseforge_id"],
        dependency_type=row["dependency_type"],
        confidence=row["confidence"],
        mandatory=bool(row["mandatory"]),
    )


def list_dependencies(mod_id: str, conn: sqlite3.Connection) -> list[DependencyLink]:
    rows = conn.execute("SELECT * FROM dependencies WHERE mod_id = ?", (mod_id,)).fetchall()
    return [_row_to_link(r) for r in rows]


def _is_resolved(link: DependencyLink, conn: sqlite3.Connection) -> bool:
    if link.depends_on_mod_id is not None:
        target = conn.execute(
            "SELECT active FROM mods WHERE id = ?", (link.depends_on_mod_id,)
        ).fetchone()
    elif link.depends_on_curseforge_id is not None:
        target = conn.execute(
            "SELECT active FROM mods WHERE curseforge_id = ?", (link.depends_on_curseforge_id,)
        ).fetchone()
    else:
        return False
    return target is not None and bool(target["active"])


def unresolved_dependencies(
    mod_id: str, conn: sqlite3.Connection, *, dependency_type: str | None = None
) -> list[DependencyLink]:
    links = list_dependencies(mod_id, conn)
    if dependency_type is not None:
        links = [link for link in links if link.dependency_type == dependency_type]
    return [link for link in links if not _is_resolved(link, conn)]


def check_required(mod_id: str, conn: sqlite3.Connection) -> None:
    """Raises UnresolvedRequiredDependencyError unless every 'required'
    dependency of mod_id is currently satisfied by an active mod. Optional
    dependencies never block — callers should warn on those separately via
    unresolved_dependencies(mod_id, conn, dependency_type="optional")."""
    missing = unresolved_dependencies(mod_id, conn, dependency_type="required")
    if missing:
        raise UnresolvedRequiredDependencyError(mod_id, missing)


# --- translation detection ---------------------------------------------------


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def description_signal(description: str, conn: sqlite3.Connection) -> DetectionSignal | None:
    """Strongest signal: a translation keyword plus a CurseForge URL that
    matches an already-installed mod's stored links."""
    if not description:
        return None
    if not any(keyword in description.lower() for keyword in _TRANSLATION_KEYWORDS):
        return None
    for match in _CURSEFORGE_URL_RE.finditer(description):
        slug = match.group(1)
        target = conn.execute("SELECT id FROM mods WHERE links LIKE ?", (f"%{slug}%",)).fetchone()
        if target is not None:
            return DetectionSignal(source_mod_id=target["id"], method="description", strength="strong")
    return None


def name_heuristic_signal(candidate_name: str, conn: sqlite3.Connection) -> DetectionSignal | None:
    """Weak pre-filter: strips common translation markers ([FR], _VF, '-
    French Translation', ...) and looks for an installed mod whose name
    closely matches what remains. Never treat this alone as confirmation."""
    stripped = _NAME_MARKER_RE.sub("", candidate_name).strip(" -_")
    if not stripped or _normalize(stripped) == _normalize(candidate_name):
        return None  # no translation marker found in the name at all

    target = _normalize(stripped)
    rows = conn.execute("SELECT id, name FROM mods").fetchall()
    by_normalized = {_normalize(row["name"]): row for row in rows}
    matches = difflib.get_close_matches(target, by_normalized.keys(), n=1, cutoff=_NAME_MATCH_CUTOFF)
    if not matches:
        return None
    row = by_normalized[matches[0]]
    return DetectionSignal(source_mod_id=row["id"], method="name_heuristic", strength="weak")


def is_translation_candidate(mod_id: str, conn: sqlite3.Connection) -> bool:
    """Cheap technical pre-check before attempting a full STBL comparison:
    the mod must be .package-only (no .ts4script) and unusually small —
    uses tracked mod_files, never a fresh filesystem scan."""
    files = conn.execute("SELECT extension, size FROM mod_files WHERE mod_id = ?", (mod_id,)).fetchall()
    if not files:
        return False
    if any(f["extension"] == ".ts4script" for f in files):
        return False
    total_size = sum(f["size"] or 0 for f in files)
    return total_size <= _SMALL_PACKAGE_MAX_BYTES


def stbl_signal(candidate_mod_id: str, source_mod_id: str, conn: sqlite3.Connection) -> DetectionSignal | None:
    """On-demand technical confirmation: candidate must pass
    is_translation_candidate() and share at least one STBL Group/Instance ID
    with the source mod's .package file(s)."""
    if not is_translation_candidate(candidate_mod_id, conn):
        return None

    candidate_row = conn.execute(
        "SELECT library_path FROM mods WHERE id = ?", (candidate_mod_id,)
    ).fetchone()
    source_row = conn.execute("SELECT library_path FROM mods WHERE id = ?", (source_mod_id,)).fetchone()
    if candidate_row is None or source_row is None:
        return None

    candidate_packages = _read_packages(candidate_mod_id, Path(candidate_row["library_path"]), conn)
    if not candidate_packages or not all(p.is_stbl_only for p in candidate_packages):
        return None

    source_packages = _read_packages(source_mod_id, Path(source_row["library_path"]), conn)

    for candidate_package in candidate_packages:
        for source_package in source_packages:
            if package_parser.matching_stbl_keys(candidate_package, source_package):
                return DetectionSignal(
                    source_mod_id=source_mod_id, method="stbl_comparison", strength="strong"
                )
    return None


def _read_packages(
    mod_id: str, library_path: Path, conn: sqlite3.Connection
) -> list[package_parser.PackageInfo]:
    rows = conn.execute(
        "SELECT relative_path FROM mod_files WHERE mod_id = ? AND extension = '.package'", (mod_id,)
    ).fetchall()
    return [package_parser.read_package(library_path / row["relative_path"]) for row in rows]


def detect_translation_signals(candidate_mod_id: str, conn: sqlite3.Connection) -> list[DetectionSignal]:
    """Runs every applicable signal for one candidate mod against the
    library. Never writes to the DB — the caller (UI) decides whether to
    call suggest_translation() based on what came back."""
    row = conn.execute(
        "SELECT name, short_description, full_description FROM mods WHERE id = ?", (candidate_mod_id,)
    ).fetchone()
    if row is None:
        return []

    signals: list[DetectionSignal] = []

    description = " ".join(filter(None, [row["short_description"], row["full_description"]]))
    desc_signal = description_signal(description, conn)
    if desc_signal is not None:
        signals.append(desc_signal)

    name_signal = name_heuristic_signal(row["name"], conn)
    if name_signal is not None:
        signals.append(name_signal)
        confirmation = stbl_signal(candidate_mod_id, name_signal.source_mod_id, conn)
        if confirmation is not None:
            signals.append(confirmation)

    return signals


def suggest_translation(candidate_mod_id: str, source_mod_id: str, conn: sqlite3.Connection) -> int:
    """Records a 'suggested' translation link — never 'confirmed' until the
    user explicitly approves it via confirm_dependency()."""
    return add_dependency(
        candidate_mod_id,
        conn=conn,
        dependency_type="translation",
        depends_on_mod_id=source_mod_id,
        confidence="suggested",
        mandatory=False,
    )
