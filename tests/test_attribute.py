import pytest

from uv_cache_warden import attribute
from uv_cache_warden.attribute import is_package_name, package_from_parts, read_link

from conftest import link_to_archive, write_file


@pytest.mark.parametrize(
    "bucket,parts,expected",
    [
        ("wheels", ["pypi", "rich", "13.7.1-py3-none-any"], "rich"),
        ("wheels", ["index", "abc123", "torch", "2.4.0-cp312"], "torch"),
        ("wheels", ["url", "deadbeef", "wheel-name", "1.0"], "wheel-name"),
        ("wheels", ["path", "abc", "thing"], None),
        ("wheels", ["git", "repo", "sha"], None),
        ("sdists", ["pypi", "psutil", "5.9.8"], "psutil"),
        ("sdists", ["index", "abc", "psutil", "5.9.8"], "psutil"),
        ("sdists", ["url", "abc", "5.9.8"], None),
        ("sdists", ["git", "repo", "sha"], None),
        ("simple", ["pypi", "rich.rkyv"], "rich"),
        ("simple", ["index", "abc", "rich.rkyv"], "rich"),
        ("simple", ["pypi", "rich.lock"], None),
        ("archive", ["someid"], None),
    ],
)
def test_package_from_parts(bucket, parts, expected):
    assert package_from_parts(bucket, parts) == expected


@pytest.mark.parametrize(
    "name,valid",
    [
        ("rich", True),
        ("nvidia-cudnn-cu12", True),
        ("zope.interface", True),
        ("a", True),
        ("", False),
        ("-leading", False),
        ("trailing-", False),
        ("has space", False),
        ("bang!", False),
    ],
)
def test_is_package_name(name, valid):
    assert is_package_name(name) is valid


def test_links_resolve_to_the_owning_package(cache):
    cache.add_package("rich", archive_id="arch-rich")
    cache.add_package("torch", archive_id="arch-torch")
    result = attribute.build(cache.layout())

    assert result.sole_owner("arch-rich") == "rich"
    assert result.sole_owner("arch-torch") == "torch"
    assert {link.package for link in result.links} == {"rich", "torch"}


def test_archive_shared_by_two_packages_has_no_sole_owner(cache):
    cache.add_package("alpha", archive_id="shared")
    cache.add_package("beta", archive_id="shared")
    result = attribute.build(cache.layout())

    assert result.archive_owners["shared"] == {"alpha", "beta"}
    assert result.sole_owner("shared") is None


def test_dangling_link_is_counted_not_attributed(cache):
    cache.add_package("rich", archive_id="arch-rich")
    link_to_archive(cache.root / "wheels-v6" / "pypi" / "rich" / "9.9.9-py3-none-any", "missing")
    result = attribute.build(cache.layout())

    assert result.unresolved_links == 1
    assert "missing" not in result.archive_owners


def test_sidecars_are_not_links_and_not_versions(cache):
    cache.add_package("rich", version="13.7.1")
    result = attribute.build(cache.layout())
    assert result.package_versions["rich"] == {"13.7.1"}


def test_read_link_ignores_a_large_file(tmp_path):
    payload = write_file(tmp_path / "big", 4096)
    assert read_link(payload) is None


def test_read_link_ignores_unrelated_content(tmp_path):
    path = tmp_path / "notalink"
    path.write_text("just some text")
    assert read_link(path) is None


def test_read_link_reads_the_windows_pointer_shape(tmp_path):
    path = tmp_path / "entry"
    path.write_text("archive-v0/abcDEF-123_")
    assert read_link(path) == "abcDEF-123_"


def test_read_link_ignores_a_stale_archive_version(tmp_path):
    path = tmp_path / "entry"
    path.write_text("archive-v9/abc123")
    assert read_link(path, archive_version=9) == "abc123"
    assert read_link(path, archive_version=0) is None


def test_stale_links_are_not_attributed(cache):
    cache.add_package("rich", archive_id="arch-rich")
    stale = cache.root / "wheels-v6" / "pypi" / "rich" / "0.0.1-py3-none-any"
    stale.write_text("archive-v7/arch-rich")
    result = attribute.build(cache.layout())

    assert result.archive_owners["arch-rich"] == {"rich"}
    assert len(result.links) == 1
