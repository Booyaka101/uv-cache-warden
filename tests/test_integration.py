"""The one test that talks to a real uv and a real index.

Skipped unless `uv` is on PATH. Run it with `pytest -m integration`.
"""

import shutil
import subprocess

import pytest

from uv_cache_warden import discovery, gc, report
from uv_cache_warden.uvcmd import Uv

pytestmark = pytest.mark.integration

PACKAGES = ["idna==3.7", "mdurl==0.1.2"]


@pytest.fixture
def real_cache(tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    cache_dir = tmp_path / "uv-cache"
    project = tmp_path / "project"
    project.mkdir()
    env = {"UV_CACHE_DIR": str(cache_dir)}
    _run(["uv", "venv"], project, env)
    _run(["uv", "pip", "install"] + PACKAGES, project, env)
    uv = Uv(executable=shutil.which("uv"), cache_dir=cache_dir, echo=False)
    return discovery.load_layout(cache_dir, source="test"), uv


def _run(argv, cwd, env):
    import os

    merged = dict(os.environ)
    merged.update(env)
    proc = subprocess.run(argv, cwd=str(cwd), env=merged, capture_output=True, text=True)
    if proc.returncode != 0:
        pytest.skip("{} failed: {}".format(" ".join(argv), proc.stderr.strip()[:400]))


def test_report_reconciles_with_uv_cache_size(real_cache):
    layout, uv = real_cache
    result = report.scan(layout, uv)

    if result.uv_cache_size is None:
        pytest.skip("this uv has no `cache size --output-format machine`")
    assert result.uv_cache_size > 0
    assert result.reconciliation_delta <= 0.02
    assert "idna" in result.packages
    assert "mdurl" in result.packages


def test_gc_brings_a_real_cache_under_budget(real_cache):
    layout, uv = real_cache
    before = uv.cache_size()
    if before is None:
        pytest.skip("this uv has no `cache size --output-format machine`")

    dry = gc.run(layout, uv, budget=before // 2, dry_run=True)
    assert dry.evicted
    assert uv.cache_size() == before

    wet = gc.run(layout, uv, budget=before // 2)
    assert wet.size_after < before
    assert wet.size_after <= wet.budget
