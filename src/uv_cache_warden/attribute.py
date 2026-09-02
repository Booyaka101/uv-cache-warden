"""Map cache paths back to the package name `uv cache clean` would accept.

Two mechanisms, both taken from crates/uv-cache/src/lib.rs:

* The `wheels-*`, `sdists-*` and `simple-*` buckets put the package name in the path, under a
  `pypi` / `index` / `url` / `path` / `git` sub-root (`WheelCacheKind`).
* Unpacked wheels live in `archive-*/<id>/` under an opaque hash, and are reached by a link
  from the wheels or sdists bucket. On Unix that link is a symlink; on Windows it is a regular
  file whose entire content is `archive-v<n>/<id>`. Resolving those links is the only way to
  say which package an archive's bytes belong to.
"""

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set

from .discovery import CacheLayout
from .sizing import scandir_tree

LINK_CONTENT_RE = re.compile(r"^archive-v(\d+)[/\\]([^/\\\s]+)$")
PACKAGE_NAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9])?$")

_SIDECAR_SUFFIXES = frozenset({".lock", ".whl", ".http", ".rev", ".msgpack"})
_SIDECAR_NAMES = frozenset({"src", ".lock", ".gitignore"})
"""uv's own filter in `find_archive_references`.

These sit beside a cache entry rather than being one, so they are neither archive links nor a
source of version numbers: `rich-13.7.1-py3-none-any.lock` would otherwise read as version
"rich".
"""

_MAX_LINK_BYTES = 512
"""A link file is ~30 bytes. Anything larger is package data, not a pointer."""


def is_package_name(name: str) -> bool:
    """Reject anything `uv cache clean` would reject, before we hand it over."""
    return bool(PACKAGE_NAME_RE.match(name))


@dataclass(frozen=True)
class LinkRef:
    referrer: Path
    archive_id: str
    package: Optional[str]


@dataclass
class Attribution:
    archive_owners: Dict[str, Set[str]] = field(default_factory=dict)
    """Archive id -> the package names that reference it."""
    links: List[LinkRef] = field(default_factory=list)
    package_versions: Dict[str, Set[str]] = field(default_factory=dict)
    unresolved_links: int = 0
    """Links whose target is missing or whose archive version is stale."""

    def sole_owner(self, archive_id: str) -> Optional[str]:
        """The package to charge an archive to, or None when it is shared or unreferenced.

        `uv cache clean <pkg>` only frees an archive once every referring package is gone, so
        charging a shared archive to one of them would promise bytes we cannot deliver.
        """
        owners = self.archive_owners.get(archive_id)
        if owners and len(owners) == 1:
            return next(iter(owners))
        return None

    def note_version(self, package: str, version: str) -> None:
        if version:
            self.package_versions.setdefault(package, set()).add(version)


def package_from_parts(bucket: str, parts: Sequence[str]) -> Optional[str]:
    """Package name for a path inside a name-indexed bucket, relative to the bucket root.

    Returns None for the layouts uv identifies by reading `metadata.msgpack` (source
    distributions from a URL, a local path or Git), which is the "best effort" line the v1
    scope draws around non-PyPI attribution.
    """
    if not parts:
        return None
    kind = parts[0]
    if bucket == "simple":
        if kind == "pypi" and len(parts) >= 2:
            return _strip_rkyv(parts[1])
        if kind == "index" and len(parts) >= 3:
            return _strip_rkyv(parts[2])
        return None
    if bucket == "wheels":
        if kind == "pypi" and len(parts) >= 2:
            return _valid(parts[1])
        if kind in ("index", "url") and len(parts) >= 3:
            return _valid(parts[2])
        return None
    if bucket == "sdists":
        if kind == "pypi" and len(parts) >= 2:
            return _valid(parts[1])
        if kind == "index" and len(parts) >= 3:
            return _valid(parts[2])
        return None
    return None


def version_from_parts(bucket: str, parts: Sequence[str]) -> Optional[str]:
    """The version-ish path segment, used only for the report's VERSIONS column."""
    if bucket == "wheels":
        if parts[:1] == ["pypi"] and len(parts) >= 3:
            return _wheel_version(parts[2])
        if parts[:1] in (["index"], ["url"]) and len(parts) >= 4:
            return _wheel_version(parts[3])
        return None
    if bucket == "sdists":
        if parts[:1] == ["pypi"] and len(parts) >= 3:
            return parts[2]
        if parts[:1] == ["index"] and len(parts) >= 4:
            return parts[3]
    return None


def read_link(path: Path, archive_version: Optional[int] = None) -> Optional[str]:
    """Return the archive id this cache entry points at, or None if it is not a link.

    Handles both shapes uv writes: a POSIX symlink into `archive-v*/`, and the Windows pointer
    file whose content is exactly `archive-v<n>/<id>`. A link naming a different archive
    version is stale, and uv's own `resolve_link` ignores it rather than following it.
    """
    try:
        st = os.lstat(str(path))
    except OSError:
        return None

    if stat.S_ISLNK(st.st_mode):
        try:
            target = os.readlink(str(path))
        except OSError:
            return None
        resolved = Path(os.path.normpath(os.path.join(str(path.parent), target)))
        parts = resolved.parts
        for index, part in enumerate(parts):
            if part.startswith("archive-v") and index + 1 < len(parts):
                return parts[index + 1]
        return None

    if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_LINK_BYTES:
        return None
    try:
        with open(str(path), "rb") as handle:
            content = handle.read(_MAX_LINK_BYTES + 1).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    match = LINK_CONTENT_RE.match(content)
    if match is None:
        return None
    if archive_version is not None and int(match.group(1)) != archive_version:
        return None
    return match.group(2)


def build(layout: CacheLayout) -> Attribution:
    """Walk the name-indexed buckets once, collecting links and version counts."""
    attribution = Attribution()
    archive_root = layout.path("archive")
    archive_version = _bucket_version(layout.bucket_names.get("archive"))

    for bucket in ("wheels", "sdists"):
        root = layout.path(bucket)
        if root is None:
            continue
        for entry, raw_parts in scandir_tree(root, prune=_SIDECAR_NAMES):
            parts = list(raw_parts)
            if _is_sidecar(parts[-1]):
                continue
            path = Path(entry.path)
            package = package_from_parts(bucket, parts)
            if package is not None:
                version = version_from_parts(bucket, parts)
                if version:
                    attribution.note_version(package, version)
            archive_id = read_link(path, archive_version)
            if archive_id is None:
                continue
            if archive_root is not None and not (archive_root / archive_id).is_dir():
                attribution.unresolved_links += 1
                continue
            attribution.links.append(LinkRef(path, archive_id, package))
            if package is not None:
                attribution.archive_owners.setdefault(archive_id, set()).add(package)
            else:
                attribution.archive_owners.setdefault(archive_id, set())

    return attribution


def _bucket_version(bucket_name: Optional[str]) -> Optional[int]:
    if not bucket_name:
        return None
    _, _, version = bucket_name.rpartition("-v")
    return int(version) if version.isdigit() else None


def _is_sidecar(name: str) -> bool:
    if name in _SIDECAR_NAMES:
        return True
    suffix = os.path.splitext(name)[1].lower()
    return suffix in _SIDECAR_SUFFIXES


def _valid(name: str) -> Optional[str]:
    return name if is_package_name(name) else None


def _strip_rkyv(name: str) -> Optional[str]:
    """The simple index stores exactly one `<package>.rkyv` per package; `.lock` siblings are not."""
    if not name.endswith(".rkyv"):
        return None
    return _valid(name[:-5])


def _wheel_version(entry: str) -> Optional[str]:
    """`"13.7.1-py3-none-any"` -> `"13.7.1"`; the rest of the stem is the wheel tag."""
    head = entry.split("-", 1)[0]
    return head or None
