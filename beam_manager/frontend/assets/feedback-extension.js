(() => {
  "use strict";

  const REPOSITORY = "RominouVTJ/Beam-MP-Server-Manager";
  const ISSUE_URL = `https://github.com/${REPOSITORY}/issues/new`;
  const words = {
    fr: {
      button: "Bug / suggestion",
      title: "Signaler un bug ou proposer une fonction",
      type: "Type",
      bug: "Bug",
      feature: "Nouvelle fonction",
      subject: "Titre",
      description: "Description",
      steps: "Étapes pour reproduire",
      expected: "Résultat attendu",
      actual: "Résultat obtenu",
      impact: "Impact / importance",
      technical: "Joindre les informations techniques non sensibles",
      cancel: "Annuler",
      submit: "Continuer sur GitHub",
      required: "Le titre et la description sont obligatoires.",
      privacy: "Aucun AuthKey, mot de passe, code de sécurité, cookie ou adresse IP n'est joint automatiquement.",
    },
    en: {
      button: "Bug / feature",
      title: "Report a bug or request a feature",
      type: "Type",
      bug: "Bug",
      feature: "Feature request",
      subject: "Title",
      description: "Description",
      steps: "Steps to reproduce",
      expected: "Expected result",
      actual: "Actual result",
      impact: "Impact / importance",
      technical: "Attach non-sensitive technical information",
      cancel: "Cancel",
      submit: "Continue on GitHub",
      required: "Title and description are required.",
      privacy: "No AuthKey, password, security code, cookie or IP address is attached automatically.",
    },
  };

  const language = () => (document.documentElement.lang === "fr" ? "fr" : "en");
  const t = (key) => words[language()][key];

  function style() {
    if (document.getElementById("beam-feedback-style")) return;
    const node = document.createElement("style");
    node.id = "beam-feedback-style";
    node.textContent = `
      .beam-feedback-button{width:100%;margin:0 0 10px;border:1px solid var(--line,#39434c);background:transparent;color:inherit;border-radius:9px;padding:9px 10px;cursor:pointer;text-align:left}
      .beam-feedback-overlay{position:fixed;inset:0;z-index:5000;background:rgba(0,0,0,.68);display:grid;place-items:center;padding:24px}
      .beam-feedback-modal{width:min(720px,100%);max-height:90vh;overflow:auto;background:var(--panel,#121920);color:inherit;border:1px solid var(--line,#39434c);border-radius:14px;padding:22px;box-shadow:0 20px 70px rgba(0,0,0,.45)}
      .beam-feedback-modal h2{margin:0 0 18px}.beam-feedback-modal label{display:grid;gap:6px;margin:12px 0}.beam-feedback-modal input,.beam-feedback-modal select,.beam-feedback-modal textarea{width:100%;box-sizing:border-box;background:var(--input,#0c1217);color:inherit;border:1px solid var(--line,#39434c);border-radius:8px;padding:10px}.beam-feedback-modal textarea{min-height:90px;resize:vertical}.beam-feedback-actions{display:flex;justify-content:flex-end;gap:10px;margin-top:18px}.beam-feedback-error{color:#ff7b7b;min-height:1.3em}.beam-feedback-privacy{opacity:.75;font-size:.9em}.beam-feedback-check{display:flex!important;grid-template-columns:auto 1fr!important;align-items:center;gap:9px!important}.beam-feedback-check input{width:auto!important}
    `;
    document.head.append(node);
  }

  function field(label, element) {
    const wrapper = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = label;
    wrapper.append(caption, element);
    return wrapper;
  }

  async function technicalInfo() {
    const result = [];
    try {
      const response = await fetch("/api/appliance/version", { cache: "no-store" });
      if (response.ok) {
        const data = await response.json();
        result.push(`Manager: ${String(data.version || "unknown")}`);
      }
    } catch {}
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (response.ok) {
        const data = await response.json();
        for (const key of ["manager", "database", "filesystem"]) {
          if (key in data) result.push(`${key}: ${String(data[key])}`);
        }
      }
    } catch {}
    result.push(`UI language: ${language()}`);
    return result.join("\n");
  }

  function openModal() {
    style();
    const overlay = document.createElement("div");
    overlay.className = "beam-feedback-overlay";
    const modal = document.createElement("div");
    modal.className = "beam-feedback-modal";
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");

    const heading = document.createElement("h2");
    heading.textContent = t("title");
    const type = document.createElement("select");
    type.innerHTML = `<option value="bug">${t("bug")}</option><option value="enhancement">${t("feature")}</option>`;
    const subject = document.createElement("input");
    subject.maxLength = 180;
    const description = document.createElement("textarea");
    description.maxLength = 3000;
    const steps = document.createElement("textarea");
    steps.maxLength = 1800;
    const expected = document.createElement("textarea");
    expected.maxLength = 1200;
    const actual = document.createElement("textarea");
    actual.maxLength = 1200;
    const impact = document.createElement("textarea");
    impact.maxLength = 800;

    const bugFields = document.createElement("div");
    bugFields.append(
      field(t("steps"), steps),
      field(t("expected"), expected),
      field(t("actual"), actual),
    );
    type.addEventListener("change", () => {
      bugFields.hidden = type.value !== "bug";
    });

    const includeTechnical = document.createElement("input");
    includeTechnical.type = "checkbox";
    includeTechnical.checked = true;
    const check = document.createElement("label");
    check.className = "beam-feedback-check";
    const checkText = document.createElement("span");
    checkText.textContent = t("technical");
    check.append(includeTechnical, checkText);

    const privacy = document.createElement("p");
    privacy.className = "beam-feedback-privacy";
    privacy.textContent = t("privacy");
    const error = document.createElement("div");
    error.className = "beam-feedback-error";

    const actions = document.createElement("div");
    actions.className = "beam-feedback-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "secondary-button";
    cancel.textContent = t("cancel");
    const submit = document.createElement("button");
    submit.type = "button";
    submit.className = "primary-button";
    submit.textContent = t("submit");
    actions.append(cancel, submit);

    modal.append(
      heading,
      field(t("type"), type),
      field(t("subject"), subject),
      field(t("description"), description),
      bugFields,
      field(t("impact"), impact),
      check,
      privacy,
      error,
      actions,
    );
    overlay.append(modal);
    document.body.append(overlay);
    subject.focus();

    const close = () => overlay.remove();
    cancel.addEventListener("click", close);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) close();
    });
    document.addEventListener("keydown", function escape(event) {
      if (event.key !== "Escape" || !document.body.contains(overlay)) return;
      document.removeEventListener("keydown", escape);
      close();
    });

    submit.addEventListener("click", async () => {
      const title = subject.value.trim();
      const detail = description.value.trim();
      if (!title || !detail) {
        error.textContent = t("required");
        return;
      }
      submit.disabled = true;
      error.textContent = "";
      const isBug = type.value === "bug";
      const sections = [
        `## ${isBug ? t("bug") : t("feature")}`,
        detail,
      ];
      if (isBug && steps.value.trim()) sections.push(`## ${t("steps")}\n${steps.value.trim()}`);
      if (isBug && expected.value.trim()) sections.push(`## ${t("expected")}\n${expected.value.trim()}`);
      if (isBug && actual.value.trim()) sections.push(`## ${t("actual")}\n${actual.value.trim()}`);
      if (impact.value.trim()) sections.push(`## ${t("impact")}\n${impact.value.trim()}`);
      if (includeTechnical.checked) {
        const info = await technicalInfo();
        if (info) sections.push(`## Technical information\n\`\`\`text\n${info}\n\`\`\``);
      }
      sections.push("_Created from Beam-MP-Server-Manager Web UI._");
      const params = new URLSearchParams({
        title,
        body: sections.join("\n\n"),
        labels: isBug ? "bug" : "enhancement",
      });
      const link = document.createElement("a");
      link.href = `${ISSUE_URL}?${params.toString()}`;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      document.body.append(link);
      link.click();
      link.remove();
      submit.disabled = false;
    });
  }

  function installButton() {
    if (document.getElementById("beam-feedback-button")) return;
    const footer = document.querySelector(".sidebar-foot");
    if (!footer) return;
    const button = document.createElement("button");
    button.id = "beam-feedback-button";
    button.type = "button";
    button.className = "beam-feedback-button";
    const refresh = () => { button.textContent = `🐞 ${t("button")}`; };
    refresh();
    button.addEventListener("click", openModal);
    footer.prepend(button);
    new MutationObserver(refresh).observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["lang"],
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", installButton, { once: true });
  } else {
    installButton();
  }
})();
