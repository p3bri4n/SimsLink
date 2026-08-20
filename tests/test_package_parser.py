import pytest

import package_parser as pp


def test_read_package_lists_resources(tmp_path, dbpf_writer):
    path = tmp_path / "mixed.package"
    dbpf_writer(
        path,
        [
            (pp.RESOURCE_TYPE_STBL, 0x80000000, 0x1234, b"hello"),
            (0x0166038C, 0x00000000, 0x5678, b"tuning-data"),
        ],
    )

    info = pp.read_package(path)

    assert len(info.resources) == 2
    assert info.resources[0].type == pp.RESOURCE_TYPE_STBL
    assert info.resources[0].group == 0x80000000
    assert info.resources[0].instance == 0x1234
    assert info.resources[0].is_stbl is True
    assert info.resources[1].is_stbl is False


def test_is_stbl_only_true_for_stbl_only_package(tmp_path, dbpf_writer):
    path = tmp_path / "translation.package"
    dbpf_writer(path, [(pp.RESOURCE_TYPE_STBL, 1, 100, b"fr")])

    info = pp.read_package(path)

    assert info.is_stbl_only is True


def test_is_stbl_only_false_for_mixed_package(tmp_path, dbpf_writer):
    path = tmp_path / "mixed.package"
    dbpf_writer(
        path,
        [
            (pp.RESOURCE_TYPE_STBL, 1, 100, b"fr"),
            (0x0166038C, 0, 200, b"tuning"),
        ],
    )

    info = pp.read_package(path)

    assert info.is_stbl_only is False


def test_is_stbl_only_false_for_empty_package(tmp_path, dbpf_writer):
    path = tmp_path / "empty.package"
    dbpf_writer(path, [])

    info = pp.read_package(path)

    assert info.resources == []
    assert info.is_stbl_only is False


def test_read_package_rejects_bad_magic(tmp_path):
    path = tmp_path / "not-a-package.package"
    path.write_bytes(b"NOPE" + b"\x00" * 100)

    with pytest.raises(pp.DbpfError):
        pp.read_package(path)


def test_read_package_rejects_truncated_file(tmp_path):
    path = tmp_path / "truncated.package"
    path.write_bytes(b"DBPF" + b"\x00" * 10)

    with pytest.raises(pp.DbpfError):
        pp.read_package(path)


def test_matching_stbl_keys_finds_shared_group_instance(tmp_path, dbpf_writer):
    source = tmp_path / "source.package"
    translation = tmp_path / "translation.package"
    dbpf_writer(source, [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en")])
    dbpf_writer(translation, [(pp.RESOURCE_TYPE_STBL, 5, 999, b"fr")])

    matches = pp.matching_stbl_keys(pp.read_package(source), pp.read_package(translation))

    assert matches == {(5, 999)}


def test_matching_stbl_keys_empty_when_no_overlap(tmp_path, dbpf_writer):
    source = tmp_path / "source.package"
    other = tmp_path / "other.package"
    dbpf_writer(source, [(pp.RESOURCE_TYPE_STBL, 5, 999, b"en")])
    dbpf_writer(other, [(pp.RESOURCE_TYPE_STBL, 1, 111, b"xx")])

    matches = pp.matching_stbl_keys(pp.read_package(source), pp.read_package(other))

    assert matches == set()
