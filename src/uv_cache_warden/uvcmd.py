"""Thin subprocess wrapper around the `uv` executable.

Every mutation uv-cache-warden performs goes through here, because uv's docs say it is never
safe to modify the cache directly. `--force` is deliberately never sent: `uv cache clean` and
`uv cache prune` block until no other process is reading the cache, and waiting is the correct
behaviour.
"""

import math
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

DEFAULT_TIMEOUT = 900.0
"""Generous by design. A `uv cache clean` may block on the cache lock behind a long `uv sync`."""

_UNRECOGNIZED = re.compile(r"unrecognized subcommand|unexpected argument|invalid value", re.I)


class UvError(Exception):
    """Base class for every failure originating in the `uv` subprocess."""


class UvNotFound(UvError):
    def __init__(self, hint: str) -> None:
        super().__init__(hint)


class UvTimeout(UvError):
    def __init__(self, args: Sequence[str], timeout: float) -> None:
        self.args_run = list(args)
        self.timeout = timeout
        super().__init__(
            "`{}` did not finish within {:g}s. Another uv process may be holding the cache "
            "lock; raise the limit with --timeout or UVCW_TIMEOUT.".format(
                " ".join(args), timeout
            )
        )


class UvFailed(UvError):
    def __init__(self, args: Sequence[str], returncode: int, stderr: str) -> None:
        self.args_run = list(args)
        self.returncode = returncode
        self.stderr = stderr
        detail = stderr.strip().splitlines()
        tail = detail[-1] if detail else "no error output"
        super().__init__("`{}` exited {}: {}".format(" ".join(args), returncode, tail))


def validate_timeout(timeout: float) -> float:
    if not math.isfinite(timeout) or not 0 < timeout <= threading.TIMEOUT_MAX:
        raise UvError(
            "Timeout must be finite, positive and at most {:g} seconds.".format(threading.TIMEOUT_MAX)
        )
    return timeout


@dataclass(frozen=True)
class UvResult:
    args: List[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def unsupported(self) -> bool:
        """True when uv rejected the command itself, i.e. this uv is too old for it."""
        return self.returncode != 0 and bool(_UNRECOGNIZED.search(self.stderr))


@dataclass
class Uv:
    """A located `uv` executable, pinned to one cache directory."""

    executable: str
    cache_dir: Optional[Path] = None
    timeout: float = DEFAULT_TIMEOUT
    echo: bool = True
    """Stream uv's stderr through to ours so its "waiting for other uv processes" shows live."""

    calls: List[List[str]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.timeout = validate_timeout(self.timeout)

    @classmethod
    def discover(
        cls,
        executable: Optional[str] = None,
        cache_dir: Optional[Path] = None,
        timeout: float = DEFAULT_TIMEOUT,
        echo: bool = True,
    ) -> "Uv":
        candidate = executable or os.environ.get("UVCW_UV") or "uv"
        found = shutil.which(candidate)
        if found is None and os.path.sep in candidate and os.path.exists(candidate):
            found = candidate
        if found is None:
            raise UvNotFound(
                "`{}` was not found on PATH. Install uv (https://docs.astral.sh/uv/getting-started/"
                "installation/) or point UVCW_UV at the executable.".format(candidate)
            )
        return cls(executable=found, cache_dir=cache_dir, timeout=timeout, echo=echo)

    def _argv(self, args: Sequence[str]) -> List[str]:
        argv = [self.executable]
        argv.extend(args)
        # Machine-readable output, and never a spinner in CI logs.
        argv.extend(["--color", "never", "--no-progress"])
        if self.cache_dir is not None:
            argv.extend(["--cache-dir", str(self.cache_dir)])
        return argv

    def run(self, *args: str, check: bool = True) -> UvResult:
        argv = self._argv(args)
        self.calls.append(argv)
        try:
            proc = subprocess.Popen(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise UvNotFound("Could not execute `{}`: {}".format(self.executable, exc))

        out_lines: List[str] = []
        err_lines: List[str] = []
        pumps = [
            threading.Thread(target=_pump, args=(proc.stdout, out_lines, None), daemon=True),
            threading.Thread(
                target=_pump,
                args=(proc.stderr, err_lines, sys.stderr if self.echo else None),
                daemon=True,
            ),
        ]
        for pump in pumps:
            pump.start()
        try:
            proc.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            raise UvTimeout(argv, self.timeout)
        finally:
            # Cancellation must not leave our cache-mutating child running after CLI exit.
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for pump in pumps:
                pump.join(timeout=5.0)

        result = UvResult(argv, proc.returncode, "".join(out_lines), "".join(err_lines))
        if check and not result.ok:
            raise UvFailed(argv, result.returncode, result.stderr)
        return result

    # -- commands ---------------------------------------------------------------------------

    def version(self) -> str:
        return self.run("--version").stdout.strip()

    def resolve_cache_dir(self) -> Path:
        return Path(self.run("cache", "dir").stdout.strip())

    def cache_size(self) -> Optional[int]:
        """Bytes as uv measures them, or None when this uv predates `uv cache size`."""
        result = self.run("cache", "size", "--output-format", "machine", check=False)
        if not result.ok:
            if result.unsupported:
                return None
            raise UvFailed(result.args, result.returncode, result.stderr)
        text = result.stdout.strip().splitlines()
        for line in reversed(text):
            line = line.strip()
            if line.isdigit():
                return int(line)
        return None

    def cache_clean(self, package: str) -> UvResult:
        """Never raises on a failed package; gc logs it, skips it and carries on."""
        return self.run("cache", "clean", package, check=False)

    def cache_prune(self, ci: bool = False) -> UvResult:
        args = ["cache", "prune"]
        if ci:
            args.append("--ci")
        return self.run(*args, check=False)


def _pump(stream, sink: List[str], echo) -> None:
    """Drain a pipe line by line, optionally mirroring it as it arrives.

    Mirroring is why this is not `communicate()`: uv's "Cache is currently in-use, waiting for
    other uv processes to finish" only helps if the user sees it while the wait is happening.
    """
    if stream is None:
        return
    try:
        for line in stream:
            sink.append(line)
            if echo is not None and not _is_noise(line):
                echo.write(line)
                echo.flush()
    finally:
        stream.close()


def _is_noise(line: str) -> bool:
    """uv's own preview banner for `cache size`; we opted in knowingly."""
    return "is experimental and may change without warning" in line
