# Contributing to Beam-MP-Server-Manager

Thanks for contributing.

## Project direction

Beam-MP-Server-Manager uses one core codebase and one Web UI. The current VMware Edition runs on Debian/Linux; a future native Windows Edition must reuse the same core rather than becoming a separate fork.

When adding functionality:

- keep business logic independent from the host operating system where practical;
- put Linux-specific process/service/filesystem behavior behind the existing backend/edition boundary;
- do not add direct `systemctl`, `journalctl`, `sudo`, PowerShell or Windows service-manager calls to common business modules;
- never commit BeamMP AuthKeys, passwords, appliance security codes, cookies, private keys, public IP addresses or other secrets;
- do not add BeamNG game assets or official map images to the repository.

## Development

Python 3.11 or newer is required for source development.

Install the project and development dependencies in a virtual environment, then run:

```text
python -m pytest -q --basetemp .pytest-tmp
```

The GitHub Actions CI also runs the full suite on Linux and Windows.

## Changes and pull requests

Keep changes focused and include tests for observable behavior. Runtime-sensitive appliance work should distinguish:

1. observed symptom;
2. evidence;
3. root cause;
4. smallest source fix;
5. automated tests;
6. runtime validation when an appliance/BeamMP interaction is involved.

Do not report runtime PASS from unit tests alone.

## Issues

Use the structured Bug Report or Feature Request forms. Reports generated from the Manager UI open a pre-filled GitHub issue for review before submission.

## Releases

Detailed release notes live in `docs/releases/`. A version is not release-ready until its canonical release note and `CHANGELOG.md` entry are updated.
