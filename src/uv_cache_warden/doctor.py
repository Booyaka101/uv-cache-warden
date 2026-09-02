"""Environment diagnostics: what uvcw can see, and what it will get wrong here.

The two questions worth answering before trusting a budget are whether our byte count agrees
with uv's, and whether access times mean anything on this filesystem. Both are checked against
the real machine rather than assumed.
"""

import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from . import __version__, report as report_mod
from .discovery import PROTECTED_PREFIXES, DiscoveryError, discover_cache_dir, load_layout
from .sizing import POSIX, format_size
from .uvcmd import Uv, UvError, UvNotFound

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    extra: dict = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail, **self.extra}


@dataclass
class Diagnosis:
    checks: List[Check] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str, **extra: object) -> None:
        self.checks.append(Check(name, status, detail, dict(extra)))

    @property
    def failed(self) -> bool:
        return any(check.status == FAIL for check in self.checks)

    def as_json(self) -> dict:
        return {
            "uvcw_version": __version__,
            "healthy": not self.failed,
            "checks": [check.as_json() for check in self.checks],
        }


def atime_policy(path: Path) -> Optional[str]:
    """A definitive answer about access-time tracking, when the OS can give one.

    Windows disables NTFS last-access updates by default, and Linux mounts are commonly
    `relatime` or `noatime`. Either turns LRU into write-order, which is worth saying out loud
    rather than inferring from timestamps.
    """
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Control\FileSystem",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "NtfsDisableLastAccessUpdate")
        except (ImportError, OSError):
            return None
        # Bit 0 set means updates are disabled; bit 1 means the value is system-managed.
        return "NtfsDisableLastAccessUpdate={} (updates {})".format(
            value, "disabled" if int(value) & 1 else "enabled"
        )

    try:
        with open("/proc/self/mounts", "r") as handle:
            mounts = [line.split() for line in handle]
    except OSError:
        return None
    best = None
    for fields in mounts:
        if len(fields) < 4:
            continue
        point = fields[1]
        if _under(path, point):
            if best is None or len(point) > len(best[1]):
                best = (fields[0], point, fields[3])
    if best is None:
        return None
    options = [opt for opt in best[2].split(",") if opt in ("noatime", "relatime", "strictatime")]
    return "{} mounted at {} with {}".format(
        best[0], best[1], ",".join(options) if options else "default atime options"
    )


def _under(path: Path, mount_point: str) -> bool:
    here = os.path.abspath(str(path))
    return here == mount_point or here.startswith(mount_point.rstrip("/") + "/")


def diagnose(cache_dir: Optional[Path] = None, uv_path: Optional[str] = None) -> Diagnosis:
    result = Diagnosis()
    result.add(
        "uv-cache-warden",
        OK,
        "{} on Python {} ({})".format(
            __version__, platform.python_version(), platform.platform()
        ),
        version=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        executable=sys.executable,
    )

    uv: Optional[Uv] = None
    try:
        uv = Uv.discover(uv_path, echo=False)
        result.add("uv", OK, "{} at {}".format(uv.version(), uv.executable), path=uv.executable)
    except UvNotFound as exc:
        result.add("uv", FAIL, str(exc))
    except UvError as exc:
        result.add("uv", FAIL, str(exc))

    try:
        root, source = discover_cache_dir(uv=uv, override=cache_dir)
    except DiscoveryError as exc:
        result.add("cache directory", FAIL, str(exc))
        return result
    result.add("cache directory", OK, "{} (from {})".format(root, source), path=str(root))

    try:
        layout = load_layout(root, source=source)
    except DiscoveryError as exc:
        result.add("cache layout", FAIL, str(exc))
        return result

    if uv is not None:
        uv.cache_dir = layout.root

    result.add(
        "buckets",
        OK,
        ", ".join(sorted(layout.bucket_names.values())),
        found=sorted(layout.bucket_names.values()),
    )
    if layout.unknown:
        result.add(
            "unknown buckets",
            WARN,
            "{} look like uv buckets but are not ones this version knows. Their bytes count "
            "toward the total and are never attributed.".format(", ".join(sorted(layout.unknown))),
            names=sorted(layout.unknown),
        )

    scan = report_mod.scan(layout, uv)
    inventory = "; ".join(
        "{} {}".format(layout.bucket_names.get(name, name), format_size(usage.disk_bytes))
        for name, usage in sorted(scan.buckets.items())
    )
    result.add(
        "bucket sizes",
        OK,
        inventory or "empty",
        buckets={
            layout.bucket_names.get(name, name): usage.disk_bytes
            for name, usage in scan.buckets.items()
        },
    )

    result.add(
        "packages",
        OK,
        "{} attributed, {} unattributed, {} protected ({})".format(
            len(scan.packages),
            format_size(scan.unattributed.disk_bytes),
            format_size(scan.protected.disk_bytes),
            ", ".join(sorted(PROTECTED_PREFIXES)),
        ),
        count=len(scan.packages),
        unattributed_bytes=scan.unattributed.disk_bytes,
        protected_bytes=scan.protected.disk_bytes,
    )

    delta = scan.reconciliation_delta
    if scan.uv_cache_size is None:
        status = WARN if scan.uv_consulted else OK
        detail = (
            "this uv has no `cache size --output-format machine`; totals come from our own walk"
            if scan.uv_consulted
            else "not checked (uv unavailable)"
        )
        result.add("reconciliation", status, detail, uv_cache_size_bytes=None)
    else:
        status = OK if (delta or 0.0) <= 0.02 else WARN
        result.add(
            "reconciliation",
            status,
            "uvcw {} vs `uv cache size` {} ({:.2%})".format(
                format_size(scan.total.disk_bytes), format_size(scan.uv_cache_size), delta or 0.0
            ),
            uvcw_bytes=scan.total.disk_bytes,
            uv_cache_size_bytes=scan.uv_cache_size,
            delta=delta,
        )

    result.add(
        "hardlinks",
        OK,
        "{} of {} files have more than one link; {} shared inside the cache, {} physical vs "
        "{} counted by uv".format(
            scan.total.hardlinked_files,
            scan.total.files,
            format_size(scan.total.shared_bytes),
            format_size(scan.total.physical_bytes),
            format_size(scan.total.disk_bytes),
        ),
        hardlinked_files=scan.total.hardlinked_files,
        files=scan.total.files,
        shared_bytes=scan.total.shared_bytes,
        physical_bytes=scan.total.physical_bytes,
    )

    policy = atime_policy(layout.root)
    if scan.atime_reliable:
        detail = "access times track reads, so eviction order is true LRU"
        if policy:
            detail = "{}; {}".format(detail, policy)
        result.add("access times", OK, detail, reliable=True, policy=policy)
    else:
        detail = (
            "access times never differ from modification times, so eviction falls back to "
            "write order"
        )
        if policy:
            detail = "{}; {}".format(detail, policy)
        result.add("access times", WARN, detail, reliable=False, policy=policy)

    result.add(
        "sizing mode",
        OK,
        "st_blocks*512 with hardlink dedupe" if POSIX else "st_size, uv does not dedupe here",
        posix=POSIX,
    )
    return result


def render(diagnosis: Diagnosis) -> str:
    width = max(len(check.name) for check in diagnosis.checks)
    marks = {OK: "  ok  ", WARN: " warn ", FAIL: " fail "}
    lines = []
    for check in diagnosis.checks:
        lines.append(
            "[{}] {}  {}".format(marks[check.status], check.name.ljust(width), check.detail)
        )
    return "\n".join(lines)
