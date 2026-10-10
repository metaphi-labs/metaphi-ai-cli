"""metaphi: the Metaphi AI command line.

    metaphi login                        sign in through the browser
    metaphi keys create prod             make an organization key (shown once)
    metaphi anonymize trace.json         anonymize a trace with that key
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from metaphi import __version__, accounts, config

SCOPE = "kennet:anonymize"


def _table(rows: list[list[str]]) -> None:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())


def _when(t: float | None) -> str:
    import time
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "-"


def cmd_login(a: argparse.Namespace) -> None:
    if a.api_key:
        if not a.api_key.startswith(config.KEY_PREFIX):
            raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
        cfg = config.load()
        cfg["api_key"] = a.api_key
        config.save(cfg)
        print(f"key saved to {config.path()}")
        return
    got = accounts.login(open_browser=not a.no_browser)
    print(f"Signed in as {got.get('email')}.")
    mine = accounts.orgs()
    if mine:
        print(f"You manage: {', '.join(o['id'] for o in mine)}. Next: metaphi keys create <name>")


def cmd_logout(a: argparse.Namespace) -> None:
    accounts.logout()
    print("Signed out.")


def cmd_whoami(a: argparse.Namespace) -> None:
    cfg = config.load()
    print(f"person: {cfg.get('email') or 'not signed in'}")
    key = config.api_key()
    print(f"key:    {'…' + key[-4:] if key else 'none'}")
    for o in (accounts.orgs() if cfg.get("token") else []):
        print(f"org:    {o['id']} ({o['role']}) holds {', '.join(o['grants']) or 'nothing yet'}")


def cmd_keys_list(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    rows = [["ID", "NAME", "KEY", "SCOPES", "CREATED", "LAST USED", "STATE"]]
    for k in o["keys"]:
        rows.append([k["id"], k["name"], k["hint"], " ".join(k["scopes"]), _when(k["created"]),
                     _when(k.get("last_used")), "revoked" if k.get("revoked") else "live"])
    _table(rows)


def cmd_keys_create(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    made = accounts.create_key(o["id"], a.name, a.scope or [SCOPE])
    print(f"Created {made['id']} ({made['name']}) on {o['id']}, holding {' '.join(made['scopes'])}.\n")
    print(f"  {made['key']}\n")
    print("This is the only time the key is shown. Store it as a secret, e.g. METAPHI_API_KEY.")
    if a.save:
        cfg = config.load()
        cfg["api_key"] = made["key"]
        config.save(cfg)
        print(f"Also saved to {config.path()} for this machine.")


def cmd_keys_revoke(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    accounts.revoke_key(o["id"], a.key_id)
    print(f"Revoked {a.key_id}.")


def cmd_members_list(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    _table([["EMAIL", "ROLE"], *([e, r] for e, r in sorted(o["members"].items()))])


def cmd_members_add(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    accounts.add_member(o["id"], a.email, a.role)
    print(f"{a.email} is now {a.role} of {o['id']}. They sign in with `metaphi login`.")


def cmd_members_remove(a: argparse.Namespace) -> None:
    o = accounts.org(a.org)
    accounts.remove_member(o["id"], a.email)
    print(f"{a.email} removed from {o['id']}.")


def cmd_anonymize(a: argparse.Namespace) -> None:
    from metaphi.anonymize import anonymize
    say = (lambda s: print(s, file=sys.stderr)) if a.json else print
    got = anonymize(Path(a.file), Path(a.output) if a.output else None, a.api_key, a.key or "",
                    a.confidential, say=say)
    if a.json:
        print(json.dumps({k: got[k] for k in ("output", "report", "twins")}, indent=2))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="metaphi", description="The Metaphi AI command line.")
    p.add_argument("--version", action="version", version=f"metaphi {__version__}")
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    s = sub.add_parser("login", help="sign in through the browser (or save a key with --api-key)")
    s.add_argument("--api-key", help="save an organization key on this machine instead of signing in")
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="sign out everywhere this sign-in is used").set_defaults(fn=cmd_logout)
    sub.add_parser("whoami", help="who you are signed in as, and your organizations").set_defaults(fn=cmd_whoami)

    keys = sub.add_parser("keys", help="your organization's keys").add_subparsers(dest="verb", required=True,
                                                                                    metavar="<verb>")
    s = keys.add_parser("list", help="every key, live and revoked")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_keys_list)
    s = keys.add_parser("create", help="make a key; it is shown once")
    s.add_argument("name", help="what the key is for, e.g. prod or staging")
    s.add_argument("--scope", action="append", help=f"a scope the key holds (default {SCOPE}); repeatable")
    s.add_argument("--save", action="store_true", help="also keep it on this machine for `metaphi anonymize`")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_keys_create)
    s = keys.add_parser("revoke", help="stop a key working")
    s.add_argument("key_id")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_keys_revoke)

    members = sub.add_parser("members", help="your organization's people").add_subparsers(
        dest="verb", required=True, metavar="<verb>")
    s = members.add_parser("list")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_members_list)
    s = members.add_parser("add", help="add a person, or change their role")
    s.add_argument("email")
    s.add_argument("--role", default="member", choices=("member", "admin", "owner"))
    s.add_argument("--org")
    s.set_defaults(fn=cmd_members_add)
    s = members.add_parser("remove")
    s.add_argument("email")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_members_remove)

    s = sub.add_parser("anonymize", help="anonymize a trace (JSON, JSON Lines or text)")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="where the anonymized trace goes (default <name>.anonymized<ext>)")
    s.add_argument("--key", help="64 hex characters: the same key gives a person the same stand-in in every trace")
    s.add_argument("--confidential", action="append", help="an organization to replace although public; repeatable")
    s.add_argument("--api-key", help="an organization key (default METAPHI_API_KEY, then the saved one)")
    s.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    s.set_defaults(fn=cmd_anonymize)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        a.fn(a)
    except accounts.Error as e:
        print(f"metaphi: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
