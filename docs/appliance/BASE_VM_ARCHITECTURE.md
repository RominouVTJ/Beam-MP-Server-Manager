# Base VM architecture

- Platform: Debian 13 x86_64 under VMware; `open-vm-tools` is enabled.
- `beammpserver:beamadmin` runs `/opt/beammp/BeamMP-Server`.
- `beammanager:beamadmin` runs the installed Python package from `/opt/beam-manager/.venv`.
- Configuration: `/etc/beam-manager`; mutable manager state: `/var/lib/beam-manager`; logs: `/var/log/beam-manager`.
- BeamMP and the primary Manager profile are local. `LocalLinuxBackend` controls systemd; there is no loopback SSH.
- Shared group access is limited to BeamMP paths needed by the Manager. No application service runs as root.
- The temporary `builder` account remains unchanged in Gate 2 and the Windows LAB private key is never copied into the source archive or appliance.
