import os

import pytest

from uv_cache_warden import discovery
from uv_cache_warden.discovery import DiscoveryError, bucket_prefix


@pytest.mark.parametrize(
    "name,expected",
    [
        ("wheels-v6", "wheels"),
        ("wheels-v7", "wheels"),
        ("sdists-v9", "sdists"),
        ("flat-index-v4", "flat-index"),
        ("archive-v0", "archive"),
        ("CACHEDIR.TAG", None),
        ("wheels", None),
        ("wheels-vX", None),
    ],
)
def test_bucket_prefix(name, expected):
    assert bucket_prefix(name) == expected


def test_layout_finds_buckets(cache):
    cache.add_package("rich")
    layout = cache.layout()
    assert layout.bucket_names["wheels"] == "wheels-v6"
    assert layout.bucket_names["archive"] == "archive-v0"
    assert layout.unknown == []


def test_layout_keeps_the_highest_bucket_version(cache):
    (cache.root / "wheels-v10").mkdir()
    layout = cache.layout()
    assert layout.bucket_names["wheels"] == "wheels-v10"


def test_layout_flags_unknown_buckets(cache):
    (cache.root / "quantum-v1").mkdir()
    layout = cache.layout()
    assert layout.unknown == ["quantum-v1"]


def test_refuses_a_directory_with_no_buckets(tmp_path):
    (tmp_path / "Documents").mkdir()
    (tmp_path / "notes.txt").write_text("hello")
    with pytest.raises(DiscoveryError) as excinfo:
        discovery.load_layout(tmp_path)
    assert "no uv cache buckets" in str(excinfo.value)


def test_refuses_a_missing_directory(tmp_path):
    with pytest.raises(DiscoveryError) as excinfo:
        discovery.load_layout(tmp_path / "nope")
    assert "does not exist" in str(excinfo.value)


def test_refuses_a_file(tmp_path):
    target = tmp_path / "file"
    target.write_text("x")
    with pytest.raises(DiscoveryError):
        discovery.load_layout(target)


def test_refuses_the_home_directory(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "wheels-v6").mkdir(parents=True)
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(home))
    with pytest.raises(DiscoveryError) as excinfo:
        discovery.load_layout(home)
    assert "home directory" in str(excinfo.value)


def test_discover_prefers_uv(cache, monkeypatch):
    from conftest import FakeUv

    monkeypatch.delenv("UVCW_CACHE_DIR", raising=False)
    cache.add_package("rich")
    uv = FakeUv(cache_dir=cache.root)
    root, source = discovery.discover_cache_dir(uv=uv)
    assert root == cache.root
    assert source == "uv cache dir"


def test_discover_falls_back_to_env(cache, monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", str(cache.root))
    monkeypatch.delenv("UVCW_CACHE_DIR", raising=False)
    root, source = discovery.discover_cache_dir(uv=None)
    assert root == cache.root
    assert source == "$UV_CACHE_DIR"


def test_discover_reports_when_nothing_exists(monkeypatch, tmp_path):
    for name in ("UVCW_CACHE_DIR", "UV_CACHE_DIR", "XDG_CACHE_HOME", "LOCALAPPDATA"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(os.path, "expanduser", lambda path: str(tmp_path / "nowhere"))
    with pytest.raises(DiscoveryError) as excinfo:
        discovery.discover_cache_dir(uv=None)
    assert "Could not locate uv's cache" in str(excinfo.value)
