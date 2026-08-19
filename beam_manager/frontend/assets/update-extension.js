(() => {
  "use strict";

  const words = {
    fr: {
      title: "Mise à jour du Manager",
      help: "Installe uniquement les versions publiées et validées de Beam-MP-Server-Manager. La configuration, les utilisateurs, les mods et les sauvegardes sont conservés.",
      installed: "Installée",
      available: "Disponible",
      last: "Dernier résultat",
      check: "Vérifier",
      install: "Installer la mise à jour",
      manual: "Installer un package local",
      none: "Aucune mise à jour disponible",
      unsupported: "Mise à jour intégrée indisponible sur cet hôte",
      checking: "Vérification…",
      scheduling: "Préparation et installation…",
      restart: "La mise à jour est programmée. Le Manager va redémarrer et cette page peut se déconnecter quelques secondes.",
      choose: "Choisir un fichier .update.zip",
      invalid: "Sélectionnez un package .update.zip valide.",
      failed: "La mise à jour n'a pas pu être lancée.",
    },
    en: {
      title: "Manager update",
      help: "Installs only published and validated Beam-MP-Server-Manager releases. Configuration, users, mods and backups are preserved.",
      installed: "Installed",
      available: "Available",
      last: "Last result",
      check: "Check",
      install: "Install update",
      manual: "Install local package",
      none: "No update available",
      unsupported: "Built-in update is unavailable on this host",
      checking: "Checking…",
      scheduling: "Preparing and installing…",
      restart: "The update is scheduled. The Manager will restart and this page may disconnect for a few seconds.",
      choose: "Choose a .update.zip file",
      invalid: "Select a valid .update.zip package.",
      failed: "The update could not be started.",
    },
  };

  const lang = () => (document.documentElement.lang === "fr" ? "fr" : "en");
  const t = (key) => words[lang()][key];

  function cookie(name) {
    return document.cookie.split("; ").find((part) => part.startsWith(`${name}=`))?.split("=").slice(1).join("=");
  }

  async function request(path, options = {}) {
    const method = options.method || "GET";
    const csrf = cookie("beam_manager_csrf");
    const response = await fetch(path, {
      ...options,
      method,
      headers: {
        Accept: "application/json",
        ...(csrf && !["GET", "HEAD"].includes(method) ? { "X-CSRF-Token": decodeURIComponent(csrf) } : {}),
        ...(options.headers || {}),
      },
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `HTTP ${response.status}`);
    return payload;
  }

  function text(tag, className, value) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = value;
    return node;
  }

  function installStyle() {
    if (document.getElementById("beam-update-style")) return;
    const style = document.createElement("style");
    style.id = "beam-update-style";
    style.textContent = `
      .beam-update-card{margin-top:14px;padding:18px;border:1px solid var(--line,#39434c);border-radius:12px;background:var(--panel,#121920)}
      .beam-update-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}.beam-update-head h3{margin:0 0 5px}.beam-update-help{margin:0;opacity:.75;max-width:760px}
      .beam-update-facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:16px 0}.beam-update-fact{padding:12px;border:1px solid var(--line,#39434c);border-radius:9px}.beam-update-fact small{display:block;opacity:.7;margin-bottom:4px}.beam-update-fact strong{word-break:break-word}
      .beam-update-actions{display:flex;gap:10px;flex-wrap:wrap;align-items:center}.beam-update-message{min-height:1.4em;margin:12px 0 0;opacity:.85}.beam-update-error{color:#ff7b7b}.beam-update-input{display:none}@media(max-width:760px){.beam-update-facts{grid-template-columns:1fr}}
    `;
    document.head.append(style);
  }

  function createCard() {
    const host = document.querySelector("#page-settings .settings-core") || document.getElementById("page-settings");
    if (!host || document.getElementById("beam-update-card")) return null;
    installStyle();
    const card = document.createElement("section");
    card.id = "beam-update-card";
    card.className = "beam-update-card";
    card.innerHTML = `
      <div class="beam-update-head"><div><h3 data-role="title"></h3><p class="beam-update-help" data-role="help"></p></div></div>
      <div class="beam-update-facts">
        <div class="beam-update-fact"><small data-role="installed-label"></small><strong data-role="installed">—</strong></div>
        <div class="beam-update-fact"><small data-role="available-label"></small><strong data-role="available">—</strong></div>
        <div class="beam-update-fact"><small data-role="last-label"></small><strong data-role="last">—</strong></div>
      </div>
      <div class="beam-update-actions">
        <button type="button" class="secondary-button" data-role="check"></button>
        <button type="button" class="primary-button" data-role="install" disabled></button>
        <button type="button" class="secondary-button" data-role="manual"></button>
        <input class="beam-update-input" data-role="file" type="file" accept=".zip,.update.zip,application/zip">
      </div>
      <p class="beam-update-message" data-role="message"></p>
    `;
    host.append(card);
    return card;
  }

  function role(card, name) { return card.querySelector(`[data-role="${name}"]`); }

  function labels(card) {
    role(card, "title").textContent = t("title");
    role(card, "help").textContent = t("help");
    role(card, "installed-label").textContent = t("installed");
    role(card, "available-label").textContent = t("available");
    role(card, "last-label").textContent = t("last");
    role(card, "check").textContent = t("check");
    role(card, "install").textContent = t("install");
    role(card, "manual").textContent = t("manual");
  }

  function lastResult(value) {
    if (!value) return "—";
    const version = value.target_version ? ` ${value.target_version}` : "";
    return `${value.status || "—"}${version}`;
  }

  async function refresh(card) {
    labels(card);
    const message = role(card, "message");
    const install = role(card, "install");
    const manual = role(card, "manual");
    message.classList.remove("beam-update-error");
    message.textContent = t("checking");
    install.disabled = true;
    try {
      const status = await request("/api/appliance/update/status");
      role(card, "installed").textContent = status.installed_version || "—";
      role(card, "available").textContent = status.available_version || "—";
      role(card, "last").textContent = lastResult(status.last_result);
      manual.disabled = !status.supported;
      install.disabled = !(status.supported && status.update_available);
      message.textContent = !status.supported ? t("unsupported") : status.update_available ? (status.message || "") : (status.message || t("none"));
    } catch (error) {
      message.classList.add("beam-update-error");
      message.textContent = error.message || t("failed");
      manual.disabled = true;
    }
  }

  async function installLatest(card) {
    const install = role(card, "install");
    const message = role(card, "message");
    install.disabled = true;
    message.classList.remove("beam-update-error");
    message.textContent = t("scheduling");
    try {
      await request("/api/appliance/update/install-latest", { method: "POST" });
      message.textContent = t("restart");
      setTimeout(() => refresh(card), 12000);
    } catch (error) {
      message.classList.add("beam-update-error");
      message.textContent = error.message || t("failed");
      await refresh(card);
    }
  }

  async function uploadPackage(card, file) {
    const message = role(card, "message");
    if (!file || !file.name.toLowerCase().endsWith(".update.zip")) {
      message.classList.add("beam-update-error");
      message.textContent = t("invalid");
      return;
    }
    const form = new FormData();
    form.append("package", file, file.name);
    message.classList.remove("beam-update-error");
    message.textContent = t("scheduling");
    try {
      await request("/api/appliance/update/upload", { method: "POST", body: form });
      message.textContent = t("restart");
      setTimeout(() => refresh(card), 12000);
    } catch (error) {
      message.classList.add("beam-update-error");
      message.textContent = error.message || t("failed");
    }
  }

  function install() {
    const card = createCard();
    if (!card) return;
    labels(card);
    role(card, "check").addEventListener("click", () => refresh(card));
    role(card, "install").addEventListener("click", () => installLatest(card));
    role(card, "manual").addEventListener("click", () => role(card, "file").click());
    role(card, "file").addEventListener("change", (event) => uploadPackage(card, event.target.files?.[0]));
    new MutationObserver(() => labels(card)).observe(document.documentElement, { attributes: true, attributeFilter: ["lang"] });
    refresh(card);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", install, { once: true });
  else install();
})();
