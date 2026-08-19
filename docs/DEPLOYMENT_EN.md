# Deploy Beam-MP-Server-Manager VMware Edition

This guide describes the intended **Beam-MP-Server-Manager v0.11.0 VMware Edition** workflow. While the v0.11 release notes still say `draft`, final appliance runtime validation is not yet closed.

The appliance already includes Debian Linux, BeamMP Server and the Web Manager. Normal users do not need to install Linux, Python, Git or Docker.

## 1. Download the release

GitHub Releases is the official download source.

If the OVA is distributed as a split 7-Zip archive, download **every** part into the same folder, for example:

```text
Beam-MP-Server-Manager-v0.11.0.7z.001
Beam-MP-Server-Manager-v0.11.0.7z.002
...
Beam-MP-Server-Manager-v0.11.0-SHA256SUMS.txt
```

The actual part count depends on final OVA size.

Verify the SHA-256 values from `SHA256SUMS`, then open `.7z.001` with 7-Zip and extract:

```text
Beam-MP-Server-Manager.ova
```

Do not import files whose hashes do not match.

## 2. Import into VMware Workstation

1. Open VMware Workstation Pro.
2. Choose **File → Open**.
3. Select `Beam-MP-Server-Manager.ova`.
4. Choose the new VM name and storage folder.
5. Allow VMware to create a new identity/MAC for the instance.
6. Make sure the network adapter is connected.
7. For a typical home server, **Bridged** networking is usually the simplest choice.
8. Start the VM.

A DHCP reservation is recommended after setup so the appliance keeps a stable LAN address.

## 3. Local First Run

On the first boot, the graphical VMware wizard asks for:

1. language;
2. country / region;
3. keyboard layout and test;
4. localization / timezone;
5. **Linux maintenance account** and password;
6. finalization.

The Linux account is for exceptional maintenance. It is separate from Web Manager accounts.

After **Finish and reboot**, the appliance automatically restarts into the graphical Beam-MP-Server-Manager desktop. The technical `beamconsole` graphical account signs in automatically and is not the maintenance/SSH identity.

The local appliance window shows information including:

- Manager state;
- BeamMP state/configuration;
- Web URL;
- appliance security code, masked by default.

Keep the security code private. Never publish it in screenshots or GitHub issues.

## 4. Create the first Web administrator

From another computer on the same LAN, open:

```text
http://APPLIANCE_IP:8765
```

Then:

1. follow the initial Web setup;
2. enter the requested code;
3. create the first Web administrator;
4. sign in.

The Web account and Linux maintenance account are separate identities.

## 5. Configure BeamMP

In the Manager:

1. enter the BeamMP AuthKey;
2. set the server name;
3. configure maximum players/cars;
4. choose public/private visibility;
5. choose the map;
6. save;
7. start BeamMP.

The AuthKey is a secret. Do not include it in GitHub issues, public screenshots or logs.

## 6. Network ports

BeamMP:

```text
TCP 30814
UDP 30814
```

Web Manager:

```text
TCP 8765
```

For Internet players, forward TCP + UDP `30814` to the appliance LAN address.

**Do not normally forward 8765 to the public Internet.** The Manager is intended for LAN administration or for a deliberately deployed secure access/reverse-proxy layer.

## 7. Maps, vehicles and mods

The Manager supports workflows including:

- official map selection;
- modded map upload;
- vehicle and other ZIP uploads;
- client distribution enable/disable;
- protection of the currently selected modded map;
- official map thumbnails when local BeamNG preview assets are available.

Normal users do not need to move ZIP files manually inside Linux.

## 8. Live Server

When a BeamNG/BeamMP client joins, Live Server can display:

- players;
- vehicles;
- ping;
- speed when available;
- movement/radar position;
- join, vehicle and disconnect events.

Available controls include server message, kick and vehicle removal.

The current radar is intentionally a local movement view, not a falsely calibrated BeamNG Big Map projection.

## 9. Backups

Runtime/user data lives outside the application code tree. A Manager update must not delete:

- Web accounts;
- configuration;
- BeamMP AuthKey;
- mods/maps/vehicles;
- backups;
- useful runtime state.

## 10. Manager updates from v0.11 onward

The **Settings** page exposes Manager updates.

It shows:

- installed version;
- available version;
- last update result.

When a newer GitHub Release contains the exact expected update package and SHA-256 digest, the Manager can download and install it.

The process:

1. downloads the official package;
2. verifies SHA-256;
3. validates package contents;
4. stages the candidate installation;
5. backs up the current Manager/state required for rollback;
6. restarts the Manager;
7. validates health and running version;
8. automatically restores the previous version if the new Manager does not become healthy.

A local `.update.zip` can also be selected as a maintenance fallback.

### Important for v0.10.0

The v0.10.0 OVA was built before this updater existed. It therefore cannot trigger a Web self-update to v0.11.0. The in-product update path starts with v0.11.0 for subsequent compatible versions.

## 11. Report a bug / request a feature

The **Bug / feature** action opens a form and prepares a GitHub issue.

Automatic technical information is deliberately restricted to non-sensitive values such as Manager version and safe health state.

Before submitting an issue, make sure it contains no:

- BeamMP AuthKey;
- password;
- appliance security code;
- session cookie/token;
- private key;
- unsanitized sensitive network address.

## 12. Exceptional Linux maintenance

Normal use does not require SSH. The Linux account created during First Run exists for specific maintenance workflows only.

`beamconsole` is not allowed over SSH.

Direct root login is locked. Privileged Manager operations use narrowly scoped helpers rather than generic root access.

## 13. Factory reset / image preparation

Factory reset is a maintenance/image-building operation and intentionally erases instance accounts/data in its scope. Do not use it as a normal restart button.

The v0.11 reset helper detaches destructive work from the invoking maintenance session before deleting maintenance users so the operation can finish and power off the VM cleanly.

## 14. Common problems

### No LAN address

Check the VMware network adapter, Bridged selection and router DHCP.

### Web Manager does not open

From the same LAN:

```text
http://APPLIANCE_IP:8765
```

Use HTTP unless you deliberately added your own HTTPS/reverse-proxy layer.

### Server works on LAN but not from the Internet

Check:

- DHCP reservation;
- TCP 30814 forwarding;
- UDP 30814 forwarding;
- actual public IP;
- possible ISP CGNAT.

### A mod is not downloaded

Check its distribution state in the Manager, restart BeamMP when required, then reconnect the client.

## 15. Windows Edition

A native Windows Edition is planned later. It will reuse the same core/Web UI but will not use this Debian VM or systemd. It is not part of v0.11.
