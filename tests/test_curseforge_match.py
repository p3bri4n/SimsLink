from backend import curseforge as cf
from backend import curseforge_match
from backend import loose_mods
from backend import mod_manager


class _FakeClient:
    """Stands in for curseforge.CurseForgeClient — only the two methods
    curseforge_match.py actually calls. `fail_times` makes match_fingerprints()
    raise a transient CurseForgeError for that many calls before succeeding,
    to test run_step()'s retry-safety.

    `matches` is keyed by fingerprint -> mod_id, same as most tests below
    only care about — a fake, deterministic file_id (the fingerprint itself)
    is synthesized for the (mod_id, file_id) tuple match_fingerprints() now
    really returns, so existing call sites don't need to invent a real-
    looking file id just to construct this fake. Tests that actually care
    about the file id (installed_version) pass `file_ids` explicitly."""

    def __init__(
        self,
        matches: dict[int, int],
        mods_by_id: dict[int, cf.CurseForgeMod],
        *,
        fail_times: int = 0,
        file_ids: dict[int, int] | None = None,
    ):
        self._matches = matches
        self._file_ids = file_ids or {}
        self._mods_by_id = mods_by_id
        self._fail_times = fail_times
        self._fail_count = 0
        self.match_calls: list[list[int]] = []
        self.get_mods_calls: list[list[int]] = []

    def match_fingerprints(self, fingerprints):
        if self._fail_count < self._fail_times:
            self._fail_count += 1
            raise cf.CurseForgeError("simulated transient failure")
        self.match_calls.append(list(fingerprints))
        return {
            fp: (self._matches[fp], self._file_ids.get(fp, fp)) for fp in fingerprints if fp in self._matches
        }

    def get_mods(self, mod_ids):
        self.get_mods_calls.append(list(mod_ids))
        return [self._mods_by_id[i] for i in mod_ids if i in self._mods_by_id]


def _make_mod(**overrides):
    defaults = dict(
        mod_id=111,
        name="Better Woohoo",
        author="SomeAuthor",
        category="Gameplay",
        short_description="desc",
        thumbnail_url="https://example.com/thumb.png",
        curseforge_url="https://www.curseforge.com/sims4/mods/better-woohoo",
        third_party_distribution_allowed=True,
    )
    defaults.update(overrides)
    return cf.CurseForgeMod(**defaults)


def _adopt_loose_file(config, conn, name: str, content: bytes) -> str:
    path = config.sims4_mods_dir / name
    path.write_bytes(content)
    imported = loose_mods.import_loose_files(config, conn)
    assert imported
    return imported[-1]


def _run_to_completion(session, conn, client, chunk_size=curseforge_match.CHUNK_SIZE):
    while not session.done:
        curseforge_match.run_step(session, conn, client, chunk_size=chunk_size)
    return session


# --- start_session() -----------------------------------------------------------------


def test_start_session_counts_every_unlinked_mod_loose_or_not(app_config, conn, tmp_path):
    # Not scoped to loose imports only (see the module docstring) — a
    # regular multi-file mod (e.g. installed via Assisted Mode's download
    # watcher, which never queries CurseForge either) is just as much a
    # candidate as a loose single-file import.
    _adopt_loose_file(app_config, conn, "Loose.package", b"real content")
    source = tmp_path / "RegularMod"
    source.mkdir()
    (source / "thing.package").write_bytes(b"other content")
    mod_manager.import_existing_folder(source, config=app_config, conn=conn)

    session = curseforge_match.start_session(conn)

    assert session.total == 2
    assert not session.done


def test_start_session_excludes_already_linked_mods(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    conn.execute("UPDATE mods SET curseforge_id = ? WHERE id = ?", (999, mod_id))
    conn.commit()

    session = curseforge_match.start_session(conn)

    assert session.total == 0
    assert session.done


def test_start_session_with_no_candidates_is_immediately_done(app_config, conn):
    session = curseforge_match.start_session(conn)

    assert session.total == 0
    assert session.done


# --- run_step() ------------------------------------------------------------------


def test_run_step_links_a_matched_loose_mod_and_commits_immediately(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient(
        {fingerprint: 111},
        {
            111: _make_mod(
                mod_id=111,
                author="RealAuthor",
                thumbnail_url="https://x/y.png",
                category="Gameplay",
                short_description="Makes it better.",
            )
        },
    )
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    assert session.checked == 1
    assert session.matched == 1
    assert session.done
    row = conn.execute(
        "SELECT curseforge_id, author, thumbnail_url, links, category, short_description, curseforge_name, name, "
        "installed_version FROM mods WHERE id = ?",
        (mod_id,),
    ).fetchone()
    assert row["curseforge_id"] == 111
    assert row["author"] == "RealAuthor"
    assert row["thumbnail_url"] == "https://x/y.png"
    assert row["category"] == "Gameplay"
    assert row["installed_version"] == str(fingerprint)  # the fake's synthesized file_id
    assert row["short_description"] == "Makes it better."
    assert row["curseforge_name"] == "Better Woohoo"  # _make_mod()'s default name
    assert row["name"] == "SomeMod"  # the locally-derived name is untouched


def test_run_step_never_overwrites_existing_curseforge_name(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    conn.execute("UPDATE mods SET curseforge_name = 'Already Set' WHERE id = ?", (mod_id,))
    conn.commit()
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111, name="New Real Name")})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    row = conn.execute("SELECT curseforge_name FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_name"] == "Already Set"


def test_run_step_never_overwrites_existing_author(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    conn.execute("UPDATE mods SET author = ? WHERE id = ?", ("ExistingAuthor", mod_id))
    conn.commit()
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111, author="CurseForgeAuthor")})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    row = conn.execute("SELECT author FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["author"] == "ExistingAuthor"


def test_regression_run_step_fills_in_installed_version_from_the_matched_file(app_config, conn):
    # Found while building a header "Update all" button: a mod linked via
    # fingerprint matching never had installed_version set at all, which
    # made /api/updates/check compare a real file id against nothing and
    # flag it as needing an update regardless of whether it actually did —
    # confirmed against a real 519-mod library where 509 were wrongly
    # flagged. The matched file's own id (distinct from the mod id) is what
    # match_fingerprints() now also returns, specifically so this can be
    # filled in with the real installed file, not a guess.
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111)}, file_ids={fingerprint: 4746855})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    row = conn.execute("SELECT installed_version FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["installed_version"] == "4746855"


def test_run_step_never_overwrites_existing_installed_version(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    conn.execute("UPDATE mods SET installed_version = '999' WHERE id = ?", (mod_id,))
    conn.commit()
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111)}, file_ids={fingerprint: 4746855})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    row = conn.execute("SELECT installed_version FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["installed_version"] == "999"


def test_run_step_processes_one_chunk_at_a_time(app_config, conn):
    for i in range(5):
        _adopt_loose_file(app_config, conn, f"Mod{i}.package", f"content {i}".encode())
    session = curseforge_match.start_session(conn)
    assert session.total == 5

    curseforge_match.run_step(session, conn, _FakeClient({}, {}), chunk_size=2)

    assert session.checked == 2
    assert not session.done
    assert len(session.remaining) == 3


def test_run_step_stopping_early_keeps_already_applied_matches(app_config, conn):
    mod_id_a = _adopt_loose_file(app_config, conn, "ModA.package", b"content a")
    mod_id_b = _adopt_loose_file(app_config, conn, "ModB.package", b"content b")
    fp_a = cf.curseforge_fingerprint(b"content a")
    fake = _FakeClient({fp_a: 111}, {111: _make_mod(mod_id=111)})
    session = curseforge_match.start_session(conn)

    # Only one step, chunk_size=1 — simulates the frontend stopping the
    # popup after the first update instead of looping to completion.
    curseforge_match.run_step(session, conn, fake, chunk_size=1)

    linked_ids = {mod_id_a, mod_id_b} & {
        r["id"] for r in conn.execute("SELECT id FROM mods WHERE curseforge_id IS NOT NULL")
    }
    assert linked_ids  # whichever of the two was processed first got its match durably saved
    assert not session.done  # the other mod is still pending, exactly as "stopped, not lost" implies


def test_run_step_links_a_multi_file_mod_via_any_matching_file(app_config, conn, tmp_path):
    # The real MC Command Center scenario: several files, none of them "the"
    # obvious representative one, but any single match is enough to link
    # the whole mod (see the module docstring).
    source = tmp_path / "MultiFileMod"
    source.mkdir()
    (source / "aaa_first.ts4script").write_bytes(b"unrelated script content")
    (source / "zzz_last.package").write_bytes(b"the real matching file")
    mod_id = mod_manager.import_existing_folder(source, config=app_config, conn=conn)
    fingerprint = cf.curseforge_fingerprint(b"the real matching file")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111, name="Multi File Mod")})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    assert session.matched == 1
    row = conn.execute("SELECT curseforge_id, curseforge_name FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_id"] == 111
    assert row["curseforge_name"] == "Multi File Mod"


def test_run_step_only_applies_one_match_per_mod_even_with_several_candidate_files(app_config, conn, tmp_path):
    # Two files in the same mod folder both happen to fingerprint-match
    # (e.g. CurseForge recognizes both) — the mod should only be counted/
    # applied once, not twice.
    source = tmp_path / "MultiMatchMod"
    source.mkdir()
    (source / "one.package").write_bytes(b"content one")
    (source / "two.package").write_bytes(b"content two")
    mod_id = mod_manager.import_existing_folder(source, config=app_config, conn=conn)
    fp_one = cf.curseforge_fingerprint(b"content one")
    fp_two = cf.curseforge_fingerprint(b"content two")
    fake = _FakeClient({fp_one: 111, fp_two: 111}, {111: _make_mod(mod_id=111)})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    assert session.matched == 1
    row = conn.execute("SELECT curseforge_id FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_id"] == 111


def test_run_step_skips_files_larger_than_cap(app_config, conn, monkeypatch):
    _adopt_loose_file(app_config, conn, "BigMod.package", b"x" * 100)
    monkeypatch.setattr(curseforge_match, "_SIZE_CAP_BYTES", 10)
    fake = _FakeClient({}, {})
    session = curseforge_match.start_session(conn)

    curseforge_match.run_step(session, conn, fake)

    assert session.checked == 1
    assert session.matched == 0
    assert session.skipped_too_large == 1
    assert fake.match_calls == []  # nothing left to send once the only file was skipped


def test_run_to_completion_across_multiple_chunks(app_config, conn):
    for i in range(7):
        _adopt_loose_file(app_config, conn, f"Mod{i}.package", f"content {i}".encode())
    session = curseforge_match.start_session(conn)

    _run_to_completion(session, conn, _FakeClient({}, {}), chunk_size=3)

    assert session.total == 7
    assert session.checked == 7
    assert session.done


# --- robustness: a transient CurseForge/network failure must never desync
# or lose progress (this is what "it just stops on its own" / "restarted
# from zero" were actually caused by — a failed chunk used to be popped off
# `remaining` and silently skipped instead of staying retryable). ------------


def test_run_step_raises_and_leaves_session_untouched_on_client_failure(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111)}, fail_times=1)
    session = curseforge_match.start_session(conn)
    remaining_before = list(session.remaining)

    try:
        curseforge_match.run_step(session, conn, fake)
        assert False, "expected CurseForgeError to propagate"
    except cf.CurseForgeError:
        pass

    # Nothing advanced — same chunk, same counters, nothing written to the DB.
    assert session.remaining == remaining_before
    assert session.checked == 0
    assert session.matched == 0
    row = conn.execute("SELECT curseforge_id FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_id"] is None


def test_run_step_retry_after_failure_processes_the_same_chunk(app_config, conn):
    mod_id = _adopt_loose_file(app_config, conn, "SomeMod.package", b"real content")
    fingerprint = cf.curseforge_fingerprint(b"real content")
    fake = _FakeClient({fingerprint: 111}, {111: _make_mod(mod_id=111, author="RealAuthor")}, fail_times=2)
    session = curseforge_match.start_session(conn)

    for _ in range(2):
        try:
            curseforge_match.run_step(session, conn, fake)
        except cf.CurseForgeError:
            continue
    # Third attempt (fail_times=2 exhausted) succeeds against the exact same
    # still-untouched chunk.
    curseforge_match.run_step(session, conn, fake)

    assert session.checked == 1
    assert session.matched == 1
    assert session.done
    row = conn.execute("SELECT curseforge_id, author FROM mods WHERE id = ?", (mod_id,)).fetchone()
    assert row["curseforge_id"] == 111
    assert row["author"] == "RealAuthor"
