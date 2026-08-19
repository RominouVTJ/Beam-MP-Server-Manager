# Appliance update package v1

This document defines the v1 self-update contract for Beam-MP-Server-Manager VMware Edition.

The update path is deliberately split into validation, staging and a narrowly privileged installation worker. Package contents are never executed directly from the uploaded ZIP.

## Container

An update is a ZIP archive containing exactly two top-level regular files:

- `manifest.json`
- `payload.tar.gz`

Directories, nested ZIP paths, duplicate entries and additional files are rejected.

## Manifest schema

`manifest.json` is UTF-8 JSON with exactly these fields:

```json
{
  "schema_version": 1,
  "product": "Beam-MP-Server-Manager",
  "version": "0.11.0",
  "min_version": "0.10.0",
  "payload": "payload.tar.gz",
  "payload_sha256": "<64 lowercase hex characters>",
  "payload_size": 123456
}
```

Rules:

- `schema_version` must be `1`;
- `product` must be exactly `Beam-MP-Server-Manager`;
- `version` and `min_version` must be valid PEP 440 versions;
- `payload` must be exactly `payload.tar.gz`;
- `payload_sha256` is the SHA-256 of the exact `payload.tar.gz` member bytes;
- `payload_size` is the exact uncompressed ZIP-member byte length of `payload.tar.gz`.

## Compatibility

Before installation:

- the installed version must be greater than or equal to `min_version`;
- the target `version` must be strictly newer than the installed version;
- the version in the staged `pyproject.toml` must match the manifest target version;
- the installed Python package must report the same target version before it is swapped into service.

Downgrades and same-version reinstalls are rejected by the normal update path.

## Validation and staging boundary

Validation/staging performs all of the following before the privileged installer is called:

1. verify the ZIP itself is a regular file and within the configured size limit;
2. reject duplicate, nested or unexpected ZIP members;
3. parse and validate the exact manifest schema;
4. compare payload byte size with `payload_size`;
5. stream the payload through SHA-256 and compare with `payload_sha256`;
6. apply version compatibility checks;
7. reject absolute paths, parent traversal, backslash/Windows-drive path ambiguity, symlinks, devices and other non-regular tar entries;
8. enforce payload file-count, per-file and expanded-size limits;
9. require the canonical project layout (`pyproject.toml`, `beam_manager/`, `appliance/`);
10. extract into the Manager-owned staging directory only.

## Privileged installation

The Web Manager has no generic passwordless root shell. Its sudo permission is restricted to the named appliance update helper.

The root helper:

1. accepts only a staging tree under `/var/lib/beam-manager/appliance-updates/staged`;
2. freezes/copies that tree into root-owned state before the Web service is stopped;
3. dispatches the destructive phase through a detached transient systemd worker;
4. builds the candidate Python environment as the unprivileged `beammanager` account;
5. verifies the installed candidate package version;
6. stops `beam-manager.service` only after the worker is detached;
7. backs up persistent Manager data;
8. atomically swaps `/opt/beam-manager` to the candidate tree;
9. restarts the Manager;
10. requires both `/api/health` and `/api/appliance/version` to pass;
11. automatically restores the previous application tree and Manager data if validation fails.

The helper does not modify BeamMP `ServerConfig.toml`, AuthKey, mods or BeamMP backups.

The last machine-readable result is written to:

```text
/var/lib/beam-appliance/update/last-result.json
```

Only non-secret fields are exposed by the Web API: status, target version, previous version, message and timestamp.

## Web API / UI

Authenticated administrators can:

- read installed/available version and last update result;
- install the latest validated update asset from the official GitHub Release;
- upload a local `.update.zip` as a manual fallback.

The normal UI does not require shell/SSH access.

## Official GitHub release discovery

The Manager expects the exact release asset name:

```text
Beam-MP-Server-Manager-vX.Y.Z.update.zip
```

A release is offered for one-click installation only when:

- its version is newer than the installed Manager;
- the exact expected asset exists;
- GitHub exposes a SHA-256 digest for that asset;
- the asset URL and any final redirect stay on the approved GitHub download hosts.

The outer downloaded ZIP SHA-256 is checked against the GitHub asset digest before the inner package/manifest/payload validation is performed.

No GitHub Personal Access Token is stored in the appliance.

## Publishable package builder

The canonical release builder is:

```text
appliance/update/build-update-package.py
```

It creates:

```text
Beam-MP-Server-Manager-vX.Y.Z.update.zip
Beam-MP-Server-Manager-vX.Y.Z.update.zip.sha256.txt
```

The release builder and the synthetic rollback-test builder are intentionally separate. Files containing the forced rollback test marker must never be published.

## v0.10 bootstrap limitation

The historical v0.10.0 OVA predates this updater and therefore cannot discover/install v0.11 by itself. v0.10 users need the normal v0.11 OVA replacement path (or an explicit maintenance/bootstrap procedure intended for development/testing).

The first release that can provide the normal in-product self-update experience for subsequent versions is v0.11.0.

## Integrity versus authenticity

SHA-256 verifies integrity. For official one-click updates, the expected asset is additionally discovered from the project's GitHub Release and its GitHub-published asset digest is required.

A future package schema may add a project-controlled detached signature without weakening the v1 validation rules.

## Edition boundary

This root/systemd update helper is specific to VMware/Linux Edition. A future native Windows Edition will use its own Windows-safe installer/update mechanism while sharing version/release policy and Web UX where practical.
