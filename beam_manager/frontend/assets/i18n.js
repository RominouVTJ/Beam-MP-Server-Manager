(() => {
  const supported = new Set(["en", "fr"]);
  const originalText = new WeakMap();
  const originalAttributes = new WeakMap();
  let catalog = {};
  let current = "en";
  let translating = false;

  function lookup(key) {
    return key.split(".").reduce((value, part) => value && value[part], catalog);
  }

  function translateElement(element) {
    if (!(element instanceof Element)) return;
    const key = element.dataset.i18n;
    if (key) {
      const value = lookup(key);
      if (typeof value === "string") element.textContent = value;
    }
    for (const attribute of ["title", "aria-label", "placeholder"]) {
      if (!element.hasAttribute(attribute)) continue;
      let originals = originalAttributes.get(element);
      if (!originals) { originals = {}; originalAttributes.set(element, originals); }
      if (!(attribute in originals)) originals[attribute] = element.getAttribute(attribute);
      const source = originals[attribute];
      if (catalog.phrases?.[source]) element.setAttribute(attribute, catalog.phrases[source]);
    }
    for (const node of element.childNodes) {
      if (node.nodeType !== Node.TEXT_NODE || !node.nodeValue.trim()) continue;
      if (!originalText.has(node)) originalText.set(node, node.nodeValue);
      const source = originalText.get(node);
      const trimmed = source.trim();
      if (catalog.phrases?.[trimmed]) node.nodeValue = source.replace(trimmed, catalog.phrases[trimmed]);
    }
  }

  function translate(root = document.body) {
    if (!root) return;
    translating = true;
    if (root instanceof Element) translateElement(root);
    root.querySelectorAll?.("*").forEach(translateElement);
    translating = false;
  }

  async function load(language) {
    const selected = supported.has(language) ? language : "en";
    try {
      const response = await fetch(`/assets/i18n-${selected}.json`, { cache: "no-store" });
      if (!response.ok) throw new Error("catalog unavailable");
      return [selected, await response.json()];
    } catch (error) {
      if (selected === "en") throw error;
      const response = await fetch("/assets/i18n-en.json", { cache: "no-store" });
      if (!response.ok) throw error;
      return ["en", await response.json()];
    }
  }

  async function setLanguage(language) {
    [current, catalog] = await load(language);
    document.documentElement.lang = current;
    try { localStorage.setItem("beam-server-manager.language", current); } catch {}
    translate();
    return current;
  }

  window.BeamI18n = { setLanguage, translate, t: (key) => lookup(key) || key, get language() { return current; } };
  document.addEventListener("DOMContentLoaded", async () => {
    let preferred = "en";
    try { preferred = localStorage.getItem("beam-server-manager.language") || "en"; } catch {}
    window.BeamI18n.ready = setLanguage(preferred).catch(() => { document.documentElement.lang = "en"; });
    await window.BeamI18n.ready;
    new MutationObserver((changes) => {
      if (translating) return;
      for (const change of changes) for (const node of change.addedNodes) if (node instanceof Element) translate(node);
    }).observe(document.body, { childList: true, subtree: true });
  });
})();

// Load optional Live-page behavior only after the core deferred scripts have
// finished their DOMContentLoaded initialization. Keeping this isolated avoids
// widening the polling fix in app.js while Gate 6B is still under runtime test.
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    if (document.querySelector('script[data-beam-live-extension]')) return;
    const script = document.createElement("script");
    script.src = "/assets/live-extension.js";
    script.async = false;
    script.dataset.beamLiveExtension = "1";
    document.head.append(script);
  }, 0);
});

// Official BeamNG images are intentionally not stored in Git. A local import
// helper copies the user's own level previews to the appliance and this small
// extension attaches them to official map cards when present.
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    if (document.querySelector('script[data-beam-map-thumbnails]')) return;
    const script = document.createElement("script");
    script.src = "/assets/map-thumbnails.js";
    script.async = false;
    script.dataset.beamMapThumbnails = "1";
    document.head.append(script);
  }, 0);
});

// Public issue reporting is intentionally implemented as a browser handoff to
// GitHub. No GitHub token or repository write credential is stored in the
// appliance; the user reviews and submits the pre-filled issue on GitHub.
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    if (document.querySelector('script[data-beam-feedback-extension]')) return;
    const script = document.createElement("script");
    script.src = "/assets/feedback-extension.js";
    script.async = false;
    script.dataset.beamFeedbackExtension = "1";
    document.head.append(script);
  }, 0);
});

// The v0.11 Manager update UI is also an optional extension so the proven
// v0.10 page structure stays stable. Non-appliance editions simply report the
// feature unsupported through the shared API.
document.addEventListener("DOMContentLoaded", () => {
  setTimeout(() => {
    if (document.querySelector('script[data-beam-update-extension]')) return;
    const script = document.createElement("script");
    script.src = "/assets/update-extension.js";
    script.async = false;
    script.dataset.beamUpdateExtension = "1";
    document.head.append(script);
  }, 0);
});
