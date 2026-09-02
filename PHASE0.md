# Phase 0 — resource verification

Every claim the brief depends on was fetched and checked before any code was written.
Verified 2026-09-02 against `uv 0.12.9 (9f9286029 2026-09-01 x86_64-pc-windows-msvc)`.

## 1. `uv` has no size or age flag — https://docs.astral.sh/uv/reference/cli/

`uv cache clean`, `uv cache prune`, `uv cache size` and `uv cache dir` each accept only the
global option set (`--cache-dir`, `--color`, `--config-file`, `--directory`, `--offline`,
`--project`, `--quiet`, `--verbose`, `--no-cache`, `--no-config`, `--no-progress`,
`--managed-python`, `--no-managed-python`, `--no-python-downloads`, `--allow-insecure-host`,
`--system-certs`, `--help`). **No size limit and no age argument on any of them.** The gap is real.

Cache directory default, quoted:

> Defaults to `$XDG_CACHE_HOME/uv` or `$HOME/.cache/uv` on macOS and Linux, and
> `%LOCALAPPDATA%\uv\cache` on Windows.

## 2. Argument definitions — crates/uv-cli/src/lib.rs

```rust
pub struct CleanArgs {
    /// The packages to remove from the cache.
    pub package: Vec<PackageName>,
    /// Force removal of the cache, ignoring in-use checks.
    /// By default, `uv cache clean` will block until no process is reading the cache.
    pub force: bool,
}

pub struct PruneArgs { pub ci: bool, pub force: bool }

pub enum CacheSizeOutputFormat {
    Auto,     // Display a human-readable size in terminals and raw bytes otherwise.
    Human,    // Display the cache size in a human-readable format.
    Machine,  // Display the cache size in raw bytes.
}

pub struct SizeArgs {
    pub output_format: CacheSizeOutputFormat,  // --output-format
    pub human: bool,                           // --human / -H
}
```

Confirmed live: `uv cache clean --help` shows `[PACKAGE]...` and `--force`.
`uv cache size --output-format machine` prints a bare integer on stdout.

## 3. Bucket names — crates/uv-cache/src/lib.rs `CacheBucket::to_str`

```rust
Self::SourceDistributions => "sdists-v9",
Self::FlatIndex           => "flat-index-v4",
Self::Git                 => "git-v0",
Self::Interpreter         => "interpreter-v4",
Self::Simple              => "simple-v24",
Self::Wheels              => "wheels-v6",
Self::Archive             => "archive-v0",
Self::Files               => "files-v0",
Self::Builds              => "builds-v0",
Self::Environments        => "environments-v2",
Self::Python              => "python-v0",
Self::Binaries            => "binaries-v0",
Self::Osv                 => "osv-v0",
```

`pub const ARCHIVE_VERSION: u8 = 0;` (line 36).

Package indexing, quoted from `CacheBucket::remove`:

> For `pypi` wheels, we expect a directory per package (indexed by name).
> For alternate indices, we expect a directory for every index (under an `index` subdirectory),
> followed by a directory per package (indexed by name).
> For direct URLs, we expect a directory for every URL, followed by a directory per package.

`WheelCacheKind` serialises to `pypi`, `index`, `url`, `path`, `editable`, `git`
(crates/uv-cache/src/wheel.rs).

Prune, line 809: `// Fourth, remove any unused archives (by searching for archives that are not symlinked).`

## 4. The archive link is NOT a symlink on Windows

This is the one place the brief's model was incomplete, and the source settles it
(crates/uv-cache/src/lib.rs, `create_link` / `resolve_link`):

```rust
/// On Windows, we write structured data ([`Link`]) to a file containing the archive ID and
/// version. On Unix, we create a symlink to the target directory.
```

```rust
impl Display for Link {
    fn fmt(&self, f: &mut Formatter<'_>) -> std::fmt::Result {
        write!(f, "archive-v{}/{}", self.version, self.id)
    }
}
```

Confirmed on disk here — `wheels-v6/pypi/rich/13.7.1-py3-none-any` is a 27-byte **regular file**
(`os.readlink` raises `WinError 4390: not a reparse point`) whose entire content is:

```
archive-v0/NoF3pkNqtljOzBKI
```

`attribute.py` therefore resolves both shapes.

## 5. Sizing rule — crates/uv-cache/src/removal.rs

```rust
/// Estimate the storage reclaimed by removing a non-directory entry.
#[cfg(unix)]
fn file_size(metadata: &Metadata) -> u64 {
    if metadata.nlink() == 1 { metadata.blocks().saturating_mul(512) } else { 0 }
}

#[cfg(not(unix))]
fn file_size(metadata: &Metadata) -> u64 { metadata.len() }
```

`add_file` additionally calls `uv_fs::physical_space(path, metadata)` for the fine-grained
count, enabled by the `cache-physical-space` preview feature.

## 6. What `uv cache size` actually measures

The brief asked us to reconcile against `uv cache size`; the number only reconciles if you know
how it is produced. `crates/uv/src/commands/cache_size.rs` delegates to `diskus 0.9.0`:

```rust
let disk_usage = DiskUsage::new(vec![cache.root().to_path_buf()]);
let total_bytes = disk_usage.count_ignoring_errors();
```

diskus defaults (`CountType::DiskUsage`, `Directories::Auto`) mean:

| | file bytes | directories | hardlink dedupe |
|---|---|---|---|
| Unix | `st_blocks * 512` | counted (`st_blocks * 512`) | yes, by `(dev, ino)`, only when `nlink > 1` |
| Windows | `metadata.len()` | counted, contribute 0 | **none** ([diskus#32](https://github.com/sharkdp/diskus/issues/32)) |

`sizing.py` reproduces this exactly as `disk_bytes`, which is the metric `uvcw gc` budgets
against. It separately reports `physical_bytes` (inode dedupe on *both* platforms, the brief's
mandatory rule) and `reclaim_bytes` (uv's own `file_size` above).

## 7. Safety contract — https://docs.astral.sh/uv/concepts/cache/

> It's never safe to modify the cache directly (e.g., by removing a file or directory).
> `uv cache prune` removes all unused cache entries and all centralized project environments.
> `uv cache clean ruff` removes all cache entries for the `ruff` package.
> Enable the `cache-physical-space` preview feature for a more accurate estimate that accounts
> for hardlinks and copy-on-write clones.

uv-cache-warden never unlinks anything. Every deletion is a `uv cache clean <pkg>` or a single
`uv cache prune`, and `--force` is never passed.

## 8. Demand — astral-sh/uv#5731

`gh api repos/astral-sh/uv/issues/5731`:

```json
{"state":"open","created_at":"2024-08-02T16:53:19Z","comments":31,"reactions":53,
 "labels":["enhancement","help wanted","cache","needs-design"],"title":"uv cache size limit"}
```

@charlesnicholson:

> We have a script in our project root that runs the git-sandboxed uv for us, so I'm just going
> to throw in a bit that looks at the cache size and prunes it if it's over 800MB or whatever.

> Being able to say "The cache has a maximum size of 4GB" and an LRU scheme that evicted cold
> entries would work very well for us.

@jroesch:

> we are using `uv` across many projects at NVIDIA. For one of projects with Python + native
> bits, we often use `uv sync` and one my colleagues has encountered the cache now routinely
> exceeding 1TB+.

## 9. Prune alone cannot reclaim orphaned tool deps — astral-sh/uv#16551

`gh api repos/astral-sh/uv/issues/16551`: `state: closed`, `state_reason: not_planned`,
opened 2025-11-02. Body:

> `uv tool install openai-whisper` [...] brings in several GBs of CUDA even on non-nvidia
> hardware. Once done with it, I uninstalled the tool. However there seems to be no way to get
> rid of the nvidia stuff from the cache with `uv cache prune`. The detection logic says that
> there is nothing "unused".

## 10. Cost model

Zero cost. `uv` is free and was installed here from PyPI into a scratch venv. PyPI publishing and
GitHub Actions are free. No account was created and no payment details exist anywhere in this
build. `pypi.org/pypi/uv-cache-warden/json` returns **404**, so the name is available.

## 11. LESSONS.md

`C:\Users\cbosc\claude-phone\ideas\LESSONS.md` read. Nothing in it contradicts this brief. The
relevant entry is 2026-08-11: this PC's bare `python` is 3.11.9, so anything needing a newer
interpreter runs through `py -3.12`. Applied — the dev venv is 3.12.
