import pytest

from backend import blacklist


def test_add_entry_returns_id(conn):
    entry_id = blacklist.add_entry("badmod", conn)

    assert isinstance(entry_id, int)


def test_add_entry_rejects_empty_pattern(conn):
    with pytest.raises(blacklist.BlacklistError):
        blacklist.add_entry("   ", conn)


def test_add_entry_stores_optional_note(conn):
    blacklist.add_entry("badmod", conn, note="Known to corrupt saves")

    entries = blacklist.list_entries(conn)

    assert entries[0].note == "Known to corrupt saves"


def test_remove_entry_deletes_it(conn):
    entry_id = blacklist.add_entry("badmod", conn)

    blacklist.remove_entry(entry_id, conn)

    assert blacklist.list_entries(conn) == []


def test_list_entries_sorted_by_pattern(conn):
    blacklist.add_entry("zeta", conn)
    blacklist.add_entry("alpha", conn)

    entries = blacklist.list_entries(conn)

    assert [e.pattern for e in entries] == ["alpha", "zeta"]


# --- matching ------------------------------------------------------------------


def test_find_matches_matches_mod_name_case_insensitively(conn):
    entries = [blacklist.BlacklistEntry(id=1, pattern="BadMod", note=None)]

    matches = blacklist.find_matches("Totally BadMod Deluxe", "totally-badmod-deluxe", entries)

    assert len(matches) == 1


def test_find_matches_matches_mod_id(conn):
    entries = [blacklist.BlacklistEntry(id=1, pattern="sketchy-slug", note=None)]

    matches = blacklist.find_matches("Innocuous Name", "sketchy-slug", entries)

    assert len(matches) == 1


def test_find_matches_empty_when_nothing_matches(conn):
    entries = [blacklist.BlacklistEntry(id=1, pattern="badmod", note=None)]

    matches = blacklist.find_matches("Totally Fine Mod", "totally-fine-mod", entries)

    assert matches == []


def test_find_matches_returns_every_matching_pattern(conn):
    entries = [
        blacklist.BlacklistEntry(id=1, pattern="bad", note=None),
        blacklist.BlacklistEntry(id=2, pattern="mod", note=None),
    ]

    matches = blacklist.find_matches("Bad Mod", "bad-mod", entries)

    assert len(matches) == 2
