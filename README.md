# uv-cache-warden

Keep [uv](https://docs.astral.sh/uv/)'s package cache under a size budget, with least-recently-used eviction. Dependency-free Python CLI (`uvcw`) plus a GitHub Action.

Version 1.1.0 prunes first and only cleans packages when the cache is still over budget. Dry runs explain when their package projections depend on pruning.

![uvcw report](docs/report.png)

## The problem

uv's cache can grow well beyond the space available on a runner. [astral-sh/uv#5731](https://github.com/astral-sh/uv/issues/5731) has been open since August 2024. One [NVIDIA report](https://github.com/astral-sh/uv/issues/5731#issuecomment-4995906061) describes a cache "routinely exceeding 1TB+". These reports establish cache pressure, not warden performance. uv ships `uv cache size`, `uv cache clean [PACKAGE]` and `uv cache prune`, but no size budget.

Warden measures the cache, prunes first when over budget, then selects cold packages only if more space is needed. That avoids discarding reusable packages when native pruning alone is enough.

The upstream automatic-pruning proposal, [#20116](https://github.com/astral-sh/uv/pull/20116), is still open as of 2026-10-17. Native pruning also removes cached environments, temporary build environments and obsolete buckets. It does not evict old wheel versions that remain referenced. A budget over those packages stays an external job.

uv's docs say it is [never safe to modify the cache directly](https://docs.astral.sh/uv/concepts/cache/). uv-cache-warden never unlinks a file. Every deletion is a `uv cache clean <package>` or `uv cache prune`, without `--ci` or `--force`. uv retains control of mutations and locking.

## Install

```console
$ uv tool install uv-cache-warden
$ uvcw --version
uvcw 1.1.0
```

Or run it without installing:

```console
$ uvx uv-cache-warden report
```

`pip install uv-cache-warden` and `pipx install uv-cache-warden` work too. Python 3.9 or newer, no runtime dependencies.

## Usage

### `uvcw report`

```console
$ uvcw report --top 8
PACKAGE       VERS       SIZE  SHARE  LAST USED
scipy            1  111.0 MiB    36%  just now
numpy            1   58.2 MiB    19%  11 days ago
pandas           1   42.2 MiB    14%  19 days ago
matplotlib       1   28.9 MiB     9%  11 days ago
fonttools        1   23.3 MiB     8%  2 days ago
pillow           1   19.8 MiB     6%  19 days ago
kiwisolver       1    5.9 MiB     2%  2 days ago
pygments         1    5.1 MiB     2%  44 days ago
... 23 more          12.9 MiB     4%
unattributed         52.0 KiB    <1%
TOTAL               307.5 MiB         (uv cache size: 307.5 MiB)
```

On a terminal the table stops at 20 rows so the header stays on screen. `--all` shows everything, and piped output is never truncated.

`--sort age` reorders the table coldest-first, which is the order `gc` evicts in, so it previews what a budget would take:

```console
$ uvcw report --sort age --top 5
PACKAGE       VERS       SIZE  SHARE  LAST USED
anyio            1  684.0 KiB    <1%  63 days ago
httpcore         1  432.0 KiB    <1%  44 days ago
pygments         1    5.1 MiB     2%  44 days ago
markupsafe       1  348.0 KiB    <1%  28 days ago
pandas           1   42.2 MiB    14%  19 days ago
... 26 more         258.7 MiB    84%
unattributed         52.0 KiB    <1%
TOTAL               307.5 MiB         (uv cache size: 307.5 MiB)
```

`--json` gives the same data with per-package `disk_bytes`, `physical_bytes`, `reclaim_bytes`, `age_seconds`, a bucket inventory and the reconciliation delta against `uv cache size`. `--json-file PATH` writes that alongside the table instead of replacing it, which is what you want in a CI log.

### `uvcw gc`

![uvcw gc](docs/gc.png)

A dry run changes nothing. It projects package cleaning against the measured cache, without guessing what native pruning will reclaim:

```console
$ uvcw gc --max-size 200MB --min-age 7d --dry-run
cache /home/dev/.cache/uv  budget 190.7 MiB (--max-size)  currently 307.5 MiB
  886.2 GiB free on this filesystem

would evict (11 of 31 packages):
  anyio       684.0 KiB  63 days ago
  httpcore    432.0 KiB  44 days ago
  pygments      5.1 MiB  44 days ago
  markupsafe  348.0 KiB  28 days ago
  pandas       42.2 MiB  19 days ago
  pillow       19.8 MiB  19 days ago
  pyparsing   588.0 KiB  19 days ago
  certifi     344.0 KiB  11 days ago
  matplotlib   28.9 MiB  11 days ago
  mdurl        80.0 KiB  11 days ago
  numpy        58.2 MiB  11 days ago

dry run: nothing was removed. Projected 307.5 MiB -> 150.8 MiB (budget 190.7 MiB).
A real run prunes first; projected package evictions may disappear.
```

Drop `--dry-run` and it runs `uv cache prune` first, re-measures, reloads the bucket layout and rescans attribution. If that meets the budget, no package is cleaned. Otherwise it cleans eligible packages oldest-first, re-measures for up to three rounds, and prunes once more only if at least one clean succeeded. The `Removed ...` and `Pruning cache at:` lines come from uv itself, streamed through so a wait on the cache lock is visible.

This real uv 0.13.1 run used an isolated cache containing `idna==3.7` and `mdurl==0.1.2`, plus a 64 KiB obsolete-bucket test fixture. Selected output, also shown in the screenshot above:

```console
$ uvcw gc --max-size 416768 --fail-over
Pruning cache at: .venv/docs-run/cache
Removed 1 file (64.0KiB)
Budget met by initial prune; no packages cleaned.
uv cache prune reclaimed 64.0 KiB; cache now 407.0 KiB.
```

Both packages remained cached and `uv pip install --offline --reinstall idna==3.7 mdurl==0.1.2` succeeded afterwards. This is a small compatibility check, not a throughput benchmark.

`--json` and `--json-file` retain the existing fields and add:

- `prune_steps`: attempted stages, each with `stage` (`initial` or `final`), `ok`, `detail`, and measured `size_after_bytes`. No attempted prune means `[]`.
- `eviction_projection_conditional`: true for an over-budget dry run with pruning enabled. `size_after_bytes` remains the unchanged measurement; `evicted` and `over_budget` describe the package projection as before. `--no-prune` skips both stages and makes that projection unconditional on pruning.

`pruned` means at least one prune succeeded. `prune_detail` aggregates stage-labelled failures. A returned nonzero prune exit can still have removed data, so warden re-measures and refreshes before selecting packages. A timeout or missing executable stops the run immediately with exit 1, without further mutation.

`reclaimed` is always measured afterwards, never projected, using `uv cache size` or the filesystem-walk fallback when machine sizing is unavailable. If a plan cannot reach the budget, the run says so and names the rule that held the bytes back, on a dry run too; `--fail-over` turns that into exit 2.

Already under budget is a no-op that exits 0:

```console
$ uvcw gc --max-size 5GB
cache /home/dev/.cache/uv  budget 4.7 GiB (--max-size)  currently 150.8 MiB
  886.4 GiB free on this filesystem
Already under budget (150.8 MiB <= 4.7 GiB). Nothing to do.
```

`--min-free` budgets against the filesystem rather than the cache, which is what a runner that keeps filling its disk actually wants. The budget becomes whatever the cache has to give back to leave that much free, so on a disk with room to spare it is a no-op:

```console
$ uvcw gc --min-free 50GB
cache /home/dev/.cache/uv  budget 150.8 MiB (--min-free)  currently 150.8 MiB
  886.4 GiB free on this filesystem
Already under budget (150.8 MiB <= 150.8 MiB). Nothing to do.
```

Give both and the tighter one wins; the header always names which constraint is binding. Even with nothing eligible for package cleaning, an over-budget real run prunes first. Initially under-budget runs, including equality, and dry runs never invoke either mutation.

### `uvcw doctor`

![uvcw doctor](docs/doctor.png)

```console
$ uvcw doctor
[ ok ] uv-cache-warden  1.1.0 on Python 3.14.4
```

The screenshot shows the full check on the same isolated cache. It includes a timestamp-fallback warning because the filesystem is mounted with `noatime`.

Two of those checks are worth knowing about before you trust a budget. **reconciliation** compares our total against `uv cache size`; they should agree exactly. **access times** says whether LRU is real here: on Windows, NTFS last-access updates are off by default, and plenty of Linux mounts are `noatime`, in which case eviction falls back to write order and `uvcw gc` says so in its header too.

`doctor` exits 1 if any check fails, so it works as a preflight step.

### Output

Colour is on for terminals and off everywhere else. `NO_COLOR` disables it, `FORCE_COLOR` forces it, `--no-color` does the same per command, and `--json` is never coloured. A scan of a large cache prints a `scanning cache... 62,000 entries` counter on stderr, but only after it has already been running for a second and a half, so quick runs stay silent and piped output stays clean.

## GitHub Action

```yaml
- uses: astral-sh/setup-uv@v10.0.1
- run: uv sync

- uses: Booyaka101/uv-cache-warden@v1
  with:
    max-size: 4GB
    min-age: 3d
    keep: torch
```

Inputs: `max-size`, `min-free`, `min-age`, `keep` (comma-separated), `mode` (`gc`, `dry-run` or `report`), `fail-over`, `no-prune`, `cache-dir`, `timeout`, `version`, `summary`. Set `no-prune: "true"` to skip both native pruning stages.
Outputs: `size-before`, `size-after`, `reclaimed`, `evicted`, `over-budget`, `json-path`. Their names and types are unchanged. The default summary includes each prune stage and its measured result; dry-run summaries distinguish measured cache size from conditional package projections.

A self-hosted runner that keeps its cache between jobs is the case this exists for:

```yaml
- uses: Booyaka101/uv-cache-warden@v1
  if: always()
  with:
    max-size: 20GB
    min-free: 50GB
```

`version: local` installs from the checkout instead of PyPI, which is how this repository tests its own action.

## Configuration

Flags win; environment variables fill in defaults. There is no config file.

| Variable | Equivalent |
|---|---|
| `UVCW_CACHE_DIR` | `--cache-dir` |
| `UVCW_UV` | `--uv` |
| `UVCW_TIMEOUT` | `--timeout` (seconds, default 900) |
| `UVCW_MAX_SIZE` | `gc --max-size` |
| `UVCW_MIN_FREE` | `gc --min-free` |
| `UVCW_MIN_AGE` | `gc --min-age` |
| `UVCW_KEEP` | `gc --keep`, comma-separated |

Package names use uv's normalization: case is ignored and runs of `.`, `_` and `-` are equivalent. For example, `--keep zope.interface` also protects `zope-interface` from package cleaning. `--timeout` and `UVCW_TIMEOUT` apply to every command, including `doctor`, and must be finite positive seconds within the platform's timeout limit. Cancelling a run stops and reaps its uv child before returning; it cannot undo cache changes uv already made.

Sizes accept `10GB`, `4GiB`, `500MiB` or a bare byte count; decimal units are powers of 1000 and binary units powers of 1024. Durations accept `s`, `m`, `h`, `d`, `w`.

Exit codes: `0` success, `1` uvcw could not do its job (no `uv`, no cache, bad arguments), `2` still over budget and `--fail-over` was given. A package uv refuses to clean is logged, skipped and named in the summary; it does not change the exit code, so use `--fail-over` if you need CI to notice.

The cache directory is found by running `uv cache dir`. An ordinary command failure falls back to `$UV_CACHE_DIR`, `$XDG_CACHE_HOME/uv`, `~/.cache/uv` and `%LOCALAPPDATA%\uv\cache`, in that order. A timeout or missing executable stops the command instead. Initial discovery refuses a directory containing no recognisable uv bucket, your home directory and a filesystem root. After uv mutates an already-validated cache, refreshing its inventory permits no remaining buckets; any remaining files still count toward the budget.

## How the numbers work

`uv cache size` delegates to [`diskus`](https://github.com/sharkdp/diskus), so uvcw reproduces diskus exactly and the two totals agree byte for byte:

| | file bytes | directories | hardlink dedupe |
|---|---|---|---|
| Unix | `st_blocks * 512` | counted | yes, by `(dev, ino)` when `nlink > 1` |
| Windows | `st_size` | counted, at their NTFS index allocation | none, [by design](https://github.com/sharkdp/diskus/issues/32) |

The cache directory's own entry counts too, which on NTFS is zero until the directory outgrows its resident index and then jumps to 4 KiB or more.

That number, `disk_bytes`, is what the budget is measured against, because it is the number the user sees. Two others appear in `--json`:

- `physical_bytes`: inode dedupe on *both* platforms. uv 0.12.7 added the `content-addressed-cache` preview feature, which hardlinks identical files within and across cached wheels, so on Windows this can sit well below `disk_bytes`.
- `reclaim_bytes`: uv's own estimate of what removing an entry frees, from `crates/uv-cache/src/removal.rs`: `blocks * 512` when `nlink == 1` and zero otherwise on Unix, `len()` on Windows.

Package attribution comes from uv's own layout. `wheels-*/<kind>/<package>/`, `sdists-*/<kind>/<package>/<version>/` and `simple-*/<kind>/<package>.rkyv` name the package directly. Unpacked wheels sit under `archive-*/<hash>/` with no name, reached by a link from the wheels bucket: a symlink on Unix, and on Windows a small regular file whose content is literally `archive-v0/<id>`. uvcw resolves both, and ignores a link naming a stale archive version, exactly as uv's own `resolve_link` does.

## Limitations

- **Source distributions from a URL, a local path or Git are not attributed.** uv identifies those by reading a msgpack blob inside the entry, and uvcw is stdlib-only. Their bytes land in `unattributed`, which only `uv cache prune` can reclaim. Wheels from an alternate index *are* attributed, because the index layout still puts the package name in the path.
- **An archive referenced by two packages is charged to neither.** `uv cache clean <pkg>` only frees it once every referrer is gone, so attributing it to one of them would promise bytes uvcw cannot deliver.
- **Managed Python interpreters (`python-v0`) and the vulnerability database (`osv-v0`) are never evicted.** They count toward the total and are reported separately as `protected`.
- **Recency ignores the simple-index bucket.** `uv lock` revalidates `simple-*` metadata for every package it resolves, so those files are freshly written even for a package nobody has installed in months. Their bytes count toward a package's size; their timestamps do not move its clock.
- **LRU degrades to write order where access times are not tracked.** Windows disables NTFS last-access updates by default and Linux mounts are commonly `noatime`; `uvcw doctor` and `uvcw gc` both say so when it applies. `last_used` is `max(atime, mtime)`, so it degrades to first-write rather than to nonsense.
- **Strict LRU can evict a lot to reach a little.** Eviction is oldest-first by design, so if the one package standing between you and the budget is also the newest, everything older goes first. The header says how many of your packages that is, and `--dry-run` shows the list before anything happens.
- **Projection is not measurement.** Evicting a package whose bytes are hardlinked to bytes that survive frees less than expected, so `gc` re-measures with `uv cache size` and runs the analysis again (up to three rounds) if it is still over. The printed `reclaimed` figure is always measured, never projected.
- **`--keep` and `--min-age` protect package cleaning, not native pruning.** uv may remove obsolete buckets, unused archives, cached environments and temporary build environments even when a related package is kept. Cached environments are recreated as needed. Use `--no-prune` to skip both native pruning stages.
- **Cleaning can require downloads and rebuilds later.** Native pruning preserves referenced wheel artifacts but removes cached environments. Package cleaning removes all cached versions of that package. Hardlinked installations can retain physical disk blocks after cache entries disappear, so a cache-size reduction is not a guarantee of equal filesystem free space. Symlink-mode environments have uv's native cache-removal limitations.
- **On a uv too old for `uv cache size`, totals come from our own walk** and there is nothing to cross-check them against. `doctor` warns when that happens.
- No daemon, no config file, no telemetry.

## Development

```console
$ git clone https://github.com/Booyaka101/uv-cache-warden
$ cd uv-cache-warden
$ python -m pip install -e ".[dev]"
$ python -m pytest -q                 # unit and Action-output tests, no network
$ python -m pytest -q -m integration  # uv 0.13.1 on PATH and PyPI access
$ python -m build
$ python -m twine check dist/*
```

The unit tests build synthetic cache trees matching `crates/uv-cache/src/lib.rs` and use a recording fake in place of the uv wrapper. The fake lives in `tests/conftest.py` and nothing under `src/` imports it.

Integration checks install tiny real packages into isolated caches, exercise native obsolete-bucket pruning, preserve packages for offline reinstall, and run the composite Action's actual shell steps on POSIX. Subprocess failures fail tests rather than turning them into skips. CI covers Linux, macOS and Windows; Windows skips the POSIX-only Action-shell and symlink-validation cases.

Releases go through a GitHub PR and require owner approval and green CI on the exact release commit. Publish the PyPI artifacts and verify registry availability before creating the versioned release and updating the floating Action `v1` tag.

[`docs/upstream-notes.md`](docs/upstream-notes.md) records every uv internal this tool depends on, with the verbatim source and a link, so it can be rechecked when uv changes.

## License

MIT.
