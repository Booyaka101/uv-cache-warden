# PROGRESS

**Status: v1.0.0 complete and verified locally. Not published, publishing is the owner's step.**

Last updated 2026-09-02.

## What is VERIFIED working

Verified on Windows 11, `uv 0.12.9`, Python 3.12.10, against real uv caches built by installing
real packages from PyPI. Every number below came out of an actual run, not a test double.

| Check | Result |
|---|---|
| `uvcw doctor` | prints cache dir, uv version, bucket inventory, reconciliation, hardlink stats, atime policy. Exit 0. |
| `uvcw report --json` reconciles with `uv cache size` | 89,698,856 vs 89,702,952 bytes = **0.0046%**, well inside 2% |
| Byte-exact reconciliation on a static cache | ours 7,344,134 vs `uv cache size` 7,344,134 = **0 bytes** |
| `uvcw gc --dry-run` names packages, mutates nothing | named `numpy` and `pandas`, `uv cache size` unchanged at 89,702,952 |
| `uvcw gc` brings the cache under budget | 89,702,952 -> 10,342,604 against a 44,702,952 budget. Exit 0. |
| Bad input, missing cache, no uv, bad size literal | each a one-line message on stderr and exit 1, no traceback |
| `--min-age` + `--fail-over` when unreachable | exits 2, explains min-age and unattributed bytes |
| Already under budget | no-op, exit 0 |
| Unit tests | **114 passed**, 0 failed, no network |
| Integration tests | **2 passed** against real uv + PyPI |
| Wheel installs from a clean venv | `uv-cache-warden==1.0.0`, zero dependencies, `uvcw --version` works |
| `twine check` | PASSED on wheel and sdist |

Reproduce: `python -m pytest -q` then `python -m pytest -q -m integration`.
The CI workflow runs the same acceptance checks on Linux, macOS and Windows across
Python 3.9-3.13.

## Owner steps remaining (all require accounts or pushes, so none were done here)

1. **Create the GitHub repo** and push. The local repo is initialised with one commit on `main`.
   `git remote add origin git@github.com:Booyaka101/uv-cache-warden.git && git push -u origin main`.
2. **Configure PyPI Trusted Publishing** for project `uv-cache-warden`, workflow `publish.yml`,
   environment `pypi`. `.github/workflows/publish.yml` is already written for it and checks that
   the tag matches `__version__` before building.
3. **Tag and release**: `git tag v1.0.0 && git push --tags`. Only after CI is green on that exact
   commit, per the house rule.
4. **List the Action on the Marketplace.** Marketplace categories are chosen in the release UI,
   not in `action.yml`; pick the `uv` and `cache` tags there. Also push a floating `v1` tag so the
   README's `Booyaka101/uv-cache-warden@v1` resolves.
5. **Post to [astral-sh/uv#5731](https://github.com/astral-sh/uv/issues/5731).** That is the single
   best first distribution step: an open `help wanted` issue with 53 reactions where several
   commenters describe writing this exact workaround by hand.

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

- **Clone check.** `difflib.SequenceMatcher` over the line lists of all 67 functions in `src/`.
  The first pass found `attribute._walk_entries` and `sizing.walk` at 45%, two directory
  walkers. Below the 60% threshold, but it was real duplication, so the shared mechanism was
  extracted into `sizing.scandir_tree(root, prune=...)` and both callers now use it. The highest
  remaining pair is 46%, two three-line `as_json` dataclass serialisers with different fields;
  a shared base class there would add indirection for six lines, so that is where it stops.
  Next is `parse_size` vs `parse_duration` at 34%, kept apart deliberately: merging them would
  make their error messages generic, and those messages are user-facing.
- **Dead code.** `pyflakes` clean. `Plan.reachable` was written and never used, so it is gone.
  `discovery.NAME_INDEXED` existed as a constant while `report.scan` inlined the same tuple; the
  constant is now used. `format_size` had an unreachable `raise AssertionError` after its loop
  and was rewritten so the branch does not exist.
- **Comments.** Only where a constraint is non-obvious: why the stderr pump is not
  `communicate()`, why directories count on Windows, why a package is recorded as evicted before
  the call, why the fake never ships. No rationale essays.
- **Prose.** No em dashes in README, CHANGELOG or the commit message. No numbered scaffolding in
  the README body.
- **No TODO/FIXME/placeholder anywhere.** Checked by grep across `src`, `tests`, `action.yml`,
  the workflows and the docs.

### (f) Features that would make this best in class

Built, because they were clearly right and cheap:

- `report --sort age`: the coldest-first order is exactly what `gc` will evict, so it previews a
  budget without running one.
- `--json-file PATH` on every command.
- Prune even when nothing is evictable, which is the only way to reclaim the orphaned bytes in
  astral-sh/uv#16551.
- Multi-round gc. A single projection pass silently under-evicts when a package's bytes are
  hardlinked to bytes that survive; `gc` now re-measures and runs again, bounded at 3 rounds.

Not built, listed as concrete next steps:

- **`--min-free SIZE`**: evict until the *filesystem* has N bytes free rather than until the
  cache is under N. `shutil.disk_usage` makes it trivial; the work is deciding how it interacts
  with `--max-size` and `--fail-over`, which is a design call, not a coding one. This is the
  single most requested shape in #5731 after a plain size cap.
- **Attribute source distributions from URL, path and Git.** uv identifies these by reading
  `metadata.msgpack`. A ~120-line msgpack reader for the subset uv writes would keep the
  stdlib-only promise and move those bytes out of `unattributed`.
- **`uvcw gc --keep-project`**: read `uv.lock` / `pyproject.toml` in the working directory and
  keep everything the current project depends on. Removes the main reason to type `--keep`.
- **A `--since` filter on report**, to show only what has been added since a previous JSON dump.
  Useful for finding which CI job is filling a shared runner cache.
- **`osv-v0` and `python-v0` opt-in eviction.** Deliberately out of v1 scope, but a self-hosted
  runner with ten managed interpreters may well want `--include-python`.
- **Package name normalisation across index kinds.** uv writes normalised names, so this has not
  bitten, but a package appearing under both `pypi` and an alternate index is currently counted
  once per path and merged by exact name only.

## Where the shipped code came from

`PHASE0.md` records every upstream fact with its quote and URL: the CLI option lists, the
`CacheBucket::to_str` names, `ARCHIVE_VERSION`, `create_link`/`resolve_link`, the `file_size`
rule in `removal.rs`, the `diskus` call in `cache_size.rs`, and the two GitHub issues with their
live API counts. Recheck that file first when uv changes.
