# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

**SimsLink** — a desktop mod manager for The Sims 4 on Linux: a FastAPI backend serving a local REST API, an HTML/CSS/JS frontend, both wrapped in a native window via pywebview, with a local SQLite database. There is no official CurseForge client for Linux; this project fills that gap. Its main differentiator is automated crash diagnosis: parsing the game's `lastException.txt` to identify which installed mod likely caused a crash.

Reference project (not a dependency, just prior art worth checking for ideas): [SimsForge](https://github.com/Teyk0o/simsforge) — different stack (TS/Express/Prisma), notable for its filesystem import flow and malicious-mod flagging.

## Language convention — read this first

- **All code, comments, docstrings, commit messages, and the README are written in English.** No exceptions.
- **Only the UI-facing strings are localized** (French and English, see `frontend/i18n/`). Never hardcode UI text — always go through the i18n layer (loaded client-side and injected into the DOM).
- Any French terms appearing in project notes or specs are for human discussion only and must never leak into source code.

## Tech stack

- Python 3.11+
- FastAPI — local REST API (`localhost:8000`) hosting all business logic (DB, scanner, mod_manager, crash_analyzer, package_parser, curseforge client).
- pywebview — wraps the frontend in a real native desktop window, not a browser tab. Linux system deps: `python3-gi` + `gir1.2-webkit2-4.0` (WebKitGTK).
- Frontend: HTML/CSS/JS (vanilla by default; React only if a view's component complexity actually justifies it) — talks to the backend via `fetch`.
- SQLite (local DB, no server component)
- `watchdog` for filesystem monitoring (mod folder + download folder)
- `concurrent.futures` / `multiprocessing` for parallelized hashing during full scans

Single language (Python) for all business logic — no multi-language split like a Tauri/Rust stack would require. Backend and frontend are two processes/layers within the same app, not a client/server product.

## Critical game constraints — never violate these

The Sims 4 loads mods from `Documents/Electronic Arts/The Sims 4/Mods/` with two different depth rules depending on file type. Getting this wrong silently breaks mods for the user with no error message, so it must be enforced in code, not just documented:

- **`.package` files**: loaded via full recursive scan — any depth under `Mods/` works.
- **`.ts4script` files**: loaded only at the root of `Mods/` **or exactly one level deep**. Anything nested further is silently ignored by the game.
- **Resulting install rule**: every mod gets its own top-level subfolder under `Mods/<mod_id>/`. Never install directly at the `Mods/` root (name collisions across mods), never nest deeper than one level (breaks `.ts4script` loading).
- `Mods/` itself stays flat. Rich organization (categories, browsing) lives in the library folder, not in the game folder.

## Architecture

```
backend/
  main.py              # FastAPI app + routes — thin HTTP layer over the modules below
  config.py             # .env loading
  db.py                 # SQLite schema + migrations
  curseforge.py         # CurseForge API client — Direct Mode only, requires CURSEFORGE_API_KEY
  download_watcher.py   # watches the download folder — Assisted Mode install/update path
  mod_manager.py         # install / enable / disable / delete / update pipeline
  dependencies.py         # dependency graph resolution, incl. translation-mod detection
  package_parser.py       # DBPF (.package) header reader — resource listing, STBL comparison
  scanner.py               # incremental scan (metadata) + on-demand full scan (hashing)
  crash_analyzer.py        # lastException.txt parsing + automated bisection
  cache_cleaner.py         # cache cleanup targets
  conflict_detector.py     # duplicate/conflict detection across installed mods
  game_options.py          # reads options.ini for settings SimsLink checks (Script Mods Allowed)
  profiles.py              # named, switchable sets of "which mods should be active"
  blacklist.py             # local, user-editable list of known-bad mod name/id patterns
  logging_config.py        # configures the "backend" logger — console + file, level from LOG_LEVEL
frontend/
  index.html
  style.css
  app.js               # or src/ if a view's complexity earns it a React tree
  i18n/
    fr.json
    en.json            # UI strings only, loaded and injected client-side
desktop.py              # launches FastAPI (background thread) + opens the pywebview window
```

`backend/*.py` (everything except `main.py`) is plain business logic, importable and testable on its own — `main.py` exposes it over HTTP, and the frontend calls those routes. Cross-module imports within `backend/` use relative imports (`from . import mod_manager`, `from .config import Config`, ...), since they're all part of the same package. When adding a feature, business logic goes in the relevant `backend/` module; `main.py` stays a thin routing layer over it, not a place to grow feature logic of its own.

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
BACKUP_RETENTION_COUNT=... # optional, default 5 — backups kept per mod under LIBRARY_DIR/.backups/
MODS_WATCHER_ENABLED=...   # optional, default true — real-time watcher for external changes under Mods/
LOG_LEVEL=...              # optional, default INFO — DEBUG/INFO/WARNING/ERROR/CRITICAL
```

## Direct Mode vs Assisted Mode

This is the most important behavioral fork in the whole app, and it's not a future nice-to-have — it's how the app must be built from day one, since API key approval isn't a prerequisite for starting development.

A persistent pill (topbar, `#modePill`) always shows the active mode — a short label plus a green/red indicator gem (`.gem`, default `--accent` green, `.warn` class switches it to `--warn` red-orange), with the fuller sentence as its hover tooltip:

| Mode | Condition | Pill label (visible) | Tooltip |
|---|---|---|---|
| Direct Mode | `CURSEFORGE_API_KEY` set and valid | "CurseForge Connected" | "Direct mode: Connected to CurseForge." |
| Assisted Mode | No key, or invalid/expired key | "CurseForge Disconnected" | "Assisted mode: Disconnected from CurseForge." |

**Direct Mode**: full catalog browsing, compatibility badges, metadata (description/screenshots) from the API, automatic update detection via `curseforge.py`.

**Assisted Mode** (must work with zero API access):
1. User clicks through to a mod's CurseForge page, opened in the system's default browser (not inside the app's pywebview window) — ordinary human browsing, not automated access, so no ToS concerns.
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

- On launch, the frontend renders the library immediately from a DB-backed route (`GET /api/mods`) — no blocking "scanning..." screen. The backend must not make that route wait on a scan.
- `backend/main.py`'s `create_app()` builds (but never starts — see below) `app.state.mods_watcher` (a `scanner.ModsFolderWatcher`) and exposes `app.state.run_startup_scan` (adopts anything dropped directly under `Mods/` via `scanner.import_untracked_mods()`, then catches up on size/`mtime` changes via `scanner.incremental_scan()`). `desktop.py` runs `run_startup_scan` on a background thread right after the server comes up — it never delays opening the pywebview window.
- `mods_watcher`'s `on_change` fires on every raw filesystem event with no debounce of its own; `create_app()` wraps it in a 2-second debounce (`schedule_mods_rescan`, backed by a `threading.Timer`) before triggering another `incremental_scan()`, so a burst of events (e.g. one symlink toggle touching several paths) costs one rescan, not several. `desktop.py` calls `app.state.mods_watcher.start()`/`.stop()` around the pywebview window's lifecycle, same pattern as `download_watcher` — but only if `config.mods_watcher_enabled` (env var `MODS_WATCHER_ENABLED`, default on); watchdog's `Observer.stop()`/`.join()` raises on a watcher that was never `.start()`ed, so that guard has to match on both ends, not just skip the start call. Read-only in Settings, same as every other env-driven value.
- Full hash scan is manual-only: `POST /api/settings/full-scan` (synchronous — the Settings "Full scan" button shows its own "scanning..." state and waits), parallelized across cores via `scanner.full_scan()`'s `ProcessPoolExecutor` since hashing is CPU-bound.
- Like `download_watcher` (see "Assisted Mode download detection" below), none of this runs as a side effect of `create_app()` itself — building the app stays free of background threads so tests aren't affected; only `desktop.py` (or a test calling `app.state.run_startup_scan()`/`app.state.schedule_mods_rescan()` directly) triggers it.

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

blacklist (
  id, pattern,          -- matched case-insensitively as a substring of a mod's name or id
  note,
  created_date
)
```

## Key features and how they should behave

### Crash Mode
- Parse `lastException.txt` only. **Never parse `lastCrash.txt`** — it's confirmed unreadable/unusable even by the community, don't waste effort on it.
- Cross-reference traceback `File "..."` lines against `mod_files` paths under `Mods/` to identify suspect mods directly.
- Fall back to regex pattern matching for known error signatures (e.g. broken imports of outdated shared libraries like sims4communitylib/ts4lib) when no mod path appears directly in the trace.
- **Never auto-suggest deletion from a single occurrence** — an isolated LastException can be benign and unrelated to any mod. Surface suspects with confidence level, let the user decide.
- If no clear suspect: offer automated bisection (binary search via symlink toggling, batch-based, user confirms after each relaunch, logarithmic convergence). Never require full file moves — always via the symlink layer.
- **Regression, fixed 2026-08-21**: `lastException.txt` was assumed to be a single raw traceback (per the unvalidated note that used to sit here); real files confirmed it's XML (`<root><report>...<desyncdata>traceback</desyncdata>...</report>...</root>`) that can bundle several unrelated occurrences accumulated over one play session. `crash_analyzer.parse_reports()` now splits each `<desyncdata>` out and `record_crash_reports()` stores one `crash_log` row per occurrence, so unrelated incidents' suspects never get merged into a single row — falls back to treating the whole input as one occurrence for plain text (a format change, or this module's own non-XML test fixtures). `POST /api/crash/analyze` returns `{"found", "reports": [{"crash_log_id", "suspects"}, ...]}` (was a single `crash_log_id`/`suspects` pair) — Crash Mode's frontend now renders one block per occurrence, each with its own suspects/bisection-start action. See `tests/test_crash_analyzer.py`'s `parse_reports`/`record_crash_reports` tests (built from real, GUID-redacted captured crash data) and CLAUDE.md's "Path-matching heuristic" note in `crash_analyzer.py`'s module docstring, also updated now that it's validated against real data.

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
- Suggest cleanup automatically after install/update/disable actions, but always require confirmation before deleting. **Landed 2026-08-21**: `suggestCacheCleanup()` in `app.js`, wired into `installFromCatalog()`, `resolvePendingDownload()` (install/replace, not dismiss), `applyUpdate()`/`doUpdateAll()` (at least one success), and `toggleActive()` — but only on the disable path, not enable (not in the spec list above, and a newly-active mod isn't what would've left stale entries behind). Reuses the exact same `/api/cache/targets` + `openConfirmModal()` + `/api/cache/clean` flow as Crash Mode's manual "Clear cache" button (`buildCacheTargetNodes()` factored out so both share the same target list rendering) — no new backend route. Stays silent (no modal, no error banner) if the target list is empty or can't be fetched, since this is a courtesy suggestion riding along an action the user already took, not something that should itself surface an error.

### Translation-mod detection
No dedicated "translation" relation type exists in the CurseForge API (only `embeddedLibrary` / `incompatible` / `optionalDependency` / `requiredDependency`), so this always needs multiple weak signals combined, never a single automatic match:
1. Parse mod description for translation keywords (multi-language) + CurseForge URLs pointing at an already-installed mod — strongest signal.
2. Name/slug heuristics (`[FR]`, `- French Translation`, `_VF`) — weak, pre-filter only.
3. `.package` binary analysis via `package_parser.py`: check the file is STBL-only (no `.ts4script`, unusually small), compare STBL Group ID/Instance ID against the candidate source mod — strong confirmation, but on-demand only, never a full-library scan.
- **Every suggested link requires user confirmation.** Store `confidence` as `confirmed` or `suggested` — never silently create a `translation` dependency.
- **Surfaced 2026-08-21**: `POST /api/mods/{mod_id}/detect-translation` (runs `detect_translation_signals()` for one mod, on-demand — never a full-library scan) and `POST /api/mods/{mod_id}/suggest-translation` (creates the `confidence='suggested'` link — never `'confirmed'` directly), plus generic `POST /api/dependencies/{id}/confirm` and `.../reject` for any suggested dependency, not just translations. Frontend: the Library detail panel's Dependencies section gets Confirm/Reject buttons on `suggested` rows, and a "Check for translation match" button that shows detected signals (grouped by source mod, so a mod matching both the name-heuristic and STBL signals doesn't offer two separate "Link" buttons for the same source) with a "Link" action per group. `mod_manager.enable()` still separately blocks on unresolved `required` dependencies, unrelated to this UI.
- **Regression caught building this**: `dependencies._read_packages()` called `package_parser.read_package()` with no error handling — a candidate `.package` that isn't valid DBPF (corrupted download, non-standard file, or just test fixture data) crashed the whole `stbl_signal()`/`detect_translation_signals()` call instead of being treated as inconclusive for that one file. Now catches `package_parser.DbpfError` per file and skips it; see `tests/test_dependencies.py::test_regression_stbl_signal_skips_unparseable_package_instead_of_raising`.

### Dependency resolution
Track `required` / `optional` / `translation` dependency types. Block install/enable only on unresolved `required` dependencies; warn but don't block on `optional`.

### Duplicate/conflict detection
`backend/conflict_detector.py`, surfaced via `GET /api/conflicts` and a dismissible-by-collapsing (not deletable) banner in the Library view. Purely informational — same "suspicion is not confirmation" rule as Crash Mode: never blocks install/enable, never suggests deleting anything.
- **Exact duplicate mods** (strongest signal): two or more mods whose *entire* file sets are byte-identical (same set of `mod_files.hash` values, not just one shared file) — "shares 100% of its files with another mod." `find_exact_duplicate_mods()` groups active mods by the frozenset of their file hashes; 2+ mods landing on the same set are reported together. Suppresses every weaker signal below for any pair it covers.
- **`.package` duplicates**: two or more mods with a byte-identical file (same `mod_files.hash`) — usually the same mod installed twice under different names. Detected via a `GROUP BY hash HAVING COUNT(DISTINCT mod_id) > 1` query — no new file parsing, `hash` is already computed at install time.
- **`.ts4script` name collisions**: two or more mods shipping a script file with the same filename at their mod root. Since `.ts4script` archives are Python zipimport archives and the interpreter's module cache keys on module name (not path), a same-named script in two mods — often each bundling their own copy of a shared library — can silently shadow one another. Detected the same way, grouping on `relative_path` instead of `hash`.
- These signals come entirely from data already in `mod_files`; nothing here re-reads file contents or re-parses `.package` headers.
- Also surfaced in the Library warnings banner (same collapsible list, not a separate one): `blacklist.py` matches (below) — the banner's toggle count and list combine both.

### "Script Mods Allowed" check
`backend/game_options.py` reads the game's `options.ini` (location: `SIMS4_USER_DIR/options.ini`) looking for a `scriptmodsenabled` key — **not** tied to a specific `[Section]`, since the game's ini format/section naming isn't officially documented and a wrong guess there would make the check silently never find anything; it scans every `key=value` line regardless of section. Returns `True`/`False` only for an unambiguous value, `None` (never treated as "disabled") when the file or key is missing. Exposed via `GET /api/status`'s `script_mods_allowed` field, re-read on every call (cheap file read) rather than cached like `direct_mode`, since the game can rewrite this file while SimsLink is running. Shown as an always-expanded warning banner in the Library view when `false`, plus a read-only line in Settings.

**Regression, fixed 2026-08-21**: the key was originally guessed as `scriptmodsallowed` (matching the setting's in-game display name), which never matches a real `options.ini` — the actual key the game writes is `scriptmodsenabled`. This left the check permanently returning `None` ("unknown") against real game data despite the setting being present; see `tests/test_game_options.py::test_regression_key_is_scriptmodsenabled_not_scriptmodsallowed`.

### Mod profiles
`backend/profiles.py`, using the `profiles`/`profile_mods` tables (previously defined in the schema but never used by any code until now). A profile is a named snapshot of "which mods should be active"; `set_profile_mods()` replaces membership wholesale (simpler for a UI that captures "the mods active right now" than one managing per-mod add/remove), and `activate_profile()` makes exactly that set active — enabling every member and disabling every non-member via the existing `mod_manager.enable()`/`disable()` symlink toggling, nothing new invented for activation itself. Fails fast (propagates `UnresolvedRequiredDependencyError`/`ModManagerError`) on the first mod that can't be enabled, rather than silently partially applying a profile. Settings UI creation flow is deliberately minimal: "save the mods active right now as a new profile" (name + current active set) rather than a full mod-picker/editor — editing membership later means deleting and recreating.

### Local mod blacklist
`backend/blacklist.py` — a simplified version of SimsForge's malicious-mod flagging (see this file's "Project" section). Purely local and manual: entries are typed in by the user via Settings, nothing fetches a shared/remote list, and a match only ever informs (surfaced in the Library warnings banner alongside conflict-detector's findings) — it never blocks install/enable, same "suspicion is not confirmation" rule as everywhere else. Matching is a case-insensitive substring check against both a mod's display name and its id/slug.

## Logging

`backend/logging_config.py`'s `configure_logging(config)` sets up the `"backend"` logger (every module gets `logging.getLogger(__name__)`, which propagates up to it) with two handlers: console (stderr) and a file at `config.log_path` (`<XDG data dir>/simslink.log`, next to the DB). Level comes from `LOG_LEVEL` (env var, default `INFO`, one of `DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL` — invalid values raise `ConfigError`). `desktop.py` calls it once at startup and also passes `config.log_level` to `uvicorn.Config(log_level=...)`, so the one setting controls uvicorn's access logging too, not just ours.

Like `download_watcher`/`mods_watcher`/`run_startup_scan`, this is **never called from `create_app()`** — only `desktop.py` (or a test passing an explicit `log_path` override, the same pattern `create_app(db_path=...)` uses) calls `configure_logging()`, so building an app during a test run never reconfigures global logging state or writes into the real XDG log file.

What actually gets logged, by design (a level setting that logged nothing would be cosmetic, not a real feature):
- **DEBUG**: every HTTP request/response via a FastAPI middleware (`method path -> status (Xms)`) — verbose tracing, not meant to be read directly.
- **INFO**: user-meaningful mutating actions — mod enabled/disabled/deleted/installed (catalog or update-applied)/profile activated/cache cleared, and which mode (Direct/Assisted) the app started in.
- **WARNING**: notable-but-expected conditions — an enable blocked by an unresolved required dependency, a configured `CURSEFORGE_API_KEY` that got rejected, a mod confirmed faulty after crash bisection.
- Truly unexpected exceptions (a real 500) aren't specially logged here — uvicorn already logs those tracebacks on its own.

Log level is read-only in Settings (`GET /api/settings`'s `log_level`/`log_path` fields), same as every other env-driven setting — not a live runtime toggle.

## Things to never do

- Never install a mod directly at the `Mods/` root, and never nest a `.ts4script` more than one folder deep.
- Never make an automated request to any CurseForge endpoint without a valid API key — this includes CDN downloads, which require a key as of July 16, 2026.
- Never scrape CurseForge pages. It's explicitly prohibited by the platform's Terms of Use and won't even solve the download problem once the CDN requires a key.
- Never silently auto-delete a mod based on crash analysis. Suspicion is not confirmation.
- Never hardcode a UI string outside the `frontend/i18n/` layer.
- Never block on a full filesystem hash scan — not the FastAPI request handling the initial page/data load, and not the frontend's render while waiting for one.
- Never gate a mode-independent feature (library, Crash Mode, cache cleanup, etc.) behind `CURSEFORGE_API_KEY` being set.

## Testing

Unit tests and regression tests are mandatory for this project, not optional. Every module listed under Architecture should have corresponding tests, and every bug fix should come with a regression test that would have caught it.

- Framework: `pytest`.
- Test files mirror the module structure: `tests/test_<module>.py` for each file under `backend/` (e.g. `tests/test_mod_manager.py`, `tests/test_scanner.py`) — business-logic modules are tested directly, independent of the HTTP layer.
- `backend/main.py`'s routes get their own tests in `tests/test_backend_main.py`, via FastAPI's `TestClient` — no real HTTP server, no browser needed. A route test should stay thin: assert the route calls the right module function with the right arguments and translates its result/errors into the right HTTP status/JSON, not re-test business logic already covered at the module level. `create_app(config, db_path=...)` takes an explicit `db_path` for exactly this reason — it lets tests point the app at an isolated tmp_path database instead of `config.db_path`'s real, fixed XDG location.
- Frontend (`frontend/app.js` or `src/`): no test framework mandated by default given the project's current size — plain vanilla JS driving `fetch` calls against already-tested routes doesn't carry much regression risk on its own. Revisit this (e.g. Vitest + React Testing Library) if/when a view's client-side state logic grows non-trivial enough that a bug there wouldn't be caught by backend tests.
- **Priority modules for thorough coverage** — these encode the game's undocumented behavior and are the easiest place for silent regressions to slip in:
  - `mod_manager.py`: the `.ts4script` depth rule and the `Mods/<mod_id>/` placement rule must have explicit tests that assert installation fails/corrects itself when a script mod would land deeper than one level, and that `.package` files at any depth are accepted.
  - `scanner.py`: incremental scan must have a test proving an unchanged mod folder triggers zero hash computation, and a test proving a changed `mtime`/size does trigger one.
  - `crash_analyzer.py`: traceback parsing needs fixture `lastException.txt` samples (both "mod path directly in trace" and "core game code only" cases) with known expected suspect output. Also test that a single occurrence never produces an auto-delete suggestion.
  - `dependencies.py`: translation-detection heuristics need fixtures per signal type (description match, name heuristic, STBL comparison) with assertions on the resulting `confidence` value — a `suggested` link must never silently become `confirmed` without explicit user action in the code path.
  - `package_parser.py`: DBPF header parsing needs at least one real `.package` fixture (a small STBL-only file and a mixed-resource file) to catch parser regressions against the binary format.
- **Mode-dependent code** (`curseforge.py`, `download_watcher.py`, and `backend/main.py`'s Direct-Mode-only routes): tests must run without a real API key or network access — mock the CurseForge API client entirely (`tests/test_backend_main.py` does this via `monkeypatch.setattr("backend.main.curseforge.CurseForgeClient", ...)`), and mock filesystem events for the download watcher (no real file I/O against a live Downloads folder in CI).
- **Regression tests**: whenever a bug is fixed, add a test reproducing the original failure before the fix, named to reference the issue (e.g. `test_regression_ts4script_nested_two_levels`).
- Run the full suite before considering any feature "done" — this applies to Claude Code's own workflow in this repo, not just to human review.

## Development workflow

Once the above testing expectations are met, follow the collaboration pattern this project already uses: freeze acceptance criteria before implementing, change one variable at a time when debugging platform-specific behavior (symlinks, filesystem quirks), and treat a passing test suite as the GO signal before moving to the next feature — not as an afterthought once everything "looks done."

## Current project status

- **FastAPI + pywebview is the app, as of 2026-08-21.** The prior Flet UI (`ui/*.py`, root `main.py`, `i18n/`) has been removed — `backend/` + `frontend/` + `desktop.py` (`simslink` script entry) is the only implementation now, covering all five views (Library, Catalog, Updates, Crash Mode, Settings). Visual design follows `simslink-mockup.html`. All business-logic modules physically moved under `backend/` in the same change (cross-module imports are now relative — `from . import mod_manager`, etc.); watch for this if grepping old commits/notes for bare `import mod_manager`-style imports, which no longer resolve.
- **Assisted Mode download detection — closed 2026-08-21.** `backend/main.py`'s `create_app()` builds a `download_watcher.DownloadWatcher` and a `PendingDownloadStore` (in-memory, lock-protected — written to by the watcher's background thread via `report_download()`, read/drained by request threads) but does not start the watcher itself, so building the app has no filesystem side effect; `desktop.py` calls `app.state.download_watcher.start()`/`.stop()` around the pywebview window's lifecycle. Routes: `GET /api/downloads/pending`, `POST /api/downloads/{token}/install`, `POST /api/downloads/{token}/replace`, `POST /api/downloads/{token}/dismiss`. The frontend polls `/api/downloads/pending` every 3s (`startDownloadPolling()` in `app.js`, no SSE/WebSocket — plain polling is fine for a single-user local app, no sub-second latency need) and shows `#downloadOverlay` when something's waiting, mirroring the old Flet dialog's Install/Replace/Dismiss choices. Tests call `app.state.report_download(path)` directly to simulate a detection with no real watcher thread involved.
- **`Mods/` real-time watcher + manual full scan — closed 2026-08-21.** See "Startup scan" above for the full wiring (`app.state.mods_watcher`, `run_startup_scan`, `schedule_mods_rescan`, `POST /api/settings/full-scan`).
- **Reviewing/confirming suggested translation dependencies — landed 2026-08-21.** See "Translation-mod detection" above for the routes and the `dependencies.py` bug fix that came with it.
- **Duplicate/conflict detection — landed 2026-08-21.** See "Duplicate/conflict detection" above. Of brief §7's cross-cutting feature list, this is the only one built so far; the rest (`options.ini` "Script Mods Allowed" check, one-click mod profiles using the already-defined but unused `profiles`/`profile_mods` tables, a batch "update all" action, a local mod blacklist) remain net-new, unstarted work, not gaps in something already built.
- **Backup retention — landed 2026-08-21.** `download_watcher.py` created a timestamped backup on every replace but never purged old ones — `LIBRARY_DIR/.backups/` grew forever. Fixed: new `Config.backup_retention_count` (env var `BACKUP_RETENTION_COUNT`, default 5, validated as a positive int in `from_env()`) + a purge step, keeping only the newest N backups per mod_id (purge is scoped per mod — replacing one mod never touches another's backup history). Exposed read-only via `GET /api/settings` and the Settings view's new "Backups" section; still no in-app editing (same as the other settings), just `.env`. Of brief §6.8's settings table, this is the only entry actually backed by real logic so far — the rest (theme, tile size, log level, pattern-sensitivity, etc.) still need the underlying subsystem built before there's anything to expose. **Extracted into `backend/backups.py` on 2026-08-21** (`backup_folder()`/`purge_old_backups()`) so `broken_mods.py`'s automatic fixes (see below) reuse the exact same mechanism instead of duplicating it — `download_watcher.confirm_replace()` calls it the same way it always did, just from the new module.
- The README's "automatic (confirmable) detection of translation mods" claim is now accurate (see above). Its batch "Update all" claim is now accurate too — see below.
- **Batch "Update all" — landed 2026-08-21.** Frontend-only; no new backend route needed, since `doUpdateAll()` in `app.js` just loops `POST /api/updates/{mod_id}/apply` over whatever `POST /api/updates/check` most recently flagged as `update_available` (tracked in `state.updatableMods`). One mod's failure doesn't abort the batch — failures are collected and reported together (`updates.update_all_partial_error`), then the check re-runs so applied mods drop off the list and failures are re-offered. The "Update all" button only appears once there are ≥2 updatable mods (with exactly one, the per-row "Update" button is already a single click). Confirmation goes through the shared `openConfirmModal()`, listing every mod about to be updated before it starts — each individual `apply` still backs up via `download_watcher.confirm_replace()` same as before, nothing new there.
- **Brief §7 "Script Mods Allowed"/profiles/blacklist, and brief §6.8 theme/tile size — landed 2026-08-21.** See "Script Mods Allowed" check / "Mod profiles" / "Local mod blacklist" above for the backend side. Frontend: Settings gets Theme (dark/light) and Library tile size (large/compact) dropdowns — both pure client-side preferences (`localStorage`, no backend route), applied via a `data-theme` attribute and a `.tile-compact` class respectively; the theme is applied pre-paint by a tiny inline `<script>` in `index.html`'s `<head>` (reads `localStorage` before the stylesheet/DOM even render) specifically to avoid a dark→light flash on load. Settings also gets Profiles and Blacklist management sections (list/create/activate/delete, list/add/remove).
- **Log level — landed 2026-08-21.** See "Logging" above for the full design. Deliberately scoped to just this half of brief §6.8's pair — **Crash Mode "pattern-sensitivity" is still explicitly not attempted**: `crash_analyzer.py`'s known-pattern matching is a binary regex check with no notion of a tunable score/threshold to attach a slider to, and — candidly noted in that module's own docstring — the traceback-parsing path has never been validated against a real `lastException.txt` (none was available during development, only synthetic fixtures). Tuning "sensitivity" on an unvalidated heuristic would mean guessing at a false-positive/false-negative tradeoff with no real crash-log data to check it against; revisit once real logs are available to validate the detector itself, not just add a control to it.
- **Automatic cache-cleanup suggestion — landed 2026-08-21.** See "Cache cleanup" above. This closed a real behavioral gap (not an aspirational one): CLAUDE.md/the brief had specified this from the start, but nothing implemented it before now — cache cleanup was manual-only via Crash Mode.
- **Real-time `Mods/` watcher toggle — landed 2026-08-21.** `MODS_WATCHER_ENABLED` env var (see "Startup scan" above), read-only in Settings like every other env-driven setting — not a live runtime toggle. Closes brief §6.8's "activer/désactiver le watcher temps réel" line.
- **Regression caught by the backend/ move, now fixed**: `config.py`'s `DEFAULT_ENV_PATH` was `Path(__file__).resolve().parent / ".env"`, which — once `config.py` lives in `backend/` instead of the project root — silently resolved to `backend/.env` instead of the real `.env`. Fixed to `.parent.parent`; see `tests/test_config.py::test_regression_default_env_path_resolves_to_project_root_not_backend_dir`. Worth remembering as a category: any other `Path(__file__)`-relative path assumption written before the move should be double-checked the same way.
- No CurseForge API key yet — build and test everything through Assisted Mode first (now fully working end-to-end, including install). `curseforge.py` is the only module blocked on key approval.
- `brief-sims4-mod-manager.md` (the detailed spec this file summarizes) was updated 2026-08-21 for the FastAPI/pywebview stack (architecture tree, Assisted Mode's install mechanics, i18n file paths, view/section naming) and hasn't been touched since — most of what it once flagged as aspirational (batch "update all", §7's duplicate detection/Script Mods Allowed/profiles/blacklist) has since landed and isn't reflected there. Two real gaps remain against §7: no rollback/restore UI for the backups `download_watcher.py` already creates (the files are there under `.backups/`, nothing surfaces them to restore from), and no changelog display on updates (CurseForge's API doesn't expose one per-file, so this isn't buildable without that data existing upstream). §6.8's full settings table (log level, pattern-sensitivity, live-editable folders) also remains open — see above. This file (`CLAUDE.md`) stays authoritative wherever the brief disagrees.
- **`GAME_VERSION` auto-detection — landed 2026-08-21.** `.env`'s comment always said "auto-detected if possible, otherwise manual", but nothing implemented the auto-detection half until now — `config.game_version` was purely a manual env var. `game_options.detect_game_version()` reads the installed build string (e.g. `1.126.78.1020`) straight out of `Game/Bin/TS4_x64.exe`'s embedded Windows PE VERSIONINFO resource (`ProductVersion`), via the new `pefile` dependency (pure Python, pip-installable, no system package needed) — works against a Wine/Proton-installed `.exe` since it's just reading raw bytes, no Windows API involved. Falls back to `TS4_DX9_x64.exe` if the primary exe is missing. `Config.from_env()` calls it only when `GAME_VERSION` is blank (a deferred `from . import game_options` inside the method body, since `game_options.py` already imports `Config` from this module — a module-level import the other way would cycle).
- **Broken-mod-folder detection + safe auto-fix — landed 2026-08-21.** New `backend/broken_mods.py`, same informational, non-blocking spirit as `conflict_detector.py`: `scan_broken_mods()` finds folders directly under `Mods/` with nothing the game would actually load (a `.package`/`.ts4script` search finds none) and classifies why — `empty`, `unextracted_archive` (a `.zip` was dropped in but never extracted), `unpacked_script` (a `.ts4script` was extracted in place instead of kept intact — the game only loads the archive itself), or `unrecognized` (files present, cause unclear — e.g. a leftover log, or non-mod data like a stray save file). Surfaced via `GET /api/mods/broken`, folded into the Library warnings banner alongside conflicts/blacklist matches (same collapsible list). `fix_broken_mod()` auto-fixes only the two safe reasons (`empty` → delete; `unextracted_archive` with exactly one zip → install it through the normal pipeline) via `POST /api/mods/broken/{name}/fix`, gated behind the frontend's confirm modal same as every other destructive action — never automatic. Always backs up the original folder first (via the newly-shared `backups.py`, see above) so a bad auto-fix is recoverable. `unpacked_script`/`unrecognized` have no fix button: reconstructing a `.ts4script` from loose files left after a bad extraction is a real capability gap, deliberately deferred as riskier (loose files may be incomplete or reordered relative to the original archive) — revisit as a separate iteration once there's a clearer picture of what's actually recoverable.
- **Desktop shell temporarily browser-only, not pywebview — since 2026-08-21.** `desktop.py` currently opens SimsLink in the system's default browser (`webbrowser.open()`) instead of a pywebview native window. Root cause not yet found: on this dev machine (GTK3 WebKit2GTK under a native Wayland session), a pywebview window rendered correctly and accepted keyboard input, but real mouse clicks never reached the page (confirmed via WebKit inspector: hit-testing found the correct DOM element under the cursor, no overlay/z-index issue, no JS errors) — reproduced under both the native Wayland GDK backend and `GDK_BACKEND=x11` (XWayland), so it isn't a simple backend-selection fix. Nothing about `backend/`'s routes or `frontend/`'s JS is pywebview-specific — the same app serves either shell unmodified — so this is purely a `desktop.py` entry-point concern. Revisit pywebview once the click-forwarding bug is root-caused; until then, don't assume a native window is running when debugging UI issues on this machine.
- **Library UI polish pass — landed 2026-08-21.** Several frontend-only changes, no backend routes added: (1) a grid/list view toggle for the Library (`state.viewMode`, persisted like theme/tile size); (2) mods with an active conflict/blacklist match sort to the top of both views, then by author (when known) then name, with a "Resolve" button inline in the warnings banner jumping straight to that mod's detail comparison; (3) mods are grouped under an author heading (a name + horizontal rule) when `author` is known — repeats per contiguous run, so the problem/non-problem split can produce the same author's heading twice; (4) a name-prefix like `[SS]`/`(JBABS)` is split off the displayed name and shown as the tile's abbreviation instead, purely a rendering change — `mod.name` in the DB is untouched; (5) a delete (trash) icon directly on every card/row, reusing the same confirm-modal delete flow as the detail panel; (6) `primary_type` now surfaces as one or two badges (CC for package content, Script for script content — a `mixed` mod gets both, not a single "mixed" label); (7) the **Catalog** nav entry is hidden entirely in Assisted Mode (it has zero content without a CurseForge key — just an empty notice); **Updates stays visible**, since its manual checklist (linked mods + "Check on CurseForge" links) still has real content without a key. A global `[hidden] { display: none !important; }` rule (added the same day) is what makes toggling `.hidden`/`element.hidden` on any of these actually work — several component classes (`.btn`, `.card`, `.warning-banner`, ...) set their own `display`, which silently defeated the browser's default `[hidden]` rule at equal specificity before that fix.
- **Best-effort `unpacked_script` repair — landed 2026-08-21.** Closes the "no fix button" gap the broken-mod-folder entry above explicitly deferred, but only for this one reason, and only as a best-effort attempt clearly labeled as such — not promoted into `FIXABLE_REASONS`/`fix_broken_mod()`, which stays reserved for the two fixes safe enough to be silent about the risk. New `broken_mods.attempt_script_repair()`: backs up the folder (same `backups.py` mechanism), re-zips its contents as-is (relative paths preserved, archive root = folder root) into a `.ts4script`, and installs it through the normal `mod_manager.install()` pipeline — this can only ever be as good as the original extraction; a partial or reordered extraction still "installs" but won't load in-game, which is exactly why it's a separate opt-in action and not folded into the automatic fixes. Route: `POST /api/mods/broken/{name}/attempt-script-repair`. Frontend: `unpacked_script` folders render as a real card/row via `brokenFolderPseudoMod()` (a synthetic mod-shaped object with `__brokenFolder` set), which `buildCard()`/`buildListRow()` branch on internally — no separate builder functions. A wrench icon opens the standard confirm modal (explicitly warning the attempt can fail silently) before calling the new route. Removed from the banner's own list in the same change (`renderWarnings()` filters `reason !== "unpacked_script"` out) since it would otherwise be double-surfaced.
- **Broken-mod cards unified with real mod cards, equal card heights, badge consolidation — landed 2026-08-21.** Follow-up to the entry above: broken folders no longer get special-cased top-of-grid placement — `render()` folds their pseudo-mods into the same `visibleMods()` array before sorting/grouping, so they sort, group under an author header (when their name matches one via the same heuristics as any other mod), and get their version extracted exactly like a real mod (`groupingAuthor()`/`splitNameVersion()` don't know or care that it's a pseudo-mod). They stay visually distinct — red (`--danger`/`--danger-glow`, a true red, not the orange `--warn` used for suspected conflicts, since a broken folder is confirmed non-functional, not merely suspected) with a broken/alert icon (`BROKEN_ICON_SVG`) instead of the folder icon it briefly had. Also: every classification pill a card can carry (type CC/Script, duplicate, broken, version, compat, disabled) now renders through one shared `buildBadges()` — previously the grid card scattered these across three separate spots (top-left disabled-tag, top-right compat-badge, bottom-left type-badges, plus version as plain text in the meta row); now they're one row (`.card-badges`, overlaid on the fixed-height thumbnail so badge count never affects card height) mirroring what the list view's `.list-mod-row-badges` already did. Equal-height grid cards: CSS Grid's default row-stretch only equalizes cards within the same row, not across the whole grid, and `-webkit-line-clamp` alone only caps the *maximum* lines — a short one-line title/description still shrank the card. Fixed by giving `.card-body h3`/`p` an explicit `min-height` matching their line-clamp (2 lines), so every card's natural height is identical regardless of content, before grid stretch even needs to apply.
- **Disabled-mod styling, "Simplified names" toggle, `unextracted_archive` promoted to a problem card — landed 2026-08-21.** Three more frontend-only follow-ups: (1) a disabled mod's title gets `text-decoration: line-through` in both views, and the list view's `.is-inactive` row gains `filter: grayscale(0.7)` on top of its existing opacity dimming — opacity alone still let badge/gem colors read as normal at 60%, which didn't look distinctly "off" the way the grid card's own grayscale thumb filter already did. (2) A "Simplified names" checkbox (`#simplifiedNamesCheckbox`, persisted like theme/tile size) sits next to the grid/list view switch in a new `.head-controls` wrapper; unticking it makes `buildCard()`/`buildListRow()` show `mod.name` raw instead of `groupingAuthor()`'s cleaned-up `displayName` (see `titleText()`) — deliberately scoped to just the title text: author-header grouping, sort order, the avatar label, and the version badge all keep using `groupingAuthor()` regardless, since those are organizational metadata, not "the mod's name" as far as this toggle goes. (3) `unextracted_archive` folders (not just `unpacked_script`) now also get the red pseudo-mod card treatment via `visibleBrokenModFolders()` (renamed from the script-only `visibleBrokenScriptFolders()`) — `buildBrokenActionButton()` dispatches the card's repair icon to the right route per reason (`attempt-script-repair` vs. the existing safe `fix` route), and returns no button at all for an archive with 2+ zips (ambiguous, no safe single-click fix) so the card still flags it as a problem without offering an action guaranteed to fail — mirroring the restraint the warnings banner's own "Fix" button already had. `empty`/`unrecognized` still stay banner-only.
- **Version badge normalization, delete on broken folders, square cards, 4-tier sort fix — landed 2026-08-21.** (1) `formatVersionBadge()` prefixes a bare version ("6.9.4") with "v" for display, leaving one that already has it ("v1.7"/`installed_version` values that already include it) untouched. (2) New `broken_mods.delete_broken_folder()` (backs up, then removes — works for any reason, unlike the reason-specific fix/repair actions) behind `DELETE /api/mods/broken/{name}`; every broken card/row now gets a delete icon alongside its reason-specific repair action — broken folders have no enable/disable concept (no symlink/active state to toggle, and nothing in them loads anyway) so delete is their only action beyond repair. (3) `.card` now uses `aspect-ratio: 1/1` instead of a fixed-height thumb + intrinsic-height body: `.thumb` is a `flex-basis` percentage of the card instead of a fixed px height, `.card-body` gets `min-height: 0` so it shrinks to the remainder, and the grid's minimum column width went from 210px to 230px to give the now-square card enough absolute room for its title/description/meta content — verified via computed-style measurement (`getBoundingClientRect()` width == height, zero `.card-body` scroll overflow, action buttons fully inside the card's bounds) rather than assuming the numbers work out. (4) The sort comparator's old two-step (active-first, then problem-first) let a broken pseudo-mod and a real conflict-flagged mod tie and fall through to alphabetical — since both were stuffed into the same `problems` Set. Replaced with `modTier()`, a single 4-value ranking (0 broken / 1 problem / 2 normal / 3 disabled) applied within each author group, matching the intended priority order exactly.
- **"Unknown author" header for the authorless bucket — landed 2026-08-21.** Mods with no real `author`, no bracket-prefix, and no inferred series match already sorted to the end of the Library grid/list (author-presence is the top sort key), but ran header-less off the end of the last real author's section — now they get their own generic header (`library.unknown_author_header`, "Auteur inconnu"/"Unknown author") in `render()`'s grouping loop, same contiguous-run logic as any other author.
- **Exact duplicate mods (100% file match) + author-based tagging — landed 2026-08-22.** New strongest conflict signal, `conflict_detector.find_exact_duplicate_mods()`: two or more active mods whose *entire* file-hash set is identical (not just one shared file, which is `duplicate_package`'s weaker signal) are grouped as `exact_duplicate_mod` and suppress every weaker signal for that pair, same precedence pattern `folder_duplication` already used. `GET /api/conflicts` now also resolves each mod's `author` (previously just name/active/install_date) so the frontend can decide which side of an exact-duplicate pair to blame — `duplicateTagModIds()`: when one member has a known author and another doesn't, the unauthored one gets the "Duplicate" tag (presumed the redundant re-download/rehost). When there's no known-vs-unknown split to go on — nobody has an author, or everybody does, whether the same author twice or two different ones — `identicalContentTagModIds()` tags every member with a neutral "Identical content"/"Contenu identique" pill instead, deliberately *not* picking a side: guessing which of two different authors' mods is "the copy" from arbitrary metadata (install date, etc.) risks a wrong, unfounded accusation baked into the UI, the same "suspicion is not confirmation" reasoning applied everywhere else in this app (blacklist matches, translation-dependency suggestions). Both tags still make the pair a "problem" tier in the sort order and get the red card border either way — only the tag text/color differs.
