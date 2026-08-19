# Beam-MP-Server-Manager

> A self-hosted Web Manager for BeamMP. The current VMware Edition packages a preconfigured Debian server, BeamMP Server and the Manager into a ready-to-import appliance.

**Stable historical release:** `v0.10.0`
**Current release:** `v0.11.0`
**License:** MIT
**Current edition:** VMware Edition · Debian 13 x86_64 · BeamMP · FastAPI

[English](#english) · [Français](#français)

---

## English

### What is it?

Beam-MP-Server-Manager is an open-source administration interface for BeamMP servers.

The current **VMware Edition** includes a preinstalled and preconfigured Linux environment, BeamMP Server and the Web Manager. Normal server administration is designed not to require SSH, Linux commands, manual TOML editing, Python, Git or Docker.

The project is being structured around **one core codebase and one Web UI**. A future native **Windows Edition** is planned without creating a separate fork.

### VMware Edition

Typical workflow:

```text
Import OVA in VMware
        ↓
First Run
        ↓
Open Web Manager
        ↓
Create Web administrator
        ↓
Configure BeamMP AuthKey / server / map
        ↓
Start Server
```

Main features:

- start, stop and restart BeamMP from the Web UI;
- server dashboard and configuration;
- BeamMP AuthKey management without exposing it in normal UI/log output;
- official and modded map management;
- vehicle and other ZIP mod management;
- enable/disable client mod distribution;
- protection for the currently selected modded map;
- Web users and sessions;
- backups and restore workflows;
- Server Live player/vehicle/ping/speed/telemetry view;
- server messages, kick and vehicle-removal Live controls;
- official map thumbnails when local BeamNG preview assets are available;
- bilingual French/English UI;
- in-app **Bug / feature** reporting to GitHub with optional non-secret diagnostics;
- from v0.11 onward, validated Manager self-update with health checks and automatic rollback.

### Network ports

- BeamMP: TCP + UDP `30814`
- Web Manager: TCP `8765`

The Web Manager should normally remain LAN-only. Do not expose port `8765` directly to the public Internet unless you deliberately deploy an appropriate secure reverse-proxy/access solution.

### Download / VMware deployment

GitHub Releases is the official download source.

Large VMware appliances are distributed as split 7-Zip archives when required. For v0.10.0 the expected files are:

```text
Beam-MP-Server-Manager-v0.10.0.7z.001
Beam-MP-Server-Manager-v0.10.0.7z.002
Beam-MP-Server-Manager-v0.10.0.7z.003
Beam-MP-Server-Manager-v0.10.0-SHA256SUMS.txt
```

Download every part into the same folder, open `.7z.001` with 7-Zip, extract `Beam-MP-Server-Manager.ova`, then import the OVA into VMware Workstation.

Full guides:

- [English VMware deployment](docs/DEPLOYMENT_EN.md)
- [Guide de déploiement VMware en français](docs/DEPLOYMENT_FR.md)

### v0.10.0 historical note

The original v0.10.0 OVA validated the BeamMP/Web/mod/telemetry functionality, but later testing found local-appliance defects in the graphical First Run/reboot path and in Linux maintenance-account creation. These defects are documented rather than hidden and are corrected on the v0.11 development line.

See [v0.10.0 release notes](docs/releases/v0.10.0.md).

### v0.11.0

v0.11 adds/corrects:

- reliable graphical First Run reboot/desktop path;
- mandatory Linux maintenance-account creation in VMware First Run;
- factory-reset execution detached safely from the maintenance session;
- Manager self-update package validation, staged atomic install and rollback;
- official GitHub Release update discovery with SHA-256 verification;
- installed/available/last-update status in Settings;
- in-app GitHub bug/feature reporting;
- release-note policy and cross-platform CI;
- architecture guardrails for the future native Windows Edition.

The detailed draft is in [v0.11.0 release notes](docs/releases/v0.11.0.md). The release is not considered final until the disposable-appliance runtime gate described there passes.

### Self-update policy

The first appliance release containing the Web self-updater is v0.11.0. Therefore the historical v0.10.0 OVA cannot magically update itself to v0.11.0. Existing v0.10.0 installations use the documented replacement/maintenance upgrade route.

Starting with v0.11.0, compatible future Manager releases can provide an asset named:

```text
Beam-MP-Server-Manager-vX.Y.Z.update.zip
```

The Manager only offers an official one-click package when it can verify the expected GitHub Release asset and SHA-256 digest. A failed new Manager health check triggers automatic rollback.

### Future Windows Edition

The Windows Edition is a planned native distribution, not a Linux VM hidden inside an installer.

Target architecture:

```text
Web UI
  ↓
FastAPI / Core
  ├─ LocalLinuxBackend   → VMware Edition
  └─ LocalWindowsBackend → Windows Edition (future)
```

The intended Windows artifact is:

```text
Beam-MP-Server-Manager-vX.Y.Z-Windows-Setup.exe
```

End users should not need Python, Git, Docker, SSH or a terminal. See [Windows Edition architecture preparation](docs/architecture/WINDOWS_EDITION_PREPARATION.md).

### Development

Python 3.11+ is used for development.

Windows example:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev,windows]"
.\scripts\start.ps1
```

Tests:

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp .pytest-tmp
```

GitHub Actions runs the full suite on Linux and Windows.

### Contributing / security

- [Contributing](CONTRIBUTING.md)
- [Security policy](SECURITY.md)
- [Third-party notices](THIRD_PARTY_NOTICES.md)
- [Changelog](CHANGELOG.md)

Never publish BeamMP AuthKeys, passwords, appliance security codes, session cookies, private keys or unsanitized sensitive network information in an issue.

---

## Français

### Présentation

Beam-MP-Server-Manager est une interface d’administration open source pour les serveurs BeamMP.

La **VMware Edition** actuelle fournit directement un environnement Debian Linux préinstallé et préconfiguré, BeamMP Server et le Web Manager. Pour l’utilisation normale, l’objectif est de ne pas avoir besoin de SSH, commandes Linux, édition manuelle du TOML, Python, Git ou Docker.

Le projet conserve **une seule base de code et une seule interface Web**. Une future **Windows Edition native** est prévue sans créer un fork séparé.

### VMware Edition

Parcours normal :

```text
Importer l'OVA dans VMware
        ↓
First Run
        ↓
Ouvrir le Web Manager
        ↓
Créer l'administrateur Web
        ↓
Configurer AuthKey / serveur / carte
        ↓
Start Server
```

Fonctions principales :

- démarrage, arrêt et redémarrage de BeamMP depuis le navigateur ;
- tableau de bord et configuration du serveur ;
- gestion de l’AuthKey BeamMP ;
- cartes officielles et moddées ;
- véhicules et autres mods ZIP ;
- activation/désactivation de la distribution aux clients ;
- protection de la carte moddée actuellement utilisée ;
- utilisateurs et sessions Web ;
- sauvegardes et restauration ;
- vue Live joueurs/véhicules/ping/vitesse/télémétrie ;
- message serveur, kick et suppression de véhicule ;
- miniatures des cartes officielles lorsque les aperçus BeamNG locaux sont disponibles ;
- interface français/anglais ;
- bouton **Bug / suggestion** vers GitHub avec diagnostics non sensibles optionnels ;
- à partir de v0.11, auto-update validé du Manager avec contrôle de santé et rollback automatique.

### Ports réseau

- BeamMP : TCP + UDP `30814`
- Web Manager : TCP `8765`

Le Web Manager doit normalement rester accessible uniquement sur le réseau local. Ne redirigez pas directement le port `8765` sur Internet pour une utilisation standard.

### Téléchargement / installation VMware

Les GitHub Releases sont la source officielle de téléchargement.

Lorsque l’OVA est trop volumineuse, elle est distribuée en archive 7-Zip découpée. Pour v0.10.0 :

```text
Beam-MP-Server-Manager-v0.10.0.7z.001
Beam-MP-Server-Manager-v0.10.0.7z.002
Beam-MP-Server-Manager-v0.10.0.7z.003
Beam-MP-Server-Manager-v0.10.0-SHA256SUMS.txt
```

Téléchargez toutes les parties dans le même dossier, ouvrez `.7z.001` avec 7-Zip, extrayez `Beam-MP-Server-Manager.ova`, puis importez l’OVA dans VMware Workstation.

Guides complets :

- [Déploiement VMware en français](docs/DEPLOYMENT_FR.md)
- [English VMware deployment](docs/DEPLOYMENT_EN.md)

### Note historique v0.10.0

L’OVA v0.10.0 originale a validé les fonctions serveur/Web/mods/télémétrie, mais des défauts locaux ont ensuite été découverts dans le reboot/desktop du First Run graphique et dans la création du compte Linux de maintenance. Ces défauts sont documentés et corrigés sur la branche v0.11.

Voir [les notes v0.10.0](docs/releases/v0.10.0.md).

### v0.11.0

v0.11 apporte notamment :

- correction du parcours graphique First Run et du reboot automatique ;
- création obligatoire du compte Linux de maintenance dans la VMware Edition ;
- factory reset détaché de la session de maintenance ;
- mise à jour du Manager avec validation, installation atomique et rollback ;
- détection de la GitHub Release officielle avec vérification SHA-256 ;
- versions installée/disponible et dernier résultat dans Settings ;
- signalement de bug / proposition de fonction vers GitHub ;
- politique de release notes et CI Linux/Windows ;
- préparation architecturale de la future Windows Edition native.

Le détail est dans [les notes v0.11.0](docs/releases/v0.11.0.md). La version ne sera considérée finale qu’après le dernier test runtime sur appliance jetable.

### Future Windows Edition

La Windows Edition sera une installation Windows native, sans VM Linux.

Objectif :

```text
Web UI
  ↓
FastAPI / Core
  ├─ LocalLinuxBackend   → VMware Edition
  └─ LocalWindowsBackend → Windows Edition (future)
```

Installateur prévu :

```text
Beam-MP-Server-Manager-vX.Y.Z-Windows-Setup.exe
```

L’utilisateur final ne devra installer ni Python, ni Git, ni Docker et ne devra utiliser ni SSH ni terminal pour l’usage normal.

Voir [la préparation de l’architecture Windows Edition](docs/architecture/WINDOWS_EDITION_PREPARATION.md).

### Open source

Beam-MP-Server-Manager est distribué sous licence MIT. Les binaires BeamMP Server et les ressources BeamNG restent des composants tiers soumis à leurs propres conditions. Les ressources officielles BeamNG ne sont pas redistribuées dans ce dépôt.
