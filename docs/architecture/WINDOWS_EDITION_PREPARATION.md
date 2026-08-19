# Windows Edition preparation

Status: architecture direction for v0.11 and later

## Product rule

Beam-MP-Server-Manager remains one project with one Web UI and one core codebase.

Editions are deployment/runtime variants, not forks:

- **Beam-MP-Server-Manager VMware Edition**: Debian appliance + BeamMP Linux + Web Manager.
- **Beam-MP-Server-Manager Windows Edition**: native Windows installation + BeamMP Server Windows + the same Web Manager.

The Windows Edition must not require Python, Git, Docker, SSH or a terminal for normal installation or use.

## Target architecture

```text
Web UI
  |
  v
FastAPI / Beam-MP-Server-Manager core
  |
  +-- LocalLinuxBackend
  |     +-- systemd / POSIX filesystem / BeamMP Server Linux
  |
  +-- LocalWindowsBackend   [future]
        +-- Windows process/service host / BeamMP-Server.exe
```

The current `beam_manager.backend.ServerBackend` protocol and `LocalLinuxBackend` are the starting point. The Windows work must extend that boundary rather than create a separate Manager implementation.

## v0.11 rule

v0.11 remains a VMware/Linux release. Windows runtime implementation is **not** a v0.11 goal.

While fixing v0.11:

1. do not introduce new Linux commands, Linux paths or systemd assumptions into business logic when an OS/backend boundary can own them;
2. preserve all existing Linux/VMware behavior and tests;
3. prefer small type/interface cleanups over broad directory moves;
4. document unavoidable Linux-only appliance code rather than forcing artificial cross-platform abstractions;
5. defer the full `LocalWindowsBackend` and Windows installer to a later development cycle after v0.11 stabilization.

## Current coupling audit

### Already correctly separated or mostly portable

| Area | Current state | Decision |
|---|---|---|
| Web UI / FastAPI routes | Mostly independent of OS and already obtains a `ServerBackend` dependency for server operations | Keep common |
| `ServerBackend` protocol | Defines service operations, filesystem access, disk free space, BeamMP version and file moves | Keep and evolve as common backend contract |
| `LocalLinuxBackend` | Owns local `systemctl`, POSIX filesystem and local BeamMP binary execution | Remain Linux-specific |
| SSH backend | Provides the same general server/file contract for remote Linux management | Keep as existing compatibility backend |
| Mod archive parsing/classification | ZIP/JSON/image logic is platform-independent | Keep common |
| Web authentication, users, sessions, operations, telemetry models | No reason to fork by OS | Keep common |
| application data directory concept | Already has platform-aware defaults in `config.py` | Keep common, refine Windows target later |

### Coupling that blocks a native Windows backend

| Dependency | Current location / example | Classification | Direction |
|---|---|---|---|
| `systemctl` + `sudo` | `LocalLinuxBackend.execute_service_command()` | Linux backend only | Keep inside `LocalLinuxBackend`; Windows backend will implement start/stop/restart/status using native process/service control |
| POSIX-only local filesystem adapter | `LocalSFTP`, `statvfs`, `chmod`, `chown`, POSIX modes | Linux backend only | Do not expose POSIX ownership/modes as core requirements; future Windows backend implements equivalent safe filesystem operations without POSIX permissions |
| `/opt/beammp` path defaults | `Settings` and several services | Mixed / currently over-coupled | Move path layout behind platform/backend-aware configuration; core must not require `/opt` |
| path validator requiring `/opt/beammp` | `Settings.remote_paths_are_fixed_under_beammp()` | Blocking | Replace later with backend-aware root validation so Windows paths such as `C:\ProgramData\Beam-MP-Server-Manager\BeamMP` can be valid |
| `systemd_service` setting/validator | `Settings` | Linux-only concept leaking into common settings | Move toward generic server runtime/service identity; keep compatibility during v0.11 |
| service classes typed as `SSHClient` | e.g. inventory/backups/BeamMP updater despite using backend-like methods | Low-risk abstraction debt | Prefer `ServerBackend` protocol typing where behavior already fits; no runtime change required |
| BeamMP platform detection via `/etc/os-release` + ELF header | `BeamMPUpdateService.platform()` | Linux-specific | Backend capability should report platform/architecture and validated asset identity |
| BeamMP binary path hard-coded `/opt/beammp/BeamMP-Server` | `BeamMPUpdateService` | Blocking | Obtain executable path from backend/platform layout |
| telemetry health paths hard-coded under `/opt/beammp` | BeamMP update health validation and some routes | Blocking | Derive paths from configured/backend layout |
| POSIX path construction (`PurePosixPath`, `/`) for host filesystem | inventory/backups | Mixed | Archive-internal BeamNG paths remain POSIX by definition; host filesystem paths should become backend path operations |
| POSIX permission modes (`0o2775`, `0o2750`, `chmod`) | inventory/backups/backend | Linux-specific behavior exposed to common services | Backend should translate/common services should request semantic directory purpose rather than rely on setgid bits in future |
| appliance First Run Linux account / locale / system configuration | `appliance/firstboot` | VMware/Linux packaging only | Keep Linux-specific; Windows Edition gets its own installer/First Run host integration while sharing Web onboarding concepts |
| factory reset, identity regeneration, LightDM/XFCE, SSH, GRUB, systemd units | `appliance/` | VMware/Linux packaging only | Do not force into common core; place future Windows packaging beside it |
| UFW/firewall provisioning | Linux appliance scripts | Linux packaging only | Future Windows packaging uses Windows Firewall APIs/commands |
| appliance self-update root helper | `appliance/update` | VMware/Linux edition only | Windows Edition will need a Windows-safe updater/installer strategy while sharing release/version policy |
| disk free calculation | backend contract already exists | Backend-specific implementation | Windows backend implements via Windows/Python filesystem APIs |
| system reboot/poweroff | appliance helpers | Edition-specific | Keep outside business core; expose only if a future common UI action needs a backend capability |
| logs | BeamMP file logs + systemd/journal environment | Mixed | Core should consume BeamMP/runtime log streams through backend; journald stays Linux-only |

## Required future backend capabilities

The existing `ServerBackend` contract is useful but too SFTP/POSIX-shaped for a clean Windows implementation. Do not rewrite it during v0.11. Evolve it incrementally toward semantic capabilities such as:

```text
server_status()
start_server()
stop_server()
restart_server()
server_pid()
read_server_logs()
beammp_platform()
beammp_executable_path()
read_file()/write_file_atomic()
list_files()/move_file()
disk_free_bytes()
```

For Windows, process ownership is expected to be:

- Beam-MP-Server-Manager runs as a Windows service;
- the Manager service directly launches and supervises `BeamMP-Server.exe`;
- BeamMP does not become a second independent Windows service unless runtime validation proves this is necessary.

The future backend therefore needs to own PID tracking, stdout/stderr capture, crash detection and restart policy.

## Validated BeamMP version policy

Automatic BeamMP updates must not blindly track `latest`.

Future common release metadata should distinguish:

- installed BeamMP version;
- available upstream BeamMP version;
- **validated/recommended BeamMP version for this Manager release**.

The Manager may download an official BeamMP binary, verify SHA-256 and install it only when that asset/version is approved by Beam-MP-Server-Manager compatibility policy.

This applies to Linux and Windows editions.

## Windows filesystem target

Program files:

```text
C:\Program Files\Beam-MP-Server-Manager\
```

Persistent/runtime data:

```text
C:\ProgramData\Beam-MP-Server-Manager\
```

Program and data must remain strictly separated so application updates do not delete configuration, database, AuthKey, mods, maps, vehicles, backups or logs.

Exact subpaths are deferred until the Windows backend/installer design is implemented.

## Windows network defaults

Future installer policy:

- TCP/UDP 30814 for BeamMP;
- TCP 8765 for Web Manager restricted to LocalSubnet by default;
- Web Manager must not be exposed publicly by default.

Normal UI must not expose Windows technical details unless the user enters an advanced/diagnostic workflow.

## Windows packaging target

Expected public artifact:

```text
Beam-MP-Server-Manager-vX.Y.Z-Windows-Setup.exe
```

The installer should:

1. install a self-contained Manager runtime;
2. create the Manager Windows service;
3. install/download the validated BeamMP Windows binary with checksum verification;
4. configure Windows Firewall;
5. start automatically;
6. open `http://localhost:8765` for First Run.

Python remains a development implementation detail. The end user must not install Python.

## First Run product contract

The Web onboarding concepts should remain common across editions:

- language;
- first Web administrator;
- BeamMP AuthKey;
- server name;
- maximum players;
- public/private;
- map;
- finish and Start Server.

Linux maintenance-account creation belongs to VMware/Linux appliance onboarding only and must not leak into Windows normal UX.

## Low-risk v0.11 refactors allowed

The following are suitable during v0.11 when touched by nearby work:

- change service type annotations from concrete `SSHClient` to `ServerBackend` when the implementation already uses only protocol methods;
- stop adding new `/opt/beammp` literals to business modules;
- pass existing configured paths instead of creating new hard-coded paths;
- isolate new system/process operations in `LocalLinuxBackend` or appliance-specific helpers;
- add architecture tests preventing new direct `systemctl`/`sudo` calls in common business modules.

Do **not** perform a repository-wide folder migration or implement the full Windows backend before v0.11 is stable.

## Proposed later structure

Direction only, not a v0.11 migration requirement:

```text
beam_manager/
  core/
  backends/
    linux.py
    windows.py
  frontend/

packaging/
  linux/
  windows/
  vmware/

tests/
  common/
  linux/
  windows/
```

Existing paths may move gradually when justified by feature work.

## v0.11 release gate

Before v0.11 is released:

- all existing Linux/VMware tests remain green;
- issue #7 First Run maintenance-account defect is fixed and runtime-validated;
- self-update work is stabilized or explicitly scoped according to issue #1;
- new v0.11 code has been reviewed for new Linux coupling;
- this architecture document is updated with any decisions made during implementation;
- public documentation branch/PR is reconciled only after final v0.11 behavior is known.

A native Windows runtime is not required for v0.11 release.