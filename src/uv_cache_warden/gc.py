"""LRU eviction down to a byte budget, performed entirely through uv subcommands.

The loop is: sort packages oldest-first, drop the ones a rule protects, and hand names to
`uv cache clean` until the projected total falls under the budget. Then a single
`uv cache prune` sweeps whatever is left unreferenced, and `uv cache size` says what really
happened.

Projection and reality can disagree, because a package's bytes may be hardlinked to bytes that
survive. So when the measured size is still over budget and eligible packages remain, the whole
analysis runs again. `MAX_ROUNDS` bounds that; a round which evicts nothing ends it.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from . import report as report_mod
from .attribute import is_package_name
from .discovery import CacheLayout
from .report import Scan
from .sizing import format_age, format_size, measure_tree
from .uvcmd import Uv, UvError

MAX_ROUNDS = 3

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
    atime_reliable: bool = True
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


def plan(
    scan: Scan,
    budget: int,
    min_age: float = 0.0,
    keep: Sequence[str] = (),
    size_before: Optional[int] = None,
) -> Plan:
    """Decide which packages to evict, oldest first, stopping as soon as the budget is met."""
    keep_set = {name.lower() for name in keep}
    current = size_before if size_before is not None else scan.total.disk_bytes
    result = Plan(
        budget=budget,
        size_before=current,
        projected=current,
        unattributed_bytes=scan.unattributed.disk_bytes,
        protected_bytes=scan.protected.disk_bytes,
    )
    if current <= budget:
        return result

    projected = current
    for package in scan.by_last_used():
        age = scan.age_of(package)
        if package.name.lower() in keep_set:
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
) -> Result:
    """Scan, evict, prune, re-measure. `dry_run` invokes no mutating uv command at all."""
    started = time.time()
    scan = report_mod.scan(layout, uv)
    size_before = scan.uv_cache_size if scan.uv_cache_size is not None else scan.total.disk_bytes

    result = Result(
        cache_dir=str(layout.root),
        budget=budget,
        size_before=size_before,
        size_after=size_before,
        dry_run=dry_run,
        atime_reliable=scan.atime_reliable,
    )

    initially_over = size_before > budget
    current_scan: Optional[Scan] = scan
    current_size = size_before
    evicted_names: Dict[str, None] = {}

    for round_index in range(MAX_ROUNDS):
        if current_scan is None:
            current_scan = report_mod.scan(layout, uv)
            current_size = (
                current_scan.uv_cache_size
                if current_scan.uv_cache_size is not None
                else current_scan.total.disk_bytes
            )
        step = plan(
            current_scan,
            budget,
            min_age=min_age,
            keep=keep,
            size_before=current_size,
        )
        step.evict = [item for item in step.evict if item.name not in evicted_names]
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

    # Prune even when nothing was evictable: unattributed archive bytes are reclaimable only
    # here, and that is exactly the case astral-sh/uv#16551 describes.
    if not dry_run and prune and initially_over:
        outcome = uv.cache_prune()
        result.pruned = outcome.ok
        if not outcome.ok:
            result.prune_detail = _first_error(outcome.stderr) or "exit {}".format(
                outcome.returncode
            )
        result.size_after = _measure(uv, layout)

    result.duration = time.time() - started
    return result


def _measure(uv: Uv, layout: CacheLayout) -> int:
    try:
        size = uv.cache_size()
    except UvError:
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


def render(result: Result) -> str:
    lines: List[str] = []
    lines.append(
        "cache {}  budget {}  currently {}".format(
            result.cache_dir, format_size(result.budget), format_size(result.size_before)
        )
    )
    first = result.plans[0] if result.plans else None
    if first is not None and not first.over_budget:
        lines.append(
            "Already under budget ({} <= {}). Nothing to do.".format(
                format_size(result.size_before), format_size(result.budget)
            )
        )
        return "\n".join(lines)

    if not result.atime_reliable:
        lines.append(
            "note: access times are not tracked on this filesystem, so eviction order is by "
            "write time rather than true last use."
        )

    if not result.evicted:
        lines.append("No package is eligible for eviction.")
        if result.pruned and result.reclaimed:
            lines.append(
                "uv cache prune reclaimed {}; cache now {}.".format(
                    format_size(result.reclaimed), format_size(result.size_after)
                )
            )
        if result.over_budget:
            lines.extend(_explain_shortfall(result))
        return "\n".join(lines)

    lines.append("")
    header = "would evict" if result.dry_run else "evicted"
    lines.append("{} ({}):".format(header, len(result.evicted)))
    width = max(len(item.name) for item in result.evicted)
    ages = {c.name: c.age_seconds for plan_ in result.plans for c in plan_.evict}
    for item in result.evicted:
        status = "" if item.ok else "  FAILED: {}".format(item.detail)
        lines.append(
            "  {}  {:>10}  {}{}".format(
                item.name.ljust(width),
                format_size(item.disk_bytes),
                format_age(ages.get(item.name)),
                status,
            )
        )

    lines.append("")
    if result.dry_run:
        projected = result.plans[0].projected
        lines.append(
            "dry run: nothing was removed. Projected {} -> {} (budget {}).".format(
                format_size(result.size_before), format_size(projected), format_size(result.budget)
            )
        )
        return "\n".join(lines)

    if result.prune_detail:
        lines.append("uv cache prune failed: {}".format(result.prune_detail))
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
            "{} package(s) could not be cleaned: {}".format(
                len(result.failed), ", ".join(item.name for item in result.failed)
            )
        )
    if result.over_budget:
        lines.extend(_explain_shortfall(result))
    return "\n".join(lines)


def _explain_shortfall(result: Result) -> List[str]:
    """Say why the budget was not reached, in terms of the rules that held bytes back."""
    lines = ["still over budget by {}.".format(format_size(result.size_after - result.budget))]
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
        "rounds": result.rounds,
        "pruned": result.pruned,
        "prune_detail": result.prune_detail,
        "evicted": [item.as_json() for item in result.evicted],
        "evicted_count": result.succeeded,
        "failed": [item.as_json() for item in result.failed],
        "skipped": [item.as_json() for item in result.skipped],
        "unattributed_bytes": result.plans[0].unattributed_bytes if result.plans else 0,
        "protected_bytes": result.plans[0].protected_bytes if result.plans else 0,
        "duration_seconds": result.duration,
    }
