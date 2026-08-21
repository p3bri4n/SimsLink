"""lastException.txt parsing + automated crash-suspect analysis + bisection.

Only lastException.txt is ever parsed — lastCrash.txt is confirmed
unreadable/unusable even by the community (see CLAUDE.md), so no code here
touches it.

A LastException is diagnostic aid, never destructive automation: analyze()/
record_crash() only ever read mods and write to crash_log — they never
enable/disable/delete a mod, and an isolated crash never produces an
auto-delete suggestion on its own (that guarantee falls out of this module
simply never calling mod_manager.delete()).

Path-matching heuristic: a .ts4script traceback frame's `File "..."` format
for zipimport-loaded modules varies across Python/game versions (it
generally embeds the archive's filename, e.g.
".../Mods/<mod_id>/foo.ts4script/module.py"). Validated 2026-08-21 against
real lastException.txt files: mod-owned frames instead show up as relative
paths (e.g. ".\\WickedWhims_v185k\\turbolib2\\events\\objects.py" or bare
"lot51_core/utils/injection.pyc"), never the "inside a .ts4script archive"
shape originally guessed — matching on mod_id/file-stem substrings anywhere
in the frame path (rather than a specific path shape) turned out to be the
right call, since it's robust to that difference too.

File-format note (also only confirmed against real data on 2026-08-21):
lastException.txt is not a single raw traceback — it's an XML document
(`<root><report>...<desyncdata>TRACEBACK TEXT</desyncdata>...</report>...
</root>`) that can bundle several unrelated crash occurrences accumulated
over a play session in one file. parse_reports() splits those apart so each
occurrence gets analyzed and stored as its own crash_log row — merging them
would silently violate "never conclude from a single occurrence" by mixing
suspects from unrelated incidents into one row. Plain, non-XML text (as
used by this module's own test fixtures, and as a defensive fallback for
any future format change) is still accepted as a single occurrence.
"""

from __future__ import annotations

import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import mod_manager
from .config import Config

_FRAME_RE = re.compile(r'File "(?P<file>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?')

_KNOWN_LIBRARY_PATTERNS: tuple[tuple[re.Pattern, str], ...] = (
    (re.compile(r"sims4communitylib", re.IGNORECASE), "sims4communitylib"),
    (re.compile(r"ts4lib", re.IGNORECASE), "ts4lib"),
    (re.compile(r"xml_injector", re.IGNORECASE), "xml_injector"),
)


class CrashAnalyzerError(Exception):
    pass


@dataclass(frozen=True)
class TraceFrame:
    file_path: str
    line: int
    function: str | None


@dataclass(frozen=True)
class Suspect:
    mod_id: str
    confidence: str  # 'direct_trace' | 'pattern_match'
    reason: str


def parse_reports(raw_file_text: str) -> list[str]:
    """Splits a lastException.txt file's content into one raw traceback
    string per occurrence. The real file format is XML with a <report> per
    occurrence, each carrying its traceback in <desyncdata> (see module
    docstring); ElementTree's .text already resolves the entity-escaped
    content back to plain text, no manual unescaping needed. Falls back to
    treating the whole input as a single occurrence when it isn't that XML
    shape (plain text, unparseable, or no <desyncdata> found) — the file
    format could change again, and a single well-formed occurrence must
    never be silently dropped just because splitting failed."""
    try:
        root = ET.fromstring(raw_file_text)
    except ET.ParseError:
        return [raw_file_text]

    reports = [elem.text for elem in root.iter("desyncdata") if elem.text]
    return reports or [raw_file_text]


def parse_trace_frames(raw_exception: str) -> list[TraceFrame]:
    return [
        TraceFrame(file_path=m.group("file"), line=int(m.group("line")), function=m.group("func"))
        for m in _FRAME_RE.finditer(raw_exception)
    ]


def _mods_index(conn: sqlite3.Connection) -> list[tuple[str, list[str]]]:
    """[(mod_id, [tracked file stems]), ...] for every installed mod."""
    index = []
    for mod in conn.execute("SELECT id FROM mods").fetchall():
        files = conn.execute(
            "SELECT relative_path FROM mod_files WHERE mod_id = ?", (mod["id"],)
        ).fetchall()
        index.append((mod["id"], [Path(f["relative_path"]).stem for f in files]))
    return index


def match_direct_trace(frames: list[TraceFrame], conn: sqlite3.Connection) -> list[Suspect]:
    """Cross-references each frame's path against Mods/<mod_id>/... — a
    direct hit is the strongest possible signal."""
    index = _mods_index(conn)
    suspects: dict[str, Suspect] = {}
    for frame in frames:
        path_lower = frame.file_path.lower()
        for mod_id, stems in index:
            hit = mod_id.lower() in path_lower or any(
                stem.lower() in path_lower for stem in stems if stem
            )
            if hit and mod_id not in suspects:
                suspects[mod_id] = Suspect(
                    mod_id=mod_id,
                    confidence="direct_trace",
                    reason=f'File "{frame.file_path}", line {frame.line}',
                )
    return list(suspects.values())


def match_known_patterns(raw_exception: str, conn: sqlite3.Connection) -> list[Suspect]:
    """Fallback for when no suspect mod appears directly in the trace: looks
    for known broken-shared-library import signatures and, if that library
    is itself installed, flags it — a common, well-documented failure mode."""
    suspects: list[Suspect] = []
    for pattern, library_name in _KNOWN_LIBRARY_PATTERNS:
        if not pattern.search(raw_exception):
            continue
        rows = conn.execute(
            "SELECT id FROM mods WHERE lower(name) LIKE ? OR lower(id) LIKE ?",
            (f"%{library_name}%", f"%{library_name}%"),
        ).fetchall()
        for row in rows:
            suspects.append(
                Suspect(
                    mod_id=row["id"],
                    confidence="pattern_match",
                    reason=f"Known broken-import signature for '{library_name}'",
                )
            )
    return suspects


def analyze(raw_exception: str, conn: sqlite3.Connection) -> list[Suspect]:
    frames = parse_trace_frames(raw_exception)
    suspects = match_direct_trace(frames, conn)
    if not suspects:
        suspects = match_known_patterns(raw_exception, conn)
    return suspects


def record_crash(raw_exception: str, *, conn: sqlite3.Connection) -> int:
    """Analyzes and stores one lastException.txt occurrence. Read-only with
    respect to mods — never enables/disables/deletes anything, and an
    isolated occurrence is never treated as proof by itself."""
    suspects = analyze(raw_exception, conn)
    active_mods = [row["id"] for row in conn.execute("SELECT id FROM mods WHERE active = 1").fetchall()]
    cursor = conn.execute(
        "INSERT INTO crash_log "
        "(date, raw_last_exception, auto_suspect_mods, active_mods_snapshot, bisection_in_progress) "
        "VALUES (?, ?, ?, ?, 0)",
        (
            _now_iso(),
            raw_exception,
            json.dumps([s.__dict__ for s in suspects]),
            json.dumps(active_mods),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def record_crash_reports(raw_file_text: str, *, conn: sqlite3.Connection) -> list[int]:
    """Entry point for a freshly-read lastException.txt: splits it into its
    individual occurrences via parse_reports() and records each as its own
    crash_log row via record_crash(), returning one id per occurrence in
    file order."""
    return [record_crash(report, conn=conn) for report in parse_reports(raw_file_text)]


def get_suspects(crash_log_id: int, conn: sqlite3.Connection) -> list[Suspect]:
    row = conn.execute("SELECT auto_suspect_mods FROM crash_log WHERE id = ?", (crash_log_id,)).fetchone()
    if row is None or not row["auto_suspect_mods"]:
        return []
    return [Suspect(**s) for s in json.loads(row["auto_suspect_mods"])]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- bisection ---------------------------------------------------------------


def _load_history(conn: sqlite3.Connection, crash_log_id: int) -> list[dict]:
    row = conn.execute(
        "SELECT bisection_history FROM crash_log WHERE id = ?", (crash_log_id,)
    ).fetchone()
    if row is None or not row["bisection_history"]:
        return []
    return json.loads(row["bisection_history"])


def _run_round(
    crash_log_id: int, candidates: list[str], *, config: Config, conn: sqlite3.Connection
) -> list[str]:
    midpoint = len(candidates) // 2
    batch_a, batch_b = candidates[:midpoint], candidates[midpoint:]
    for mod_id in batch_a:
        mod_manager.disable(mod_id, config=config, conn=conn)

    history = _load_history(conn, crash_log_id)
    history.append({"disabled": batch_a, "kept_active": batch_b, "result": None})
    conn.execute(
        "UPDATE crash_log SET bisection_in_progress = 1, bisection_history = ? WHERE id = ?",
        (json.dumps(history), crash_log_id),
    )
    conn.commit()
    return batch_a


def start_bisection(crash_log_id: int, *, config: Config, conn: sqlite3.Connection) -> list[str]:
    """Begins bisection using the crash's active_mods_snapshot as the initial
    suspect pool (batch-based symlink toggling, never a physical file move).
    Returns the mod_ids disabled for round 1 — tell the user to relaunch the
    game and report back via report_bisection_result()."""
    row = conn.execute(
        "SELECT active_mods_snapshot FROM crash_log WHERE id = ?", (crash_log_id,)
    ).fetchone()
    if row is None:
        raise CrashAnalyzerError(f"No such crash log entry: {crash_log_id}")
    candidates = json.loads(row["active_mods_snapshot"])
    if len(candidates) < 2:
        raise CrashAnalyzerError("Bisection needs at least 2 candidate mods")

    return _run_round(crash_log_id, candidates, config=config, conn=conn)


def report_bisection_result(
    crash_log_id: int, crash_occurred: bool, *, config: Config, conn: sqlite3.Connection
) -> list[str] | str | None:
    """Call after the user relaunches and reports whether the crash still
    happens. Returns the next batch disabled (bisection continues), the
    single converged candidate mod_id (still requires an explicit
    confirm_faulty_mod() call — convergence alone is never treated as proof),
    or None if the pool was exhausted without narrowing to one mod."""
    history = _load_history(conn, crash_log_id)
    if not history or history[-1]["result"] is not None:
        raise CrashAnalyzerError(f"No bisection round in progress for crash log {crash_log_id}")

    current = history[-1]
    batch_a: list[str] = current["disabled"]
    batch_b: list[str] = current["kept_active"]
    current["result"] = "crash" if crash_occurred else "no_crash"

    # Either way, batch_a is cleared back to baseline (active) before the
    # next round — the culprit half becomes the new candidate pool, and the
    # invariant "all current candidates are active" must hold at round start.
    for mod_id in batch_a:
        mod_manager.enable(mod_id, config=config, conn=conn)
    new_candidates = batch_b if crash_occurred else batch_a

    conn.execute(
        "UPDATE crash_log SET bisection_history = ? WHERE id = ?", (json.dumps(history), crash_log_id)
    )
    conn.commit()

    if len(new_candidates) <= 1:
        conn.execute("UPDATE crash_log SET bisection_in_progress = 0 WHERE id = ?", (crash_log_id,))
        conn.commit()
        return new_candidates[0] if new_candidates else None

    return _run_round(crash_log_id, new_candidates, config=config, conn=conn)


def confirm_faulty_mod(crash_log_id: int, mod_id: str, conn: sqlite3.Connection) -> None:
    """Records the user's explicit confirmation that mod_id caused the
    crash. Bisection convergence alone is never enough — this is a separate,
    deliberate step, and it never deletes the mod; that's still up to the
    user via mod_manager.delete()."""
    conn.execute("UPDATE crash_log SET confirmed_faulty_mod_id = ? WHERE id = ?", (mod_id, crash_log_id))
    conn.commit()
