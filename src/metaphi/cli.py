"""metaphi: the Metaphi AI command line.

    metaphi login                        sign in through the browser; your organization's keys come with it
    metaphi twin trace.json              twin a trace with that key

Made to be driven by an agent as much as typed: nothing waits on a question away from a terminal, `--json` puts
the result (or the error object) alone on stdout while progress goes to stderr, and an error that one command
clears names that command (`Next: ...`, `next_step` in JSON).
"""
from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
import time
import uuid
from pathlib import Path

from metaphi import __version__, accounts, config, errors, skill


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


def _out(a: argparse.Namespace, result: dict, *lines: str) -> None:
    """A command's result: the object alone on stdout under --json, else its lines."""
    print(json.dumps(result, indent=2) if getattr(a, "json", False) else "\n".join(x for x in lines if x))


def _progress(a: argparse.Namespace):
    """Where a command says what it is doing: stderr under --json, which keeps stdout the result alone."""
    return (lambda s: print(s, file=sys.stderr)) if getattr(a, "json", False) else print


def _signed_in(a: argparse.Namespace, got: dict) -> None:
    cfg = config.load()
    console = f"{config.accounts_url()}/console"
    if cfg.get("test_api_key"):
        _out(a, {"email": got.get("email"), "org": got.get("org"), "keys": True, "console": console,
                 "next_step": "metaphi twin <file> --test"},
             f"Signed in as {got.get('email')}.",
             f"Ready for {got.get('org')}: its live key and test key are saved on this machine.",
             "Next: metaphi twin <file>   (--test for a free test run)",
             "" if skill.installed() else "Working through Claude Code? `metaphi skill install` teaches it this CLI.",
             f"Usage, keys, members and billing: {console}")
        return
    why = got.get("no_api_key") or "the server sent none"
    _out(a, {"email": got.get("email"), "org": got.get("org"), "keys": False, "no_api_key": why, "console": console},
         f"Signed in as {got.get('email')}.",
         f"No key was saved: {why}.",
         "Signing in again changes nothing until that does; then `metaphi login` hands the keys over.",
         f"Usage, keys, members and billing: {console}")


def cmd_login(a: argparse.Namespace) -> None:
    key = a.api_key or (_read_key() if a.with_api_key else None)
    if a.with_api_key or a.api_key:
        if not key or not key.startswith(config.KEY_PREFIX):
            raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
        cfg = config.load()
        cfg["api_key"] = key
        config.save(cfg)
        _out(a, {"keys": True}, "key saved on this machine")
        return
    say = _progress(a)
    if a.complete:
        held = accounts.pending()
        if held is not None:
            say(f"Waiting for the approval of code {held['user_code']} at {held['verification_uri']}")
        # away from a terminal the caller is a tool call with a clock of its own: answer within it, to be called again
        _signed_in(a, accounts.finish(wait_s=a.timeout or (None if _interactive() else 60.0), at_once=True))
        return
    if a.no_wait or not (a.wait or _interactive()):
        # away from a terminal nobody reads a prompt that then blocks: hand the page and the code over, and stop
        held = accounts.pending() or accounts.start()   # asked twice, the code a person may hold stays the code
        if not a.no_browser:
            accounts.webbrowser.open(held["open"])
        _out(a, {"verification_uri": held["verification_uri"], "verification_uri_complete": held["open"],
                 "user_code": held["user_code"], "expires_in": int(held["expires"] - time.time()), "status": "pending",
                 "next_step": "metaphi login --complete"},
             f"Open {held['verification_uri']} and enter the code {held['user_code']}",
             f"(or open {held['open']})" if held["open"] != held["verification_uri"] else "",
             "Next: metaphi login --complete   (waits for the approval; safe to run again)")
        return
    _signed_in(a, accounts.login(say=say, open_browser=not a.no_browser))


def cmd_logout(a: argparse.Namespace) -> None:
    accounts.logout()
    print("Signed out.")


def _hint(key: str | None) -> str | None:
    return f"…{key[-4:]}" if key else None


def cmd_whoami(a: argparse.Namespace) -> None:
    """Who the server says this machine is, not what the config file remembers."""
    cfg = config.load()
    env = os.environ.get("METAPHI_API_KEY")
    key = env or cfg.get("api_key")
    if not cfg.get("token") and not key:
        raise accounts.need("not signed in; run `metaphi login`", "not_signed_in", "metaphi login")
    orgs, credit = [], {}
    if cfg.get("token"):
        orgs = accounts.orgs()   # a sign-in that has ended is refused here
        with contextlib.suppress(config.Error):   # a member who is not shown the balance
            credit = {o["id"]: o for o in accounts.org_credits()}
    got = {"email": cfg.get("email") if cfg.get("token") else None,
           "organizations": [{"id": o["id"], "role": o.get("role"), "grants": o.get("grants") or [],
                              **({"credits_usd": credit[o["id"]]["credits_usd"],
                                  "can_buy": bool(credit[o["id"]].get("can_buy"))} if o["id"] in credit else {})}
                             for o in orgs],
           "key": {"source": "METAPHI_API_KEY" if env else "saved", "hint": _hint(key)} if key else None,
           "test_key": _hint(cfg.get("test_api_key")),
           "console": f"{config.accounts_url()}/console"}
    if not key and cfg.get("no_api_key"):
        got["no_api_key"] = cfg["no_api_key"]
    lines = [f"person: {got['email'] or 'not signed in (a saved key only)'}"]
    for o in got["organizations"]:
        lines.append(f"org:    {o['id']} ({o['role']}) holds {', '.join(o['grants']) or 'nothing yet'}"
                     + (f"; ${o['credits_usd']:,.2f} prepaid credit" if "credits_usd" in o else ""))
    lines.append(f"key:    {got['key']['hint']} ({got['key']['source']})" if key else
                 f"key:    none ({got.get('no_api_key') or 'run `metaphi login`'})")
    if got["test_key"]:
        lines.append(f"test:   {got['test_key']}")
    _out(a, got, *lines)


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
    say = _progress(a)
    key = uuid.uuid4().hex  # one Idempotency-Key for both attempts: the retry after a top-up cannot start a second run

    def run() -> dict:
        return twin(Path(a.file), Path(a.output) if a.output else None, a.api_key, a.key or "",
                    a.confidential, say=say, deadline_s=a.timeout, test=a.test, idempotency_key=key,
                    detach=a.no_wait)
    try:
        got = run()
    except errors.BillingError as e:
        if e.code != "out_of_credit" or a.json or not _interactive() or not _refill(e):
            raise
        got = run()  # once: a second refusal is the error
    if a.json:
        print(json.dumps(got, indent=2))


def _when(at: object) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(at)) if isinstance(at, (int, float)) else "-"


def _line(t: dict) -> str:
    return f"{t['id']}  {t['status']:<9}  {_when(t.get('created'))}  {t.get('filename') or ''}".rstrip()


def cmd_twins_list(a: argparse.Namespace) -> None:
    from metaphi.twin import recent
    got = recent(a.limit, test=a.test)
    _out(a, {"data": got}, *([_line(t) for t in got] or ["no twins yet: metaphi twin <file>"]))


def cmd_twins_get(a: argparse.Namespace) -> None:
    from metaphi.twin import ENDED, back, get
    t = get(a.id, test=a.test)
    error = t.get("error") if isinstance(t.get("error"), dict) else {}
    _out(a, t, _line(t), *([f"error: {error.get('message')} (code: {error.get('code')})"] if error else []),
         *([] if t["status"] in ENDED and t["status"] != "completed" else [f"Next: {back(a.id, a.test)}"]))


def cmd_twins_wait(a: argparse.Namespace) -> None:
    from metaphi.twin import wait
    got = wait(a.id, Path(a.output) if a.output else None, say=_progress(a), deadline_s=a.timeout, test=a.test)
    if a.json:
        print(json.dumps(got, indent=2))


def _usd(value: str) -> float:
    try:
        usd = round(float(value), 2)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an amount: {value!r}") from None
    if not 1 <= usd <= 10000:
        raise argparse.ArgumentTypeError("an amount is between 1 and 10000 dollars")
    return usd


def _interactive() -> bool:
    """A person is at both ends: only then may a command ask a question."""
    return all(s is not None and s.isatty() for s in (sys.stdin, sys.stdout))


def _credit_url(e: errors.MetaphiError) -> str:
    """Where credit is added: the address the server sent, else the console's on the configured accounts host."""
    try:
        return config.check_url(e.action_url, "the add-credit page")
    except config.Error:
        return f"{config.accounts_url()}/console?add=credit"


def _buy(o: dict, usd: float, open_browser: bool = True) -> None:
    got = accounts.buy(o["id"], usd, open_browser=open_browser)
    print(f"Paid. {o['id']}: ${got['credits_usd']:,.2f} prepaid credit")


def _top_up(o: dict) -> bool:
    """Ask for an amount and take it through Checkout; False when the person declines (n, Ctrl-C, end of input)."""
    while True:
        try:
            said = input("Add credit now? Amount in $ [100]: ").strip().lstrip("$").lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if said in ("n", "no"):
            return False
        try:
            usd = _usd("100" if said in ("", "y", "yes") else said)
        except argparse.ArgumentTypeError as e:
            print(e)
            continue
        _buy(o, usd)
        return True


def _refill(e: errors.MetaphiError) -> bool:
    """Out of credit at a terminal: offer the top-up to someone who may buy it. True once it is paid."""
    try:
        o = accounts.credit_of(None)
    except Exception:  # noqa: BLE001 - not signed in, several organizations, accounts down: the refusal stands
        return False
    if o["credits_usd"] > 0:  # not the organization this key draws on
        return False
    if not o.get("can_buy"):
        e.advice = f"An owner or billing member of {o['id']} has to add credit: {_credit_url(e)}"
        return False
    print(f"{o['id']} is out of prepaid credit.")
    return _top_up(o)


def cmd_credits(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    out = o["credits_usd"] <= 0
    _out(a, {"org": o["id"], "credits_usd": o["credits_usd"], "can_buy": bool(o.get("can_buy")),
             **({"next_step": "metaphi credits buy"} if out and o.get("can_buy") else {})},
         f"{o['id']}: ${o['credits_usd']:,.2f} prepaid credit",
         *(["Runs are refused until credit is added" + (": `metaphi credits buy`." if o.get("can_buy") else
                                                       "; ask an owner or billing member to add it.")] if out else []))


def cmd_credits_buy(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    if not o.get("can_buy"):
        raise accounts.Error(f"only an owner or billing member of {o['id']} can buy its credit")
    _buy(o, a.amount, open_browser=not a.no_browser)


def cmd_skill_install(a: argparse.Namespace) -> None:
    path = skill.install(a.project, a.force)
    _out(a, {"path": str(path), "next_step": "metaphi skill show"},
         f"wrote {path}", "Claude Code now reaches for `metaphi` when a trace needs twinning; start a new session.")


def cmd_skill_remove(a: argparse.Namespace) -> None:
    path = skill.remove(a.project)
    _out(a, {"removed": str(path) if path else None}, f"removed {path}" if path else "no kennet skill there")


EXAMPLES = """\
examples:
  metaphi login                      sign in; your organization's keys are saved on this machine
  metaphi whoami                     who you are, your organization, its balance, the key in use
  metaphi twin trace.json --test     a free test twin, written to trace.twin.json
  metaphi twin trace.json --no-wait  start a twin and return its id
  metaphi twins list                 your organization's twins, newest first
  metaphi twins wait twin_...        come back for a twin: wait until it ends, write its file

from an agent or a script (no terminal): add --json; stdout is then the result or the error object alone.
  metaphi skill install              teach Claude Code this CLI (once per machine)
  metaphi skill show                 the guide an agent follows, for this version
  metaphi login --json               prints the page and the code, and returns
  metaphi login --complete           waits for the approval, saves the keys
"""


def _json_flag(s: argparse.ArgumentParser) -> None:
    s.add_argument("--json", action="store_true", help="print the result as JSON on stdout")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="metaphi", description="The Metaphi AI command line.", epilog=EXAMPLES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"metaphi {__version__}")
    p.add_argument("--debug-keyring", nargs=0, action=_DebugKeyring, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("login", help="sign in through the browser and save your organization's keys (or save a key "
                       "you have with --with-api-key)")
    s.add_argument("--with-api-key", action="store_true",
                   help="save an organization key on this machine instead of signing in; reads it from stdin")
    s.add_argument("--api-key", action=_OnArgv, help=argparse.SUPPRESS)
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    how = s.add_mutually_exclusive_group()
    how.add_argument("--no-wait", action="store_true", help="print the page and the code and return; "
                     "`metaphi login --complete` finishes it. The default away from a terminal")
    how.add_argument("--complete", action="store_true", help="wait for the approval of the sign-in under way and "
                     "save the keys")
    how.add_argument("--wait", action="store_true", help="wait for the approval in this call, even away from a "
                     "terminal")
    s.add_argument("--timeout", type=_seconds, help="with --complete: seconds to wait for the approval (default: "
                   "until the code expires at a terminal, 60 away from one); past it, run --complete again")
    _json_flag(s)
    s.set_defaults(fn=cmd_login)
    sub.add_parser("logout", help="sign out everywhere this sign-in is used, and revoke its keys").set_defaults(
        fn=cmd_logout)
    s = sub.add_parser("whoami", help="who the server says you are: your organizations, their balance, the key "
                       "in use")
    _json_flag(s)
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("credits", help="your organization's prepaid balance")
    s.add_argument("--org")
    _json_flag(s)
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
    s.add_argument("--no-wait", action="store_true", help="start the twin and return its id; `metaphi twins wait "
                   "<id>` comes back for it")
    _json_flag(s)
    s.add_argument("--timeout", type=_seconds, help="seconds to wait for the twin (default METAPHI_TWIN_TIMEOUT, or "
                   "7200)")
    s.set_defaults(fn=cmd_twin)

    s = sub.add_parser("twins", help="your organization's twins: list them, read one, come back for one")
    s.set_defaults(fn=lambda a: s.print_help())
    twins = s.add_subparsers(dest="verb", metavar="<verb>")
    v = twins.add_parser("list", help="the twins, newest first")
    v.add_argument("--limit", type=int, default=10, help="how many (default 10)")
    v.set_defaults(fn=cmd_twins_list)
    g = twins.add_parser("get", help="one twin as it stands now")
    g.add_argument("id")
    g.set_defaults(fn=cmd_twins_get)
    w = twins.add_parser("wait", help="wait until a twin ends and write its file")
    w.add_argument("id")
    w.add_argument("-o", "--output", help="where the twinned trace goes (default <name>.twin<ext> here)")
    w.add_argument("--timeout", type=_seconds, help="seconds to wait (default METAPHI_TWIN_TIMEOUT, or 7200)")
    w.set_defaults(fn=cmd_twins_wait)
    for verb in (v, g, w):
        verb.add_argument("--test", action="store_true", help="test twins: read with the saved test key")
        _json_flag(verb)

    s = sub.add_parser("skill", help="the Kennet skill: teach a coding agent (Claude Code) this CLI")
    s.set_defaults(fn=lambda a: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("install", help="write the skill where Claude Code reads it (~/.claude/skills/kennet)")
    v.add_argument("--project", action="store_true", help="into this directory's .claude/skills instead, to commit "
                   "for a team")
    v.add_argument("--force", action="store_true", help="replace a skill file there that metaphi did not write")
    _json_flag(v)
    v.set_defaults(fn=cmd_skill_install)
    verbs.add_parser("show", help="print the guide an agent follows, for this version").set_defaults(
        fn=lambda a: print(skill.GUIDE, end=""))
    v = verbs.add_parser("remove", help="take the skill away")
    v.add_argument("--project", action="store_true")
    _json_flag(v)
    v.set_defaults(fn=cmd_skill_remove)

    sub.add_parser("help", help="this help").set_defaults(fn=lambda a: p.print_help())
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a line is read when it is said, also down a pipe
        with contextlib.suppress(Exception):
            stream.reconfigure(line_buffering=True)
    p = parser()
    a = p.parse_args(argv)
    try:
        if a.command is None:  # bare `metaphi`: the help, and the one next step
            p.print_help()
            print("\nNext: metaphi twin <file>" if config.api_key() else "\nGet started: metaphi login")
            return 0
        a.fn(a)
    except config.Error as e:
        # `error: <message> (code: <code>, request: <request id>)`; --json also puts the error object on stdout
        error = e if isinstance(e, errors.MetaphiError) else errors.MetaphiError(str(e))
        advice = getattr(error, "advice", None)
        if error.code == "out_of_credit":  # where it is paid, and the command that pays it and waits
            error.action_url = _credit_url(error)
            error.next_step = None if advice else "metaphi credits buy"
        if getattr(a, "json", False):
            print(json.dumps({"error": error.to_dict()}, indent=2))
        print(f"error: {error}", file=sys.stderr)
        if error.code == "out_of_credit":
            print(advice or f"Add credit: {error.action_url}  or run: metaphi credits buy --amount <amount>",
                  file=sys.stderr)
        elif error.next_step:
            print(f"Next: {error.next_step}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
