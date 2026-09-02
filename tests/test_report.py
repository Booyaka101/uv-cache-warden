import json
import os
import time

from uv_cache_warden import report

from conftest import write_file

HOUR = 3600.0


def test_packages_carry_their_archive_bytes(cache):
    cache.add_package("rich", size=50_000)
    cache.add_package("torch", size=500_000)
    result = report.scan(cache.layout())

    assert set(result.packages) == {"rich", "torch"}
    assert result.packages["torch"].usage.disk_bytes > result.packages["rich"].usage.disk_bytes
    assert result.packages["torch"].archives == 1


def test_unattributable_bytes_land_in_their_own_bucket(cache):
    cache.add_package("rich", size=10_000)
    write_file(cache.root / "archive-v0" / "orphan" / "data.bin", 200_000)
    write_file(cache.root / "builds-v0" / "scratch" / "obj", 30_000)
    result = report.scan(cache.layout())

    assert "orphan" not in result.packages
    assert result.unreferenced_archives == 1
    assert result.unattributed.disk_bytes >= 230_000


def test_protected_buckets_are_never_a_package(cache):
    write_file(cache.root / "python-v0" / "cpython-3.12" / "python", 900_000)
    write_file(cache.root / "osv-v0" / "db.json", 5_000)
    result = report.scan(cache.layout())

    assert result.packages == {}
    assert result.protected.disk_bytes >= 905_000
    # The only unattributed file is CACHEDIR.TAG; the rest is directory entries, cache root
    # included, which is what `uv cache size` counts them as too.
    assert result.unattributed.files == 1


def test_shared_archive_is_unattributed(cache):
    cache.add_package("alpha", size=80_000, archive_id="shared")
    cache.add_package("beta", size=0, archive_id="shared")
    result = report.scan(cache.layout())

    assert result.shared_archives == 1
    assert result.unattributed.disk_bytes >= 80_000


def test_lru_order_is_oldest_first(cache):
    now = time.time()
    cache.add_package("newest", mtime=now - HOUR)
    cache.add_package("middle", mtime=now - 10 * HOUR)
    cache.add_package("oldest", mtime=now - 100 * HOUR)
    result = report.scan(cache.layout())

    assert [p.name for p in result.by_last_used()] == ["oldest", "middle", "newest"]


def test_json_is_serialisable_and_totals_add_up(cache):
    cache.add_package("rich", size=20_000)
    cache.add_package("torch", size=40_000)
    result = report.scan(cache.layout())
    payload = json.loads(json.dumps(report.to_json(result)))

    attributed = sum(item["disk_bytes"] for item in payload["packages"])
    assert payload["package_count"] == 2
    assert (
        attributed + payload["unattributed"]["disk_bytes"] + payload["protected"]["disk_bytes"]
        == payload["total"]["disk_bytes"]
    )


def test_render_shows_top_n_and_a_remainder_row(cache):
    for index in range(5):
        cache.add_package("pkg{}".format(index), size=(index + 1) * 10_000)
    result = report.scan(cache.layout())
    text = report.render(result, top=2)

    assert "pkg4" in text
    assert "pkg0" not in text
    assert "... 3 more" in text
    assert "TOTAL" in text


def test_render_notes_unreferenced_archives(cache):
    cache.add_package("rich")
    write_file(cache.root / "archive-v0" / "orphan" / "data.bin", 300_000)
    text = report.render(report.scan(cache.layout()))

    assert "uv cache prune" in text


def test_sort_orders(cache):
    now = time.time()
    cache.add_package("big", size=90_000, mtime=now - HOUR)
    cache.add_package("cold", size=10_000, mtime=now - 500 * HOUR)
    result = report.scan(cache.layout())

    assert [p.name for p in result.sorted_packages(report.SORT_SIZE)] == ["big", "cold"]
    assert [p.name for p in result.sorted_packages(report.SORT_AGE)] == ["cold", "big"]
    assert [p.name for p in result.sorted_packages(report.SORT_NAME)] == ["big", "cold"]
    assert report.render(result, top=1, sort=report.SORT_AGE).splitlines()[1].startswith("cold")


def test_simple_index_metadata_does_not_reset_the_clock(cache):
    """`uv lock` rewrites simple-index entries wholesale; that must not make a cold package hot."""
    now = time.time()
    cache.add_package("cold", size=50_000, mtime=now - 200 * HOUR)
    fresh = cache.root / "simple-v24" / "pypi" / "cold.rkyv"
    os.utime(str(fresh), (now, now))
    result = report.scan(cache.layout())

    assert result.age_of(result.packages["cold"]) > 100 * HOUR
    assert result.packages["cold"].usage.disk_bytes >= 50_000


def test_scan_total_matches_a_plain_measurement(cache):
    """The classified pass and the flat pass must agree, or reconciliation drifts silently.

    They diverged once: `scan` walked only the entries *under* the cache root and never counted
    the root directory itself, which `uv cache size` does. On NTFS that is zero until the
    directory outgrows its resident index, so it stayed invisible on small caches.
    """
    from uv_cache_warden.sizing import measure_tree

    cache.add_package("rich", size=30_000)
    cache.add_package("torch", size=90_000)
    result = report.scan(cache.layout())

    assert result.total.disk_bytes == measure_tree(cache.root).disk_bytes
    assert result.total.dirs == measure_tree(cache.root).dirs


def test_the_cache_root_itself_is_counted(cache):
    cache.add_package("rich")
    result = report.scan(cache.layout())
    walked = list(report.walk(cache.root))

    assert walked[0][1] == ()
    assert result.buckets[report.ROOT_BUCKET].dirs >= 1
