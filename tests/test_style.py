import io

import pytest

from uv_cache_warden import style
from uv_cache_warden.style import LEFT, RIGHT, Progress, Style, labelled, share, supports_color


class FakeTTY(io.StringIO):
    def __init__(self, tty=True):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("NO_COLOR", "FORCE_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)


def test_no_color_wins_over_force_color(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.setenv("NO_COLOR", "1")
    assert supports_color(FakeTTY()) is False


def test_force_color_works_without_a_tty(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    assert supports_color(FakeTTY(tty=False)) is True


def test_a_pipe_gets_no_color():
    assert supports_color(FakeTTY(tty=False)) is False


def test_dumb_terminal_gets_no_color(monkeypatch):
    monkeypatch.setenv("TERM", "dumb")
    assert supports_color(FakeTTY()) is False


def test_paint_is_a_noop_when_disabled():
    assert Style(enabled=False).paint("x", "red") == "x"


def test_paint_wraps_in_escape_codes_when_enabled():
    assert Style(enabled=True).paint("x", "red") == "\033[31mx\033[0m"


def test_table_aligns_each_column():
    rows = [("PACKAGE", "SIZE"), ("a", "1.0 KiB"), ("longer-name", "10.0 MiB")]
    out = style.table(rows, (LEFT, RIGHT)).splitlines()

    # Column one is as wide as "longer-name"; SIZE is right-aligned so the units line up.
    assert out[0] == "PACKAGE          SIZE"
    assert out[1] == "a             1.0 KiB"
    assert out[2] == "longer-name  10.0 MiB"


def test_table_does_not_paint_trailing_blank_cells():
    """Padded blanks wrapped in escape codes survive rstrip and leave coloured whitespace."""
    rows = [("a", "1", ""), ("b", "2", "note")]
    out = style.table(rows, (LEFT, LEFT, LEFT), styles=[("dim",), None], style=Style(enabled=True))
    first = out.splitlines()[0]

    assert first.endswith("\033[0m")
    assert not first.endswith("  \033[0m")
    assert "\033[2m" in first


def test_table_is_empty_for_no_rows():
    assert style.table([], (LEFT,)) == ""


@pytest.mark.parametrize(
    "part,whole,expected",
    [(50, 100, "50%"), (1, 1000, "<1%"), (0, 100, "-"), (100, 100, "100%"), (5, 0, "-")],
)
def test_share(part, whole, expected):
    assert share(part, whole) == expected


def test_labelled_wraps_under_the_detail_column():
    text = labelled("buckets", "alpha beta gamma delta epsilon", 10, Style(width=30), indent=6)
    lines = text.splitlines()

    assert lines[0].startswith("buckets   ")
    assert len(lines) > 1
    # Continuation lines sit under the detail, not back at column zero.
    assert lines[1].startswith(" " * 18)
    assert lines[1].strip()


def test_progress_stays_silent_when_the_scan_is_quick():
    stream = FakeTTY()
    progress = Progress(stream=stream, delay=10.0)
    for count in range(0, 10000, 2000):
        progress(count)
    progress.done()

    assert stream.getvalue() == ""


def test_progress_appears_once_the_scan_is_slow():
    stream = FakeTTY()
    progress = Progress(stream=stream, delay=0.0)
    progress(0)
    progress(50000)
    assert "50,000 entries" in stream.getvalue()

    progress.done()
    assert stream.getvalue().endswith("\r")


def test_progress_is_disabled_without_a_tty():
    stream = FakeTTY(tty=False)
    progress = Progress(stream=stream, delay=0.0)
    progress(0)
    progress(50000)
    progress.done()

    assert stream.getvalue() == ""


def test_width_never_collapses_below_a_readable_minimum(monkeypatch):
    narrow = type("Size", (), {"columns": 10})()
    monkeypatch.setattr(style.shutil, "get_terminal_size", lambda default: narrow)
    assert Style().width == style.MIN_WIDTH
