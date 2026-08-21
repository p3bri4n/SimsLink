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
- Incremental scan runs as a background task in the FastAPI process (not on the request thread handling the page load): compare size + `mtime` per file against `mod_files`; only changed/new files get a full hash. Unchanged mod folders are skipped entirely.
- A `watchdog`-based real-time watcher on `Mods/` catches external changes while the app is running, reducing the next startup scan to just catching up on changes made while the app was closed.
- Full hash scan is manual-only (Settings → "Full scan"), parallelized across cores since hashing is CPU-bound.

**Not yet wired up**: `scanner.py` has both the incremental/full scan functions and a `ModsFolderWatcher` class, but neither `desktop.py` nor `backend/main.py` starts the real-time watcher yet, and there's no "Full scan" trigger in the Settings view (`/api/settings` is read-only so far). Both are still open work, not just documentation of a finished behavior.

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
- **Not yet surfaced in either API or frontend** — `dependencies.py` implements detection and confirm/reject, but nothing in `backend/main.py` exposes it yet. `mod_manager.enable()` does already block on unresolved `required` dependencies (see below), so that half is live.

### Dependency resolution
Track `required` / `optional` / `translation` dependency types. Block install/enable only on unresolved `required` dependencies; warn but don't block on `optional`.

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
- The `Mods/` real-time watcher (`scanner.ModsFolderWatcher`) is a **separate, still-open gap** — not started anywhere, no route/UI for it. Same for a Settings UI to trigger a manual full scan, and for reviewing/confirming suggested translation dependencies (see "Translation-mod detection" above) — `dependencies.py`'s detection/confirm/reject functions have no API route at all yet.
- **Regression caught by the backend/ move, now fixed**: `config.py`'s `DEFAULT_ENV_PATH` was `Path(__file__).resolve().parent / ".env"`, which — once `config.py` lives in `backend/` instead of the project root — silently resolved to `backend/.env` instead of the real `.env`. Fixed to `.parent.parent`; see `tests/test_config.py::test_regression_default_env_path_resolves_to_project_root_not_backend_dir`. Worth remembering as a category: any other `Path(__file__)`-relative path assumption written before the move should be double-checked the same way.
- No CurseForge API key yet — build and test everything through Assisted Mode first (now fully working end-to-end, including install). `curseforge.py` is the only module blocked on key approval.
- `brief-sims4-mod-manager.md` (the detailed spec this file summarizes) still describes the old Flet-based design (its "6. Vues Flet" section, in particular) — it hasn't been updated for the FastAPI/pywebview stack yet, so treat this file as authoritative wherever the two disagree.
