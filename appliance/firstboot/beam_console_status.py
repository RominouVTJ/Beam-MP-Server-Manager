#!/usr/bin/env python3
"""Permanent, non-interactive tty1 status screen for a configured appliance."""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Sequence


CONFIG = Path("/opt/beammp/ServerConfig.toml")
APPLIANCE_CONFIG = Path("/etc/beam-appliance/config.json")
PAIRING_CODE = Path("/var/lib/beam-manager/setup-pairing.secret")
IP = "/usr/sbin/ip"
IGNORED_INTERFACE_PREFIXES = ("docker", "veth", "virbr", "br-", "podman", "tun", "tap")


def _run_json(command: Sequence[str]) -> list[dict[str, object]]:
    import subprocess

    try:
        completed = subprocess.run(
            list(command), capture_output=True, text=True, check=False, timeout=3,
            env={**os.environ, "LC_ALL": "C"},
        )
        if completed.returncode != 0:
            return []
        value = json.loads(completed.stdout)
        return value if isinstance(value, list) else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []


def _usable_interface(name: str) -> bool:
    return bool(name) and name != "lo" and not name.startswith(IGNORED_INTERFACE_PREFIXES)


def _address_for_interface(name: str, run_json: Callable[[Sequence[str]], list[dict[str, object]]]) -> str | None:
    if not _usable_interface(name):
        return None
    for interface in run_json((IP, "-json", "-4", "address", "show", "dev", name, "scope", "global")):
        entries = interface.get("addr_info", [])
        for address in entries if isinstance(entries, list) else []:
            if not isinstance(address, dict) or address.get("family") != "inet":
                continue
            candidate = str(address.get("local", ""))
            try:
                parsed = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if isinstance(parsed, ipaddress.IPv4Address) and not (
                parsed.is_loopback or parsed.is_link_local or parsed.is_unspecified or parsed.is_multicast
            ):
                return candidate
    return None


def local_ip(run_json: Callable[[Sequence[str]], list[dict[str, object]]] = _run_json) -> str | None:
    """Choose the lowest-metric default-route IPv4, then a usable UP interface."""
    candidates: list[tuple[int, str]] = []
    for route in run_json((IP, "-json", "route", "show", "default")):
        name = str(route.get("dev", ""))
        if not _usable_interface(name):
            continue
        try:
            metric = int(route.get("metric", 0))
        except (TypeError, ValueError):
            metric = 0
        candidates.append((metric, name))
    for _, name in sorted(candidates):
        address = _address_for_interface(name, run_json)
        if address:
            return address

    for interface in run_json((IP, "-json", "-4", "address", "show", "up", "scope", "global")):
        name = str(interface.get("ifname", ""))
        address = _address_for_interface(name, run_json)
        if address:
            return address
    return None


def manager_online() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8765/api/health", timeout=2) as response:
            body = json.load(response)
        return response.status == 200 and body.get("manager") == "ok"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def authkey_configured(path: Path = CONFIG) -> bool:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("AuthKey"):
                return bool(line.split("=", 1)[1].strip().strip('"').strip())
    except (OSError, IndexError):
        pass
    return False


def configured_language(path: Path = APPLIANCE_CONFIG) -> str:
    try:
        language = json.loads(path.read_text(encoding="utf-8")).get("default_language")
        return language if language in {"en", "fr"} else "en"
    except (OSError, ValueError, AttributeError):
        return "en"


def pairing_code(path: Path = PAIRING_CODE) -> str | None:
    try:
        value = path.read_text(encoding="ascii").strip().upper()
    except OSError:
        return None
    import re
    return value if re.fullmatch(r"[A-Z2-9]{4}(?:-[A-Z2-9]{4}){2}(?:(?:-[A-Z2-9]{4}){2})?", value) else None


def render(language: str = "en") -> str:
    address = local_ip()
    online = manager_online()
    configured = authkey_configured()
    pairing = pairing_code()
    web = f"http://{address}:8765" if address else "—"
    if language == "fr":
        network_note = "" if address else "\nAucune adresse réseau détectée.\nVérifiez votre connexion réseau.\n"
        pairing_note = f"\nAppairage Web {pairing}\n" if pairing else ""
        return f"""BEAM-MP-SERVER-MANAGER

Manager        {'ONLINE' if online else 'OFFLINE'}
BeamMP         {'CONFIGURÉ' if configured else 'NON CONFIGURÉ'}

LAN IP         {address or '—'}
Web            {web}
{network_note}
{pairing_note}
Terminez l'installation depuis votre navigateur.
Maintenance locale : Alt+F2"""
    network_note = "" if address else "\nNo network address detected.\nCheck your network connection.\n"
    pairing_note = f"\nWeb pairing    {pairing}\n" if pairing else ""
    return f"""BEAM-MP-SERVER-MANAGER

Manager        {'ONLINE' if online else 'OFFLINE'}
BeamMP         {'CONFIGURED' if configured else 'NOT CONFIGURED'}

LAN IP         {address or '—'}
Web            {web}
{network_note}
{pairing_note}
Complete setup from your browser.
Local maintenance: Alt+F2"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--language", choices=("en", "fr"))
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()
    language = args.language or configured_language()
    while True:
        print("\033c" + render(language), flush=True)
        if not args.watch:
            return 0
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    raise SystemExit(main())
