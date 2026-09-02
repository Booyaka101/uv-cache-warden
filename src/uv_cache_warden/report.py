"""One pass over the cache, classified into packages, buckets and leftovers.

Everything else in the tool reads a `Scan`: `report` renders it, `gc` sorts it by last-used and
`doctor` inspects its bucket inventory. The pass is single because hardlink dedupe has to be
global, and walking twice with two inode sets would double-count.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import attribute
from .attribute import Attribution
from .discovery import NAME_INDEXED, PROTECTED_PREFIXES, CacheLayout, bucket_prefix
from .sizing import Counter, Usage, format_age, format_size, walk
from .uvcmd import Uv, UvError

ROOT_BUCKET = "(root)"
"""Where loose entries such as CACHEDIR.TAG land in the per-bucket inventory."""

SORT_SIZE = "size"
SORT_AGE = "age"
SORT_NAME = "name"
SORT_ORDERS = (SORT_SIZE, SORT_AGE, SORT_NAME)


@dataclass
class PackageUsage:
    name: str
    usage: Usage
    versions: int = 0
    archives: int = 0

    @property
    def last_used(self) -> Optional[float]:
        return self.usage.last_used


@dataclass
class Scan:
    layout: CacheLayout
    packages: Dict[str, PackageUsage] = field(default_factory=dict)
    buckets: Dict[str, Usage] = field(default_factory=dict)
    unattributed: Usage = field(default_factory=Usage)
    protected: Usage = field(default_factory=Usage)
    """Bytes in buckets v1 refuses to evict by name: python-v0 and osv-v0."""
    total: Usage = field(default_factory=Usage)
    counter: Counter = field(default_factory=Counter)
    attribution: Attribution = field(default_factory=Attribution)
    shared_archives: int = 0
    unreferenced_archives: int = 0
    unreferenced_bytes: int = 0
    """Archive bytes with no link pointing at them. Only `uv cache prune` can reclaim these."""
    scanned_at: float = field(default_factory=time.time)
    uv_consulted: bool = False
    uv_cache_size: Optional[int] = None
    uv_version: Optional[str] = None

    @property
    def atime_reliable(self) -> bool:
        return not self.counter.atime_looks_frozen

    def sorted_packages(self, order: str = SORT_SIZE) -> List[PackageUsage]:
        if order == SORT_AGE:
            return self.by_last_used()
        if order == SORT_NAME:
            return sorted(self.packages.values(), key=lambda p: p.name)
        return sorted(self.packages.values(), key=lambda p: (-p.usage.disk_bytes, p.name))

    def by_last_used(self) -> List[PackageUsage]:
        """Oldest first. Packages with no usable timestamp sort as oldest."""
        return sorted(
            self.packages.values(),
            key=lambda p: (p.last_used if p.last_used is not None else 0.0, p.name),
        )

    def age_of(self, package: PackageUsage) -> Optional[float]:
        if package.last_used is None:
            return None
        return max(0.0, self.scanned_at - package.last_used)

    @property
    def reconciliation_delta(self) -> Optional[float]:
        """Fractional gap between our total and `uv cache size`, or None if uv could not say."""
        if self.uv_cache_size is None or self.uv_cache_size == 0:
            return None
        return abs(self.total.disk_bytes - self.uv_cache_size) / float(self.uv_cache_size)


def scan(layout: CacheLayout, uv: Optional[Uv] = None) -> Scan:
    """Walk the whole cache root once and attribute every byte."""
    attribution = attribute.build(layout)
    result = Scan(layout=layout, attribution=attribution)
    counter = result.counter

    archive_usage: Dict[str, Usage] = {}

    for _path, parts, st, is_dir in walk(layout.root):
        prefix = bucket_prefix(parts[0])
        bucket = prefix if prefix in layout.bucket_names else None
        target = result.buckets.setdefault(bucket or ROOT_BUCKET, Usage())
        entry_usage = Usage()
        counter.add(entry_usage, st, is_dir)
        target.merge(entry_usage)
        result.total.merge(entry_usage)

        if bucket in PROTECTED_PREFIXES:
            result.protected.merge(entry_usage)
            continue

        owner: Optional[str] = None
        if bucket in NAME_INDEXED:
            owner = attribute.package_from_parts(bucket, list(parts[1:]))
        elif bucket == "archive" and len(parts) >= 2:
            archive_id = parts[1]
            archive_usage.setdefault(archive_id, Usage()).merge(entry_usage)
            owner = attribution.sole_owner(archive_id)

        if owner is None:
            result.unattributed.merge(entry_usage)
            continue

        package = result.packages.get(owner)
        if package is None:
            package = PackageUsage(name=owner, usage=Usage())
            result.packages[owner] = package
        package.usage.merge(entry_usage)

    for name, package in result.packages.items():
        package.versions = len(attribution.package_versions.get(name, ()))

    for archive_id, usage in archive_usage.items():
        owners = attribution.archive_owners.get(archive_id)
        if not owners:
            result.unreferenced_archives += 1
            result.unreferenced_bytes += usage.disk_bytes
        elif len(owners) > 1:
            result.shared_archives += 1
        else:
            owned = result.packages.get(next(iter(owners)))
            if owned is not None:
                owned.archives += 1

    if uv is not None:
        result.uv_consulted = True
        try:
            result.uv_cache_size = uv.cache_size()
        except UvError:
            result.uv_cache_size = None
        try:
            result.uv_version = uv.version()
        except UvError:
            result.uv_version = None

    return result


def to_json(result: Scan, top: Optional[int] = None, sort: str = SORT_SIZE) -> dict:
    packages = result.sorted_packages(sort)
    if top is not None:
        packages = packages[:top]
    return {
        "cache_dir": str(result.layout.root),
        "cache_dir_source": result.layout.source,
        "scanned_at": result.scanned_at,
        "uv_version": result.uv_version,
        "uv_cache_size_bytes": result.uv_cache_size,
        "reconciliation_delta": result.reconciliation_delta,
        "atime_reliable": result.atime_reliable,
        "buckets": {
            result.layout.bucket_names.get(name, name): _usage_json(usage)
            for name, usage in sorted(result.buckets.items())
        },
        "packages": [
            {
                "name": package.name,
                "versions": package.versions,
                "archives": package.archives,
                "last_used": package.last_used,
                "age_seconds": result.age_of(package),
                **_usage_json(package.usage),
            }
            for package in packages
        ],
        "package_count": len(result.packages),
        "unattributed": _usage_json(result.unattributed),
        "protected": _usage_json(result.protected),
        "unreferenced_archives": result.unreferenced_archives,
        "unreferenced_bytes": result.unreferenced_bytes,
        "shared_archives": result.shared_archives,
        "unresolved_links": result.attribution.unresolved_links,
        "total": _usage_json(result.total),
    }


def _usage_json(usage: Usage) -> dict:
    return {
        "disk_bytes": usage.disk_bytes,
        "physical_bytes": usage.physical_bytes,
        "reclaim_bytes": usage.reclaim_bytes,
        "shared_bytes": usage.shared_bytes,
        "files": usage.files,
        "dirs": usage.dirs,
        "hardlinked_files": usage.hardlinked_files,
        "last_used": usage.last_used,
    }


def render(result: Scan, top: Optional[int] = None, sort: str = SORT_SIZE) -> str:
    packages = result.sorted_packages(sort)
    shown = packages[:top] if top is not None else packages
    hidden = len(packages) - len(shown)

    rows: List[Tuple[str, str, str, str]] = [("PACKAGE", "VERSIONS", "SIZE", "LAST USED")]
    for package in shown:
        rows.append(
            (
                package.name,
                str(package.versions) if package.versions else "-",
                format_size(package.usage.disk_bytes),
                format_age(result.age_of(package)),
            )
        )
    if hidden > 0:
        remainder = sum(p.usage.disk_bytes for p in packages[len(shown) :])
        rows.append(
            ("... {} more".format(hidden), "", format_size(remainder), ""),
        )
    if result.unattributed.disk_bytes or not packages:
        rows.append(("unattributed", "", format_size(result.unattributed.disk_bytes), ""))
    if result.protected.disk_bytes:
        rows.append(("protected", "", format_size(result.protected.disk_bytes), ""))

    total_note = ""
    if result.uv_cache_size is not None:
        total_note = "(uv cache size: {})".format(format_size(result.uv_cache_size))
    rows.append(("TOTAL", "", format_size(result.total.disk_bytes), total_note))

    lines = [_format_table(rows)]
    lines.extend(_footnotes(result))
    return "\n".join(lines)


def _footnotes(result: Scan) -> List[str]:
    notes: List[str] = []
    reclaim = result.total.reclaim_bytes
    if abs(reclaim - result.total.disk_bytes) > result.total.disk_bytes * 0.01:
        notes.append(
            "Clearing the whole cache would free {} by uv's own accounting; the {} above is "
            "what `uv cache size` reports.".format(
                format_size(reclaim), format_size(result.total.disk_bytes)
            )
        )
    if result.total.shared_bytes:
        notes.append(
            "{} of hardlinked content is shared between cache entries; the real footprint is "
            "{}.".format(
                format_size(result.total.shared_bytes), format_size(result.total.physical_bytes)
            )
        )
    if result.unreferenced_archives:
        notes.append(
            "{} unreferenced archive(s) holding {} can only be reclaimed by `uv cache "
            "prune`.".format(
                result.unreferenced_archives, format_size(result.unreferenced_bytes)
            )
        )
    if result.shared_archives:
        notes.append(
            "{} archive(s) are referenced by more than one package and are counted as "
            "unattributed.".format(result.shared_archives)
        )
    if not result.atime_reliable:
        notes.append(
            "Access times on this filesystem never differ from modification times "
            "(noatime?), so LAST USED reflects when an entry was written."
        )
    delta = result.reconciliation_delta
    if delta is not None and delta > 0.02:
        notes.append(
            "Our total differs from `uv cache size` by {:.1%}; the cache probably changed "
            "while we scanned it.".format(delta)
        )
    if result.uv_consulted and result.uv_cache_size is None:
        notes.append(
            "This uv has no `uv cache size --output-format machine`; totals are measured "
            "directly and cannot be cross-checked."
        )
    return notes


def _format_table(rows: List[Tuple[str, str, str, str]]) -> str:
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    lines = []
    for row in rows:
        cells = [row[i].ljust(widths[i]) for i in range(4)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)
