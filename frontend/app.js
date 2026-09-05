// SimsLink frontend — all five views (FastAPI + pywebview). Talks to
// backend/main.py's /api/ routes; never hardcodes UI text — everything
// user-facing goes through t(), backed by i18n/{en,fr}.json.

const DOWNLOAD_POLL_INTERVAL_MS = 3000;

const state = {
  mods: [],
  conflicts: [],
  blacklistMatches: [],
  brokenMods: [],
  conflictsExpanded: false,
  strings: {},
  filterQuery: "",
  currentDetailId: null,
  status: null,
  lang: "en",
  theme: "dark",
  tileSize: "large",
  viewMode: "grid",
  simplifiedNames: true,
  catalogWired: false,
  updatesWired: false,
  crashWired: false,
  settingsWired: false,
  currentPendingDownload: null,
  updatableMods: [],
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

function compatKey(status) {
  return status === "compatible" || status === "incompatible" ? status : "unknown";
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

// A broken folder's action button dispatches on `reason`: 'unpacked_script'
// gets the best-effort re-zip attempt (attempt-script-repair), while
// 'unextracted_archive' reuses the existing safe auto-fix (fix_broken_mod's
// single-zip extraction, same route/confirm flow as the warnings banner's
// own "Fix" button) — there's no third case here, 'empty'/'unrecognized'
// don't get a card at all (see visibleBrokenModFolders()). Returns null
// when no action is safe to offer (an ambiguous multi-zip archive) so the
// card still appears — flagged as a problem — just without a button that
// would be guaranteed to fail.
function buildBrokenActionButton(folder) {
  if (folder.reason === "unpacked_script") {
    const btn = document.createElement("button");
    btn.className = "icon-btn repair-icon-btn";
    btn.title = t("library.broken_repair_button_title");
    btn.innerHTML = REPAIR_ICON_SVG;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      confirmScriptRepair(folder);
    });
    return btn;
  }
  if (folder.reason === "unextracted_archive" && folder.zip_names.length === 1) {
    const btn = document.createElement("button");
    btn.className = "icon-btn repair-icon-btn";
    btn.title = t("library.conflicts.broken_mod_fix_button");
    btn.innerHTML = REPAIR_ICON_SVG;
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      confirmFixBrokenMod(folder);
    });
    return btn;
  }
  return null;
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
  const candidates = state.brokenMods.filter(
    (folder) => folder.reason === "unpacked_script" || folder.reason === "unextracted_archive"
  );
  return query ? candidates.filter((folder) => folder.name.toLowerCase().includes(query)) : candidates;
}

function showCompatBadge() {
  // compat_status is only ever meaningful with CurseForge metadata
  // (game_version_min/max) — in Assisted Mode every mod is stuck at
  // 'unknown', so the badge would just repeat the same non-information on
  // every single card.
  return !!(state.status && state.status.direct_mode);
}

function compatGemClass(status) {
  if (status === "incompatible") return "warn";
  if (status === "compatible") return "";
  return "unknown";
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

function splitNameVersion(name) {
  const trimmed = (name || "").trim();
  const dupMatch = /^(.*?)(\s*\(\d+\))$/.exec(trimmed);
  const dupSuffix = dupMatch ? dupMatch[2].trim() : "";
  const withoutDup = dupMatch ? dupMatch[1] : trimmed;

  const match = VERSION_SUFFIX_RE.exec(withoutDup);
  if (!match) return { displayName: name, version: null };

  const rest = (match[1] !== undefined ? match[1] : match[3]).trim();
  const version = match[1] !== undefined ? match[2] : match[4];
  if (!rest) return { displayName: name, version: null };

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

function computeInferredAuthorTags(mods) {
  const candidates = mods.filter((mod) => !(mod.author || "").trim() && !splitNamePrefix(baseName(mod)).prefix);
  const segmentsById = new Map(candidates.map((mod) => [mod.id, nameSegments(baseName(mod))]));

  // groupsByK[k - 1] = lowercase-prefix -> { display, ids } for that segment count.
  const groupsByK = [];
  for (let k = 1; k <= MAX_INFERRED_PREFIX_SEGMENTS; k++) {
    const groups = new Map();
    candidates.forEach((mod) => {
      const segments = segmentsById.get(mod.id);
      if (segments.length <= k) return; // nothing would be left of the name after stripping
      const display = segments.slice(0, k).join(" ");
      if (display.replace(/\s+/g, "").length < MIN_INFERRED_PREFIX_CHARS) return;
      const key = display.toLowerCase();
      if (!groups.has(key)) groups.set(key, { display, ids: [] });
      groups.get(key).ids.push(mod.id);
    });
    groupsByK.push(groups);
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
      const key = segments.slice(0, k).join(" ").toLowerCase();
      const group = groupsByK[k - 1].get(key);
      if (!group || group.ids.length < 2) continue;
      if (!best || group.ids.length >= best.size) best = { display: group.display, size: group.ids.length, k };
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

// Toggled by the "Simplified names" checkbox next to the view switch —
// affects only the title text shown on a card/row. Grouping (author
// headers, sort order, the avatar label) and the version badge stay driven
// by groupingAuthor() either way, since those aren't "the mod's name" as
// far as this toggle is concerned, just organizational metadata derived
// from it.
function titleText(mod, displayName) {
  return state.simplifiedNames ? displayName : mod.name;
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
    throw new Error(detail);
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
}

// --- mod list / grid ----------------------------------------------------------

async function loadMods() {
  const [mods, conflicts, blacklistMatches, brokenMods] = await Promise.all([
    apiRequest("/api/mods"),
    apiRequest("/api/conflicts"),
    apiRequest("/api/blacklist/matches"),
    apiRequest("/api/mods/broken"),
  ]);
  state.mods = mods;
  state.conflicts = conflicts;
  state.blacklistMatches = blacklistMatches;
  state.brokenMods = brokenMods;
}

function visibleMods() {
  const query = state.filterQuery.trim().toLowerCase();
  if (!query) return state.mods;
  return state.mods.filter(
    (m) => m.name.toLowerCase().includes(query) || (m.author || "").toLowerCase().includes(query)
  );
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
    return;
  }
  banner.hidden = false;

  document.getElementById("conflictsToggle").textContent = t("library.conflicts.toggle", {
    count: totalCount,
  });

  list.hidden = !state.conflictsExpanded;
  list.innerHTML = "";
  bannerFolders.forEach((folder) => {
    const row = document.createElement("div");
    row.className = "conflict-row";
    const kindLabel = document.createElement("span");
    kindLabel.className = "kind";
    kindLabel.textContent = t(`library.conflicts.broken_mod_${folder.reason}`);
    row.appendChild(kindLabel);
    const filesList =
      folder.sample_files.join(", ") + (folder.file_count > folder.sample_files.length ? ", …" : "");
    row.append(
      t(`library.conflicts.broken_mod_${folder.reason}_line`, {
        name: folder.name,
        zips: folder.zip_names.join(", "),
        files: filesList,
      })
    );
    if (folder.reason === "empty") {
      const fixBtn = document.createElement("button");
      fixBtn.className = "btn btn-sm";
      fixBtn.textContent = t("library.conflicts.broken_mod_fix_button");
      fixBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        confirmFixBrokenMod(folder);
      });
      row.appendChild(fixBtn);
    }
    list.appendChild(row);
  });
}

function problemModIds() {
  // Mods already installed and flagged by conflict_detector.py or
  // blacklist.py — surfaced as a red highlight directly on their card, in
  // addition to the detailed collapsible list above. Unmanaged broken mod
  // folders (broken_mods.py) are a separate, higher-priority tier — see
  // modTier() — since "confirmed non-functional" outranks "suspected".
  const ids = new Set();
  state.conflicts.forEach((group) => group.mods.forEach((m) => ids.add(m.id)));
  state.blacklistMatches.forEach((match) => ids.add(match.mod_id));
  return ids;
}

// Sort priority within an author group (or the trailing authorless
// bucket): 0 = broken folder (confirmed non-functional), 1 = problem mod
// (conflict/blacklist flagged, only ever active — conflict_detector.py
// doesn't consider disabled mods), 2 = normal active mod, 3 = disabled.
function modTier(mod, problems) {
  if (mod.__brokenFolder) return 0;
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

function render() {
  document.getElementById("subtitle").textContent = t("library.subtitle", {
    installed: state.mods.length,
    active: state.mods.filter((m) => m.active).length,
  });

  renderWarnings();

  const grid = document.getElementById("grid");
  grid.innerHTML = "";

  const problems = problemModIds();
  const duplicateTags = duplicateTagModIds();
  const identicalContentTags = identicalContentTagModIds();
  const brokenPseudos = visibleBrokenModFolders().map(brokenFolderPseudoMod);
  const visible = visibleMods().concat(brokenPseudos);
  const inferredTags = computeInferredAuthorTags(visible);
  // Author is the primary key — a problem mod shares its author's group
  // rather than being pulled out into a separate top-of-page section. Mods
  // with no author at all sort after ones that have one, rather than
  // clumping arbitrarily at the top under an empty "author". Within a
  // group (or the trailing authorless bucket), four distinct tiers, in
  // this order: broken folders, then problem mods (conflict/blacklist
  // flagged), then normal active mods, then disabled ones — broken and
  // problem are NOT the same tier (a broken folder is confirmed
  // non-functional, a problem mod only suspected), and a disabled mod is
  // never itself a "problem" (conflict_detector.py only considers active
  // mods) but still needs its own tier below normal, not just "not a
  // problem". Alphabetical is the final tiebreak within a tier.
  const mods = visible.slice().sort((a, b) => {
    const authorA = groupingAuthor(a, inferredTags).label;
    const authorB = groupingAuthor(b, inferredTags).label;
    if (!authorA !== !authorB) return authorA ? -1 : 1;
    const authorDiff = authorA.localeCompare(authorB, undefined, { sensitivity: "base" });
    if (authorDiff !== 0) return authorDiff;
    const tierDiff = modTier(a, problems) - modTier(b, problems);
    if (tierDiff !== 0) return tierDiff;
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  });
  if (!mods.length) {
    grid.appendChild(elementWithText("div", "empty-state", t("library.empty")));
    return;
  }
  const buildFn = state.viewMode === "list" ? buildListRow : buildCard;
  // Author is now the sole top-level sort key, so each author's mods are
  // always contiguous — a header only needs to check "did the author
  // change since the last mod", no reset-on-authorless-mod needed. Mods
  // with no author at all (no real `mod.author`, no bracket prefix, no
  // inferred series match) already sort last (see the comparator above);
  // they still get a header of their own — a generic "Unknown author" one
  // — instead of running headerless off the end of the previous group.
  let lastHeaderAuthor = null;
  mods.forEach((mod) => {
    const { label: author } = groupingAuthor(mod, inferredTags);
    if (author !== lastHeaderAuthor) {
      grid.appendChild(buildAuthorHeader(author || t("library.unknown_author_header")));
    }
    lastHeaderAuthor = author;
    grid.appendChild(
      buildFn(mod, inferredTags, problems.has(mod.id), duplicateTags.has(mod.id), identicalContentTags.has(mod.id))
    );
  });
}

function buildAuthorHeader(author) {
  const header = document.createElement("div");
  header.className = "author-group-header";
  header.appendChild(elementWithText("span", null, author));
  const rule = document.createElement("div");
  rule.className = "rule";
  header.appendChild(rule);
  return header;
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
function buildBadges(mod, version, isDuplicate, isIdenticalContent, badgeClassName, gemSizePx) {
  const broken = mod.__brokenFolder;
  const badges = document.createElement("span"); // caller decides tag/class via appendChild target
  const items = [];
  if (broken) {
    items.push(elementWithText("span", `${badgeClassName} danger-badge`, t("library.broken_tag")));
  }
  if (isDuplicate) {
    items.push(elementWithText("span", `${badgeClassName} warn-badge`, t("library.duplicate_tag")));
  } else if (isIdenticalContent) {
    items.push(elementWithText("span", badgeClassName, t("library.identical_content_tag")));
  }
  const rowVersion = mod.installed_version || version;
  if (rowVersion) {
    items.push(elementWithText("span", badgeClassName, formatVersionBadge(rowVersion)));
  }
  primaryTypeBadgeKeys(mod.primary_type).forEach((key) => items.push(elementWithText("span", badgeClassName, t(key))));
  if (!broken && showCompatBadge()) {
    const compat = document.createElement("span");
    compat.className = badgeClassName;
    const gem = document.createElement("span");
    gem.className = "gem" + (compatGemClass(mod.compat_status) ? " " + compatGemClass(mod.compat_status) : "");
    gem.style.width = gemSizePx;
    gem.style.height = gemSizePx;
    compat.appendChild(gem);
    compat.append(" " + t(`library.compat.${compatKey(mod.compat_status)}`));
    items.push(compat);
  }
  if (!broken && !mod.active) {
    items.push(elementWithText("span", badgeClassName, t("library.disabled_tag")));
  }
  items.forEach((item) => badges.appendChild(item));
  return { el: badges, count: items.length };
}

function buildListRow(mod, inferredTags, hasIssue, isDuplicate, isIdenticalContent) {
  const { label, displayName, version } = groupingAuthor(mod, inferredTags);
  const broken = mod.__brokenFolder;
  const row = document.createElement("div");
  row.className = "list-mod-row" + (mod.active ? "" : " is-inactive") + (broken ? " is-broken" : hasIssue ? " has-issue" : "");
  if (!broken) row.addEventListener("click", () => openDetail(mod.id));

  const avatar = document.createElement("div");
  avatar.className = "avatar";
  if (broken) {
    avatar.innerHTML = BROKEN_ICON_SVG;
  } else {
    avatar.textContent = label || initials(mod.name);
    if (label && label.length > 4) avatar.classList.add("long");
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

  const badges = buildBadges(mod, version, isDuplicate, isIdenticalContent, "pill", "7px").el;
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
    row.appendChild(buildDeleteIconButton(mod));
  }

  return row;
}

function buildCard(mod, inferredTags, hasIssue, isDuplicate, isIdenticalContent) {
  const { label, displayName, version } = groupingAuthor(mod, inferredTags);
  const broken = mod.__brokenFolder;
  const card = document.createElement("div");
  card.className = "card" + (mod.active ? "" : " is-inactive") + (broken ? " is-broken" : hasIssue ? " has-issue" : "");
  if (!broken) card.addEventListener("click", () => openDetail(mod.id));

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  const initialEl = document.createElement("span");
  initialEl.className = "initial";
  if (broken) {
    initialEl.innerHTML = BROKEN_ICON_SVG;
  } else {
    initialEl.textContent = label || initials(mod.name);
    if (label && label.length > 4) initialEl.classList.add("long");
  }
  thumb.appendChild(initialEl);

  const badges = buildBadges(mod, version, isDuplicate, isIdenticalContent, "cc-badge", "8px");
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
  meta.appendChild(document.createElement("span"));
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
    metaActions.appendChild(buildDeleteIconButton(mod));
  }
  meta.appendChild(metaActions);
  body.appendChild(meta);

  card.appendChild(thumb);
  card.appendChild(body);
  return card;
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

function wireSearch() {
  document.getElementById("searchInput").addEventListener("input", (e) => {
    state.filterQuery = e.target.value;
    render();
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

const VIEWS = ["library", "catalog", "updates", "crash", "settings"];

function switchView(view) {
  document.querySelectorAll(".nav-item[data-view]").forEach((el) => {
    el.classList.toggle("active", el.dataset.view === view);
  });
  VIEWS.forEach((v) => {
    document.getElementById(`view-${v}`).hidden = v !== view;
  });
  if (view === "catalog") initCatalogView();
  if (view === "updates") initUpdatesView();
  if (view === "crash") initCrashView();
  if (view === "settings") initSettingsView();
}

// --- catalog view ------------------------------------------------------------------

function initCatalogView() {
  const direct = !!(state.status && state.status.direct_mode);
  document.getElementById("catalogAssistedNotice").hidden = direct;
  document.getElementById("catalogSearchBar").hidden = !direct;
  if (!direct || state.catalogWired) return;

  state.catalogWired = true;
  document.getElementById("catalogSearchButton").addEventListener("click", doCatalogSearch);
  document.getElementById("catalogSearchInput").addEventListener("keydown", (e) => {
    if (e.key === "Enter") doCatalogSearch();
  });
}

async function doCatalogSearch() {
  const query = document.getElementById("catalogSearchInput").value.trim();
  const results = document.getElementById("catalogResults");
  results.innerHTML = "";
  let mods;
  try {
    mods = await apiRequest(`/api/catalog/search?q=${encodeURIComponent(query)}`);
  } catch (err) {
    showError("catalogErrorBanner", t("catalog.search_error", { error: err.message }));
    return;
  }
  if (!mods.length) {
    results.appendChild(elementWithText("div", "empty-state", t("catalog.no_results")));
    return;
  }
  mods.forEach((mod) => results.appendChild(buildCatalogRow(mod)));
}

function buildCatalogRow(mod) {
  const row = document.createElement("div");
  row.className = "catalog-row";

  row.appendChild(elementWithText("div", "avatar", initials(mod.name)));

  const info = document.createElement("div");
  info.className = "info";
  const title = document.createElement("h3");
  title.appendChild(document.createTextNode(mod.name));
  if (mod.author) {
    title.appendChild(elementWithText("span", "author", mod.author));
  }
  info.appendChild(title);
  info.appendChild(elementWithText("p", null, mod.short_description || ""));
  row.appendChild(info);

  const action = document.createElement("button");
  action.className = "btn" + (mod.third_party_distribution_allowed ? " primary" : "");
  if (mod.third_party_distribution_allowed) {
    action.textContent = t("catalog.install_button");
    action.addEventListener("click", () => installFromCatalog(mod, action));
  } else {
    action.textContent = t("catalog.open_on_curseforge_button");
    action.addEventListener("click", () => openExternal(mod.curseforge_url));
  }
  row.appendChild(action);

  return row;
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

// --- crash mode view ---------------------------------------------------------------

function initCrashView() {
  if (state.crashWired) return;
  state.crashWired = true;
  document.getElementById("crashAnalyzeButton").addEventListener("click", doAnalyzeCrash);
  document.getElementById("crashClearCacheButton").addEventListener("click", clickClearCache);
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

function buildCacheTargetNodes(targets) {
  return targets.length
    ? targets.map((tgt) => elementWithText("div", "empty-inline", `${tgt.name} — ${tgt.description}`))
    : [elementWithText("div", "empty-inline", t("crash.clear_cache_nothing"))];
}

async function clickClearCache() {
  let targets;
  try {
    targets = await apiRequest("/api/cache/targets");
  } catch (err) {
    showError("crashErrorBanner", t("crash.clear_cache_error", { error: err.message }));
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
    showError("crashErrorBanner", t("crash.clear_cache_error", { error: err.message }));
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

    const themeSelect = document.getElementById("themeSelect");
    themeSelect.value = state.theme;
    themeSelect.addEventListener("change", (e) => applyTheme(e.target.value));

    const tileSizeSelect = document.getElementById("tileSizeSelect");
    tileSizeSelect.value = state.tileSize;
    tileSizeSelect.addEventListener("change", (e) => {
      applyTileSize(e.target.value);
      render();
    });

    document.getElementById("fullScanButton").addEventListener("click", doFullScan);
    document.getElementById("createProfileButton").addEventListener("click", doCreateProfile);
    document.getElementById("addBlacklistButton").addEventListener("click", doAddBlacklistEntry);
  }
  loadSettings();
  loadProfiles();
  loadBlacklist();
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
    ["settings.folder.game_dir", settings.game_dir],
    ["settings.folder.mods_dir", settings.mods_dir],
    ["settings.folder.user_dir", settings.user_dir],
    ["settings.folder.library_dir", settings.library_dir],
    ["settings.folder.download_watch_dir", settings.download_watch_dir],
  ].forEach(([labelKey, value]) => {
    const row = document.createElement("div");
    row.className = "settings-row";
    row.appendChild(elementWithText("span", null, t(labelKey)));
    row.appendChild(elementWithText("span", "value", value));
    container.appendChild(row);
  });

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

// --- theme / tile size (client-side preferences, no backend involved) --------------

function applyTheme(theme) {
  state.theme = theme;
  if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else {
    document.documentElement.removeAttribute("data-theme");
  }
  localStorage.setItem("simslink-theme", theme);
}

function applyTileSize(size) {
  state.tileSize = size;
  document.getElementById("grid").classList.toggle("tile-compact", size === "compact");
  localStorage.setItem("simslink-tile-size", size);
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

// Whether card/row titles show the cleaned-up name (bracket prefix, trailing
// version, and inferred series prefix all stripped — see groupingAuthor())
// or the mod's original, untouched name. Purely a display preference, same
// as theme/tile size — grouping/sorting/the avatar label are unaffected
// either way, only the title text shown in buildCard()/buildListRow().
function applySimplifiedNames(enabled) {
  state.simplifiedNames = enabled;
  document.getElementById("simplifiedNamesCheckbox").checked = enabled;
  localStorage.setItem("simslink-simplified-names", enabled ? "1" : "0");
}

function wireSimplifiedNamesToggle() {
  document.getElementById("simplifiedNamesCheckbox").addEventListener("change", (e) => {
    applySimplifiedNames(e.target.checked);
    render();
  });
}

// --- profiles ----------------------------------------------------------------------

async function loadProfiles() {
  const container = document.getElementById("profilesList");
  container.innerHTML = "";
  let items;
  try {
    items = await apiRequest("/api/profiles");
  } catch (err) {
    showError("profilesErrorBanner", t("settings.profiles_error", { error: err.message }));
    return;
  }
  if (!items.length) {
    container.appendChild(elementWithText("div", "empty-inline", t("settings.profiles_empty")));
    return;
  }
  items.forEach((profile) => container.appendChild(buildProfileRow(profile)));
}

function buildProfileRow(profile) {
  const row = document.createElement("div");
  row.className = "list-row";

  const info = document.createElement("div");
  info.className = "info";
  info.appendChild(elementWithText("span", null, profile.name));
  info.appendChild(
    elementWithText("span", "note", t("settings.profile_mod_count", { count: profile.mod_ids.length }))
  );
  row.appendChild(info);

  const actions = document.createElement("div");
  actions.className = "actions";
  const activateBtn = document.createElement("button");
  activateBtn.className = "btn btn-sm primary";
  activateBtn.textContent = t("settings.activate_profile_button");
  activateBtn.addEventListener("click", () => doActivateProfile(profile.id, activateBtn));
  const deleteBtn = document.createElement("button");
  deleteBtn.className = "btn btn-sm";
  deleteBtn.textContent = t("settings.delete_profile_button");
  deleteBtn.addEventListener("click", () => doDeleteProfile(profile.id));
  actions.appendChild(activateBtn);
  actions.appendChild(deleteBtn);
  row.appendChild(actions);

  return row;
}

async function doCreateProfile() {
  const input = document.getElementById("newProfileNameInput");
  const name = input.value.trim();
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
    input.value = "";
    await loadProfiles();
  } catch (err) {
    showError("profilesErrorBanner", t("settings.profiles_error", { error: err.message }));
  }
}

async function doActivateProfile(profileId, button) {
  button.disabled = true;
  try {
    await apiRequest(`/api/profiles/${profileId}/activate`, { method: "POST" });
    await loadMods();
    render();
  } catch (err) {
    showError("profilesErrorBanner", t("settings.profiles_error", { error: err.message }));
  } finally {
    button.disabled = false;
  }
}

async function doDeleteProfile(profileId) {
  try {
    await apiRequest(`/api/profiles/${profileId}`, { method: "DELETE" });
    await loadProfiles();
  } catch (err) {
    showError("profilesErrorBanner", t("settings.profiles_error", { error: err.message }));
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

async function openDetail(modId) {
  let mod;
  try {
    mod = await apiRequest(`/api/mods/${encodeURIComponent(modId)}`);
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
    return;
  }
  state.currentDetailId = modId;
  renderDetail(mod);
  document.getElementById("detail").classList.add("show");
  document.getElementById("overlay").classList.add("show");
}

function closeDetail() {
  document.getElementById("detail").classList.remove("show");
  document.getElementById("overlay").classList.remove("show");
  state.currentDetailId = null;
}

function renderDetail(mod) {
  document.getElementById("dName").textContent = mod.name;
  document.getElementById("dAuthor").textContent = t("library.detail.author", {
    value: mod.author || t("library.unknown"),
  });

  const statusPill = document.getElementById("dStatus");
  statusPill.hidden = !showCompatBadge();
  if (!statusPill.hidden) {
    statusPill.innerHTML = "";
    const gem = document.createElement("span");
    gem.className = "gem" + (compatGemClass(mod.compat_status) ? " " + compatGemClass(mod.compat_status) : "");
    gem.style.width = "7px";
    gem.style.height = "7px";
    statusPill.appendChild(gem);
    statusPill.append(" " + t(`library.compat.${compatKey(mod.compat_status)}`));
  }

  document.getElementById("dVersion").textContent = mod.installed_version || t("library.unknown");
  document.getElementById("dType").textContent = mod.primary_type || "";
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

  const filesContainer = document.getElementById("dFiles");
  filesContainer.innerHTML = "";
  if (!mod.files.length) {
    filesContainer.appendChild(elementWithText("div", "empty-inline", t("library.detail.no_files")));
  } else {
    mod.files.forEach((path) => filesContainer.appendChild(elementWithText("div", "file-row", path)));
  }

  document.getElementById("openFolderBtn").onclick = () => openFolder(mod.id);
  document.getElementById("deleteBtn").onclick = () => confirmDelete(mod);
  document.getElementById("dTranslationSuggestions").innerHTML = "";
  document.getElementById("detectTranslationBtn").onclick = () => doDetectTranslation(mod.id);
}

function formatInstallDate(isoString) {
  if (!isoString) return t("library.unknown");
  const parsed = new Date(isoString);
  return Number.isNaN(parsed.getTime()) ? t("library.unknown") : parsed.toLocaleDateString();
}

// A conflict's `mods` entries already carry {id, name, active, install_date}
// (see GET /api/conflicts) — enough to act on directly, without a full
// mod-detail fetch. Used for the side-by-side comparison cards below.
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

function renderDetailConflicts(mod) {
  // Reuses state.conflicts/state.blacklistMatches already loaded for the
  // Library grid (see problemModIds()) — openDetail() is only reachable
  // from there, so both are guaranteed populated. Every mod involved gets
  // its own comparison card with the facts relevant to "which one do I
  // keep" (install date, active state) and its own Disable/Delete — never
  // a recommendation of which side to pick, same "suspicion is not
  // confirmation" rule as the Library warnings banner this mirrors.
  const modConflicts = state.conflicts.filter((g) => g.mods.some((m) => m.id === mod.id));
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
        group.kind === "exact_duplicate_mod"
          ? t("library.detail.conflict_exact_duplicate_summary", { count: group.file_count })
          : group.kind === "folder_duplication"
          ? t("library.detail.conflict_folder_duplication_summary")
          : group.kind === "ts4script_name_collision"
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

function openConfirmModal({ title, message, extraNodes, confirmLabel, onConfirm }) {
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
  document.getElementById("confirmOverlay").classList.add("show");
}

function closeConfirm() {
  document.getElementById("confirmOverlay").classList.remove("show");
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
      zips: folder.zip_names.join(", "),
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
  // Theme was already applied pre-paint by the inline <script> in
  // index.html's <head> — this just syncs `state`/the Settings dropdown to
  // match, not re-applies it (avoids a redundant DOM write, not a flash risk).
  applyTheme(localStorage.getItem("simslink-theme") || "dark");
  applyTileSize(localStorage.getItem("simslink-tile-size") || "large");
  applyViewMode(localStorage.getItem("simslink-view-mode") || "grid");
  applySimplifiedNames(localStorage.getItem("simslink-simplified-names") !== "0");
  wireSearch();
  wireNav();
  wireViewToggle();
  wireSimplifiedNamesToggle();
  document.getElementById("confirmCancelBtn").addEventListener("click", closeConfirm);
  document.getElementById("confirmOverlay").addEventListener("click", (e) => {
    if (e.target.id === "confirmOverlay") closeConfirm();
  });
  document.getElementById("conflictsToggle").addEventListener("click", () => {
    state.conflictsExpanded = !state.conflictsExpanded;
    renderWarnings();
  });

  try {
    await Promise.all([loadStatus(), loadMods()]);
    render();
  } catch (err) {
    showError("errorBanner", t("library.action_error", { error: err.message }));
  }

  startDownloadPolling();
}

document.addEventListener("DOMContentLoaded", init);
