import subprocess
import sys

import pytest

from uv_cache_warden import uvcmd


@pytest.mark.parametrize("interrupted", [False, True])
def test_cancelled_subprocess_is_reaped(monkeypatch, interrupted):
    real_popen = subprocess.Popen
    children = []

    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        if interrupted:
            real_wait = child.wait

            def wait(timeout=None):
                child.wait = real_wait
                raise KeyboardInterrupt

            child.wait = wait
        return child

    monkeypatch.setattr(uvcmd.subprocess, "Popen", start)
    uv = uvcmd.Uv(sys.executable, timeout=0.1, echo=False)
    try:
        with pytest.raises(KeyboardInterrupt if interrupted else uvcmd.UvTimeout):
            uv.run("-c", "import time; time.sleep(30)")
        assert len(children) == 1
        assert children[0].poll() is not None
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), 1e100])
def test_invalid_timeouts_are_rejected_before_starting_uv(timeout):
    with pytest.raises(uvcmd.UvError, match="Timeout"):
        uvcmd.Uv(sys.executable, timeout=timeout)


def test_uv_failures_and_machine_size_parsing(monkeypatch):
    uv = uvcmd.Uv(sys.executable, echo=False)
    with pytest.raises(uvcmd.UvFailed, match="exited 7"):
        uv.run("-c", "import sys; print('error: example', file=sys.stderr); sys.exit(7)")
    result = uv.run("-c", "import sys; sys.exit(7)", check=False)
    assert not result.ok and result.returncode == 7
    monkeypatch.setattr(uv, "run", lambda *args, **kwargs: uvcmd.UvResult([], 0, "notice\n12345\n", ""))
    assert uv.cache_size() == 12345
