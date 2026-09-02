"""Argument parsing, subcommand dispatch and exit codes.

Exit codes: 0 success, 1 the tool could not do its job (no uv, no cache, bad arguments),
2 the budget was not met and `--fail-over` asked us to say so.
"""

import argparse
import json
import os
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import __version__, doctor, gc as gc_mod, report as report_mod
from .discovery import CacheLayout, DiscoveryError, discover_cache_dir, load_layout
from .sizing import parse_duration, parse_size
from .style import Progress, Style
from .uvcmd import DEFAULT_TIMEOUT, Uv, UvError, UvNotFound

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_OVER_BUDGET = 2

PROG = "uvcw"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Enforce a size budget on Astral uv's package cache.",
        epilog=(
            "Every deletion is performed by uv itself (`uv cache clean`, `uv cache prune`); "
            "uvcw never unlinks a cache file. Defaults can also be set through UVCW_CACHE_DIR, "
            "UVCW_UV, UVCW_TIMEOUT, UVCW_MAX_SIZE, UVCW_MIN_FREE, UVCW_MIN_AGE and UVCW_KEEP."
        ),
    )
    parser.add_argument("--version", action="version", version="{} {}".format(PROG, __version__))
    subparsers = parser.add_subparsers(dest="command")

    report_parser = subparsers.add_parser(
        "report", help="Per-package size breakdown with last-access times."
    )
    _add_common(report_parser)
    report_parser.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    report_parser.add_argument(
        "--top",
        type=_positive_int,
        metavar="N",
        help="Show only the first N packages. Defaults to {} on a terminal so the header stays "
        "on screen; piped output is never truncated.".format(report_mod.DEFAULT_TOP),
    )
    report_parser.add_argument(
        "--all", action="store_true", help="Show every package, however long the table."
    )
    report_parser.add_argument(
        "--sort",
        choices=report_mod.SORT_ORDERS,
        default=report_mod.SORT_SIZE,
        help="Order packages by size (default), by age (coldest first, the order gc evicts in) "
        "or by name.",
    )
    report_parser.set_defaults(handler=_cmd_report)

    gc_parser = subparsers.add_parser(
        "gc", help="Evict least-recently-used packages until the cache fits a budget."
    )
    _add_common(gc_parser)
    gc_parser.add_argument(
        "--max-size",
        metavar="SIZE",
        help="Cache size budget, e.g. 10GB, 4GiB or a byte count. Give this or --min-free, "
        "or both.",
    )
    gc_parser.add_argument(
        "--min-free",
        metavar="SIZE",
        help="Evict until at least this much space is free on the cache's filesystem, e.g. 20GB. "
        "Combined with --max-size, the tighter constraint wins.",
    )
    gc_parser.add_argument(
        "--min-age",
        metavar="DURATION",
        help="Never evict a package used more recently than this, e.g. 7d, 12h.",
    )
    gc_parser.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="PKG",
        help="Never evict this package. Repeatable.",
    )
    gc_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the eviction list and run no mutating uv command.",
    )
    gc_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    gc_parser.add_argument(
        "--fail-over",
        action="store_true",
        help="Exit 2 if the cache is still over budget afterwards.",
    )
    gc_parser.add_argument(
        "--no-prune",
        dest="prune",
        action="store_false",
        help="Skip the final `uv cache prune`.",
    )
    gc_parser.set_defaults(handler=_cmd_gc, prune=True)

    doctor_parser = subparsers.add_parser(
        "doctor", help="Report the cache location, uv version, buckets and sizing accuracy."
    )
    _add_common(doctor_parser)
    doctor_parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    doctor_parser.set_defaults(handler=_cmd_doctor)

    return parser


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-color", action="store_true", help="Never colourise output. NO_COLOR does the same."
    )
    parser.add_argument(
        "--cache-dir",
        metavar="PATH",
        default=None,
        help="Cache directory to inspect. Defaults to `uv cache dir`.",
    )
    parser.add_argument(
        "--uv", metavar="PATH", default=None, help="Path to the uv executable."
    )
    parser.add_argument(
        "--json-file",
        metavar="PATH",
        default=None,
        help="Also write the JSON result to this file, so a run can log text and emit data.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        metavar="SECONDS",
        default=None,
        help=(
            "Seconds to wait for a uv subprocess. uv blocks until no other process is reading "
            "the cache, so this needs headroom. Default {:g}.".format(DEFAULT_TIMEOUT)
        ),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if getattr(args, "command", None) is None:
        parser.print_help()
        return EXIT_ERROR
    try:
        return args.handler(args)
    except UvNotFound as exc:
        return _fail(str(exc))
    except DiscoveryError as exc:
        return _fail(str(exc))
    except UvError as exc:
        return _fail(str(exc))
    except KeyboardInterrupt:
        return _fail("interrupted")
    except OSError as exc:
        return _fail("filesystem error: {}".format(exc))


def _fail(message: str) -> int:
    sys.stderr.write("{}: error: {}\n".format(PROG, message))
    return EXIT_ERROR


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("{!r} is not a whole number".format(text))
    if value < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return value


def _timeout(args: argparse.Namespace) -> float:
    if args.timeout is not None:
        return args.timeout
    raw = os.environ.get("UVCW_TIMEOUT")
    if raw:
        try:
            return float(raw)
        except ValueError:
            raise DiscoveryError("UVCW_TIMEOUT must be a number of seconds, got {!r}".format(raw))
    return DEFAULT_TIMEOUT


def _open(args: argparse.Namespace, echo: bool = True) -> Tuple[Uv, CacheLayout]:
    """Locate uv and the cache, and pin uv to the cache we are about to act on."""
    uv = Uv.discover(args.uv, timeout=_timeout(args), echo=echo)
    override = Path(args.cache_dir) if args.cache_dir else None
    root, source = discover_cache_dir(uv=uv, override=override)
    layout = load_layout(root, source=source)
    uv.cache_dir = layout.root
    return uv, layout


def _style(args: argparse.Namespace) -> Style:
    """Colour is for humans reading a terminal, never for JSON or a redirected log."""
    if getattr(args, "no_color", False) or getattr(args, "json", False):
        return Style(enabled=False)
    return Style.detect(sys.stdout)


def _progress(args: argparse.Namespace) -> Progress:
    return Progress(enabled=not getattr(args, "json", False))


def _emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    """Write the result once, in whichever forms were asked for."""
    sys.stdout.write((json.dumps(payload, indent=2) if args.json else text) + "\n")
    if args.json_file:
        with open(args.json_file, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, indent=2) + "\n")


def _row_limit(args: argparse.Namespace) -> Optional[int]:
    if args.all or args.json:
        return None
    if args.top is not None:
        return args.top
    return report_mod.DEFAULT_TOP if sys.stdout.isatty() else None


def _cmd_report(args: argparse.Namespace) -> int:
    style = _style(args)
    uv, layout = _open(args, echo=not args.json)
    progress = _progress(args)
    try:
        result = report_mod.scan(layout, uv, progress=progress)
    finally:
        progress.done()
    top = _row_limit(args)
    _emit(
        args,
        report_mod.to_json(result, top=None if args.all else args.top, sort=args.sort),
        report_mod.render(result, top=top, sort=args.sort, style=style),
    )
    return EXIT_OK


def _cmd_gc(args: argparse.Namespace) -> int:
    style = _style(args)
    raw_size = args.max_size or os.environ.get("UVCW_MAX_SIZE")
    raw_free = args.min_free or os.environ.get("UVCW_MIN_FREE")
    if not raw_size and not raw_free:
        return _fail(
            "give a budget: --max-size 10GB, --min-free 20GB, or both "
            "(UVCW_MAX_SIZE / UVCW_MIN_FREE also work)"
        )
    try:
        max_size = parse_size(raw_size) if raw_size else None
        min_free = parse_size(raw_free) if raw_free else None
    except ValueError as exc:
        return _fail(str(exc))

    raw_age = args.min_age or os.environ.get("UVCW_MIN_AGE")
    min_age = 0.0
    if raw_age:
        try:
            min_age = parse_duration(raw_age)
        except ValueError as exc:
            return _fail(str(exc))

    keep: List[str] = list(args.keep)
    keep.extend(_split_env(os.environ.get("UVCW_KEEP")))

    uv, layout = _open(args, echo=not args.json)
    try:
        current = uv.cache_size()
    except UvError:
        current = None
    if current is None:
        from .sizing import measure_tree

        current = measure_tree(layout.root).disk_bytes
    budget, bound_by = gc_mod.budget_for(current, layout.root, max_size, min_free)

    progress = _progress(args)
    try:
        result = gc_mod.run(
            layout,
            uv,
            budget=budget,
            min_age=min_age,
            keep=keep,
            dry_run=args.dry_run,
            prune=args.prune,
            progress=progress,
            bound_by=bound_by,
        )
    finally:
        progress.done()
    _emit(args, gc_mod.to_json(result), gc_mod.render(result, style=style))

    if args.fail_over and result.over_budget:
        return EXIT_OVER_BUDGET
    return EXIT_OK


def _cmd_doctor(args: argparse.Namespace) -> int:
    style = _style(args)
    override = Path(args.cache_dir) if args.cache_dir else None
    progress = _progress(args)
    try:
        diagnosis = doctor.diagnose(cache_dir=override, uv_path=args.uv, progress=progress)
    finally:
        progress.done()
    _emit(args, diagnosis.as_json(), doctor.render(diagnosis, style=style))
    return EXIT_ERROR if diagnosis.failed else EXIT_OK


def _split_env(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]


if __name__ == "__main__":
    sys.exit(main())
