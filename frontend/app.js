// SimsLink frontend — Library view only (vertical slice of the FastAPI +
// pywebview migration, see CLAUDE.md). Talks to backend/main.py's /api/
// routes; never hardcodes UI text — everything user-facing goes through
// t(), backed by i18n/{en,fr}.json (CLAUDE.md's i18n rule applies to JS
// just as much as it did to the old Flet Python code).

const state = {
  mods: [],
  strings: {},
  filterQuery: "",
  currentDetailId: null,
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

function showError(message) {
  const banner = document.getElementById("errorBanner");
  banner.textContent = message;
  banner.classList.add("show");
  clearTimeout(showError._timer);
  showError._timer = setTimeout(() => banner.classList.remove("show"), 5000);
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
  const status = await apiRequest("/api/status");
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
  state.mods = await apiRequest("/api/mods");
}

function visibleMods() {
  const query = state.filterQuery.trim().toLowerCase();
  if (!query) return state.mods;
  return state.mods.filter(
    (m) => m.name.toLowerCase().includes(query) || (m.author || "").toLowerCase().includes(query)
  );
}

function render() {
  document.getElementById("subtitle").textContent = t("library.subtitle", {
    installed: state.mods.length,
    active: state.mods.filter((m) => m.active).length,
  });

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
    showError(t("library.action_error", { error: err.message }));
  }
}

// --- search --------------------------------------------------------------------

function wireSearch() {
  document.getElementById("searchInput").addEventListener("input", (e) => {
    state.filterQuery = e.target.value;
    render();
  });
}

// --- detail panel ----------------------------------------------------------------

async function openDetail(modId) {
  let mod;
  try {
    mod = await apiRequest(`/api/mods/${encodeURIComponent(modId)}`);
  } catch (err) {
    showError(t("library.action_error", { error: err.message }));
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
  return row;
}

async function openFolder(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}/open-folder`, { method: "POST" });
  } catch (err) {
    showError(t("library.action_error", { error: err.message }));
  }
}

// --- delete confirmation ------------------------------------------------------

function confirmDelete(mod) {
  document.getElementById("confirmTitle").textContent = t("library.delete_confirm.title");
  document.getElementById("confirmMessage").textContent = t("library.delete_confirm.message", {
    name: mod.name,
  });
  document.getElementById("confirmCancelBtn").textContent = t("library.delete_confirm.cancel");
  document.getElementById("confirmOkBtn").textContent = t("library.delete_confirm.confirm");
  document.getElementById("confirmOkBtn").onclick = () => doDelete(mod.id);
  document.getElementById("confirmOverlay").classList.add("show");
}

function closeConfirm() {
  document.getElementById("confirmOverlay").classList.remove("show");
}

async function doDelete(modId) {
  try {
    await apiRequest(`/api/mods/${encodeURIComponent(modId)}`, { method: "DELETE" });
    closeConfirm();
    closeDetail();
    await loadMods();
    render();
  } catch (err) {
    showError(t("library.action_error", { error: err.message }));
  }
}

// --- init ------------------------------------------------------------------------

async function init() {
  await loadI18n(detectLang());
  applyStaticI18n();
  wireSearch();
  document.getElementById("confirmCancelBtn").addEventListener("click", closeConfirm);
  document.getElementById("confirmOverlay").addEventListener("click", (e) => {
    if (e.target.id === "confirmOverlay") closeConfirm();
  });

  try {
    await Promise.all([loadStatus(), loadMods()]);
    render();
  } catch (err) {
    showError(t("library.action_error", { error: err.message }));
  }
}

document.addEventListener("DOMContentLoaded", init);
