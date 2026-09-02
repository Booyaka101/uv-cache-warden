# PROGRESS

**Status: v1.0.1 shipped. On PyPI, released on GitHub, listed on the Marketplace. Nothing outstanding.**

Last updated 2026-09-02.

## What is VERIFIED working

Verified on Windows 11, `uv 0.12.9`, Python 3.12.10, against two real uv caches built by
installing real packages from PyPI (34 packages / 248 MiB, and a 1.9 GiB / 58k-file variant).
Every number below came out of an actual run of the installed wheel, not a test double.

| Check | Result |
|---|---|
| `uvcw doctor` | 11 checks: cache dir, uv version, buckets, sizes, packages, reconciliation, hardlinks, access times, filesystem, sizing mode. Exit 0. |
| Reconciliation, 248 MiB cache | uvcw 259,755,955 vs `uv cache size` 259,755,955 = **byte-exact** |
| Reconciliation, 1.9 GiB cache | uvcw 2,056,888,558 vs `uv cache size` 2,056,888,558 = **byte-exact** |
| Cross-check against an independent `os.walk` | identical to both, 58,066 files / 5,633 dirs |
| `uvcw gc --dry-run` mutates nothing | named 18 of 34 packages, `uv cache size` unchanged at 259,755,955 |
| `uvcw gc` reaches the budget | 259,755,955 -> 116,818,274 against a 150,000,000 budget. Exit 0. |
| Dry run that falls short | says "projected: still over budget by 10.0 MiB" and why; `--fail-over` exits 2 |
| `--min-free` | 150 MB shortfall on the disk produced a 104.7 MiB cache budget; header names `--min-free` |
| `--max-size` and `--min-free` together | tighter one wins, header names it |
| Already under budget | no-op, exit 0 |
| Bad input, missing cache, no uv, bad size literal | one-line message on stderr and exit 1, no traceback |
| Colour rules | `NO_COLOR` beats `FORCE_COLOR`; pipes, `--json` and `--no-color` all plain; Windows VT enabled explicitly |
| Progress counter | appears at 62,000 entries on the 1.9 GiB cache, silent on fast scans, zero bytes on stderr when piped |
| Unit tests | **139 passed**, 0 failed, no network |
| Integration tests | **2 passed** against real uv + PyPI |
| Wheel installs from a clean venv | `uv-cache-warden==1.0.0`, zero dependencies, `uvcw --version` works |
| `twine check` | PASSED on wheel and sdist |

Reproduce: `python -m pytest -q` then `python -m pytest -q -m integration`.
The CI workflow runs the same acceptance checks on Linux, macOS and Windows across
Python 3.9-3.13.

Screenshots in `docs/` were taken by driving a real PowerShell console and capturing the window,
not mocked up.

## Shipped

| Step | State |
|---|---|
| GitHub repo | https://github.com/Booyaka101/uv-cache-warden, public, topics uv/cache/python/packaging/ci/github-actions |
| CI on the release commit | 11/11 green (ubuntu 3.9-3.13, macos 3.11/3.13, windows 3.9/3.13, composite action self-test, build) |
| PyPI | `uv-cache-warden` 1.0.0 and 1.0.1, wheel + sdist, uploaded by `publish.yml` using the `PYPI_API_TOKEN` repo secret |
| `uvx uv-cache-warden report` from PyPI | works on 1.0.1 (failed on 1.0.0, see below) |
| GitHub releases | v1.0.0 and v1.0.1 (latest), floating `v1` tag on the 1.0.1 commit |
| Marketplace listing | live at https://github.com/marketplace/actions/uv-cache-warden, Continuous integration / Dependency management, v1.0.1 |
| astral-sh/uv#5731 comment | drafted, then decided against, see below |

1.0.0 went out first and turned out not to satisfy the brief's own acceptance line: `uvx
uv-cache-warden report` failed with "an executable named `uv-cache-warden` is not provided",
because uvx runs the executable named after the package and only `uvcw` was installed. 1.0.1
adds the second entry point. PyPI versions are immutable, so both exist; 1.0.1 is latest.

## Distribution

Discovery is passive by choice. The package is on PyPI under the name people would search for,
the Action is listed on the Marketplace under Continuous integration and Dependency management,
and the README's opening paragraph carries the phrases someone types into GitHub search when
their disk fills up.

A comment on astral-sh/uv#5731 was drafted and then deliberately not posted. It is Astral's
tracker, and a third-party tool link on a maintainer-owned issue can read as a plug however
carefully it is worded. That judgement is not worth revisiting on a whim; if it ever is, the
draft is in git history at commit cd95a4b.

To move the publish workflow to Trusted Publishing later: on pypi.org add a publisher for
`Booyaka101/uv-cache-warden`, workflow `publish.yml`, environment `pypi`, then delete the
`password:` line in `.github/workflows/publish.yml` and the repo secret.

## Final review findings

### (e) What we missed against the brief, and what was done about it

Re-read line by line. Everything in V1 SCOPE, EDGE CASES, TESTS and ACCEPTANCE CHECKS is
implemented and exercised. Four things the brief implied that were not in the first draft, all
now fixed:

- **The brief's model of archive links was incomplete.** It says archives are "referenced by
  symlinks" and to "build target->package by resolving those symlinks". That is only true on
  Unix. On Windows uv writes a plain 27-byte file whose content is `archive-v0/<id>`, and
  `os.readlink` on it raises `WinError 4390`. Proven on disk before writing the resolver;
  `attribute.read_link` handles both, and ignores links naming a stale archive version the way
  uv's own `resolve_link` does.
- **"Reconcile against `uv cache size`" needs to know what that number is.** It is
  `diskus 0.9.0`, not uv's own removal accounting, and the two differ. Reproducing diskus exactly
  is what took the delta from 1.9% to 0 bytes; the missing piece on Windows was that NTFS
  directories report their index allocation through `st_size`.
- **Windows junctions.** The brief asks to "distinguish directory symlinks from file symlinks".
  The sharper problem is that `S_ISLNK` and `os.path.islink` are both **false** for a junction,
  so a walk that only checks for symlinks follows one straight out of the cache. `sizing.is_reparse`
  checks `FILE_ATTRIBUTE_REPARSE_POINT`, which is what Rust's `FileType::is_symlink` does, and
  there is a test that creates a real junction via `_winapi.CreateJunction`.
- **Recency was being reset by index metadata.** The final end-to-end run showed every package
  as "just now" despite deliberately aged wheels. Cause: `simple-*/pypi/<pkg>.rkyv` is rewritten
  whenever uv resolves, and it was feeding the package's clock. Only the ageing script was
  incomplete in that run, but the underlying behaviour was wrong for a real `uv lock`, which
  touches every package's metadata at once and would have made `--min-age` block everything.
  Metadata bytes still count toward size; metadata timestamps no longer count toward recency.
- **"Report both an on-disk total and a would-reclaim total."** The would-reclaim figure was only
  in the JSON at first. The text report now states it whenever it differs from the on-disk total
  by more than 1%.

Also added beyond the brief because the review asked for them: `--json-file PATH` (a CI step
wants the table in the log *and* the data on disk), and `report --sort {size,age,name}`.

### House-rules checks

- **Clone check.** `difflib.SequenceMatcher` over the line lists of all 86 functions in `src/`.
  The first pass found `attribute._walk_entries` and `sizing.walk` at 45%, two directory
  walkers. Below the 60% threshold, but it was real duplication, so the shared mechanism was
  extracted into `sizing.scandir_tree(root, prune=...)` and both callers now use it. The highest
  remaining pair is 46%, two three-line `as_json` dataclass serialisers with different fields;
  a shared base class there would add indirection for six lines, so that is where it stops.
  Next is `_cmd_doctor` vs `_cmd_report` at 40%, two different workflows whose shared parts are
  already extracted into `_emit`, `_style` and `_progress`, then `parse_size` vs
  `parse_duration` at 34%, kept apart deliberately: merging them would make their error messages
  generic, and those messages are user-facing.
- **Dead code.** `pyflakes` clean. `Plan.reachable` was written and never used, so it is gone.
  `discovery.NAME_INDEXED` existed as a constant while `report.scan` inlined the same tuple; the
  constant is now used. `format_size` had an unreachable `raise AssertionError` after its loop
  and was rewritten so the branch does not exist. `discovery.open_cache` was never called and is
  gone. A `bar()` helper written for the report was deleted rather than shipped unused once the
  percentage column replaced it.
- **Comments.** Only where a constraint is non-obvious: why the stderr pump is not
  `communicate()`, why directories count on Windows, why a package is recorded as evicted before
  the call, why the fake never ships. No rationale essays.
- **Prose.** No em dashes in README, CHANGELOG or the commit message. No numbered scaffolding in
  the README body.
- **No TODO/FIXME/placeholder anywhere.** Checked by grep across `src`, `tests`, `action.yml`,
  the workflows and the docs.

### Second review pass: what the screenshots showed

The first version was reviewed by driving a real PowerShell console, capturing the window and
reading the images. Four things were only visible that way:

- **The report scrolled its own header off.** 34 packages on a standard window left a wall of
  unlabelled numbers. A terminal now stops at 20 rows with a `... N more` line carrying the
  remainder; `--all` and any piped or JSON output are still complete.
- **Sizes were left-aligned**, so `876.1 KiB` and `1.1 MiB` did not line up and the column was
  unreadable. Right-aligned now.
- **No sense of proportion.** A `SHARE` column was added. The first attempt was an eight-cell
  bar, which rendered 41 MiB and 24 KiB identically because everything under 12% rounded to one
  cell. That is worse than no column, so it was replaced with an exact percentage.
- **`doctor` wrapped long details back to column zero**, destroying the alignment that made it
  readable. Details now wrap under the detail column.

Two bugs were found by the same pass, both invisible to the test suite as it stood:

- **A dry run that could not reach the budget did not say so.** It printed
  `Projected 247.7 MiB -> 153.0 MiB (budget 143.1 MiB)` and stopped, which reads as success,
  while `--fail-over` was quietly exiting 2. The wet path explained itself; the dry path did
  not. Both now share one `_outcome` helper.
- **The cache root's own directory entry was never counted.** `uv cache size` counts the path it
  is given. On NTFS a directory reports zero until it outgrows its resident index, so this was
  exactly zero bytes on every cache tested until one grew, then 8 KiB. Caught by the growing
  test cache, fixed by having `walk` yield the root, and now pinned by a test asserting the
  classified pass and the flat pass agree.

### (f) Features that would make this best in class

Built, because they were clearly right and cheap:

- `report --sort age`: the coldest-first order is exactly what `gc` will evict, so it previews a
  budget without running one.
- `report --top` default of 20 on a terminal, plus `--all`, plus a `SHARE` percentage column.
- `gc --min-free SIZE`: budget against the filesystem rather than the cache. This is the shape a
  runner that keeps filling its disk actually needs, and it is what jroesch's 1 TB case in #5731
  is really asking for. Combined with `--max-size`, the tighter constraint wins and the header
  names it.
- Colour, with `NO_COLOR` / `FORCE_COLOR` / `--no-color` and never on JSON or a pipe. Windows
  needs `SetConsoleMode(ENABLE_VIRTUAL_TERMINAL_PROCESSING)` explicitly, so that is done.
- A stderr progress counter for slow scans. A 1 TB cache is roughly a million files; measured
  here at about 27,000 entries a second on Windows, that is over half a minute of looking hung.
- `--json-file PATH` on every command.
- Prune even when nothing is evictable, which is the only way to reclaim the orphaned bytes in
  astral-sh/uv#16551.
- Multi-round gc, bounded at 3, because a single projection under-evicts when a package's bytes
  are hardlinked to bytes that survive.

Not built, listed as concrete next steps:

- **Attribute source distributions from URL, path and Git.** uv identifies these by reading
  `metadata.msgpack`. A ~120-line msgpack reader for the subset uv writes would keep the
  stdlib-only promise and move those bytes out of `unattributed`.
- **`uvcw gc --keep-project`**: read `uv.lock` or `pyproject.toml` in the working directory and
  keep everything the current project depends on. Removes the main reason to type `--keep`.
- **A size-aware tie-break within an age band.** Strict LRU will evict thirty small old packages
  to get at one large new one. Bucketing by day and taking the largest first inside each bucket
  would reach the same budget with far less churn, while still being recognisably LRU. This is
  the most valuable remaining idea and the one that needs a design decision, not just code.
- **`--since` on report**, to diff against an earlier JSON dump. Useful for finding which CI job
  is filling a shared runner cache.
- **Opt-in eviction of `python-v0` and `osv-v0`.** Out of v1 scope deliberately, but a runner
  with ten managed interpreters may want `--include-python`.

## Where the shipped code came from

`PHASE0.md` records every upstream fact with its quote and URL: the CLI option lists, the
`CacheBucket::to_str` names, `ARCHIVE_VERSION`, `create_link`/`resolve_link`, the `file_size`
rule in `removal.rs`, the `diskus` call in `cache_size.rs`, and the two GitHub issues with their
live API counts. Recheck that file first when uv changes.
