# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-10-17

### Changed

- Over-budget real GC now runs native `uv cache prune` before selecting any package. It
  re-measures and refreshes bucket layout and attribution even after a returned prune failure.
  If pruning meets the budget, no package clean is issued. A final prune runs only
  after at least one successful package clean. `--no-prune` disables both stages.
- Dry runs retain measured after-size and existing package projections, but explicitly warn
  that pruning in a real run may make those projected evictions unnecessary.
- The Action defaults to 1.1.0 and includes prune stages and conditional projections in its
  summary, without changing its existing outputs. Documentation and terminal captures use a
  real uv 0.13.1 scratch-cache run with successful offline reinstall.

### Added

- Additive JSON fields `prune_steps` (stage, status, diagnostic and measured after-size) and
  `eviction_projection_conditional`. `pruned` remains true when any stage succeeds;
  `prune_detail` aggregates stage-labelled failures.
- Regression checks for prune-only preservation, partial failure, refreshed stale buckets,
  dry-run file invariance, fatal subprocess errors and Action summaries. CI pins uv 0.13.1;
  integration subprocess failures fail rather than silently skip.
- Action `no-prune` input for parity with the CLI, defaulting to false.

### Fixed

- Timeouts and missing executables now propagate through discovery, scanning and GC
  measurement to the CLI instead of falling back and potentially continuing mutation.
  Ordinary measurement failures still fall back to the existing filesystem walk.
- Previously attempted names no longer consume later rounds' projected savings. Failed
  cleans are not retried, and surviving eligible packages can be considered within the
  existing three-round limit.
- Failed Action invocations no longer emit stale outputs or summaries from a previous run.
- Post-mutation inventory refresh accepts a cache whose last recognized bucket uv removed;
  initial discovery still refuses directories without recognized cache buckets.
- Action result parsing and output-write failures now fail the step instead of being masked.
- Cancellation stops and reaps the uv subprocess. All commands, including `doctor`, honor
  validated finite positive timeouts; diagnostic subprocess failures stay structured.
- Package aliases share normalized attribution and keep/min-age protection. Unknown archive
  referrers prevent sole-ownership claims, and stale POSIX archive links are ignored.
- Byte budgets above float's exact integer range retain their precision. Overflowing size
  and duration literals return validation errors rather than tracebacks or infinite ages.

## [1.0.3] - 2026-09-02

### Changed

- Documentation only, no code change. The README's examples and screenshots were captured on a
  Windows scratch directory, so every path in them read like a test rig. They are now taken on
  Linux at the documented default cache location, which is where most of the reports in
  astral-sh/uv#5731 come from, and they show the `st_blocks*512` sizing and `relatime`
  access-time branches rather than the NTFS ones.

## [1.0.2] - 2026-09-02

### Changed

- Documentation only, no code change. Dropped a "Distribution" section from the README that was
  build-planning notes rather than anything a user of the tool needs, and replaced `PHASE0.md`
  with [`docs/upstream-notes.md`](docs/upstream-notes.md), which keeps the verbatim uv sources
  the byte accounting depends on and drops the surrounding project chatter.

## [1.0.1] - 2026-09-02

### Fixed

- `uvx uv-cache-warden` now works. uvx runs the executable named after the package, and 1.0.0
  only shipped `uvcw`, so the one-line install in the README failed with "an executable named
  `uv-cache-warden` is not provided". Both names now resolve to the same entry point.
- The publish workflow only runs for release tags (`v1.2.3`), so pushing the floating `v1` tag
  the Action is used through no longer produces a failed run.

## [1.0.0] - 2026-09-02

First release.

### Added

- `uvcw report`: per-package breakdown of the uv cache with sizes and last-access times,
  `--top N` and `--json`. Totals reconcile with `uv cache size --output-format machine`.
- `uvcw gc --max-size SIZE`: least-recently-used eviction down to a byte budget, with
  `--min-age`, `--keep`, `--dry-run`, `--json` and `--fail-over`. Every deletion goes through
  `uv cache clean <package>` followed by a single `uv cache prune`; nothing is unlinked
  directly and `--force` is never passed to uv.
- `uvcw doctor`: cache location and how it was found, uv version, bucket inventory with
  sizes, reconciliation against `uv cache size`, hardlink statistics and whether access times
  are tracked on this filesystem.
- `--json-file PATH` on every command, so a run can print a human summary and emit machine
  data in one pass.
- `uvcw gc --min-free SIZE`: budget against free space on the cache's filesystem rather than
  against the cache. Combined with `--max-size`, the tighter constraint wins and the header
  names which one is binding.
- `uvcw report --sort {size,age,name}` and `--all`. On a terminal the table stops at 20 rows so
  the header stays on screen; piped and JSON output are never truncated.
- A `SHARE` column giving each package's percentage of the cache, and right-aligned sizes.
- Colour on terminals, honouring `NO_COLOR`, `FORCE_COLOR` and `--no-color`, never applied to
  JSON or to a redirected stream. Windows consoles get VT processing enabled explicitly.
- A `scanning cache... N entries` counter on stderr for scans that run longer than 1.5 seconds.
- Composite GitHub Action with `gc`, `dry-run` and `report` modes, a `min-free` input, outputs for
  `size-before`, `size-after`, `reclaimed`, `evicted`, `over-budget` and `json-path`, and a
  job summary table.
- `UVCW_CACHE_DIR`, `UVCW_UV`, `UVCW_TIMEOUT`, `UVCW_MAX_SIZE`, `UVCW_MIN_AGE` and
  `UVCW_KEEP` as defaults for the matching flags.

### Notes on accuracy

- Byte accounting reproduces `diskus 0.9.0`, which is what `uv cache size` uses:
  `st_blocks * 512` with `(dev, ino)` dedupe on Unix, `st_size` with no dedupe on Windows,
  directories counted on both. A physical total with inode dedupe on both platforms and uv's
  own reclaim estimate are reported alongside it.
- Archive links are resolved in both shapes uv writes them: a symlink on Unix, and on Windows
  a regular file containing `archive-v<n>/<id>`.
- Directory traversal stops at symlinks and at Windows junctions. `S_ISLNK` is false for a
  junction, so a walk that only checks for symlinks would follow one straight out of the cache.
- A package's last-used time ignores the `simple-*` bucket. `uv lock` rewrites that metadata for
  every package it resolves, which would otherwise make an entire lockfile look freshly used.
- The cache root's own directory entry is counted. `uv cache size` counts the path it is given,
  and on NTFS a directory reports zero until it outgrows its resident index, so omitting it was
  invisible on a small cache and 8 KiB short on a larger one.
- Recency comes from regular files only; directories and symlinks contribute their mtime. On a
  `relatime` mount (the Linux default) listing a directory or reading a symlink bumps its atime,
  and the scan does both to every entry, so the tool was marking every package as used by its
  own visit. Found by running the suite on ext4 under WSL before the first CI run finished.
- A dry run that cannot reach the budget says so, and says which rule held the bytes back. It
  previously printed the projection and stopped, which read as success even while `--fail-over`
  was exiting 2.

[1.1.0]: https://github.com/Booyaka101/uv-cache-warden/compare/v1.0.3...upgrade/prune-first-1.1.0
[1.0.3]: https://github.com/Booyaka101/uv-cache-warden/releases/tag/v1.0.3
[1.0.2]: https://github.com/Booyaka101/uv-cache-warden/releases/tag/v1.0.2
[1.0.1]: https://github.com/Booyaka101/uv-cache-warden/releases/tag/v1.0.1
[1.0.0]: https://github.com/Booyaka101/uv-cache-warden/releases/tag/v1.0.0
