"""Compatibility checks against a real uv and a real index.

Skipped unless `uv` is on PATH. Run it with `pytest -m integration`.
"""

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from uv_cache_warden import discovery, gc, report
from uv_cache_warden.uvcmd import Uv

from conftest import snapshot_tree

pytestmark = pytest.mark.integration

PACKAGES = ["idna==3.7", "mdurl==0.1.2"]


@pytest.fixture
def real_cache(tmp_path):
    if shutil.which("uv") is None:
        if os.environ.get("CI"):
            pytest.fail("CI integration checks require uv on PATH")
        pytest.skip("uv is not on PATH")
    cache_dir = tmp_path / "uv-cache"
    project = tmp_path / "project"
    project.mkdir()
    env = {"UV_CACHE_DIR": str(cache_dir)}
    _run(["uv", "venv", "--python", sys.executable, "--no-python-downloads"], project, env)
    _run(["uv", "pip", "install"] + PACKAGES, project, env)
    uv = Uv(executable=shutil.which("uv"), cache_dir=cache_dir, echo=False)
    return discovery.load_layout(cache_dir, source="test"), uv


def _run(argv, cwd, env, expected=0):
    merged = dict(os.environ)
    # An activated venv would make `uv pip install` target it instead of the fresh one.
    merged.pop("VIRTUAL_ENV", None)
    merged.update(env)
    proc = subprocess.run(argv, cwd=str(cwd), env=merged, capture_output=True, text=True, timeout=120)
    assert proc.returncode == expected, "{} exited {}: {}".format(" ".join(argv), proc.returncode, proc.stderr.strip())
    return proc


def test_report_reconciles_with_uv_cache_size(real_cache):
    layout, uv = real_cache
    result = report.scan(layout, uv)

    assert result.uv_cache_size is not None, "integration target requires machine-size support"
    assert result.uv_cache_size > 0
    assert result.reconciliation_delta <= 0.02
    assert "idna" in result.packages
    assert "mdurl" in result.packages


def test_gc_brings_a_real_cache_under_budget(real_cache):
    layout, uv = real_cache
    before = uv.cache_size()
    assert before is not None, "integration target requires machine-size support"

    snapshot = snapshot_tree(layout.root)
    dry = gc.run(layout, uv, budget=before // 2, dry_run=True)
    assert dry.evicted
    assert uv.cache_size() == before
    assert snapshot_tree(layout.root) == snapshot

    wet = gc.run(layout, uv, budget=before // 2)
    assert wet.size_after < before
    assert wet.size_after <= wet.budget


def test_initial_prune_preserves_packages_and_offline_reinstall(real_cache):
    layout, uv = real_cache
    before_scan = report.scan(layout, uv)
    obsolete = layout.root / "wheels-v0"
    obsolete.mkdir()
    (obsolete / "obsolete-test-bucket").write_bytes(b"x" * 65536)
    before = uv.cache_size()
    budget = before_scan.uv_cache_size
    assert before is not None and budget is not None and before > budget

    result = gc.run(discovery.load_layout(layout.root), uv, budget=budget)
    assert result.evicted == []
    assert not result.over_budget and result.size_before == before
    assert result.prune_steps == [{
        "stage": "initial", "ok": True, "detail": "", "size_after_bytes": result.size_after,
    }]
    assert not obsolete.exists()
    assert set(report.scan(discovery.load_layout(layout.root), uv).packages) == set(before_scan.packages)
    mutations = [call[1:3] for call in uv.calls if call[1:3] in (["cache", "prune"], ["cache", "clean"])]
    assert mutations == [["cache", "prune"]]

    project = layout.root.parent / "project"
    proc = _run([uv.executable, "pip", "install", "--offline", "--reinstall"] + PACKAGES,
                project, {"UV_CACHE_DIR": str(layout.root)})
    assert "idna==3.7" in proc.stderr and "mdurl==0.1.2" in proc.stderr


def test_cli_prune_only_json_on_real_cache(real_cache):
    layout, uv = real_cache
    budget = uv.cache_size()
    obsolete = layout.root / "wheels-v0"
    obsolete.mkdir()
    (obsolete / "obsolete-test-bucket").write_bytes(b"x" * 65536)
    proc = _run([sys.executable, "-m", "uv_cache_warden.cli", "gc", "--uv", uv.executable,
                 "--cache-dir", str(layout.root), "--max-size", str(budget), "--json", "--fail-over"],
                layout.root.parent, {"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")})
    data = json.loads(proc.stdout)
    assert data["evicted"] == [] and data["evicted_count"] == 0
    assert data["reclaimed_bytes"] >= 65536
    assert not data["over_budget"] and len(data["prune_steps"]) == 1


def test_pruning_last_obsolete_bucket_reports_success(real_cache, tmp_path):
    _, uv = real_cache
    root = tmp_path / "obsolete-only"
    obsolete = root / "wheels-v0"
    obsolete.mkdir(parents=True)
    (obsolete / "fixture").write_bytes(b"x" * 65536)
    result = gc.run(discovery.load_layout(root), Uv(uv.executable, cache_dir=root, echo=False), budget=10000)
    assert not result.over_budget and result.evicted == []
    assert result.plans[0].package_count == 0
    assert len(result.prune_steps) == 1 and result.prune_steps[0]["ok"]
    # Refresh permission is not permission to start GC against an unrecognized directory.
    with pytest.raises(discovery.DiscoveryError, match="no uv cache buckets"):
        discovery.load_layout(root)


@pytest.mark.skipif(os.name == "nt", reason="composite Action smoke uses a POSIX bash runner")
def test_composite_action_shell_install_and_modes(real_cache, tmp_path):
    layout, uv = real_cache
    root = Path(__file__).resolve().parents[1]
    source = (root / "action.yml").read_text()
    install, run = source.split("    - name: Run uv-cache-warden", 1)
    install_script = textwrap.dedent(install.split("      run: |\n", 1)[1])
    run_script = textwrap.dedent(run.split("      run: |\n", 1)[1])
    env = {
        "UV_CACHE_DIR": str(layout.root), "UV_TOOL_DIR": str(tmp_path / "tools"),
        "UV_TOOL_BIN_DIR": str(tmp_path / "bin"), "UV_PYTHON_DOWNLOADS": "never",
        "UVCW_INSTALL_VERSION": "local", "GITHUB_ACTION_PATH": str(root),
        "GITHUB_PATH": str(tmp_path / "github-path"), "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"), "UVCW_SUMMARY": "true",
        "UVCW_CACHE_DIR": str(layout.root), "UVCW_KEEP": "", "UVCW_MIN_AGE": "",
        "UVCW_MIN_FREE": "", "UVCW_TIMEOUT": "30", "UVCW_FAIL_OVER": "true",
        "PATH": str(tmp_path / "bin") + os.pathsep + os.environ["PATH"],
        "TMPDIR": str(tmp_path),
    }
    _run(["bash", "-c", install_script], root, env)
    budget = uv.cache_size()
    for mode in ("report", "dry-run", "gc"):
        obsolete = layout.root / "wheels-v0"
        obsolete.mkdir(exist_ok=True)
        (obsolete / "obsolete-test-bucket").write_bytes(b"x" * 65536)
        env.update(UVCW_MODE=mode, UVCW_MAX_SIZE=str(budget), GITHUB_OUTPUT=str(tmp_path / (mode + "-outputs")))
        _run(["bash", "-c", run_script], root, env)
        data = json.loads((tmp_path / "uvcw-result.json").read_text())
        if mode == "report":
            assert {"idna", "mdurl"} <= {p["name"] for p in data["packages"]}
        elif mode == "dry-run":
            assert data["eviction_projection_conditional"] and data["prune_steps"] == []
            assert obsolete.exists()
        else:
            assert data["evicted_count"] == 0 and not data["over_budget"]
            assert len(data["prune_steps"]) == 1 and not obsolete.exists()
    assert "Budget met by initial prune; no packages cleaned." in (tmp_path / "summary").read_text()
    summary = (tmp_path / "summary").read_bytes()
    env.update(UVCW_TIMEOUT="not-a-number", GITHUB_OUTPUT=str(tmp_path / "failed-outputs"))
    proc = _run(["bash", "-c", run_script], root, env, expected=1)
    assert "uvcw produced no result file" in proc.stdout
    assert not (tmp_path / "uvcw-result.json").exists()
    assert not (tmp_path / "failed-outputs").exists()
    assert (tmp_path / "summary").read_bytes() == summary
