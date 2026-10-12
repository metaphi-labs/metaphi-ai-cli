"""kennet: the command line for Kennet, Metaphi AI's RL infrastructure.

    kennet login                        sign in through the browser; your organization's keys come with it
    kennet org create "Acme"            make your organization, if you are in none; Metaphi AI approves it
    kennet twin trace.json              twin a trace with that key

Made to be driven by an agent as much as typed: nothing waits on a question away from a terminal, `--json` puts
the result (or the error object) alone on stdout while progress goes to stderr, and an error that one command
clears names that command (`Next: ...`, `next_step` in JSON).

Exit status: 0 done; 1 failed; 2 the command line was wrong; 3 a sign-in or a key is needed (`AUTH`); 4 the same
call may work later (`LATER`: the service is unavailable, rate limited, or a wait ran out).
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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from metaphi import __version__, accounts, config, errors, skill

FAILED, USAGE, AUTH, LATER = 1, 2, 3, 4


def status(error: errors.MetaphiError) -> int:
    """The exit status an error ends the command with."""
    if error.type == "auth_error":
        return AUTH
    return LATER if error.retryable or error.type == "rate_limit_error" else FAILED


class _OnArgv(argparse.Action):
    """A secret passed as an argument: still accepted, but it lands in shell history and `ps`, so say so."""

    def __call__(self, p: argparse.ArgumentParser, ns: argparse.Namespace, value: object, opt: str | None = None):
        print(f"kennet: warning: {opt} puts the key in your shell history and process list and will be removed; "
              "use METAPHI_API_KEY or `kennet login --with-api-key` (reads stdin)", file=sys.stderr)
        setattr(ns, self.dest, value)


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
    if cfg.get("test_api_key"):
        _out(a, {"email": got.get("email"), "org": got.get("org"), "keys": True,
                 "next_step": "kennet twin <file> --test"},
             f"Signed in as {got.get('email')}.",
             f"Ready for {got.get('org')}: its live key and test key are saved on this machine.",
             "Next: kennet twin <file>   (--test for a free test run)",
             "" if skill.installed() else "Working through Claude Code? `kennet skill install` teaches it this CLI.")
        return
    # signed in, and handed no key: the same refusal `whoami` and `twin` make, with nothing here left to run
    why = got.get("no_api_key") or "the server sent none"
    if not a.json:
        print("\n".join((f"Signed in as {got.get('email')}.", f"No key was saved: {why}.",
                         "`kennet login` picks the keys up once that changes, with no second sign-in.")))
    from metaphi.twin import no_key
    e = no_key({"no_api_key": why, "email": got.get("email"), "no_api_key_code": got.get("no_api_key_code")},
               again=False)
    e.context = {"email": got.get("email"), "org": got.get("org"), "keys": False,
                 "reason": got.get("no_api_key_code")}
    raise e


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
    if accounts.pending() is None and (got := accounts.pickup()) is not None:
        # signed in already: the keys come by the token this machine holds, and nobody opens a browser
        _signed_in(a, got)
        return
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
                 "next_step": "kennet login --complete"},
             f"Open {held['verification_uri']} and enter the code {held['user_code']}",
             f"(or open {held['open']})" if held["open"] != held["verification_uri"] else "",
             "Next: kennet login --complete   (waits for the approval; safe to run again)")
        return
    _signed_in(a, accounts.login(say=say, open_browser=not a.no_browser))


def cmd_logout(a: argparse.Namespace) -> None:
    accounts.logout()
    print("Signed out.")


def _hint(key: str | None) -> str | None:
    return f"…{key[-4:]}" if key else None


def _key_status(key: str | None, source: str) -> tuple[dict | None, errors.MetaphiError | None]:
    """A key as `whoami` shows it, {source, hint, status}, asked of the twin service: `status` is "ok" or the
    code a call on it would meet now; and that error."""
    if not key:
        return None, None
    from metaphi.twin import check
    e = check(key)
    return {"source": source, "hint": _hint(key), "status": "ok" if e is None else e.code or "error"}, e


def cmd_whoami(a: argparse.Namespace) -> None:
    """Who the servers say this machine is, not what the config file remembers: accounts for the person and their
    organizations, the twin service for each key."""
    cfg = config.load()
    env = os.environ.get("METAPHI_API_KEY")
    key = env or cfg.get("api_key")
    if not cfg.get("token") and not key:
        raise accounts.need("not signed in; run `kennet login`", "not_signed_in", "kennet login")
    orgs, credit = [], {}
    with ThreadPoolExecutor(max_workers=2) as pool:   # the keys are asked of the twin service meanwhile
        asked = (pool.submit(_key_status, key, "METAPHI_API_KEY" if env else "saved"),
                 pool.submit(_key_status, cfg.get("test_api_key"), "saved"))
        if cfg.get("token"):
            orgs = accounts.orgs()   # a sign-in that has ended is refused here
            with contextlib.suppress(config.Error):   # a member who is not shown the balance
                credit = {o["id"]: o for o in accounts.org_credits()}
        (live, refused), (test, _) = asked[0].result(), asked[1].result()
    got = {"email": cfg.get("email") if cfg.get("token") else None,
           "org": cfg.get("org") if cfg.get("token") else None,
           "organizations": [{"id": o["id"], "role": o.get("role"), "grants": o.get("grants") or [],
                              **({"admission": o["admission"]} if o.get("admission") else {}),
                              **({"credits_usd": credit[o["id"]]["credits_usd"],
                                  "can_buy": bool(credit[o["id"]].get("can_buy"))} if o["id"] in credit else {})}
                             for o in orgs],
           "key": live, "test_key": test}
    lines = [f"person: {got['email'] or 'not signed in (a saved key only)'}"]
    if got["org"] and got["org"] not in [o["id"] for o in got["organizations"]]:
        lines.append(f"org:    {got['org']}")
    for o in got["organizations"]:
        waits = o.get("admission") == "pending"
        lines.append(f"org:    {o['id']} ({o['role']}) "
                     + ("waits for Metaphi AI to approve it" if waits else
                        f"holds {', '.join(o['grants']) or 'nothing yet'}")
                     + (f"; ${o['credits_usd']:,.2f} prepaid credit" if "credits_usd" in o else ""))
    said = {"ok": "works"}
    lines.append(f"key:    {live['hint']} ({live['source']}): {said.get(live['status'], live['status'])}" if live else
                 f"key:    none ({cfg.get('no_api_key') or 'run `kennet login`'})")
    if test:
        lines.append(f"test:   {test['hint']}: {said.get(test['status'], test['status'])}")
    if live and refused is None:
        _out(a, got, *lines)
        return
    if not a.json:
        print("\n".join(lines))
    from metaphi.twin import no_key
    error = refused or no_key(cfg)
    error.context = got
    raise error


def _seconds(said: str) -> float:
    try:
        got = float(said)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{said!r} is not a number of seconds") from None
    if got <= 0:
        raise argparse.ArgumentTypeError("a number of seconds above zero")
    return got


def cmd_twin(a: argparse.Namespace) -> None:
    from metaphi.twin import consistency_key, twin
    say = _progress(a)
    key = uuid.uuid4().hex  # one Idempotency-Key for both attempts: the retry after a top-up cannot start a second run

    def run() -> dict:
        return twin(Path(a.file), Path(a.output) if a.output else None, a.api_key,
                    consistency_key(a.key, Path(a.key_file) if a.key_file else None),
                    a.confidential, say=say, deadline_s=a.timeout, test=a.test, idempotency_key=key,
                    detach=a.no_wait, out_dir=Path(a.out) if a.out else None)
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
    _out(a, {"data": got}, *([_line(t) for t in got] or ["no twins yet: kennet twin <file>"]))


def cmd_twins_get(a: argparse.Namespace) -> None:
    from metaphi.twin import ENDED, back, get
    t = get(a.id, test=a.test)
    error = t.get("error") if isinstance(t.get("error"), dict) else {}
    _out(a, t, _line(t), *([f"error: {error.get('message')} (code: {error.get('code')})"] if error else []),
         *([] if t["status"] in ENDED and t["status"] != "completed" else [f"Next: {back(a.id, a.test)}"]))


def cmd_twins_wait(a: argparse.Namespace) -> None:
    from metaphi.twin import wait
    got = wait(a.id, Path(a.output) if a.output else None, say=_progress(a), deadline_s=a.timeout, test=a.test,
               out_dir=Path(a.out) if a.out else None)
    if a.json:
        print(json.dumps(got, indent=2))


def _size(n: object) -> str:
    n = float(n) if isinstance(n, (int, float)) else 0.0
    return f"{n / 1e6:.1f} MB" if n >= 1e5 else f"{n / 1e3:.1f} kB"


def _delivery_line(d: dict) -> str:
    return (f"{d['id']}  {d.get('delivered') or '-'}  {d['task_count']} tasks  {d['image_count']} images  "
            f"{_size(d.get('bytes'))}" + (f"  (newer: {d['newer']})" if d.get("newer") else ""))


def cmd_delivery_list(a: argparse.Namespace) -> None:
    from metaphi import delivery
    got = delivery.held()
    _out(a, {"data": got}, *([_delivery_line(d) for d in got] or ["your organization holds no deliveries yet"]),
         *([f"Next: kennet delivery show {got[0]['id']}"] if got else []))


def _most(tasks: list, field: str) -> object:
    held = [t[field] for t in tasks if isinstance(t.get(field), (int, float))]
    return f"{max(held):g}" if held else "?"


def _tally(tasks: list, field: str) -> str:
    seen: dict[str, int] = {}
    for t in tasks:
        if t.get(field):
            seen[t[field]] = seen.get(t[field], 0) + 1
    return ", ".join(f"{k} {n}" for k, n in sorted(seen.items(), key=lambda kv: -kv[1]))


def cmd_delivery_show(a: argparse.Namespace) -> None:
    from metaphi import delivery
    d = delivery.show(a.id)
    tasks = d["tasks"]
    kinds = "; ".join(x for x in (_tally(tasks, "category"), _tally(tasks, "difficulty")) if x)
    lines = [f"{d['id']}  delivered {d.get('delivered') or '-'}",
             f"  tasks     {d['task_count']}" + (f"  ({kinds})" if kinds else ""),
             f"  images    {d['image_count']}, each pinned by digest, in {d.get('registry') or '-'}",
             f"  download  {_size(d.get('bytes'))}",
             (f"  a run     up to {_most(tasks, 'cpus')} CPUs, {_most(tasks, 'memory_mb')} MB memory, "
              f"{_most(tasks, 'storage_mb')} MB disk; the agent up to {_most(tasks, 'agent_timeout_s')} s, "
              f"grading up to {_most(tasks, 'verifier_timeout_s')} s"),
             *([f"  newer     {d['newer']}  (kennet delivery diff {d['id']} {d['newer']})"] if d.get("newer") else [])]
    if a.tasks:
        lines += [f"  {t['name']}  {t['tree'][7:19]}  {len(t['images'])} images  {_most([t], 'cpus')} CPUs  "
                  f"{_most([t], 'memory_mb')} MB  {_most([t], 'agent_timeout_s')} s" for t in tasks]
    _out(a, d, *lines, f"Next: kennet delivery pull {d['id']}")


def cmd_delivery_pull(a: argparse.Namespace) -> None:
    from metaphi import delivery
    got = delivery.pull(a.id, Path(a.to) if a.to else None, dry_run=a.dry_run, say=_progress(a))
    got["next_step"] = (f"kennet delivery pull {a.id} --to {got['dir']}" if a.dry_run
                        else f"kennet delivery verify {got['dir']}")
    n = len(got["fetched"])
    said = (f"{a.id} would fetch {n} of {got['tasks']} tasks into {got['dir']}; {got['unchanged']} are there already"
            if a.dry_run else
            f"{a.id} is at {got['dir']}: {got['tasks']} tasks ({n} fetched, {got['unchanged']} already there), "
            f"{_size(got['bytes'])} downloaded")
    _out(a, got, said, *([] if a.dry_run else [f"Lock: {got['lock']}"]), f"Next: {got['next_step']}")


def cmd_delivery_verify(a: argparse.Namespace) -> int:
    from metaphi import delivery
    got = delivery.verify(Path(a.dir))
    lines = []
    for t in got["differ"]:
        lines.append(f"{t['state']:<8} {t['name']}"
                     + (": a file's mode differs; its bytes are as delivered" if t["modes_only"] else ""))
        lines += [f"  {f['state']:<8} {f['path']}" + "".join(
            f"  {said} {f[k][:12]}" for k, said in (("delivered", "delivered"), ("here", "here")) if f.get(k))
            for f in t["files"]]
    lines += [f"added    {name}  (not in the delivery)" for name in got["added"]]
    lines += [f"{got['sums']:<8} SHA256SUMS"] if got["sums"] != "as_delivered" else []
    lines.append(f"{got['id']}: {got['as_delivered']} of {got['tasks']} tasks as delivered")
    _out(a, got, *lines, *([f"Next: {got['next_step']}"] if got["next_step"] else []))
    return 0 if got["ok"] else FAILED


def _until(at: object) -> str:
    return time.strftime("%H:%M", time.localtime(at)) if isinstance(at, (int, float)) else "?"


def cmd_registry_login(a: argparse.Namespace) -> None:
    from metaphi import delivery
    got = delivery.registry_login(a.client)
    got["next_step"] = "kennet delivery list"
    _out(a, got, f"{got['client']} is signed in to {got['server']} until {_until(got['expires_at'])} (one hour).",
         f"Your images: {got['repository']}/<image>@sha256:...  as each delivery's lock names them",
         "Run this again when a pull is refused; nothing here is a stored key.")


def cmd_registry_token(a: argparse.Namespace) -> None:
    from metaphi import delivery
    got = delivery.registry_token()
    _out(a, got, got["token"])


def cmd_delivery_diff(a: argparse.Namespace) -> None:
    from metaphi import delivery
    got = delivery.diff(a.a, a.b)
    lines = [f"added    {n}" for n in got["added"]] + [f"removed  {n}" for n in got["removed"]]
    for t in got["changed"]:
        what = "grading changed: earlier scores on it do not carry over" if t["grading_changed"] else \
            "grading unchanged"
        lines.append(f"changed  {t['name']}  ({what}; {len(t['files'])} files"
                     + (", images" if t["images_changed"] else "") + ")")
    lines.append(f"{got['from']} -> {got['to']}: {len(got['added'])} added, {len(got['removed'])} removed, "
                 f"{len(got['changed'])} changed ({len(got['grading_changed'])} in their grading), "
                 f"{got['unchanged']} unchanged")
    _out(a, got, *lines, f"Next: kennet delivery pull {got['to']}")


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


def cmd_org_create(a: argparse.Namespace) -> None:
    made = accounts.create_org(a.name)
    ready = made.get("admission") == "admitted"
    _out(a, {"id": made["id"], "name": made.get("name"), "role": made.get("role"),
             "admission": made.get("admission"), "next_step": "kennet login"},
         str(made.get("message") or f"{made['id']} is made and you own it."),
         "Next: kennet login" + ("" if ready else "   (once it is approved; it says so until then)"))


def cmd_credits(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    out = o["credits_usd"] <= 0
    _out(a, {"org": o["id"], "credits_usd": o["credits_usd"], "can_buy": bool(o.get("can_buy")),
             **({"next_step": "kennet credits buy"} if out and o.get("can_buy") else {})},
         f"{o['id']}: ${o['credits_usd']:,.2f} prepaid credit",
         *(["Runs are refused until credit is added" + (": `kennet credits buy`." if o.get("can_buy") else
                                                       "; ask an owner or billing member to add it.")] if out else []))


def cmd_credits_buy(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    if not o.get("can_buy"):
        raise accounts.Error(f"only an owner or billing member of {o['id']} can buy its credit")
    _buy(o, a.amount, open_browser=not a.no_browser)


def cmd_skill_install(a: argparse.Namespace) -> None:
    path = skill.install(a.project, a.force)
    _out(a, {"path": str(path), "next_step": "kennet skill show"},
         f"wrote {path}", "Claude Code now reaches for `kennet` when a trace needs twinning; start a new session.")


def cmd_skill_remove(a: argparse.Namespace) -> None:
    path = skill.remove(a.project)
    _out(a, {"removed": str(path) if path else None}, f"removed {path}" if path else "no kennet skill there")


EXAMPLES = """\
examples:
  kennet login                      sign in; your organization's keys are saved on this machine
  kennet org create "Acme"          make your organization, if you are in none; Metaphi AI approves it
  kennet whoami                     who you are, your organization, its balance, the key in use
  kennet twin trace.json --test     a free test twin, written to trace.twin.json
  kennet twin trace.json --no-wait  start a twin and return its id
  kennet twin trace.json --out twins/   write the twin into a directory
  kennet twins list                 your organization's twins, newest first
  kennet twins wait twin_...        come back for a twin: wait until it ends, write its file
  kennet delivery list              the task sets your organization holds
  kennet pull eswe-mini@0.1.0       fetch one into ./eswe-mini-0.1.0; again, only what differs
  kennet verify ./eswe-mini-0.1.0   check it against its lock, offline
  kennet registry login             sign docker in to pull its images, for an hour

from an agent or a script (no terminal): add --json; stdout is then the result or the error object alone.
  kennet skill install              teach Claude Code this CLI (once per machine)
  kennet skill show                 the guide an agent follows, for this version
  kennet login --json               prints the page and the code, and returns
  kennet login --complete           waits for the approval, saves the keys
  METAPHI_API_KEY=mai_sk_... kennet twin trace.json --json    no sign-in: a key from the console

exit status: 0 done, 1 failed, 2 usage, 3 sign-in or key needed, 4 try the same call later
"""


def _json_flag(s: argparse.ArgumentParser) -> None:
    s.add_argument("--json", action="store_true", help="print the result as JSON on stdout")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kennet", description="Kennet, Metaphi AI's RL infrastructure, from the command line.", epilog=EXAMPLES,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"kennet {__version__}")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    s = sub.add_parser("login", help="sign in through the browser and save your organization's keys (or save a key "
                       "you have with --with-api-key)")
    s.add_argument("--with-api-key", action="store_true",
                   help="save an organization key on this machine instead of signing in; reads it from stdin")
    s.add_argument("--api-key", action=_OnArgv, help=argparse.SUPPRESS)
    s.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    how = s.add_mutually_exclusive_group()
    how.add_argument("--no-wait", action="store_true", help="print the page and the code and return; "
                     "`kennet login --complete` finishes it. The default away from a terminal")
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
    s = sub.add_parser("whoami", help="who the servers say you are: your organizations, their balance, and whether "
                       "the key in use works")
    _json_flag(s)
    s.set_defaults(fn=cmd_whoami)

    s = sub.add_parser("org", help="your organization: make it, if you are in none")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("create", help="make an organization and own it; Metaphi AI approves it for Kennet and "
                         "mails you, then `kennet login` saves its keys")
    v.add_argument("name", help="what it is called, e.g. \"Acme Robotics\"")
    _json_flag(v)
    v.set_defaults(fn=cmd_org_create)

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
    where = s.add_mutually_exclusive_group()
    where.add_argument("-o", "--output", help="where the twinned trace goes (default <name>.twin<ext> beside the file)")
    where.add_argument("--out", metavar="DIR", help="the directory the twin goes in, as <name>.twin<ext>; made if "
                       "missing")
    keyed = s.add_mutually_exclusive_group()
    keyed.add_argument("--key-file", metavar="PATH", help="a file holding the consistency key (64 hex characters): "
                       "the same key gives a person the same stand-in in every trace. KENNET_CONSISTENCY_KEY is "
                       "read when neither this nor --key is given")
    keyed.add_argument("--key", help="the consistency key itself; it shows in the process list, so prefer --key-file")
    s.add_argument("--confidential", action="append", help="an organization to replace although public; repeatable")
    s.add_argument("--test", action="store_true",
                   help="run on the test key `kennet login` saved: free, small, kept apart from live twins")
    s.add_argument("--api-key", action=_OnArgv, help="deprecated: set METAPHI_API_KEY instead")
    s.add_argument("--no-wait", action="store_true", help="start the twin and return its id; `kennet twins wait "
                   "<id>` comes back for it")
    _json_flag(s)
    s.add_argument("--timeout", type=_seconds, help="seconds to wait for the twin (default METAPHI_TWIN_TIMEOUT, or "
                   "7200)")
    s.set_defaults(fn=cmd_twin)

    s = sub.add_parser("twins", help="your organization's twins: list them, read one, come back for one")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    twins = s.add_subparsers(dest="verb", metavar="<verb>")
    v = twins.add_parser("list", help="the twins, newest first")
    v.add_argument("--limit", type=int, default=10, help="how many (default 10)")
    v.set_defaults(fn=cmd_twins_list)
    g = twins.add_parser("get", help="one twin as it stands now")
    g.add_argument("id")
    g.set_defaults(fn=cmd_twins_get)
    w = twins.add_parser("wait", help="wait until a twin ends and write its file")
    w.add_argument("id")
    where = w.add_mutually_exclusive_group()
    where.add_argument("-o", "--output", help="where the twinned trace goes (default <name>.twin<ext> here)")
    where.add_argument("--out", metavar="DIR", help="the directory the twin goes in, as <name>.twin<ext>; made if "
                       "missing")
    w.add_argument("--timeout", type=_seconds, help="seconds to wait (default METAPHI_TWIN_TIMEOUT, or 7200)")
    w.set_defaults(fn=cmd_twins_wait)
    for verb in (v, g, w):
        verb.add_argument("--test", action="store_true", help="test twins: read with the saved test key")
        _json_flag(verb)

    s = sub.add_parser("delivery", help="the task sets your organization holds: list, show, pull, verify, diff")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="delivery_command", metavar="<command>")
    v = verbs.add_parser("list", help="the deliveries your organization holds, newest first")
    _json_flag(v)
    v.set_defaults(fn=cmd_delivery_list)
    v = verbs.add_parser("show", help="one delivery: its tasks, images and what a run of it asks")
    v.add_argument("id", metavar="<name@version>")
    v.add_argument("--tasks", action="store_true", help="a line for every task")
    _json_flag(v)
    v.set_defaults(fn=cmd_delivery_show)

    def _pull(v: argparse.ArgumentParser) -> None:
        v.add_argument("id", metavar="<name@version>")
        v.add_argument("--to", metavar="DIR", help="where it goes (default: ./<name>-<version>)")
        v.add_argument("--dry-run", action="store_true", help="say what would be fetched, and fetch nothing")
        _json_flag(v)
        v.set_defaults(fn=cmd_delivery_pull)

    def _verify(v: argparse.ArgumentParser) -> None:
        v.add_argument("dir", metavar="<dir>", help="a pulled delivery")
        _json_flag(v)
        v.set_defaults(fn=cmd_delivery_verify)

    _pull(verbs.add_parser("pull", help="fetch a delivery into a directory; again, it fetches only what differs"))
    _verify(verbs.add_parser("verify", help="check a pulled delivery against its lock: offline, exit 1 if it "
                                            "differs"))
    v = verbs.add_parser("diff", help="what changed between two versions, and whether a task's grading did")
    v.add_argument("a", metavar="<from>", help="a delivery (<name@version>) or a pulled directory")
    v.add_argument("b", metavar="<to>", help="a delivery (<name@version>) or a pulled directory")
    _json_flag(v)
    v.set_defaults(fn=cmd_delivery_diff)
    s = sub.add_parser("registry", help="your organization's image repository: sign a container client in for an "
                                        "hour, or print the token")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="registry_command", metavar="<command>")
    v = verbs.add_parser("login", help="sign docker (or podman, nerdctl) in to pull your deliveries' images, "
                                       "for an hour")
    v.add_argument("--client", metavar="<name>", help="the client to sign in; default: the first of docker, "
                                                      "podman, nerdctl found")
    _json_flag(v)
    v.set_defaults(fn=cmd_registry_login)
    v = verbs.add_parser("token", help="print the hour's token alone, for whatever else pulls your images "
                                       "(--json: with its server, repository, username and expiry)")
    _json_flag(v)
    v.set_defaults(fn=cmd_registry_token)
    _pull(sub.add_parser("pull", help="the same as `delivery pull`"))
    _verify(sub.add_parser("verify", help="the same as `delivery verify`"))

    s = sub.add_parser("skill", help="the Kennet skill: teach a coding agent (Claude Code) this CLI")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("install", help="write the skill where Claude Code reads it (~/.claude/skills/kennet)")
    v.add_argument("--project", action="store_true", help="into this directory's .claude/skills instead, to commit "
                   "for a team")
    v.add_argument("--force", action="store_true", help="replace a skill file there that kennet did not write")
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
        if a.command is None:  # bare `kennet`: the help, and the one next step
            p.print_help()
            print("\nNext: kennet twin <file>" if config.api_key() else "\nGet started: kennet login")
            return 0
        ended = a.fn(a)
        if isinstance(ended, int) and ended:   # the command ran and its answer is no: `verify` on a changed tree
            return ended
    except config.Error as e:
        # `error: <message> (code: <code>, request: <request id>)`; --json also puts the error object on stdout
        error = e if isinstance(e, errors.MetaphiError) else errors.MetaphiError(str(e))
        advice = getattr(error, "advice", None)
        if error.code == "out_of_credit":  # where it is paid, and the command that pays it and waits
            error.action_url = _credit_url(error)
            error.next_step = None if advice else "kennet credits buy"
        elif error.code in ("invalid_api_key", "missing_api_key"):   # a key revoked or replaced: a sign-in saves new ones
            error.next_step = "kennet login"
        if getattr(a, "json", False):
            print(json.dumps({"error": error.to_dict(), **error.context}, indent=2))
        print(f"error: {error}", file=sys.stderr)
        if error.code == "out_of_credit":
            print(advice or f"Add credit: {error.action_url}  or run: kennet credits buy --amount <amount>",
                  file=sys.stderr)
        elif error.next_step:
            print(f"Next: {error.next_step}", file=sys.stderr)
        elif error.retry_after_s is not None:
            print(f"Try again in {error.retry_after_s:.0f}s.", file=sys.stderr)
        return status(error)
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
