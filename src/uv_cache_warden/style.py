"""Terminal presentation shared by the three commands.

Colour is opt-out through `NO_COLOR`, opt-in through `FORCE_COLOR`, and otherwise only used
when the stream is a terminal, so piping into a file or a CI log never yields escape codes.
Everything here degrades to plain ASCII: the layout has to survive with colour switched off,
because that is how most people will read it.
"""

import os
import shutil
import sys
import textwrap
from typing import List, Optional, Sequence

RESET = "\033[0m"
_CODES = {
    "bold": "1",
    "dim": "2",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
}

MIN_WIDTH = 60
DEFAULT_WIDTH = 100


def supports_color(stream=None) -> bool:
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        if not stream.isatty():
            return False
    except (AttributeError, ValueError):
        return False
    if os.name == "nt":
        return _enable_windows_vt(stream)
    return True


def _enable_windows_vt(stream) -> bool:
    """Turn on ANSI handling for this console. Windows does not do it for us."""
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel32 = ctypes.windll.kernel32
        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        enable_virtual_terminal_processing = 0x0004
        if mode.value & enable_virtual_terminal_processing:
            return True
        return bool(
            kernel32.SetConsoleMode(handle, mode.value | enable_virtual_terminal_processing)
        )
    except Exception:
        return False


class Style:
    """Paints text, or does not. One instance decides for a whole command's output."""

    def __init__(self, enabled: bool = False, width: Optional[int] = None):
        self.enabled = enabled
        self._width = width

    @classmethod
    def detect(cls, stream=None) -> "Style":
        return cls(enabled=supports_color(stream))

    @property
    def width(self) -> int:
        if self._width is not None:
            return self._width
        columns = shutil.get_terminal_size((DEFAULT_WIDTH, 24)).columns
        return max(MIN_WIDTH, columns)

    def paint(self, text: str, *names: str) -> str:
        if not self.enabled or not names:
            return text
        codes = ";".join(_CODES[name] for name in names if name in _CODES)
        return "{}{}m{}{}".format("\033[", codes, text, RESET) if codes else text

    def heading(self, text: str) -> str:
        return self.paint(text, "bold")

    def dim(self, text: str) -> str:
        return self.paint(text, "dim")

    def note(self, text: str) -> str:
        """A statement of fact about the environment, not a problem."""
        return self.dim(self.wrap("note: " + text))

    def warn(self, text: str) -> str:
        return self.paint(self.wrap(text), "yellow")

    def wrap(self, text: str, indent: str = "  ") -> str:
        return "\n".join(
            textwrap.wrap(
                text,
                width=self.width,
                subsequent_indent=indent,
                break_long_words=False,
                break_on_hyphens=False,
            )
        ) or text


LEFT = "<"
RIGHT = ">"


def table(
    rows: Sequence[Sequence[str]],
    aligns: Sequence[str],
    gap: str = "  ",
    styles: Optional[Sequence[Optional[Sequence[str]]]] = None,
    style: Optional[Style] = None,
) -> str:
    """Render aligned columns.

    Colour is applied after padding is computed, since escape codes would otherwise be counted
    as visible characters and throw every column out.
    """
    if not rows:
        return ""
    columns = max(len(row) for row in rows)
    widths = [
        max(len(row[index]) if index < len(row) else 0 for row in rows)
        for index in range(columns)
    ]
    lines: List[str] = []
    for position, row in enumerate(rows):
        names = styles[position] if (styles is not None and position < len(styles)) else None
        # Trailing blanks are dropped before painting. Padding them would end the line in escape
        # codes wrapped around whitespace, which `rstrip` can no longer see.
        last = max(
            (index for index in range(columns) if index < len(row) and row[index]), default=-1
        )
        cells = []
        for index in range(last + 1):
            text = row[index] if index < len(row) else ""
            align = aligns[index] if index < len(aligns) else LEFT
            padded = text.rjust(widths[index]) if align == RIGHT else text.ljust(widths[index])
            if names and style is not None:
                padded = style.paint(padded, *names)
            cells.append(padded)
        lines.append(gap.join(cells).rstrip())
    return "\n".join(lines)


def share(part: int, whole: int) -> str:
    """A package's share of the cache, as a number rather than a bar.

    An eight-cell bar cannot tell 1% from 12%, so at cache scale it renders 41 MiB and 24 KiB
    identically. A percentage is narrower and never lies.
    """
    if whole <= 0:
        return "-"
    fraction = part / float(whole)
    if fraction <= 0:
        return "-"
    if fraction < 0.01:
        return "<1%"
    return "{:.0f}%".format(fraction * 100)


def labelled(label: str, detail: str, label_width: int, style: Style, indent: int = 0) -> str:
    """A label plus a detail that wraps under the detail column, not back to column zero.

    `indent` is the printed width of whatever the caller puts before the label. It is passed as a
    number because that prefix usually contains escape codes, whose length is not their width.
    """
    pad = " " * (indent + label_width + 2)
    body = textwrap.wrap(
        detail,
        width=max(20, style.width - len(pad)),
        break_long_words=False,
        break_on_hyphens=False,
    ) or [detail]
    first = "{}  {}".format(label.ljust(label_width), body[0])
    return "\n".join([first] + [pad + line for line in body[1:]])


class Progress:
    """A one-line stderr counter that only appears once a scan is visibly slow.

    A 1 TB cache is a million files; without this the tool looks hung for the best part of a
    minute. Anything faster than `delay` prints nothing at all.
    """

    def __init__(self, stream=None, delay: float = 1.5, enabled: bool = True):
        self.stream = stream if stream is not None else sys.stderr
        self.delay = delay
        self.enabled = enabled and _is_tty(self.stream)
        self._start: Optional[float] = None
        self._shown = False
        self._count = 0

    def __call__(self, count: int) -> None:
        if not self.enabled:
            return
        import time

        self._count = count
        if self._start is None:
            self._start = time.time()
            return
        if time.time() - self._start < self.delay:
            return
        self._shown = True
        self.stream.write("\r  scanning cache... {:,} entries".format(count))
        self.stream.flush()

    def done(self) -> None:
        if self._shown:
            self.stream.write("\r" + " " * 46 + "\r")
            self.stream.flush()
            self._shown = False


def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False
