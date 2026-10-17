import pytest

from uv_cache_warden import doctor
from uv_cache_warden.uvcmd import UvNotFound, UvTimeout

from conftest import FakeUv


@pytest.mark.parametrize("stage", ["version", "resolve_cache_dir", "cache_size"])
@pytest.mark.parametrize("error", [UvNotFound("uv disappeared"), UvTimeout(["uv"], 1)])
def test_subprocess_errors_remain_structured_diagnostics(cache, monkeypatch, stage, error):
    cache.add_package("rich")
    uv = FakeUv(cache.root, size=100)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(uv, stage, fail)
    monkeypatch.setattr(doctor.Uv, "discover", lambda *args, **kwargs: uv)
    override = None if stage == "resolve_cache_dir" else cache.root
    result = doctor.diagnose(cache_dir=override)
    assert result.failed
    assert result.as_json()["healthy"] is False
    assert any(str(error) in check.detail for check in result.checks)
    assert uv.mutating_calls == []
