# Changelog

All notable Beam-MP-Server-Manager releases are listed here.

Detailed canonical release notes live in `docs/releases/`. GitHub Release text should mirror the corresponding detailed note when a release is published.

## Unreleased

### v0.11.0

Source implementation now includes:

- VMware Edition Manager self-update package validation/staging;
- narrowly privileged atomic install, health validation and automatic rollback;
- official GitHub Release discovery and SHA-256-verified one-click update path;
- Settings update UI plus manual `.update.zip` fallback;
- corrected First Run graphical reboot/desktop contract;
- corrected factory reset detachment/poweroff behavior;
- mandatory Linux maintenance-account page and finalization gate in First Run;
- in-app GitHub bug/feature reporting with non-secret diagnostics only;
- structured public GitHub issue forms;
- GitHub Actions cross-platform pytest CI;
- MIT open-source project metadata/contribution/security files;
- architecture guardrails for a future native Windows Edition without forking the core.

Final v0.11.0 release remains blocked on disposable-appliance runtime validation of the maintenance-account fix and the healthy-update / forced-rollback sequence. Do not label this version released before that runtime gate passes.

See `docs/releases/v0.11.0.md` for the detailed draft release record.

## v0.10.0 - 2026-08-17

First formal appliance release candidate with validated BeamMP server management, Web administration, mod/map/vehicle management, Server Live telemetry, appliance onboarding, backups and OVA packaging workflow.

A local-console First Run regression was discovered after the original OVA validation: completion did not reliably reboot into the graphical appliance desktop. The defect was later corrected and runtime-validated on the v0.11 development branch and is not retroactively hidden from the v0.10 history.

A second historical First Run defect was later discovered: the graphical flow contained, but skipped, the Linux maintenance-account page. This is corrected on the v0.11 branch and remains documented as a v0.10 artifact limitation rather than being retroactively hidden.

See `docs/releases/v0.10.0.md` for the detailed release record.

## Earlier development history

Earlier semantic releases have not yet been proven from repository history. Issue #6 tracks the historical inventory and backfill. Development milestones must not be assigned fabricated version numbers merely to make this file look tidy.
