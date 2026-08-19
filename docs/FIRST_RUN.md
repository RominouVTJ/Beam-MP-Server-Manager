# Appliance First Run

## Architecture

`beam-appliance-firstboot.service` owns `tty1` only while
`/var/lib/beam-appliance/firstboot-complete` is absent. It executes the
root-owned Python wizard directly; it never starts a shell. The permanent
`beam-appliance-status.service` owns the same console only after the marker
exists and runs as `beammanager`. A maintenance login remains available on
`tty2` (`Alt+F2`).

Progress is atomically stored in
`/var/lib/beam-appliance/firstboot-state.json` (root, mode 0600). The public
system profile is atomically stored in `/etc/beam-appliance/config.json`
(root, mode 0644). The completion marker is the final write. Rebooting before
that write resumes from the last verified stage.

## Screen sequence

1. Bilingual language choice.
2. Country/region choice: France, United Kingdom or United States.
3. Editable keyboard preset, immediately applied to console and X11.
4. Free keyboard test and confirmation/change choice.
5. Editable locale and valid IANA timezone choice.
6. Maintenance username, password and confirmation.
7. Configuration/Manager health wait.
8. Final Manager, LAN address, URL and BeamMP status.

The maintenance password travels only over an anonymous pipe to `chpasswd`.
It is never an argument, file, Manager setting, or log value. The maintenance
account is independent from the `beammanager` and `beammpserver` service
accounts. Direct root login is locked and SSH root login is denied.

## Network selection

The status code reads the JSON output of `ip route` and selects the global
IPv4 address on the lowest-metric default-route interface. Loopback and common
container/virtual bridge interfaces are excluded. If no default route exists,
the first usable UP global interface is used. If no address is ready, setup
still completes and the permanent screen retries every five seconds.

## XFCE recommendation for the human decision

Gate 3 does not remove or alter XFCE. For the candidate appliance, a headless
console is the recommended default: remote browser administration is the
product workflow, while tty2 and SSH preserve recovery access. This reduces
installed size, idle RAM, boot work, update surface and desktop-specific
maintenance. Keeping XFCE is justified only if local graphical diagnosis is a
required supported workflow; it otherwise adds a second user interface without
helping normal setup.
