// SimsLink frontend — all five views (FastAPI + pywebview). Talks to
// backend/main.py's /api/ routes; never hardcodes UI text — everything
// user-facing goes through t(), backed by i18n/{en,fr}.json.

const DOWNLOAD_POLL_INTERVAL_MS = 3000;

const state = {
  mods: [],
  conflicts: [],
  blacklistMatches: [],
  conflictsExpanded: false,
  strings: {},
  filterQuery: "",
  currentDetailId: null,
  status: null,
  lang: "en",
  theme: "dark",
  tileSize: "large",
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
}

// --- mod list / grid ----------------------------------------------------------

async function loadMods() {
  const [mods, conflicts, blacklistMatches] = await Promise.all([
    apiRequest("/api/mods"),
    apiRequest("/api/conflicts"),
    apiRequest("/api/blacklist/matches"),
  ]);
  state.mods = mods;
  state.conflicts = conflicts;
  state.blacklistMatches = blacklistMatches;
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

  const banner = document.getElementById("conflictsBanner");
  const list = document.getElementById("conflictsList");
  const totalCount = state.conflicts.length + state.blacklistMatches.length;

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
  state.conflicts.forEach((group) => {
    const names = group.mods.map((m) => m.name).join(", ");
    const row = document.createElement("div");
    row.className = "conflict-row";
    const kindLabel = document.createElement("span");
    kindLabel.className = "kind";
    kindLabel.textContent = t(`library.conflicts.${group.kind}`);
    row.appendChild(kindLabel);
    row.append(
      group.kind === "ts4script_name_collision"
        ? t("library.conflicts.ts4script_name_collision_line", { names, filename: group.identifier })
        : t("library.conflicts.duplicate_package_line", { names })
    );
    list.appendChild(row);
  });
  state.blacklistMatches.forEach((match) => {
    const row = document.createElement("div");
    row.className = "conflict-row";
    const kindLabel = document.createElement("span");
    kindLabel.className = "kind";
    kindLabel.textContent = t("library.conflicts.blacklist_match");
    row.appendChild(kindLabel);
    row.append(
      t("library.conflicts.blacklist_match_line", { name: match.mod_name, patterns: match.patterns.join(", ") })
    );
    list.appendChild(row);
  });
}

function render() {
  document.getElementById("subtitle").textContent = t("library.subtitle", {
    installed: state.mods.length,
    active: state.mods.filter((m) => m.active).length,
  });

  renderWarnings();

  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  const mods = visibleMods();
  if (!mods.length) {
    grid.appendChild(elementWithText("div", "empty-state", t("library.empty")));
    return;
  }
  mods.forEach((mod) => grid.appendChild(buildCard(mod)));
}

function buildCard(mod) {
  const card = document.createElement("div");
  card.className = "card" + (mod.active ? "" : " is-inactive");
  card.addEventListener("click", () => openDetail(mod.id));

  const thumb = document.createElement("div");
  thumb.className = "thumb";
  thumb.appendChild(elementWithText("span", "initial", initials(mod.name)));

  if (!mod.active) {
    thumb.appendChild(elementWithText("span", "disabled-tag", t("library.disabled_tag")));
  }

  const badge = document.createElement("span");
  badge.className = "compat-badge";
  const badgeGem = document.createElement("span");
  badgeGem.className = "gem" + (compatGemClass(mod.compat_status) ? " " + compatGemClass(mod.compat_status) : "");
  badgeGem.style.width = "8px";
  badgeGem.style.height = "8px";
  badge.appendChild(badgeGem);
  badge.append(" " + t(`library.compat.${compatKey(mod.compat_status)}`));
  thumb.appendChild(badge);

  const body = document.createElement("div");
  body.className = "card-body";
  body.appendChild(elementWithText("h3", null, mod.name));
  body.appendChild(elementWithText("p", null, mod.short_description || ""));

  const meta = document.createElement("div");
  meta.className = "card-meta";
  meta.appendChild(elementWithText("span", null, mod.installed_version || ""));
  const toggle = document.createElement("span");
  toggle.className = "toggle" + (mod.active ? " on" : "");
  toggle.addEventListener("click", (e) => {
    e.stopPropagation();
    toggleActive(mod);
  });
  meta.appendChild(toggle);
  body.appendChild(meta);

  card.appendChild(thumb);
  card.appendChild(body);
  return card;
}

async function toggleActive(mod) {
  const endpoint = mod.active ? "disable" : "enable";
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(mod.id)}/${endpoint}`, { method: "POST" });
    await loadMods();
    render();
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
  renderSuspects(result.crash_log_id, result.suspects);
}

function renderSuspects(crashLogId, suspects) {
  if (suspects.length) {
    const nodes = [elementWithText("h4", null, t("crash.suspects_heading"))];
    suspects.forEach((s) => {
      nodes.push(
        elementWithText(
          "div",
          "dep-row",
          t("crash.suspect_line", { name: modName(s.mod_id), confidence: s.confidence, reason: s.reason })
        )
      );
    });
    setCrashStatus(...nodes);
    return;
  }

  const startBtn = document.createElement("button");
  startBtn.className = "btn primary";
  startBtn.textContent = t("crash.start_bisection");
  startBtn.addEventListener("click", () => startBisection(crashLogId));
  setCrashStatus(elementWithText("div", "empty-state", t("crash.no_suspects")), startBtn);
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

async function clickClearCache() {
  let targets;
  try {
    targets = await apiRequest("/api/cache/targets");
  } catch (err) {
    showError("crashErrorBanner", t("crash.clear_cache_error", { error: err.message }));
    return;
  }
  const extraNodes = targets.length
    ? targets.map((tgt) => elementWithText("div", "empty-inline", `${tgt.name} — ${tgt.description}`))
    : [elementWithText("div", "empty-inline", t("crash.clear_cache_nothing"))];
  openConfirmModal({
    title: t("crash.clear_cache_title"),
    extraNodes,
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
  statusPill.innerHTML = "";
  const gem = document.createElement("span");
  gem.className = "gem" + (compatGemClass(mod.compat_status) ? " " + compatGemClass(mod.compat_status) : "");
  gem.style.width = "7px";
  gem.style.height = "7px";
  statusPill.appendChild(gem);
  statusPill.append(" " + t(`library.compat.${compatKey(mod.compat_status)}`));

  document.getElementById("dVersion").textContent = mod.installed_version || t("library.unknown");
  document.getElementById("dType").textContent = mod.primary_type || "";
  document.getElementById("dDesc").textContent =
    mod.full_description || mod.short_description || t("library.detail.no_description");

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

async function doDelete(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}`, { method: "DELETE" });
    closeConfirm();
    closeDetail();
    await loadMods();
    render();
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
  wireSearch();
  wireNav();
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
