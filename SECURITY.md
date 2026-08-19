# Security policy

## Reporting a vulnerability

Please do **not** publish credentials, exploit details containing real secrets, or sensitive appliance information in a public GitHub issue.

For ordinary functional bugs, use the Bug Report form and sanitize logs/screenshots first.

Until a dedicated private vulnerability-reporting channel is configured on the public repository, security-sensitive findings should be reported through GitHub's private security advisory / private vulnerability reporting feature when available for this repository rather than as a public issue.

## Secrets that must never be attached

Do not include:

- BeamMP AuthKeys;
- Web account passwords;
- Linux maintenance passwords;
- appliance security/recovery codes;
- session cookies or CSRF tokens;
- SSH private keys;
- private network topology or public IP addresses unless strictly necessary and explicitly sanitized.

## Supported versions

Security fixes target the latest supported Beam-MP-Server-Manager release. Historical release notes document older versions but do not imply ongoing security support.

## Appliance security boundaries

The Manager Web interface is intended to require authentication when exposed on the LAN. The native Windows Edition is planned to bind locally / restrict LAN exposure by default. Privileged appliance operations must use narrowly scoped helpers rather than a generic passwordless root shell.
