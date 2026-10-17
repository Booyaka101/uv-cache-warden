"""LRU eviction down to a byte budget, performed entirely through uv subcommands.

Prune first, then select packages oldest-first from refreshed attribution if still over budget.
After successful package cleans, a final prune sweeps newly unreferenced entries.

Projection and reality can disagree, because a package's bytes may be hardlinked to bytes that
survive. So when the measured size is still over budget and eligible packages remain, the whole
analysis runs again. `MAX_ROUNDS` bounds that; a round which evicts nothing ends it.
"""

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import report as report_mod
from .attribute import is_package_name, normalize_name
from .discovery import CacheLayout, DiscoveryError, load_layout
from .report import Scan
from .sizing import format_age, format_size, measure_tree
from .style import LEFT, RIGHT, Style, table
from .uvcmd import Uv, UvFailed

MAX_ROUNDS = 3

BOUND_MAX_SIZE = "--max-size"
BOUND_MIN_FREE = "--min-free"

SKIP_KEEP = "kept by --keep"
SKIP_MIN_AGE = "used within --min-age"
SKIP_INVALID = "not a name `uv cache clean` accepts"


@dataclass
class Candidate:
    name: str
    disk_bytes: int
    age_seconds: Optional[float]
    versions: int

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "disk_bytes": self.disk_bytes,
            "age_seconds": self.age_seconds,
            "versions": self.versions,
        }


@dataclass
class Skipped:
    name: str
    disk_bytes: int
    reason: str

    def as_json(self) -> dict:
        return {"name": self.name, "disk_bytes": self.disk_bytes, "reason": self.reason}


@dataclass
class Plan:
    """What eviction would do, given one scan."""

    budget: int
    size_before: int
    evict: List[Candidate] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    projected: int = 0
    unattributed_bytes: int = 0
    protected_bytes: int = 0
    package_count: int = 0
    """Attributed packages in the cache, so the plan can say how much of it is going."""

    @property
    def over_budget(self) -> bool:
        return self.size_before > self.budget


@dataclass
class Eviction:
    name: str
    disk_bytes: int
    ok: bool
    detail: str = ""

    def as_json(self) -> dict:
        return {
            "name": self.name,
            "disk_bytes": self.disk_bytes,
            "ok": self.ok,
            "detail": self.detail,
        }


@dataclass
class Result:
    cache_dir: str
    budget: int
    size_before: int
    size_after: int
    dry_run: bool
    evicted: List[Eviction] = field(default_factory=list)
    skipped: List[Skipped] = field(default_factory=list)
    plans: List[Plan] = field(default_factory=list)
    pruned: bool = False
    prune_detail: str = ""
    prune_steps: List[dict] = field(default_factory=list)
    eviction_projection_conditional: bool = False
    atime_reliable: bool = True
    bound_by: str = BOUND_MAX_SIZE
    free_before: Optional[int] = None
    free_after: Optional[int] = None
    rounds: int = 0
    duration: float = 0.0

    @property
    def reclaimed(self) -> int:
        return max(0, self.size_before - self.size_after)

    @property
    def succeeded(self) -> int:
        return sum(1 for item in self.evicted if item.ok)

    @property
    def failed(self) -> List[Eviction]:
        return [item for item in self.evicted if not item.ok]

    @property
    def projected_after(self) -> int:
        """Bytes we expect to be left. For a dry run that is a projection, not a measurement."""
        if self.dry_run and self.plans:
            return self.plans[0].projected
        return self.size_after

    @property
    def over_budget(self) -> bool:
        return self.projected_after > self.budget


def budget_for(
    cache_size: int,
    root: Path,
    max_size: Optional[int] = None,
    min_free: Optional[int] = None,
) -> Tuple[int, str]:
    """Turn the size constraints into one byte budget, and name whichever one binds.

    `--min-free` is about the filesystem, not the cache: to leave N bytes free we have to give
    back whatever the disk is short, so the cache budget is its current size minus that shortfall.
    When both are given the tighter one wins.
    """
    candidates: List[Tuple[int, str]] = []
    if max_size is not None:
        candidates.append((max_size, BOUND_MAX_SIZE))
    if min_free is not None:
        try:
            free = shutil.disk_usage(str(root)).free
        except OSError as exc:
            raise DiscoveryError(
                "Cannot read free space for {}: {}".format(root, exc)
            )
        shortfall = max(0, min_free - free)
        candidates.append((max(0, cache_size - shortfall), BOUND_MIN_FREE))
    if not candidates:
        raise ValueError("at least one of max_size or min_free is required")
    return min(candidates, key=lambda item: item[0])


def plan(
    scan: Scan,
    budget: int,
    min_age: float = 0.0,
    keep: Sequence[str] = (),
    size_before: Optional[int] = None,
    excluded: Sequence[str] = (),
) -> Plan:
    """Decide which packages to evict, oldest first, stopping as soon as the budget is met."""
    keep_set = {normalize_name(name) for name in keep}
    excluded_set = {normalize_name(name) for name in excluded}
    current = size_before if size_before is not None else scan.total.disk_bytes
    result = Plan(
        budget=budget,
        size_before=current,
        projected=current,
        unattributed_bytes=scan.unattributed.disk_bytes,
        protected_bytes=scan.protected.disk_bytes,
        package_count=len(scan.packages),
    )
    if current <= budget:
        return result

    projected = current
    for package in scan.by_last_used():
        normalized = normalize_name(package.name)
        if normalized in excluded_set:
            continue
        age = scan.age_of(package)
        if normalized in keep_set:
            result.skipped.append(Skipped(package.name, package.usage.disk_bytes, SKIP_KEEP))
            continue
        if not is_package_name(package.name):
            result.skipped.append(Skipped(package.name, package.usage.disk_bytes, SKIP_INVALID))
            continue
        if min_age > 0 and (age is None or age < min_age):
            result.skipped.append(Skipped(package.name, package.usage.disk_bytes, SKIP_MIN_AGE))
            continue
        if projected <= budget:
            continue
        result.evict.append(
            Candidate(
                name=package.name,
                disk_bytes=package.usage.disk_bytes,
                age_seconds=age,
                versions=package.versions,
            )
        )
        excluded_set.add(normalized)
        projected -= package.usage.disk_bytes

    result.projected = projected
    return result


def run(
    layout: CacheLayout,
    uv: Uv,
    budget: int,
    min_age: float = 0.0,
    keep: Sequence[str] = (),
    dry_run: bool = False,
    prune: bool = True,
    progress: Optional[Callable[[int], None]] = None,
    bound_by: str = BOUND_MAX_SIZE,
) -> Result:
    """Prune before selecting packages. `dry_run` invokes no mutating uv command."""
    started = time.time()
    scan = report_mod.scan(layout, uv, progress=progress)
    size_before = scan.uv_cache_size if scan.uv_cache_size is not None else scan.total.disk_bytes

    result = Result(
        cache_dir=str(layout.root),
        budget=budget,
        size_before=size_before,
        size_after=size_before,
        dry_run=dry_run,
        atime_reliable=scan.atime_reliable,
        bound_by=bound_by,
        free_before=_free(layout.root),
        eviction_projection_conditional=dry_run and prune and size_before > budget,
    )

    initially_over = size_before > budget
    current_scan: Optional[Scan] = scan
    current_size = size_before
    evicted_names: Dict[str, None] = {}

    if initially_over and not dry_run and prune:
        _prune(result, "initial", uv, layout)
        current_scan = None
        current_size = result.size_after

    for round_index in range(MAX_ROUNDS):
        if current_scan is None:
            layout = load_layout(layout.root, source=layout.source, allow_empty=True)
            current_scan = report_mod.scan(layout, uv, progress=progress)
            current_size = (
                current_scan.uv_cache_size
                if current_scan.uv_cache_size is not None
                else current_scan.total.disk_bytes
            )
            result.size_after = current_size
        step = plan(
            current_scan,
            budget,
            min_age=min_age,
            keep=keep,
            size_before=current_size,
            excluded=tuple(evicted_names),
        )
        result.plans.append(step)
        result.rounds = round_index + 1
        if round_index == 0:
            result.skipped = step.skipped

        if not step.evict:
            break
        if dry_run:
            result.evicted = [Eviction(c.name, c.disk_bytes, True, "dry run") for c in step.evict]
            break

        for candidate in step.evict:
            # Recorded before the call, so a package uv refused to clean is not retried next round.
            evicted_names[candidate.name] = None
            outcome = uv.cache_clean(candidate.name)
            if outcome.ok:
                result.evicted.append(Eviction(candidate.name, candidate.disk_bytes, True))
            else:
                result.evicted.append(
                    Eviction(
                        candidate.name,
                        candidate.disk_bytes,
                        False,
                        _first_error(outcome.stderr) or "exit {}".format(outcome.returncode),
                    )
                )

        current_scan = None
        measured = _measure(uv, layout)
        current_size = measured
        result.size_after = measured
        if measured <= budget:
            break

    if not dry_run and prune and result.succeeded:
        _prune(result, "final", uv, layout)

    result.free_after = _free(layout.root)
    result.duration = time.time() - started
    return result


def _prune(result: Result, stage: str, uv: Uv, layout: CacheLayout) -> None:
    outcome = uv.cache_prune()
    detail = ""
    if not outcome.ok:
        detail = _first_error(outcome.stderr) or "exit {}".format(outcome.returncode)
        failure = "{}: {}".format(stage, detail)
        result.prune_detail = "; ".join(filter(None, (result.prune_detail, failure)))
    result.pruned = result.pruned or outcome.ok
    # A nonzero exit can follow partial mutation. Always measure before doing anything else.
    result.size_after = _measure(uv, layout)
    result.prune_steps.append({
        "stage": stage,
        "ok": outcome.ok,
        "detail": detail,
        "size_after_bytes": result.size_after,
    })


def _free(root: Path) -> Optional[int]:
    try:
        return shutil.disk_usage(str(root)).free
    except OSError:
        return None


def _measure(uv: Uv, layout: CacheLayout) -> int:
    try:
        size = uv.cache_size()
    except UvFailed:
        size = None
    if size is not None:
        return size
    return measure_tree(layout.root).disk_bytes


def _first_error(stderr: str) -> str:
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("error"):
            return stripped
        if stripped:
            return stripped
    return ""


def render(result: Result, style: Optional[Style] = None) -> str:
    style = style if style is not None else Style()
    lines: List[str] = [
        "{}  budget {} ({})  currently {}".format(
            style.heading("cache " + result.cache_dir),
            style.heading(format_size(result.budget)),
            result.bound_by,
            style.heading(format_size(result.size_before)),
        )
    ]
    if result.free_before is not None:
        lines.append(
            style.dim("  {} free on this filesystem".format(format_size(result.free_before)))
        )

    if result.size_before <= result.budget:
        lines.append(
            style.paint(
                "Already under budget ({} <= {}). Nothing to do.".format(
                    format_size(result.size_before), format_size(result.budget)
                ),
                "green",
            )
        )
        return "\n".join(lines)

    if not result.atime_reliable:
        lines.append(
            style.note(
                "access times are not tracked on this filesystem, so eviction order is by "
                "write time rather than true last use."
            )
        )

    if not result.evicted:
        if not result.over_budget and result.prune_steps:
            lines.append("Budget met by initial prune; no packages cleaned.")
        else:
            lines.append("No package is eligible for eviction.")
        if result.pruned and result.reclaimed:
            lines.append(
                "uv cache prune reclaimed {}; cache now {}.".format(
                    format_size(result.reclaimed), format_size(result.size_after)
                )
            )
        lines.extend(_outcome(result, style))
        return "\n".join(lines)

    lines.append("")
    total_packages = result.plans[0].package_count if result.plans else 0
    lines.append(
        style.heading(
            "{} ({} of {} packages):".format(
                "would evict" if result.dry_run else "evicted",
                len(result.evicted),
                total_packages,
            )
        )
    )
    ages = {c.name: c.age_seconds for step in result.plans for c in step.evict}
    rows = [
        (
            "  " + item.name,
            format_size(item.disk_bytes),
            format_age(ages.get(item.name)),
            "" if item.ok else "FAILED: " + item.detail,
        )
        for item in result.evicted
    ]
    styles = [None if item.ok else ("red",) for item in result.evicted]
    lines.append(table(rows, (LEFT, RIGHT, LEFT, LEFT), styles=styles, style=style))

    lines.append("")
    lines.extend(_outcome(result, style))
    return "\n".join(lines)


def _outcome(result: Result, style: Style) -> List[str]:
    """The closing verdict, identical in shape whether or not anything was actually removed."""
    lines: List[str] = []
    if result.dry_run:
        lines.append(
            "dry run: nothing was removed. Projected {} -> {} (budget {}).".format(
                format_size(result.size_before),
                format_size(result.projected_after),
                format_size(result.budget),
            )
        )
        if result.eviction_projection_conditional:
            lines.append("A real run prunes first; projected package evictions may disappear.")
    else:
        if result.prune_detail:
            lines.append(style.paint("uv cache prune failed: " + result.prune_detail, "red"))
        if result.evicted:
            lines.append(
                "reclaimed {}; cache now {} (budget {}); evicted {} package{}".format(
                    format_size(result.reclaimed),
                    format_size(result.size_after),
                    format_size(result.budget),
                    result.succeeded,
                    "" if result.succeeded == 1 else "s",
                )
            )
        if result.failed:
            lines.append(
                style.paint(
                    "{} package(s) could not be cleaned: {}".format(
                        len(result.failed), ", ".join(item.name for item in result.failed)
                    ),
                    "red",
                )
            )
    if result.over_budget:
        lines.extend(_explain_shortfall(result, style))
    elif not result.dry_run and result.evicted:
        lines.append(style.paint("under budget.", "green"))
    return lines


def _explain_shortfall(result: Result, style: Style) -> List[str]:
    """Say why the budget was not reached, in terms of the rules that held bytes back.

    A dry run has to say this too. Reporting a plan that stops short of the budget without
    saying so reads as success, and `--fail-over` would then exit 2 with no stated reason.
    """
    short = result.projected_after - result.budget
    lines = [
        style.paint(
            "{} still over budget by {}.".format(
                "projected:" if result.dry_run else "result:", format_size(short)
            ),
            "yellow",
        )
    ]
    last = result.plans[-1] if result.plans else None
    if last is None:
        return lines
    held: Dict[str, int] = {}
    for item in last.skipped:
        held[item.reason] = held.get(item.reason, 0) + item.disk_bytes
    for reason, total in sorted(held.items(), key=lambda kv: -kv[1]):
        lines.append("  {} held back by packages {}".format(format_size(total), reason))
    if last.unattributed_bytes:
        lines.append(
            "  {} is unattributed (no package name to pass to `uv cache clean`); only "
            "`uv cache prune` can touch it".format(format_size(last.unattributed_bytes))
        )
    if last.protected_bytes:
        lines.append(
            "  {} is in protected buckets (managed Python interpreters, vulnerability "
            "database) which uvcw never evicts".format(format_size(last.protected_bytes))
        )
    return lines


def to_json(result: Result) -> dict:
    return {
        "cache_dir": result.cache_dir,
        "dry_run": result.dry_run,
        "budget_bytes": result.budget,
        "size_before_bytes": result.size_before,
        "size_after_bytes": result.size_after,
        "reclaimed_bytes": result.reclaimed,
        "over_budget": result.over_budget,
        "atime_reliable": result.atime_reliable,
        "bound_by": result.bound_by,
        "free_before_bytes": result.free_before,
        "free_after_bytes": result.free_after,
        "rounds": result.rounds,
        "pruned": result.pruned,
        "prune_detail": result.prune_detail,
        "prune_steps": result.prune_steps,
        "eviction_projection_conditional": result.eviction_projection_conditional,
        "evicted": [item.as_json() for item in result.evicted],
        "evicted_count": result.succeeded,
        "package_count": result.plans[0].package_count if result.plans else 0,
        "failed": [item.as_json() for item in result.failed],
        "skipped": [item.as_json() for item in result.skipped],
        "unattributed_bytes": result.plans[0].unattributed_bytes if result.plans else 0,
        "protected_bytes": result.plans[0].protected_bytes if result.plans else 0,
        "duration_seconds": result.duration,
    }
