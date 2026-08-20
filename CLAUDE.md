# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**SimsLink** — a desktop mod manager for The Sims 4 on Linux, written in Python with a Flet UI and a local SQLite database. There is no official CurseForge client for Linux; this project fills that gap. Its main differentiator is automated crash diagnosis: parsing the game's `lastException.txt` to identify which installed mod likely caused a crash.

Reference project (not a dependency, just prior art worth checking for ideas): [SimsForge](https://github.com/Teyk0o/simsforge) — different stack (TS/Express/Prisma), notable for its filesystem import flow and malicious-mod flagging.

## Language convention — read this first

- **All code, comments, docstrings, commit messages, and the README are written in English.** No exceptions.
- **Only the UI-facing strings are localized** (French and English, see `i18n/`). Never hardcode UI text in Python files — always go through the i18n layer.
- Any French terms appearing in project notes or specs are for human discussion only and must never leak into source code.

## Tech stack

- Python 3.11+
- Flet (desktop UI)
- SQLite (local DB, no server component)
- `watchdog` for filesystem monitoring (mod folder + download folder)
- `concurrent.futures` / `multiprocessing` for parallelized hashing during full scans

## Critical game constraints — never violate these

The Sims 4 loads mods from `Documents/Electronic Arts/The Sims 4/Mods/` with two different depth rules depending on file type. Getting this wrong silently breaks mods for the user with no error message, so it must be enforced in code, not just documented:

- **`.package` files**: loaded via full recursive scan — any depth under `Mods/` works.
- **`.ts4script` files**: loaded only at the root of `Mods/` **or exactly one level deep**. Anything nested further is silently ignored by the game.
- **Resulting install rule**: every mod gets its own top-level subfolder under `Mods/<mod_id>/`. Never install directly at the `Mods/` root (name collisions across mods), never nest deeper than one level (breaks `.ts4script` loading).
- `Mods/` itself stays flat. Rich organization (categories, browsing) lives in the library folder, not in the game folder.

## Architecture

```
config.py            # .env loading
db.py                 # SQLite schema + migrations
curseforge.py         # CurseForge API client — Direct Mode only, requires CURSEFORGE_API_KEY
download_watcher.py   # watches the download folder — Assisted Mode install/update path
mod_manager.py         # install / enable / disable / delete / update pipeline
dependencies.py        # dependency graph resolution, incl. translation-mod detection
package_parser.py      # DBPF (.package) header reader — resource listing, STBL comparison
scanner.py              # incremental scan (metadata) + on-demand full scan (hashing)
crash_analyzer.py       # lastException.txt parsing + automated bisection
i18n/                   # fr.json, en.json — UI strings only
ui/
  library.py
  catalog.py
  updates.py
  crash_mode.py
  settings.py
```

### Library + symlink model

- **Library**: the real storage location for installed mods (flat structure — no need for multi-level categorization here either; that idea was explicitly dropped).
- **`Mods/<mod_id>/`**: a symlink (or a copy, as a fallback if the filesystem doesn't support symlinks) pointing into the library.
- Enabling/disabling a mod = creating/removing the symlink. Fast, reversible, no file movement.

## Environment variables (`.env`)

```
SIMS4_GAME_DIR=...        # game install directory
SIMS4_MODS_DIR=...        # .../Documents/Electronic Arts/The Sims 4/Mods
SIMS4_USER_DIR=...        # .../Documents/Electronic Arts/The Sims 4
LIBRARY_DIR=...           # app-managed mod library
CURSEFORGE_API_KEY=...    # optional — see Direct Mode / Assisted Mode below
DOWNLOAD_WATCH_DIR=...    # watched download folder for Assisted Mode, default ~/Downloads
GAME_VERSION=...          # auto-detected if possible, otherwise manual
```

## Direct Mode vs Assisted Mode

This is the most important behavioral fork in the whole app, and it's not a future nice-to-have — it's how the app must be built from day one, since API key approval isn't a prerequisite for starting development.

A persistent banner always shows the active mode:

| Mode | Condition | Banner text |
|---|---|---|
| Direct Mode | `CURSEFORGE_API_KEY` set and valid | "Direct Mode — connected to CurseForge" |
| Assisted Mode | No key, or invalid/expired key | "Assisted Mode — browser-based install" |

**Direct Mode**: full catalog browsing, compatibility badges, metadata (description/screenshots) from the API, automatic update detection via `curseforge.py`.

**Assisted Mode** (must work with zero API access):
1. User clicks through to a mod's CurseForge page (`webbrowser.open()`) — ordinary human browsing, not automated access, so no ToS concerns.
2. `download_watcher.py` watches `DOWNLOAD_WATCH_DIR` and detects new `.zip`/`.package`/`.ts4script` files.
3. User confirms the detected file is the intended mod.
4. File goes through the standard install pipeline (extraction, `.ts4script` depth check, placement under `Mods/<mod_id>/`, DB entry) — identical to Direct Mode from this point on.
5. The mod's CurseForge URL is still stored locally for later manual re-checking.
6. Updates work the same way: no automatic version comparison is possible, so the Updates view becomes a manual checklist with "Check on CurseForge" links; a new download for an already-installed mod is detected and offered as a replacement (with backup) via the same watcher.

**What's unavailable in Assisted Mode** (must be clearly surfaced in the UI, never silently degraded): automatic "update available" badges, changelogs, pre-download compatibility checks against `game_version_min/max`.

**Fully mode-independent**: library, symlinks, local dependency tracking, Crash Mode, cache cleanup, enable/disable/delete, incremental scanning.

Do not gate core functionality behind having an API key. `curseforge.py` should be the only module that hard-requires `CURSEFORGE_API_KEY`.

## Startup scan — must never block the UI

A full recursive hash scan on every launch is a real cost on large libraries (real-world example: 20k+ `.package` files). Implement accordingly:

- On launch, render the library immediately from DB state — no blocking "scanning..." screen.
- Incremental scan runs in the background: compare size + `mtime` per file against `mod_files`; only changed/new files get a full hash. Unchanged mod folders are skipped entirely.
- A `watchdog`-based real-time watcher on `Mods/` catches external changes while the app is running, reducing the next startup scan to just catching up on changes made while the app was closed.
- Full hash scan is manual-only (Settings → "Full scan"), parallelized across cores since hashing is CPU-bound.

## Database schema (SQLite)

```sql
mods (
  id, curseforge_id, name, author, category,
  library_path, primary_type,          -- 'package' | 'script' | 'mixed'
  installed_version, latest_version,
  game_version_min, game_version_max,
  compat_status,                       -- 'compatible' | 'incompatible' | 'unknown'
  third_party_distribution_allowed,    -- bool, from CurseForge API
  active, install_date, update_date,
  thumbnail_url, thumbnail_local,
  short_description, full_description,
  screenshots JSON,
  links JSON                           -- curseforge_url, author_site, donation, etc.
)

mod_files (
  id, mod_id, relative_path, hash, extension
)

dependencies (
  id, mod_id, depends_on_curseforge_id,
  dependency_type,     -- 'required' | 'optional' | 'translation'
  confidence,           -- 'confirmed' | 'suggested' (for auto-detected translations)
  mandatory
)

profiles (id, name)
profile_mods (profile_id, mod_id)

crash_log (
  id, date,
  raw_last_exception TEXT,
  auto_suspect_mods JSON,      -- mods identified via traceback parsing
  active_mods_snapshot JSON,
  bisection_in_progress BOOLEAN,
  bisection_history JSON,      -- tested batches + results
  confirmed_faulty_mod_id,
  user_note
)
```

## Key features and how they should behave

### Crash Mode
- Parse `lastException.txt` only. **Never parse `lastCrash.txt`** — it's confirmed unreadable/unusable even by the community, don't waste effort on it.
- Cross-reference traceback `File "..."` lines against `mod_files` paths under `Mods/` to identify suspect mods directly.
- Fall back to regex pattern matching for known error signatures (e.g. broken imports of outdated shared libraries like sims4communitylib/ts4lib) when no mod path appears directly in the trace.
- **Never auto-suggest deletion from a single occurrence** — an isolated LastException can be benign and unrelated to any mod. Surface suspects with confidence level, let the user decide.
- If no clear suspect: offer automated bisection (binary search via symlink toggling, batch-based, user confirms after each relaunch, logarithmic convergence). Never require full file moves — always via the symlink layer.

### Cache cleanup
Target these specifically, with the reasoning baked into any confirmation dialog:
- `localthumbcache.package` — delete the file (regenerates; stale entries after mod changes can cause invalid lookups and crashes)
- `cache/` — clear contents, keep the folder and any `FileCache.cfg`/`.ini`
- `cachestr/` — clear contents, keep the folder
- `cachewebkit/` — delete if present (only exists while game is running)
- `onlinethumbnailcache/` — delete if present
- `localsimtexturecache.package` — delete if present
- **Never touch**: saves, tray files, screenshots, `options.ini`, `resource.cfg`.
- **Never include** `lastException.txt`/`lastCrash.txt` in this cleanup — they're managed separately by Crash Mode so diagnostic history isn't lost before analysis.
- Suggest cleanup automatically after install/update/disable actions, but always require confirmation before deleting.

### Translation-mod detection
No dedicated "translation" relation type exists in the CurseForge API (only `embeddedLibrary` / `incompatible` / `optionalDependency` / `requiredDependency`), so this always needs multiple weak signals combined, never a single automatic match:
1. Parse mod description for translation keywords (multi-language) + CurseForge URLs pointing at an already-installed mod — strongest signal.
2. Name/slug heuristics (`[FR]`, `- French Translation`, `_VF`) — weak, pre-filter only.
3. `.package` binary analysis via `package_parser.py`: check the file is STBL-only (no `.ts4script`, unusually small), compare STBL Group ID/Instance ID against the candidate source mod — strong confirmation, but on-demand only, never a full-library scan.
- **Every suggested link requires user confirmation.** Store `confidence` as `confirmed` or `suggested` — never silently create a `translation` dependency.

### Dependency resolution
Track `required` / `optional` / `translation` dependency types. Block install/enable only on unresolved `required` dependencies; warn but don't block on `optional`.

## Things to never do

- Never install a mod directly at the `Mods/` root, and never nest a `.ts4script` more than one folder deep.
- Never make an automated request to any CurseForge endpoint without a valid API key — this includes CDN downloads, which require a key as of July 16, 2026.
- Never scrape CurseForge pages. It's explicitly prohibited by the platform's Terms of Use and won't even solve the download problem once the CDN requires a key.
- Never silently auto-delete a mod based on crash analysis. Suspicion is not confirmation.
- Never hardcode a UI string outside the `i18n/` layer.
- Never block the UI thread with a full filesystem hash scan.
- Never gate a mode-independent feature (library, Crash Mode, cache cleanup, etc.) behind `CURSEFORGE_API_KEY` being set.

## Testing

Unit tests and regression tests are mandatory for this project, not optional. Every module listed under Architecture should have corresponding tests, and every bug fix should come with a regression test that would have caught it.

- Framework: `pytest`.
- Test files mirror the module structure: `tests/test_<module>.py` for each file under the project root (e.g. `tests/test_mod_manager.py`, `tests/test_scanner.py`).
- **Priority modules for thorough coverage** — these encode the game's undocumented behavior and are the easiest place for silent regressions to slip in:
  - `mod_manager.py`: the `.ts4script` depth rule and the `Mods/<mod_id>/` placement rule must have explicit tests that assert installation fails/corrects itself when a script mod would land deeper than one level, and that `.package` files at any depth are accepted.
  - `scanner.py`: incremental scan must have a test proving an unchanged mod folder triggers zero hash computation, and a test proving a changed `mtime`/size does trigger one.
  - `crash_analyzer.py`: traceback parsing needs fixture `lastException.txt` samples (both "mod path directly in trace" and "core game code only" cases) with known expected suspect output. Also test that a single occurrence never produces an auto-delete suggestion.
  - `dependencies.py`: translation-detection heuristics need fixtures per signal type (description match, name heuristic, STBL comparison) with assertions on the resulting `confidence` value — a `suggested` link must never silently become `confirmed` without explicit user action in the code path.
  - `package_parser.py`: DBPF header parsing needs at least one real `.package` fixture (a small STBL-only file and a mixed-resource file) to catch parser regressions against the binary format.
- **Mode-dependent code** (`curseforge.py` vs `download_watcher.py`): tests must run without a real API key or network access — mock the CurseForge API client entirely, and mock filesystem events for the download watcher (no real file I/O against a live Downloads folder in CI).
- **Regression tests**: whenever a bug is fixed, add a test reproducing the original failure before the fix, named to reference the issue (e.g. `test_regression_ts4script_nested_two_levels`).
- Run the full suite before considering any feature "done" — this applies to Claude Code's own workflow in this repo, not just to human review.

## Development workflow

Once the above testing expectations are met, follow the collaboration pattern this project already uses: freeze acceptance criteria before implementing, change one variable at a time when debugging platform-specific behavior (symlinks, filesystem quirks), and treat a passing test suite as the GO signal before moving to the next feature — not as an afterthought once everything "looks done."

## Current project status

- No CurseForge API key yet — build and test everything through Assisted Mode first (`download_watcher.py` + manual install/update flow). `curseforge.py` is the only module blocked on key approval.
- Full technical brief with rationale and research notes: see `brief-sims4-mod-manager.md` in the project root for the complete spec this file summarizes.
