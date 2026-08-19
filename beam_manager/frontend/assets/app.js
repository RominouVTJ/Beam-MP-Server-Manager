const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const t = (key) => window.BeamI18n?.t(key) || key;
const state = { user: null, config: null, status: null, live: null, network: null, health: null, users: [], page: "dashboard", importKind: "any" };
const LIVE_POLL_INTERVAL_MS = 1000;
let livePollTimer = null;
let livePollInFlight = false;
let livePollingEnabled = false;

function cookie(name) { return document.cookie.split("; ").find((part) => part.startsWith(`${name}=`))?.split("=").slice(1).join("="); }
async function api(path, options = {}) {
  const method = options.method || "GET";
  const csrf = cookie("beam_manager_csrf");
  const form = options.body instanceof FormData;
  let response;
  try {
    response = await fetch(path, { ...options, method, headers: { Accept: "application/json", ...(!form && options.body ? { "Content-Type": "application/json" } : {}), ...(csrf && !["GET", "HEAD"].includes(method) ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}), ...(options.headers || {}) } });
  } catch { throw new Error(t("errors.network")); }
  const type = response.headers.get("content-type") || "";
  const payload = type.includes("json") ? await response.json().catch(() => ({})) : null;
  if (!response.ok) {
    const safe = response.status === 401 ? t("errors.authentication") : response.status === 403 ? t("errors.authorization") : response.status === 409 ? t("errors.conflict") : t("errors.request_failed");
    const detail = typeof payload?.detail === "string" ? payload.detail : Array.isArray(payload?.detail) ? payload.detail.map((item) => item?.msg).filter(Boolean).join(" · ") : "";
    throw new Error(detail || safe);
  }
  return response.status === 204 ? null : payload;
}

function toast(title, message = "", kind = "success") {
  const item = document.createElement("div"); item.className = `toast ${kind}`;
  const strong = document.createElement("strong"); strong.textContent = title;
  const span = document.createElement("span"); span.textContent = message;
  item.append(strong, span); $("#toast-stack").append(item); setTimeout(() => item.remove(), 6000);
}
function empty(key = "common.empty") { const item = document.createElement("p"); item.className = "empty-state"; item.textContent = t(key); return item; }
function mapName(path = "") { const part = path.split("/").filter(Boolean).at(-2) || path || t("common.unknown"); return part.replaceAll("_", " ").replace(/\b\w/g, (value) => value.toUpperCase()); }
function size(value = 0) { return new Intl.NumberFormat(undefined, { style: "unit", unit: value >= 1048576 ? "megabyte" : "kilobyte", maximumFractionDigits: 1 }).format(value / (value >= 1048576 ? 1048576 : 1024)); }
function setText(id, value) { const element = $(id); if (element) element.textContent = value ?? "—"; }
function clientId() {
  if (globalThis.crypto?.getRandomValues) {
    const values = new Uint32Array(4); globalThis.crypto.getRandomValues(values);
    return [...values].map((value) => value.toString(16).padStart(8, "0")).join("");
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

function currentThemeUser() { return state.user?.username || localStorage.getItem("beam-manager.last-user") || "anonymous"; }
function resolveTheme(choice) { return choice === "system" ? (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light") : choice; }
function applyTheme(choice, persist = true) {
  const selected = ["system", "light", "dark"].includes(choice) ? choice : "system";
  document.documentElement.dataset.themeChoice = selected; document.documentElement.dataset.theme = resolveTheme(selected);
  $("#theme-color")?.setAttribute("content", document.documentElement.dataset.theme === "dark" ? "#090d11" : "#f2f5f1");
  if (persist) localStorage.setItem(`beam-manager.theme.${currentThemeUser()}`, selected);
  $$('button[data-theme-choice]').forEach((button) => button.setAttribute("aria-pressed", String(button.dataset.themeChoice === selected)));
}
function loadThemeForUser() { applyTheme(localStorage.getItem(`beam-manager.theme.${currentThemeUser()}`) || localStorage.getItem("beam-manager.theme.anonymous") || "system", false); }
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (document.documentElement.dataset.themeChoice === "system") applyTheme("system", false); });

function showPage(page) {
  if (!$("#page-" + page)) return;
  state.page = page; $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
  $$('[data-page]').forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  $("#sidebar").classList.remove("open"); $("#drawer-scrim").hidden = true;
  if (page === "maps") loadMaps(); if (page === "vehicles") loadVehicles(); if (page === "mods") loadMods(); if (page === "backups") loadBackups(); if (page === "settings") loadSettings();
}

function renderTopbar() {
  const online = Boolean(state.status?.online), label = online ? t("live.online") : t("live.offline");
  $("#header-status").dataset.state = online ? "online" : "offline"; setText("#header-status-label", label); setText("#dashboard-status", label);
  $("#dashboard-status-dot").classList.toggle("online", online);
  const name = state.config?.Name || state.config?.name || "—", map = mapName(state.config?.Map || state.config?.map_path);
  const players = `${state.live?.player_count || 0} / ${state.config?.MaxPlayers || state.config?.max_players || "—"}`;
  setText("#topbar-server-name", name); setText("#dashboard-server-name", name); setText("#topbar-map", map); setText("#dashboard-map", map); setText("#stat-live-players", players); setText("#dashboard-players", players); renderServiceActions();
}
function visibilityLabel(value) { return t(`network.visibility_${value || "unknown"}`); }
function reachabilityLabel(value) { return t(`network.reachability_${value || "unknown"}`); }
function renderServiceActions() {
  const online = Boolean(state.status?.online), configured = Boolean(state.health?.authkey_configured);
  const start = $('[data-service-action="start"]'), stop = $('[data-service-action="stop"]'), restart = $('[data-service-action="restart"]');
  if (start) start.disabled = online || !configured;
  if (stop) stop.disabled = !online;
  if (restart) restart.disabled = !online || !configured;
}

function renderAuthKeyGate() {
  const configured = Boolean(state.health?.authkey_configured);
  const warning = $("#authkey-warning"); if (warning) warning.hidden = configured;
  setText("#authkey-status", configured ? t("authkey.configured") : t("authkey.not_configured"));
  $$('[data-service-action="start"], [data-service-action="restart"]').forEach((button) => { button.title = configured ? "" : t("authkey.start_blocked"); });
  renderServiceActions();
}

function renderNetwork() {
  if (!state.network) return;
  const endpoint = state.network.lan_ip ? `${state.network.lan_ip}:${state.network.beammp_port}` : t("common.unavailable");
  const publicHost = state.network.configured_public_address || state.network.public_ip;
  const publicEndpoint = publicHost ? `${publicHost}:${state.network.beammp_port}` : t("common.unavailable");
  setText("#dashboard-lan", endpoint); setText("#dashboard-public", publicEndpoint); setText("#settings-lan", endpoint); setText("#settings-public", state.network.public_ip || t("common.unavailable"));
  const visibility = visibilityLabel(state.network.visibility), reachability = reachabilityLabel(state.network.reachability);
  setText("#network-visibility", visibility); setText("#dashboard-visibility", visibility); setText("#network-reachability", reachability); setText("#settings-reachability", reachability); setText("#topbar-network", state.network.reachability === "unknown" ? t("network.topbar_check") : t("network.topbar_lan"));
  setText("#network-help", state.network.reachability === "unknown" ? t("network.unknown_help") : t("network.lan_help"));
}

function playerCard(player) {
  const card = document.createElement("article"); card.className = "player-card";
  const title = document.createElement("strong"); title.textContent = player.name;
  const info = document.createElement("span"); const vehicle = player.vehicles?.[0]; info.textContent = vehicle ? `${vehicle.model || t("live.vehicle")} · ${Math.round(vehicle.speed_kmh || 0)} km/h · ${Math.round(vehicle.ping_ms || 0)} ms` : t("live.no_vehicle");
  card.append(title, info); return card;
}
function renderLive() {
  const players = state.live?.players || []; setText("#live-player-count", String(players.length)); setText("#live-status", state.live?.available ? t("live.online") : (state.live?.message || t("live.unavailable")));
  for (const selector of ["#live-player-cards", "#dashboard-player-cards"]) { const host = $(selector); host.replaceChildren(...players.map(playerCard)); if (!players.length) host.append(empty("players.none")); }
  renderRadar(players);
}
function renderRadar(players) {
  const canvas = $("#radar-canvas"), image = $("#minimap-image"); if (!canvas) return;
  const calibration = state.live?.map_calibration || null;
  const box = canvas.getBoundingClientRect(); canvas.width = Math.max(300, box.width * devicePixelRatio); canvas.height = Math.max(240, box.height * devicePixelRatio);
  const ctx = canvas.getContext("2d"), w = canvas.width, h = canvas.height; ctx.clearRect(0, 0, w, h);
  if (calibration) {
    const url = `/api/minimap/images/${encodeURIComponent(calibration.map_id)}`;
    if (image.dataset.source !== url) { image.dataset.source = url; image.src = url; }
    image.hidden = false; setText("#live-map-help", t("live.map_ready"));
  } else {
    image.hidden = true; setText("#live-map-help", t("live.map_missing"));
    ctx.fillStyle = "#e7eee9"; ctx.fillRect(0, 0, w, h); ctx.strokeStyle = "rgba(79,128,101,.22)"; ctx.lineWidth = devicePixelRatio;
    for (let x = 0; x <= 8; x++) { ctx.beginPath(); ctx.moveTo(w*x/8,0); ctx.lineTo(w*x/8,h); ctx.stroke(); }
    for (let y = 0; y <= 6; y++) { ctx.beginPath(); ctx.moveTo(0,h*y/6); ctx.lineTo(w,h*y/6); ctx.stroke(); }
  }
  const located = players.flatMap((player) => (player.vehicles || []).filter((vehicle) => vehicle.position).map((vehicle) => ({ player, vehicle })));
  located.forEach(({ player, vehicle }, index) => {
    let nx, ny;
    if (calibration) {
      const dx = calibration.world_max_x - calibration.world_min_x, dy = calibration.world_max_y - calibration.world_min_y; if (!dx || !dy) return;
      nx = (vehicle.position[0] - calibration.world_min_x) / dx; ny = (vehicle.position[1] - calibration.world_min_y) / dy;
      if (calibration.invert_x) nx = 1 - nx; if (calibration.invert_y) ny = 1 - ny;
      const angle = Number(calibration.rotation || 0) * Math.PI / 180;
      if (angle) { const px = nx-.5, py = ny-.5; nx = .5 + px*Math.cos(angle) - py*Math.sin(angle); ny = .5 + px*Math.sin(angle) + py*Math.cos(angle); }
    } else { const span = 4000; nx = .5 + Math.max(-span,Math.min(span,vehicle.position[0]))/(span*2); ny = .5 - Math.max(-span,Math.min(span,vehicle.position[1]))/(span*2); }
    if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return;
    const x = nx*w, y = ny*h; ctx.fillStyle = "#22c55e"; ctx.beginPath(); ctx.arc(x,y,7*devicePixelRatio,0,Math.PI*2); ctx.fill(); ctx.lineWidth=3*devicePixelRatio; ctx.strokeStyle="rgba(255,255,255,.9)"; ctx.stroke(); ctx.fillStyle=getComputedStyle(document.body).color; ctx.font=`${12*devicePixelRatio}px sans-serif`; ctx.fillText(player.name || String(index+1),x+12*devicePixelRatio,y-10*devicePixelRatio);
  });
}

async function loadCore() {
  const results = await Promise.allSettled([api("/api/server/status"), api("/api/server/config"), api("/api/live"), api("/api/pending"), api("/api/health")]);
  if (results[0].status === "fulfilled") state.status = results[0].value; if (results[1].status === "fulfilled") state.config = results[1].value; if (results[2].status === "fulfilled") state.live = results[2].value; if (results[4].status === "fulfilled") state.health = results[4].value;
  renderTopbar(); renderLive(); renderAuthKeyGate(); if (results[3].status === "fulfilled") $("#pending-bar").hidden = !results[3].value.count;
  api("/api/network/status").then((value) => { state.network = value; renderNetwork(); }).catch(() => { state.network = { reachability: "unknown", visibility: state.config?.Private ? "unlisted" : "listed" }; renderNetwork(); });
  startLivePolling();
}

function scheduleLivePoll(delay = LIVE_POLL_INTERVAL_MS) {
  if (livePollTimer) clearTimeout(livePollTimer);
  livePollTimer = null;
  if (!livePollingEnabled || document.hidden) return;
  livePollTimer = setTimeout(refreshLive, delay);
}

async function refreshLive() {
  livePollTimer = null;
  if (!livePollingEnabled || document.hidden || livePollInFlight) return;
  livePollInFlight = true;
  try {
    state.live = await api("/api/live");
    renderLive();
    renderTopbar();
  } catch {
    // Keep the last good snapshot and avoid toast-spamming on transient polling errors.
  } finally {
    livePollInFlight = false;
    scheduleLivePoll();
  }
}

function startLivePolling() {
  livePollingEnabled = true;
  scheduleLivePoll();
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (livePollTimer) clearTimeout(livePollTimer);
    livePollTimer = null;
    return;
  }
  if (livePollingEnabled) scheduleLivePoll(0);
});

function contentVisual(item, kind) {
  const visual = document.createElement("div"); visual.className = `content-visual ${kind}-visual`; visual.dataset.mapId = item.id || item.internal_name || "generic";
  const source = item.thumbnail_url || item.preview_url;
  if (source) { const image = document.createElement("img"); image.src = source; image.alt = ""; image.loading = "lazy"; visual.append(image); }
  if (kind === "map" && item.selected) { const ribbon = document.createElement("span"); ribbon.className = "active-ribbon"; ribbon.textContent = t("maps.active"); visual.append(ribbon); }
  return visual;
}
function contentCard(item, kind) {
  const selected = kind === "map" && Boolean(item.selected);
  const enabled = Boolean(item.active);
  const card = document.createElement("article"); card.className = `content-card${selected ? " active" : ""}${enabled && !selected ? " enabled" : ""}`;
  if (["map", "vehicle"].includes(kind)) card.append(contentVisual(item, kind));
  const body = document.createElement("div"); body.className = "content-card-body";
  const title = document.createElement("strong"); title.textContent = item.name || item.display_name || mapName(item.path);
  const badge = document.createElement("span"); badge.className = "state-badge";
  if (kind === "map") badge.textContent = selected ? t("content_state.selected_map") : item.official ? t("content_state.official") : enabled ? t("content_state.distributed") : t("content_state.installed");
  else badge.textContent = enabled ? t("content_state.distributed") : t("content_state.installed");
  body.append(title, badge);

  if (kind === "map") {
    const use = document.createElement("button"); use.className = selected ? "secondary-button" : "primary-button"; use.textContent = selected ? t("maps.in_use") : t("maps.use"); use.disabled = selected; use.onclick = () => selectMap(item.path || item.map_path); body.append(use);
    if (!item.official) {
      const distribution = document.createElement("button"); distribution.className = "secondary-button"; distribution.textContent = enabled ? t("content_state.disable_distribution") : t("content_state.enable_distribution"); distribution.disabled = selected && enabled; distribution.onclick = () => toggleMod(item.id, enabled); body.append(distribution);
      const remove = document.createElement("button"); remove.className = "danger-button"; remove.textContent = t("common.delete"); remove.disabled = selected; remove.title = selected ? t("content_state.selected_protected") : ""; remove.onclick = () => deleteMod(item.id); body.append(remove);
    }
  } else {
    const distribution = document.createElement("button"); distribution.className = "secondary-button"; distribution.textContent = enabled ? t("content_state.disable_distribution") : t("content_state.enable_distribution"); distribution.onclick = () => toggleMod(item.id, enabled); body.append(distribution);
    const remove = document.createElement("button"); remove.className = "danger-button"; remove.textContent = t("common.delete"); remove.onclick = () => deleteMod(item.id); body.append(remove);
  }
  card.append(body); return card;
}
async function selectMap(path) {
  try { const config = await api("/api/maps/select", { method: "POST", body: JSON.stringify({ path }) }); state.config = config; renderTopbar(); setText("#active-map-name", mapName(path)); $("#pending-bar").hidden = false; toast(t("feedback.map_selected"), t("feedback.restart_required")); await loadMaps(); }
  catch { toast(t("feedback.map_failed"), t("feedback.config_unchanged"), "error"); }
}
async function loadMaps() {
  try {
    const payload = await api("/api/maps");
    const official = payload.official.map((item) => ({ ...item, selected: item.path === payload.active_path }));
    const modded = payload.modded.map((item) => ({ ...item, path: item.map_path, official: false, selected: item.map_path === payload.active_path }));
    const items = [...official, ...modded];
    setText("#active-map-name", mapName(payload.active_path)); setText("#maps-count", String(items.length)); $("#maps-grid").replaceChildren(...items.map((item) => contentCard(item, "map")));
  } catch { $("#maps-grid").replaceChildren(empty("errors.content_unavailable")); }
}
async function loadVehicles() { try { const payload = await api("/api/vehicles"); setText("#vehicles-count", String(payload.total_count)); $("#vehicles-grid").replaceChildren(...payload.items.map((item) => contentCard(item, "vehicle"))); if (!payload.items.length) $("#vehicles-grid").append(empty()); } catch { $("#vehicles-grid").replaceChildren(empty("errors.content_unavailable")); } }
async function toggleMod(id, active) {
  try {
    await api(`/api/mods/${id}/${active ? "disable" : "enable"}`, { method: "POST" });
    $("#pending-bar").hidden = false;
    toast(active ? t("feedback.mod_disabled") : t("feedback.mod_enabled"), t("feedback.restart_required"));
    await Promise.allSettled([loadMaps(), loadVehicles(), loadMods()]);
  } catch (error) { toast(t("feedback.mod_failed"), error?.message || t("errors.request_failed"), "error"); }
}
async function deleteMod(id) {
  try {
    const maps = await api("/api/maps").catch(() => null);
    const selected = maps?.modded?.find((item) => item.id === id && item.map_path === maps.active_path);
    if (selected) { toast(t("feedback.mod_failed"), t("content_state.selected_protected"), "error"); return; }
    if (!confirm(t("confirmations.delete_mod"))) return;
    const item = maps?.modded?.find((candidate) => candidate.id === id);
    await api(`/api/mods/${id}`, { method: "DELETE" });
    if (item?.active) $("#pending-bar").hidden = false;
    toast(t("feedback.mod_deleted"));
    await Promise.allSettled([loadMaps(), loadVehicles(), loadMods()]);
  } catch (error) { toast(t("feedback.mod_failed"), error?.message || t("errors.request_failed"), "error"); }
}
async function loadMods() {
  try { const payload = await api("/api/mods"), items = [...payload.items, ...payload.unidentified]; setText("#mods-count", String(items.length)); const rows = items.map((item) => { const row = document.createElement("article"); row.className = "mod-row"; const info = document.createElement("div"); const name = document.createElement("strong"); name.textContent = item.display_name; const detail = document.createElement("span"); detail.textContent = `${t(`content_type.${item.type}`)} · ${item.active ? t("common.active") : t("common.disabled")}`; info.append(name, detail); const actions = document.createElement("div"); const toggle = document.createElement("button"); toggle.className = "secondary-button"; toggle.textContent = item.active ? t("mods.disable") : t("mods.enable"); toggle.onclick = () => toggleMod(item.id, item.active); const remove = document.createElement("button"); remove.className = "danger-button"; remove.textContent = t("mods.delete"); remove.onclick = () => deleteMod(item.id); actions.append(toggle, remove); row.append(info, actions); return row; }); $("#mods-table").replaceChildren(...rows); if (!rows.length) $("#mods-table").append(empty()); }
  catch { $("#mods-table").replaceChildren(empty("errors.content_unavailable")); }
}

async function loadBackups() { try { const payload = await api("/api/backups"); const cards = payload.items.map((backup) => { const card = document.createElement("article"); card.className = "content-card"; const name = document.createElement("strong"); name.textContent = backup.name; const meta = document.createElement("span"); meta.textContent = new Date(backup.created_at).toLocaleString(); const actions = document.createElement("div"); const restore = document.createElement("button"); restore.className = "secondary-button"; restore.textContent = t("backups.restore"); restore.onclick = () => restoreBackup(backup.id); const remove = document.createElement("button"); remove.className = "danger-button"; remove.textContent = t("common.delete"); remove.onclick = () => removeBackup(backup.id); actions.append(restore, remove); card.append(name, meta, actions); return card; }); $("#backups-grid").replaceChildren(...cards); if (!cards.length) $("#backups-grid").append(empty("backups.none")); } catch { $("#backups-grid").replaceChildren(empty("errors.content_unavailable")); } }
async function createBackup() { try { await api("/api/backups", { method: "POST", body: JSON.stringify({ name: null }) }); toast(t("feedback.backup_done")); loadBackups(); } catch { toast(t("feedback.backup_failed"), t("errors.request_failed"), "error"); } }
async function restoreBackup(id) { if (!confirm(t("confirmations.restore_backup"))) return; try { await api(`/api/backups/${id}/restore`, { method: "POST" }); $("#pending-bar").hidden = false; toast(t("feedback.backup_restored"), t("feedback.restart_required")); } catch { toast(t("feedback.restore_failed"), t("errors.request_failed"), "error"); } }
async function removeBackup(id) { if (!confirm(t("confirmations.delete_backup"))) return; try { await api(`/api/backups/${id}`, { method: "DELETE" }); toast(t("feedback.backup_deleted")); loadBackups(); } catch { toast(t("feedback.backup_failed"), t("errors.request_failed"), "error"); } }

async function loadSettings() {
  if (state.config) {
    $("#setting-server-name").value = state.config.Name || "";
    $("#setting-description").value = state.config.Description || "";
    $("#setting-tags").value = state.config.Tags || "";
    $("#setting-max-players").value = state.config.MaxPlayers;
    $("#setting-max-cars").value = state.config.MaxCars;
    $("#setting-public-listing").checked = !state.config.Private;
    $("#setting-allow-guests").checked = Boolean(state.config.AllowGuests);
    $("#setting-log-chat").checked = Boolean(state.config.LogChat);
    $("#setting-debug").checked = Boolean(state.config.Debug);
    $("#setting-information-packet").checked = Boolean(state.config.InformationPacket);
    $("#setting-port").value = state.config.Port;
    $("#setting-bind-ip").value = state.config.IP || "::";
    $("#setting-resource-folder").value = state.config.ResourceFolder || "Resources";
    setText("#setting-current-map", mapName(state.config.Map));
  }
  $("#language-select").value = window.BeamI18n?.language || "en";
  loadThemeForUser(); renderNetwork(); renderAuthKeyGate(); loadUsers();
}
async function saveServerSettings(event) {
  event.preventDefault();
  const payload = {
    Name: $("#setting-server-name").value,
    Description: $("#setting-description").value,
    Tags: $("#setting-tags").value,
    MaxPlayers: Number($("#setting-max-players").value),
    MaxCars: Number($("#setting-max-cars").value),
    Private: !$("#setting-public-listing").checked,
    AllowGuests: $("#setting-allow-guests").checked,
    LogChat: $("#setting-log-chat").checked,
    Debug: $("#setting-debug").checked,
    InformationPacket: $("#setting-information-packet").checked,
    Port: Number($("#setting-port").value),
    IP: $("#setting-bind-ip").value,
    ResourceFolder: $("#setting-resource-folder").value
  };
  try {
    state.config = await api("/api/server/config", { method: "PATCH", body: JSON.stringify(payload) });
    renderTopbar(); $("#pending-bar").hidden = false;
    toast(t("feedback.settings_saved"), t("feedback.restart_required"));
  } catch (error) {
    toast(t("feedback.settings_failed"), error?.message || t("errors.request_failed"), "error");
  }
}

async function saveAuthKey(event) { event.preventDefault(); try { await api("/api/beammp/authkey", { method: "POST", body: JSON.stringify({ authkey: $("#authkey-value").value, security_code: $("#authkey-security-code").value }) }); $("#authkey-value").value = ""; $("#authkey-security-code").value = ""; state.health = await api("/api/health"); renderAuthKeyGate(); toast(t("authkey.saved")); } catch (error) { toast(t("authkey.save_failed"), error?.message || t("errors.request_failed"), "error"); } }
function userRow(user) { const row=document.createElement("article"); row.className="mod-row user-row"; const info=document.createElement("div"); const title=document.createElement("strong"); title.textContent=user.username; const meta=document.createElement("span"); meta.textContent=`${t(`users.${user.role}`)} · ${user.enabled?t("users.enabled"):t("users.disabled")} · ${user.active_sessions||0} ${t("users.sessions")}`; info.append(title,meta); const actions=document.createElement("div"); const toggle=document.createElement("button"); toggle.className="secondary-button"; toggle.textContent=user.enabled?t("users.disable"):t("users.enable"); toggle.onclick=()=>setUserEnabled(user.username,!user.enabled); const role=document.createElement("button"); role.className="secondary-button"; role.textContent=user.role==="admin"?t("users.make_viewer"):t("users.make_admin"); role.onclick=()=>setUserRole(user.username,user.role==="admin"?"viewer":"admin"); const password=document.createElement("button"); password.className="secondary-button"; password.textContent=t("users.reset_password"); password.onclick=()=>resetUserPassword(user.username); const remove=document.createElement("button"); remove.className="danger-button"; remove.textContent=t("common.delete"); remove.onclick=()=>deleteUser(user.username); actions.append(toggle,role,password,remove); row.append(info,actions); return row; }
async function loadUsers(){try{const payload=await api("/api/users");state.users=payload.items||[];$("#users-table")?.replaceChildren(...state.users.map(userRow));}catch{}}
function securityCodeForUsers(){const code=$("#users-security-code").value.trim();if(!code){toast(t("security.code_required"),"","error");return null}return code;}
async function createUser(event){event.preventDefault();const code=securityCodeForUsers();if(!code)return;try{await api("/api/users",{method:"POST",body:JSON.stringify({username:$("#new-user-name").value,password:$("#new-user-password").value,confirmation:$("#new-user-confirmation").value,role:$("#new-user-role").value,security_code:code})});event.currentTarget.reset();toast(t("users.created"));loadUsers();}catch(error){toast(t("users.change_failed"),error?.message||t("errors.request_failed"),"error");}}
async function setUserEnabled(username,enabled){const code=securityCodeForUsers();if(!code)return;try{await api(`/api/users/${encodeURIComponent(username)}/enabled`,{method:"POST",body:JSON.stringify({enabled,security_code:code})});loadUsers();}catch(error){toast(t("users.change_failed"),error?.message||t("errors.request_failed"),"error");}}
async function setUserRole(username,role){const code=securityCodeForUsers();if(!code)return;try{await api(`/api/users/${encodeURIComponent(username)}/role`,{method:"POST",body:JSON.stringify({role,security_code:code})});loadUsers();}catch(error){toast(t("users.change_failed"),error?.message||t("errors.request_failed"),"error");}}
async function resetUserPassword(username){const code=securityCodeForUsers();if(!code)return;const password=prompt(t("users.new_password"));if(!password)return;const confirmation=prompt(t("users.confirm_new_password"));if(password!==confirmation){toast(t("users.change_failed"),t("users.password_mismatch"),"error");return;}try{await api(`/api/users/${encodeURIComponent(username)}/password`,{method:"POST",body:JSON.stringify({password,confirmation,security_code:code})});toast(t("users.password_reset"));loadUsers();}catch(error){toast(t("users.change_failed"),error?.message||t("errors.request_failed"),"error");}}
async function deleteUser(username){const code=securityCodeForUsers();if(!code||!confirm(t("users.confirm_delete")))return;try{await api(`/api/users/${encodeURIComponent(username)}`,{method:"DELETE",body:JSON.stringify({security_code:code})});loadUsers();}catch(error){toast(t("users.change_failed"),error?.message||t("errors.request_failed"),"error");}}
async function loadAdvanced() { const [logs, diagnostic] = await Promise.allSettled([api("/api/manager/logs?limit=100"), api("/api/diagnostic")]); $("#manager-logs").textContent = logs.status === "fulfilled" ? logs.value.entries.map((entry) => `${entry.timestamp || ""} ${entry.level} ${entry.message}`).join("\n") : t("errors.content_unavailable"); $("#diagnostic-output").textContent = diagnostic.status === "fulfilled" ? JSON.stringify(diagnostic.value, null, 2) : t("errors.content_unavailable"); }
async function testConnectivity() {
  const button = $("#test-connectivity");
  if (button) button.disabled = true;
  setText("#connectivity-result", t("network.test_running"));
  try {
    const result = await api("/api/network/connectivity-check", { method: "POST" });
    setText("#connectivity-public-endpoint", result.public_ip ? `${result.public_ip}:${result.port}` : t("common.unavailable"));
    let message = result.local_ok ? t("network.test_local_ok") : t("network.test_local_failed");
    if (result.external_status === "ok") message = `${message} · ${t("network.test_external_ok")}`;
    else if (result.external_status === "error") message = `${message} · ${t("network.test_external_failed")}`;
    else if (result.external_status === "unavailable") message = `${message} · ${t("network.test_external_unavailable")}`;
    setText("#connectivity-result", message);
    const ok = Boolean(result.local_ok && result.external_ok);
    $("#connectivity-result")?.classList.toggle("ok", ok);
    $("#connectivity-result")?.classList.toggle("bad", !ok);
    if (result.external_details) toast(t("network.checkbeammp"), result.external_details, result.external_ok ? "success" : "error");
  } catch (error) {
    setText("#connectivity-result", error?.message || t("network.test_local_failed"));
  } finally {
    if (button) button.disabled = false;
  }
}

async function checkUpdate() { try { const value = await api("/api/beammp/update-check?refresh=true"); setText("#installed-beammp-version", value.installed_version || t("common.unknown")); setText("#latest-beammp-version", value.latest_version || t("common.unknown")); toast(t("feedback.update_checked")); } catch { toast(t("feedback.update_failed"), t("errors.network"), "error"); } }

const ImportQueue = {
  items: [], running: false,
  add(files) { for (const file of files) { if (!file.name.toLowerCase().endsWith(".zip")) { toast(t("imports.invalid_zip"), file.name, "error"); continue; } this.items.push({ id: clientId(), file, name: file.name, size: file.size, progress: 0, state: "waiting", result: "", error: "" }); } this.render(); this.run(); },
  async run() { if (this.running) return; this.running = true; while (true) { const item = this.items.find((value) => value.state === "waiting"); if (!item) break; await this.process(item); } this.running = false; },
  analyze(item) { return new Promise((resolve, reject) => { item.state = "uploading"; this.render(); const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/uploads/analyze"); xhr.setRequestHeader("Accept", "application/json"); const csrf = cookie("beam_manager_csrf"); if (csrf) xhr.setRequestHeader("X-CSRF-Token", decodeURIComponent(csrf)); xhr.upload.onprogress = (event) => { if (event.lengthComputable) { item.progress = Math.round(event.loaded / event.total * 100); this.render(); } }; xhr.onerror = () => reject(new Error(t("errors.network"))); xhr.onload = () => { let payload = {}; try { payload = JSON.parse(xhr.responseText || "{}"); } catch {} if (xhr.status >= 200 && xhr.status < 300) resolve(payload); else reject(new Error(typeof payload?.detail === "string" ? payload.detail : t("imports.analysis_failed"))); }; const form = new FormData(); form.append("file", item.file); xhr.send(form); }); },
  async process(item) { try { const analysis = await this.analyze(item); item.state = "analyzing"; item.progress = 100; item.result = t(`content_type.${analysis.type}`); this.render(); if (state.importKind !== "any" && analysis.type !== state.importKind) throw new Error(t("imports.unexpected_type")); item.state = "installing"; item.progress = 0; this.render(); let job; try { job = await api("/api/uploads/install", { method: "POST", body: JSON.stringify({ token: analysis.token, replace: false }) }); } catch (error) { if (!analysis.duplicate || !confirm(t("confirmations.replace_mod"))) throw error; job = await api("/api/uploads/install", { method: "POST", body: JSON.stringify({ token: analysis.token, replace: true }) }); } while (!["success", "error", "cancelled"].includes(job.state)) { await new Promise((resolve) => setTimeout(resolve, 500)); job = await api(`/api/uploads/jobs/${job.id}`); item.progress = job.total ? Math.round(job.transferred / job.total * 100) : 0; this.render(); } if (job.state !== "success") throw new Error(job.message || t("imports.install_failed")); item.state = "done"; item.progress = 100; item.result = t(`imports.result_${analysis.type}`); this.render(); await Promise.allSettled([loadMaps(), loadVehicles(), loadMods()]); } catch (error) { item.state = "error"; item.error = error.message || t("imports.install_failed"); this.render(); } },
  retry(id) { const item = this.items.find((value) => value.id === id); if (!item) return; item.state = "waiting"; item.error = ""; item.progress = 0; this.render(); this.run(); },
  remove(id) { this.items = this.items.filter((value) => value.id !== id); this.render(); },
  clear() { this.items = this.items.filter((value) => !["done", "error"].includes(value.state)); this.render(); },
  render() { $("#import-panel").hidden = !this.items.length; const done = this.items.filter((item) => item.state === "done").length; setText("#import-summary", `${done} / ${this.items.length}`); const rows = this.items.map((item) => { const row = document.createElement("article"); row.className = "import-item"; row.dataset.state = item.state; const head = document.createElement("div"); const title = document.createElement("strong"); title.textContent = item.name; const meta = document.createElement("span"); meta.textContent = `${size(item.size)} · ${t(`imports.state_${item.state}`)}`; head.append(title, meta); const track = document.createElement("div"); track.className = "import-progress"; const fill = document.createElement("i"); fill.style.width = `${item.progress}%`; track.append(fill); const result = document.createElement("p"); result.textContent = item.error || item.result || `${item.progress} %`; if (item.state === "error") result.className = "error-text"; const actions = document.createElement("div"); if (item.state === "error") { const retry = document.createElement("button"); retry.className = "text-link"; retry.textContent = t("imports.retry"); retry.onclick = () => this.retry(item.id); actions.append(retry); } if (["done", "error"].includes(item.state)) { const remove = document.createElement("button"); remove.className = "text-link"; remove.textContent = t("imports.remove"); remove.onclick = () => this.remove(item.id); actions.append(remove); } row.append(head, track, result, actions); return row; }); $("#import-list").replaceChildren(...rows); }
};
window.BeamImportQueue = ImportQueue;

async function serviceAction(action) {
  if (["stop", "restart"].includes(action) && !confirm(t(`confirmations.${action}_server`))) return;
  const button = $(`[data-service-action="${action}"]`); if (button?.disabled) return; if (button) button.disabled = true;
  try { await api(`/api/server/actions/${action}`, { method: "POST" }); toast(t(`feedback.server_${action}`)); await loadCore(); }
  catch (error) { toast(t("feedback.server_action_failed"), error?.message || t("errors.request_failed"), "error"); }
  finally { renderServiceActions(); }
}
async function applyRestart() { try { $("#apply-restart").disabled = true; await api("/api/apply", { method: "POST" }); $("#pending-bar").hidden = true; toast(t("feedback.server_restart")); await loadCore(); } catch { toast(t("feedback.restart_failed"), t("errors.request_failed"), "error"); } finally { $("#apply-restart").disabled = false; } }

$("#show-recovery")?.addEventListener("click",()=>{$("#recovery-panel").hidden=!$("#recovery-panel").hidden;});
$("#recover-admin")?.addEventListener("click",async()=>{const error=$("#recovery-error");error.textContent="";const password=$("#recovery-password").value,confirmation=$("#recovery-confirmation").value;if(password!==confirmation){error.textContent=t("users.password_mismatch");return;}try{await api("/api/auth/recover",{method:"POST",body:JSON.stringify({username:$("#recovery-username").value,security_code:$("#recovery-code").value,password,confirmation})});$("#recovery-panel").hidden=true;$("#login-form [name=username]").value=$("#recovery-username").value;toast(t("recovery.done"));}catch{error.textContent=t("recovery.failed");}});

$$('[data-page]').forEach((button) => button.addEventListener("click", () => showPage(button.dataset.page))); $$('[data-go-page]').forEach((button) => button.addEventListener("click", () => showPage(button.dataset.goPage))); $$('[data-service-action]').forEach((button) => button.addEventListener("click", () => serviceAction(button.dataset.serviceAction)));
$("#menu-toggle").addEventListener("click", () => { $("#sidebar").classList.toggle("open"); $("#drawer-scrim").hidden = !$("#sidebar").classList.contains("open"); });
$("#authkey-form")?.addEventListener("submit", saveAuthKey); $("#create-user-form")?.addEventListener("submit", createUser); $("#drawer-scrim").addEventListener("click", () => { $("#sidebar").classList.remove("open"); $("#drawer-scrim").hidden = true; });
async function copyText(value) {
  if (!value || value === "—") return false;
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {}
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  textarea.style.opacity = "0";
  document.body.append(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  let copied = false;
  try { copied = document.execCommand("copy"); } catch {}
  textarea.remove();
  return copied;
}

$$('[data-copy-target]').forEach((button) => button.addEventListener("click", async () => {
  const target = $("#" + button.dataset.copyTarget);
  const value = target?.textContent?.trim();
  if (!value || value === "—") return;
  if (await copyText(value)) toast(t("feedback.copied"), value);
  else toast(t("errors.request_failed"), "", "error");
}));
$$('button[data-theme-choice]').forEach((button) => button.addEventListener("click", () => applyTheme(button.dataset.themeChoice))); $("#language-select").addEventListener("change", async (event) => { await window.BeamI18n.setLanguage(event.target.value); ImportQueue.render(); renderTopbar(); renderNetwork(); renderLive(); });
$$('[data-import-pick]').forEach((button) => button.addEventListener("click", () => { state.importKind = button.dataset.acceptKind; $("#import-file-input").click(); })); $("#import-file-input").addEventListener("change", (event) => { ImportQueue.add(event.target.files); event.target.value = ""; });
$$('[data-import-zone]').forEach((zone) => { zone.addEventListener("dragover", (event) => { event.preventDefault(); zone.classList.add("dragging"); }); zone.addEventListener("dragleave", () => zone.classList.remove("dragging")); zone.addEventListener("drop", (event) => { event.preventDefault(); zone.classList.remove("dragging"); state.importKind = zone.querySelector('[data-import-pick]').dataset.acceptKind; ImportQueue.add(event.dataTransfer.files); }); });
$("#test-connectivity")?.addEventListener("click", testConnectivity);
$("#clear-completed").addEventListener("click", () => ImportQueue.clear()); $("#create-backup").addEventListener("click", createBackup); $("#server-settings-form").addEventListener("submit", saveServerSettings); $("#check-update").addEventListener("click", checkUpdate); $("#advanced-settings").addEventListener("toggle", (event) => { if (event.target.open) loadAdvanced(); }); $("#refresh-logs").addEventListener("click", loadAdvanced); $("#refresh-diagnostic").addEventListener("click", loadAdvanced); $("#apply-restart").addEventListener("click", applyRestart);
$("#logout").addEventListener("click", async () => { await api("/api/auth/logout", { method: "POST" }); location.reload(); });
$("#login-form").addEventListener("submit", async (event) => { event.preventDefault(); const data = new FormData(event.currentTarget); try { const value = await api("/api/auth/login", { method: "POST", body: JSON.stringify({ username: data.get("username"), password: data.get("password"), trust_device: data.has("trust_device") }) }); state.user = value.user; localStorage.setItem("beam-manager.last-user", value.user.username); loadThemeForUser(); $("#login-gate").hidden = true; loadCore(); } catch (error) { setText("#login-error", error.message); } });

document.addEventListener("DOMContentLoaded", async () => { try { const auth = await api("/api/auth/state"); state.user = auth.user; if (auth.user?.username) localStorage.setItem("beam-manager.last-user", auth.user.username); loadThemeForUser(); if (auth.login_required && !auth.authenticated) { $("#login-gate").hidden = false; return; } await loadCore(); } catch { $("#login-gate").hidden = false; } });
