# Release artifact distribution

GitHub Releases is the primary download channel for Beam-MP-Server-Manager.

## Large OVA rule

When the VMware OVA cannot be uploaded as one practical GitHub Release asset, distribute it as a split 7-Zip archive with each part kept at approximately 1.9 GB maximum.

Users must download every part into the same directory, open the `.7z.001` part with 7-Zip, extract the `.ova`, then import the extracted appliance into VMware Workstation.

A `SHA256SUMS` companion file must be published with the release so every distributed part can be verified before extraction.

## v0.10.0

The validated OVA is approximately 4.3 GB. The official GitHub Release `v0.10.0` therefore uses these distribution names:

- `Beam-MP-Server-Manager-v0.10.0.7z.001`
- `Beam-MP-Server-Manager-v0.10.0.7z.002`
- `Beam-MP-Server-Manager-v0.10.0.7z.003`
- `Beam-MP-Server-Manager-v0.10.0-SHA256SUMS.txt`

The three archive parts reconstruct/extract `Beam-MP-Server-Manager.ova`.

## Future releases

Apply the same naming convention with the release version substituted in the file names. For example, if the v0.11.0 OVA still requires splitting:

- `Beam-MP-Server-Manager-v0.11.0.7z.001`
- subsequent numbered parts as required;
- `Beam-MP-Server-Manager-v0.11.0-SHA256SUMS.txt`.

Do not hard-code a fixed number of parts for future versions; the final OVA size determines the number required.

The canonical release notes must state whether the OVA is a single asset or split archive and list the actual published filenames.

## Other channels

GitHub remains the canonical project and download platform. Other channels may be used for announcements, support or test builds, but must not silently become the authoritative release source.
