import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from uv_cache_warden import __version__, cli


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("UVCW_CACHE_DIR", "UVCW_MAX_SIZE", "UVCW_MIN_AGE", "UVCW_KEEP", "UVCW_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)


def test_no_command_prints_help(capsys):
    assert cli.main([]) == cli.EXIT_ERROR
    assert "usage: uvcw" in capsys.readouterr().out


def test_version_flag():
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--version"])
    assert excinfo.value.code == 0


def test_missing_uv_is_a_clear_message(cache, capsys, monkeypatch):
    cache.add_package("rich")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    code = cli.main(["report", "--cache-dir", str(cache.root)])
    assert code == cli.EXIT_ERROR
    assert "was not found on PATH" in capsys.readouterr().err


def test_missing_cache_dir_is_a_clear_message(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "uv")
    code = cli.main(["report", "--cache-dir", str(tmp_path / "absent")])
    assert code == cli.EXIT_ERROR
    assert "does not exist" in capsys.readouterr().err


def test_gc_requires_a_budget(capsys):
    assert cli.main(["gc"]) == cli.EXIT_ERROR
    assert "--max-size is required" in capsys.readouterr().err


def test_gc_rejects_a_bad_size(capsys, cache):
    code = cli.main(["gc", "--max-size", "10 parsecs", "--cache-dir", str(cache.root)])
    assert code == cli.EXIT_ERROR
    assert "unknown size unit" in capsys.readouterr().err


def test_gc_rejects_a_bad_duration(capsys, cache):
    code = cli.main(
        ["gc", "--max-size", "1GB", "--min-age", "soon", "--cache-dir", str(cache.root)]
    )
    assert code == cli.EXIT_ERROR
    assert "not a duration" in capsys.readouterr().err


def test_bad_timeout_env_is_reported(capsys, cache, monkeypatch):
    monkeypatch.setenv("UVCW_TIMEOUT", "soonish")
    monkeypatch.setattr(shutil, "which", lambda name: "uv")
    cache.add_package("rich")
    code = cli.main(["report", "--cache-dir", str(cache.root)])
    assert code == cli.EXIT_ERROR
    assert "UVCW_TIMEOUT" in capsys.readouterr().err


def _fake_uv_factory(cache_root, monkeypatch):
    from conftest import FakeUv
    from uv_cache_warden.sizing import measure_tree

    def discover(executable=None, **kwargs):
        return FakeUv(cache_dir=cache_root, size=lambda: measure_tree(cache_root).disk_bytes)

    monkeypatch.setattr(cli.Uv, "discover", staticmethod(discover))


def test_report_json_round_trips(cache, capsys, monkeypatch):
    cache.add_package("rich", size=20_000)
    _fake_uv_factory(cache.root, monkeypatch)
    assert cli.main(["report", "--json", "--cache-dir", str(cache.root)]) == cli.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["packages"][0]["name"] == "rich"
    assert payload["cache_dir"] == str(cache.root)


def test_gc_dry_run_exits_zero_and_names_a_package(cache, capsys, monkeypatch):
    cache.add_package("rich", size=200_000)
    _fake_uv_factory(cache.root, monkeypatch)
    code = cli.main(["gc", "--max-size", "1000", "--dry-run", "--cache-dir", str(cache.root)])
    assert code == cli.EXIT_OK
    assert "rich" in capsys.readouterr().out


def test_fail_over_exits_two_when_the_budget_cannot_be_met(cache, capsys, monkeypatch):
    cache.add_package("rich", size=200_000)
    _fake_uv_factory(cache.root, monkeypatch)
    code = cli.main(
        [
            "gc",
            "--max-size",
            "1000",
            "--min-age",
            "52w",
            "--fail-over",
            "--cache-dir",
            str(cache.root),
        ]
    )
    assert code == cli.EXIT_OVER_BUDGET
    assert "still over budget" in capsys.readouterr().out


def test_env_vars_supply_gc_defaults(cache, capsys, monkeypatch):
    cache.add_package("rich", size=200_000)
    cache.add_package("torch", size=200_000)
    _fake_uv_factory(cache.root, monkeypatch)
    monkeypatch.setenv("UVCW_MAX_SIZE", "1000")
    monkeypatch.setenv("UVCW_KEEP", "rich, torch")
    code = cli.main(["gc", "--dry-run", "--cache-dir", str(cache.root)])
    assert code == cli.EXIT_OK
    assert "No package is eligible" in capsys.readouterr().out


def test_module_entry_point_runs(tmp_path):
    """`cli.py` has to work as a script, since that is what the console script calls into."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.run(
        [sys.executable, "-m", "uv_cache_warden.cli", "--version"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert proc.returncode == 0
    assert __version__ in proc.stdout


def test_json_file_is_written_alongside_the_text_output(cache, capsys, tmp_path, monkeypatch):
    cache.add_package("rich", size=20_000)
    _fake_uv_factory(cache.root, monkeypatch)
    target = tmp_path / "out.json"
    code = cli.main(
        ["report", "--json-file", str(target), "--cache-dir", str(cache.root)]
    )
    assert code == cli.EXIT_OK
    assert "PACKAGE" in capsys.readouterr().out
    assert json.loads(target.read_text())["packages"][0]["name"] == "rich"


def test_gc_json_file_records_the_run(cache, tmp_path, monkeypatch):
    cache.add_package("rich", size=200_000)
    _fake_uv_factory(cache.root, monkeypatch)
    target = tmp_path / "gc.json"
    assert (
        cli.main(
            [
                "gc",
                "--max-size",
                "1000",
                "--json-file",
                str(target),
                "--cache-dir",
                str(cache.root),
            ]
        )
        == cli.EXIT_OK
    )
    payload = json.loads(target.read_text())
    assert payload["evicted_count"] == 1
    assert payload["size_after_bytes"] < payload["size_before_bytes"]


def test_doctor_json_file(tmp_path, cache, monkeypatch):
    cache.add_package("rich")
    _fake_uv_factory(cache.root, monkeypatch)
    target = tmp_path / "doctor.json"
    cli.main(["doctor", "--json-file", str(target), "--cache-dir", str(cache.root)])
    assert "checks" in json.loads(target.read_text())
