#!/usr/bin/env python3
from __future__ import annotations

import json
import urllib.request
import webbrowser
from pathlib import Path
import tkinter as tk

CONFIG = Path("/etc/beam-appliance/config.json")
STATUS_URL = "http://127.0.0.1:8765/api/appliance/desktop-status"


def selected_language():
    try:
        value = json.loads(CONFIG.read_text(encoding="utf-8")).get("default_language", "en")
        return value if value in {"fr", "en"} else "en"
    except Exception:
        return "en"


WORDS = {
    "fr": {
        "title": "État de l'appliance",
        "manager": "Manager Web",
        "beammp": "Serveur BeamMP",
        "auth": "AuthKey",
        "users": "Comptes Web",
        "sessions": "Sessions actives",
        "web": "Interface Web",
        "security": "Code de sécurité",
        "keep": "Conservez ce code. Il est requis pour la récupération administrateur et les modifications sensibles.",
        "open": "Ouvrir le Manager",
        "copy": "Copier",
        "show": "Afficher",
        "hide": "Masquer",
        "online": "EN LIGNE",
        "offline": "HORS LIGNE",
        "not_configured": "NON CONFIGURÉ",
        "configured": "CONFIGURÉE ✓",
        "missing": "NON RENSEIGNÉE",
        "waiting": "Connexion au Manager…",
    },
    "en": {
        "title": "Appliance status",
        "manager": "Web Manager",
        "beammp": "BeamMP server",
        "auth": "AuthKey",
        "users": "Web accounts",
        "sessions": "Active sessions",
        "web": "Web interface",
        "security": "Security code",
        "keep": "Keep this code. It is required for administrator recovery and sensitive changes.",
        "open": "Open Manager",
        "copy": "Copy",
        "show": "Show",
        "hide": "Hide",
        "online": "ONLINE",
        "offline": "OFFLINE",
        "not_configured": "NOT CONFIGURED",
        "configured": "CONFIGURED ✓",
        "missing": "NOT SET",
        "waiting": "Connecting to Manager…",
    },
}


class ApplianceWindow:
    def __init__(self):
        self.lang = selected_language()
        self.words = WORDS[self.lang]
        self.security_visible = False
        self.root = tk.Tk()
        self.root.title("Beam-MP-Server-Manager")
        self.root.geometry("790x560")
        self.root.minsize(700, 500)
        self.root.configure(bg="#111714")
        self.values = {
            key: tk.StringVar(value="—")
            for key in ("manager", "beammp", "auth", "users", "sessions", "web", "security")
        }
        self._build()
        self._refresh()

    def _label(self, parent, text, size=12, bold=False, fg="#f4f7f5"):
        return tk.Label(
            parent,
            text=text,
            bg="#1a221e",
            fg=fg,
            font=("DejaVu Sans", size, "bold" if bold else "normal"),
        )

    def _build(self):
        tk.Label(
            self.root,
            text="BEAM-MP-SERVER-MANAGER",
            bg="#111714",
            fg="#62d98b",
            font=("DejaVu Sans", 20, "bold"),
        ).pack(anchor="w", padx=30, pady=(26, 4))
        tk.Label(
            self.root,
            text=self.words["title"],
            bg="#111714",
            fg="#f4f7f5",
            font=("DejaVu Sans", 14),
        ).pack(anchor="w", padx=30, pady=(0, 18))

        panel = tk.Frame(self.root, bg="#1a221e", padx=24, pady=22)
        panel.pack(fill="both", expand=True, padx=30, pady=(0, 20))

        for key in ("manager", "beammp", "auth", "users", "sessions", "web"):
            row = tk.Frame(panel, bg="#1a221e")
            row.pack(fill="x", pady=7)
            self._label(row, self.words[key], 12, True).pack(side="left")
            tk.Label(
                row,
                textvariable=self.values[key],
                bg="#1a221e",
                fg="#f4f7f5",
                font=("DejaVu Sans Mono", 12, "bold"),
            ).pack(side="right")

        security = tk.Frame(panel, bg="#1a221e")
        security.pack(fill="x", pady=(18, 5))
        self._label(security, self.words["security"], 12, True).pack(anchor="w")

        self.security_entry = tk.Entry(
            security,
            textvariable=self.values["security"],
            state="readonly",
            readonlybackground="#f4f7f5",
            fg="#111714",
            font=("DejaVu Sans Mono", 16),
            relief="flat",
            show="•",
        )
        self.security_entry.pack(side="left", fill="x", expand=True, ipady=7, pady=(7, 0))

        self.security_toggle = tk.Button(
            security,
            text=self.words["show"],
            command=self._toggle_security,
            bg="#d6ded9",
            fg="#07110b",
            relief="flat",
            padx=12,
            pady=8,
        )
        self.security_toggle.pack(side="left", padx=(10, 0), pady=(7, 0))

        tk.Button(
            security,
            text=self.words["copy"],
            command=self._copy_security,
            bg="#62d98b",
            fg="#07110b",
            relief="flat",
            padx=14,
            pady=8,
        ).pack(side="left", padx=(8, 0), pady=(7, 0))

        self._label(panel, self.words["keep"], 11, True, "#ffcf70").pack(anchor="w", pady=(8, 14))
        tk.Button(
            panel,
            text=self.words["open"],
            command=self._open_web,
            bg="#62d98b",
            fg="#07110b",
            relief="flat",
            padx=18,
            pady=11,
            font=("DejaVu Sans", 12, "bold"),
        ).pack(anchor="e")

    def _toggle_security(self):
        self.security_visible = not self.security_visible
        self.security_entry.configure(show="" if self.security_visible else "•")
        self.security_toggle.configure(text=self.words["hide"] if self.security_visible else self.words["show"])

    def _copy_security(self):
        """Copy exactly like Ctrl+C in the Tk entry, which VMware vmusr bridges correctly."""
        value = self.values["security"].get()
        if not value or value == "—":
            return

        previous_focus = self.root.focus_get()
        previous_show = self.security_entry.cget("show")
        try:
            # VMware's X11 clipboard bridge reacts reliably to the Entry class
            # <<Copy>> binding. Temporarily unmask only inside this synchronous
            # callback so the copied selection is the real code, not bullets.
            self.security_entry.configure(show="")
            self.security_entry.selection_range(0, tk.END)
            self.security_entry.icursor(tk.END)
            self.security_entry.focus_set()
            self.security_entry.event_generate("<<Copy>>", when="now")
        except tk.TclError:
            # Keep a standards-compliant UTF8_STRING fallback for non-X11 Tk.
            self.root.clipboard_clear()
            self.root.clipboard_append(value, type="UTF8_STRING")
        finally:
            self.security_entry.selection_clear()
            self.security_entry.configure(show=previous_show)
            if previous_focus is not None:
                try:
                    previous_focus.focus_set()
                except tk.TclError:
                    pass
            self.root.update_idletasks()

    def _open_web(self):
        value = self.values["web"].get()
        if value and value != "—":
            webbrowser.open(value)

    def _refresh(self):
        try:
            with urllib.request.urlopen(STATUS_URL, timeout=2) as response:
                data = json.load(response)
            self.values["manager"].set(
                self.words["online"] if data.get("manager") == "online" else self.words["offline"]
            )
            state = data.get("beammp")
            self.values["beammp"].set(
                self.words["online"]
                if state == "online"
                else self.words["not_configured"]
                if state == "not_configured"
                else self.words["offline"]
            )
            self.values["auth"].set(
                self.words["configured"] if data.get("authkey_configured") else self.words["missing"]
            )
            self.values["users"].set(str(data.get("users_total", 0)))
            self.values["sessions"].set(str(data.get("active_sessions", 0)))
            self.values["web"].set(data.get("web_url") or "—")
            self.values["security"].set(data.get("security_code") or "—")
        except Exception:
            self.values["manager"].set(self.words["waiting"])
        self.root.after(3000, self._refresh)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    ApplianceWindow().run()
