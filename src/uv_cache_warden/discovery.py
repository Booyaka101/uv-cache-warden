"""Locate uv's cache directory and confirm what we found really is one.

The guard matters more than the search: `uvcw gc` hands package names to `uv cache clean`, and
pointing that at the wrong directory is how you lose a home directory. Nothing proceeds unless
the path holds at least one directory whose name matches a known uv bucket.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .uvcmd import Uv, UvFailed

BUCKET_PREFIXES = frozenset(
    {
        "archive",
        "binaries",
        "builds",
        "environments",
        "files",
        "flat-index",
        "git",
        "interpreter",
        "osv",
        "python",
        "sdists",
        "simple",
        "wheels",
    }
)
"""Bucket names minus their `-v<n>` suffix, from crates/uv-cache/src/lib.rs `CacheBucket::to_str`.

Matching on the prefix means a bucket version bump (`wheels-v6` -> `wheels-v7`) does not break
the tool.
"""

NAME_INDEXED = ("wheels", "sdists", "simple")
"""Buckets whose layout puts the package name in the path, so we can attribute their bytes."""

METADATA_BUCKETS = frozenset({"simple"})
"""Buckets whose timestamps say nothing about whether a package's bytes were used.

`uv lock` revalidates the simple index for every package it resolves, so those entries are all
freshly written even for a package nobody has installed in months.
"""

PROTECTED_PREFIXES = frozenset({"python", "osv"})
"""Managed interpreters and the vulnerability database. v1 never evicts these, by design."""

_BUCKET_RE = re.compile(r"^(?P<prefix>[a-z][a-z-]*)-v(?P<version>\d+)$")


class DiscoveryError(Exception):
    """The cache directory could not be located, or is not a uv cache."""


@dataclass(frozen=True)
class CacheLayout:
    root: Path
    source: str
    """How the root was found, e.g. "uv cache dir" or "$UV_CACHE_DIR"."""
    buckets: Dict[str, Path]
    """Bucket prefix -> directory, e.g. {"wheels": .../wheels-v6}."""
    bucket_names: Dict[str, str]
    """Bucket prefix -> the versioned directory name actually on disk."""
    unknown: List[str]
    """Directory entries that look like buckets but are not in BUCKET_PREFIXES."""

    def path(self, prefix: str) -> Optional[Path]:
        return self.buckets.get(prefix)


def bucket_prefix(name: str) -> Optional[str]:
    """`"wheels-v6"` -> `"wheels"`. None when the name is not bucket-shaped at all."""
    match = _BUCKET_RE.match(name)
    return match.group("prefix") if match else None


def candidate_dirs() -> List[Tuple[str, Path]]:
    """Every fallback location, in the order uv itself documents them."""
    out: List[Tuple[str, Path]] = []
    override = os.environ.get("UVCW_CACHE_DIR")
    if override:
        out.append(("$UVCW_CACHE_DIR", Path(override)))
    uv_cache = os.environ.get("UV_CACHE_DIR")
    if uv_cache:
        out.append(("$UV_CACHE_DIR", Path(uv_cache)))
    xdg = os.environ.get("XDG_CACHE_HOME")
    if xdg:
        out.append(("$XDG_CACHE_HOME/uv", Path(xdg) / "uv"))
    home = Path(os.path.expanduser("~"))
    out.append(("~/.cache/uv", home / ".cache" / "uv"))
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        out.append((r"%LOCALAPPDATA%\uv\cache", Path(local_appdata) / "uv" / "cache"))
    return out


def discover_cache_dir(
    uv: Optional[Uv] = None, override: Optional[Path] = None
) -> Tuple[Path, str]:
    """Return (path, source). `uv cache dir` wins; the documented fallbacks follow."""
    if override is not None:
        return Path(override), "--cache-dir"

    env_override = os.environ.get("UVCW_CACHE_DIR")
    if env_override:
        return Path(env_override), "$UVCW_CACHE_DIR"

    if uv is not None:
        try:
            resolved = uv.resolve_cache_dir()
        except UvFailed:
            resolved = None
        if resolved is not None and str(resolved):
            return resolved, "uv cache dir"

    for source, path in candidate_dirs():
        if path.is_dir():
            return path, source

    raise DiscoveryError(
        "Could not locate uv's cache. Tried `uv cache dir` and the documented fallbacks "
        "($UV_CACHE_DIR, $XDG_CACHE_HOME/uv, ~/.cache/uv, %LOCALAPPDATA%\\uv\\cache). "
        "Pass --cache-dir explicitly."
    )


def load_layout(
    root: Path, source: str = "explicit", *, allow_empty: bool = False
) -> CacheLayout:
    """Read the inventory. Only refreshes after native mutation may accept no buckets.

    Initial discovery must retain the default guard. uv pruning can remove every recognized
    bucket from an already-validated cache; its remaining files still count toward the budget.
    """
    root = Path(root)
    if not root.exists():
        raise DiscoveryError("Cache directory does not exist: {}".format(root))
    if not root.is_dir():
        raise DiscoveryError("Cache path is not a directory: {}".format(root))

    resolved = root.resolve()
    if resolved.parent == resolved:
        raise DiscoveryError("Refusing to treat a filesystem root as a cache: {}".format(resolved))
    if resolved == Path(os.path.expanduser("~")).resolve():
        raise DiscoveryError(
            "Refusing to treat your home directory as a cache: {}".format(resolved)
        )

    buckets: Dict[str, Path] = {}
    bucket_names: Dict[str, str] = {}
    bucket_versions: Dict[str, int] = {}
    unknown: List[str] = []
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError as exc:
        raise DiscoveryError("Cannot read cache directory {}: {}".format(root, exc))

    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        match = _BUCKET_RE.match(entry.name)
        if match is None:
            continue
        prefix = match.group("prefix")
        if prefix not in BUCKET_PREFIXES:
            unknown.append(entry.name)
            continue
        # Keep the highest version if uv ever leaves two generations behind.
        version = int(match.group("version"))
        if version >= bucket_versions.get(prefix, -1):
            buckets[prefix] = Path(entry.path)
            bucket_names[prefix] = entry.name
            bucket_versions[prefix] = version

    if not buckets and not allow_empty:
        raise DiscoveryError(
            "{} contains no uv cache buckets (expected directories such as wheels-v6, "
            "sdists-v9 or archive-v0). Refusing to touch it.".format(root)
        )

    return CacheLayout(
        root=root,
        source=source,
        buckets=buckets,
        bucket_names=bucket_names,
        unknown=unknown,
    )
