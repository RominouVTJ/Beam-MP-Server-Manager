(() => {
  const baseRenderLive = renderLive;
  const radarHistory = new Map();
  const MAX_TRAIL_POINTS = 90;
  let controlBusy = false;
  let activeRadarMap = null;

  function words() {
    const fr = (document.documentElement.lang || "fr").toLowerCase().startsWith("fr");
    return fr ? {
      broadcastTitle: "Message serveur",
      broadcastPlaceholder: "Message visible par tous les joueurs…",
      send: "Envoyer à tous",
      sent: "Commande envoyée",
      queued: "BeamMP va l'appliquer dans quelques instants.",
      kick: "Expulser",
      kickReason: "Raison de l'expulsion (facultative)",
      removeVehicle: "Retirer véhicule",
      removeConfirm: "Retirer ce véhicule de la session ?",
      radar: "Radar local · échelle automatique · trace des déplacements. Fond de carte réel non configuré.",
    } : {
      broadcastTitle: "Server message",
      broadcastPlaceholder: "Message visible to every player…",
      send: "Send to everyone",
      sent: "Command sent",
      queued: "BeamMP will apply it within a moment.",
      kick: "Kick",
      kickReason: "Kick reason (optional)",
      removeVehicle: "Remove vehicle",
      removeConfirm: "Remove this vehicle from the session?",
      radar: "Local radar · automatic scale · movement trail. Real map background is not configured.",
    };
  }

  function installStyles() {
    if (document.querySelector("style[data-live-extension]")) return;
    const style = document.createElement("style");
    style.dataset.liveExtension = "1";
    style.textContent = `
      .live-control-panel{margin-bottom:16px;padding:16px 18px}
      .live-broadcast-form{display:flex;align-items:end;gap:10px;flex-wrap:wrap}
      .live-broadcast-form .field{flex:1 1 360px;margin:0}
      .live-player-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
      .live-player-actions button{min-height:34px;padding:7px 11px;border-radius:9px}
      .live-player-actions .danger-button{border:1px solid rgba(190,45,45,.35);background:rgba(190,45,45,.08);color:inherit;font-weight:700;cursor:pointer}
      .live-player-actions .danger-button:hover{background:rgba(190,45,45,.14)}
      .live-player-actions button:disabled,.live-broadcast-form button:disabled{opacity:.55;cursor:not-allowed}
      .player-card.live-managed{align-items:stretch}
      @media(max-width:720px){.live-broadcast-form{display:block}.live-broadcast-form button{width:100%;margin-top:8px}}
    `;
    document.head.append(style);
  }

  async function sendControl(payload) {
    if (controlBusy) return;
    controlBusy = true;
    document.querySelectorAll("[data-live-control]").forEach((button) => { button.disabled = true; });
    try {
      await api("/api/live/control", { method: "POST", body: JSON.stringify(payload) });
      const copy = words();
      toast(copy.sent, copy.queued);
    } catch (error) {
      toast(t("errors.request_failed"), error.message || "", "error");
    } finally {
      controlBusy = false;
      document.querySelectorAll("[data-live-control]").forEach((button) => { button.disabled = false; });
    }
  }

  function ensureBroadcastPanel() {
    const page = document.querySelector("#page-live");
    if (!page || document.querySelector("#live-control-panel")) return;
    const layout = page.querySelector(".live-layout");
    if (!layout) return;
    const panel = document.createElement("article");
    panel.id = "live-control-panel";
    panel.className = "core-panel live-control-panel";
    panel.innerHTML = `
      <form class="live-broadcast-form" id="live-broadcast-form">
        <label class="field"><span></span><input id="live-broadcast-message" maxlength="500" autocomplete="off"></label>
        <button class="primary-button" type="submit" data-live-control></button>
      </form>
    `;
    layout.before(panel);
    const form = panel.querySelector("form");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const input = panel.querySelector("#live-broadcast-message");
      const message = input.value.trim();
      if (!message) return;
      await sendControl({ action: "say", message });
      input.value = "";
    });
    refreshBroadcastLabels();
  }

  function refreshBroadcastLabels() {
    const panel = document.querySelector("#live-control-panel");
    if (!panel) return;
    const copy = words();
    panel.querySelector(".field span").textContent = copy.broadcastTitle;
    panel.querySelector("input").placeholder = copy.broadcastPlaceholder;
    panel.querySelector("button").textContent = copy.send;
    panel.hidden = state.user?.role !== "admin";
  }

  function decoratePlayerCards(players) {
    const host = document.querySelector("#live-player-cards");
    if (!host || state.user?.role !== "admin") return;
    const cards = [...host.querySelectorAll(".player-card")];
    cards.forEach((card, index) => {
      const player = players[index];
      if (!player || card.querySelector(".live-player-actions")) return;
      card.classList.add("live-managed");
      const actions = document.createElement("div");
      actions.className = "live-player-actions";
      for (const vehicle of player.vehicles || []) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary-button";
        button.dataset.liveControl = "remove-vehicle";
        button.textContent = (player.vehicles || []).length > 1 ? `${words().removeVehicle} #${vehicle.id}` : words().removeVehicle;
        button.addEventListener("click", () => {
          if (!confirm(words().removeConfirm)) return;
          sendControl({ action: "remove_vehicle", player_id: player.id, vehicle_id: vehicle.id });
        });
        actions.append(button);
      }
      const kick = document.createElement("button");
      kick.type = "button";
      kick.className = "danger-button";
      kick.dataset.liveControl = "kick";
      kick.textContent = words().kick;
      kick.addEventListener("click", () => {
        const reason = prompt(words().kickReason, "");
        if (reason === null) return;
        sendControl({ action: "kick", player_id: player.id, reason: reason.trim() || null });
      });
      actions.append(kick);
      card.append(actions);
    });
  }

  function keyFor(player, vehicle) {
    return `${player.id}:${vehicle.id}`;
  }

  function updateHistory(players) {
    const active = new Set();
    for (const player of players) {
      for (const vehicle of player.vehicles || []) {
        if (!Array.isArray(vehicle.position) || vehicle.position.length < 2) continue;
        const key = keyFor(player, vehicle);
        active.add(key);
        const point = [Number(vehicle.position[0]), Number(vehicle.position[1])];
        if (!Number.isFinite(point[0]) || !Number.isFinite(point[1])) continue;
        const history = radarHistory.get(key) || [];
        const previous = history.at(-1);
        if (!previous || Math.hypot(point[0] - previous[0], point[1] - previous[1]) >= 0.5) {
          history.push(point);
          while (history.length > MAX_TRAIL_POINTS) history.shift();
        }
        radarHistory.set(key, history);
      }
    }
    for (const key of radarHistory.keys()) if (!active.has(key)) radarHistory.delete(key);
  }

  function radarBounds(located) {
    const points = [];
    for (const { player, vehicle } of located) {
      const key = keyFor(player, vehicle);
      points.push(...(radarHistory.get(key) || []));
      points.push([Number(vehicle.position[0]), Number(vehicle.position[1])]);
    }
    if (!points.length) return null;
    let minX = Math.min(...points.map((point) => point[0]));
    let maxX = Math.max(...points.map((point) => point[0]));
    let minY = Math.min(...points.map((point) => point[1]));
    let maxY = Math.max(...points.map((point) => point[1]));
    const minimumSpan = 500;
    let spanX = Math.max(minimumSpan, maxX - minX);
    let spanY = Math.max(minimumSpan, maxY - minY);
    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;
    spanX *= 1.18;
    spanY *= 1.18;
    minX = centerX - spanX / 2; maxX = centerX + spanX / 2;
    minY = centerY - spanY / 2; maxY = centerY + spanY / 2;
    return { minX, maxX, minY, maxY };
  }

  function canvasSetup() {
    const canvas = document.querySelector("#radar-canvas");
    if (!canvas) return null;
    const box = canvas.getBoundingClientRect();
    canvas.width = Math.max(300, box.width * devicePixelRatio);
    canvas.height = Math.max(240, box.height * devicePixelRatio);
    return { canvas, ctx: canvas.getContext("2d"), w: canvas.width, h: canvas.height };
  }

  function drawMarker(ctx, player, vehicle, x, y, dark) {
    ctx.fillStyle = "#22c55e";
    ctx.beginPath(); ctx.arc(x, y, 7 * devicePixelRatio, 0, Math.PI * 2); ctx.fill();
    ctx.lineWidth = 3 * devicePixelRatio; ctx.strokeStyle = "rgba(255,255,255,.92)"; ctx.stroke();
    ctx.fillStyle = dark ? "#f2f6f3" : "#152019";
    ctx.font = `${12 * devicePixelRatio}px sans-serif`;
    const speed = Math.round(vehicle.speed_kmh || 0);
    ctx.fillText(`${player.name} · ${speed} km/h`, x + 12 * devicePixelRatio, y - 10 * devicePixelRatio);
  }

  function drawFallbackRadar(players) {
    const image = document.querySelector("#minimap-image");
    if (image) image.hidden = true;
    updateHistory(players);
    const located = players.flatMap((player) => (player.vehicles || [])
      .filter((vehicle) => Array.isArray(vehicle.position) && vehicle.position.length >= 2)
      .map((vehicle) => ({ player, vehicle })));
    const setup = canvasSetup();
    if (!setup) return;
    const { ctx, w, h } = setup;
    const dark = document.documentElement.dataset.theme === "dark";
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = dark ? "#101820" : "#edf2ee";
    ctx.fillRect(0, 0, w, h);

    const bounds = radarBounds(located);
    const cx = w / 2, cy = h / 2;
    ctx.lineWidth = devicePixelRatio;
    ctx.strokeStyle = dark ? "rgba(170,200,185,.14)" : "rgba(49,94,70,.15)";
    for (const radius of [0.18, 0.34, 0.50, 0.66]) {
      ctx.beginPath();
      ctx.arc(cx, cy, Math.min(w, h) * radius, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, cy); ctx.lineTo(w, cy); ctx.stroke();

    const project = (point) => {
      if (!bounds) return [cx, cy];
      return [
        ((point[0] - bounds.minX) / (bounds.maxX - bounds.minX)) * w,
        (1 - ((point[1] - bounds.minY) / (bounds.maxY - bounds.minY))) * h,
      ];
    };

    for (const { player, vehicle } of located) {
      const history = radarHistory.get(keyFor(player, vehicle)) || [];
      if (history.length > 1) {
        ctx.beginPath();
        history.forEach((point, index) => {
          const [x, y] = project(point);
          if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.lineWidth = 2 * devicePixelRatio;
        ctx.strokeStyle = dark ? "rgba(46,204,113,.45)" : "rgba(22,163,74,.42)";
        ctx.stroke();
      }
      const [x, y] = project([vehicle.position[0], vehicle.position[1]]);
      drawMarker(ctx, player, vehicle, x, y, dark);
    }
    setText("#live-map-help", words().radar);
  }

  function drawCalibratedMap(players, calibration) {
    const image = document.querySelector("#minimap-image");
    if (!image) return;
    const url = `/api/minimap/images/${encodeURIComponent(calibration.map_id)}`;
    if (image.dataset.source !== url) {
      image.dataset.source = url;
      image.onload = () => renderRadar(state.live?.players || []);
      image.src = url;
    }
    image.hidden = true;

    const setup = canvasSetup();
    if (!setup) return;
    const { ctx, w, h } = setup;
    const dark = document.documentElement.dataset.theme === "dark";
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = dark ? "#101820" : "#edf2ee";
    ctx.fillRect(0, 0, w, h);
    if (!image.complete || !image.naturalWidth || !image.naturalHeight) {
      setText("#live-map-help", t("live.map_ready"));
      return;
    }

    const scale = Math.min(w / image.naturalWidth, h / image.naturalHeight);
    const drawW = image.naturalWidth * scale;
    const drawH = image.naturalHeight * scale;
    const left = (w - drawW) / 2;
    const top = (h - drawH) / 2;
    ctx.drawImage(image, left, top, drawW, drawH);

    updateHistory(players);
    const located = players.flatMap((player) => (player.vehicles || [])
      .filter((vehicle) => Array.isArray(vehicle.position) && vehicle.position.length >= 2)
      .map((vehicle) => ({ player, vehicle })));
    const dx = Number(calibration.world_max_x) - Number(calibration.world_min_x);
    const dy = Number(calibration.world_max_y) - Number(calibration.world_min_y);
    if (!dx || !dy) return;

    const project = (point) => {
      let nx = (Number(point[0]) - Number(calibration.world_min_x)) / dx;
      let ny = (Number(point[1]) - Number(calibration.world_min_y)) / dy;
      if (calibration.invert_x) nx = 1 - nx;
      if (calibration.invert_y) ny = 1 - ny;
      const angle = Number(calibration.rotation || 0) * Math.PI / 180;
      if (angle) {
        const px = nx - 0.5, py = ny - 0.5;
        nx = 0.5 + px * Math.cos(angle) - py * Math.sin(angle);
        ny = 0.5 + px * Math.sin(angle) + py * Math.cos(angle);
      }
      if (nx < 0 || nx > 1 || ny < 0 || ny > 1) return null;
      return [left + nx * drawW, top + ny * drawH];
    };

    for (const { player, vehicle } of located) {
      const history = radarHistory.get(keyFor(player, vehicle)) || [];
      const projectedHistory = history.map(project).filter(Boolean);
      if (projectedHistory.length > 1) {
        ctx.beginPath();
        projectedHistory.forEach(([x, y], index) => {
          if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        });
        ctx.lineWidth = 2 * devicePixelRatio;
        ctx.strokeStyle = "rgba(34,197,94,.55)";
        ctx.stroke();
      }
      const position = project(vehicle.position);
      if (!position) continue;
      drawMarker(ctx, player, vehicle, position[0], position[1], dark);
    }
    setText("#live-map-help", t("live.map_ready"));
  }

  renderRadar = function enhancedRenderRadar(players) {
    const calibration = state.live?.map_calibration || null;
    const mapKey = calibration?.map_id || "fallback";
    if (activeRadarMap !== mapKey) {
      activeRadarMap = mapKey;
      radarHistory.clear();
    }
    if (calibration) {
      drawCalibratedMap(players || [], calibration);
      return;
    }
    drawFallbackRadar(players || []);
  };

  renderLive = function enhancedRenderLive() {
    baseRenderLive();
    ensureBroadcastPanel();
    refreshBroadcastLabels();
    decoratePlayerCards(state.live?.players || []);
  };

  installStyles();
  ensureBroadcastPanel();
  refreshBroadcastLabels();
  if (state.live) renderLive();
})();
