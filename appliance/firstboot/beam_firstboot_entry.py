#!/usr/bin/env python3
"""Canonical v0.11 First Run entrypoint with the maintenance-account gate.

The historical v0.10 GUI contains the account page and account-creation engine,
but its normal GUI routing skipped that page.  This entrypoint keeps the
validated historical implementation and replaces only the routing/finalization
classes required by v0.11.

It is intentionally runnable both from the repository and when installed as
/usr/local/libexec/beam-appliance-firstboot.  The latter must import the
application copy from /opt/beam-manager rather than relying on the current
working directory.
"""
from __future__ import annotations

import sys
from pathlib import Path


APPLICATION_ROOT = Path("/opt/beam-manager")
if APPLICATION_ROOT.is_dir():
    application_root = str(APPLICATION_ROOT)
    if application_root not in sys.path:
        sys.path.insert(0, application_root)

try:
    from appliance.firstboot import beam_firstboot as legacy
except ImportError:  # Repository/direct execution fallback.
    import beam_firstboot as legacy  # type: ignore[no-redef]


_PATCHED = False
_ORIGINAL_ENGINE = legacy.FirstBootEngine
_ORIGINAL_MODEL = legacy.FirstBootGUIModel
_ORIGINAL_GUI = legacy.FirstBootGUI


class V011FirstBootEngine(_ORIGINAL_ENGINE):
    """Historical engine plus a hard maintenance-account finalization gate."""

    def finalize(self, selection) -> None:
        if self.complete():
            return
        progress = self.progress()
        stage = str(progress.get("stage", "new"))
        if legacy.stage_index(stage) < legacy.stage_index("account_created"):
            raise legacy.FirstBootError(
                "Linux maintenance account must be created before First Run can finish"
            )

        recorded = self.selection()
        username = (
            recorded.maintenance_username
            if recorded is not None and recorded.maintenance_username
            else selection.maintenance_username
        )
        try:
            username = legacy.validate_username(username)
        except ValueError as exc:
            raise legacy.FirstBootError(
                "Linux maintenance account is missing from First Run state"
            ) from exc

        exists = self.executor.execute(
            ("/usr/bin/getent", "passwd", username), check=False
        )
        if exists.returncode != 0:
            raise legacy.FirstBootError(
                "Linux maintenance account was not created on the appliance"
            )

        status = self.executor.execute(
            ("/usr/bin/passwd", "--status", username), check=False
        )
        fields = status.stdout.split()
        if status.returncode != 0 or len(fields) < 2 or fields[1] != "P":
            raise legacy.FirstBootError(
                "Linux maintenance account has no usable password"
            )

        selection.maintenance_username = username
        super().finalize(selection)


class V011FirstBootGUIModel(_ORIGINAL_MODEL):
    """Route localization through mandatory Linux account creation."""

    def _resume_page(self) -> str:
        if self.engine.complete():
            return "finalization"
        if legacy.stage_index(self.stage) >= legacy.stage_index("account_created"):
            return "finalization"
        if legacy.stage_index(self.stage) >= legacy.stage_index("localization_applied"):
            return "account"
        if legacy.stage_index(self.stage) >= legacy.stage_index("region_selected"):
            return "keyboard"
        if legacy.stage_index(self.stage) >= legacy.stage_index("language_selected"):
            return "location"
        return "language"

    def confirm_keyboard(self) -> None:
        if not self.selection:
            raise ValueError("Missing selection")
        self.engine.confirm_keyboard(self.selection)
        self.engine.ensure_localization(self.selection)
        self.stage = "localization_applied"
        self.page = "account"


class V011FirstBootGUI(_ORIGINAL_GUI):
    """Historical GUI with the account page inserted into the normal flow."""

    def _header(self, active: int) -> None:
        # v0.10 had four visible steps because the account page was skipped.
        # v0.11 makes the account a real fourth step and finalization the fifth.
        if self.current_page in {"finalization", "complete"} and active == 4:
            active = 5
        super()._header(active)

    def _confirm_keyboard(self) -> None:
        if self._safe(
            "confirm_keyboard_and_localization",
            self.model.confirm_keyboard,
            rerender=self.show_keyboard,
        ):
            self.show_account()

    def show_account(self) -> None:
        # `admin` is a convenient safe default for an appliance.  It remains
        # editable, so owners can choose another valid maintenance identity.
        if self.model.selection and not self.model.selection.maintenance_username:
            self.model.selection.maintenance_username = "admin"
        super().show_account()


def apply_patches() -> None:
    """Switch legacy runtime globals to the isolated v0.11 classes once."""
    global _PATCHED
    if _PATCHED:
        return
    legacy.GUI_WORDS["fr"]["steps"] = (
        "Langue",
        "Localisation",
        "Clavier",
        "Compte maintenance",
        "Finalisation",
    )
    legacy.GUI_WORDS["en"]["steps"] = (
        "Language",
        "Location",
        "Keyboard",
        "Maintenance account",
        "Finalization",
    )
    legacy.FirstBootEngine = V011FirstBootEngine
    legacy.FirstBootGUIModel = V011FirstBootGUIModel
    legacy.FirstBootGUI = V011FirstBootGUI
    _PATCHED = True


def main() -> int:
    apply_patches()
    return legacy.main()


if __name__ == "__main__":
    raise SystemExit(main())
