# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-09-02

First release.

### Added

- `uvcw report` — per-package breakdown of the uv cache with sizes and last-access times,
  `--top N` and `--json`. Totals reconcile with `uv cache size --output-format machine`.
- `uvcw gc --max-size SIZE` — least-recently-used eviction down to a byte budget, with
  `--min-age`, `--keep`, `--dry-run`, `--json` and `--fail-over`. Every deletion goes through
  `uv cache clean <package>` followed by a single `uv cache prune`; nothing is unlinked
  directly and `--force` is never passed to uv.
- `uvcw doctor` — cache location and how it was found, uv version, bucket inventory with
  sizes, reconciliation against `uv cache size`, hardlink statistics and whether access times
  are tracked on this filesystem.
- `--json-file PATH` on every command, so a run can print a human summary and emit machine
  data in one pass.
- Composite GitHub Action with `gc`, `dry-run` and `report` modes, outputs for
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

[1.0.0]: https://github.com/Booyaka101/uv-cache-warden/releases/tag/v1.0.0
