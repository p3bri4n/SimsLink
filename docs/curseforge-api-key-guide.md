# Getting a CurseForge API key (for Direct Mode)

SimsLink works fully without one — see [Direct Mode vs. Assisted Mode](../README.md#direct-mode-vs-assisted-mode) in the README. This guide is only for the extra convenience of Direct Mode: in-app catalog browsing, automatic compatibility badges, and automatic update detection.

## Why you need your own key

CurseForge's API is gated behind a key, and that key is issued per applicant, not per app. SimsLink can't ship with one key embedded for every user to share — that's against CurseForge's terms, and a single shared key would get rate-limited or revoked instantly under real usage. Each user who wants Direct Mode requests their own key and drops it into their own `.env` file; SimsLink never sees or stores anyone else's key.

There's no OAuth-style "log in with your CurseForge account" alternative for this — the only path is an API key application. Scraping CurseForge pages instead is explicitly prohibited by their Terms of Use (and wouldn't solve the download problem anyway: the CDN itself has required a key for downloads since July 16, 2026).

## How to request one

1. Go to [console.curseforge.com](https://console.curseforge.com/) and sign in with your CurseForge account.
2. Submit an API key application, describing SimsLink as a personal-use Sims 4 mod manager for Linux.
3. Applications are reviewed manually by the Overwolf team. Mod managers are a common, well-understood use case, and approval rates for this category have historically been high — but there's no guaranteed timeline.

## Once you have a key

Add it to your `.env` file (see `.env.example`):

```
CURSEFORGE_API_KEY=your-key-here
```

Restart SimsLink. The mode banner switches from "Assisted Mode" to "Direct Mode" automatically once the key is present and verified — no other configuration needed.

## One thing a key doesn't change

Some mod authors disable third-party distribution for their mods specifically (a per-mod setting on CurseForge's side). For those, even in Direct Mode, SimsLink falls back to an "Open on CurseForge" link instead of an in-app download — that's the mod author's choice, not a SimsLink limitation.
