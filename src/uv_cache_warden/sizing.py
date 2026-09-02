"""Byte accounting that agrees with uv, plus the size/duration literals the CLI parses.

Three totals come out of a walk, and they are genuinely different numbers:

``disk_bytes``
    What ``uv cache size`` reports. uv delegates to ``diskus 0.9.0``, so this reproduces
    diskus exactly: ``st_blocks * 512`` on Unix (directories included) with hardlink dedupe
    by ``(dev, ino)`` for files whose ``nlink > 1``; plain ``st_size`` on Windows with no
    dedupe at all (diskus#32). This is the metric ``uvcw gc`` budgets against, because it is
    the metric the user sees.

``physical_bytes``
    The real footprint. Inode dedupe on *both* platforms. uv 0.12.7 added the
    ``content-addressed-cache`` preview feature, which hardlinks identical files within and
    across cached wheels, so on Windows this can be far below ``disk_bytes``.

``reclaim_bytes``
    uv's own estimate of what removing the entry would free, from
    ``crates/uv-cache/src/removal.rs``: ``blocks * 512`` when ``nlink == 1`` and 0 otherwise on
    Unix, ``len()`` on Windows.
"""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Container, Iterator, List, Optional, Set, Tuple

POSIX = os.name != "nt" and hasattr(os.stat_result, "st_blocks")

_SIZE_UNITS = {
    "": 1,
    "b": 1,
    "k": 10**3,
    "kb": 10**3,
    "kib": 2**10,
    "m": 10**6,
    "mb": 10**6,
    "mib": 2**20,
    "g": 10**9,
    "gb": 10**9,
    "gib": 2**30,
    "t": 10**12,
    "tb": 10**12,
    "tib": 2**40,
}

_DURATION_UNITS = {
    "s": 1.0,
    "sec": 1.0,
    "secs": 1.0,
    "m": 60.0,
    "min": 60.0,
    "mins": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hrs": 3600.0,
    "d": 86400.0,
    "day": 86400.0,
    "days": 86400.0,
    "w": 604800.0,
    "week": 604800.0,
    "weeks": 604800.0,
}

_NUMBER = r"(?P<value>\d+(?:\.\d+)?)"
_SIZE_RE = re.compile(_NUMBER + r"\s*(?P<unit>[a-zA-Z]*)$")
_DURATION_RE = re.compile(_NUMBER + r"\s*(?P<unit>[a-zA-Z]+)$")


def parse_size(text: str) -> int:
    """``"10GB"`` -> 10_000_000_000. Accepts decimal (kB/MB/GB/TB) and binary (KiB/MiB/...)."""
    match = _SIZE_RE.match(text.strip())
    if match is None:
        raise ValueError(
            "{!r} is not a size. Use a number with an optional unit, e.g. 10GB, 500MiB, "
            "2.5GiB or a bare byte count.".format(text)
        )
    unit = match.group("unit").lower()
    if unit not in _SIZE_UNITS:
        raise ValueError(
            "{!r} has an unknown size unit {!r}. Known units: B, kB, KiB, MB, MiB, GB, GiB, "
            "TB, TiB.".format(text, match.group("unit"))
        )
    value = float(match.group("value")) * _SIZE_UNITS[unit]
    if value < 0:
        raise ValueError("Size must not be negative: {!r}".format(text))
    return int(value)


def parse_duration(text: str) -> float:
    """``"7d"`` -> 604800.0 seconds. Accepts s/m/h/d/w."""
    match = _DURATION_RE.match(text.strip())
    if match is None:
        raise ValueError(
            "{!r} is not a duration. Use a number and a unit, e.g. 30m, 12h, 7d or 2w.".format(
                text
            )
        )
    unit = match.group("unit").lower()
    if unit not in _DURATION_UNITS:
        raise ValueError(
            "{!r} has an unknown duration unit {!r}. Known units: s, m, h, d, w.".format(
                text, match.group("unit")
            )
        )
    return float(match.group("value")) * _DURATION_UNITS[unit]


def format_size(num_bytes: int) -> str:
    """Binary units, matching how uv prints sizes."""
    value = float(num_bytes)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    index = 0
    while abs(value) >= 1024.0 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    if index == 0:
        return "{:.0f} B".format(value)
    return "{:.1f} {}".format(value, units[index])


def format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 0:
        seconds = 0.0
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return "{:.0f} minutes ago".format(seconds / 60)
    if seconds < 172800:
        return "{:.0f} hours ago".format(seconds / 3600)
    return "{:.0f} days ago".format(seconds / 86400)


@dataclass
class Usage:
    """Accumulated bytes for one subtree or one package."""

    disk_bytes: int = 0
    physical_bytes: int = 0
    reclaim_bytes: int = 0
    files: int = 0
    dirs: int = 0
    hardlinked_files: int = 0
    shared_bytes: int = 0
    """Physical bytes skipped because another counted path already owns that inode."""
    last_used: Optional[float] = None

    def merge(self, other: "Usage") -> None:
        self.disk_bytes += other.disk_bytes
        self.physical_bytes += other.physical_bytes
        self.reclaim_bytes += other.reclaim_bytes
        self.files += other.files
        self.dirs += other.dirs
        self.hardlinked_files += other.hardlinked_files
        self.shared_bytes += other.shared_bytes
        self.touch(other.last_used)

    def touch(self, when: Optional[float]) -> None:
        if when is None:
            return
        if self.last_used is None or when > self.last_used:
            self.last_used = when


@dataclass
class Counter:
    """Walks the cache once, holding the inode set that makes dedupe global.

    Dedupe has to span the whole cache rather than each subtree, or a file hardlinked between
    two packages would be counted twice in the total.
    """

    seen_inodes: Set[Tuple[int, int]] = field(default_factory=set)
    atime_equal_mtime: int = 0
    atime_samples: int = 0

    def add(self, usage: Usage, st: os.stat_result, is_dir: bool) -> None:
        unit = _disk_unit(st, is_dir)
        if is_dir:
            usage.dirs += 1
            usage.disk_bytes += unit
            usage.physical_bytes += unit
            usage.touch(last_used_of(st))
            return

        usage.files += 1
        usage.touch(last_used_of(st))
        self.atime_samples += 1
        if int(st.st_atime) == int(st.st_mtime):
            self.atime_equal_mtime += 1

        hardlinked = st.st_nlink > 1
        if hardlinked:
            usage.hardlinked_files += 1
        key = (st.st_dev, st.st_ino)
        duplicate = hardlinked and st.st_ino != 0 and key in self.seen_inodes
        if hardlinked and st.st_ino != 0:
            self.seen_inodes.add(key)

        # diskus dedupes hardlinks on Unix only.
        usage.disk_bytes += 0 if (duplicate and POSIX) else unit
        if duplicate:
            usage.shared_bytes += unit
        else:
            usage.physical_bytes += unit
        usage.reclaim_bytes += _reclaim_unit(st)

    @property
    def atime_looks_frozen(self) -> bool:
        """Heuristic for a noatime mount: atime never diverges from mtime anywhere."""
        if self.atime_samples < 20:
            return False
        return self.atime_equal_mtime >= self.atime_samples * 0.99


def _disk_unit(st: os.stat_result, is_dir: bool) -> int:
    if POSIX:
        return int(st.st_blocks) * 512
    # diskus counts `metadata.len()`, and on NTFS a directory reports its index allocation
    # (0 while the index is resident, 4096 and up once it is not). Excluding directories here
    # put us 139 KiB under `uv cache size` on a 7 MiB test cache.
    return int(st.st_size)


def _reclaim_unit(st: os.stat_result) -> int:
    """crates/uv-cache/src/removal.rs `file_size`."""
    if POSIX:
        return int(st.st_blocks) * 512 if st.st_nlink == 1 else 0
    return int(st.st_size)


def is_reparse(st: os.stat_result) -> bool:
    """True for anything that redirects elsewhere: a symlink, or a Windows junction.

    `S_ISLNK` is false for a junction and `os.path.islink` returns False for one, so a walk that
    only checks for symlinks descends straight through it. Rust's `FileType::is_symlink` covers
    both, which is why uv and diskus stop at one.
    """
    if stat.S_ISLNK(st.st_mode):
        return True
    attributes = getattr(st, "st_file_attributes", 0)
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def last_used_of(st: os.stat_result) -> float:
    """Newest of atime and mtime.

    Under `noatime` the access time is frozen at creation, so taking the max degrades LRU to
    FIFO instead of reporting a nonsense date. `doctor` and `gc` both say when that happens.
    """
    return max(st.st_atime, st.st_mtime)


def stat_entry(entry: "os.DirEntry[str]") -> Optional[os.stat_result]:
    """Stat without following links, forcing a real stat where DirEntry would not give inodes.

    On Windows `os.DirEntry.stat()` returns st_ino, st_dev and st_nlink as zero, which would
    silently disable hardlink dedupe on exactly the platform where uv's content-addressed cache
    makes it matter most.
    """
    try:
        if POSIX:
            return entry.stat(follow_symlinks=False)
        if entry.is_symlink():
            return entry.stat(follow_symlinks=False)
        return os.stat(entry.path, follow_symlinks=False)
    except OSError:
        return None


def scandir_tree(
    root: Path, prune: Container[str] = frozenset()
) -> Iterator[Tuple["os.DirEntry[str]", Tuple[str, ...]]]:
    """Yield (entry, parts-relative-to-root) for everything under `root`, depth first.

    Links are yielded as entries in their own right and never descended into, so an escaping
    symlink or junction cannot take a walk outside the cache. `prune` names directories not to
    descend, which is how the attribution pass skips the unpacked `src` trees it has no use for.
    """
    stack: List[Tuple[str, Tuple[str, ...]]] = [(str(root), ())]
    while stack:
        current, prefix = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            parts = prefix + (entry.name,)
            yield entry, parts
            if entry.name in prune:
                continue
            try:
                # `entry.stat` is free on Windows and cached on POSIX; it is only the inode
                # fields that cost an extra call, and the descend decision does not need them.
                if entry.is_dir(follow_symlinks=False) and not is_reparse(
                    entry.stat(follow_symlinks=False)
                ):
                    stack.append((entry.path, parts))
            except OSError:
                continue


def walk(root: Path) -> Iterator[Tuple[str, Tuple[str, ...], os.stat_result, bool]]:
    """Yield (path, parts-relative-to-root, stat, is_dir) for every entry under `root`."""
    for entry, parts in scandir_tree(root):
        st = stat_entry(entry)
        if st is None:
            continue
        yield entry.path, parts, st, stat.S_ISDIR(st.st_mode) and not is_reparse(st)


def measure_tree(root: Path, counter: Optional[Counter] = None) -> Usage:
    """Total one subtree, including `root` itself.

    diskus counts the path it was given, so the cache root's own directory entry is part of
    what `uv cache size` reports.
    """
    counter = counter if counter is not None else Counter()
    usage = Usage()
    try:
        root_st = os.stat(str(root), follow_symlinks=False)
    except OSError:
        return usage
    counter.add(usage, root_st, stat.S_ISDIR(root_st.st_mode))
    for _path, _parts, st, is_dir in walk(root):
        counter.add(usage, st, is_dir)
    return usage
