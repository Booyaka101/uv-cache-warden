import time
import shutil

import pytest

from uv_cache_warden import gc, report
from uv_cache_warden.uvcmd import UvFailed, UvNotFound, UvResult, UvTimeout

from conftest import FakeUv, snapshot_tree, write_file

HOUR = 3600.0
DAY = 86400.0


def build_three(cache):
    now = time.time()
    cache.add_package("cold", size=400_000, mtime=now - 30 * DAY)
    cache.add_package("warm", size=300_000, mtime=now - 3 * DAY)
    cache.add_package("hot", size=200_000, mtime=now - HOUR)
    return cache.layout()


def measured(layout):
    from uv_cache_warden.sizing import measure_tree

    return measure_tree(layout.root).disk_bytes


def test_already_under_budget_does_nothing(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=10**9)

    assert result.evicted == []
    assert uv.mutating_calls == []
    assert "Nothing to do" in gc.render(result)


def test_evicts_oldest_first_and_stops_at_the_budget(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    before = measured(layout)
    result = gc.run(layout, uv, budget=before - 350_000)

    assert [item.name for item in result.evicted] == ["cold"]
    assert result.size_after < before


def test_keep_excludes_a_package(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1, keep=["cold"])

    names = [item.name for item in result.evicted]
    assert "cold" not in names
    assert {"warm", "hot"} <= set(names)
    assert any(item.reason == gc.SKIP_KEEP for item in result.skipped)


def test_keep_is_case_insensitive(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1, keep=["COLD"])

    assert "cold" not in [item.name for item in result.evicted]


@pytest.mark.parametrize("keep", ["zope.interface", "ZOPE_INTERFACE", "zope---interface"])
def test_keep_accepts_normalized_package_aliases(cache, keep):
    cache.add_package("zope-interface", size=100_000)
    uv = FakeUv(cache.root, size=100_000)
    result = gc.run(cache.layout(), uv, budget=1, keep=[keep], prune=False)
    assert uv.mutating_calls == []
    assert [item.reason for item in result.skipped] == [gc.SKIP_KEEP]


def test_recent_alias_protects_all_package_versions(cache):
    cache.add_package("zope.interface", mtime=time.time() - 86400 * 30)
    cache.add_package("ZOPE_INTERFACE", mtime=time.time())
    uv = FakeUv(cache.root, size=100_000)
    result = gc.run(cache.layout(), uv, budget=1, min_age=86400, prune=False)
    assert uv.mutating_calls == []
    assert [item.reason for item in result.skipped] == [gc.SKIP_MIN_AGE]


@pytest.mark.parametrize("budget", [0, 10000])
def test_post_mutation_refresh_accepts_no_recognized_buckets(cache, monkeypatch, budget):
    layout = cache.layout()
    size = [20000]

    def prune():
        size[0] = 500

    refreshed = []
    original_load = gc.load_layout

    def load(root, **kwargs):
        assert kwargs["allow_empty"] is True
        refreshed.append(root)
        result = original_load(root, **kwargs)
        result.buckets.clear()
        result.bucket_names.clear()
        return result

    monkeypatch.setattr(gc, "load_layout", load)
    uv = FakeUv(cache.root, size=lambda: size[0], prune_effect=prune)
    result = gc.run(layout, uv, budget=budget)
    assert refreshed == [cache.root]
    assert result.size_after == 500 and result.over_budget == (budget == 0)
    assert result.plans[0].package_count == 0
    assert uv.mutating_calls == [["cache", "prune"]]


def test_min_age_excludes_recent_packages(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1, min_age=7 * DAY)

    assert [item.name for item in result.evicted] == ["cold"]
    reasons = {item.reason for item in result.skipped}
    assert gc.SKIP_MIN_AGE in reasons


def test_dry_run_invokes_no_mutating_command(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    before = measured(layout)
    snapshot = snapshot_tree(layout.root)
    result = gc.run(layout, uv, budget=1, dry_run=True)

    assert uv.mutating_calls == []
    assert measured(layout) == before
    assert snapshot_tree(layout.root) == snapshot
    assert result.size_after == result.size_before
    assert len(result.evicted) == 3
    assert "dry run" in gc.render(result)


def test_dry_run_and_real_run_agree_when_prune_reclaims_nothing(cache):
    layout = build_three(cache)
    budget = measured(layout) - 350_000

    def uv():
        return FakeUv(cache_dir=layout.root, size=lambda: measured(layout))

    dry = gc.run(layout, uv(), budget=budget, dry_run=True)
    wet = gc.run(layout, uv(), budget=budget)

    assert [item.name for item in dry.evicted] == [item.name for item in wet.evicted]


def test_a_failed_clean_is_reported_and_does_not_stop_the_run(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout), clean_fails={"cold"})
    result = gc.run(layout, uv, budget=1)

    assert [item.name for item in result.failed] == ["cold"]
    assert {"warm", "hot"} <= {item.name for item in result.evicted if item.ok}
    assert "could not be cleaned" in gc.render(result)


def test_still_over_budget_explains_why(cache):
    layout = build_three(cache)
    write_file(cache.root / "archive-v0" / "orphan" / "blob", 5_000_000)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1000, min_age=365 * DAY)

    text = gc.render(result)
    assert result.over_budget
    assert "still over budget" in text
    assert "unattributed" in text


def test_prune_runs_even_when_nothing_is_evictable(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1000, min_age=365 * DAY)

    assert ["cache", "prune"] in uv.calls
    assert result.evicted == []


def test_invalid_package_names_are_never_passed_to_uv(cache):
    layout = build_three(cache)
    scan = report.scan(layout)
    scan.packages["bad name!"] = report.PackageUsage(name="bad name!", usage=scan.total)
    step = gc.plan(scan, budget=1)

    assert "bad name!" not in [item.name for item in step.evict]
    assert gc.SKIP_INVALID in {item.reason for item in step.skipped}


def test_json_payload_is_complete(cache):
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    payload = gc.to_json(gc.run(layout, uv, budget=1, dry_run=True))

    assert payload["dry_run"] is True
    assert payload["budget_bytes"] == 1
    assert payload["size_before_bytes"] == payload["size_after_bytes"]
    assert len(payload["evicted"]) == 3
    legacy_types = {
        "cache_dir": str, "dry_run": bool, "budget_bytes": int,
        "size_before_bytes": int, "size_after_bytes": int, "reclaimed_bytes": int,
        "over_budget": bool, "atime_reliable": bool, "bound_by": str,
        "free_before_bytes": (int, type(None)), "free_after_bytes": (int, type(None)),
        "rounds": int, "pruned": bool, "prune_detail": str, "evicted": list,
        "evicted_count": int, "package_count": int, "failed": list, "skipped": list,
        "unattributed_bytes": int, "protected_bytes": int, "duration_seconds": float,
    }
    assert legacy_types.keys() <= payload.keys()
    for key, expected in legacy_types.items():
        assert isinstance(payload[key], expected), key
    assert isinstance(payload["prune_steps"], list)
    assert type(payload["eviction_projection_conditional"]) is bool


def test_budget_for_prefers_the_tighter_constraint(cache, monkeypatch):
    layout = build_three(cache)
    class Usage:
        def __init__(self, free):
            self.free = free

    monkeypatch.setattr(gc.shutil, "disk_usage", lambda path: Usage(free=10_000))

    # 5,000 bytes of headroom are missing, so the cache must give back 5,000 of its 100,000.
    budget, bound = gc.budget_for(100_000, layout.root, max_size=None, min_free=15_000)
    assert (budget, bound) == (95_000, gc.BOUND_MIN_FREE)

    budget, bound = gc.budget_for(100_000, layout.root, max_size=90_000, min_free=15_000)
    assert (budget, bound) == (90_000, gc.BOUND_MAX_SIZE)

    budget, bound = gc.budget_for(100_000, layout.root, max_size=99_000, min_free=None)
    assert (budget, bound) == (99_000, gc.BOUND_MAX_SIZE)


def test_budget_for_is_a_noop_when_the_disk_has_room(cache, monkeypatch):
    layout = build_three(cache)

    class Usage:
        free = 10**12

    monkeypatch.setattr(gc.shutil, "disk_usage", lambda path: Usage())
    budget, bound = gc.budget_for(100_000, layout.root, max_size=None, min_free=20 * 10**9)
    assert budget == 100_000 and bound == gc.BOUND_MIN_FREE


def test_budget_for_needs_at_least_one_constraint(cache):
    layout = build_three(cache)
    with pytest.raises(ValueError):
        gc.budget_for(100_000, layout.root)


def test_dry_run_says_when_the_plan_falls_short(cache):
    """A dry run that cannot reach the budget must say so; --fail-over exits 2 on it."""
    layout = build_three(cache)
    uv = FakeUv(cache_dir=layout.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1000, min_age=7 * DAY, dry_run=True)

    text = gc.render(result)
    assert result.over_budget
    assert "still over budget" in text
    assert gc.SKIP_MIN_AGE in text


def test_prune_only_acceptance_values_preserve_packages(cache, monkeypatch):
    layout = build_three(cache)
    original = snapshot_tree(cache.root)
    gib = 2**30
    size = [12 * gib]
    uv = FakeUv(cache.root, size=lambda: size[0], prune_effect=lambda: size.__setitem__(0, 9 * gib))
    real_scan = report.scan

    def scripted_scan(*args, **kwargs):
        scan = real_scan(*args, **kwargs)
        for name in ("cold", "warm", "hot"):
            scan.packages[name].usage.disk_bytes = 3 * gib
        return scan

    monkeypatch.setattr(report, "scan", scripted_scan)
    result = gc.run(layout, uv, budget=10 * gib)
    payload = gc.to_json(result)
    assert payload["size_before_bytes"] == 12884901888
    assert payload["size_after_bytes"] == 9663676416
    assert payload["reclaimed_bytes"] == 3221225472
    assert payload["evicted"] == [] and payload["evicted_count"] == 0
    assert payload["over_budget"] is False
    assert payload["package_count"] == 3
    assert payload["prune_steps"] == [{
        "stage": "initial", "ok": True, "detail": "", "size_after_bytes": 9663676416,
    }]
    assert uv.mutating_calls == [["cache", "prune"]]
    assert snapshot_tree(cache.root) == original
    assert "Budget met by initial prune; no packages cleaned." in gc.render(result)
    assert "Already under budget" not in gc.render(result)


@pytest.mark.parametrize("reclaim", [0, 100_000])
def test_prune_then_clean_then_final_prune(cache, reclaim):
    layout = build_three(cache)
    remaining = [0]

    def prune():
        remaining[0] = reclaim

    uv = FakeUv(cache.root, size=lambda: measured(layout) - remaining[0], prune_effect=prune)
    before = measured(layout)
    result = gc.run(layout, uv, budget=before - 350_000)
    assert uv.mutating_calls == [
        ["cache", "prune"], ["cache", "clean", "cold"], ["cache", "prune"],
    ]
    assert [step["stage"] for step in result.prune_steps] == ["initial", "final"]
    assert result.size_before == before
    assert result.reclaimed == before - result.size_after


def test_initial_prune_partial_failure_reloads_buckets_before_selecting(cache):
    layout = build_three(cache)
    stale = cache.root / "wheels-v99"
    stale.mkdir()
    layout = cache.layout()

    def prune():
        if stale.exists():
            shutil.rmtree(stale)
        return UvResult([], 1, "", "error: partially pruned")

    uv = FakeUv(cache.root, size=lambda: measured(layout), prune_effect=prune)
    result = gc.run(layout, uv, budget=1, keep=["hot"], min_age=7 * DAY)
    assert [item.name for item in result.evicted] == ["cold"]
    assert result.plans[0].package_count == 3
    assert result.prune_detail == "initial: error: partially pruned; final: error: partially pruned"
    assert result.pruned is False
    initial_index = uv.calls.index(["cache", "prune"])
    clean_index = uv.calls.index(["cache", "clean", "cold"])
    assert ["cache", "size"] in uv.calls[initial_index + 1:clean_index]
    assert ["--version"] in uv.calls[initial_index + 1:clean_index]


@pytest.mark.parametrize("failure_stage", ["initial", "final"])
def test_pruned_means_any_stage_succeeded(cache, failure_stage):
    layout = build_three(cache)
    stages = iter(["initial", "final"])

    def prune():
        if next(stages) == failure_stage:
            return UvResult([], 1, "", "error: prune failed")

    uv = FakeUv(cache.root, size=lambda: measured(layout), prune_effect=prune)
    result = gc.run(layout, uv, budget=1)
    assert result.pruned is True
    assert result.prune_detail == failure_stage + ": error: prune failed"
    assert len(result.prune_steps) == 2


def test_returned_failure_can_still_meet_budget_without_cleaning(cache):
    layout = build_three(cache)
    size = [100]

    def prune():
        size[0] = 50
        return UvResult([], 1, "", "error: partial removal")

    uv = FakeUv(cache.root, size=lambda: size[0], prune_effect=prune)
    result = gc.run(layout, uv, budget=50)
    assert result.size_after == 50 and result.reclaimed == 50
    assert uv.mutating_calls == [["cache", "prune"]]
    assert result.prune_steps[0]["ok"] is False
    assert "initial: error: partial removal" in gc.render(result)


def test_equality_boundary_is_a_noop(cache):
    layout = build_three(cache)
    uv = FakeUv(cache.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=measured(layout))
    assert uv.mutating_calls == []
    assert result.prune_steps == [] and not result.over_budget


@pytest.mark.parametrize("dry_run", [True, False])
def test_no_prune_disables_both_stages_and_conditional_projection(cache, dry_run):
    layout = build_three(cache)
    uv = FakeUv(cache.root, size=lambda: measured(layout))
    result = gc.run(layout, uv, budget=1, prune=False, dry_run=dry_run)
    assert all(call[:2] == ["cache", "clean"] for call in uv.mutating_calls)
    assert result.prune_steps == [] and not result.pruned
    assert not result.eviction_projection_conditional
    assert "A real run prunes first" not in gc.render(result)


def test_dry_projection_warns_and_can_differ_from_real_run(cache):
    layout = build_three(cache)
    size = [100]
    uv = FakeUv(cache.root, size=lambda: size[0], prune_effect=lambda: size.__setitem__(0, 50))
    dry = gc.run(layout, uv, budget=50, dry_run=True)
    assert dry.evicted and dry.eviction_projection_conditional
    assert dry.size_after == 100 and dry.prune_steps == []
    assert uv.mutating_calls == []
    assert "projected package evictions may disappear" in gc.render(dry)
    wet = gc.run(layout, uv, budget=50)
    assert wet.evicted == []


def test_failed_clean_is_not_retried_and_does_not_trigger_final_prune(cache):
    cache.add_package("cold", size=100_000)
    layout = cache.layout()
    uv = FakeUv(cache.root, size=lambda: measured(layout), clean_fails={"cold"})
    result = gc.run(layout, uv, budget=1)
    assert uv.mutating_calls == [["cache", "prune"], ["cache", "clean", "cold"]]
    assert result.failed[0].name == "cold"


@pytest.mark.parametrize("error", [UvTimeout(["uv"], 1), UvNotFound("uv disappeared")])
@pytest.mark.parametrize("point", ["scan_size", "scan_version", "initial_prune", "after_prune", "refresh", "clean", "after_clean", "final_prune", "after_final_prune"])
def test_fatal_uv_errors_stop_all_subsequent_mutations(cache, error, point):
    layout = build_three(cache)
    uv = FakeUv(cache.root, size=lambda: measured(layout))
    original_size = uv.cache_size

    def fail(*args, **kwargs):
        raise error

    if point == "final_prune":
        def prune():
            uv._record("cache", "prune")
            if sum(call == ["cache", "prune"] for call in uv.calls) == 2:
                raise error
            return UvResult([], 0, "", "")
        uv.cache_prune = prune
    elif point in ("scan_size", "scan_version", "initial_prune", "clean"):
        method = {"scan_size": "cache_size", "scan_version": "version", "initial_prune": "cache_prune", "clean": "cache_clean"}[point]
        setattr(uv, method, fail)
    else:
        def size():
            prunes = sum(call == ["cache", "prune"] for call in uv.mutating_calls)
            cleans = any(call[:2] == ["cache", "clean"] for call in uv.mutating_calls)
            if ((point == "after_prune" and prunes) or
                    (point == "after_final_prune" and prunes == 2) or
                    (point == "after_clean" and cleans) or
                    (point == "refresh" and len([c for c in uv.calls if c == ["cache", "size"]]) == 2)):
                raise error
            return original_size()
        uv.cache_size = size
    with pytest.raises(type(error)):
        gc.run(layout, uv, budget=1)
    prunes = sum(call == ["cache", "prune"] for call in uv.mutating_calls)
    if point in ("final_prune", "after_final_prune"):
        assert prunes == 2
    else:
        assert prunes <= 1
    if point not in ("clean", "after_clean", "final_prune", "after_final_prune"):
        assert not any(call[:2] == ["cache", "clean"] for call in uv.mutating_calls)


@pytest.mark.parametrize("unsupported", [None, UvFailed(["uv", "cache", "size"], 2, "unsupported")])
def test_missing_machine_size_support_uses_walk(cache, unsupported):
    layout = build_three(cache)

    def size():
        if unsupported is not None:
            raise unsupported
        return None

    uv = FakeUv(cache.root, size=size)
    before = measured(layout)
    result = gc.run(layout, uv, budget=before - 350_000)
    assert result.size_before == before and result.size_after == measured(layout)
    assert result.succeeded == 1 and len(result.prune_steps) == 2


def test_unknown_timestamp_is_protected_by_min_age(cache):
    layout = build_three(cache)
    scan = report.scan(layout)
    scan.packages["cold"].usage.last_used = None
    step = gc.plan(scan, budget=1, min_age=DAY)
    assert "cold" not in [c.name for c in step.evict]
    assert any(s.name == "cold" and s.reason == gc.SKIP_MIN_AGE for s in step.skipped)


def test_nonreclaiming_cleans_are_bounded_by_max_rounds(cache, monkeypatch):
    layout = build_three(cache)
    scan = report.scan(layout)
    size = scan.total.disk_bytes
    for package in scan.packages.values():
        package.usage.disk_bytes = size // 2
    # Every round projects success after one clean, but the measured total never falls.
    monkeypatch.setattr(report, "scan", lambda *args, **kwargs: scan)
    uv = FakeUv(cache.root, size=size)
    monkeypatch.setattr(uv, "cache_clean", lambda name: (uv._record("cache", "clean", name) or UvResult([], 0, "", "")))
    result = gc.run(layout, uv, budget=size // 2 + 1)
    assert result.rounds == gc.MAX_ROUNDS
    names = [call[2] for call in uv.mutating_calls if call[:2] == ["cache", "clean"]]
    assert len(names) == len(set(names))
    assert names == ["cold", "warm", "hot"]
    assert result.over_budget
