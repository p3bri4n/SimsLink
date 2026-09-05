// SimsLink frontend — all five views (FastAPI + pywebview). Talks to
// backend/main.py's /api/ routes; never hardcodes UI text — everything
// user-facing goes through t(), backed by i18n/{en,fr}.json.

const DOWNLOAD_POLL_INTERVAL_MS = 3000;
const LIBRARY_POLL_INTERVAL_MS = 5000;

// Shared instance for render()'s sort comparator: `str.localeCompare(other,
// undefined, {sensitivity: "base"})` builds a new Intl.Collator internally
// on every single call — negligible for a handful of comparisons, but a
// real cost once a sort does thousands of them (n=1150 log2(1150) ≈ 12000
// comparisons after a bulk loose-file import, see loose_mods.py). A single
// shared Collator's .compare() does the same comparison without repeating
// that setup work each time.
const BASE_COLLATOR = new Intl.Collator(undefined, { sensitivity: "base" });

const state = {
  mods: [],
  conflicts: [],
  blacklistMatches: [],
  brokenMods: [],
  rezippedMods: [],
  missingMods: [],
  missingModsExpanded: false,
  // loose_mods.suggest_groupings() — see renderMergeableBanner() and
  // openMergeComparison() below. Part of the regular Library data cycle
  // (fetchLibraryData()) so it stays in sync like everything else, rather
  // than a separate Settings-only fetch.
  looseSuggestions: [],
  // compat_quarantine.py's preview — active mods flagged incompatible with
  // the current game version, plus active mods that locally required-depend
  // on one. Part of the regular Library data cycle like looseSuggestions
  // above. The actually-paused list (compat_quarantine table) is a Settings-
  // only concern instead — see loadCompatQuarantineManageList().
  compatQuarantinePreview: [],
  looseOnlyFilter: false,
  translationOnlyFilter: false,
  linkedOnlyFilter: false,
  incompatibleOnlyFilter: false,
  typeFilter: "",
  stateFilter: "",
  librarySnapshot: null,
  conflictsExpanded: false,
  // Session-only — hides the warnings banner until the next launch without
  // touching any of the underlying broken folders/files. Cleared as soon as
  // the list is genuinely emptied (see renderWarnings()) so a later, new
  // problem isn't silently suppressed by a stale dismissal.
  conflictsBannerDismissed: false,
  // Same session-only expand/dismiss pattern as conflictsExpanded/
  // conflictsBannerDismissed above, generalized for the "broken mods"/
  // "mods to unzip"/"duplicate mods" banners — see renderBanner().
  bannerExpanded: { broken: false, unzip: false, duplicates: false, mergeable: false, compat: false },
  bannerDismissed: { broken: false, unzip: false, duplicates: false, mergeable: false, compat: false },
  strings: {},
  filterQuery: "",
  currentDetailId: null,
  status: null,
  lang: "en",
  viewMode: "grid",
  simplifiedNames: true,
  groupByAuthor: true,
  collapsedAuthors: new Set(),
  catalogWired: false,
  updatesWired: false,
  settingsWired: false,
  currentPendingDownload: null,
  updatableMods: [],
  // Duplicate comparison — see openDuplicateComparison(). Null when the
  // view isn't open; dupResolving guards a same-tick double click while a
  // resolveDuplicateSelection() request is in flight.
  dupCompareGroup: null,
  dupResolving: false,
  // Merge comparison (checkbox-select) — see openMergeComparison(). Null
  // when the view isn't open.
  mergeCompareGroup: null,
  mergeCompareSelected: new Set(),
  // CurseForge fingerprint-match popup — see openMatchCurseforgeModal().
  // Set false to make the in-flight step() loop stop after its current
  // request instead of scheduling another one.
  matchCurseforgeRunning: false,
  // Same idea, for the "Synchronize" bulk sync popup — see
  // openSyncCurseforgeModal().
  syncCurseforgeRunning: false,
};

function detectLang() {
  const lang = (navigator.language || "en").toLowerCase();
  return lang.startsWith("fr") ? "fr" : "en";
}

async function loadI18n(lang) {
  const res = await fetch(`i18n/${lang}.json`);
  state.strings = await res.json();
}

function t(key, params) {
  const template = state.strings[key] || key;
  if (!params) return template;
  return template.replace(/\{(\w+)\}/g, (_, name) => (name in params ? params[name] : `{${name}}`));
}

function applyStaticI18n() {
  document.title = t("app.title");
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.dataset.i18n);
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.placeholder = t(el.dataset.i18nPlaceholder);
  });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => {
    el.title = t(el.dataset.i18nTitle);
  });
}

function elementWithText(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  el.textContent = text;
  return el;
}

// primary_type is computed from which file types the mod actually ships
// (mod_manager.py): 'package', 'script', or 'mixed' (both). A 'mixed' mod
// gets both badges — it genuinely contains both, not an either/or.
function primaryTypeBadgeKeys(primaryType) {
  const keys = [];
  if (primaryType === "package" || primaryType === "mixed") keys.push("library.cc_badge");
  if (primaryType === "script" || primaryType === "mixed") keys.push("library.script_badge");
  return keys;
}

const DELETE_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>' +
  '<path d="M10 11v6"/><path d="M14 11v6"/><path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';

function buildDeleteIconButton(mod) {
  const btn = document.createElement("button");
  btn.className = "icon-btn delete-icon-btn";
  btn.title = t("library.delete_button_title");
  btn.innerHTML = DELETE_ICON_SVG;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmDelete(mod);
  });
  return btn;
}

// Broken folders have no enable/disable concept (see confirmDeleteBrokenFolder()'s
// comment) so this is the one action every broken card/row gets in addition
// to whichever reason-specific repair button buildBrokenActionButton() offers.
function buildDeleteBrokenButton(folder) {
  const btn = document.createElement("button");
  btn.className = "icon-btn delete-icon-btn";
  btn.title = t("library.delete_button_title");
  btn.innerHTML = DELETE_ICON_SVG;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmDeleteBrokenFolder(folder);
  });
  return btn;
}

const BROKEN_ICON_SVG =
  '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">' +
  '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>' +
  '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

const REPAIR_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>';

const UNZIP_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M21 8v13H3V8"/><path d="M1 3h22v5H1z"/><path d="M10 12h4"/></svg>';

// "Reveal in file manager" — used for a mod's own open-folder action and a
// cache target's open action alike (see open_folder()/open_cache_target()
// in main.py, both xdg-open a directory).
const FOLDER_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M4 20h16a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13c0 1.1.9 2 2 2z"/></svg>';

// "Look at this in the file manager" — the cleanup banner's sole per-row
// action (renderWarnings()): view-only, since deleting straight from that
// list was judged too risky for content the app never fully classified
// ('empty'/'unrecognized' — see broken_mods.py). Same magnifying-glass
// glyph as the search input/header match button.
const SEARCH_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/></svg>';

// Enable/disable toggle — a filled circle with a power-switch notch, same
// stroke weight as the other 14px detail-panel action icons.
const POWER_ICON_SVG =
  '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M18.36 6.64a9 9 0 1 1-12.73 0"/><line x1="12" y1="2" x2="12" y2="12"/></svg>';

// 'unextracted_archive' is a distinct, less alarming category than
// 'unpacked_script' — nothing is actually broken, a zip is just sitting
// unopened — so it gets its own purple "Zip" identity instead of the red
// "Cassé"/"Broken" one. Shared by every card/row/detail-view spot that
// needs to pick a state class, badge text, or icon color.
function brokenStateClass(folder) {
  return folder.reason === "unextracted_archive" ? "is-archive" : "is-broken";
}

// A broken folder's action button dispatches on `reason`: 'unpacked_script'
// gets the best-effort re-zip attempt (attempt-script-repair, wrench icon),
// while 'unextracted_archive' always gets an unzip action now — a single
// unambiguous archive goes through the existing safe auto-fix
// (fix_broken_mod, same route/confirm flow as the warnings banner's own
// "Fix" button), two or more open a picker letting the user choose which
// one(s) to extract (extract_selected_zips — "often we have to choose one
// or several": mutually exclusive variants, or several required pieces).
// 'empty'/'unrecognized' don't get a card at all (see
// visibleBrokenModFolders()). Shared by the compact icon button (card/row)
// and the full-text button (detail panel) so the reason-dispatch logic
// lives in one place.
function brokenActionSpec(folder) {
  if (folder.reason === "unpacked_script") {
    return {
      label: t("library.broken_repair_button_title"),
      onClick: () => confirmScriptRepair(folder),
      icon: REPAIR_ICON_SVG,
      iconBtnClass: "repair-icon-btn",
    };
  }
  if (folder.reason === "unextracted_archive") {
    const single = folder.zip_paths.length === 1;
    return {
      label: t("library.conflicts.broken_mod_fix_button"),
      onClick: () => (single ? confirmFixBrokenMod(folder) : confirmExtractZips(folder)),
      icon: UNZIP_ICON_SVG,
      iconBtnClass: "unzip-icon-btn",
    };
  }
  return null;
}

function buildBrokenActionButton(folder) {
  const spec = brokenActionSpec(folder);
  if (!spec) return null;
  const btn = document.createElement("button");
  btn.className = `icon-btn ${spec.iconBtnClass}`;
  btn.title = spec.label;
  btn.innerHTML = spec.icon;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    spec.onClick();
  });
  return btn;
}

function confirmScriptRepair(folder) {
  openConfirmModal({
    title: t("library.broken_repair_confirm.title"),
    message: t("library.broken_repair_confirm.message", { name: folder.name }),
    confirmLabel: t("library.broken_repair_confirm.confirm"),
    onConfirm: () => doAttemptScriptRepair(folder.name),
  });
}

async function doAttemptScriptRepair(name) {
  try {
    await apiRequest(`/api/mods/broken/${encodeURIComponent(name)}/attempt-script-repair`, { method: "POST" });
    closeConfirm();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// Not a real installed mod — no detail panel to open, only the repair
// action makes sense here. Filtered by the same search query as real mods
// for consistency. 'empty'/'unrecognized' stay banner-only (see
// renderWarnings()): there's nothing to repair for the former and no
// confident diagnosis for the latter, so a red "problem" card would just
// be misleading noise.
function visibleBrokenModFolders() {
  const query = state.filterQuery.trim().toLowerCase();
  let candidates = state.brokenMods.filter(
    (folder) => folder.reason === "unpacked_script" || folder.reason === "unextracted_archive"
  );
  if (state.stateFilter) {
    // Same "broken" (red)/"zip" (purple) split brokenStateClass() already
    // uses for card styling — see stateFilterBucket() for the real-mod half
    // of this same switch.
    candidates = candidates.filter(
      (folder) => (brokenStateClass(folder) === "is-archive" ? "zip" : "broken") === state.stateFilter
    );
  }
  return query ? candidates.filter((folder) => folder.name.toLowerCase().includes(query)) : candidates;
}

function initials(name) {
  return (name || "")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0].toUpperCase())
    .join("");
}

// Display-only: many mod names lead with a "[SS]"/"(AstroBluu)"-style author
// or series tag. Pulling it out shortens the name shown on cards/rows and
// gives the tile avatar something more meaningful than the first letters of
// "[SS]" itself — never touches mod.name/the stored data, purely rendering.
function splitNamePrefix(name) {
  const match = /^[[(]([^\])]+)[\])]\s*(.*)$/.exec(name || "");
  if (!match || !match[2].trim()) return { prefix: null, rest: name };
  return { prefix: match[1].trim(), rest: match[2].trim() };
}

// Display-only: many mod names bake an explicit version number in as a
// trailing suffix ("_v6.9.4", "_2.6.1", "V1.7", "xxx v1.051", "xxx v4",
// "xxx - v7.0.0"). Pulling it out keeps the shown name focused on the
// mod's actual title and gives a real version to display even in Assisted
// Mode, which has no CurseForge `installed_version` metadata. A trailing
// "(1)"/"(2)" duplicate-copy marker (the same signal conflict_detector.py's
// folder_duplication looks for) doesn't count against "at the end" — the
// version is still detected right before it, and the marker itself is kept
// in the displayed name since it's a meaningful duplicate signal on its
// own, not noise to hide. Never touches mod.name — purely rendering.
const VERSION_SUFFIX_RE =
  /^(.*?)[\s_]*(?:[-–]\s*)?[vV]\.?\s*(\d+(?:\.\d+){0,3})$|^(.*?)[\s_-]+(\d+(?:\.\d+){1,3})$/;

// Many CC/script names are prefixed with a run of "!"/"~" purely to force
// alphabetical sort-to-top in a file browser (Windows Explorer, a Downloads
// folder) — meaningless inside SimsLink's own sorting, and it blocks a
// "[XX]"/"(XX)" prefix or a shared series prefix sitting right behind it
// from ever being recognized (e.g. "![L]_PettyExes..." never matched the
// "[L]" prefix below without this). A leading "1_"/"01."-style index number
// is the same idea with digits instead of punctuation — e.g. three real
// "1_[SS] ..." mods in one library landed in their own bogus "1 [SS]"
// cluster instead of joining the 23 other "[SS]"-prefixed mods, since the
// leading "1_" blocked splitNamePrefix() below from ever seeing the
// bracket. Deliberately narrow: only strips a digit run immediately
// followed by "_"/"." (an unambiguous filename-style separator), never a
// plain space — a real title can legitimately start with a number that way
// ("100 Baby Challenge", "3 Wishes"...) and there's no reliable way to tell
// the two apart from the text alone. Both stripped first, ahead of every
// other step; never touches mod.name.
function stripLeadingNoise(name) {
  return (name || "").replace(/^[!~]+\s*/, "").replace(/^\d{1,3}[_.]+\s*/, "");
}

// French CurseForge translations overwhelmingly append a standardized
// credit suffix — "- Traduction FR par <translator> (<date>)", sometimes
// with an underscore instead of " - " as the separator — the single most
// common source of clutter in that kind of library. Stripped before version
// detection below, since a version, when present, sits right before this
// suffix rather than at the very end of the raw name ("DynamicTeenLife
// v2.9.5 - Traduction FR par Kimikosoma (12-07-2025)") — VERSION_SUFFIX_RE
// only matches a trailing version, so this had to go first for that case to
// be recognized at all. Display-only, same as everything else here — never
// a grouping/clustering signal on its own (a translator credit says nothing
// about the original mod's author).
const TRANSLATION_CREDIT_SUFFIX_RE = /[\s_.\-–—]*Traduction\s+FR\s+par\s+.+$/i;

// Same signal as TRANSLATION_CREDIT_SUFFIX_RE above, surfaced separately as
// a badge (see buildBadges()) rather than only ever hidden from the title —
// a user browsing a big library wants to know at a glance which mods are
// themselves a fan translation of another mod, not just have the credit
// text cleaned out of the name. Deliberately looser than the suffix-strip
// regex (no "par"/"by <translator>" required) so it also catches names that
// just say "Traduction"/"Translation" without the full credit sentence;
// tests the raw mod.name, so it still fires with "Simplified names" off.
const TRANSLATION_NAME_RE = /\btraduction\b|\btranslation\b/i;

function isTranslationByName(mod) {
  return TRANSLATION_NAME_RE.test(mod.name || "");
}

function splitNameVersion(name) {
  const withoutNoise = stripLeadingNoise((name || "").trim());
  const withoutCredit = withoutNoise.replace(TRANSLATION_CREDIT_SUFFIX_RE, "").trim() || withoutNoise;

  const dupMatch = /^(.*?)(\s*\(\d+\))$/.exec(withoutCredit);
  const dupSuffix = dupMatch ? dupMatch[2].trim() : "";
  const withoutDup = dupMatch ? dupMatch[1] : withoutCredit;

  const match = VERSION_SUFFIX_RE.exec(withoutDup);
  if (!match) return { displayName: withoutCredit, version: null };

  const rest = (match[1] !== undefined ? match[1] : match[3]).trim();
  const version = match[1] !== undefined ? match[2] : match[4];
  if (!rest) return { displayName: withoutCredit, version: null };

  return { displayName: dupSuffix ? `${rest} ${dupSuffix}` : rest, version };
}

// Best-effort fallback for mods with neither a real `author` (no CurseForge
// metadata in Assisted Mode) nor a "[XX]"/"(XX)" name prefix: many mod
// series share a leading word-sequence (e.g. "adeepindigo_gameplaymods_...",
// "LIN-DIAN 20230530_HAIR SET" / "LIN-DIAN 20230910_Crocs_Set", "Slice of
// Life ... Beauty Features PL" / "Slice of Life ... Fun Juice Features PL").
// Splitting only on the *first* delimiter would cut "LIN-DIAN" in half at
// its internal hyphen, and would never notice a multi-word series name like
// "Slice of Life" — so instead this clusters on the longest run of leading
// word-segments (split on underscore/whitespace only; a hyphen stays part of
// its segment, e.g. "LIN-DIAN" is one segment) shared by 2+ mods, trying 4
// segments down to 1 and greedily removing whatever already matched at a
// longer length before trying shorter ones. Never written back as
// mod.author; purely a rendering/grouping aid.
const MAX_INFERRED_PREFIX_SEGMENTS = 4;
const MIN_INFERRED_PREFIX_CHARS = 3;

function nameSegments(name) {
  return (name || "").trim().split(/[_\s]+/).filter(Boolean);
}

// Everything after the k-th run of underscores/whitespace in `name` — finds
// the same split point nameSegments() would have used for that mod's own
// text, rather than assuming every mod in a cluster used identical
// delimiters (one might use a space where another used an underscore).
function sliceAfterSegments(name, k) {
  const trimmed = (name || "").trim();
  const re = /[_\s]+/g;
  let count = 0;
  let match;
  while ((match = re.exec(trimmed)) !== null) {
    count += 1;
    if (count === k) return trimmed.slice(match.index + match[0].length).trim();
  }
  return "";
}

// mod.name with any trailing version suffix already stripped (see
// splitNameVersion below) — computed once and threaded through the
// author/prefix clustering that follows, so a version number never gets
// mistaken for (or absorbed into) a shared segment. Without this, two mods
// differing only by version (e.g. "CoolHairSet_v6.9.4" and
// "CoolHairSet_v6.9.4(1)") would cluster on "CoolHairSet" as if it were a
// shared author/series prefix, leaving nothing but the version number
// itself as the displayed name.
function baseName(mod) {
  return splitNameVersion(mod.name).displayName;
}

// A "Name - Rest" mod (the very common " - "-separated convention) tokenizes
// (see nameSegments()) with the lone "-" as its own segment — so a 2-segment
// prefix match ends up as "Adeepindigo -" instead of clean "Adeepindigo".
// Stripped off whichever prefix text is about to become a key/display, so
// it never leaks into a group header or into the segment-count tie-break
// below (a k that only differs from a shorter one by this trailing dash no
// longer looks like a "longer, more descriptive" match).
function stripTrailingDashSegment(text) {
  return text.replace(/\s*[-–—]+$/, "").trim();
}

function computeInferredAuthorTags(mods) {
  // A mod with a manual namespace_override (see the header edit icon) is
  // excluded here even though groupingAuthor() would never consult its
  // inferred tag anyway (the override always wins first) — its *segments*
  // must not silently keep influencing which casing wins for an unrelated
  // mod that's still auto-clustered under the same lowercase key. Without
  // this, correcting one mod's grouping could flip the label shown on every
  // other mod still relying on the auto-inferred one.
  const candidates = mods.filter(
    (mod) =>
      !(mod.namespace_override || "").trim() &&
      !(mod.author || "").trim() &&
      !splitNamePrefix(baseName(mod)).prefix
  );
  const segmentsById = new Map(candidates.map((mod) => [mod.id, nameSegments(baseName(mod))]));

  // groupsByK[k - 1] = lowercase-prefix -> { variants: casing -> count, ids } for that segment count.
  const groupsByK = [];
  for (let k = 1; k <= MAX_INFERRED_PREFIX_SEGMENTS; k++) {
    const groups = new Map();
    candidates.forEach((mod) => {
      const segments = segmentsById.get(mod.id);
      if (segments.length <= k) return; // nothing would be left of the name after stripping
      const display = stripTrailingDashSegment(segments.slice(0, k).join(" "));
      if (display.replace(/\s+/g, "").length < MIN_INFERRED_PREFIX_CHARS) return;
      const key = display.toLowerCase();
      if (!groups.has(key)) groups.set(key, { variants: new Map(), ids: [] });
      const group = groups.get(key);
      group.variants.set(display, (group.variants.get(display) || 0) + 1);
      group.ids.push(mod.id);
    });
    groupsByK.push(groups);
  }

  // Whichever exact casing appears most often among the group's members
  // wins the displayed label — not just whichever mod happened to be
  // iterated first (a name-casing lottery that depends on nothing more
  // meaningful than array order). A tie keeps the first-seen variant (Map
  // preserves insertion order).
  function majorityCasing(variants) {
    let best = null;
    for (const [text, count] of variants) {
      if (!best || count > best.count) best = { text, count };
    }
    return best.text;
  }

  // Per mod, pick whichever segment count covers the *largest* number of
  // mods — not the longest matching prefix. A short, shared brand tag
  // ("adeepindigo", 14 mods) should win over a longer but narrower
  // sub-family match ("adeepindigo gameplaymods", 8 mods) that would
  // otherwise needlessly fragment one author into several headers. Ties
  // (same coverage at more than one length, e.g. an author whose every mod
  // happens to share a longer common run too) prefer the longer, more
  // descriptive prefix — later, larger k values overwrite on a tie below.
  const tags = new Map();
  candidates.forEach((mod) => {
    const segments = segmentsById.get(mod.id);
    let best = null;
    for (let k = 1; k <= MAX_INFERRED_PREFIX_SEGMENTS && segments.length > k; k++) {
      const key = stripTrailingDashSegment(segments.slice(0, k).join(" ")).toLowerCase();
      const group = groupsByK[k - 1].get(key);
      if (!group || group.ids.length < 2) continue;
      if (!best || group.ids.length >= best.size)
        best = { display: majorityCasing(group.variants), size: group.ids.length, k };
    }
    if (best) tags.set(mod.id, { display: best.display, segmentCount: best.k });
  });
  return tags;
}

// The single label used for sorting, the group header, and the tile
// abbreviation: a confirmed author always wins, then a "[XX]" name prefix,
// then the inferred fallback above. `inferredTags` is computed once per
// render() over the whole visible set (clustering needs to see all mods at
// once). `displayName` is `mod.name` with whichever prefix was used to
// derive `label` stripped off — never done for a confirmed `mod.author`,
// since that's separate metadata, not text embedded in the name.
function groupingAuthor(mod, inferredTags) {
  const { displayName: base, version } = splitNameVersion(mod.name);
  // A manual correction (see #author-group-header's edit icon) always wins
  // — it exists specifically to override a wrong guess below, including a
  // real mod.author (a user might reasonably want to group differently
  // from CurseForge's own author field for their own purposes). Only the
  // grouping label changes; displayName/version stay driven by the actual
  // name, same as any other mod.
  const override = (mod.namespace_override || "").trim();
  if (override) return { label: override, displayName: base, version };
  const author = (mod.author || "").trim();
  if (author) return { label: author, displayName: base, version };
  const { prefix, rest } = splitNamePrefix(base);
  if (prefix) return { label: prefix, displayName: rest, version };
  const inferred = inferredTags.get(mod.id);
  if (inferred) {
    const stripped = sliceAfterSegments(base, inferred.segmentCount);
    return { label: inferred.display, displayName: stripped || base, version };
  }
  return { label: "", displayName: base, version };
}

// A mod linked to CurseForge (curseforge_match.py, or a Direct Mode catalog
// install) may carry its real, human-authored name separately from `name`
// (which for a loose import is just whatever local text it was adopted
// under, e.g. a raw filename — see migration 10). That real name always
// wins over anything derived/guessed from the local one, and — since it's
// already correct, not something to clean up — completely bypasses the
// "Simplified names" toggle below.
//
// Otherwise, toggled by the "Simplified names" switch next to the view
// switch — affects only the title text shown on a card/row. Grouping
// (author headers, sort order, the avatar label) and the version badge stay
// driven by groupingAuthor() either way, since those aren't "the mod's
// name" as far as this toggle is concerned, just organizational metadata
// derived from it. Underscores left over after the prefix/version/series
// stripping (e.g. "HAIR_SET", "Crocs_Set") are swapped for spaces too —
// only in simplified mode, never touching the raw name shown when the
// toggle is off. A leading "-"/"–" is also trimmed: sliceAfterSegments()
// (see groupingAuthor()) cuts right after the shared prefix's last
// delimiter, which is often itself a " - " separator (e.g. "Kiara - Baker"
// clustered on "Kiara" leaves "- Baker" behind) — without this the dash
// would show up stuck to the front of an otherwise-clean title.
function titleText(mod, displayName) {
  if (mod.curseforge_name) return mod.curseforge_name;
  if (!state.simplifiedNames) return mod.name;
  return displayName
    .replace(/_+/g, " ")
    .replace(/^[\s\-–—]+/, "")
    .replace(/\s+/g, " ")
    .trim();
}

function showError(bannerId, message) {
  const banner = document.getElementById(bannerId);
  banner.textContent = message;
  banner.classList.add("show");
  clearTimeout(banner._errorTimer);
  banner._errorTimer = setTimeout(() => banner.classList.remove("show"), 5000);
}

async function apiRequest(path, options) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch (_) {
      /* no JSON body */
    }
    const err = new Error(detail);
    // Not read by most callers, but lets a caller distinguish e.g. a 502
    // (transient upstream failure, worth retrying) from other errors —
    // see runMatchCurseforgeStep().
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}

// --- status / mode pill ------------------------------------------------------

async function loadStatus() {
  state.status = await apiRequest("/api/status");
  renderStatus();
}

function renderStatus() {
  const status = state.status;
  if (!status) return;
  const gem = document.getElementById("modeGem");
  const label = document.getElementById("modeLabel");
  const pill = document.getElementById("modePill");

  gem.classList.toggle("pulse", status.direct_mode);
  gem.classList.toggle("warn", !status.direct_mode);
  label.textContent = status.direct_mode ? t("mode.direct_label") : t("mode.assisted_label");
  pill.title = status.direct_mode ? t("mode.direct_banner") : t("mode.assisted_banner");

  document.getElementById("footVersion").textContent =
    `v${status.app_version} · GAME ${status.game_version || "—"}`;

  // Catalog has genuinely nothing to show without a CurseForge key (just an
  // empty notice) — hide its nav entry entirely rather than link to an
  // empty view. Updates keeps its nav entry: its manual checklist (linked
  // mods + "Check on CurseForge" links) still has real content in Assisted
  // Mode, unlike Catalog.
  document.querySelector('.nav-item[data-view="catalog"]').hidden = !status.direct_mode;
  // "Try to identify mods" (openMatchCurseforgeModal()) needs a real
  // CurseForge connection to mean anything.
  document.getElementById("headerMatchCurseforgeButton").hidden = !status.direct_mode;
  // Same reasoning for "Synchronize" (openSyncCurseforgeModal()).
  document.getElementById("headerSyncCurseforgeButton").hidden = !status.direct_mode;
}

// --- mod list / grid ----------------------------------------------------------

async function fetchLibraryData() {
  const [
    mods,
    conflicts,
    blacklistMatches,
    brokenMods,
    rezippedMods,
    missingMods,
    looseSuggestions,
    compatQuarantinePreview,
  ] = await Promise.all([
    apiRequest("/api/mods"),
    apiRequest("/api/conflicts"),
    apiRequest("/api/blacklist/matches"),
    apiRequest("/api/mods/broken"),
    apiRequest("/api/mods/rezipped"),
    apiRequest("/api/mods/missing"),
    apiRequest("/api/mods/loose/suggested-groups"),
    apiRequest("/api/compat/quarantine/preview"),
  ]);
  return {
    mods,
    conflicts,
    blacklistMatches,
    brokenMods,
    rezippedMods,
    missingMods,
    looseSuggestions,
    compatQuarantinePreview,
  };
}

function applyLibraryData(data) {
  state.mods = data.mods;
  state.conflicts = data.conflicts;
  state.blacklistMatches = data.blacklistMatches;
  state.brokenMods = data.brokenMods;
  state.rezippedMods = data.rezippedMods;
  state.missingMods = data.missingMods;
  state.looseSuggestions = data.looseSuggestions;
  state.compatQuarantinePreview = data.compatQuarantinePreview;
  // Shared with pollLibrary() below: lets a background poll tell whether
  // anything actually changed since the last fetch (by either path) before
  // paying for a re-render.
  state.librarySnapshot = JSON.stringify(data);
}

// A tracked mod whose real library folder was manually rezipped in place
// (see backend/broken_mods.py's scan_rezipped_mods() docstring — e.g. dezip
// via the app, then rezip directly in the game's Mods/<mod_id>/ folder,
// which is a symlink straight into that same library folder). Returns the
// archive path(s) found there, or null when this mod isn't in that state.
function rezippedZipPaths(modId) {
  const entry = state.rezippedMods.find((r) => r.mod_id === modId);
  return entry ? entry.zip_paths : null;
}

function rezippedModIds() {
  return new Set(state.rezippedMods.map((r) => r.mod_id));
}

async function loadMods() {
  applyLibraryData(await fetchLibraryData());
}

// Picks up external changes to Mods/ (manual edits, another tool) without
// requiring a page reload — the backend's own Mods/ watcher already reacts
// within ~2s (see CLAUDE.md's "Startup scan"), but nothing previously told
// an already-open Library tab to re-fetch and reflect that. Deliberately
// plain polling rather than a WebSocket/SSE push: this is a local,
// single-user app already comfortable with the same tradeoff for Assisted
// Mode's download detection (see startDownloadPolling()) — a few seconds of
// latency costs nothing here, and polling avoids a connection lifecycle to
// manage for no visible benefit.
let libraryPollInFlight = false;

function startLibraryPolling() {
  setInterval(pollLibrary, LIBRARY_POLL_INTERVAL_MS);
}

async function pollLibrary() {
  // Skip while the Library tab isn't even visible, and never let a slow
  // response cause overlapping requests to pile up behind it.
  if (libraryPollInFlight || document.getElementById("view-library").hidden) return;
  libraryPollInFlight = true;
  try {
    const data = await fetchLibraryData();
    if (JSON.stringify(data) !== state.librarySnapshot) {
      applyLibraryData(data);
      render();
    }
  } catch (err) {
    // Silent — a transient failure during a background poll shouldn't
    // interrupt the user with an error banner; the next tick (or the next
    // action-triggered loadMods()) resyncs as usual.
  } finally {
    libraryPollInFlight = false;
  }
}

// The single-select "Cassés/Zip/Doublons/Normaux/Désactivés/Tout" state
// filter (#stateFilterToggle) — buckets a real mod into exactly one
// category, same priority order as modTier() (a rezipped mod outranks
// disabled, which outranks a flagged "problem" mod, which outranks normal).
// Never returns "broken": that bucket only ever applies to an unmanaged
// broken folder (see visibleBrokenModFolders()), never a real tracked mod
// row — filtering real mods for it naturally yields an empty result, which
// is correct. "duplicate" reuses problemModIds()'s population, which also
// includes blacklist.py pattern matches alongside actual duplicate signals
// (see problemModIds()) — both already share the same orange "problem"
// card treatment everywhere else in the app.
function stateFilterBucket(mod, problems, rezipped) {
  if (rezipped.has(mod.id)) return "zip";
  if (!mod.active) return "disabled";
  if (problems.has(mod.id)) return "duplicate";
  return "normal";
}

function visibleMods() {
  const query = state.filterQuery.trim().toLowerCase();
  let base = state.looseOnlyFilter ? state.mods.filter((m) => m.is_loose_import) : state.mods;
  if (state.translationOnlyFilter) base = base.filter(isTranslationByName);
  if (state.linkedOnlyFilter) base = base.filter((m) => m.curseforge_id);
  if (state.incompatibleOnlyFilter) base = base.filter((m) => m.compat_status === "incompatible");
  if (state.typeFilter) base = base.filter((m) => m.primary_type === state.typeFilter);
  if (state.stateFilter) {
    const problems = problemModIds();
    const rezipped = rezippedModIds();
    base = base.filter((m) => stateFilterBucket(m, problems, rezipped) === state.stateFilter);
  }
  if (!query) return base;
  return base.filter(
    (m) => m.name.toLowerCase().includes(query) || (m.author || "").toLowerCase().includes(query)
  );
}

// Rebuilds a collapsible banner's toggle button as [chevron, text] instead
// of plain textContent, so the chevron can rotate with expand/collapse
// state — same visual language as buildAuthorHeader()'s own chevron.
// `.collapsed` (list hidden) points right; expanded points down (the
// SVG's natural, unrotated orientation).
function setCollapseToggleContent(buttonId, text, expanded) {
  const btn = document.getElementById(buttonId);
  btn.classList.toggle("collapsed", !expanded);
  btn.innerHTML = "";
  const chevron = document.createElement("span");
  chevron.className = "chevron";
  chevron.innerHTML = CHEVRON_ICON_SVG;
  btn.appendChild(chevron);
  btn.appendChild(document.createTextNode(text));
}

function renderWarnings() {
  // Game-wide, not mod-specific — always a single line, not part of the
  // collapsible list below.
  document.getElementById("scriptModsWarning").hidden = !(
    state.status && state.status.script_mods_allowed === false
  );

  // Real conflicts (duplicate files, script collisions, folder duplication,
  // blacklist matches) used to also list here, but every one of them is
  // already surfaced on the mod's own card/row (red border, "Duplicate" tag)
  // and expanded in its detail panel — repeating them here was redundant
  // now that that visual treatment exists. Unmanaged broken mod folders
  // (broken_mods.py) mostly stay here: they have no card at all to attach a
  // highlight to, since they were never actually installed. 'unpacked_script'
  // and 'unextracted_archive' are the exceptions — both get their own red
  // pseudo-mod card in the grid (see visibleBrokenModFolders(),
  // brokenFolderPseudoMod()/buildCard()) since a repair action makes sense
  // to attach to them, so they're excluded here to avoid saying the same
  // thing twice. 'empty'/'unrecognized' stay banner-only.
  const banner = document.getElementById("conflictsBanner");
  const list = document.getElementById("conflictsList");
  const bannerFolders = state.brokenMods.filter(
    (folder) => folder.reason !== "unpacked_script" && folder.reason !== "unextracted_archive"
  );
  const totalCount = bannerFolders.length;

  if (!totalCount) {
    banner.hidden = true;
    list.innerHTML = "";
    // The list being genuinely empty (every entry fixed/deleted) is what
    // "cleared" means for a dismissal to expire — a *new* problem showing
    // up later should never be silently suppressed by a stale dismiss.
    state.conflictsBannerDismissed = false;
    return;
  }
  if (state.conflictsBannerDismissed) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;

  // No chevron here, unlike every other collapsible banner — this one's
  // heading reads fine as plain text and the row-level bullets (see
  // style.css's --row-accent) already give the list enough visual identity
  // without it.
  document.getElementById("conflictsToggle").textContent = t("library.conflicts.toggle", { count: totalCount });

  list.hidden = !state.conflictsExpanded;
  list.innerHTML = "";
  // Single line per row: folder name, then its sample files after a ">" —
  // 'unrecognized' content this app never fully classified is exactly the
  // case where a wrong guess is most likely, so no delete action is offered
  // here at all anymore (too risky to remove sight-unseen) — only a way to
  // go look at it for real in the file manager before deciding by hand.
  bannerFolders.forEach((folder) => {
    const row = document.createElement("div");
    row.className = "conflict-row warning-list-row--tight";

    let label = folder.name;
    if (folder.sample_files.length) {
      const remaining = folder.file_count - folder.sample_files.length;
      label += " > " + folder.sample_files.join(", ") + (remaining > 0 ? ` +${remaining}…` : "");
    }
    const info = document.createElement("div");
    info.className = "info";
    info.appendChild(elementWithText("span", "name", label));
    row.appendChild(info);

    const viewBtn = document.createElement("button");
    viewBtn.type = "button";
    viewBtn.className = "icon-btn";
    viewBtn.title = t("library.conflicts.open_folder_button_title");
    viewBtn.innerHTML = SEARCH_ICON_SVG;
    viewBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      doOpenBrokenFolder(folder.name);
    });
    row.appendChild(viewBtn);

    list.appendChild(row);
  });
}

async function doOpenBrokenFolder(name) {
  try {
    await apiRequest(`/api/mods/broken/${encodeURIComponent(name)}/open`, { method: "POST" });
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// --- broken/unzip/duplicates summary banners --------------------------------------
//
// Same collapsible, session-dismissible pattern as the "Fichiers à
// nettoyer" banner above (renderWarnings()'s conflictsBanner), generalized
// here since it's now shared by three more populations. `key` indexes into
// state.bannerExpanded/bannerDismissed. `buildList` only runs when the
// banner is actually going to show (count > 0, not dismissed) — no need to
// build rows for content nobody can see.
function renderBanner(key, { bannerId, toggleId, listId, count, toggleText, buildList }) {
  const banner = document.getElementById(bannerId);
  const list = document.getElementById(listId);
  if (!count) {
    banner.hidden = true;
    list.innerHTML = "";
    // Cleared the moment the list is genuinely empty — a later, new
    // problem should never be silently suppressed by a stale dismissal.
    state.bannerDismissed[key] = false;
    return;
  }
  if (state.bannerDismissed[key]) {
    banner.hidden = true;
    return;
  }
  banner.hidden = false;
  setCollapseToggleContent(toggleId, toggleText, state.bannerExpanded[key]);
  list.hidden = !state.bannerExpanded[key];
  list.innerHTML = "";
  buildList(list);
}

function wireBanner(key, toggleId, dismissBtnId, rerender) {
  document.getElementById(toggleId).addEventListener("click", () => {
    state.bannerExpanded[key] = !state.bannerExpanded[key];
    rerender();
  });
  document.getElementById(dismissBtnId).addEventListener("click", (e) => {
    e.stopPropagation();
    state.bannerDismissed[key] = true;
    rerender();
  });
}

// Builds the common [name + sub-line of files, open/action icons] row shape
// shared by the broken-mods and unzip banners below — `filesText` is
// whatever's most relevant to show (sample_files for broken/unrecognized,
// zip_paths for anything unzip-related), `extraButtons` is the reason-
// specific action(s) before the trailing delete icon.
function buildWarningListRow(name, filesText, extraButtons, deleteBtn) {
  const row = document.createElement("div");
  row.className = "warning-list-row";
  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("span", "name", name));
  if (filesText) info.appendChild(elementWithText("span", "files", filesText));
  row.appendChild(info);
  extraButtons.forEach((btn) => btn && row.appendChild(btn));
  if (deleteBtn) row.appendChild(deleteBtn);
  return row;
}

function renderBrokenModsBanner() {
  const folders = state.brokenMods.filter((f) => f.reason === "unpacked_script");
  renderBanner("broken", {
    bannerId: "brokenModsBanner",
    toggleId: "brokenModsToggle",
    listId: "brokenModsList",
    count: folders.length,
    toggleText: t("library.broken_banner.toggle", { count: folders.length }),
    buildList: (list) => {
      folders.forEach((folder) => {
        const remaining = folder.file_count - folder.sample_files.length;
        const filesText = folder.sample_files.length
          ? folder.sample_files.join(", ") + (remaining > 0 ? ` +${remaining}…` : "")
          : "";
        const openBtn = document.createElement("button");
        openBtn.type = "button";
        openBtn.className = "icon-btn";
        openBtn.title = t("library.conflicts.open_folder_button_title");
        openBtn.innerHTML = FOLDER_ICON_SVG;
        openBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          doOpenBrokenFolder(folder.name);
        });
        list.appendChild(
          buildWarningListRow(
            folder.name,
            filesText,
            [openBtn, buildBrokenActionButton(folder)],
            buildDeleteBrokenButton(folder)
          )
        );
      });
    },
  });
}

// Combines two different populations that both mean "needs unzipping" from
// a user's point of view, even though they're two different backend
// concepts: an unmanaged folder with a zip dropped in but never extracted
// (broken_mods.py's 'unextracted_archive'), and an already-tracked mod
// whose real library folder got manually rezipped in place (see
// scan_rezipped_mods()'s module docstring). Each gets its own reason-
// specific fix action, but they share one banner/bulk-action since the
// user-facing story ("go unzip these") is the same either way.
function renderUnzipBanner() {
  const unmanaged = state.brokenMods.filter((f) => f.reason === "unextracted_archive");
  const rezipped = state.rezippedMods;
  const totalCount = unmanaged.length + rezipped.length;
  const bulkEligible = unzipBulkEligible();

  const bulkBtn = document.getElementById("unzipAllButton");
  bulkBtn.hidden = bulkEligible.length === 0;
  bulkBtn.textContent = t("library.unzip_banner.unzip_all_button", { count: bulkEligible.length });

  renderBanner("unzip", {
    bannerId: "unzipBanner",
    toggleId: "unzipToggle",
    listId: "unzipList",
    count: totalCount,
    toggleText: t("library.unzip_banner.toggle", { count: totalCount }),
    buildList: (list) => {
      unmanaged.forEach((folder) => {
        list.appendChild(
          buildWarningListRow(
            folder.name,
            folder.zip_paths.join(", "),
            [buildBrokenActionButton(folder)],
            buildDeleteBrokenButton(folder)
          )
        );
      });
      rezipped.forEach((entry) => {
        const mod = { id: entry.mod_id, name: entry.name };
        const deleteBtn = document.createElement("button");
        deleteBtn.type = "button";
        deleteBtn.className = "icon-btn delete-icon-btn";
        deleteBtn.title = t("library.delete_button_title");
        deleteBtn.innerHTML = DELETE_ICON_SVG;
        deleteBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          confirmDelete(mod);
        });
        list.appendChild(
          buildWarningListRow(entry.name, entry.zip_paths.join(", "), [buildRezipActionButton(mod, entry.zip_paths)], deleteBtn)
        );
      });
    },
  });
}

// Only entries with exactly one archive qualify — same restraint
// brokenActionSpec()/buildRezipActionButton() already apply individually
// (2+ zips is ambiguous, no safe guess at which is "the" content), so bulk
// "unzip all" only ever fires actions that were already safe/unambiguous
// one at a time.
function unzipBulkEligible() {
  return [
    ...state.brokenMods.filter((f) => f.reason === "unextracted_archive" && f.zip_paths.length === 1),
    ...state.rezippedMods.filter((r) => r.zip_paths.length === 1),
  ];
}

function confirmUnzipAll() {
  const eligible = unzipBulkEligible();
  if (!eligible.length) return;
  openConfirmModal({
    title: t("library.unzip_banner.unzip_all_confirm.title"),
    message: t("library.unzip_banner.unzip_all_confirm.message", { count: eligible.length }),
    confirmLabel: t("library.unzip_banner.unzip_all_confirm.confirm"),
    onConfirm: () => doUnzipAll(eligible),
  });
}

async function doUnzipAll(eligible) {
  const errors = [];
  for (const entry of eligible) {
    try {
      if (entry.mod_id) {
        // A rezipped, already-tracked mod (state.rezippedMods shape).
        await apiRequest(`/api/mods/${encodeURIComponent(entry.mod_id)}/fix-rezip`, { method: "POST" });
      } else {
        // An unmanaged broken folder (state.brokenMods shape).
        await apiRequest(`/api/mods/broken/${encodeURIComponent(entry.name)}/fix`, { method: "POST" });
      }
    } catch (err) {
      errors.push(err.message);
    }
  }
  closeConfirm();
  await loadMods();
  render();
  if (errors.length) {
    showError("errorBanner", t("library.unzip_banner.unzip_all_partial_error", { errors: errors.join("; ") }));
  }
}

// Strips a trailing separator/parenthesis fragment left over after slicing
// text down to a shared prefix — e.g. "Mod (1)"/"Mod (2)" share "Mod ("
// character-for-character, which would otherwise render with a dangling
// unmatched "(".
function trimTrailingSeparators(text) {
  return text.replace(/[\s_\-(]+$/, "");
}

// Two mods flagged as duplicates overwhelmingly share the exact same
// display name (that's the whole point — see conflict_detector.py) —
// showing both in full ("Realistic Birth Mod ↔ Realistic Birth Mod") was
// pure repetition. Collapsing to the shared part plus a count is both
// shorter and actually informative when the names do differ slightly.
function duplicateGroupLabel(group) {
  const common = trimTrailingSeparators(longestCommonPrefix(group.mods.map((m) => m.name)));
  const label = common.length >= 3 ? common : group.mods[0].name;
  return t("library.duplicates_banner.row_label", { name: label, count: group.mods.length });
}

function renderDuplicatesBanner() {
  const groups = state.conflicts.filter((g) => g.kind === "exact_duplicate_mod" || g.kind === "folder_duplication");
  const resolvableCount = duplicateTagModIds().size;

  const bulkBtn = document.getElementById("resolveDuplicatesBulkButton");
  bulkBtn.hidden = groups.length === 0;
  bulkBtn.disabled = resolvableCount === 0;
  bulkBtn.title = resolvableCount === 0 ? t("library.duplicates_banner.resolve_all_none_title") : "";
  bulkBtn.textContent = t("library.duplicates_banner.resolve_all_button", { count: resolvableCount });

  renderBanner("duplicates", {
    bannerId: "duplicatesBanner",
    toggleId: "duplicatesToggle",
    listId: "duplicatesList",
    count: groups.length,
    toggleText: t("library.duplicates_banner.toggle", { count: groups.length }),
    buildList: (list) => {
      groups.forEach((group) => {
        const resolveBtn = document.createElement("button");
        resolveBtn.className = "icon-btn";
        resolveBtn.title = t("library.duplicates_banner.resolve_button_title");
        resolveBtn.innerHTML = REPAIR_ICON_SVG;
        resolveBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openDuplicateComparison(group.mods[0].id);
        });
        const row = buildWarningListRow(duplicateGroupLabel(group), "", [resolveBtn], null);
        row.classList.add("warning-list-row--tight");
        list.appendChild(row);
      });
    },
  });
}

// Only ever acts on duplicateTagModIds() — the subset of duplicate groups
// where an author/name-suffix signal already lets the app presume which
// side is the redundant copy (see that function's own comment). A group
// with no such signal (both sides equally unknown, e.g. neither has
// CurseForge author data — common in Assisted Mode) is never auto-picked;
// those stay manual-only via the per-group "Comparer" button/click-to-
// compare flow, same "suspicion is not confirmation" rule as everywhere
// else in this app.
function confirmResolveDuplicatesBulk() {
  const resolvableIds = [...duplicateTagModIds()];
  if (!resolvableIds.length) return;
  const { choice, deleteRadio } = buildDisableDeleteChoice();
  openConfirmModal({
    title: t("library.duplicates_banner.resolve_all_confirm.title"),
    message: t("library.duplicates_banner.resolve_all_confirm.message", { count: resolvableIds.length }),
    extraNodes: [choice],
    confirmLabel: t("library.duplicates_banner.resolve_all_confirm.confirm"),
    onConfirm: () => doResolveDuplicatesBulk(resolvableIds, deleteRadio.checked ? "delete" : "disable"),
  });
}

async function doResolveDuplicatesBulk(modIds, action) {
  const errors = await applyActionToMods(modIds, action);
  closeConfirm();
  await loadMods();
  render();
  if (action === "disable" && errors.length < modIds.length) await suggestCacheCleanup();
  if (errors.length) {
    showError("errorBanner", t("library.duplicates_banner.resolve_all_partial_error", { errors: errors.join("; ") }));
  }
}

// compat_quarantine.py's preview — active mods flagged incompatible with the
// current game version, plus (best-effort — see that module's docstring)
// active mods that locally required-depend on one. Unlike duplicatesBanner's
// "resolve unambiguous" bulk button, this one never disables anything with a
// single click: the dependency cascade isn't guaranteed exhaustive, so
// openQuarantineConfirmButton always opens a checkbox-per-mod confirm step
// first (confirmQuarantineMods()) instead of a silent default.
function quarantineReasonText(candidate, all) {
  if (candidate.reason === "incompatible") {
    return t("library.compat_quarantine_banner.reason_incompatible");
  }
  const parent = all.find((c) => c.mod_id === candidate.reason);
  return t("library.compat_quarantine_banner.reason_depends_on", {
    name: parent ? parent.name : candidate.reason,
  });
}

function renderCompatQuarantineBanner() {
  const candidates = state.compatQuarantinePreview;

  const reviewBtn = document.getElementById("openQuarantineConfirmButton");
  reviewBtn.hidden = candidates.length === 0;
  reviewBtn.textContent = t("library.compat_quarantine_banner.review_button", { count: candidates.length });

  renderBanner("compat", {
    bannerId: "compatQuarantineBanner",
    toggleId: "compatQuarantineToggle",
    listId: "compatQuarantinePreviewList",
    count: candidates.length,
    toggleText: t("library.compat_quarantine_banner.toggle", { count: candidates.length }),
    buildList: (list) => {
      candidates.forEach((candidate) => {
        list.appendChild(
          buildWarningListRow(candidate.name, quarantineReasonText(candidate, candidates), [], null)
        );
      });
    },
  });
}

// Every candidate is pre-checked (unlike confirmExtractZips()'s "pick one
// variant" picker, this is the full computed set — deselecting is the
// exception, not choosing among alternatives) so a user can still exclude a
// mod they know is fine before confirming, without the cascade's best-effort
// nature ever being applied unattended.
function confirmQuarantineMods() {
  const candidates = state.compatQuarantinePreview;
  if (!candidates.length) return;
  const checkboxByModId = new Map();
  const list = document.createElement("div");
  list.className = "zip-choice-list";
  candidates.forEach((candidate) => {
    const label = document.createElement("label");
    label.className = "zip-choice";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = true;
    checkboxByModId.set(candidate.mod_id, checkbox);
    label.appendChild(checkbox);
    label.appendChild(
      document.createTextNode(`${candidate.name} — ${quarantineReasonText(candidate, candidates)}`)
    );
    list.appendChild(label);
  });

  openConfirmModal({
    title: t("library.compat_quarantine_banner.confirm.title"),
    message: t("library.compat_quarantine_banner.confirm.message", { count: candidates.length }),
    extraNodes: [list],
    confirmLabel: t("library.compat_quarantine_banner.confirm.confirm"),
    onConfirm: () => {
      const selected = candidates
        .map((c) => c.mod_id)
        .filter((modId) => checkboxByModId.get(modId).checked);
      if (!selected.length) {
        showError("errorBanner", t("library.compat_quarantine_banner.confirm.none_selected"));
        return;
      }
      doQuarantineMods(selected);
    },
  });
}

async function doQuarantineMods(modIds) {
  try {
    const result = await apiRequest("/api/compat/quarantine", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mod_ids: modIds }),
    });
    closeConfirm();
    await loadMods();
    render();
    if (state.settingsWired) loadCompatQuarantineManageList();
    if (result.quarantined.length) await suggestCacheCleanup();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// Same collapsible-banner pattern as the three above, for
// loose_mods.suggest_groupings()'s output — moved here from a Settings-only
// list so it's visible wherever the Library actually is, and so a group
// can be resolved through the richer multi-panel comparator (see
// openMergeComparison()) instead of a bare "name1, name2, name3" line. No
// bulk action: unlike duplicatesBanner's "resolve unambiguous" (which only
// ever acts on a signal that already picks a side), there's no safe
// unattended default for "which of these loose files should become one
// mod" — every group goes through the comparator, confirmed-CurseForge or
// not.
// "aaa (5 fichiers)" — the shared leading part of the member names plus a
// count, mirroring duplicateGroupLabel() above. Falls back to the group's
// own suggested_name when there's no real shared prefix to show (a
// curseforge_id-confirmed group's members can have entirely unrelated file
// names — see loose_mods.suggest_groupings()). The confirmed/unconfirmed
// distinction itself isn't shown here at all anymore — it's exactly what
// the comparator's own reliability banner already states once you open it
// (openMergeComparison()), so repeating it as a pill on every row was pure
// duplication.
function mergeableGroupLabel(group) {
  const common = trimTrailingSeparators(longestCommonPrefix(group.mod_names));
  const label = common.length >= 3 ? common : group.suggested_name;
  return t("library.mergeable_banner.row_label", { name: label, count: group.mod_ids.length });
}

function renderMergeableBanner() {
  const groups = state.looseSuggestions;
  renderBanner("mergeable", {
    bannerId: "mergeableBanner",
    toggleId: "mergeableToggle",
    listId: "mergeableList",
    count: groups.length,
    toggleText: t("library.mergeable_banner.toggle", { count: groups.length }),
    buildList: (list) => {
      groups.forEach((group) => {
        const mergeBtn = document.createElement("button");
        mergeBtn.className = "icon-btn";
        mergeBtn.title = t("library.mergeable_banner.merge_button_title");
        mergeBtn.innerHTML = REPAIR_ICON_SVG;
        mergeBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          openMergeComparison(group);
        });
        const row = buildWarningListRow(mergeableGroupLabel(group), "", [mergeBtn], null);
        row.classList.add("warning-list-row--tight");
        list.appendChild(row);
      });
    },
  });
}

// A mod deleted while it was still part of a saved state
// (profiles.record_missing_mod_if_saved()) — a reminder to maybe reinstall
// it, not a problem, so its own banner rather than folded into
// renderWarnings() above (see index.html's comment on #missingModsBanner).
// Each row is manually dismissible; nothing here is destructive.
function renderMissingMods() {
  const banner = document.getElementById("missingModsBanner");
  const list = document.getElementById("missingModsList");

  if (!state.missingMods.length) {
    banner.hidden = true;
    list.innerHTML = "";
    return;
  }
  banner.hidden = false;

  setCollapseToggleContent(
    "missingModsToggle",
    t("library.missing_mods.toggle", { count: state.missingMods.length }),
    state.missingModsExpanded
  );

  list.hidden = !state.missingModsExpanded;
  list.innerHTML = "";
  state.missingMods.forEach((entry) => {
    const row = document.createElement("div");
    row.className = "missing-mod-row";

    const info = document.createElement("div");
    info.className = "info";
    info.appendChild(elementWithText("span", "name", entry.name));
    info.appendChild(
      elementWithText("span", "meta", t("library.missing_mods.source_profiles", { profiles: entry.source_profile_names }))
    );
    row.appendChild(info);

    if (entry.curseforge_url) {
      const link = document.createElement("button");
      link.className = "curseforge-link";
      link.textContent = t("library.missing_mods.view_on_curseforge");
      link.addEventListener("click", (e) => {
        e.stopPropagation();
        openExternal(entry.curseforge_url);
      });
      row.appendChild(link);
    }

    const dismissBtn = document.createElement("button");
    dismissBtn.className = "icon-btn delete-icon-btn";
    dismissBtn.title = t("library.missing_mods.dismiss_button_title");
    dismissBtn.innerHTML = DELETE_ICON_SVG;
    dismissBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      doDismissMissingMod(entry.id);
    });
    row.appendChild(dismissBtn);

    list.appendChild(row);
  });
}

async function doDismissMissingMod(entryId) {
  // No confirm modal: this only clears a reminder, it doesn't touch any
  // real file or mod — unlike every other delete-icon action in this app.
  try {
    await apiRequest(`/api/mods/missing/${encodeURIComponent(entryId)}`, { method: "DELETE" });
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// A quick way to find mods adopted from a loose file at Mods/ root
// (loose_mods.py) amid an otherwise-organized library — see
// looseOnlyFilterBtn in index.html. Hidden entirely once there are none
// left (nothing to filter, no reason to advertise the button).
function renderLooseFilterChip() {
  const btn = document.getElementById("looseOnlyFilterBtn");
  const count = state.mods.filter((m) => m.is_loose_import).length;
  if (!count && !state.looseOnlyFilter) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.textContent = t("library.loose_filter_chip", { count });
  btn.classList.toggle("active", state.looseOnlyFilter);
}

// Same idea as the loose-import chip above, for TRANSLATION_NAME_RE
// matches (see isTranslationByName()/the "Traduction" badge in
// buildBadges()) — lets a user confirm which fan translations they
// actually have installed. Hidden entirely once there are none.
function renderTranslationFilterChip() {
  const btn = document.getElementById("translationOnlyFilterBtn");
  const count = state.mods.filter(isTranslationByName).length;
  if (!count && !state.translationOnlyFilter) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.textContent = t("library.translation_filter_chip", { count });
  btn.classList.toggle("active", state.translationOnlyFilter);
}

function wireTranslationFilterChip() {
  document.getElementById("translationOnlyFilterBtn").addEventListener("click", () => {
    state.translationOnlyFilter = !state.translationOnlyFilter;
    render();
  });
}

function wireLooseFilterChip() {
  document.getElementById("looseOnlyFilterBtn").addEventListener("click", () => {
    state.looseOnlyFilter = !state.looseOnlyFilter;
    render();
  });
}

// Same idea as the two chips above, for mods with a real curseforge_id
// (Direct Mode catalog install, or the header's CurseForge fingerprint-
// match popup for a loose import) — same underlying signal as a card's
// green/red link-status dot (see buildCard()). Hidden entirely once there
// are none.
function renderLinkedFilterChip() {
  const btn = document.getElementById("linkedOnlyFilterBtn");
  const count = state.mods.filter((m) => m.curseforge_id).length;
  if (!count && !state.linkedOnlyFilter) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.textContent = t("library.linked_filter_chip", { count });
  btn.classList.toggle("active", state.linkedOnlyFilter);
}

function wireLinkedFilterChip() {
  document.getElementById("linkedOnlyFilterBtn").addEventListener("click", () => {
    state.linkedOnlyFilter = !state.linkedOnlyFilter;
    render();
  });
}

// Same idea as the three chips above, for mod.compat_status === 'incompatible'
// (compat_status.py's badge classification against the installed game
// version — see curseforge.py's compat_status()) — same underlying signal
// as the card's "Incompatible" badge (see buildBadges()). Hidden entirely
// once there are none.
function renderIncompatibleFilterChip() {
  const btn = document.getElementById("incompatibleOnlyFilterBtn");
  const count = state.mods.filter((m) => m.compat_status === "incompatible").length;
  if (!count && !state.incompatibleOnlyFilter) {
    btn.hidden = true;
    return;
  }
  btn.hidden = false;
  btn.textContent = t("library.incompatible_filter_chip", { count });
  btn.classList.toggle("active", state.incompatibleOnlyFilter);
}

function wireIncompatibleFilterChip() {
  document.getElementById("incompatibleOnlyFilterBtn").addEventListener("click", () => {
    state.incompatibleOnlyFilter = !state.incompatibleOnlyFilter;
    render();
  });
}

function problemModIds() {
  // Mods already installed and flagged by conflict_detector.py or
  // blacklist.py — surfaced as a red highlight directly on their card, in
  // addition to the detailed collapsible list above. Unmanaged broken mod
  // folders (broken_mods.py) are a separate, higher-priority tier — see
  // modTier() — since "confirmed non-functional" outranks "suspected".
  //
  // 'duplicate_package' is deliberately excluded: it fires on a mod sharing
  // just *some* of its files with another (an override bundling a few of
  // the same resources, say), which is common enough and weak enough a
  // signal that treating it as a full "problem" (red border, sorts first)
  // was more alarming than useful. It's still visible in the mod's own
  // detail panel (renderDetailConflicts() doesn't filter by kind) — this
  // only stops it from being a grid-wide red flag. 'folder_duplication'
  // and 'exact_duplicate_mod' (name pattern / 100% file match) are much
  // stronger signals and still count.
  const ids = new Set();
  state.conflicts
    .filter((group) => group.kind !== "duplicate_package")
    .forEach((group) => group.mods.forEach((m) => ids.add(m.id)));
  state.blacklistMatches.forEach((match) => ids.add(match.mod_id));
  return ids;
}

// Sort priority within an author group (or the trailing authorless
// bucket): 0 = broken folder or rezipped mod (both confirmed
// non-functional — a rezipped mod's real folder has nothing loadable in it
// right now, same as a broken folder, just with a DB row still attached),
// 1 = problem mod (conflict/blacklist flagged, only ever active —
// conflict_detector.py doesn't consider disabled mods), 2 = normal active
// mod, 3 = disabled.
function modTier(mod, problems, rezipped) {
  if (mod.__brokenFolder) return 0;
  if (rezipped && rezipped.has(mod.id)) return 0;
  if (!mod.active) return 3;
  if (problems.has(mod.id)) return 1;
  return 2;
}

// Within a folder_duplication conflict (backend/conflict_detector.py), only
// the "(1)"/"(2)"/... suffixed member(s) get the "Duplicate" tag — the
// unsuffixed one is presumed the original, the suffixed one(s) the accidental
// re-download/re-install. An exact_duplicate_mod group (100% shared files,
// see conflict_detector.py) uses `author` for the same call instead, since
// there's no name pattern to go on: whichever member has no known author is
// presumed the redundant one when at least one other member does have one.
// Purely a display tag, doesn't affect which mod problemModIds() flags (the
// whole group still gets the red border either way).
function duplicateTagModIds() {
  const ids = new Set();
  state.conflicts
    .filter((group) => group.kind === "folder_duplication")
    .forEach((group) => group.mods.forEach((m) => {
      if (/\(\d+\)\s*$/.test(m.name)) ids.add(m.id);
    }));
  state.conflicts
    .filter((group) => group.kind === "exact_duplicate_mod")
    .forEach((group) => {
      const authored = group.mods.filter((m) => m.author);
      const unauthored = group.mods.filter((m) => !m.author);
      if (authored.length && unauthored.length) {
        unauthored.forEach((m) => ids.add(m.id));
      }
    });
  return ids;
}

// The other half of an exact_duplicate_mod group's tagging: when there's no
// known-vs-unknown author split to blame one side with (nobody has an
// author, or everybody does — same author or different ones), neither
// member is singled out as "the duplicate". They still get flagged as
// identical to each other, just without an accusation — picking a side with
// no real basis (e.g. by install date) risks wrongly implying one author's
// mod is a rip-off of another's.
function identicalContentTagModIds() {
  const ids = new Set();
  state.conflicts
    .filter((group) => group.kind === "exact_duplicate_mod")
    .forEach((group) => {
      const authored = group.mods.filter((m) => m.author);
      const unauthored = group.mods.filter((m) => !m.author);
      if (!(authored.length && unauthored.length)) {
        group.mods.forEach((m) => ids.add(m.id));
      }
    });
  return ids;
}

// An unmanaged broken folder (broken_mods.py's 'unpacked_script' or
// 'unextracted_archive' reasons — see visibleBrokenModFolders()) is
// rendered as a pseudo-mod so it goes through the exact same placement,
// author-namespace, and version-extraction logic as a real installed mod
// (see the caller's request: it shouldn't get special-cased top-of-grid
// treatment anymore). buildCard()/buildListRow() key off `__brokenFolder`
// to swap in the broken icon/red theme and the repair action instead of
// toggle/delete — everything else (thumb, badges row, meta row) is shared
// scaffolding so its card is indistinguishable in size/layout from a real
// one, which is what keeps every card the same height.
function brokenFolderPseudoMod(folder) {
  return {
    id: `broken:${folder.name}`,
    name: folder.name,
    author: null,
    active: true,
    primary_type: null,
    installed_version: null,
    compat_status: "unknown",
    short_description: t(`library.conflicts.broken_mod_${folder.reason}`),
    __brokenFolder: folder,
  };
}

// Incremented on every render() call — see its own chunked-append comment
// for why a stale in-progress chunk run needs to recognize it's obsolete.
let renderGeneration = 0;

function render() {
  // Computed once and reused below for the grid itself — also lets the
  // subtitle report how many mods a filter (loose/translation/type/search)
  // is currently hiding, not just the raw installed/active counts.
  const filteredMods = visibleMods();
  const active = state.mods.filter((m) => m.active).length;
  document.getElementById("subtitle").textContent =
    filteredMods.length !== state.mods.length
      ? t("library.subtitle_filtered", { visible: filteredMods.length, installed: state.mods.length, active })
      : t("library.subtitle", { installed: state.mods.length, active });

  renderBrokenModsBanner();
  renderUnzipBanner();
  renderDuplicatesBanner();
  renderCompatQuarantineBanner();
  renderMergeableBanner();
  renderWarnings();
  renderMissingMods();
  renderLooseFilterChip();
  renderTranslationFilterChip();
  renderLinkedFilterChip();
  renderIncompatibleFilterChip();
  renderTypeFilterCounts();

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  const problems = problemModIds();
  const duplicateTags = duplicateTagModIds();
  const identicalContentTags = identicalContentTagModIds();
  const rezipped = rezippedModIds();
  const brokenPseudos = visibleBrokenModFolders().map(brokenFolderPseudoMod);
  const visible = filteredMods.concat(brokenPseudos);
  const inferredTags = computeInferredAuthorTags(visible);
  // groupingAuthor() does real work (name-splitting, regex, prefix lookup)
  // and the sort comparator below calls it on both operands of every
  // comparison — O(n log n) calls for what only needs to be computed once
  // per mod. At normal library sizes this doesn't matter, but it dominates
  // render()'s cost well before the chunked-append loop even starts once a
  // library reaches the thousands (a bulk loose-file import can do that —
  // see loose_mods.py). Cached here and reused by both the comparator and
  // the header-tracking check in the append loop below.
  const groupingCache = new Map();
  function cachedGrouping(mod) {
    let result = groupingCache.get(mod.id);
    if (!result) {
      result = groupingAuthor(mod, inferredTags);
      groupingCache.set(mod.id, result);
    }
    return result;
  }
  // Author is the primary sort key when groupByAuthor is on — a problem mod
  // shares its author's group rather than being pulled out into a separate
  // top-of-page section, and mods with no author at all sort after ones
  // that have one, rather than clumping arbitrarily at the top under an
  // empty "author". With groupByAuthor off, tier is the sole key instead —
  // there's no grouping to speak of, just the priority order below. Within
  // a group (or the trailing authorless bucket, or the whole list when
  // ungrouped), four distinct tiers, in this order: broken folders, then
  // problem mods (conflict/blacklist flagged), then normal active mods,
  // then disabled ones — broken and problem are NOT the same tier (a broken
  // folder is confirmed non-functional, a problem mod only suspected), and
  // a disabled mod is never itself a "problem" (conflict_detector.py only
  // considers active mods) but still needs its own tier below normal, not
  // just "not a problem". Alphabetical is the final tiebreak within a tier.
  const mods = visible.slice().sort((a, b) => {
    if (state.groupByAuthor) {
      const authorA = cachedGrouping(a).label;
      const authorB = cachedGrouping(b).label;
      if (!authorA !== !authorB) return authorA ? -1 : 1;
      const authorDiff = BASE_COLLATOR.compare(authorA, authorB);
      if (authorDiff !== 0) return authorDiff;
    }
    const tierDiff = modTier(a, problems, rezipped) - modTier(b, problems, rezipped);
    if (tierDiff !== 0) return tierDiff;
    return BASE_COLLATOR.compare(a.name, b.name);
  });
  if (!mods.length) {
    grid.appendChild(elementWithText("div", "empty-state", t("library.empty")));
    return;
  }
  const buildFn = state.viewMode === "list" ? buildListRow : buildCard;
  // Every mod currently sharing a given grouping label, keyed by that label
  // — computed once (a single pass) rather than filtering `mods` again for
  // every header, which would reintroduce an O(n) cost per header (real
  // once a library has hundreds of small, distinct guessed namespaces).
  // Used only by the header's edit icon, to apply a correction to every
  // member of a mis-clustered group at once.
  const membersByLabel = new Map();
  if (state.groupByAuthor) {
    mods.forEach((mod) => {
      const { label } = cachedGrouping(mod);
      if (!membersByLabel.has(label)) membersByLabel.set(label, []);
      membersByLabel.get(label).push(mod);
    });
  }
  // Author is the sole top-level sort key when grouping is on, so each
  // author's mods are always contiguous — a header only needs to check "did
  // the author change since the last mod", no reset-on-authorless-mod
  // needed. Mods with no author at all (no real `mod.author`, no bracket
  // prefix, no inferred series match) already sort last (see the
  // comparator above); they still get a header of their own — a generic
  // "Unknown author" one — instead of running headerless off the end of
  // the previous group. A collapsed group's header stays (so it can be
  // expanded again) but its members are skipped entirely.
  //
  // Appended in chunks across animation frames rather than one blocking
  // loop: building/appending a card is cheap per-item, but a library that's
  // grown into the thousands (e.g. after a bulk loose-file import — see
  // loose_mods.py) turned this into a single ~100-150ms freeze, felt on
  // every render() call including one per search keystroke. A small
  // library still finishes within the very first chunk (one frame), so
  // this changes nothing visible at normal scale. renderGeneration guards
  // against two overlapping chunked runs if render() is called again
  // (e.g. a fast second keystroke) before the previous pass finished —
  // the stale run just stops appending into a grid a newer call already
  // cleared.
  renderGeneration += 1;
  const myGeneration = renderGeneration;
  const CHUNK_BUDGET_MS = 8;
  let index = 0;
  let lastHeaderAuthor = null;

  function appendChunk() {
    if (myGeneration !== renderGeneration) return;
    const start = performance.now();
    while (index < mods.length && performance.now() - start < CHUNK_BUDGET_MS) {
      const mod = mods[index];
      index += 1;
      let skip = false;
      if (state.groupByAuthor) {
        const { label: author } = cachedGrouping(mod);
        if (author !== lastHeaderAuthor) {
          grid.appendChild(
            buildAuthorHeader(author, author || t("library.unknown_author_header"), membersByLabel.get(author) || [])
          );
        }
        lastHeaderAuthor = author;
        skip = state.collapsedAuthors.has(author);
      }
      if (!skip) {
        grid.appendChild(
          buildFn(
            mod,
            inferredTags,
            problems.has(mod.id),
            duplicateTags.has(mod.id),
            identicalContentTags.has(mod.id),
            rezippedZipPaths(mod.id)
          )
        );
      }
    }
    if (index < mods.length) {
      requestAnimationFrame(appendChunk);
    }
  }
  appendChunk();
}

const CHEVRON_ICON_SVG =
  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">' +
  '<polyline points="6 9 12 15 18 9"/></svg>';

// `rawAuthor` is the grouping key (used for collapse state, matches what
// the render() loop tracks as lastHeaderAuthor — including "" for the
// authorless bucket); `displayText` is what's actually shown (a friendly
// "Unknown author" fallback in that case). Clicking anywhere on the header
// toggles that group's collapse state and re-renders — collapsed groups
// keep their header (so they can be expanded again) but skip every member
// card/row (see render()).
const EDIT_ICON_SVG =
  '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z"/></svg>';

// `members` is every mod currently grouped under this header (see render()'s
// membersByLabel) — needed so the edit icon can apply a correction to the
// whole group at once, not just one mod, since the label is shared by all
// of them.
function buildAuthorHeader(rawAuthor, displayText, members) {
  const header = document.createElement("div");
  const collapsed = state.collapsedAuthors.has(rawAuthor);
  header.className = "author-group-header" + (collapsed ? " collapsed" : "");
  const chevron = document.createElement("span");
  chevron.className = "chevron";
  chevron.innerHTML = CHEVRON_ICON_SVG;
  header.appendChild(chevron);
  header.appendChild(elementWithText("span", null, displayText));

  // This grouping label is frequently a *guessed* mod/series namespace
  // (a bracket prefix, or a shared-name-segments cluster), not a confirmed
  // author — it can be wrong. This lets the user correct it directly
  // rather than living with a bad guess forever.
  const editBtn = document.createElement("button");
  editBtn.className = "icon-btn author-edit-btn";
  editBtn.title = t("library.edit_namespace_button_title");
  editBtn.innerHTML = EDIT_ICON_SVG;
  editBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmEditNamespace(rawAuthor, members);
  });
  header.appendChild(editBtn);

  const rule = document.createElement("div");
  rule.className = "rule";
  header.appendChild(rule);
  header.addEventListener("click", () => {
    if (state.collapsedAuthors.has(rawAuthor)) {
      state.collapsedAuthors.delete(rawAuthor);
    } else {
      state.collapsedAuthors.add(rawAuthor);
    }
    render();
  });
  return header;
}

function confirmEditNamespace(rawAuthor, members) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "text-input";
  input.value = rawAuthor;
  input.style.width = "100%";
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("confirmOkBtn").click();
  });
  openConfirmModal({
    title: t("library.edit_namespace_confirm.title"),
    message: t("library.edit_namespace_confirm.message", { count: members.length }),
    extraNodes: [input],
    confirmLabel: t("library.edit_namespace_confirm.confirm"),
    onConfirm: () => doSetNamespaceOverride(members, input.value.trim()),
  });
  input.focus();
  input.select();
}

async function doSetNamespaceOverride(members, value) {
  // A broken-folder pseudo-mod (see brokenFolderPseudoMod()) has no real
  // mods row to correct — excluded here rather than earlier so the confirm
  // message's member count still reflects everything visually grouped.
  const realMods = members.filter((mod) => !mod.__brokenFolder);
  try {
    await Promise.all(
      realMods.map((mod) =>
        apiRequest(`/api/mods/${encodeURIComponent(mod.id)}/namespace-override`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: value || null }),
        })
      )
    );
    closeConfirm();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// Settings' "Clear custom groupings" button: a bulk undo for every manual
// correction made via the header edit icon above (doSetNamespaceOverride()),
// for when a library-wide re-organization means starting over is easier
// than editing each group one at a time. Loops the exact same per-mod route,
// just with value: null for every mod that currently has an override set.
function confirmClearNamespaceOverrides() {
  const resultEl = document.getElementById("settingsNamespacesResult");
  resultEl.textContent = "";
  const overridden = state.mods.filter((mod) => (mod.namespace_override || "").trim());
  if (!overridden.length) {
    resultEl.textContent = t("settings.clear_namespace_overrides_none");
    return;
  }
  openConfirmModal({
    title: t("settings.clear_namespace_overrides_confirm.title"),
    message: t("settings.clear_namespace_overrides_confirm.message", { count: overridden.length }),
    confirmLabel: t("settings.clear_namespace_overrides_confirm.confirm"),
    onConfirm: () => doClearNamespaceOverrides(overridden),
  });
}

async function doClearNamespaceOverrides(overridden) {
  const resultEl = document.getElementById("settingsNamespacesResult");
  try {
    await Promise.all(
      overridden.map((mod) =>
        apiRequest(`/api/mods/${encodeURIComponent(mod.id)}/namespace-override`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ value: null }),
        })
      )
    );
    closeConfirm();
    resultEl.textContent = t("settings.clear_namespace_overrides_result", { count: overridden.length });
    await loadMods();
    render();
  } catch (err) {
    showError("settingsNamespacesErrorBanner", t("library.action_error", { error: err.message }));
  }
}

// Normalizes a version string for display: "6.9.4" -> "v6.9.4", but
// "v1.7"/"V1.7" (either extracted from a name that already had one, or a
// CurseForge `installed_version` that already includes one) is left as-is
// rather than becoming "vv1.7".
function formatVersionBadge(rawVersion) {
  return /^v/i.test(rawVersion) ? rawVersion : `v${rawVersion}`;
}

// Every classification pill a mod/broken-folder can carry — duplicate,
// broken, type (CC/Script), version, compat, disabled — lives in one single
// row (`badgeClassName`/`gemSizePx` let the caller pick the pill vs.
// cc-badge styling and gem size, since the grid card and list row use
// different components for otherwise the exact same set of badges). Shared
// by buildCard()/buildListRow() so "all badges in the same place" holds in
// both views.
function buildBadges(mod, version, isDuplicate, isIdenticalContent, badgeClassName, rezipZips) {
  const broken = mod.__brokenFolder;
  const badges = document.createElement("span"); // caller decides tag/class via appendChild target
  const items = [];
  if (broken) {
    const archive = broken.reason === "unextracted_archive";
    items.push(
      elementWithText(
        "span",
        `${badgeClassName} ${archive ? "archive-badge" : "danger-badge"}`,
        t(archive ? "library.archive_tag" : "library.broken_tag")
      )
    );
  } else if (rezipZips) {
    // Same purple "Zip" identity as an unmanaged unextracted_archive folder
    // — a rezipped mod is in the exact same state (a zip sitting where
    // loadable files should be), it just still has a DB row/symlink too.
    items.push(elementWithText("span", `${badgeClassName} archive-badge`, t("library.archive_tag")));
  }
  if (isDuplicate || isIdenticalContent) {
    // Same wording either way now — only the color differs: "warn-badge"
    // (orange) when one side is presumed the culprit (no known author),
    // plain/neutral when neither side is singled out (see
    // duplicateTagModIds()/identicalContentTagModIds()).
    items.push(
      elementWithText("span", `${badgeClassName}${isDuplicate ? " warn-badge" : ""}`, t("library.duplicate_tag"))
    );
  }
  if (!broken && mod.compat_status === "incompatible") {
    // Orange, not red — a declared game-version range can be wrong/overly
    // conservative (curseforge.py's compat_status()), so this is a caution,
    // not a confirmed-broken claim like a real broken folder's red badge.
    items.push(elementWithText("span", `${badgeClassName} warn-badge`, t("library.incompatible_tag")));
  }
  if (!broken && mod.is_loose_import) {
    // Neutral, not alarming — a mod adopted from a loose file at Mods/
    // root isn't broken or suspicious, just not yet organized (see
    // loose_mods.py). Plain badge styling, same as the type pills below.
    items.push(elementWithText("span", badgeClassName, t("library.loose_import_tag")));
  }
  if (!broken && isTranslationByName(mod)) {
    // Purely a name-pattern signal (see TRANSLATION_NAME_RE) — lets a user
    // see at a glance which of their installed mods are themselves fan
    // translations of another mod, e.g. to confirm one is actually present
    // for a base mod they use. Neutral styling, same reasoning as the loose
    // import badge above: this isn't a defect, just informational.
    items.push(elementWithText("span", badgeClassName, t("library.translation_tag")));
  }
  const rowVersion = mod.installed_version || version;
  if (rowVersion) {
    items.push(elementWithText("span", badgeClassName, formatVersionBadge(rowVersion)));
  }
  primaryTypeBadgeKeys(mod.primary_type).forEach((key) => items.push(elementWithText("span", badgeClassName, t(key))));
  if (!broken && !mod.active) {
    items.push(elementWithText("span", badgeClassName, t("library.disabled_tag")));
  }
  items.forEach((item) => badges.appendChild(item));
  return { el: badges, count: items.length };
}

function buildListRow(mod, inferredTags, hasIssue, isDuplicate, isIdenticalContent, rezipZips) {
  const { label, displayName, version } = groupingAuthor(mod, inferredTags);
  const broken = mod.__brokenFolder;
  const row = document.createElement("div");
  row.className =
    "list-mod-row" +
    (mod.active ? "" : " is-inactive") +
    (broken ? " " + brokenStateClass(broken) : rezipZips ? " is-archive" : hasIssue ? " has-issue" : "");
  row.addEventListener("click", () =>
    broken ? openBrokenDetail(mod.__brokenFolder) : openModDetailOrCompare(mod)
  );

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (!broken && mod.thumbnail_url) {
    const img = document.createElement("img");
    img.className = "avatar-image";
    img.src = mod.thumbnail_url;
    img.alt = "";
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.remove();
      fillAvatarInitial(avatar, mod, label, broken);
    });
    avatar.appendChild(img);
  } else {
    fillAvatarInitial(avatar, mod, label, broken);
  }
  row.appendChild(avatar);

  const info = document.createElement("div");
  info.className = "info";
  const h3 = document.createElement("h3");
  h3.appendChild(document.createTextNode(titleText(mod, displayName)));
  if (mod.author) h3.appendChild(elementWithText("span", "author", mod.author));
  info.appendChild(h3);
  info.appendChild(elementWithText("p", null, mod.short_description || ""));
  row.appendChild(info);

  const badges = buildBadges(mod, version, isDuplicate, isIdenticalContent, "pill", rezipZips).el;
  badges.className = "list-mod-row-badges";
  row.appendChild(badges);

  if (broken) {
    const actionBtn = buildBrokenActionButton(mod.__brokenFolder);
    if (actionBtn) row.appendChild(actionBtn);
    row.appendChild(buildDeleteBrokenButton(mod.__brokenFolder));
  } else {
    const toggle = document.createElement("span");
    toggle.className = "toggle" + (mod.active ? " on" : "");
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleActive(mod);
    });
    row.appendChild(toggle);
    const rezipBtn = buildRezipActionButton(mod, rezipZips);
    if (rezipBtn) row.appendChild(rezipBtn);
    row.appendChild(buildDeleteIconButton(mod));
  }

  return row;
}

// The letter-avatar placeholder a card's thumb shows when there's no real
// CurseForge thumbnail (or it failed to load) — factored out since the
// image-load error handler in buildCard() needs to build the exact same
// fallback a card without a thumbnail_url would have shown from the start.
function buildThumbInitial(mod, label, broken) {
  const initialEl = document.createElement("span");
  initialEl.className = "initial";
  if (broken) {
    initialEl.innerHTML = BROKEN_ICON_SVG;
  } else {
    initialEl.textContent = label || initials(mod.name);
    if (label && label.length > 4) initialEl.classList.add("long");
  }
  return initialEl;
}

// List row's counterpart to buildThumbInitial() above — the avatar div is
// its own text container (no separate .initial child), so this sets
// textContent/innerHTML directly on the element it's given.
function fillAvatarInitial(el, mod, label, broken) {
  if (broken) {
    el.innerHTML = BROKEN_ICON_SVG;
  } else {
    el.textContent = label || initials(mod.name);
    if (label && label.length > 4) el.classList.add("long");
  }
}

function buildCard(mod, inferredTags, hasIssue, isDuplicate, isIdenticalContent, rezipZips) {
  const { label, displayName, version } = groupingAuthor(mod, inferredTags);
  const broken = mod.__brokenFolder;
  const card = document.createElement("div");
  card.className =
    "card" +
    (mod.active ? "" : " is-inactive") +
    (broken ? " " + brokenStateClass(broken) : rezipZips ? " is-archive" : hasIssue ? " has-issue" : "");
  card.addEventListener("click", () =>
    broken ? openBrokenDetail(mod.__brokenFolder) : openModDetailOrCompare(mod)
  );

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (!broken && mod.thumbnail_url) {
    // A real thumbnail only ever comes from CurseForge (Direct Mode catalog
    // install, or Settings' CurseForge fingerprint matching for a loose
    // import) — falls back to the usual letter-avatar if the image 404s or
    // the CDN is briefly unreachable, rather than showing a broken-image icon.
    const img = document.createElement("img");
    img.className = "thumb-image";
    img.src = mod.thumbnail_url;
    img.alt = "";
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.remove();
      thumb.appendChild(buildThumbInitial(mod, label, broken));
    });
    thumb.appendChild(img);
  } else {
    thumb.appendChild(buildThumbInitial(mod, label, broken));
  }

  const badges = buildBadges(mod, version, isDuplicate, isIdenticalContent, "cc-badge", rezipZips);
  if (badges.count) {
    badges.el.className = "card-badges";
    thumb.appendChild(badges.el);
  }

  const body = document.createElement("div");
  body.className = "card-body";
  body.appendChild(elementWithText("h3", null, titleText(mod, displayName)));
  body.appendChild(elementWithText("p", null, mod.short_description || ""));

  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.appendChild(broken ? document.createElement("span") : buildLinkStatusIndicator(mod));
  const metaActions = document.createElement("span");
  metaActions.className = "card-meta-actions";
  if (broken) {
    const actionBtn = buildBrokenActionButton(mod.__brokenFolder);
    if (actionBtn) metaActions.appendChild(actionBtn);
    metaActions.appendChild(buildDeleteBrokenButton(mod.__brokenFolder));
  } else {
    const toggle = document.createElement("span");
    toggle.className = "toggle" + (mod.active ? " on" : "");
    toggle.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleActive(mod);
    });
    metaActions.appendChild(toggle);
    const rezipBtn = buildRezipActionButton(mod, rezipZips);
    if (rezipBtn) metaActions.appendChild(rezipBtn);
    metaActions.appendChild(buildDeleteIconButton(mod));
  }
  meta.appendChild(metaActions);
  body.appendChild(meta);

  card.appendChild(thumb);
  card.appendChild(body);
  return card;
}

// Four states, driven by mod.link_state (precomputed backend-side by
// mod_link_state() in main.py — combines curseforge_id/compat_status/
// latest_version, since "is an update available AND is it itself
// compatible" needs a real network-derived value, not something this
// component should guess at from raw fields): red "Non lié" when unlinked,
// green "Lié" for a plain link, orange "Incompatible" when linked but the
// installed file is incompatible with no update pending, purple "MAJ
// disponible" when a pending update is itself confirmed compatible. Card-
// only, at the left edge of the bottom meta row (the toggle/action icons
// occupy the right, via .card-meta's justify-content: space-between) — same
// underlying link signal as renderLinkedFilterChip()'s filter count.
const LINK_STATE_CLASSES = {
  linked: "is-linked",
  incompatible: "is-incompatible",
  update_available: "is-update-available",
};
const LINK_STATE_LABEL_KEYS = {
  unlinked: "library.link_status.unlinked_label",
  linked: "library.link_status.linked_label",
  incompatible: "library.link_status.incompatible_label",
  update_available: "library.link_status.update_available_label",
};
function buildLinkStatusIndicator(mod) {
  const state = mod.link_state || (mod.curseforge_id ? "linked" : "unlinked");
  const wrap = document.createElement("span");
  wrap.className = "link-status" + (LINK_STATE_CLASSES[state] ? " " + LINK_STATE_CLASSES[state] : "");
  const dot = document.createElement("span");
  dot.className = "link-status-dot";
  wrap.appendChild(dot);
  wrap.append(t(LINK_STATE_LABEL_KEYS[state] || LINK_STATE_LABEL_KEYS.unlinked));
  return wrap;
}

async function toggleActive(mod) {
  const wasActive = mod.active;
  const endpoint = wasActive ? "disable" : "enable";
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(mod.id)}/${endpoint}`, { method: "POST" });
    await loadMods();
    render();
    // CLAUDE.md's Cache cleanup: suggest after install/update/disable — not
    // enable, which isn't in that list (a newly-active mod isn't what left
    // stale cache entries behind).
    if (wasActive) await suggestCacheCleanup();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// --- search --------------------------------------------------------------------

const SEARCH_DEBOUNCE_MS = 150;
let searchDebounceTimer = null;

// A full render() rebuilds every card in the grid — cheap for a small
// library, but a real cost once it's in the thousands (see render()'s own
// chunked-append comment). Debouncing means fast typing costs one render
// at the end, not one per keystroke.
function wireSearch() {
  const input = document.getElementById("searchInput");
  const clearBtn = document.getElementById("searchClearBtn");
  input.addEventListener("input", (e) => {
    state.filterQuery = e.target.value;
    clearBtn.hidden = !e.target.value;
    clearTimeout(searchDebounceTimer);
    searchDebounceTimer = setTimeout(render, SEARCH_DEBOUNCE_MS);
  });
  clearBtn.addEventListener("click", () => {
    input.value = "";
    state.filterQuery = "";
    clearBtn.hidden = true;
    clearTimeout(searchDebounceTimer);
    render();
    input.focus();
  });
}

// --- view navigation -------------------------------------------------------------

function wireNav() {
  document.querySelectorAll(".nav-item[data-view]").forEach((el) => {
    el.addEventListener("click", () => {
      if (el.classList.contains("disabled")) return;
      switchView(el.dataset.view);
    });
  });
}

const VIEWS = ["library", "catalog", "updates", "settings"];

function switchView(view) {
  document.querySelectorAll(".nav-item[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  VIEWS.forEach((v) => {
    document.getElementById(`view-${v}`).hidden = v !== view;
  });
  if (view === "catalog") initCatalogView();
  if (view === "updates") initUpdatesView();
  if (view === "settings") initSettingsView();
}

// --- catalog view ------------------------------------------------------------------

function initCatalogView() {
  const direct = !!(state.status && state.status.direct_mode);
  document.getElementById("catalogAssistedNotice").hidden = direct;
  document.getElementById("catalogSearchBar").hidden = !direct;
  if (!direct) return;

  if (state.catalogWired) return;
  state.catalogWired = true;
  document.getElementById("catalogSearchButton").addEventListener("click", doCatalogSearch);
  document.getElementById("catalogSearchInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doCatalogSearch();
  });
  document.getElementById("catalogSortSelect").addEventListener("change", doCatalogSearch);
  document.getElementById("catalogPeriodSelect").addEventListener("change", doCatalogSearch);

  // An empty query browses the full catalog (confirmed against the real
  // API — see backend/main.py's search_catalog) — this is what makes the
  // view show something the moment it's opened, sorted by whatever the
  // selects default to (Most popular / All time), instead of sitting empty
  // until the user types a search.
  doCatalogSearch();
}

async function doCatalogSearch() {
  const query = document.getElementById("catalogSearchInput").value.trim();
  const sort = document.getElementById("catalogSortSelect").value;
  const period = document.getElementById("catalogPeriodSelect").value;
  const results = document.getElementById("catalogResults");
  results.innerHTML = "";
  const params = new URLSearchParams({ q: query, sort });
  if (period) params.set("period", period);
  let mods;
  try {
    mods = await apiRequest(`/api/catalog/search?${params.toString()}`);
  } catch (err) {
    showError("catalogErrorBanner", t("catalog.search_error", { error: err.message }));
    return;
  }
  if (!mods.length) {
    results.appendChild(elementWithText("div", "empty-state", t("catalog.no_results")));
    return;
  }
  mods.forEach((mod) => results.appendChild(buildCatalogCard(mod)));
}

// Reuses the Library grid's own .card/.thumb/.card-body/.card-meta styling
// (see buildCard() in app.js) so catalog results read as the same visual
// language as installed mods, just with an Install/Open-on-CurseForge action
// where the Library card has its enable/disable toggle.
function buildCatalogCard(mod) {
  const card = document.createElement("div");
  card.className = "card";

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  if (mod.thumbnail_url) {
    const img = document.createElement("img");
    img.className = "thumb-image";
    img.src = mod.thumbnail_url;
    img.alt = "";
    img.loading = "lazy";
    img.addEventListener("error", () => {
      img.remove();
      thumb.appendChild(buildCatalogThumbInitial(mod));
    });
    thumb.appendChild(img);
  } else {
    thumb.appendChild(buildCatalogThumbInitial(mod));
  }
  card.appendChild(thumb);

  const body = document.createElement("div");
  body.className = "card-body";
  const title = document.createElement("h3");
  title.appendChild(document.createTextNode(mod.name));
  if (mod.author) title.appendChild(elementWithText("span", "author", mod.author));
  body.appendChild(title);
  body.appendChild(elementWithText("p", null, mod.short_description || ""));

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const stats = [];
  if (mod.download_count) stats.push(t("catalog.downloads_count", { count: formatCompactNumber(mod.download_count) }));
  if (mod.date_modified) stats.push(t("catalog.updated_on", { date: formatInstallDate(mod.date_modified) }));
  meta.appendChild(elementWithText("span", null, stats.join(" · ")));

  const action = document.createElement("button");
  action.className = "btn btn-sm" + (mod.third_party_distribution_allowed ? " primary" : "");
  if (mod.third_party_distribution_allowed) {
    action.textContent = t("catalog.install_button");
    action.addEventListener("click", () => installFromCatalog(mod, action));
  } else {
    action.textContent = t("catalog.open_on_curseforge_button");
    action.addEventListener("click", () => openExternal(mod.curseforge_url));
  }
  const metaActions = document.createElement("span");
  metaActions.className = "card-meta-actions";
  metaActions.appendChild(action);
  meta.appendChild(metaActions);
  body.appendChild(meta);

  card.appendChild(body);
  return card;
}

function buildCatalogThumbInitial(mod) {
  const initialEl = document.createElement("span");
  initialEl.className = "initial";
  const label = initials(mod.name);
  initialEl.textContent = label;
  if (label.length > 4) initialEl.classList.add("long");
  return initialEl;
}

async function installFromCatalog(mod, button) {
  button.disabled = true;
  try {
    await apiRequest(`/api/catalog/${mod.mod_id}/install`, { method: "POST" });
    button.textContent = t("catalog.installed_label");
    await loadMods();
    await suggestCacheCleanup();
  } catch (err) {
    button.disabled = false;
    showError("catalogErrorBanner", t("catalog.install_error", { error: err.message }));
  }
}

async function openExternal(url) {
  if (!url) return;
  try {
    await apiRequest("/api/open-external", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
  } catch (err) {
    showError("catalogErrorBanner", t("catalog.open_error", { error: err.message }));
  }
}

// --- updates view ------------------------------------------------------------------

function initUpdatesView() {
  const direct = !!(state.status && state.status.direct_mode);
  document.getElementById("updatesAssistedNotice").hidden = direct;
  document.getElementById("updatesChecklist").hidden = direct;
  document.getElementById("updatesDirectBar").hidden = !direct;
  document.getElementById("updatesResults").hidden = !direct;

  if (direct) {
    if (!state.updatesWired) {
      state.updatesWired = true;
      document.getElementById("updatesCheckButton").addEventListener("click", doCheckUpdates);
      document.getElementById("updateAllButton").addEventListener("click", clickUpdateAll);
    }
    return;
  }
  loadUpdatesChecklist();
}

function buildLinkRow(name, buttonLabel, onClick) {
  const row = document.createElement("div");
  row.className = "catalog-row";
  row.appendChild(elementWithText("div", "avatar", initials(name)));
  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("h3", null, name));
  info.appendChild(elementWithText("p", null, ""));
  row.appendChild(info);
  const btn = document.createElement("button");
  btn.className = "btn";
  btn.textContent = buttonLabel;
  btn.addEventListener("click", onClick);
  row.appendChild(btn);
  return row;
}

async function loadUpdatesChecklist() {
  const container = document.getElementById("updatesChecklist");
  container.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/updates/checklist");
  } catch (err) {
    showError("updatesErrorBanner", t("updates.check_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    container.appendChild(elementWithText("div", "empty-state", t("updates.empty_assisted")));
    return;
  }
  items.forEach((item) => {
    container.appendChild(
      buildLinkRow(item.name, t("updates.check_on_curseforge_button"), () => openExternal(item.curseforge_url))
    );
  });
}

async function doCheckUpdates() {
  const results = document.getElementById("updatesResults");
  const updateAllButton = document.getElementById("updateAllButton");
  results.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/updates/check", { method: "POST" });
  } catch (err) {
    showError("updatesErrorBanner", t("updates.check_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    results.appendChild(elementWithText("div", "empty-state", t("updates.empty_direct")));
    updateAllButton.hidden = true;
    return;
  }
  const actionable = items.filter((i) => i.status === "update_available");
  const errored = items.filter((i) => i.status === "error");
  if (!actionable.length) {
    results.appendChild(elementWithText("div", "empty-state", t("updates.no_updates")));
  }
  actionable.forEach((item) => results.appendChild(buildUpdateRow(item)));
  errored.forEach((item) =>
    results.appendChild(
      elementWithText("div", "empty-inline", t("updates.check_error", { error: `${item.name}: ${item.error}` }))
    )
  );

  state.updatableMods = actionable.map((item) => ({ id: item.id, name: item.name }));
  updateAllButton.hidden = actionable.length < 2; // one mod is already a single click away below
}

function buildUpdateRow(item) {
  const row = document.createElement("div");
  row.className = "catalog-row";
  row.appendChild(elementWithText("div", "avatar", initials(item.name)));
  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("h3", null, item.name));
  info.appendChild(elementWithText("p", null, t("updates.update_available")));
  row.appendChild(info);
  const btn = document.createElement("button");
  btn.className = "btn primary";
  btn.textContent = t("updates.update_button");
  btn.addEventListener("click", () => applyUpdate(item.id, btn));
  row.appendChild(btn);
  return row;
}

async function applyUpdateForMod(modId) {
  await apiRequest(`/api/updates/${encodeURIComponent(modId)}/apply`, { method: "POST" });
}

async function applyUpdate(modId, button) {
  button.disabled = true;
  try {
    await applyUpdateForMod(modId);
    button.textContent = t("catalog.installed_label");
    await loadMods();
    await suggestCacheCleanup();
  } catch (err) {
    button.disabled = false;
    showError("updatesErrorBanner", t("updates.update_error", { error: err.message }));
  }
}

function clickUpdateAll() {
  const updatable = state.updatableMods || [];
  if (!updatable.length) return;
  openConfirmModal({
    title: t("updates.update_all_title"),
    message: t("updates.update_all_message", { count: updatable.length }),
    extraNodes: updatable.map((item) => elementWithText("div", "empty-inline", item.name)),
    confirmLabel: t("updates.update_all_confirm"),
    onConfirm: doUpdateAll,
  });
}

async function doUpdateAll() {
  closeConfirm();
  const updatable = state.updatableMods || [];
  const button = document.getElementById("updateAllButton");
  button.disabled = true;

  const failures = [];
  for (const item of updatable) {
    try {
      await applyUpdateForMod(item.id);
    } catch (err) {
      failures.push(`${item.name}: ${err.message}`);
    }
  }

  button.disabled = false;
  await loadMods();
  if (failures.length) {
    showError("updatesErrorBanner", t("updates.update_all_partial_error", { errors: failures.join("; ") }));
  }
  if (failures.length < updatable.length) await suggestCacheCleanup(); // at least one succeeded
  await doCheckUpdates(); // re-check: applied mods drop off, failures are re-offered
}

// --- crash diagnosis popup ---------------------------------------------------------

// Wired once from init() (like #confirmOverlay/#downloadOverlay) rather
// than lazily on first view — there's no "Crash Mode" page to lazily visit
// anymore, just a header button reachable from anywhere in the app.
function wireCrashModal() {
  document.getElementById("headerCrashButton").addEventListener("click", openCrashModal);
  document.getElementById("crashCloseBtn").addEventListener("click", closeCrashModal);
  document.getElementById("crashOverlay").addEventListener("click", (e) => {
    if (e.target.id === "crashOverlay") closeCrashModal();
  });
}

// Opening the popup *is* "I crashed" now — analysis starts immediately,
// no separate button to click first inside the modal.
function openCrashModal() {
  document.getElementById("crashOverlay").classList.add("show");
  doAnalyzeCrash();
}

function closeCrashModal() {
  document.getElementById("crashOverlay").classList.remove("show");
}

function modName(modId) {
  const mod = state.mods.find((m) => m.id === modId);
  return mod ? mod.name : modId;
}

function setCrashStatus(...nodes) {
  const status = document.getElementById("crashStatus");
  status.innerHTML = "";
  nodes.forEach((node) => status.appendChild(node));
}

async function doAnalyzeCrash() {
  let result;
  try {
    result = await apiRequest("/api/crash/analyze", { method: "POST" });
  } catch (err) {
    showError("crashErrorBanner", t("crash.analyze_error", { error: err.message }));
    return;
  }
  if (!result.found) {
    setCrashStatus(elementWithText("div", "empty-state", t("crash.no_exception_file")));
    return;
  }
  renderCrashReports(result.reports);
}

function renderCrashReports(reports) {
  if (!reports.length) {
    setCrashStatus(elementWithText("div", "empty-state", t("crash.no_exception_file")));
    return;
  }
  setCrashStatus(...reports.map((report, index) => renderCrashReportBlock(report, index, reports.length)));
}

// lastException.txt can bundle several unrelated occurrences in one file, so
// /api/crash/analyze returns a list of reports (see crash_analyzer.py) —
// each gets its own suspects list and its own independently-scoped
// bisection flow, rather than pretending the file was a single incident.
function renderCrashReportBlock(report, index, total) {
  const block = document.createElement("div");
  block.className = "crash-report-block";
  if (total > 1) {
    block.appendChild(elementWithText("h4", null, t("crash.report_heading", { index: index + 1, total })));
  }

  if (report.suspects.length) {
    block.appendChild(elementWithText("h4", null, t("crash.suspects_heading")));
    report.suspects.forEach((s) => {
      block.appendChild(
        elementWithText(
          "div",
          "dep-row",
          t("crash.suspect_line", { name: modName(s.mod_id), confidence: s.confidence, reason: s.reason })
        )
      );
    });
    return block;
  }

  const startBtn = document.createElement("button");
  startBtn.className = "btn primary";
  startBtn.textContent = t("crash.start_bisection");
  startBtn.addEventListener("click", () => startBisection(report.crash_log_id));
  block.appendChild(elementWithText("div", "empty-state", t("crash.no_suspects")));
  block.appendChild(startBtn);
  return block;
}

async function startBisection(crashLogId) {
  try {
    const result = await apiRequest(`/api/crash/${crashLogId}/bisection/start`, { method: "POST" });
    await loadMods();
    renderBisectionRound(crashLogId, result.disabled);
  } catch (err) {
    showError("crashErrorBanner", t("crash.bisection_error", { error: err.message }));
  }
}

function renderBisectionRound(crashLogId, disabled) {
  const names = disabled.map(modName).join(", ");
  const row = document.createElement("div");
  row.className = "confirm-actions";
  const stillBtn = document.createElement("button");
  stillBtn.className = "btn";
  stillBtn.textContent = t("crash.bisection_still_crashes");
  stillBtn.addEventListener("click", () => reportBisection(crashLogId, true));
  const fixedBtn = document.createElement("button");
  fixedBtn.className = "btn primary";
  fixedBtn.textContent = t("crash.bisection_fixed");
  fixedBtn.addEventListener("click", () => reportBisection(crashLogId, false));
  row.appendChild(stillBtn);
  row.appendChild(fixedBtn);
  setCrashStatus(elementWithText("p", null, t("crash.bisection_round", { mods: names })), row);
}

async function reportBisection(crashLogId, crashOccurred) {
  let result;
  try {
    result = await apiRequest(`/api/crash/${crashLogId}/bisection/report`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ crash_occurred: crashOccurred }),
    });
  } catch (err) {
    showError("crashErrorBanner", t("crash.bisection_error", { error: err.message }));
    return;
  }
  await loadMods(); // bisection toggles active state via symlinks — keep Library in sync
  if (result.status === "next_round") {
    renderBisectionRound(crashLogId, result.disabled);
  } else if (result.status === "converged") {
    renderConverged(crashLogId, result.mod_id);
  } else {
    setCrashStatus(elementWithText("div", "empty-state", t("crash.bisection_inconclusive")));
  }
}

function renderConverged(crashLogId, modId) {
  const confirmBtn = document.createElement("button");
  confirmBtn.className = "btn primary";
  confirmBtn.textContent = t("crash.confirm_faulty");
  confirmBtn.addEventListener("click", () => confirmFaulty(crashLogId, modId));
  setCrashStatus(elementWithText("p", null, t("crash.bisection_converged", { name: modName(modId) })), confirmBtn);
}

async function confirmFaulty(crashLogId, modId) {
  try {
    await apiRequest(`/api/crash/${crashLogId}/confirm-faulty`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mod_id: modId }),
    });
  } catch (err) {
    showError("crashErrorBanner", t("crash.bisection_error", { error: err.message }));
    return;
  }
  setCrashStatus(elementWithText("div", "empty-state", t("crash.faulty_confirmed", { name: modName(modId) })));
}

// tgt.name is the real, unmodified file/folder name (cache_cleaner.py's
// fixed target specs, e.g. "localthumbcache.package") — shown verbatim in
// its own monospace line, not folded into a simplified sentence, so its
// extension (or lack of one, for a directory target) stays visible exactly
// as it is on disk.
function buildCacheTargetNodes(targets) {
  if (!targets.length) return [elementWithText("div", "empty-inline", t("crash.clear_cache_nothing"))];
  return targets.map((tgt) => {
    const row = document.createElement("div");
    row.className = "cache-target-row";
    const info = document.createElement("div");
    info.className = "info";
    info.appendChild(elementWithText("span", "name", tgt.name));
    info.appendChild(elementWithText("span", "desc", tgt.description));
    row.appendChild(info);
    const openBtn = document.createElement("button");
    openBtn.type = "button";
    openBtn.className = "icon-btn";
    openBtn.title = t("cache.open_target_button_title");
    openBtn.innerHTML = FOLDER_ICON_SVG;
    openBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      doOpenCacheTarget(tgt.name);
    });
    row.appendChild(openBtn);
    return row;
  });
}

async function doOpenCacheTarget(name) {
  try {
    await apiRequest(`/api/cache/targets/${encodeURIComponent(name)}/open`, { method: "POST" });
  } catch (err) {
    showError("headerErrorBanner", t("library.action_error", { error: err.message }));
  }
}

// Header button, reachable from any view (like Clear cache/My game
// crashed) — reveals SIMS4_MODS_DIR in the file manager, same xdg-open
// pattern as every other "open location" action in this app.
async function doOpenModsFolder() {
  try {
    await apiRequest("/api/settings/open-mods-folder", { method: "POST" });
  } catch (err) {
    showError("headerErrorBanner", t("library.action_error", { error: err.message }));
  }
}

// --- CurseForge fingerprint-match popup -------------------------------------------
//
// Header button (Direct Mode only — see renderStatus()) opens a popup that
// drives backend/curseforge_match.py's chunked, resumable match: POST
// .../start once, then loop POST .../step (curseforge_match.CHUNK_SIZE mods
// per call) until `done`. Each step's matches are already committed on the
// backend by the time its response comes back, so "stop" here is purely
// client-side — setting state.matchCurseforgeRunning false just means the
// loop doesn't schedule another step after the one already in flight;
// nothing already found is ever lost.

function setMatchCurseforgeProgress(checked, total, matched) {
  document.getElementById("matchCurseforgeStatusText").textContent = t("library.match_curseforge_modal.status", {
    checked,
    total,
    matched,
  });
  const fill = document.getElementById("matchCurseforgeProgressFill");
  fill.style.width = total ? `${Math.round((checked / total) * 100)}%` : "0%";
}

// The one bottom button doubles as Stop (while a run is in flight) and
// Close (once it's done or the user already stopped it) — swapping label
// and handler in place rather than two separate buttons that only one of
// which is ever relevant at a time.
function setMatchCurseforgeButtonMode(mode) {
  const btn = document.getElementById("matchCurseforgeStopBtn");
  if (mode === "stop") {
    btn.textContent = t("library.match_curseforge_modal.stop_button");
    btn.onclick = () => {
      state.matchCurseforgeRunning = false;
    };
  } else {
    btn.textContent = t("library.match_curseforge_modal.close_button");
    btn.onclick = closeMatchCurseforgeModal;
  }
}

async function openMatchCurseforgeModal() {
  document.getElementById("matchCurseforgeErrorBanner").textContent = "";
  document.getElementById("matchCurseforgeErrorBanner").classList.remove("show");
  setMatchCurseforgeProgress(0, 0, 0);
  setMatchCurseforgeButtonMode("stop");
  state.matchCurseforgeRunning = true;
  document.getElementById("matchCurseforgeOverlay").classList.add("show");

  let session;
  try {
    session = await apiRequest("/api/settings/match-curseforge/start", { method: "POST" });
  } catch (err) {
    showError("matchCurseforgeErrorBanner", t("library.action_error", { error: err.message }));
    setMatchCurseforgeButtonMode("close");
    state.matchCurseforgeRunning = false;
    return;
  }
  setMatchCurseforgeProgress(session.checked, session.total, session.matched);
  if (session.total === 0) {
    document.getElementById("matchCurseforgeStatusText").textContent = t("library.match_curseforge_modal.none_to_check");
    setMatchCurseforgeButtonMode("close");
    state.matchCurseforgeRunning = false;
    return;
  }
  await runMatchCurseforgeStep();
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

const MATCH_CURSEFORGE_MAX_RETRIES = 4;
const MATCH_CURSEFORGE_RETRY_DELAY_MS = 2000;

// A single chunk can fail transiently (a slow/rate-limited CurseForge
// response, a network blip) — backend/curseforge_match.py's run_step()
// deliberately leaves its session state untouched until the API calls
// succeed, specifically so retrying the exact same POST retries the exact
// same chunk rather than skipping or duplicating it (see its docstring).
// The backend reports this as a 502 (main.py), distinguishing it from a
// real error (400 no-session, etc.) that shouldn't be retried at all — an
// un-retried transient failure was the whole "it just stops on its own"
// complaint this replaced.
async function runMatchCurseforgeStep(retryCount = 0) {
  let session;
  try {
    session = await apiRequest("/api/settings/match-curseforge/step", { method: "POST" });
  } catch (err) {
    if (state.matchCurseforgeRunning && err.status === 502 && retryCount < MATCH_CURSEFORGE_MAX_RETRIES) {
      document.getElementById("matchCurseforgeStatusText").textContent = t(
        "library.match_curseforge_modal.retrying",
        { attempt: retryCount + 1, max: MATCH_CURSEFORGE_MAX_RETRIES }
      );
      await sleep(MATCH_CURSEFORGE_RETRY_DELAY_MS * (retryCount + 1));
      if (state.matchCurseforgeRunning) await runMatchCurseforgeStep(retryCount + 1);
      return;
    }
    showError("matchCurseforgeErrorBanner", t("library.action_error", { error: err.message }));
    setMatchCurseforgeButtonMode("close");
    state.matchCurseforgeRunning = false;
    return;
  }
  setMatchCurseforgeProgress(session.checked, session.total, session.matched);
  // Every step's matches are already committed — refresh the Library so a
  // "Linked" badge/thumbnail that just landed shows up while the popup is
  // still open, not only after closing it.
  await loadMods();
  render();

  if (session.done) {
    document.getElementById("matchCurseforgeStatusText").textContent = t("library.match_curseforge_modal.done", {
      checked: session.checked,
      matched: session.matched,
    });
    setMatchCurseforgeButtonMode("close");
    state.matchCurseforgeRunning = false;
    return;
  }
  if (!state.matchCurseforgeRunning) {
    // Stopped by the user mid-run — the popup stays open showing exactly
    // where it left off, not silently closed out from under them.
    document.getElementById("matchCurseforgeStatusText").textContent = t("library.match_curseforge_modal.stopped", {
      checked: session.checked,
      matched: session.matched,
    });
    setMatchCurseforgeButtonMode("close");
    return;
  }
  await runMatchCurseforgeStep(0); // reset retry count — this step succeeded
}

function closeMatchCurseforgeModal() {
  state.matchCurseforgeRunning = false;
  document.getElementById("matchCurseforgeOverlay").classList.remove("show");
}

// --- CurseForge sync popup ("Synchronize") ------------------------------------------
//
// Same chunked/resumable/progress-popup shape as the match-curseforge block
// above, driving backend/curseforge_dependencies.py's SyncSession instead:
// refreshes declared dependencies + compat_status for every already-linked
// mod (not just newly-matching loose ones). Simpler error handling than
// match-curseforge's step loop, though — run_sync_step() already absorbs a
// single mod's transient failure internally (counted in `errors`, that mod
// just stays stale for a later run) rather than failing the whole chunk, so
// there's no retryable-502 case here to retry client-side, only the
// not-retryable 401 (key rejected mid-run).

function setSyncCurseforgeProgress(checked, total, synced) {
  document.getElementById("syncCurseforgeStatusText").textContent = t("library.sync_curseforge_modal.status", {
    checked,
    total,
    synced,
  });
  const fill = document.getElementById("syncCurseforgeProgressFill");
  fill.style.width = total ? `${Math.round((checked / total) * 100)}%` : "0%";
}

function setSyncCurseforgeButtonMode(mode) {
  const btn = document.getElementById("syncCurseforgeStopBtn");
  if (mode === "stop") {
    btn.textContent = t("library.match_curseforge_modal.stop_button"); // shared generic "Stop"/"Close" text
    btn.onclick = () => {
      state.syncCurseforgeRunning = false;
    };
  } else {
    btn.textContent = t("library.match_curseforge_modal.close_button");
    btn.onclick = closeSyncCurseforgeModal;
  }
}

async function openSyncCurseforgeModal() {
  document.getElementById("syncCurseforgeErrorBanner").textContent = "";
  document.getElementById("syncCurseforgeErrorBanner").classList.remove("show");
  setSyncCurseforgeProgress(0, 0, 0);
  setSyncCurseforgeButtonMode("stop");
  state.syncCurseforgeRunning = true;
  document.getElementById("syncCurseforgeOverlay").classList.add("show");

  let session;
  try {
    session = await apiRequest("/api/settings/sync-curseforge/start", { method: "POST" });
  } catch (err) {
    showError("syncCurseforgeErrorBanner", t("library.action_error", { error: err.message }));
    setSyncCurseforgeButtonMode("close");
    state.syncCurseforgeRunning = false;
    return;
  }
  setSyncCurseforgeProgress(session.checked, session.total, session.synced);
  if (session.total === 0) {
    document.getElementById("syncCurseforgeStatusText").textContent = t("library.sync_curseforge_modal.none_to_check");
    setSyncCurseforgeButtonMode("close");
    state.syncCurseforgeRunning = false;
    return;
  }
  await runSyncCurseforgeStep();
}

async function runSyncCurseforgeStep() {
  let session;
  try {
    session = await apiRequest("/api/settings/sync-curseforge/step", { method: "POST" });
  } catch (err) {
    showError("syncCurseforgeErrorBanner", t("library.action_error", { error: err.message }));
    setSyncCurseforgeButtonMode("close");
    state.syncCurseforgeRunning = false;
    return;
  }
  setSyncCurseforgeProgress(session.checked, session.total, session.synced);
  // Every step's updates are already committed — refresh the Library so a
  // compat badge/dependency that just landed shows up while the popup is
  // still open, not only after closing it.
  await loadMods();
  render();

  if (session.done) {
    document.getElementById("syncCurseforgeStatusText").textContent = t("library.sync_curseforge_modal.done", {
      checked: session.checked,
      synced: session.synced,
      errors: session.errors,
    });
    setSyncCurseforgeButtonMode("close");
    state.syncCurseforgeRunning = false;
    return;
  }
  if (!state.syncCurseforgeRunning) {
    // Stopped by the user mid-run — the popup stays open showing exactly
    // where it left off, not silently closed out from under them.
    document.getElementById("syncCurseforgeStatusText").textContent = t("library.sync_curseforge_modal.stopped", {
      checked: session.checked,
      synced: session.synced,
    });
    setSyncCurseforgeButtonMode("close");
    return;
  }
  await runSyncCurseforgeStep();
}

function closeSyncCurseforgeModal() {
  state.syncCurseforgeRunning = false;
  document.getElementById("syncCurseforgeOverlay").classList.remove("show");
}

async function clickClearCache() {
  let targets;
  try {
    targets = await apiRequest("/api/cache/targets");
  } catch (err) {
    // No popup is open yet at this point (the fetch that would feed one
    // just failed) — headerErrorBanner floats under the header buttons
    // themselves instead of a banner buried inside an unopened modal.
    showError("headerErrorBanner", t("crash.clear_cache_error", { error: err.message }));
    return;
  }
  openConfirmModal({
    title: t("crash.clear_cache_title"),
    extraNodes: buildCacheTargetNodes(targets),
    confirmLabel: t("crash.clear_cache_confirm"),
    onConfirm: doClearCache,
  });
}

// CLAUDE.md's Cache cleanup: "Suggest cleanup automatically after
// install/update/disable actions, but always require confirmation before
// deleting" — reuses the exact same targets/confirm/clean flow as the
// manual "Clear cache" button above, just triggered proactively. Stays
// silent (no modal, no error banner) if the target list is empty or can't
// be fetched — this is a courtesy suggestion, not the primary action the
// user just took, so it should never be the thing that surfaces an error.
async function suggestCacheCleanup() {
  let targets;
  try {
    targets = await apiRequest("/api/cache/targets");
  } catch (err) {
    return;
  }
  if (!targets.length) return;
  openConfirmModal({
    title: t("cache.suggest_title"),
    message: t("cache.suggest_message"),
    extraNodes: buildCacheTargetNodes(targets),
    confirmLabel: t("crash.clear_cache_confirm"),
    onConfirm: doClearCache,
  });
}

async function doClearCache() {
  try {
    await apiRequest("/api/cache/clean", { method: "POST" });
  } catch (err) {
    // closeConfirm() below always runs, so the generic confirm modal is
    // gone by the time this would render — same floating banner
    // clickClearCache() uses, not the (possibly closed) crash modal's own.
    showError("headerErrorBanner", t("crash.clear_cache_error", { error: err.message }));
  } finally {
    closeConfirm();
  }
}

// --- settings view -----------------------------------------------------------------

function initSettingsView() {
  if (!state.settingsWired) {
    state.settingsWired = true;
    const select = document.getElementById("languageSelect");
    select.value = state.lang;
    select.addEventListener("change", (e) => switchLanguage(e.target.value));

    document.getElementById("fullScanButton").addEventListener("click", doFullScan);
    document.getElementById("addBlacklistButton").addEventListener("click", doAddBlacklistEntry);
    document.getElementById("savePathsButton").addEventListener("click", doSavePaths);
    document.getElementById("importLooseFilesButton").addEventListener("click", doImportLooseFiles);
    document.getElementById("clearNamespaceOverridesButton").addEventListener("click", confirmClearNamespaceOverrides);
    document.getElementById("releaseCompatQuarantineButton").addEventListener("click", doReleaseCompatQuarantine);
    document.getElementById("browseGameDirBtn").addEventListener("click", () => doBrowseFolder("settingsGameDirInput"));
    document.getElementById("browseUserDirBtn").addEventListener("click", () => doBrowseFolder("settingsUserDirInput"));
    document
      .getElementById("browseLibraryDirBtn")
      .addEventListener("click", () => doBrowseFolder("settingsLibraryDirInput"));
  }
  // Cleared on every visit (not just the one-time wiring above) so a
  // notice from a previous save doesn't linger after navigating away and
  // back — doSavePaths() itself re-shows these right after loadSettings()
  // refreshes the input values below, so this only ever matters for a
  // fresh, unrelated visit to the Settings view.
  document.getElementById("settingsPathsErrorBanner").textContent = "";
  document.getElementById("settingsPathsWarnings").hidden = true;
  document.getElementById("settingsPathsSavedNotice").hidden = true;
  loadSettings();
  loadBlacklist();
  loadCompatQuarantineManageList();
}

// game_dir_no_executable is currently the only warning code path_settings.py
// returns — mapped through i18n like every other user-facing string, unlike
// a hard-rejection error's raw message (see doSavePaths()'s catch, which
// follows this app's existing convention of passing exception text straight
// into the generic "Error: {error}" template).
function pathWarningText(code) {
  return t(`settings.path_warnings.${code}`);
}

// Native OS folder picker (POST /api/settings/pick-folder, zenity — see
// main.py's comment on why this has to be a backend call rather than a
// plain <input type="file" webkitdirectory>, which can't resolve to a real
// absolute path). Fills the field it was opened from; typing the path
// directly still works exactly as before, this is just a shortcut.
// {path: null} covers both a cancelled dialog and picking the same folder
// already typed — either way there's nothing to change, so it's silent.
async function doBrowseFolder(inputId) {
  const input = document.getElementById(inputId);
  let result;
  try {
    result = await apiRequest("/api/settings/pick-folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ initial_dir: input.value.trim() || null }),
    });
  } catch (err) {
    showError("settingsPathsErrorBanner", t("settings.browse_folder_error", { error: err.message }));
    return;
  }
  if (result.path) input.value = result.path;
}

async function doSavePaths() {
  const payload = {
    sims4_game_dir: document.getElementById("settingsGameDirInput").value.trim(),
    sims4_user_dir: document.getElementById("settingsUserDirInput").value.trim(),
    library_dir: document.getElementById("settingsLibraryDirInput").value.trim(),
  };
  const errorBanner = document.getElementById("settingsPathsErrorBanner");
  const warningsEl = document.getElementById("settingsPathsWarnings");
  const savedNotice = document.getElementById("settingsPathsSavedNotice");
  errorBanner.textContent = "";
  warningsEl.hidden = true;
  savedNotice.hidden = true;

  let result;
  try {
    result = await apiRequest("/api/settings/paths", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    showError("settingsPathsErrorBanner", t("library.action_error", { error: err.message }));
    return;
  }

  if (result.warnings.length) {
    warningsEl.hidden = false;
    warningsEl.textContent = result.warnings.map(pathWarningText).join(" ");
  }
  savedNotice.hidden = false;
  await loadSettings();
}

async function loadSettings() {
  const container = document.getElementById("settingsFolders");
  container.innerHTML = "";
  let settings;
  try {
    settings = await apiRequest("/api/settings");
  } catch (err) {
    showError("settingsErrorBanner", t("library.action_error", { error: err.message }));
    return;
  }
  [
    ["settings.folder.mods_dir", settings.mods_dir],
    ["settings.folder.download_watch_dir", settings.download_watch_dir],
  ].forEach(([labelKey, value]) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    row.appendChild(elementWithText("span", null, t(labelKey)));
    row.appendChild(elementWithText("span", "value", value));
    container.appendChild(row);
  });

  document.getElementById("settingsGameDirInput").value = settings.game_dir;
  document.getElementById("settingsUserDirInput").value = settings.user_dir;
  document.getElementById("settingsLibraryDirInput").value = settings.library_dir;

  const backupsContainer = document.getElementById("settingsBackups");
  backupsContainer.innerHTML = "";
  [["settings.backup_retention_count", String(settings.backup_retention_count)]].forEach(([labelKey, value]) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    row.appendChild(elementWithText("span", null, t(labelKey)));
    row.appendChild(elementWithText("span", "value", value));
    backupsContainer.appendChild(row);
  });

  const watcherEl = document.getElementById("settingsModsWatcher");
  watcherEl.innerHTML = "";
  watcherEl.appendChild(elementWithText("span", null, t("settings.mods_watcher_label")));
  watcherEl.appendChild(
    elementWithText(
      "span",
      "value",
      settings.mods_watcher_enabled ? t("settings.mods_watcher_enabled") : t("settings.mods_watcher_disabled")
    )
  );

  const loggingContainer = document.getElementById("settingsLogging");
  loggingContainer.innerHTML = "";
  [
    ["settings.log_level_label", settings.log_level],
    ["settings.log_path_label", settings.log_path],
  ].forEach(([labelKey, value]) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    row.appendChild(elementWithText("span", null, t(labelKey)));
    row.appendChild(elementWithText("span", "value", value));
    loggingContainer.appendChild(row);
  });

  renderScriptModsSetting();
}

function renderScriptModsSetting() {
  const el = document.getElementById("settingsScriptMods");
  el.innerHTML = "";
  const allowed = state.status ? state.status.script_mods_allowed : null;
  const label =
    allowed === true
      ? t("settings.script_mods_enabled")
      : allowed === false
        ? t("settings.script_mods_disabled")
        : t("library.unknown");
  el.appendChild(elementWithText("span", null, t("settings.script_mods_allowed_label")));
  el.appendChild(elementWithText("span", "value", label));
}

function applyViewMode(mode) {
  state.viewMode = mode;
  document.getElementById("grid").classList.toggle("grid-list-mode", mode === "list");
  document.querySelectorAll("#viewToggle .view-toggle-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.viewMode === mode);
  });
  localStorage.setItem("simslink-view-mode", mode);
}

function wireViewToggle() {
  document.querySelectorAll("#viewToggle .view-toggle-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyViewMode(btn.dataset.viewMode);
      render();
    });
  });
}

// A real 3-position switch (see #typeFilterToggle in index.html). "Mix" is
// value "" — the neutral/no-filter position, not primary_type === 'mixed' —
// so it shows CC-only, script-only, and genuinely mixed mods alike; "CC"/
// "Scripts" (value "package"/"script") narrow the grid to that one
// primary_type only.
function applyTypeFilter(value) {
  state.typeFilter = value;
  document.querySelectorAll("#typeFilterToggle .type-filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.typeFilter === value);
  });
  localStorage.setItem("simslink-type-filter", value);
}

function wireTypeFilterToggle() {
  document.querySelectorAll("#typeFilterToggle .type-filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyTypeFilter(btn.dataset.typeFilter);
      render();
    });
  });
}

// How many real installed mods (state.mods — not broken-folder pseudo-mods,
// which have no primary_type) each of the three type-filter positions above
// would show — "Mix" is the neutral/no-filter position, so its count is the
// full library, same as clicking it would reveal. Deliberately independent
// of every other active filter (state filter, search, loose/translation
// chips), same "always the raw library total" convention the loose/
// translation filter chips already use — this answers "how many of each do
// I have", not "how many are currently visible". Called at the end of
// render() (after state.mods is populated and after any applyStaticI18n()
// call, since that always runs before render()), so it's the sole owner of
// these three buttons' text — no data-i18n attribute on them in index.html.
function renderTypeFilterCounts() {
  let packageCount = 0;
  let scriptCount = 0;
  state.mods.forEach((mod) => {
    if (mod.primary_type === "package") packageCount++;
    else if (mod.primary_type === "script") scriptCount++;
  });
  document.querySelector('#typeFilterToggle [data-type-filter="package"]').textContent =
    `${t("library.type_filter.cc")} (${packageCount})`;
  document.querySelector('#typeFilterToggle [data-type-filter=""]').textContent =
    `${t("library.type_filter.mix")} (${state.mods.length})`;
  document.querySelector('#typeFilterToggle [data-type-filter="script"]').textContent =
    `${t("library.type_filter.scripts")} (${scriptCount})`;
}

// Same real-switch pattern as applyTypeFilter() above, one position always
// active — "Tout" is value "" (no filter). See stateFilterBucket() for what
// each of the other five positions actually matches.
function applyStateFilter(value) {
  state.stateFilter = value;
  document.querySelectorAll("#stateFilterToggle .type-filter-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.stateFilter === value);
  });
  localStorage.setItem("simslink-state-filter", value);
}

function wireStateFilterToggle() {
  document.querySelectorAll("#stateFilterToggle .type-filter-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      applyStateFilter(btn.dataset.stateFilter);
      render();
    });
  });
}

// Shared by every "Yes/No" segmented switch in the head controls (simplified
// names, group by author, ...) — sets which button looks active without
// touching state or firing a callback, so init() can sync the UI to a
// restored preference without re-triggering its own change handler.
function setYesNoSwitch(containerId, enabled) {
  document.querySelectorAll(`#${containerId} .switch-btn`).forEach((btn) => {
    btn.classList.toggle("active", (btn.dataset.value === "yes") === enabled);
  });
}

function wireYesNoSwitch(containerId, onChange) {
  document.querySelectorAll(`#${containerId} .switch-btn`).forEach((btn) => {
    btn.addEventListener("click", () => onChange(btn.dataset.value === "yes"));
  });
}

// Whether card/row titles show the cleaned-up name (bracket prefix, trailing
// version, and inferred series prefix all stripped — see groupingAuthor())
// or the mod's original, untouched name. Purely a display preference, same
// as tile size — grouping/sorting/the avatar label are unaffected either
// way, only the title text shown in buildCard()/buildListRow().
function applySimplifiedNames(enabled) {
  state.simplifiedNames = enabled;
  setYesNoSwitch("simplifiedNamesSwitch", enabled);
  localStorage.setItem("simslink-simplified-names", enabled ? "1" : "0");
}

function wireSimplifiedNamesToggle() {
  wireYesNoSwitch("simplifiedNamesSwitch", (enabled) => {
    applySimplifiedNames(enabled);
    render();
  });
}

// Whether the Library groups mods under an author header at all. Off:
// render() sorts by tier then name only, with no author headers (and
// nothing to collapse — see the author-group-header click handler).
function applyGroupByAuthor(enabled) {
  state.groupByAuthor = enabled;
  setYesNoSwitch("groupByAuthorSwitch", enabled);
  localStorage.setItem("simslink-group-by-author", enabled ? "1" : "0");
}

function wireGroupByAuthorToggle() {
  wireYesNoSwitch("groupByAuthorSwitch", (enabled) => {
    applyGroupByAuthor(enabled);
    render();
  });
}

// --- save/load library state (icons on the Library page, left of the switches) -----
// Moved here from a dedicated Settings section — same underlying
// profiles.py feature (see CLAUDE.md's "Mod profiles" for why there's no
// parallel backend), just triggered from two icons instead of a page
// section, since there's rarely more than a couple of saved states to pick
// from at once.

const SAVE_ICON_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/>' +
  '<polyline points="17 21 17 13 7 13 7 21"/><polyline points="7 3 7 8 15 8"/></svg>';

// A plain open folder — clearer for "load a saved state" than the
// previous restore/history-style arrow, and pairs naturally with the
// floppy-disk "save" icon above (put something away / bring it back out).
const LOAD_ICON_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg>';

function wireSaveLoadStateButtons() {
  document.getElementById("saveStateBtn").addEventListener("click", promptSaveState);
  document.getElementById("loadStateBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    toggleLoadStateDropdown();
  });
  // Click-outside-to-close: the load button's own click also reaches this
  // (events bubble up to document after the button's handler runs), but
  // it's inside .state-dropdown-wrap, so the containment check below
  // correctly leaves the dropdown alone in that case.
  document.addEventListener("click", (e) => {
    const wrap = document.querySelector(".state-dropdown-wrap");
    if (wrap && !wrap.contains(e.target)) closeLoadStateDropdown();
  });
}

function promptSaveState() {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "text-input";
  input.placeholder = t("settings.profile_name_placeholder");
  input.style.width = "100%";
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") document.getElementById("confirmOkBtn").click();
  });
  openConfirmModal({
    title: t("library.save_state_confirm.title"),
    extraNodes: [input],
    confirmLabel: t("library.save_state_confirm.confirm"),
    onConfirm: () => doSaveState(input.value.trim()),
  });
  input.focus();
}

async function doSaveState(name) {
  if (!name) return;
  try {
    const profile = await apiRequest("/api/profiles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    const modIds = state.mods.filter((m) => m.active).map((m) => m.id);
    await apiRequest(`/api/profiles/${profile.id}/mods`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mod_ids: modIds }),
    });
    closeConfirm();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

async function toggleLoadStateDropdown() {
  const dropdown = document.getElementById("loadStateDropdown");
  if (!dropdown.hidden) {
    closeLoadStateDropdown();
    return;
  }
  dropdown.hidden = false;
  await loadStateList();
}

function closeLoadStateDropdown() {
  document.getElementById("loadStateDropdown").hidden = true;
}

async function loadStateList() {
  const container = document.getElementById("loadStateList");
  container.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/profiles");
  } catch (err) {
    showError("loadStateErrorBanner", t("settings.profiles_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    container.appendChild(elementWithText("div", "empty-inline", t("settings.profiles_empty")));
    return;
  }
  items.forEach((profile) => container.appendChild(buildLoadStateRow(profile)));
}

function buildLoadStateRow(profile) {
  const row = document.createElement("div");
  row.className = "state-row";

  const top = document.createElement("div");
  top.className = "state-row-top";
  top.appendChild(elementWithText("span", "name", profile.name));

  const actions = document.createElement("span");
  actions.className = "actions";
  const loadBtn = document.createElement("button");
  loadBtn.className = "icon-btn";
  loadBtn.title = t("settings.activate_profile_button");
  loadBtn.innerHTML = LOAD_ICON_SVG;
  loadBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    doActivateProfile(profile.id, loadBtn);
  });
  const deleteBtn = document.createElement("button");
  deleteBtn.className = "icon-btn delete-icon-btn";
  deleteBtn.title = t("settings.delete_profile_button");
  deleteBtn.innerHTML = DELETE_ICON_SVG;
  deleteBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    doDeleteProfile(profile.id);
  });
  actions.appendChild(loadBtn);
  actions.appendChild(deleteBtn);
  top.appendChild(actions);
  row.appendChild(top);

  row.appendChild(
    elementWithText(
      "div",
      "state-row-meta",
      `${t("settings.profile_mod_count", { count: profile.mod_ids.length })} ${t("settings.profile_saved_at", {
        date: formatSavedAt(profile.created_date),
      })}`
    )
  );

  return row;
}

async function doActivateProfile(profileId, button) {
  button.disabled = true;
  try {
    await apiRequest(`/api/profiles/${profileId}/activate`, { method: "POST" });
    closeLoadStateDropdown();
    await loadMods();
    render();
  } catch (err) {
    showError("loadStateErrorBanner", t("settings.profiles_error", { error: err.message }));
  } finally {
    button.disabled = false;
  }
}

async function doDeleteProfile(profileId) {
  try {
    await apiRequest(`/api/profiles/${profileId}`, { method: "DELETE" });
    await loadStateList();
  } catch (err) {
    showError("loadStateErrorBanner", t("settings.profiles_error", { error: err.message }));
  }
}

// --- blacklist -----------------------------------------------------------------------

async function loadBlacklist() {
  const container = document.getElementById("blacklistList");
  container.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/blacklist");
  } catch (err) {
    showError("blacklistErrorBanner", t("settings.blacklist_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    container.appendChild(elementWithText("div", "empty-inline", t("settings.blacklist_empty")));
    return;
  }
  items.forEach((entry) => container.appendChild(buildBlacklistRow(entry)));
}

function buildBlacklistRow(entry) {
  const row = document.createElement("div");
  row.className = "list-row";

  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("span", null, entry.pattern));
  if (entry.note) info.appendChild(elementWithText("span", "note", entry.note));
  row.appendChild(info);

  const deleteBtn = document.createElement("button");
  deleteBtn.className = "btn btn-sm";
  deleteBtn.textContent = t("settings.remove_blacklist_button");
  deleteBtn.addEventListener("click", () => doRemoveBlacklistEntry(entry.id));
  row.appendChild(deleteBtn);

  return row;
}

async function doAddBlacklistEntry() {
  const input = document.getElementById("newBlacklistPatternInput");
  const pattern = input.value.trim();
  if (!pattern) return;
  try {
    await apiRequest("/api/blacklist", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pattern }),
    });
    input.value = "";
    await loadBlacklist();
    await loadMods(); // blacklist matches feed into the Library warnings banner
    render();
  } catch (err) {
    showError("blacklistErrorBanner", t("settings.blacklist_error", { error: err.message }));
  }
}

async function doRemoveBlacklistEntry(entryId) {
  try {
    await apiRequest(`/api/blacklist/${entryId}`, { method: "DELETE" });
    await loadBlacklist();
    await loadMods();
    render();
  } catch (err) {
    showError("blacklistErrorBanner", t("settings.blacklist_error", { error: err.message }));
  }
}

// --- compat quarantine management (Settings) ------------------------------------------

async function loadCompatQuarantineManageList() {
  const container = document.getElementById("compatQuarantineManageList");
  container.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/compat/quarantine");
  } catch (err) {
    showError("settingsCompatQuarantineErrorBanner", t("settings.compat_quarantine_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    container.appendChild(elementWithText("div", "empty-inline", t("settings.compat_quarantine_empty")));
    return;
  }
  items.forEach((entry) => container.appendChild(buildCompatQuarantineRow(entry)));
}

function buildCompatQuarantineRow(entry) {
  const row = document.createElement("div");
  row.className = "list-row";

  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("span", null, entry.name));
  const reasonText =
    entry.reason === "incompatible"
      ? t("library.compat_quarantine_banner.reason_incompatible")
      : t("library.compat_quarantine_banner.reason_depends_on", { name: entry.reason });
  info.appendChild(elementWithText("span", "note", reasonText));
  row.appendChild(info);

  const statusClass = entry.compat_status === "incompatible" ? "cc-badge warn-badge" : "cc-badge";
  const statusText =
    entry.compat_status === "incompatible"
      ? t("library.incompatible_tag")
      : t("settings.compat_quarantine_ready_badge");
  row.appendChild(elementWithText("span", statusClass, statusText));

  const forgetBtn = document.createElement("button");
  forgetBtn.className = "btn btn-sm";
  forgetBtn.textContent = t("settings.compat_quarantine_forget_button");
  forgetBtn.addEventListener("click", () => doForgetQuarantined(entry.mod_id));
  row.appendChild(forgetBtn);

  return row;
}

async function doForgetQuarantined(modId) {
  try {
    await apiRequest(`/api/compat/quarantine/${encodeURIComponent(modId)}`, { method: "DELETE" });
    await loadCompatQuarantineManageList();
  } catch (err) {
    showError("settingsCompatQuarantineErrorBanner", t("settings.compat_quarantine_error", { error: err.message }));
  }
}

async function doReleaseCompatQuarantine() {
  const resultEl = document.getElementById("settingsCompatQuarantineResult");
  resultEl.textContent = "";
  try {
    const result = await apiRequest("/api/compat/quarantine/release", { method: "POST" });
    resultEl.textContent = t("settings.compat_quarantine_release_result", {
      released: result.released.length,
      stillIncompatible: result.still_incompatible.length,
      failed: result.failed.length,
    });
    await loadCompatQuarantineManageList();
    await loadMods();
    render();
  } catch (err) {
    showError("settingsCompatQuarantineErrorBanner", t("settings.compat_quarantine_error", { error: err.message }));
  }
}

async function doFullScan() {
  const button = document.getElementById("fullScanButton");
  const resultEl = document.getElementById("settingsScanResult");
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = t("settings.full_scan_running");
  resultEl.textContent = "";
  try {
    const stats = await apiRequest("/api/settings/full-scan", { method: "POST" });
    resultEl.textContent = t("settings.full_scan_result", stats);
    await loadMods(); // hashes/removed files may have changed what Library shows
    render();
  } catch (err) {
    showError("settingsErrorBanner", t("settings.full_scan_error", { error: err.message }));
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function doImportLooseFiles() {
  const button = document.getElementById("importLooseFilesButton");
  const resultEl = document.getElementById("settingsLooseImportResult");
  button.disabled = true;
  const originalLabel = button.textContent;
  button.textContent = t("settings.import_loose_files_running");
  resultEl.textContent = "";
  try {
    const stats = await apiRequest("/api/settings/import-loose-files", { method: "POST" });
    resultEl.textContent = t("settings.import_loose_files_result", stats);
    // Refreshes state.looseSuggestions too (part of fetchLibraryData()), so
    // the Library's "Mods fusionnables" banner (renderMergeableBanner())
    // picks up any newly-eligible groups from this import without a
    // separate fetch — that banner replaced this Settings section's own
    // former inline suggestions list.
    await loadMods();
    render();
  } catch (err) {
    showError("settingsLooseErrorBanner", t("library.action_error", { error: err.message }));
  } finally {
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

async function switchLanguage(lang) {
  state.lang = lang;
  await loadI18n(lang);
  applyStaticI18n();
  renderStatus();
  render();
  closeDetail();
  // Catalog/Updates results and the Crash Mode status area are ephemeral,
  // action-triggered content (not cached in `state`) — they simply show in
  // the previous language until the next search/check/analyze, same
  // trade-off Settings already accepts elsewhere per CLAUDE.md ("no
  // persistence layer yet" for this view).
  if (state.settingsWired) loadSettings();
}

// --- detail panel ----------------------------------------------------------------

// Routes a card/row click straight to the duplicate comparator when the
// clicked mod is part of a real duplicate group (exact_duplicate_mod/
// folder_duplication — see findDuplicateGroupForMod()), regardless of
// whether *this particular* mod is the one visually tagged "Duplicate"/
// "Identical content" (duplicateTagModIds() only tags the presumed-copy
// side of a pair) — clicking either member of the pair opens the same
// comparator.
function openModDetailOrCompare(mod) {
  if (findDuplicateGroupForMod(mod.id)) {
    openDuplicateComparison(mod.id);
  } else {
    openDetail(mod.id);
  }
}

async function openDetail(modId) {
  let mod;
  try {
    mod = await apiRequest(`/api/mods/${encodeURIComponent(modId)}`);
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
    return;
  }
  if (state.dupCompareGroup) closeDuplicateComparison(); // in case it was open behind it
  state.currentDetailId = modId;
  renderDetail(mod);
  document.getElementById("detail").classList.add("show");
  document.getElementById("overlay").classList.add("show");
}

// Not a real installed mod — no /api/mods/{id} row to fetch, no
// dependencies/conflicts/translation data to show. Reuses the same detail
// panel DOM as openDetail()/renderDetail() below, just populated from the
// folder object already in hand (see visibleBrokenModFolders()) instead of
// a network round-trip.
function openBrokenDetail(folder) {
  if (state.dupCompareGroup) closeDuplicateComparison(); // in case it was open behind it
  state.currentDetailId = `broken:${folder.name}`;
  renderBrokenDetail(folder);
  document.getElementById("detail").classList.add("show");
  document.getElementById("overlay").classList.add("show");
}

function renderBrokenDetail(folder) {
  document.querySelector(".detail-hero").className = "detail-hero " + brokenStateClass(folder);

  const { displayName, version } = splitNameVersion(folder.name);
  document.getElementById("dName").textContent = titleText({ name: folder.name }, displayName);
  document.getElementById("dAuthor").textContent = t("library.detail.author", { value: t("library.unknown") });
  document.getElementById("dVersion").textContent = version ? formatVersionBadge(version) : t("library.unknown");
  document.getElementById("dType").textContent = t(
    folder.reason === "unextracted_archive" ? "library.archive_tag" : "library.broken_tag"
  );
  document.getElementById("dCurseforgeLink").hidden = true; // a broken folder is never linked
  document.getElementById("dDesc").textContent = t(`library.conflicts.broken_mod_${folder.reason}`);

  document.getElementById("dConflictsSection").hidden = true;
  document.getElementById("dDependenciesSection").hidden = true;

  const filesContainer = document.getElementById("dFiles");
  filesContainer.innerHTML = "";
  if (!folder.sample_files.length) {
    filesContainer.appendChild(elementWithText("div", "empty-inline", t("library.detail.no_files")));
  } else {
    folder.sample_files.forEach((path) => filesContainer.appendChild(elementWithText("div", "file-row", path)));
    const remaining = folder.file_count - folder.sample_files.length;
    if (remaining > 0) {
      filesContainer.appendChild(elementWithText("div", "empty-inline", `+${remaining}…`));
    }
  }

  document.getElementById("openFolderBtn").hidden = true;
  document.getElementById("toggleActiveBtn").hidden = true;
  document.getElementById("deleteBtn").hidden = true;
  const brokenActions = document.getElementById("dBrokenActions");
  brokenActions.hidden = false;
  brokenActions.innerHTML = "";
  const spec = brokenActionSpec(folder);
  if (spec) {
    const actionBtn = document.createElement("button");
    actionBtn.className = `icon-btn detail-icon-btn ${spec.iconBtnClass}`;
    actionBtn.title = spec.label;
    actionBtn.innerHTML = spec.icon;
    actionBtn.onclick = spec.onClick;
    brokenActions.appendChild(actionBtn);
  }
  const deleteBtn = document.createElement("button");
  deleteBtn.className = "icon-btn detail-icon-btn danger";
  deleteBtn.title = t("library.detail.delete_button");
  deleteBtn.innerHTML = DELETE_ICON_SVG;
  deleteBtn.onclick = () => confirmDeleteBrokenFolder(folder);
  brokenActions.appendChild(deleteBtn);
}

function closeDetail() {
  document.getElementById("detail").classList.remove("show");
  document.getElementById("overlay").classList.remove("show");
  state.currentDetailId = null;
}

// A mod already flagged by conflict_detector.py or blacklist.py — same
// "problem" notion problemModIds() uses for the Library grid's red border,
// checked independently here since a detail view can be opened straight
// from a card that already computed it, but also directly (e.g. a future
// deep link) without that context handy.
function isProblemMod(modId) {
  return (
    state.conflicts.some((g) => g.mods.some((m) => m.id === modId)) ||
    state.blacklistMatches.some((m) => m.mod_id === modId)
  );
}

function renderDetail(mod) {
  const rezipZips = rezippedZipPaths(mod.id);
  document.querySelector(".detail-hero").className =
    "detail-hero" + (rezipZips ? " is-archive" : isProblemMod(mod.id) ? " is-problem" : "");

  document.getElementById("openFolderBtn").hidden = false;
  document.getElementById("toggleActiveBtn").hidden = false;
  document.getElementById("deleteBtn").hidden = false;
  // Unlike a true broken folder (renderBrokenDetail(), which has no
  // enable/disable/delete concept of its own), a rezipped mod is still a
  // real managed mod — open-folder/toggle/delete stay visible, this
  // container just adds the one-click re-extract icon alongside them when
  // unambiguous.
  const brokenActions = document.getElementById("dBrokenActions");
  brokenActions.innerHTML = "";
  brokenActions.hidden = !rezipZips || rezipZips.length !== 1;
  if (!brokenActions.hidden) {
    const actionBtn = document.createElement("button");
    actionBtn.className = "icon-btn detail-icon-btn unzip-icon-btn";
    actionBtn.title = t("library.conflicts.broken_mod_fix_button");
    actionBtn.innerHTML = UNZIP_ICON_SVG;
    actionBtn.onclick = () => confirmFixRezippedMod(mod);
    brokenActions.appendChild(actionBtn);
  }
  document.getElementById("dDependenciesSection").hidden = false;

  // curseforge_name (when set) always wins here too — same reasoning as
  // titleText() above, just not routed through it since the detail panel
  // was never subject to the "Simplified names" toggle in the first place
  // (it always showed the full, untouched mod.name for an unlinked mod).
  document.getElementById("dName").textContent = mod.curseforge_name || mod.name;
  document.getElementById("dAuthor").textContent = t("library.detail.author", {
    value: mod.author || t("library.unknown"),
  });

  document.getElementById("dVersion").textContent = mod.installed_version || t("library.unknown");
  document.getElementById("dType").textContent = mod.primary_type || "";

  const curseforgeLinkBtn = document.getElementById("dCurseforgeLink");
  curseforgeLinkBtn.hidden = !mod.curseforge_url;
  curseforgeLinkBtn.onclick = mod.curseforge_url ? () => openExternal(mod.curseforge_url) : null;

  document.getElementById("dDesc").textContent =
    mod.full_description || mod.short_description || t("library.detail.no_description");

  renderDetailConflicts(mod);

  const depContainer = document.getElementById("dDependencies");
  depContainer.innerHTML = "";
  if (!mod.dependencies.length) {
    depContainer.appendChild(elementWithText("div", "empty-inline", t("library.detail.no_dependencies")));
  } else {
    mod.dependencies.forEach((dep) => depContainer.appendChild(buildDependencyRow(dep)));
  }
  renderDependencyGraph(mod);

  const filesContainer = document.getElementById("dFiles");
  filesContainer.innerHTML = "";
  if (!mod.files.length) {
    filesContainer.appendChild(elementWithText("div", "empty-inline", t("library.detail.no_files")));
  } else {
    mod.files.forEach((path) => filesContainer.appendChild(elementWithText("div", "file-row", path)));
  }

  document.getElementById("openFolderBtn").onclick = () => openFolder(mod.id);
  const toggleBtn = document.getElementById("toggleActiveBtn");
  toggleBtn.title = t(mod.active ? "library.detail.resolve_disable_button" : "library.detail.resolve_enable_button");
  toggleBtn.classList.toggle("active-on", mod.active);
  toggleBtn.onclick = async () => {
    await toggleActive(mod);
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  };
  document.getElementById("deleteBtn").onclick = () => confirmDelete(mod);
  document.getElementById("dTranslationSuggestions").innerHTML = "";
  document.getElementById("detectTranslationBtn").onclick = () => doDetectTranslation(mod.id);

  const curseforgeDepsBtn = document.getElementById("detectCurseforgeDependenciesBtn");
  curseforgeDepsBtn.hidden = !mod.curseforge_id || !(state.status && state.status.direct_mode);
  curseforgeDepsBtn.onclick = () => doDetectCurseforgeDependencies(mod.id);
}

// --- duplicate comparison ------------------------------------------------------------
//
// Replaces the old cramped in-panel "conflict-sides" mini comparison for a
// real duplicate match (exact_duplicate_mod/folder_duplication — see
// buildConflictSide()'s comment). Clicking a card/row tagged "Duplicate"/
// "Identical content" opens this comparator directly, no upfront popup.
//
// This used to be one full #detail-style panel per mod, side by side,
// needing a custom horizontal-wheel-scroll fix for a large group that broke
// repeatedly (three separate fix attempts, still not reliable — see
// CLAUDE.md) — and it buried the one fact that actually distinguishes two
// duplicates (where each copy lives on disk, often with a telltale
// "(1)"/"-copy" suffix) under a wall of description/dependencies/files
// identical across the whole group by definition. Now: a single vertical
// list, one compact card per mod, folder name front and center — native
// scroll, no size limit on the group.

// The strongest signal wins if a mod is somehow in more than one such group
// at once (shouldn't normally happen — conflict_detector.py's own
// precedence already suppresses weaker signals for a covered pair, but this
// stays defensive rather than assuming that always holds).
function findDuplicateGroupForMod(modId) {
  return (
    state.conflicts.find((g) => g.kind === "exact_duplicate_mod" && g.mods.some((m) => m.id === modId)) ||
    state.conflicts.find((g) => g.kind === "folder_duplication" && g.mods.some((m) => m.id === modId)) ||
    null
  );
}

// Renders straight from the group's own {id, name, author, active,
// install_date, library_path} data (see GET /api/conflicts) — no per-mod
// fetch needed, unlike the old full-detail-panel version.
function openDuplicateComparison(modId) {
  const group = findDuplicateGroupForMod(modId);
  if (!group) {
    // Shouldn't happen (callers only reach this for a mod already confirmed
    // to be part of a duplicate group), but fall back to the normal single
    // detail view rather than opening an empty comparison.
    openDetail(modId);
    return;
  }
  closeDetail(); // in case the single #detail panel was already open behind it
  state.dupCompareGroup = group;
  const list = document.getElementById("dupCompareList");
  list.innerHTML = "";
  // Highlight only the part that actually differs between the folder names
  // in *this* group — a per-name regex guess (a trailing "-051" that just
  // happens to look like a disambiguator, say) previously lit up on every
  // card independently, including a card where that "suffix" is really just
  // part of the shared name. Comparing against the group's own common
  // prefix instead only ever highlights a real difference.
  const commonPrefixLen = longestCommonPrefix(group.mods.map((m) => folderBaseName(m.library_path))).length;
  group.mods.forEach((modLite) => list.appendChild(buildDuplicateCompareCard(modLite, commonPrefixLen)));
  document.getElementById("overlay").classList.add("show");
  document.getElementById("dupComparePanel").classList.add("show");
}

// Splits the trailing folder name off a mod's on-disk library_path.
function folderBaseName(path) {
  if (!path) return "";
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || path;
}

// Character length of the longest prefix shared by every string — used to
// find where a group's folder names actually start to differ.
function longestCommonPrefix(strings) {
  if (!strings.length) return "";
  let prefix = strings[0];
  for (const s of strings.slice(1)) {
    let j = 0;
    const max = Math.min(prefix.length, s.length);
    while (j < max && prefix[j] === s[j]) j++;
    prefix = prefix.slice(0, j);
    if (!prefix) break;
  }
  return prefix;
}

function buildDuplicateCompareCard(modLite, commonPrefixLen) {
  const card = document.createElement("div");
  card.className = "dup-compare-card";

  const folderRow = document.createElement("div");
  folderRow.className = "dup-compare-card-folder";
  folderRow.innerHTML = FOLDER_ICON_SVG;
  const folderName = folderBaseName(modLite.library_path);
  folderRow.appendChild(elementWithText("span", null, folderName.slice(0, commonPrefixLen)));
  const diff = folderName.slice(commonPrefixLen);
  if (diff) folderRow.appendChild(elementWithText("span", "dup-folder-suffix", diff));
  card.appendChild(folderRow);

  card.appendChild(
    elementWithText("div", "dup-compare-card-name", modLite.author ? `${modLite.name} · ${modLite.author}` : modLite.name)
  );
  card.appendChild(
    elementWithText(
      "div",
      "dup-compare-card-meta",
      t("library.detail.conflict_side_meta", {
        date: formatInstallDate(modLite.install_date),
        state: t(modLite.active ? "library.detail.conflict_side_active" : "library.detail.conflict_side_inactive"),
      })
    )
  );

  card.appendChild(elementWithText("span", "pill dup-compare-keep-hint", t("library.dup_compare.keep_hint")));

  card.addEventListener("click", () => resolveDuplicateSelection(modLite.id));
  return card;
}

// Clicking a card keeps that mod and deletes every other member of the
// group — no upfront disable/delete choice, the comparator's whole point is
// to pick the survivor and drop the rest. dupResolving guards a same-tick
// double click while the request is in flight.
async function resolveDuplicateSelection(keepModId) {
  if (!state.dupCompareGroup || state.dupResolving) return;
  state.dupResolving = true;
  const others = state.dupCompareGroup.mods.map((m) => m.id).filter((id) => id !== keepModId);
  const errors = await applyActionToMods(others, "delete");
  state.dupResolving = false;
  await loadMods();
  render();
  closeDuplicateComparison();
  if (errors.length) {
    showError("errorBanner", t("library.detail.dedup_partial_error", { errors: errors.join("; ") }));
  } else {
    await openDetail(keepModId);
  }
}

function closeDuplicateComparison() {
  document.getElementById("overlay").classList.remove("show");
  document.getElementById("dupComparePanel").classList.remove("show");
  document.getElementById("dupCompareList").innerHTML = "";
  state.dupCompareGroup = null;
}

// #overlay is shared with the single #detail panel and the merge comparator
// below — close whichever is actually open rather than assuming it's
// always #detail.
function closeAnyDetail() {
  if (state.dupCompareGroup) {
    closeDuplicateComparison();
  } else if (state.mergeCompareGroup) {
    closeMergeComparison();
  } else {
    closeDetail();
  }
}

// --- merge comparison ------------------------------------------------------------
//
// The manual resolution tool for loose_mods.suggest_groupings() — same
// single vertical-list shell as the duplicate comparator above, checkbox-
// select instead of click-to-keep since merging combines an arbitrary
// subset of the group rather than picking one survivor. What actually
// matters for this decision isn't per-mod detail (the group's own banner
// row already shows the names) but whether the merge can be trusted to
// reconstruct the mod correctly at all — a property of the whole group
// (loose_mods.suggest_groupings()'s curseforge_id: a confirmed shared
// identity, matched via real file fingerprints, vs. just a name-prefix
// guess), so that's shown once at the top rather than repeated per card.
const CHECK_CIRCLE_ICON_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 17.01"/></svg>';
const ALERT_ICON_SVG =
  '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
  '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>' +
  '<line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

function openMergeComparison(group) {
  closeDetail(); // in case the single #detail panel was already open behind it
  state.mergeCompareGroup = group;
  state.mergeCompareSelected = new Set(group.mod_ids);

  const linked = group.curseforge_id != null;
  const banner = document.getElementById("mergeCompareReliability");
  banner.className = "compare-panel-banner " + (linked ? "is-linked" : "is-unlinked");
  banner.innerHTML = linked ? CHECK_CIRCLE_ICON_SVG : ALERT_ICON_SVG;
  banner.appendChild(
    elementWithText("span", null, t(linked ? "library.merge_compare.reliability_linked" : "library.merge_compare.reliability_unlinked"))
  );

  const list = document.getElementById("mergeCompareList");
  list.innerHTML = "";
  group.mod_ids.forEach((modId, i) => list.appendChild(buildMergeCompareCard(modId, group.mod_names[i])));

  const nameInput = document.getElementById("mergeCompareNameInput");
  nameInput.value = group.suggested_name;
  document.getElementById("mergeCompareConfirmBtn").onclick = () => doMergeFromComparison(nameInput.value.trim());
  updateMergeCompareBar();

  document.getElementById("overlay").classList.add("show");
  document.getElementById("mergeComparePanel").classList.add("show");
}

function buildMergeCompareCard(modId, name) {
  const card = document.createElement("div");
  card.className = "merge-compare-card";
  const label = document.createElement("label");
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.mergeCompareSelected.has(modId);
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) state.mergeCompareSelected.add(modId);
    else state.mergeCompareSelected.delete(modId);
    updateMergeCompareBar();
  });
  label.appendChild(checkbox);
  label.appendChild(elementWithText("span", "merge-compare-card-name", name));
  card.appendChild(label);
  return card;
}

function updateMergeCompareBar() {
  const count = state.mergeCompareSelected.size;
  const btn = document.getElementById("mergeCompareConfirmBtn");
  btn.disabled = count < 2;
  btn.textContent = t("library.merge_compare.confirm_button", { count });
}

async function doMergeFromComparison(newName) {
  const modIds = [...state.mergeCompareSelected];
  if (modIds.length < 2) return; // guarded by the button's own disabled state, defensive here too
  if (!newName) {
    showError("errorBanner", t("settings.loose_suggestion_name_required"));
    return;
  }
  try {
    await apiRequest("/api/mods/loose/merge", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mod_ids: modIds, new_name: newName }),
    });
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
    return;
  }
  closeMergeComparison();
  await loadMods();
  render();
}

function closeMergeComparison() {
  document.getElementById("overlay").classList.remove("show");
  document.getElementById("mergeComparePanel").classList.remove("show");
  document.getElementById("mergeCompareList").innerHTML = "";
  document.getElementById("mergeCompareReliability").innerHTML = "";
  state.mergeCompareGroup = null;
  state.mergeCompareSelected = new Set();
}

// "14 341 713" -> "14M" (or "14 M", "14 mille", ... — whatever the current
// UI language's own compact-notation convention is; Intl handles the
// abbreviation letter/spacing per locale instead of this hardcoding "k"/"M").
function formatCompactNumber(n) {
  return new Intl.NumberFormat(state.lang || undefined, { notation: "compact" }).format(n);
}

function formatInstallDate(isoString) {
  if (!isoString) return t("library.unknown");
  const parsed = new Date(isoString);
  return Number.isNaN(parsed.getTime()) ? t("library.unknown") : parsed.toLocaleDateString();
}

// Profiles ("saved states") can reasonably be saved more than once per day,
// so unlike formatInstallDate() this includes the time, not just the date.
function formatSavedAt(isoString) {
  if (!isoString) return t("library.unknown");
  const parsed = new Date(isoString);
  return Number.isNaN(parsed.getTime()) ? t("library.unknown") : parsed.toLocaleString();
}

// A conflict's `mods` entries already carry {id, name, active, install_date}
// (see GET /api/conflicts) — enough to act on directly, without a full
// mod-detail fetch. Used for the side-by-side comparison cards below —
// weaker conflict kinds only (duplicate_package, ts4script_name_collision,
// blacklist); an exact_duplicate_mod/folder_duplication match (a real
// "doublon") is excluded from this view entirely and instead opens the
// dedicated multi-panel comparison — see renderDetailConflicts() and
// openDuplicateComparison() below.
function buildConflictSide(modLite) {
  const side = document.createElement("div");
  side.className = "conflict-side";
  side.appendChild(elementWithText("span", "name", modLite.name));
  side.appendChild(
    elementWithText(
      "span",
      "meta",
      t("library.detail.conflict_side_meta", {
        date: formatInstallDate(modLite.install_date),
        state: t(modLite.active ? "library.detail.conflict_side_active" : "library.detail.conflict_side_inactive"),
      })
    )
  );
  const actions = document.createElement("div");
  actions.className = "actions";
  const toggleBtn = document.createElement("button");
  toggleBtn.className = "btn btn-sm";
  toggleBtn.textContent = t(
    modLite.active ? "library.detail.resolve_disable_button" : "library.detail.resolve_enable_button"
  );
  toggleBtn.addEventListener("click", async (e) => {
    e.stopPropagation();
    await toggleActive(modLite);
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  });
  const deleteBtn = document.createElement("button");
  deleteBtn.className = "btn btn-sm danger";
  deleteBtn.textContent = t("library.detail.resolve_delete_button");
  deleteBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmDelete(modLite);
  });
  actions.appendChild(toggleBtn);
  actions.appendChild(deleteBtn);
  side.appendChild(actions);
  return side;
}

// Applies `action` ("disable" or "delete") to every id in `otherModIds`,
// collecting per-mod failures instead of aborting on the first one — same
// partial-failure shape as doUpdateAll(). Shared by the duplicate-comparison
// "keep this one" flow below; the caller handles refreshing state/UI and
// reporting `errors` afterward.
async function applyActionToMods(otherModIds, action) {
  const errors = [];
  for (const modId of otherModIds) {
    try {
      if (action === "delete") {
        await apiRequest(`/api/mods/${encodeURIComponent(modId)}`, { method: "DELETE" });
      } else {
        await apiRequest(`/api/mods/${encodeURIComponent(modId)}/disable`, { method: "POST" });
      }
    } catch (err) {
      errors.push(err.message);
    }
  }
  return errors;
}

function renderDetailConflicts(mod) {
  // Reuses state.conflicts/state.blacklistMatches already loaded for the
  // Library grid (see problemModIds()) — openDetail() is only reachable
  // from there, so both are guaranteed populated. Every mod involved gets
  // its own comparison card with the facts relevant to "which one do I
  // keep" (install date, active state) and its own Disable/Delete — never
  // a recommendation of which side to pick, same "suspicion is not
  // confirmation" rule as the Library warnings banner this mirrors.
  //
  // A mod that's itself a real "doublon" (exact_duplicate_mod/
  // folder_duplication — see findDuplicateGroupForMod()) gets none of this
  // old per-side Disable/Delete widget at all anymore, not even for some
  // other, weaker signal (e.g. a duplicate_package overlap with a third,
  // unrelated mod) — its dedup story is now entirely the dedicated
  // multi-panel comparator's job (openDuplicateComparison()), reachable
  // straight from its card; mixing "go compare" with a leftover
  // manual tool here would just be confusing. A non-duplicate mod that
  // merely shares a weak signal with someone else's real duplicate is
  // unaffected — this only suppresses the section for the duplicate mod's
  // own detail view.
  const modConflicts = findDuplicateGroupForMod(mod.id)
    ? []
    : state.conflicts.filter((g) => g.mods.some((m) => m.id === mod.id));
  const blacklistMatch = state.blacklistMatches.find((m) => m.mod_id === mod.id);
  const section = document.getElementById("dConflictsSection");
  const container = document.getElementById("dConflicts");
  container.innerHTML = "";

  if (!modConflicts.length && !blacklistMatch) {
    section.hidden = true;
    return;
  }
  section.hidden = false;

  modConflicts.forEach((group) => {
    const wrapper = document.createElement("div");
    wrapper.className = "conflict-group";
    wrapper.appendChild(elementWithText("span", "kind", t(`library.conflicts.${group.kind}`)));
    wrapper.appendChild(
      elementWithText(
        "div",
        "summary",
        group.kind === "ts4script_name_collision"
          ? t("library.detail.conflict_ts4script_collision_summary", { filename: group.identifier })
          : t("library.detail.conflict_duplicate_package_summary", { count: group.file_count })
      )
    );
    const sides = document.createElement("div");
    sides.className = "conflict-sides";
    group.mods.forEach((modLite) => sides.appendChild(buildConflictSide(modLite)));
    wrapper.appendChild(sides);
    container.appendChild(wrapper);
  });

  if (blacklistMatch) {
    const wrapper = document.createElement("div");
    wrapper.className = "conflict-group";
    wrapper.appendChild(elementWithText("span", "kind", t("library.conflicts.blacklist_match")));
    wrapper.appendChild(
      elementWithText(
        "div",
        "summary",
        t("library.detail.conflict_blacklist_line", { patterns: blacklistMatch.patterns.join(", ") })
      )
    );
    const removeActions = document.createElement("div");
    removeActions.className = "actions";
    blacklistMatch.pattern_ids.forEach((patternId, i) => {
      const removeBtn = document.createElement("button");
      removeBtn.className = "btn btn-sm";
      removeBtn.textContent = t("library.detail.conflict_blacklist_remove_button", {
        pattern: blacklistMatch.patterns[i],
      });
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        doRemoveBlacklistEntryFromDetail(patternId);
      });
      removeActions.appendChild(removeBtn);
    });
    const sides = document.createElement("div");
    sides.className = "conflict-sides";
    sides.appendChild(buildConflictSide(mod));
    wrapper.appendChild(removeActions);
    wrapper.appendChild(sides);
    container.appendChild(wrapper);
  }
}

async function doRemoveBlacklistEntryFromDetail(entryId) {
  try {
    await apiRequest(`/api/blacklist/${entryId}`, { method: "DELETE" });
    await loadMods();
    render();
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

function buildDependencyRow(dep) {
  const row = document.createElement("div");
  row.className = "dep-row";
  row.dataset.dependencyId = dep.id; // scroll/highlight target for renderDependencyGraph()'s node clicks
  const label = dep.resolved_name
    || (dep.depends_on_curseforge_id != null
      ? t("library.detail.dependency_unknown_target", { id: dep.depends_on_curseforge_id })
      : t("library.detail.dependency_unknown_mod"));
  row.appendChild(elementWithText("span", null, label));
  row.appendChild(
    elementWithText(
      "span",
      "type",
      `${t("library.detail.dependency_type." + dep.dependency_type)} · ${t(
        "library.detail.dependency_confidence." + dep.confidence
      )}`
    )
  );

  if (dep.confidence === "suggested") {
    const actions = document.createElement("span");
    actions.className = "actions";
    const confirmBtn = document.createElement("button");
    confirmBtn.className = "btn btn-sm";
    confirmBtn.textContent = t("library.detail.dependency_confirm_button");
    confirmBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      resolveDependency(dep.id, "confirm");
    });
    const rejectBtn = document.createElement("button");
    rejectBtn.className = "btn btn-sm";
    rejectBtn.textContent = t("library.detail.dependency_reject_button");
    rejectBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      resolveDependency(dep.id, "reject");
    });
    actions.appendChild(confirmBtn);
    actions.appendChild(rejectBtn);
    row.appendChild(actions);
  }

  return row;
}

async function resolveDependency(dependencyId, action) {
  try {
    await apiRequest(`/api/dependencies/${dependencyId}/${action}`, { method: "POST" });
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  } catch (err) {
    showError("errorBanner", t("library.detail.dependency_action_error", { error: err.message }));
  }
}

// --- dependency graph ----------------------------------------------------------------
//
// A small inline-SVG visual overview above the real #dDependencies list
// (buildDependencyRow()) — the mod's own node at top, one node per
// dependency below, connected by an edge. Color encodes dependency_type
// (required/optional/translation), a dashed border/edge encodes
// confidence==='suggested'. Clicking a node scrolls to and briefly
// highlights its .dep-row below rather than duplicating Confirm/Reject
// inside the SVG (see index.html's comment on #dDependencyGraph) — keeps
// the tested confirm/reject logic in exactly one place.

function svgEl(tag, attrs) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const key in attrs) el.setAttribute(key, attrs[key]);
  return el;
}

function truncateLabel(text, maxLen) {
  if (!text) return "";
  return text.length > maxLen ? text.slice(0, maxLen - 1) + "…" : text;
}

const DEP_GRAPH_WIDTH = 320;
const DEP_GRAPH_NODE_WIDTH = 96;
const DEP_GRAPH_NODE_HEIGHT = 30;
const DEP_GRAPH_ROW_GAP = 16;
const DEP_GRAPH_NODE_GAP = 8;
const DEP_GRAPH_NODES_PER_ROW = 3;

function renderDependencyGraph(mod) {
  const container = document.getElementById("dDependencyGraph");
  container.innerHTML = "";
  if (!mod.dependencies.length) {
    container.hidden = true;
    return;
  }
  container.hidden = false;

  const centerWidth = 150;
  const centerHeight = 32;
  const centerCx = DEP_GRAPH_WIDTH / 2;
  const centerY = 6;
  const centerBottomY = centerY + centerHeight;

  const rows = [];
  for (let i = 0; i < mod.dependencies.length; i += DEP_GRAPH_NODES_PER_ROW) {
    rows.push(mod.dependencies.slice(i, i + DEP_GRAPH_NODES_PER_ROW));
  }
  const firstRowY = centerBottomY + 30;
  const totalHeight = firstRowY + rows.length * (DEP_GRAPH_NODE_HEIGHT + DEP_GRAPH_ROW_GAP);

  const svg = svgEl("svg", { viewBox: `0 0 ${DEP_GRAPH_WIDTH} ${totalHeight}`, width: "100%", height: String(totalHeight) });

  const centerGroup = svgEl("g", { class: "dep-node dep-node--self" });
  centerGroup.appendChild(svgEl("rect", { x: centerCx - centerWidth / 2, y: centerY, width: centerWidth, height: centerHeight, rx: 8 }));
  const centerText = svgEl("text", { x: centerCx, y: centerY + centerHeight / 2 + 4, "text-anchor": "middle" });
  centerText.textContent = truncateLabel(mod.curseforge_name || mod.name, 20);
  centerGroup.appendChild(centerText);
  svg.appendChild(centerGroup);

  rows.forEach((rowDeps, rowIndex) => {
    const rowY = firstRowY + rowIndex * (DEP_GRAPH_NODE_HEIGHT + DEP_GRAPH_ROW_GAP);
    const totalRowWidth = rowDeps.length * DEP_GRAPH_NODE_WIDTH + (rowDeps.length - 1) * DEP_GRAPH_NODE_GAP;
    let nodeX = (DEP_GRAPH_WIDTH - totalRowWidth) / 2;
    rowDeps.forEach((dep) => {
      const nodeCx = nodeX + DEP_GRAPH_NODE_WIDTH / 2;
      const stateClass = "dep-node--" + dep.dependency_type + (dep.confidence === "suggested" ? " dep-node--suggested" : "");

      svg.appendChild(svgEl("line", { x1: centerCx, y1: centerBottomY, x2: nodeCx, y2: rowY, class: "dep-graph-edge " + stateClass }));

      const group = svgEl("g", { class: "dep-node " + stateClass });
      group.appendChild(svgEl("rect", { x: nodeX, y: rowY, width: DEP_GRAPH_NODE_WIDTH, height: DEP_GRAPH_NODE_HEIGHT, rx: 8 }));
      const text = svgEl("text", { x: nodeCx, y: rowY + DEP_GRAPH_NODE_HEIGHT / 2 + 4, "text-anchor": "middle" });
      text.textContent = truncateLabel(dep.resolved_name || t("library.detail.dependency_unknown_mod"), 13);
      group.appendChild(text);
      group.addEventListener("click", () => highlightDependencyRow(dep.id));
      svg.appendChild(group);

      nodeX += DEP_GRAPH_NODE_WIDTH + DEP_GRAPH_NODE_GAP;
    });
  });

  container.appendChild(svg);

  const legend = document.createElement("div");
  legend.className = "dep-graph-legend";
  ["required", "optional", "translation"].forEach((type) => {
    const item = document.createElement("span");
    item.className = "dep-graph-legend-item dep-node--" + type;
    item.appendChild(elementWithText("span", "dep-graph-legend-dot", ""));
    item.append(t("library.detail.dependency_type." + type));
    legend.appendChild(item);
  });
  container.appendChild(legend);
}

function highlightDependencyRow(dependencyId) {
  const row = document.querySelector('.dep-row[data-dependency-id="' + dependencyId + '"]');
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "nearest" });
  row.classList.add("dep-row-highlight");
  setTimeout(() => row.classList.remove("dep-row-highlight"), 1200);
}

// Direct Mode + linked-mod only (see renderDetail()'s button visibility).
// Writes 'suggested' rows directly, unlike doDetectTranslation() below —
// see curseforge_dependencies.py's module docstring for why the two-step
// detect-then-pick dance translation needs doesn't apply here.
async function doDetectCurseforgeDependencies(modId) {
  const button = document.getElementById("detectCurseforgeDependenciesBtn");
  button.disabled = true;
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}/detect-curseforge-dependencies`, { method: "POST" });
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  } catch (err) {
    showError("errorBanner", t("library.detail.detect_curseforge_dependencies_error", { error: err.message }));
  } finally {
    button.disabled = false;
  }
}

// --- translation detection ---------------------------------------------------------

function groupSignalsBySource(signals) {
  const bySource = new Map();
  for (const signal of signals) {
    if (!bySource.has(signal.source_mod_id)) {
      bySource.set(signal.source_mod_id, {
        source_mod_id: signal.source_mod_id,
        source_mod_name: signal.source_mod_name,
        methods: [],
      });
    }
    bySource.get(signal.source_mod_id).methods.push(signal.method);
  }
  return [...bySource.values()];
}

async function doDetectTranslation(modId) {
  const button = document.getElementById("detectTranslationBtn");
  const container = document.getElementById("dTranslationSuggestions");
  button.disabled = true;
  container.innerHTML = "";
  try {
    const signals = await apiRequest(`/api/mods/${encodeURIComponent(modId)}/detect-translation`, {
      method: "POST",
    });
    if (!signals.length) {
      container.appendChild(
        elementWithText("div", "empty-inline", t("library.detail.detect_translation_no_signals"))
      );
      return;
    }
    groupSignalsBySource(signals).forEach((group) => {
      const methods = group.methods.map((m) => t(`library.detail.signal_method.${m}`)).join(", ");
      const row = document.createElement("div");
      row.className = "dep-row";
      row.appendChild(
        elementWithText(
          "span",
          null,
          t("library.detail.translation_suggestion_line", { name: group.source_mod_name, methods })
        )
      );
      const linkBtn = document.createElement("button");
      linkBtn.className = "btn btn-sm primary";
      linkBtn.textContent = t("library.detail.link_translation_button");
      linkBtn.addEventListener("click", () => doSuggestTranslation(modId, group.source_mod_id, linkBtn));
      row.appendChild(linkBtn);
      container.appendChild(row);
    });
  } catch (err) {
    showError("errorBanner", t("library.detail.detect_translation_error", { error: err.message }));
  } finally {
    button.disabled = false;
  }
}

async function doSuggestTranslation(modId, sourceModId, button) {
  button.disabled = true;
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}/suggest-translation`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source_mod_id: sourceModId }),
    });
    if (state.currentDetailId) await openDetail(state.currentDetailId);
  } catch (err) {
    button.disabled = false;
    showError("errorBanner", t("library.detail.dependency_action_error", { error: err.message }));
  }
}

async function openFolder(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}/open-folder`, { method: "POST" });
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// --- confirmation modal (delete mod / clear cache) ----------------------------

// `onCancel` is optional — every call site that doesn't pass one keeps
// behaving exactly as before (Cancel/backdrop-click just closes the modal).
let confirmOnCancel = null;

function openConfirmModal({ title, message, extraNodes, confirmLabel, onConfirm, onCancel }) {
  document.getElementById("confirmTitle").textContent = title;
  const messageEl = document.getElementById("confirmMessage");
  messageEl.textContent = message || "";
  messageEl.hidden = !message;
  const extra = document.getElementById("confirmExtra");
  extra.innerHTML = "";
  (extraNodes || []).forEach((node) => extra.appendChild(node));
  document.getElementById("confirmCancelBtn").textContent = t("common.cancel");
  document.getElementById("confirmOkBtn").textContent = confirmLabel;
  document.getElementById("confirmOkBtn").onclick = onConfirm;
  confirmOnCancel = onCancel || null;
  document.getElementById("confirmOverlay").classList.add("show");
}

// Used internally by onConfirm handlers (which call this directly) — never
// triggers onCancel, since confirming isn't cancelling.
function closeConfirm() {
  document.getElementById("confirmOverlay").classList.remove("show");
}

// Cancel button / backdrop click — the only two paths that count as
// actually declining, as opposed to closeConfirm() above.
function cancelConfirm() {
  closeConfirm();
  const callback = confirmOnCancel;
  confirmOnCancel = null;
  if (callback) callback();
}

function confirmDelete(mod) {
  openConfirmModal({
    title: t("library.delete_confirm.title"),
    message: t("library.delete_confirm.message", { name: mod.name }),
    confirmLabel: t("library.delete_confirm.confirm"),
    onConfirm: () => doDelete(mod.id),
  });
}

function confirmFixBrokenMod(folder) {
  openConfirmModal({
    title: t("library.conflicts.broken_mod_fix_confirm.title"),
    message: t(`library.conflicts.broken_mod_fix_confirm.${folder.reason}_message`, {
      name: folder.name,
      zips: folder.zip_paths.join(", "),
    }),
    confirmLabel: t("library.conflicts.broken_mod_fix_confirm.confirm"),
    onConfirm: () => doFixBrokenMod(folder.name),
  });
}

async function doFixBrokenMod(name) {
  try {
    await apiRequest(`/api/mods/broken/${encodeURIComponent(name)}/fix`, { method: "POST" });
    closeConfirm();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// A rezipped mod's fix action (see rezippedZipPaths()) — only offered as a
// one-click button for the unambiguous single-archive case, same restraint
// fix_broken_mod()/brokenActionSpec() apply: with more than one zip present
// there's no safe guess at which is "the" content, so no button is shown at
// all (the purple "Zip" badge/border still flags the mod either way).
function buildRezipActionButton(mod, zipPaths) {
  if (!zipPaths || zipPaths.length !== 1) return null;
  const btn = document.createElement("button");
  btn.className = "icon-btn unzip-icon-btn";
  btn.title = t("library.conflicts.broken_mod_fix_button");
  btn.innerHTML = UNZIP_ICON_SVG;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    confirmFixRezippedMod(mod);
  });
  return btn;
}

function confirmFixRezippedMod(mod) {
  openConfirmModal({
    title: t("library.rezip_fix_confirm.title"),
    message: t("library.rezip_fix_confirm.message", { name: mod.name }),
    confirmLabel: t("library.rezip_fix_confirm.confirm"),
    onConfirm: () => doFixRezippedMod(mod.id),
  });
}

async function doFixRezippedMod(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}/fix-rezip`, { method: "POST" });
    closeConfirm();
    // The fix reinstalls the mod (same mod_id in the common case, see
    // broken_mods.fix_rezipped_mod()) — closing rather than trying to
    // reopen avoids showing stale detail data for a mod that just got torn
    // down and rebuilt.
    if (state.currentDetailId === modId) closeDetail();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// A folder with 2+ archives can't be resolved with a single click the way
// fix_broken_mod() handles the unambiguous single-archive case — "often we
// have to choose one or several" (mutually exclusive variants, or several
// required pieces bundled separately), so this opens the shared confirm
// modal with a checkbox per archive (via openConfirmModal()'s extraNodes)
// instead of a plain yes/no message. The first archive is pre-checked as a
// reasonable default, not a recommendation.
function confirmExtractZips(folder) {
  const checkboxByPath = new Map();
  const list = document.createElement("div");
  list.className = "zip-choice-list";
  folder.zip_paths.forEach((zipPath, i) => {
    const label = document.createElement("label");
    label.className = "zip-choice";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = i === 0;
    checkboxByPath.set(zipPath, checkbox);
    label.appendChild(checkbox);
    label.appendChild(document.createTextNode(zipPath));
    list.appendChild(label);
  });

  openConfirmModal({
    title: t("library.extract_zips_confirm.title"),
    message: t("library.extract_zips_confirm.message", { name: folder.name }),
    extraNodes: [list],
    confirmLabel: t("library.extract_zips_confirm.confirm"),
    onConfirm: () => {
      const selected = folder.zip_paths.filter((zipPath) => checkboxByPath.get(zipPath).checked);
      if (!selected.length) {
        showError("errorBanner", t("library.extract_zips_confirm.none_selected"));
        return;
      }
      doExtractZips(folder.name, selected);
    },
  });
}

async function doExtractZips(name, zipPaths) {
  try {
    await apiRequest(`/api/mods/broken/${encodeURIComponent(name)}/extract-zips`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zip_paths: zipPaths }),
    });
    closeConfirm();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// A broken folder has no enable/disable concept — it isn't a managed mod
// with an active/inactive symlink, and nothing the game would load sits in
// it anyway (that's the whole reason it's flagged), so unlike a real mod
// there's no toggle here, only delete.
function confirmDeleteBrokenFolder(folder) {
  openConfirmModal({
    title: t("library.broken_delete_confirm.title"),
    message: t("library.broken_delete_confirm.message", { name: folder.name }),
    confirmLabel: t("library.broken_delete_confirm.confirm"),
    onConfirm: () => doDeleteBrokenFolder(folder.name),
  });
}

async function doDeleteBrokenFolder(name) {
  try {
    await apiRequest(`/api/mods/broken/${encodeURIComponent(name)}`, { method: "DELETE" });
    closeConfirm();
    await loadMods();
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

async function doDelete(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}`, { method: "DELETE" });
    closeConfirm();
    // Deleting the *other* side of a conflict comparison from within the
    // currently open mod's detail panel shouldn't kick the user out of it —
    // only close when the deleted mod is the one actually being viewed.
    const wasCurrentDetail = state.currentDetailId === modId;
    if (wasCurrentDetail) closeDetail();
    await loadMods();
    render();
    if (!wasCurrentDetail && state.currentDetailId) await openDetail(state.currentDetailId);
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }
}

// --- Assisted Mode download detection -----------------------------------------------
//
// The backend's DownloadWatcher runs on its own background thread and just
// queues what it finds (see backend/main.py's PendingDownloadStore) — there's
// no push channel from a plain REST API to the frontend, so this polls
// instead. A local single-user desktop app doesn't need sub-second latency
// here; a few seconds is an imperceptible delay for "I just dropped a file
// in my Downloads folder."

function startDownloadPolling() {
  checkPendingDownloads();
  setInterval(checkPendingDownloads, DOWNLOAD_POLL_INTERVAL_MS);
}

async function checkPendingDownloads() {
  if (state.currentPendingDownload) return; // already showing one — let the user decide first
  if (document.getElementById("confirmOverlay").classList.contains("show")) return;
  let items;
  try {
    items = await apiRequest("/api/downloads/pending");
  } catch (err) {
    return; // background poll — don't spam an error banner over a transient failure
  }
  if (items.length) showDownloadDialog(items[0]);
}

function showDownloadDialog(item) {
  state.currentPendingDownload = item;
  const replaceBtn = document.getElementById("downloadReplaceBtn");

  if (item.candidate_mod_id) {
    document.getElementById("downloadMessage").textContent = t("downloads.detected_replace_message", {
      filename: item.filename,
      mod_name: item.candidate_mod_name,
    });
    replaceBtn.hidden = false;
    replaceBtn.textContent = t("downloads.replace_button");
    replaceBtn.onclick = () => resolvePendingDownload(item.token, "replace", item.candidate_mod_id);
  } else {
    document.getElementById("downloadMessage").textContent = t("downloads.detected_message", {
      filename: item.filename,
    });
    replaceBtn.hidden = true;
  }

  document.getElementById("downloadTitle").textContent = t("downloads.detected_title");
  document.getElementById("downloadDismissBtn").textContent = t("common.dismiss");
  document.getElementById("downloadInstallBtn").textContent = t("downloads.install_button");
  document.getElementById("downloadDismissBtn").onclick = () => resolvePendingDownload(item.token, "dismiss");
  document.getElementById("downloadInstallBtn").onclick = () => resolvePendingDownload(item.token, "install");
  document.getElementById("downloadOverlay").classList.add("show");
}

async function resolvePendingDownload(token, action, modId) {
  document.getElementById("downloadOverlay").classList.remove("show");
  state.currentPendingDownload = null;
  try {
    if (action === "install") {
      await apiRequest(`/api/downloads/${token}/install`, { method: "POST" });
    } else if (action === "replace") {
      await apiRequest(`/api/downloads/${token}/replace`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mod_id: modId }),
      });
    } else {
      await apiRequest(`/api/downloads/${token}/dismiss`, { method: "POST" });
      return;
    }
    await loadMods();
    render();
    await suggestCacheCleanup();
  } catch (err) {
    showError("errorBanner", t("downloads.error", { error: err.message }));
  }
}

// --- init ------------------------------------------------------------------------

async function init() {
  state.lang = detectLang();
  await loadI18n(state.lang);
  applyStaticI18n();
  applyViewMode(localStorage.getItem("simslink-view-mode") || "grid");
  applySimplifiedNames(localStorage.getItem("simslink-simplified-names") !== "0");
  applyGroupByAuthor(localStorage.getItem("simslink-group-by-author") !== "0");
  applyTypeFilter(localStorage.getItem("simslink-type-filter") ?? "");
  applyStateFilter(localStorage.getItem("simslink-state-filter") ?? "");
  wireSearch();
  wireNav();
  wireViewToggle();
  wireTypeFilterToggle();
  wireStateFilterToggle();
  wireSimplifiedNamesToggle();
  wireGroupByAuthorToggle();
  wireSaveLoadStateButtons();
  wireLooseFilterChip();
  wireTranslationFilterChip();
  wireLinkedFilterChip();
  wireIncompatibleFilterChip();
  // Escape backs out of whichever of the duplicate/merge comparator or the
  // single mod detail panel is currently open — closeAnyDetail() already
  // knows which one that is (or safely no-ops if none are). Doesn't touch
  // other overlays (confirm/crash/match-curseforge modals) — those use
  // their own distinct overlay elements, out of scope here.
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeAnyDetail();
  });
  wireCrashModal();
  document.getElementById("headerClearCacheButton").addEventListener("click", clickClearCache);
  document.getElementById("headerOpenModsFolderButton").addEventListener("click", doOpenModsFolder);
  document.getElementById("headerMatchCurseforgeButton").addEventListener("click", openMatchCurseforgeModal);
  document.getElementById("matchCurseforgeCloseBtn").addEventListener("click", closeMatchCurseforgeModal);
  document.getElementById("matchCurseforgeOverlay").addEventListener("click", (e) => {
    if (e.target.id === "matchCurseforgeOverlay") closeMatchCurseforgeModal();
  });
  document.getElementById("headerSyncCurseforgeButton").addEventListener("click", openSyncCurseforgeModal);
  document.getElementById("syncCurseforgeCloseBtn").addEventListener("click", closeSyncCurseforgeModal);
  document.getElementById("syncCurseforgeOverlay").addEventListener("click", (e) => {
    if (e.target.id === "syncCurseforgeOverlay") closeSyncCurseforgeModal();
  });
  document.getElementById("confirmCancelBtn").addEventListener("click", cancelConfirm);
  document.getElementById("confirmOverlay").addEventListener("click", (e) => {
    if (e.target.id === "confirmOverlay") cancelConfirm();
  });
  document.getElementById("conflictsToggle").addEventListener("click", () => {
    state.conflictsExpanded = !state.conflictsExpanded;
    renderWarnings();
  });
  document.getElementById("conflictsDismissBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    state.conflictsBannerDismissed = true;
    renderWarnings();
  });
  wireBanner("broken", "brokenModsToggle", "brokenModsDismissBtn", renderBrokenModsBanner);
  wireBanner("unzip", "unzipToggle", "unzipDismissBtn", renderUnzipBanner);
  wireBanner("duplicates", "duplicatesToggle", "duplicatesDismissBtn", renderDuplicatesBanner);
  wireBanner("compat", "compatQuarantineToggle", "compatQuarantineDismissBtn", renderCompatQuarantineBanner);
  wireBanner("mergeable", "mergeableToggle", "mergeableDismissBtn", renderMergeableBanner);
  document.getElementById("unzipAllButton").addEventListener("click", (e) => {
    e.stopPropagation();
    confirmUnzipAll();
  });
  document.getElementById("resolveDuplicatesBulkButton").addEventListener("click", (e) => {
    e.stopPropagation();
    confirmResolveDuplicatesBulk();
  });
  document.getElementById("openQuarantineConfirmButton").addEventListener("click", (e) => {
    e.stopPropagation();
    confirmQuarantineMods();
  });
  document.getElementById("missingModsToggle").addEventListener("click", () => {
    state.missingModsExpanded = !state.missingModsExpanded;
    renderMissingMods();
  });

  try {
    await Promise.all([loadStatus(), loadMods()]);
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }

  startDownloadPolling();
  startLibraryPolling();
}

document.addEventListener("DOMContentLoaded", init);
