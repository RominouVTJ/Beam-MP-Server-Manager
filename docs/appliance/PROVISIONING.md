# Provisioning

`appliance/provisioning/install.sh` is non-interactive, CWD-independent, uses absolute target paths and is reasonably idempotent. It validates Debian 13/x86_64, installs the minimal dependency set, creates accounts and directories, and downloads the exact `BeamMP-Server.debian.13.x86_64` asset from the official BeamMP GitHub release API. The official `sha256:` asset digest must exist and match before installation.

Final Debian packages: `ca-certificates`, `curl`, `jq`, `python3`, `python3-venv`, `open-vm-tools`, `ufw`, `liblua5.3-0`, `iproute2`, and `sudo`. `wget`, `unzip`, and system `python3-pip` are not required.

The Manager is installed as a real Python package in `/opt/beam-manager/.venv`. `beam-manager migrate-linux` creates a new schema with a local primary profile, LAN onboarding enabled, and no users or sessions. UFW permits OpenSSH, 8765/TCP and 30814/TCP+UDP without fixing a product `/24`; the initial broader LAN setting is a First Run input to narrow later.

Verification is codified in `appliance/tests/verify.sh`. Temporary Web-admin test state is removed with `appliance/tests/reset-web-test-state.sh`, which is not a factory reset.

The installer disables and re-enables `beam-appliance-firstboot.service` when
installing it so an older `multi-user.target.wants` link cannot survive a unit
migration. The resulting link is under `sysinit.target.wants`. Plymouth units
are reproducibly masked, GRUB receives `plymouth.enable=0`, warning-level kernel
messages are kept off the interactive console, and UFW journal logging remains
enabled. Gate 3 reboot requests are scheduled with a transient systemd timer;
the SSH command therefore returns success before the connection is lost to the
reboot.
