"""Account and network management from the shell: ``yoosee-web users|networks``.

The commands open the same SQLite database as the running service. Changes apply on
the service's next request, and password changes or disabling end open sessions.
"""
import argparse
import getpass
import json
from pathlib import Path
import sqlite3
import sys
import tomllib

from .accounts import UserStore

PRESETS = {
    "admin": {"roles": ["web_admin"], "capabilities": []},
    "camera-admin": {"roles": ["camera_admin", "viewer"], "capabilities": []},
    "viewer": {"roles": ["viewer"], "capabilities": []},
    "watcher": {"roles": ["viewer"], "capabilities": ["watcher"]},
}


def _store(config_path):
    config = tomllib.loads(Path(config_path).read_text())
    web = config["web"]
    store = UserStore(web.get("auth_database", "~/.local/state/yoosee-cam-utils/viewer-users.sqlite3"), web["allowed_networks"])
    return store, list(config["cameras"])


def _password(args, confirm=True):
    if args.password_stdin:
        value = sys.stdin.readline().rstrip("\r\n")
        if not value:
            raise ValueError("No password on stdin.")
        return value
    if not sys.stdin.isatty():
        raise ValueError("Use --password-stdin when stdin is not a terminal.")
    first = getpass.getpass("Password: ")
    if confirm and getpass.getpass("Repeat password: ") != first:
        raise ValueError("The passwords differ.")
    return first


def _cameras(value, available):
    if value in (None, "", "all"):
        return list(available)
    chosen = [item.strip() for item in value.split(",") if item.strip()]
    unknown = [item for item in chosen if item not in available]
    if unknown:
        raise ValueError("Unknown camera IDs: " + ", ".join(unknown))
    return chosen


def _find(store, username):
    for item in store.inventory():
        if item["username"] == username:
            return item
    raise KeyError(username)


def _build_parser():
    p = argparse.ArgumentParser(prog="yoosee-web", description="Manage Watchpost accounts and source networks.")
    p.add_argument("--config", required=True)
    sub = p.add_subparsers(dest="group", required=True)
    users = sub.add_parser("users").add_subparsers(dest="action", required=True)
    users.add_parser("list")
    add = users.add_parser("add")
    add.add_argument("username")
    add.add_argument("--preset", choices=sorted(PRESETS), required=True)
    add.add_argument("--cameras", help="comma-separated camera IDs, or 'all' (default for non-admin presets)")
    add.add_argument("--networks", help="optional comma-separated source IPs/CIDRs for this account")
    add.add_argument("--record", dest="record", action="store_true", default=None, help="allow recording (default except for watchers)")
    add.add_argument("--no-record", dest="record", action="store_false")
    add.add_argument("--link-camera-password", action="store_true", help="try this account's password on cameras that reject theirs")
    add.add_argument("--password-stdin", action="store_true")
    passwd = users.add_parser("passwd")
    passwd.add_argument("username")
    passwd.add_argument("--password-stdin", action="store_true")
    for name in ("enable", "disable"):
        users.add_parser(name).add_argument("username")
    for name in ("grant", "revoke"):
        s = users.add_parser(name)
        s.add_argument("username")
        s.add_argument("cameras", help="comma-separated camera IDs")
    nets = users.add_parser("set-networks")
    nets.add_argument("username")
    nets.add_argument("networks", help="comma-separated IPs/CIDRs, or '-' to clear")
    networks = sub.add_parser("networks").add_subparsers(dest="action", required=True)
    networks.add_parser("list")
    for name in ("add", "remove"):
        networks.add_parser(name).add_argument("cidr")
    return p


def _update(store, camera_ids, username, **changes):
    """Re-save an account with a few fields changed, keeping everything else."""
    item = _find(store, username)
    perms = dict(changes.get("cameras", item["cameras"]))
    store.update_access(
        username, roles=item["roles"], camera_permissions=perms,
        networks=changes.get("networks", item["networks"]), camera_ids=camera_ids,
        capabilities=[c for c in item["capabilities"] if c != "record"],
        record="record" in item["capabilities"],
    )


def run(argv):
    args = _build_parser().parse_args(argv)
    try:
        store, camera_ids = _store(args.config)
        if args.group == "networks":
            if args.action == "list":
                for row in store.list_networks(detail=True):
                    print(f"{row['cidr']}\t{row['source']}")
            elif args.action == "add":
                store.add_networks(args.cidr)
            else:
                store.remove_network(args.cidr, None)
            return 0
        if args.action == "list":
            for item in store.inventory():
                print(json.dumps({key: item[key] for key in ("username", "roles", "capabilities", "enabled", "cameras", "networks")}, sort_keys=True))
        elif args.action == "add":
            preset = PRESETS[args.preset]
            cameras = {} if args.preset == "admin" else {
                camera: ("camera_admin" if args.preset == "camera-admin" else "viewer")
                for camera in _cameras(args.cameras, camera_ids)}
            store.create_user(
                args.username, _password(args), roles=preset["roles"], camera_permissions=cameras,
                networks=[n for n in (args.networks or "").split(",") if n.strip()], camera_ids=camera_ids,
                capabilities=preset["capabilities"], record=args.record, camera_link=args.link_camera_password)
            print(f"Created {args.preset} account {args.username}.")
        elif args.action == "passwd":
            store.set_password(args.username, _password(args))
            print("Password changed. Open sessions for the account ended.")
        elif args.action in ("enable", "disable"):
            store.set_enabled(args.username, args.action == "enable")
        elif args.action in ("grant", "revoke"):
            item = _find(store, args.username)
            chosen = _cameras(args.cameras, camera_ids)
            perms = dict(item["cameras"])
            level = "camera_admin" if "camera_admin" in item["roles"] else "viewer"
            for camera in chosen:
                if args.action == "grant":
                    perms[camera] = level
                else:
                    perms.pop(camera, None)
            _update(store, camera_ids, args.username, cameras=perms)
        elif args.action == "set-networks":
            networks = [] if args.networks == "-" else [n for n in args.networks.split(",") if n.strip()]
            _update(store, camera_ids, args.username, networks=networks)
        return 0
    except KeyError as error:
        print(f"Unknown account or item: {error}", file=sys.stderr)
    except (ValueError, OSError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
    return 2
