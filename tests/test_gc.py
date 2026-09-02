import time

from uv_cache_warden import gc, report

from conftest import FakeUv, write_file

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
    result = gc.run(layout, uv, budget=1, dry_run=True)

    assert uv.mutating_calls == []
    assert measured(layout) == before
    assert result.size_after == result.size_before
    assert len(result.evicted) == 3
    assert "dry run" in gc.render(result)


def test_dry_run_and_real_run_agree_on_the_eviction_list(cache):
    layout = build_three(cache)
    budget = measured(layout) - 350_000
    dry = gc.run(layout, FakeUv(cache_dir=layout.root, size=lambda: measured(layout)), budget=budget, dry_run=True)
    wet = gc.run(layout, FakeUv(cache_dir=layout.root, size=lambda: measured(layout)), budget=budget)

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
