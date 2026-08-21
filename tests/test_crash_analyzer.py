import zipfile
from pathlib import Path

import pytest

from backend import crash_analyzer as ca
from backend import mod_manager

FIXTURES = Path(__file__).parent / "fixtures"


def _install_mod(app_config, conn, tmp_path, name, filename="mymod.package", suffix=""):
    archive = tmp_path / f"{name}{suffix}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(filename, b"data")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=name)


def _install_ts4script_mod(app_config, conn, tmp_path, mod_id_hint, script_filename):
    archive = tmp_path / f"{mod_id_hint}.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(script_filename, b"bytecode")
    return mod_manager.install(archive, config=app_config, conn=conn, mod_name=mod_id_hint)


# --- direct-trace matching (CLAUDE.md priority coverage) --------------------


def test_match_direct_trace_finds_mod_in_traceback(app_config, conn, tmp_path):
    mod_id = _install_ts4script_mod(app_config, conn, tmp_path, "bettermod", "bettermod.ts4script")
    raw = (FIXTURES / "lastexception_mod_in_trace.txt").read_text()

    suspects = ca.analyze(raw, conn)

    assert len(suspects) == 1
    assert suspects[0].mod_id == mod_id
    assert suspects[0].confidence == "direct_trace"


def test_analyze_returns_empty_for_core_game_only_trace(app_config, conn, tmp_path):
    _install_ts4script_mod(app_config, conn, tmp_path, "bettermod", "bettermod.ts4script")
    raw = (FIXTURES / "lastexception_core_only.txt").read_text()

    assert ca.analyze(raw, conn) == []


def test_match_known_pattern_flags_installed_shared_library(app_config, conn, tmp_path):
    lib_id = _install_mod(app_config, conn, tmp_path, "Sims4CommunityLib", filename="s4cl.package")
    raw = (FIXTURES / "lastexception_known_pattern.txt").read_text()

    suspects = ca.analyze(raw, conn)

    assert any(s.mod_id == lib_id and s.confidence == "pattern_match" for s in suspects)


def test_match_known_pattern_empty_when_library_not_installed(app_config, conn):
    raw = (FIXTURES / "lastexception_known_pattern.txt").read_text()

    assert ca.analyze(raw, conn) == []


# --- parse_reports: real lastException.txt is XML, may bundle several ------
# occurrences in one file (CLAUDE.md's "Regression, fixed 2026-08-21")


def test_parse_reports_splits_real_multi_occurrence_file(app_config, conn):
    raw = (FIXTURES / "lastexception_real_multi_report.txt").read_text()

    reports = ca.parse_reports(raw)

    assert len(reports) == 3
    # Each split-out report is that occurrence's own traceback text, not the
    # whole file -- entities are resolved back to plain text (no leftover
    # &lt;/&gt;/&#13; from the XML encoding) and unrelated occurrences don't
    # bleed into each other.
    assert "DramaNodeScoringBucket" in reports[0]
    assert "DramaNodeScoringBucket" not in reports[1]
    assert "Posture Exit" in reports[1]
    assert "NotImplementedError" in reports[2]
    for report in reports:
        assert "&lt;" not in report and "&#13;" not in report


def test_parse_reports_falls_back_to_whole_input_for_plain_text(app_config, conn):
    raw = (FIXTURES / "lastexception_mod_in_trace.txt").read_text()

    assert ca.parse_reports(raw) == [raw]


def test_regression_parse_reports_extracts_mod_frame_from_real_xml_escaping(
    app_config, conn, tmp_path
):
    # Real desyncdata content escapes '<'/'>' (e.g. "<function ... at 0x...>")
    # but never the quotes inside `File "..."` frames -- confirms the
    # frame-matching regex still works once ElementTree resolves entities,
    # against a real captured crash rather than a synthetic fixture.
    mod_id = _install_ts4script_mod(
        app_config, conn, tmp_path, "WickedWhims", "WickedWhims_v185k.ts4script"
    )
    raw = (FIXTURES / "lastexception_real_mod_in_trace.txt").read_text()

    [report] = ca.parse_reports(raw)
    suspects = ca.analyze(report, conn)

    assert any(s.mod_id == mod_id for s in suspects)


# --- record_crash_reports: one crash_log row per occurrence, never merged ---


def test_record_crash_reports_creates_one_row_per_occurrence(app_config, conn):
    raw = (FIXTURES / "lastexception_real_multi_report.txt").read_text()

    crash_log_ids = ca.record_crash_reports(raw, conn=conn)

    assert len(crash_log_ids) == 3
    assert len(set(crash_log_ids)) == 3
    assert conn.execute("SELECT COUNT(*) FROM crash_log").fetchone()[0] == 3
    stored = [
        conn.execute(
            "SELECT raw_last_exception FROM crash_log WHERE id = ?", (crash_log_id,)
        ).fetchone()["raw_last_exception"]
        for crash_log_id in crash_log_ids
    ]
    # Each row stores only its own occurrence's traceback -- not the other
    # two, and not the raw multi-report file.
    assert "DramaNodeScoringBucket" in stored[0]
    assert "Posture Exit" not in stored[0]
    assert "Posture Exit" in stored[1]
    assert "NotImplementedError" in stored[2]


def test_record_crash_reports_single_occurrence_matches_record_crash(app_config, conn, tmp_path):
    mod_id = _install_ts4script_mod(app_config, conn, tmp_path, "bettermod", "bettermod.ts4script")
    raw = (FIXTURES / "lastexception_mod_in_trace.txt").read_text()

    [crash_log_id] = ca.record_crash_reports(raw, conn=conn)

    suspects = ca.get_suspects(crash_log_id, conn)
    assert len(suspects) == 1
    assert suspects[0].mod_id == mod_id


# --- record_crash: read-only w.r.t. mods, never auto-deletes ----------------


def test_record_crash_never_touches_mod_state(app_config, conn, tmp_path):
    mod_id = _install_ts4script_mod(app_config, conn, tmp_path, "bettermod", "bettermod.ts4script")
    raw = (FIXTURES / "lastexception_mod_in_trace.txt").read_text()

    for _ in range(5):  # repeated occurrences must still never trigger anything
        ca.record_crash(raw, conn=conn)

    row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["active"] == 1  # still installed and active — never auto-disabled/deleted
    assert conn.execute("SELECT COUNT(*) FROM crash_log").fetchone()[0] == 5


def test_record_crash_stores_suspects_and_snapshot(app_config, conn, tmp_path):
    mod_id = _install_ts4script_mod(app_config, conn, tmp_path, "bettermod", "bettermod.ts4script")
    raw = (FIXTURES / "lastexception_mod_in_trace.txt").read_text()

    crash_log_id = ca.record_crash(raw, conn=conn)

    suspects = ca.get_suspects(crash_log_id, conn)
    assert len(suspects) == 1
    assert suspects[0].mod_id == mod_id

    row = conn.execute("SELECT active_mods_snapshot FROM crash_log WHERE id = ?", (crash_log_id,)).fetchone()
    import json

    assert json.loads(row["active_mods_snapshot"]) == [mod_id]


# --- bisection ----------------------------------------------------------------


def _install_n_mods(app_config, conn, tmp_path, n):
    return [
        _install_mod(app_config, conn, tmp_path, f"Mod{i}", filename=f"mod{i}.package", suffix=str(i))
        for i in range(n)
    ]


def test_start_bisection_disables_half_the_active_mods(app_config, conn, tmp_path):
    mod_ids = _install_n_mods(app_config, conn, tmp_path, 4)
    crash_log_id = ca.record_crash("no useful trace", conn=conn)

    disabled = ca.start_bisection(crash_log_id, config=app_config, conn=conn)

    assert len(disabled) == 2
    for mod_id in disabled:
        row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
        assert row["active"] == 0
    row = conn.execute("SELECT bisection_in_progress FROM crash_log WHERE id = ?", (crash_log_id,)).fetchone()
    assert row["bisection_in_progress"] == 1


def test_start_bisection_requires_at_least_two_candidates(app_config, conn, tmp_path):
    _install_n_mods(app_config, conn, tmp_path, 1)
    crash_log_id = ca.record_crash("trace", conn=conn)

    with pytest.raises(ca.CrashAnalyzerError):
        ca.start_bisection(crash_log_id, config=app_config, conn=conn)


def test_bisection_converges_to_single_culprit(app_config, conn, tmp_path):
    mod_ids = _install_n_mods(app_config, conn, tmp_path, 4)
    culprit = mod_ids[3]
    crash_log_id = ca.record_crash("trace", conn=conn)

    def crash_happens_without(disabled_batch):
        # Simulates reality: the crash still happens unless the culprit is
        # among the disabled batch.
        return culprit not in disabled_batch

    disabled = ca.start_bisection(crash_log_id, config=app_config, conn=conn)
    result = ca.report_bisection_result(
        crash_log_id, crash_happens_without(disabled), config=app_config, conn=conn
    )

    # Keep stepping until convergence (O(log n) rounds for 4 candidates -> at most 2).
    steps = 0
    while isinstance(result, list):
        steps += 1
        assert steps <= 3  # generous bound; true bisection of 4 converges in 2 rounds
        result = ca.report_bisection_result(
            crash_log_id, crash_happens_without(result), config=app_config, conn=conn
        )

    assert result == culprit

    # Every batch disabled mid-bisection is unconditionally restored by the
    # very next report_bisection_result() call, so nothing is left disabled
    # once convergence is reached — including the identified culprit itself:
    # crash_analyzer only ever diagnoses, it never leaves state changed
    # without a separate, explicit follow-up action from the user.
    for mod_id in mod_ids:
        row = conn.execute("SELECT active FROM mods WHERE id = ?", (mod_id,)).fetchone()
        assert row["active"] == 1

    row = conn.execute("SELECT bisection_in_progress FROM crash_log WHERE id = ?", (crash_log_id,)).fetchone()
    assert row["bisection_in_progress"] == 0


def test_report_bisection_result_without_active_round_raises(app_config, conn, tmp_path):
    _install_n_mods(app_config, conn, tmp_path, 4)
    crash_log_id = ca.record_crash("trace", conn=conn)

    with pytest.raises(ca.CrashAnalyzerError):
        ca.report_bisection_result(crash_log_id, True, config=app_config, conn=conn)


def test_confirm_faulty_mod_records_without_deleting(app_config, conn, tmp_path):
    mod_ids = _install_n_mods(app_config, conn, tmp_path, 2)
    crash_log_id = ca.record_crash("trace", conn=conn)

    ca.confirm_faulty_mod(crash_log_id, mod_ids[0], conn)

    row = conn.execute(
        "SELECT confirmed_faulty_mod_id FROM crash_log WHERE id = ?", (crash_log_id,)
    ).fetchone()
    assert row["confirmed_faulty_mod_id"] == mod_ids[0]
    # Never auto-deleted — that's still a separate, explicit user action.
    assert conn.execute("SELECT COUNT(*) FROM mods WHERE id = ?", (mod_ids[0],)).fetchone()[0] == 1
