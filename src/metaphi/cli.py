"""metaphi: the Metaphi AI command line.

    metaphi login                        sign in through the browser; your organization's keys come with it
    metaphi twin trace.json              twin a trace with that key
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path

from metaphi import __version__, accounts, config, errors


class _OnArgv(argparse.Action):
    """A secret passed as an argument: still accepted, but it lands in shell history and `ps`, so say so."""

    def __call__(self, p: argparse.ArgumentParser, ns: argparse.Namespace, value: object, opt: str | None = None):
        print(f"metaphi: warning: {opt} puts the key in your shell history and process list and will be removed; "
              "use METAPHI_API_KEY or `metaphi login --with-api-key` (reads stdin)", file=sys.stderr)
        setattr(ns, self.dest, value)


class _DebugKeyring(argparse.Action):
    """Say where secrets would be kept, and stop: the release proves a real keychain backend loads in the binary."""

    def __call__(self, p: argparse.ArgumentParser, ns: argparse.Namespace, value: object, opt: str | None = None):
        print(config.keyring_backend())
        p.exit()


def _read_key() -> str:
    """An organization key from stdin when it is piped, else from a prompt that does not echo."""
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.readline().strip()
    return getpass.getpass("Organization key (mai_sk_...): ").strip()


def cmd_login(a: argparse.Namespace) -> None:
    key = a.api_key or (_read_key() if a.with_api_key else None)
    if a.with_api_key or a.api_key:
        if not key or not key.startswith(config.KEY_PREFIX):
            raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
        cfg = config.load()
        cfg["api_key"] = key
        config.save(cfg)
        print("key saved on this machine")
        return
    got = accounts.login(open_browser=not a.no_browser)
    print(f"Signed in as {got.get('email')}.")
    if config.load().get("test_api_key"):
        print(f"Ready for {got.get('org')}: its live key and test key are saved on this machine.")
        print("Next: metaphi twin <file>   (--test for a free test run)")
    else:
        print(f"No key was saved: {got.get('no_api_key') or 'the server sent none'}.")
    print(f"Keys, members and billing are managed at {config.accounts_url()}/keys")


def cmd_logout(a: argparse.Namespace) -> None:
    accounts.logout()
    print("Signed out.")


def cmd_whoami(a: argparse.Namespace) -> None:
    cfg = config.load()
    print(f"person: {cfg.get('email') or 'not signed in'}")
    key = config.api_key()
    print(f"key:    {'…' + key[-4:] if key else 'none'}")
    if cfg.get("test_api_key"):
        print(f"test:   …{cfg['test_api_key'][-4:]}")
    for o in (accounts.orgs() if cfg.get("token") else []):
        print(f"org:    {o['id']} ({o['role']}) holds {', '.join(o['grants']) or 'nothing yet'}")


def _seconds(said: str) -> float:
    try:
        got = float(said)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{said!r} is not a number of seconds") from None
    if got <= 0:
        raise argparse.ArgumentTypeError("a number of seconds above zero")
    return got


def cmd_twin(a: argparse.Namespace) -> None:
    from metaphi.twin import twin
    say = (lambda s: print(s, file=sys.stderr)) if a.json else print
    got = twin(Path(a.file), Path(a.output) if a.output else None, a.api_key, a.key or "",
               a.confidential, say=say, deadline_s=a.timeout, test=a.test)
    if a.json:
        print(json.dumps({"output": got["output"]}, indent=2))


def _usd(value: str) -> float:
    try:
        usd = round(float(value), 2)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an amount: {value!r}") from None
    if not 1 <= usd <= 10000:
        raise argparse.ArgumentTypeError("an amount is between 1 and 10000 dollars")
    return usd


def cmd_credits(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    print(f"{o['id']}: ${o['credits_usd']:,.2f} prepaid credit")
    if o["credits_usd"] <= 0:
        print("Runs are refused until credit is added" + (": `metaphi credits buy`." if o.get("can_buy") else
                                                         "; ask an owner or billing member to add it."))


def cmd_credits_buy(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    if not o.get("can_buy"):
        raise accounts.Error(f"only an owner or billing member of {o['id']} can buy its credit")
    got = accounts.buy(o["id"], a.amount, open_browser=not a.no_browser)
    print(f"Paid. {o['id']}: ${got['credits_usd']:,.2f} prepaid credit")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="metaphi", description="The Metaphi AI command line.")
    p.add_argument("--version", action="version", version=f"metaphi {__version__}")
    p.add_argument("--debug-keyring", nargs=0, action=_DebugKeyring, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    s = sub.add_parser("login", help="sign in through the browser and save your organization's keys (or save a key "
                       "you have with --with-api-key)")
    s.add_argument("--with-api-key", action="store_true",
                   help="save an organization key on this machine instead of signing in; reads it from stdin")
    s.add_argument("--api-key", action=_OnArgv, help=argparse.SUPPRESS)
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="sign out everywhere this sign-in is used, and revoke its keys").set_defaults(
        fn=cmd_logout)
    sub.add_parser("whoami", help="who you are signed in as, and your organizations").set_defaults(fn=cmd_whoami)

    s = sub.add_parser("credits", help="your organization's prepaid balance")
    s.add_argument("--org")
    s.set_defaults(fn=cmd_credits)
    credits = s.add_subparsers(dest="verb", metavar="<verb>")
    b = credits.add_parser("buy", help="add credit through Stripe Checkout (owners and billing members)")
    b.add_argument("--amount", type=_usd, default=100.0, help="dollars to add (default 100)")
    b.add_argument("--org")
    b.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    b.set_defaults(fn=cmd_credits_buy)

    s = sub.add_parser("twin", help="twin a trace (JSON, JSON Lines or text)")
    s.add_argument("file")
    s.add_argument("-o", "--output", help="where the twinned trace goes (default <name>.twin<ext>)")
    s.add_argument("--key", help="64 hex characters: the same key gives a person the same stand-in in every trace")
    s.add_argument("--confidential", action="append", help="an organization to replace although public; repeatable")
    s.add_argument("--test", action="store_true",
                   help="run on the test key `metaphi login` saved: free, small, kept apart from live twins")
    s.add_argument("--api-key", action=_OnArgv, help="deprecated: set METAPHI_API_KEY instead")
    s.add_argument("--json", action="store_true", help="print the result as JSON on stdout")
    s.add_argument("--timeout", type=_seconds, help="seconds to wait for the twin (default METAPHI_TWIN_TIMEOUT, or "
                   "7200)")
    s.set_defaults(fn=cmd_twin)
    return p


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        a.fn(a)
    except config.Error as e:
        # `error: <message> (code: <code>, request: <request id>)`; --json also puts the error object on stdout
        error = e if isinstance(e, errors.MetaphiError) else errors.MetaphiError(str(e))
        if getattr(a, "json", False):
            print(json.dumps({"error": error.to_dict()}, indent=2))
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
