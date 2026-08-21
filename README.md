# SimsLink

A desktop mod manager for The Sims 4 on Linux, with automated crash diagnosis.

There is no official CurseForge client for Linux; SimsLink fills that gap. Its main
differentiator is parsing the game's `lastException.txt` to identify which installed
mod likely caused a crash.

## Status

Early development. Runs in **Assisted Mode** (browser-based install, no CurseForge API
key required) until a CurseForge API key is approved — see `CLAUDE.md` for the
Direct Mode / Assisted Mode split and `brief-sims4-mod-manager.md` for the full spec.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # fill in your Sims 4 paths
```

## Run

```bash
simslink        # or: python desktop.py
```

Requires WebKitGTK for the desktop window (`python3-gi` + `gir1.2-webkit2-4.0` on Debian/Ubuntu-based distros).

## Tests

```bash
pytest
```
