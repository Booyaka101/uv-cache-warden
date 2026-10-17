import os

import pytest

from uv_cache_warden import sizing
from uv_cache_warden.sizing import POSIX, Counter, Usage, format_size, measure_tree

from conftest import write_file


@pytest.mark.parametrize(
    "text,expected",
    [
        ("1024", 1024),
        ("1KiB", 1024),
        ("1kB", 1000),
        ("10GB", 10_000_000_000),
        ("2.5GiB", int(2.5 * 2**30)),
        ("500 MiB", 500 * 2**20),
        ("4tb", 4 * 10**12),
        ("9007199254740993", 9007199254740993),
        ("9007199254740993.125KiB", 9223372036854776960),
    ],
)
def test_parse_size(text, expected):
    assert sizing.parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "GB", "10 parsecs", "-5GB", "1.2.3GB", "9" * 400 + "GB"])
def test_parse_size_rejects_nonsense(text):
    with pytest.raises(ValueError):
        sizing.parse_size(text)


@pytest.mark.parametrize(
    "text,expected", [("30s", 30), ("15m", 900), ("12h", 43200), ("7d", 604800), ("2w", 1209600)]
)
def test_parse_duration(text, expected):
    assert sizing.parse_duration(text) == expected


@pytest.mark.parametrize("text", ["7", "", "7 fortnights", "d", "9" * 400 + "d"])
def test_parse_duration_rejects_nonsense(text):
    with pytest.raises(ValueError):
        sizing.parse_duration(text)


def test_format_size():
    assert format_size(0) == "0 B"
    assert format_size(1024) == "1.0 KiB"
    assert format_size(23_700_000_000) == "22.1 GiB"


def test_hardlinked_file_counts_once(tmp_path):
    """A file hardlinked twice inside the cache must not be counted twice."""
    write_file(tmp_path / "a" / "payload.bin", 64_000)
    (tmp_path / "b").mkdir()
    try:
        os.link(str(tmp_path / "a" / "payload.bin"), str(tmp_path / "b" / "payload.bin"))
    except OSError as exc:
        pytest.skip("filesystem does not support hardlinks: {}".format(exc))

    usage = measure_tree(tmp_path)
    assert usage.files == 2
    assert usage.hardlinked_files == 2
    assert usage.shared_bytes >= 64_000
    assert usage.physical_bytes < 128_000
    if POSIX:
        # diskus dedupes hardlinks on Unix, so uv's own total sees one copy too.
        assert usage.disk_bytes == usage.physical_bytes
    else:
        # diskus does not dedupe on Windows (diskus#32), so uv counts both copies.
        assert usage.disk_bytes >= 128_000


def test_dedupe_is_global_across_subtrees(tmp_path):
    write_file(tmp_path / "one" / "f", 32_000)
    (tmp_path / "two").mkdir()
    try:
        os.link(str(tmp_path / "one" / "f"), str(tmp_path / "two" / "f"))
    except OSError as exc:
        pytest.skip("filesystem does not support hardlinks: {}".format(exc))

    counter = Counter()
    first = Usage()
    second = Usage()
    for _path, _parts, st, is_dir in sizing.walk(tmp_path / "one"):
        counter.add(first, st, is_dir)
    for _path, _parts, st, is_dir in sizing.walk(tmp_path / "two"):
        counter.add(second, st, is_dir)

    assert first.physical_bytes >= 32_000
    # The second walk still counts its own directory entry, but not the file's bytes again.
    assert second.physical_bytes < 32_000
    assert second.shared_bytes >= 32_000


def _link_directory(target, link):
    """A directory symlink, or a junction where Windows will not grant symlink privilege."""
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        if os.name != "nt":
            raise
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


def test_walk_does_not_follow_directory_links(tmp_path):
    outside = tmp_path / "outside"
    write_file(outside / "huge.bin", 100_000)
    inside = tmp_path / "cache"
    inside.mkdir()
    try:
        _link_directory(outside, inside / "escape")
    except (OSError, NotImplementedError, ImportError) as exc:
        pytest.skip("cannot link directories here: {}".format(exc))

    usage = measure_tree(inside)
    # The link itself is counted as one entry, exactly as diskus counts it, but nothing behind it.
    assert usage.dirs == 1
    assert usage.disk_bytes < 100_000


def test_last_used_prefers_the_newer_timestamp(tmp_path):
    path = write_file(tmp_path / "f", 10)
    os.utime(str(path), (1_000_000, 2_000_000))
    st = os.stat(str(path))
    assert sizing.last_used_of(st) == 2_000_000


def test_atime_looks_frozen_needs_a_sample(tmp_path):
    counter = Counter()
    assert counter.atime_looks_frozen is False
    counter.atime_samples = 100
    counter.atime_equal_mtime = 100
    assert counter.atime_looks_frozen is True
    counter.atime_equal_mtime = 50
    assert counter.atime_looks_frozen is False


def test_directory_atime_does_not_count_as_use(tmp_path):
    """Listing a directory bumps its atime under relatime; that is our visit, not the user's."""
    write_file(tmp_path / "pkg" / "f", 10, mtime=1_000_000)
    os.utime(str(tmp_path / "pkg"), (2_000_000_000, 1_000_000))
    usage = measure_tree(tmp_path / "pkg")
    assert usage.last_used == 1_000_000
