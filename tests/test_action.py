import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest

from uv_cache_warden import __version__, gc

from conftest import FakeUv

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["prune-only", "dry-run", "report", "clean", "failed-prune"])
def test_action_outputs_and_summary(cache, tmp_path, mode):
    cache.add_package("cold", size=100_000)
    size = [200_000]
    uv = FakeUv(cache.root, size=lambda: size[0], prune_effect=lambda: size.__setitem__(0, 100_000))
    if mode == "report":
        data = {"total": {"disk_bytes": 200_000}}
    else:
        data = gc.to_json(gc.run(cache.layout(), uv, budget=100_000, dry_run=mode == "dry-run"))
        if mode == "clean":
            data["evicted_count"] = 1
        if mode == "failed-prune":
            data["prune_steps"][0]["ok"] = False
            data["over_budget"] = True
    result = tmp_path / "result.json"
    result.write_text(json.dumps(data), encoding="utf-8")
    output, summary = tmp_path / "outputs", tmp_path / "summary"
    source = (ROOT / "action.yml").read_text(encoding="utf-8")
    script = textwrap.dedent(source.split("<<'PY'\n", 1)[1].split("\n        PY", 1)[0])
    env = dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary), UVCW_SUMMARY="true")
    proc = subprocess.run([sys.executable, "-", str(result)], input=script, env=env,
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert set(values) == {"size-before", "size-after", "reclaimed", "evicted", "over-budget", "json-path"}
    assert int(values["size-after"]) == data.get("size_after_bytes", 200_000)
    assert int(values["evicted"]) == data.get("evicted_count", 0)
    text = summary.read_text()
    if mode == "prune-only":
        assert "Budget met by initial prune; no packages cleaned." in text
        assert "| initial prune | ok;" in text
    elif mode == "dry-run":
        assert "package evictions are conditional" in text
        assert "Cache after is measured and unchanged" in text
    elif mode == "failed-prune":
        assert "| initial prune | failed;" in text
        assert "Budget met by initial prune" not in text
    else:
        assert "Budget met by initial prune" not in text


def test_release_versions_and_workflow_guards_match():
    action = (ROOT / "action.yml").read_text()
    manifest = (ROOT / "pyproject.toml").read_text()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert 'default: "{}"'.format(__version__) in action
    assert 'version = "{}"'.format(__version__) in manifest
    assert ci.count('version: "0.13.1"') == 2
    assert "python -m pytest -q -m integration" in ci
    assert "/tmp/clean/bin/uv-cache-warden --version" in ci
    assert "uses: ./" in ci and "version: local" in ci


@pytest.mark.skipif(os.name == "nt", reason="Action shell regressions require POSIX bash")
@pytest.mark.parametrize("scenario", ["no-result", "invalid-json", "output-error", "no-prune", "fail-over"])
def test_action_shell_fails_closed_and_passes_options(tmp_path, scenario):
    source = (ROOT / "action.yml").read_text()
    script = textwrap.dedent(source.split("    - name: Run uv-cache-warden", 1)[1].split("      run: |\n", 1)[1])
    stub = tmp_path / "uvcw"
    stub.write_text("#!/usr/bin/env python3\n" + textwrap.dedent("""\
        import json, os, sys
        from pathlib import Path
        mode = os.environ['SCENARIO']
        target = Path(sys.argv[sys.argv.index('--json-file') + 1])
        if mode == 'no-result':
            sys.exit(0)
        if mode == 'invalid-json':
            target.write_text('{')
            sys.exit(0)
        if mode == 'no-prune' and '--no-prune' not in sys.argv:
            sys.exit(9)
        target.write_text(json.dumps({
            'size_before_bytes': 200, 'size_after_bytes': 100,
            'reclaimed_bytes': 100, 'evicted_count': 1,
            'over_budget': mode == 'fail-over',
        }))
        sys.exit(2 if mode == 'fail-over' else 0)
        """))
    stub.chmod(0o755)
    output = tmp_path / "outputs"
    if scenario == "output-error":
        output.mkdir()
    env = dict(os.environ, SCENARIO=scenario, PATH=str(tmp_path) + os.pathsep + os.environ["PATH"],
               RUNNER_TEMP=str(tmp_path), UVCW_CACHE_DIR="", UVCW_MODE="gc", UVCW_MAX_SIZE="100",
               UVCW_MIN_FREE="", UVCW_FAIL_OVER="true", UVCW_NO_PRUNE="true", UVCW_SUMMARY="false",
               GITHUB_OUTPUT=str(output))
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=30)
    expected = 0 if scenario == "no-prune" else 2 if scenario == "fail-over" else 1
    assert proc.returncode == expected, proc.stdout + proc.stderr
