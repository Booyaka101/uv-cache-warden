"""Synthetic uv caches, built to the layout in crates/uv-cache/src/lib.rs."""

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from uv_cache_warden import discovery  # noqa: E402
from uv_cache_warden.uvcmd import UvResult  # noqa: E402


class FakeUv:
    """Records every uv invocation instead of running one.

    Only the tests use this. Nothing in `src/` imports it, so the shipped package can never
    fall back to a fake.
    """

    def __init__(self, cache_dir=None, size=None, clean_fails=()):
        self.cache_dir = cache_dir
        self.calls = []
        self._size = size
        self._clean_fails = set(clean_fails)
        self.timeout = 900.0
        self.echo = False
        self.executable = "fake-uv"

    def _record(self, *args):
        self.calls.append(list(args))

    @property
    def mutating_calls(self):
        return [call for call in self.calls if call[:2] in (["cache", "clean"], ["cache", "prune"])]

    def version(self):
        self._record("--version")
        return "uv 0.0.0-fake"

    def resolve_cache_dir(self):
        self._record("cache", "dir")
        return Path(self.cache_dir)

    def cache_size(self):
        self._record("cache", "size")
        if self._size is None:
            return None
        return self._size() if callable(self._size) else self._size

    def cache_clean(self, package):
        self._record("cache", "clean", package)
        if package in self._clean_fails:
            return UvResult(["uv", "cache", "clean", package], 2, "", "error: nope\n")
        removed = _remove_package(Path(self.cache_dir), package)
        return UvResult(["uv", "cache", "clean", package], 0, "Removed {}\n".format(removed), "")

    def cache_prune(self, ci=False):
        self._record("cache", "prune")
        return UvResult(["uv", "cache", "prune"], 0, "No unused entries found\n", "")


def _remove_package(root: Path, package: str) -> int:
    """Mimic `uv cache clean <pkg>`: drop the package's entries, then unreferenced archives."""
    removed = 0
    for bucket in ("wheels-v6", "sdists-v9"):
        target = root / bucket / "pypi" / package
        if target.is_dir():
            removed += sum(1 for _ in target.rglob("*"))
            shutil.rmtree(target)
    rkyv = root / "simple-v24" / "pypi" / "{}.rkyv".format(package)
    if rkyv.exists():
        rkyv.unlink()
        removed += 1

    archive_root = root / "archive-v0"
    if archive_root.is_dir():
        referenced = set()
        for bucket in ("wheels-v6", "sdists-v9"):
            bucket_root = root / bucket
            if not bucket_root.is_dir():
                continue
            for path in bucket_root.rglob("*"):
                from uv_cache_warden.attribute import read_link

                archive_id = read_link(path)
                if archive_id:
                    referenced.add(archive_id)
        for entry in list(archive_root.iterdir()):
            if entry.name not in referenced and entry.is_dir():
                removed += sum(1 for _ in entry.rglob("*"))
                shutil.rmtree(entry)
    return removed


def write_file(path: Path, size: int, mtime: float = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if mtime is not None:
        os.utime(str(path), (mtime, mtime))
    return path


def set_times(root: Path, when: float) -> None:
    """Age a whole subtree, directories included.

    `last_used` is the newest timestamp anywhere under a package, so leaving one freshly
    created parent directory behind would make an ancient package look brand new.
    """
    nofollow = os.utime in getattr(os, "supports_follow_symlinks", set())
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink():
            if nofollow:
                os.utime(str(path), (when, when), follow_symlinks=False)
            continue
        os.utime(str(path), (when, when))
    os.utime(str(root), (when, when))


def link_to_archive(link_path: Path, archive_id: str) -> None:
    """Write the archive reference in whichever shape uv uses on this platform."""
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        link_path.write_text("archive-v0/{}".format(archive_id), encoding="utf-8")
        return
    target = os.path.relpath(
        str(link_path.parents[2].parent / "archive-v0" / archive_id), str(link_path.parent)
    )
    os.symlink(target, str(link_path), target_is_directory=True)


class CacheBuilder:
    def __init__(self, root: Path):
        self.root = root
        for bucket in ("archive-v0", "wheels-v6", "sdists-v9", "simple-v24"):
            (root / bucket).mkdir(parents=True, exist_ok=True)
        (root / "CACHEDIR.TAG").write_bytes(b"Signature: 8a477f597d28d172789f06886806bc55")

    def add_package(self, name, version="1.0.0", size=4096, mtime=None, archive_id=None):
        """One wheel entry, its simple-index entry and an unpacked archive it links to."""
        archive_id = archive_id or "arch-{}".format(name)
        entry = "{}-py3-none-any".format(version)
        wheel_dir = self.root / "wheels-v6" / "pypi" / name
        wheel_dir.mkdir(parents=True, exist_ok=True)
        write_file(wheel_dir / "{}.http".format(entry), 64, mtime)
        write_file(wheel_dir / "{}-py3-none-any.lock".format(name), 0, mtime)
        write_file(self.root / "simple-v24" / "pypi" / "{}.rkyv".format(name), 128, mtime)

        archive_dir = self.root / "archive-v0" / archive_id
        write_file(archive_dir / "{}/__init__.py".format(name.replace("-", "_")), size, mtime)
        link_to_archive(wheel_dir / entry, archive_id)
        if mtime is not None:
            set_times(archive_dir, mtime)
            set_times(wheel_dir, mtime)
        return archive_dir

    def layout(self):
        return discovery.load_layout(self.root, source="test")


@pytest.fixture
def cache(tmp_path):
    return CacheBuilder(tmp_path / "uv-cache")
