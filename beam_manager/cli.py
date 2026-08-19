from __future__ import annotations

import argparse
import getpass
import sqlite3
import sys

from beam_manager.config import get_settings
from beam_manager.phase5 import Phase5Store, utc_now


def _store() -> Phase5Store:
    settings = get_settings()
    return Phase5Store(
        settings.data_dir / "beamserver.db",
        settings.data_dir,
        settings.session_secret_file,
    )


def _password(confirm: bool = True) -> str:
    password = getpass.getpass("Mot de passe : ")
    if confirm and password != getpass.getpass("Confirmation : "):
        raise ValueError("Les mots de passe ne correspondent pas")
    return password


def _username(value: str | None) -> str:
    return value or input("Nom d'utilisateur : ").strip()


def _user_command(args: argparse.Namespace) -> int:
    store = _store()
    if args.action == "list":
        users = store.users()
        if not users:
            print("Aucun utilisateur configure.")
        for user in users:
            state = "actif" if user["enabled"] else "desactive"
            print(f"{user['username']}\t{user['role']}\t{state}")
        return 0
    username = _username(args.username)
    if args.action == "add":
        user = store.add_user(username, _password(), args.role)
        print(f"Utilisateur {user['username']} cree avec le role {user['role']}.")
    elif args.action == "passwd":
        store.set_user_password(username, _password())
        print(f"Mot de passe de {username} modifie; ses sessions ont ete invalidees.")
    elif args.action == "disable":
        store.set_user_enabled(username, False)
        print(f"Utilisateur {username} desactive; ses sessions ont ete invalidees.")
    elif args.action == "enable":
        store.set_user_enabled(username, True)
        print(f"Utilisateur {username} active.")
    elif args.action == "role":
        store.set_user_role(username, args.role)
        print(f"Role de {username} modifie en {args.role}; ses sessions ont ete invalidees.")
    elif args.action == "delete":
        if not args.yes and input(f"Supprimer {username} ? [y/N] ").strip().casefold() not in {"y", "yes", "o", "oui"}:
            print("Annule.")
            return 1
        store.delete_user(username)
        print(f"Utilisateur {username} supprime.")
    return 0


def _prepare_linux(args: argparse.Namespace) -> int:
    store = _store()
    now = utc_now().isoformat()
    with store.connect() as db:
        db.execute("DELETE FROM auth_sessions")
        db.execute("DELETE FROM users")
        db.execute("DELETE FROM admin_credentials")
        db.execute(
            "UPDATE server_profiles SET host=?,backend='local',ssh_key_path='',updated_at=? WHERE id='primary'",
            (args.host, now),
        )
        db.execute(
            """INSERT INTO manager_settings(key,value_json,updated_at) VALUES('lan_enabled','true',?)
            ON CONFLICT(key) DO UPDATE SET value_json='true',updated_at=excluded.updated_at""",
            (now,),
        )
    print("Migration Linux preparee : profil principal local, LAN actif, aucun compte importe.")
    return 0


def _firstboot_sync(args: argparse.Namespace) -> int:
    if args.timezone not in {
        "Europe/Paris", "Europe/London", "America/New_York", "America/Chicago",
        "America/Denver", "America/Los_Angeles",
    }:
        raise ValueError("Fuseau horaire IANA invalide")
    store = _store()
    store.apply_firstboot_settings({
        "default_language": args.language,
        "country": args.country,
        "locale": args.locale,
        "timezone": args.timezone,
        "keyboard_layout": args.keyboard,
    })
    pairing_code = "" if store.admin_configured() else store.ensure_setup_pairing_code()
    print(f"SETUP_PAIRING_CODE={pairing_code}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="beam-manager", description="Administration locale de Beam-MP-Server-Manager")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Demarrer le service web")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    user = commands.add_parser("user", help="Gerer les comptes sans interface web")
    actions = user.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add")
    add.add_argument("username", nargs="?")
    add.add_argument("--role", choices=("admin", "viewer"), default="admin")
    actions.add_parser("list")
    for name in ("passwd", "disable", "enable"):
        item = actions.add_parser(name)
        item.add_argument("username")
    role = actions.add_parser("role")
    role.add_argument("username")
    role.add_argument("role", choices=("admin", "viewer"))
    delete = actions.add_parser("delete")
    delete.add_argument("username")
    delete.add_argument("--yes", action="store_true")
    migrate = commands.add_parser("migrate-linux", help="Preparer le profil principal Linux local")
    migrate.add_argument("--host")
    firstboot = commands.add_parser("firstboot-sync", help=argparse.SUPPRESS)
    firstboot.add_argument("--language", choices=("en", "fr"), required=True)
    firstboot.add_argument("--country", choices=("France", "United Kingdom", "United States"), required=True)
    firstboot.add_argument("--locale", choices=("fr_FR.UTF-8", "en_GB.UTF-8", "en_US.UTF-8"), required=True)
    firstboot.add_argument("--timezone", required=True)
    firstboot.add_argument("--keyboard", choices=("fr", "gb", "us"), required=True)
    return parser


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "beam_manager.asgi:app",
        host=args.host or settings.manager_host,
        port=args.port or settings.manager_port,
        proxy_headers=False,
    )
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "user":
            return _user_command(args)
        if args.command == "serve":
            return _serve(args)
        if args.command == "firstboot-sync":
            return _firstboot_sync(args)
        if args.host is None:
            args.host = get_settings().server_host
        return _prepare_linux(args)
    except (ValueError, LookupError, sqlite3.Error, OSError) as exc:
        print(f"Erreur : {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
