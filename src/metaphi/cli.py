"""kennet: the command line for Kennet, Metaphi AI's RL infrastructure.

    kennet login                        sign in through the browser; your organization's keys come with it
    kennet org create "Acme"            make your organization, if you are in none; Metaphi AI approves it
    kennet org join acme                accept the invitation an organization sent you; its keys come with it
    kennet org members                  its members; `add`, `role`, `remove` change them (owners and admins)
    kennet keys create ci | ...         a service key for CI, piped into its secret; `list`, `revoke`
    kennet twin trace.json              twin a trace with that key

Made to be driven by an agent as much as typed: nothing waits on a question away from a terminal, `--json` puts
the result (or the error object) alone on stdout while progress goes to stderr, and an error that one command
clears names that command (`Next: ...`, `next_step` in JSON).

Exit status: 0 done; 1 failed; 2 the command line was wrong; 3 a sign-in or a key is needed (`AUTH`); 4 the same
call may work later (`LATER`: the service is unavailable or unreachable, rate limited, or a wait ran out); 5 this
version is too old for the service (`UPGRADE`: `upgrade_required`, and `next_step` is the upgrade command for this
install). Whatever goes wrong ends in one line and a next step, never a traceback: a network that fails is `network`,
and anything the CLI did not expect is `internal`, its traceback in the debug log the line names.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import getpass
import json
import math
import os
import re
import shlex
import sys
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from metaphi import __version__, accounts, admin, agents, config, errors, skill, update

FAILED, USAGE, AUTH, LATER, UPGRADE = 1, 2, 3, 4, 5


def status(error: errors.MetaphiError) -> int:
    """The exit status an error ends the command with."""
    if error.code == "upgrade_required":
        return UPGRADE
    if error.code == "test_runs_used":   # the service calls it a limit; it is spent, and no wait brings it back
        return FAILED
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


def _invitation_line(i: dict) -> str:
    return (f"{i.get('org_name') or i['org']} has invited you to join it"
            + (f" as {i['role']}" if i.get("role") else "") + (f", until {_when(i['expires'])}" if i.get("expires")
                                                               else "") + "."
            + (f" ({i['id']})" if i.get("id") else ""))


def _signed_in(a: argparse.Namespace, got: dict, joined: dict | None = None) -> None:
    cfg = config.load()
    said = f"You joined {joined['org']}" + (f" as {joined['role']}." if joined.get("role") else ".") if joined else ""
    if cfg.get("test_api_key"):
        _out(a, {"email": got.get("email"), "org": got.get("org"), "keys": True,
                 **({"joined": joined} if joined else {}), "next_step": "kennet twin <file> --test"},
             said, f"Signed in as {got.get('email')}.",
             f"Ready for {got.get('org')}: its live key and test key are saved on this machine.",
             "Next: kennet twin <file>   (--test for a free test run)",
             "" if agents.skill_installed() else "Working through a coding agent? `kennet agents install` teaches it "
             "this CLI.")
        return
    # signed in, and handed no key: the same refusal `whoami` and `twin` make, with nothing here left to run
    why = got.get("no_api_key") or "the server sent none"
    from metaphi.twin import no_key
    e = no_key({**got, "no_api_key": why}, again=False, fresh=accounts.invited(got))
    reason = e.context.get("reason")
    if not a.json:
        lines = [said, f"Signed in as {got.get('email')}."]
        if reason not in ("invitation_pending", "removed"):   # those the error says whole, with what clears it
            lines += [f"No key was saved: {why}.",
                      "`kennet login` picks the keys up once that changes, with no second sign-in."]
        print("\n".join(x for x in lines if x))
    e.context = {"email": got.get("email"), "org": got.get("org"), "keys": False, **e.context,
                 **({"joined": joined} if joined else {})}
    raise e


def cmd_login(a: argparse.Namespace) -> None:
    if a.json:   # a caller reading JSON is a program: the page and the code are handed over, never a browser tab
        a.no_browser = True
    key = a.api_key or (_read_key() if a.with_api_key else None)
    if a.with_api_key or a.api_key:
        if not key or not key.startswith(config.KEY_PREFIX):
            raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
        cfg = config.load()
        cfg["api_key"] = key
        cfg.pop("api_key_from", None)   # saved by hand: a sign-in handed no key never takes it away
        config.save(cfg)
        _out(a, {"keys": True}, "key saved on this machine")
        return
    say = _progress(a)
    # a refusal for a sign-in too old to change an organization (`step_up_required`) asks for a fresh one, not a pickup
    if accounts.pending() is None and not config.load().get("step_up") and (got := accounts.pickup()) is not None:
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


def _invitations(cfg: dict, orgs: list) -> list[dict]:
    """The invitations open to the signed-in person, as accounts lists them now. Only when it cannot say does the
    one the last sign-in saved stand in, while it has not expired and the person is in no organization."""
    try:
        return accounts.invitations()
    except config.Error:
        held = accounts.invited(cfg)
        return [held] if held and not accounts.expired(held) and not orgs else []


def cmd_whoami(a: argparse.Namespace) -> None:
    """Who the servers say this machine is, not what the config file remembers: accounts for the person and their
    organizations, the twin service for each key."""
    cfg = config.load()
    env = os.environ.get("METAPHI_API_KEY")
    key = env or cfg.get("api_key")
    if not cfg.get("token") and not key:
        raise accounts.need("not signed in; run `kennet login`", "not_signed_in", "kennet login")
    orgs, credit, left = [], {}, {}
    with ThreadPoolExecutor(max_workers=2) as pool:   # the keys are asked of the twin service meanwhile
        asked = (pool.submit(_key_status, key, "METAPHI_API_KEY" if env else "saved"),
                 pool.submit(_key_status, cfg.get("test_api_key"), "saved"))
        if cfg.get("token"):
            orgs = accounts.orgs()   # a sign-in that has ended is refused here
            with contextlib.suppress(config.Error):   # a member who is not shown the balance
                credit = {o["id"]: o for o in accounts.org_credits()}
            for o in orgs:
                with contextlib.suppress(config.Error):   # an accounts that does not say it yet
                    left[o["id"]] = accounts.available(o["id"])
        (live, refused), (test, _) = asked[0].result(), asked[1].result()
    gone = refused is not None and refused.code in ("invalid_api_key", "missing_api_key")
    invitations = _invitations(cfg, orgs) if cfg.get("token") else []
    invitation = invitations[0] if invitations and (live is None or refused is not None) else None
    cfg = {k: v for k, v in cfg.items() if k != "invitation"}   # what accounts says now, never the saved one
    if invitation is not None:
        if gone:
            live, refused = None, None   # the key is spent for good; what clears it is the invitation
    elif gone and cfg.get("token") and not env:
        # the saved key is refused: why, from what the sign-in may read and nothing that changes a key (asking
        # `POST /keys/cli` would replace the keys of every machine the person signed in on)
        refused.message = ("this machine's key was replaced by a sign-in elsewhere; run `kennet login`" if orgs else
                           "this machine's key no longer works and you're not in an organization; run "
                           "`kennet login` to see why")
    got = {"email": cfg.get("email") if cfg.get("token") else None,
           "org": cfg.get("org") if cfg.get("token") else None,
           "organizations": [{"id": o["id"], "role": o.get("role"), "grants": o.get("grants") or [],
                              **({"admission": o["admission"]} if o.get("admission") else {}),
                              **({k: left[o["id"]].get(k) for k in ("available_usd", "bound", "resets")}
                                 if o["id"] in left else {}),
                              **({"credits_usd": credit[o["id"]]["credits_usd"],
                                  "can_buy": bool(credit[o["id"]].get("can_buy"))} if o["id"] in credit else {})}
                             for o in orgs],
           "key": live, "test_key": test, **({"invitations": invitations} if invitations else {})}
    lines = [f"person: {got['email'] or 'not signed in (a saved key only)'}"]
    if got["org"] and got["org"] not in [o["id"] for o in got["organizations"]]:
        lines.append(f"org:    {got['org']}")
    for o in got["organizations"]:
        waits = o.get("admission") == "pending"
        lines.append(f"org:    {o['id']} ({o['role']}) "
                     + ("waits for Metaphi AI to approve it" if waits else
                        f"holds {', '.join(o['grants']) or 'nothing yet'}"))
        if "bound" in o:
            lines.append(f"you:    {_left(o)}")
        if "credits_usd" in o:
            lines.append(f"balance: {_money(o['credits_usd'])}")
    lines += [f"invite: {_invitation_line(i)}" for i in invitations]
    said = {"ok": "works"}
    lines.append(f"key:    {live['hint']} ({live['source']}): {said.get(live['status'], live['status'])}" if live else
                 "key:    none" + ("" if invitation or cfg.get("no_api_key_code") == "removed" else
                                   f" ({cfg.get('no_api_key') or 'run `kennet login`'})"))
    if test:
        lines.append(f"test:   {test['hint']}: {said.get(test['status'], test['status'])}")
    if live and refused is None:
        _out(a, got, *lines)
        return
    if not a.json:
        print("\n".join(lines))
    from metaphi.twin import no_key
    error = refused or no_key(cfg, fresh=invitation)
    error.context = {**error.context, **got}
    raise error


def _left(o: dict) -> str:
    """What the person may spend now in the organization, and what bounds it, as whoami says it to every role."""
    if o["available_usd"] is None:
        return "no limit"
    if o["bound"] == "balance":
        return f"{_money(o['available_usd'])} left (org balance)"
    day = datetime.date.fromisoformat(o["resets"])
    whose = "your" if o["bound"] == "limit" else "org"
    return f"{_money(o['available_usd'])} left ({whose} monthly limit, resets {day:%b} {day.day})"


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
        try:
            got = run()
        except errors.BillingError as e:
            if e.code != "out_of_credit" or a.json or not _interactive() or not _refill(e):
                raise
            got = run()  # once: a second refusal is the error
    except errors.MetaphiError as e:
        if e.code == "test_runs_used":   # the live run of the same file, as it was asked for
            e.next_step = _live(a)
            e.message = f"its free test run is used; run it live: {e.next_step}"
        raise
    if a.json:
        print(json.dumps(got, indent=2))


def _live(a: argparse.Namespace) -> str:
    """`kennet twin` as it was run, live: every flag but --test and the secrets (--api-key, --key), which are never
    echoed; METAPHI_API_KEY and KENNET_CONSISTENCY_KEY carry those."""
    words = ["kennet", "twin", a.file]
    for flag, value in (("-o", a.output), ("--out", a.out), ("--key-file", a.key_file)):
        if value:
            words += [flag, value]
    for name in a.confidential or []:
        words += ["--confidential", name]
    if a.no_wait:
        words.append("--no-wait")
    if a.timeout:
        words += ["--timeout", f"{a.timeout:g}"]
    return shlex.join(words)


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
    got["next_step"] = (f"kennet delivery pull {got['id']} --to {got['dir']}" if a.dry_run
                        else f"kennet delivery verify {got['dir']}")
    n = len(got["fetched"])
    said = (f"{got['id']} would fetch {n} of {got['tasks']} tasks into {got['dir']}; {got['unchanged']} are there already"
            if a.dry_run else
            f"{got['id']} is at {got['dir']}: {got['tasks']} tasks ({n} fetched, {got['unchanged']} already there), "
            f"{_size(got['bytes'])} downloaded")
    _out(a, got, said, *([] if a.dry_run else [f"Lock: {got['lock']}"]),
         *([f"Newer: {got['newer']} is out  (kennet delivery diff {got['id']} {got['newer']})"] if got["newer"] else []),
         f"Next: {got['next_step']}")


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
    print(f"Paid. {o['id']}: balance {_money(got['credits_usd'])}")


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
    print(f"{o['id']} is out of credit.")
    return _top_up(o)


APPROVAL = "Metaphi AI approves new organizations; you're emailed when it's done, then run `kennet login`"


def cmd_org_create(a: argparse.Namespace) -> None:
    made = accounts.create_org(a.name)
    ready = made.get("admission") == "admitted"
    message = str(made.get("message") or (f"{made['id']} is made and you own it." if ready else APPROVAL))
    # the server says what follows: `kennet login` once it is approved, nothing to run while it waits for the mail.
    # One that does not say: the same, from `admission`
    step = made.get("next_step", "kennet login" if ready else None)
    step = step if isinstance(step, str) and step else None
    _out(a, {"id": made["id"], "name": made.get("name"), "role": made.get("role"),
             "admission": made.get("admission"), "message": message, "next_step": step},
         message, f"Next: {step}" if step else "")


def cmd_org_join(a: argparse.Namespace) -> None:
    joined = _stepped_up(a, lambda: accounts.join(a.org))
    got = accounts.pickup()
    if got is None:   # joined, and the sign-in ended meanwhile: a fresh one hands the keys over
        raise accounts.need(f"you joined {joined['org']}; your sign-in has ended, so its keys did not come",
                            "not_signed_in", "kennet login")
    _signed_in(a, got, joined)


def _stepped_up(a: argparse.Namespace, change):
    """A change to the organization; refused for a sign-in too old (`step_up_required`), a person at a terminal is
    offered a fresh sign-in and the change is tried once more. Away from one, the refusal names `kennet login`."""
    try:
        return change()
    except accounts.Error as e:
        if e.code != "step_up_required" or a.json or not _interactive():
            raise
        try:
            said = input(f"{e.message}\nSign in again now? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            raise e from None
        if said not in ("", "y", "yes"):
            raise
        accounts.login()
        return change()   # once: a second refusal is the error


def _members(o: dict) -> tuple[list[dict], list[dict]]:
    """An organization's members [{email, role}], owners first, and its open invitations [{id, email, role, expires}]."""
    held = o.get("members") or {}
    members = sorted(({"email": m, "role": r} for m, r in held.items()),
                     key=lambda m: (accounts.ROLES.index(m["role"]) if m["role"] in accounts.ROLES else 9, m["email"]))
    invitations = [{k: i.get(k) for k in ("id", "email", "role", "expires")} for i in o.get("invitations") or []]
    return members, invitations


def _member_lines(members: list[dict], invitations: list[dict]) -> list[str]:
    width = max([len(m["email"]) for m in members + invitations] or [0])
    return ([f"  {m['email']:<{width}}  {m['role']}" for m in members]
            + [f"  {i['email']:<{width}}  {i['role']}  invited, until {_when(i.get('expires'))}" for i in invitations])


def cmd_org_members(a: argparse.Namespace) -> None:
    o = accounts.managed(a.org)
    members, invitations = _members(o)
    _out(a, {"org": o["id"], "role": o.get("role"), "members": members, "invitations": invitations},
         f"{o['id']} (you: {o.get('role')})", *_member_lines(members, invitations))


def cmd_org_members_set(a: argparse.Namespace) -> None:
    """`add` and `role`: one PUT, which adds a person in the role or changes a member's."""
    o = accounts.managed(a.org)
    email = a.email.strip().lower()
    try:
        got = _stepped_up(a, lambda: accounts.set_member(o["id"], email, a.role))
    except accounts.Error as e:
        if getattr(e, "status", None) != 503:
            raise
        # the address is off the organization's domains and the invitation cannot be mailed from here
        raise accounts.Error(f"the invitation to {email} could not be mailed; invite them on the keys page: "
                             f"{config.accounts_url()}/keys", code="mail_unavailable") from None
    members, _ = _members(got)
    result = {"org": o["id"], "email": email, "role": a.role}
    invited = got.get("invited") if isinstance(got.get("invited"), dict) else {}
    if email in invited and email not in (got.get("members") or {}):
        # off the organization's domains: the server mails an invitation, accepted signed in as that address
        _out(a, {**result, "status": "invited", "invitation": got.get("id"), "members": members},
             f"invitation mailed to {email}; they accept it signed in as that address")
        return
    _out(a, {**result, "status": "member", "members": members}, f"{email} is {a.role} of {o['id']}")


def cmd_org_members_remove(a: argparse.Namespace) -> None:
    o = accounts.managed(a.org)
    email = a.email.strip().lower()
    got = _stepped_up(a, lambda: accounts.remove_member(o["id"], email))
    members, _ = _members(got)
    uninvited = "invited" in got   # the address was invited, not a member: the invitation is taken back
    _out(a, {"org": o["id"], "email": email, "status": "uninvited" if uninvited else "removed", "members": members},
         f"{email}'s invitation to {o['id']} is taken back" if uninvited else f"{email} is no longer in {o['id']}")


KEY_NAME_MAX = 64   # accounts' `NAME_MAX`


def _key_name(said: str) -> str:
    name = said.strip()
    if not 0 < len(name) <= KEY_NAME_MAX:
        raise argparse.ArgumentTypeError(f"a key is named, in at most {KEY_NAME_MAX} characters")
    return name


def _key(k: dict) -> dict:
    """A key as `kennet keys` says it: never the key, never its hash."""
    return {f: k.get(f) for f in ("id", "name", "hint", "scopes", "livemode", "owner", "created_by", "created",
                                  "last_used", "revoked", "revoked_by")}


def _rows(rows: list[tuple]) -> list[str]:
    """A table: each column as wide as its widest cell, the last one as it is."""
    width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(c.ljust(w) for c, w in zip(r, width)) + "  " + r[-1] for r in rows]


def _key_lines(held: list[dict]) -> list[str]:
    rows = [("NAME", "ID", "KEY", "MODE", "SCOPES", "CREATED", "LAST USED", "BY")] + [
        (k["name"], k["id"], k["hint"], "live" if k["livemode"] else "test", ",".join(k["scopes"] or []) or "-",
         _when(k["created"]), _when(k["last_used"]) if k["last_used"] else "never",
         ("terminal " if k["owner"] == "cli" else "") + str(k["created_by"] or "-")
         + (f"  revoked {_when(k['revoked'])}" if k["revoked"] else "")) for k in held]
    return _rows(rows)


def _org_keys(a: argparse.Namespace) -> tuple[dict, list[dict]]:
    """The organization a keys command acts on, and its keys as accounts lists them now, live ones first."""
    o = accounts.managed(a.org, "keys")
    return o, [_key(k) for k in o.get("keys") or [] if isinstance(k, dict)]


def cmd_keys_list(a: argparse.Namespace) -> None:
    o, held = _org_keys(a)
    shown = held if a.all else [k for k in held if not k["revoked"]]
    _out(a, {"org": o["id"], "keys": shown},
         *(_key_lines(shown) if shown else [f"{o['id']} holds no {'' if a.all else 'live '}keys"]),
         "" if shown else "Next: kennet keys create <name>")


def _no_keys_yet(o: dict) -> accounts.Error:
    """An organization that holds nothing a key could carry: why, as `kennet login` says it."""
    if o.get("admission") == "pending":
        return accounts.Error(f"{o['id']} waits for Metaphi AI to approve it; keys can be made once it is (you're "
                              "emailed)", code="awaiting_approval")
    return accounts.Error(f"{o['id']} holds no access a key could carry yet; ask Metaphi AI for it", code="no_access")


def _secret_goes(a: argparse.Namespace) -> str:
    """Where `create` puts the key, decided before it is made, so no key is made that nobody receives: onto a
    terminal (a person, with the shown-once warning, as gh, stripe and vercel do), down a pipe alone (stdout not a
    terminal, no --json), or into the --json answer only with --show-secret. A JSON answer is what an agent reads,
    so without the flag nothing is made."""
    if a.json:
        if a.show_secret:
            return "shown"
        e = accounts.Error("--json never carries the key unless --show-secret says to; nothing was made",
                           code="secret_destination_required")
        e.next_step = f"kennet keys create {shlex.quote(a.name)} | gh secret set METAPHI_API_KEY"
        e.advice = "pipe it into your secret store, as the next step does, with no --json"
        raise e
    return "shown" if sys.stdout.isatty() else "piped"


def cmd_keys_create(a: argparse.Namespace) -> None:
    goes = _secret_goes(a)
    o, held = _org_keys(a)
    grants = list(o.get("grants") or [])
    if not grants:
        raise _no_keys_yet(o)
    scopes = sorted(set(a.scope)) if a.scope else grants   # by default all the organization holds, as the console
    if beyond := [x for x in scopes if x not in grants]:
        raise accounts.Error(f"{o['id']} does not hold {', '.join(beyond)}; it holds {', '.join(grants)}",
                             code="scope_not_held")
    if taken := next((k for k in held if k["name"] == a.name and not k["revoked"]), None):
        e = accounts.Error(f"{o['id']} has a live key named {a.name!r} already ({taken['id']})", code="name_taken")
        e.advice = f"pick another name, or revoke that one first: kennet keys revoke {taken['id']}"
        raise e
    try:
        made = _stepped_up(a, lambda: accounts.create_key(o["id"], a.name, scopes, a.test))
    except accounts.Error as e:
        if getattr(e, "status", None) != 409:
            raise
        e.code, e.next_step = "too_many_keys", "kennet keys list"   # accounts' cap on live service keys
        e.advice = "revoke a key the organization no longer uses (kennet keys revoke <name>), then create this one"
        raise
    if not a.json:   # the key first, before anything that reads the answer could fail and hide it
        print(made["key"])
    got = {"org": o["id"], **_key(made)}
    said = (f"Created {'test' if a.test else 'live'} key {made.get('name') or a.name} ({made.get('id') or '?'}, "
            f"{made.get('hint') or '?'}) for {o['id']}: {', '.join(made.get('scopes') or scopes)}")
    print(said, file=sys.stderr)   # what it is and the warning on stderr; stdout is the key alone
    if a.json:   # --show-secret: the one JSON answer that carries the key
        print(json.dumps({**got, "key": made["key"], "warning": "shown once: accounts keeps only its hash"},
                         indent=2))
    print("It is not shown again; accounts keeps only its hash." + (" Store it now." if goes == "shown" else ""),
          file=sys.stderr)


def _target(held: list[dict], said: str) -> dict:
    """The key `revoke` names: by id, or by the name of a live key; a word that is both, of different keys, is
    neither."""
    by_id = next((k for k in held if k["id"] == said), None)
    named = [k for k in held if k["name"] == said and not k["revoked"] and k is not by_id]
    if by_id is not None and not named:
        return by_id
    if by_id is not None:
        e = accounts.Error(f"{said!r} is the id of {by_id['name']} and the name of {named[0]['id']}; name one by "
                           "the other's id", code="ambiguous_key")
        e.next_step = "kennet keys list"
        raise e
    if len(named) == 1:
        return named[0]
    if named:
        e = accounts.Error(f"{len(named)} live keys are named {said!r} ({', '.join(k['id'] for k in named)}); name "
                           "one by its id", code="ambiguous_key")
        e.next_step = f"kennet keys revoke {named[0]['id']}"
        raise e
    e = accounts.Error(f"no live key named or with the id {said!r}", code="no_key")
    e.next_step = "kennet keys list"
    raise e


def cmd_keys_revoke(a: argparse.Namespace) -> None:
    o, held = _org_keys(a)
    k = _target(held, a.key)
    what = f"{k['name']} ({k['id']}, {k['hint']})"
    if k["revoked"]:
        _out(a, {"org": o["id"], **k, "status": "already_revoked"}, f"{what} was revoked already, {_when(k['revoked'])}")
        return
    if not a.yes:
        if a.json or not _interactive():   # nothing waits on a question away from a terminal: the flag is the yes
            e = accounts.Error(f"revoking {what} stops everything that uses it; confirm with --yes",
                               code="confirmation_required")
            e.next_step = shlex.join(["kennet", "keys", "revoke", k["id"], "--yes"]
                                     + (["--org", a.org] if a.org else []) + (["--json"] if a.json else []))
            raise e
        try:
            sure = input(f"Revoke {what} of {o['id']}? Anything using it stops working. [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sure = ""
        if sure not in ("y", "yes"):
            print("Nothing revoked.")
            return
    gone = _key(_stepped_up(a, lambda: accounts.revoke_key(o["id"], k["id"])))
    _out(a, {"org": o["id"], **gone, "status": "revoked"}, f"Revoked {what}: it stops working within a minute.",
         "That was a terminal's key: `kennet login` there hands it new ones." if k["owner"] == "cli" else "")


def cmd_credits(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    out = o["credits_usd"] <= 0
    _out(a, {"org": o["id"], "credits_usd": o["credits_usd"], "can_buy": bool(o.get("can_buy")),
             **({"next_step": "kennet credits buy"} if out and o.get("can_buy") else {})},
         f"{o['id']}: balance {_money(o['credits_usd'])}",
         *(["Runs are refused until credit is added" + (": `kennet credits buy`." if o.get("can_buy") else
                                                       "; ask an owner or billing member to add it.")] if out else []))


def _spend_lines(got: dict) -> list[str]:
    """A person's month: its total, each cap, then model calls by model, compute by supplier and device, and runs."""
    lines = [f"{got.get('email') or 'you'}, {got['month']}: {_money(got.get('usd'))} spent"]
    lines += [f"  cap {c['product']} ({c['subject']}): {_money(c['spent'])} of {_money(c['usd'])}"
              + (f", {_money(c['authorized'])} held by running work" if c.get("authorized") else "")
              for c in got.get("caps") or []]
    for title, head, rows in (
            ("Models", ("ROUTE", "MODEL", "CALLS", "SPENT"),
             [(m["route"], str(m.get("model") or "-"), str(m["calls"]), _money(m["usd"]))
              for m in got.get("models") or []]),
            ("Compute", ("SUPPLIER/DEVICE", "SANDBOXES", "SPENT"),
             [(c["route"].split("/", 1)[-1], str(c["sandboxes"]), _money(c["usd"])) for c in got.get("compute") or []]),
            ("Runs", ("RUN", "SPENT"), [(r["job"], _money(r["usd"])) for r in got.get("runs") or []])):
        if rows:
            lines += ["", title, *_rows([head, *rows])]
    if got.get("unattributed_usd"):
        lines.append(f"Under no run: {_money(got['unattributed_usd'])}")
    return lines


def cmd_spend(a: argparse.Namespace) -> None:
    got = accounts.my_spend(a.month, a.org)
    _out(a, got, *_spend_lines(got))


def cmd_admin_spend(a: argparse.Namespace) -> None:
    got = admin.spend(a.email, a.month)
    if a.email:
        _out(a, got, *_spend_lines(got))
        return
    people = got.get("people") or []
    rows = [("PERSON", "SPENT", "MODELS", "COMPUTE")] + [
        (p["email"], _money(p["usd"]), _money(p.get("models_usd")), _money(p.get("compute_usd"))) for p in people]
    _out(a, got, f"{got['month']}: {len(people)} people", *(_rows(rows) if people else ["no spend"]))


def _month(said: str) -> str:
    said = said.strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", said):
        raise argparse.ArgumentTypeError(f"{said!r} is not a month (YYYY-MM)")
    return said


def cmd_credits_buy(a: argparse.Namespace) -> None:
    o = accounts.credit_of(a.org)
    if not o.get("can_buy"):
        raise accounts.Error(f"only an owner or billing member of {o['id']} can buy its credit")
    _buy(o, a.amount, open_browser=not a.no_browser)


def _money(usd: object) -> str:
    if not isinstance(usd, (int, float)):
        return "-"
    return f"{'-' if usd < 0 else ''}${abs(usd):,.2f}"


def _subject(said: str) -> str:
    """A subject as accounts names it: `org:<id>`, or a person's address in lower case."""
    said = said.strip()
    if said.startswith(admin.ORG) and len(said) > len(admin.ORG):
        return said
    if "@" not in said or " " in said:
        raise argparse.ArgumentTypeError(f"{said!r} is neither an address nor org:<id>")
    return said.lower()


def _email(said: str) -> str:
    said = said.strip().lower()
    if "@" not in said or " " in said:
        raise argparse.ArgumentTypeError(f"{said!r} is not an address")
    return said


def _amount(said: str) -> float:
    try:
        usd = round(float(said.lstrip("$")), 2)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an amount: {said!r}") from None
    if not math.isfinite(usd):
        raise argparse.ArgumentTypeError(f"not an amount: {said!r}")
    return usd


def _days(said: str) -> float:
    try:
        got = float(said)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{said!r} is not a number of days") from None
    if not 0 < got < math.inf:
        raise argparse.ArgumentTypeError("a number of days above zero")
    return got


def cmd_admin_orgs(a: argparse.Namespace) -> None:
    got = admin.orgs()
    rows = [("ID", "NAME", "ADMISSION", "MEMBERS", "KEYS", "CREDIT", "GRANTS")] + [
        (o["id"], str(o.get("name") or "-"), str(o.get("admission") or "-"), str(len(o.get("roles") or {})),
         str(o.get("live_keys", 0)), _money(o.get("credits_usd")), ",".join(o.get("grants") or []) or "-")
        for o in got]
    _out(a, {"data": got}, *(_rows(rows) if got else ["no organizations"]))


def cmd_admin_admissions(a: argparse.Namespace) -> None:
    got = admin.admissions()
    rows = [("ORG", "NAME", "ASKED BY", "ASKED")] + [
        (o["org"], str(o.get("name") or "-"), str(o.get("asked_by") or "-"), _when(o.get("asked"))) for o in got]
    _out(a, {"data": got}, *(_rows(rows) if got else ["no organization waits for approval"]),
         f"Next: kennet admin org {got[0]['org']} admit" if got else "")


def _access_lines(got: dict) -> list[str]:
    members, _ = _members({"members": got.get("roles") or {}})
    live = [k for k in got.get("keys") or [] if not k.get("revoked")]
    return [(f"{got['org']}: {got.get('admission') or 'not asked for Kennet'}; "
             f"holds {', '.join(got.get('grants') or []) or 'nothing'}; keys: {len(live)} live"),
            *_member_lines(members, [])]


def cmd_admin_org(a: argparse.Namespace) -> None:
    got = admin.access(a.org)
    _out(a, got, *_access_lines(got))


def cmd_admin_org_add(a: argparse.Namespace) -> None:
    got = admin.set_role(a.org, a.email, a.role)
    _out(a, got, f"{a.email} is {a.role} of {a.org}")


def cmd_admin_org_grant(a: argparse.Namespace) -> None:
    got = admin.grant_org(a.org, a.scope) if a.org_verb == "grant" else admin.ungrant_org(a.org, a.scope)
    _out(a, got, *_access_lines(got))


def cmd_admin_org_admit(a: argparse.Namespace) -> None:
    got = admin.admit(a.org) if a.org_verb == "admit" else admin.unadmit(a.org)
    _out(a, got, *_access_lines(got))


def cmd_admin_org_billing(a: argparse.Namespace) -> None:
    got = admin.billing(a.org, a.mode, a.credit_limit)
    _out(a, got, f"{a.org} is {got.get('mode', a.mode)}"
         + (f", up to {_money(got['credit_limit_usd'])} below zero" if got.get("credit_limit_usd") is not None else "")
         + (f"; headroom {_money(got['headroom_usd'])}" if isinstance(got.get("headroom_usd"), (int, float)) else ""))


def cmd_admin_org_funds(a: argparse.Namespace) -> None:
    got = admin.funds_members(a.org, a.state == "on")
    _out(a, got, f"{a.org} {'pays' if got.get('funds_members') else 'does not pay'} for its members' spend")


def cmd_admin_org_transactions(a: argparse.Namespace) -> None:
    got = admin.transactions(a.org, a.limit, a.type)
    held = got.get("data") or []
    rows = [("ID", "TYPE", "AMOUNT", "WHEN", "FOR")] + [
        (t["id"], t["type"], _money(t.get("amount_usd")), _when(t.get("created")),
         " ".join(str(t[k]) for k in ("product", "route", "ref") if t.get(k)) or "-") for t in held]
    _out(a, got, *(_rows(rows) if held else [f"{a.org} has no transactions"]),
         f"balance {_money(got.get('credits_usd'))}" + ("; more with --limit" if got.get("has_more") else ""))


def cmd_admin_grants(a: argparse.Namespace) -> None:
    if a.verb == "grants":
        got = admin.grants(a.email)
    else:
        got = admin.grant(a.email, a.scope) if a.verb == "grant" else admin.revoke(a.email, a.scope)
    held = got.get("held") or {}
    _out(a, got, f"{got.get('email', a.email)}" + (f" (admin: {got['admin']})" if got.get("admin") else ""),
         *(f"  {r}: {' '.join(s) or '-'}" for r, s in held.items()))


def cmd_admin_credit(a: argparse.Namespace) -> None:
    if a.usd == 0:
        raise accounts.Error("an amount other than zero")
    got = admin.credit(a.subject, a.usd, a.days, a.note)
    said = (f"{_money(a.usd)} to {a.subject}" + (f" for {a.days:g} days" if a.days else "")
            if got.get("granted", True) else f"that grant to {a.subject} was made already")
    _out(a, got, f"{said}; balance {_money(got.get('credits_usd'))}")


def cmd_admin_end(a: argparse.Namespace) -> None:
    got = admin.end(a.subject, a.grant)
    _out(a, got, (f"grant {a.grant} ended; {_money(got.get('lost_usd'))} left on it is gone" if got.get("ended")
                  else f"grant {a.grant} had ended already") + f"; balance {_money(got.get('credits_usd'))}")


def cmd_admin_limits(a: argparse.Namespace) -> None:
    if a.usd is not None or a.clear:
        got = admin.set_limit(a.subject, a.product, None if a.clear else a.usd)
    else:
        got = admin.limits(a.subject)
    held = got.get("limits") or []
    _out(a, got, *([f"{got.get('subject', a.subject)} {c['product']}: {_money(c['usd'])} a month" for c in held]
                   or [f"{got.get('subject', a.subject)}: no limit"]))


def cmd_admin_models(a: argparse.Namespace) -> None:
    if a.patterns or a.clear or a.none:
        got = admin.set_models(a.subject, a.product, None if a.clear else [] if a.none else a.patterns)
    else:
        got = admin.models(a.subject)
    held = got.get("models") or []
    _out(a, got, *([f"{got.get('subject', a.subject)} {m['product']}: {', '.join(m['models']) or 'no model'}"
                    for m in held] or [f"{got.get('subject', a.subject)}: any model its products offer"]))


def cmd_admin_audit(a: argparse.Namespace) -> None:
    got = admin.audit(a.actor, a.days, a.limit)
    rows = [("WHEN", "WHO", "STATUS", "CHANGE")] + [
        (_when(r.get("at")), str(r.get("actor") or "-"), str(r.get("status") or "-"),
         f"{r.get('method')} {r.get('path')}" + (f"?{r['query']}" if r.get("query") else "")) for r in got]
    _out(a, {"data": got}, *(_rows(rows) if got else ["no changes in that window"]))


def cmd_update(a: argparse.Namespace) -> None:
    got = update.now()
    if got["updated"]:
        said = f"kennet {got['latest']} is installed; it runs from the next command."
    elif got.get("next_step"):
        said = f"kennet {got['latest']} is out (you have {got['version']}). Upgrade: {got['next_step']}"
    else:
        said = f"kennet {got['version']} is the newest."
    _out(a, got, said)


_SAID = {"added": "MCP server added", "installed": "MCP server already added by kennet",
         "present": "a server named kennet was there already; left as it is"}


def cmd_agents_install(a: argparse.Namespace) -> None:
    rows = agents.install(a.harness, a.project, a.force)
    lines = []
    for r in rows:
        h = agents.BY_NAME[r["harness"]]
        skill_said = {"written": f"skill at {r['skill']}",
                      "kept": f"skill: {r['skill']} is not kennet's; left as it is (--force replaces it)"}.get(
            r["skill_state"]) or f"skill skipped: {r.get('skill_detail')}"
        mcp_said = _SAID.get(r["mcp"]) or f"MCP server skipped: {r['mcp_detail']}"
        lines.append(f"{h.title}: {skill_said}; {mcp_said}" + (f"; sign in: {r['sign_in']}" if r.get("sign_in")
                                                              else ""))
    # the first sign-in a command does; each row says its own
    first = next((r["sign_in"] for r in rows if r.get("sign_in") and r["sign_in"] == agents.BY_NAME[
        r["harness"]].sign_in), None)
    _out(a, {"harnesses": rows, "next_step": first}, *lines, "Start a new session in each for the skill to load.")


def cmd_agents_remove(a: argparse.Namespace) -> None:
    got = agents.remove(a.harness, a.project)
    _out(a, got, *(f"removed {x}" for x in got["removed"]), *(f"left {x['target']}: {x['why']}" for x in got["left"]),
         "" if got["removed"] or got["left"] else "nothing of kennet's there")


def cmd_agents_list(a: argparse.Namespace) -> None:
    rows = agents.status(a.project)
    _out(a, {"harnesses": rows},
         *(f"{r['harness']:<9} {'here' if r['present'] else '-':<5}"
           + (f" skill {r['skill_state']:<8} mcp {r['mcp']}" if "skill" in r else "") for r in rows))


EXAMPLES = """\
examples:
  kennet login                      sign in; your organization's keys are saved on this machine
  kennet org create "Acme"          make your organization, if you are in none; Metaphi AI approves it
  kennet org join acme              accept the invitation an organization sent you; its keys come with it
  kennet org members                its members and open invitations (owners and admins)
  kennet org members add bob@acme.com --role admin   add a person; another domain gets an invitation link
  kennet keys list                  the organization's service keys (owners and admins)
  kennet keys create ci | gh secret set METAPHI_API_KEY   a key for CI, straight into its secret
  kennet keys revoke github-ci      stop it working; asks first at a terminal, --yes elsewhere
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
  kennet admin org acme             Metaphi AI's admins: what an organization holds; `kennet admin -h` for the rest

from an agent or a script (no terminal): add --json; stdout is then the result or the error object alone.
  kennet agents install             teach every coding agent here this CLI, and connect Kennet's MCP server
  kennet skill show                 the guide an agent follows, for this version
  kennet login --json               prints the page and the code, and returns; never opens a browser
  kennet login --complete           waits for the approval, saves the keys
  METAPHI_API_KEY=mai_sk_... kennet twin trace.json --json    no sign-in: a key from the console

exit status: 0 done, 1 failed, 2 usage, 3 sign-in or key needed, 4 try the same call later, 5 upgrade kennet
"""


def _json_flag(s: argparse.ArgumentParser) -> None:
    s.add_argument("--json", action="store_true", help="print the result as JSON on stdout")


def _harness_flags(v: argparse.ArgumentParser) -> None:
    v.add_argument("--harness", action="append", choices=agents.NAMES, metavar="<name>",
                   help="only this harness (repeatable): " + ", ".join(agents.NAMES) + "; default every one here")
    v.add_argument("--project", action="store_true", help="into this repository's files instead (.agents/skills, "
                   ".mcp.json and each harness's own), to commit for a team")
    _json_flag(v)


def _agents_install(v: argparse.ArgumentParser) -> None:
    _harness_flags(v)
    v.add_argument("--force", action="store_true", help="replace a skill file there that kennet did not write")
    v.set_defaults(fn=cmd_agents_install)


def _agents_remove(v: argparse.ArgumentParser) -> None:
    _harness_flags(v)
    v.set_defaults(fn=cmd_agents_remove)


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

    s = sub.add_parser("org", help="your organization: make it, if you are in none, or join the one that invited "
                       "you; manage its members")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("create", help="make an organization and own it; Metaphi AI approves it for Kennet and "
                         "mails you, then `kennet login` saves its keys")
    v.add_argument("name", help="what it is called, e.g. \"Acme Robotics\"")
    _json_flag(v)
    v.set_defaults(fn=cmd_org_create)
    v = verbs.add_parser("join", help="accept the invitation an organization sent you, and save its keys")
    v.add_argument("org", help="the organization that invited you (`kennet whoami` names it), or the invitation's id "
                   "(inv_...)")
    _json_flag(v)
    v.set_defaults(fn=cmd_org_join)
    m = verbs.add_parser("members", help="its members and open invitations; add, change or remove one (owners and "
                         "admins)")
    m.add_argument("--org", help="the organization, when you manage several")
    _json_flag(m)
    m.set_defaults(fn=cmd_org_members)
    members = m.add_subparsers(dest="members_verb", metavar="<verb>")
    v = members.add_parser("add", help="add a person; an address off your organization's domains is sent an "
                           "invitation link instead")
    v.add_argument("email")
    v.add_argument("--role", choices=accounts.ROLES, default="member", help="default member")
    v.set_defaults(fn=cmd_org_members_set)
    r = members.add_parser("role", help="change a member's role (only an owner makes or changes an owner)")
    r.add_argument("email")
    r.add_argument("role", choices=accounts.ROLES)
    r.set_defaults(fn=cmd_org_members_set)
    d = members.add_parser("remove", help="take a member out, or take back an invitation; the keys they made stay")
    d.add_argument("email")
    d.set_defaults(fn=cmd_org_members_remove)
    for verb in (v, r, d):
        verb.add_argument("--org", help="the organization, when you manage several")
        _json_flag(verb)

    s = sub.add_parser("keys", help="your organization's service keys, for CI and servers: list, create, revoke "
                       "(owners and admins)")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("list", help="its live keys, newest first: name, id, the last four, scopes, last use")
    v.add_argument("--all", action="store_true", help="revoked keys too")
    v.set_defaults(fn=cmd_keys_list)
    c = verbs.add_parser("create", help="make a service key and print it, once: alone on stdout, so it pipes into a "
                         "secret store")
    c.add_argument("name", type=_key_name, help="what it is for, e.g. github-ci")
    c.add_argument("--test", action="store_true", help="a test key (mai_sk_test_): free, capped, kept apart from live")
    c.add_argument("--scope", action="append", metavar="SCOPE", help="a scope it holds; repeatable (default: all the "
                   "organization holds)")
    c.add_argument("--show-secret", action="store_true", help="put the key in the --json answer (without it, "
                   "--json makes nothing)")
    c.set_defaults(fn=cmd_keys_create)
    r = verbs.add_parser("revoke", help="stop a key working, within a minute; its record stays")
    r.add_argument("key", metavar="<name|id>", help="a live key's name, or a key's id (key_...)")
    r.add_argument("-y", "--yes", action="store_true", help="revoke without asking; needed away from a terminal")
    r.set_defaults(fn=cmd_keys_revoke)
    for verb in (v, c, r):
        verb.add_argument("--org", help="the organization, when you manage several")
        _json_flag(verb)

    s = sub.add_parser("spend", help="what you spent this month against your caps: models by model, sandboxes by "
                       "supplier and GPU, and each run")
    s.add_argument("--month", type=_month, help="a UTC month, YYYY-MM (default: this one)")
    s.add_argument("--org", help="your spend in this organization alone")
    _json_flag(s)
    s.set_defaults(fn=cmd_spend)

    s = sub.add_parser("credits", help="your organization's balance")
    s.add_argument("--org")
    _json_flag(s)
    s.set_defaults(fn=cmd_credits)
    credits = s.add_subparsers(dest="verb", metavar="<verb>")
    b = credits.add_parser("buy", help="add credit through Stripe Checkout (owners and billing members)")
    b.add_argument("--amount", type=_usd, default=100.0, help="dollars to add (default 100)")
    b.add_argument("--org")
    b.add_argument("--no-browser", action="store_true", help="print the link instead of opening it")
    b.set_defaults(fn=cmd_credits_buy)

    s = sub.add_parser("admin", help="Metaphi AI's admins: organizations, people's grants, credit, limits and models "
                       "(an account holding metaphi:admin)")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    v = verbs.add_parser("orgs", help="every organization: admission, members, keys, credit, grants")
    _json_flag(v)
    v.set_defaults(fn=cmd_admin_orgs)
    v = verbs.add_parser("spend", help="every person's spend this month, the most first; or one person's whole")
    v.add_argument("--email", type=_email, help="one person's month, as `kennet spend` shows it to them")
    v.add_argument("--month", type=_month, help="a UTC month, YYYY-MM (default: this one)")
    _json_flag(v)
    v.set_defaults(fn=cmd_admin_spend)
    v = verbs.add_parser("admissions", help="the organizations waiting to be approved, the longest wait first")
    _json_flag(v)
    v.set_defaults(fn=cmd_admin_admissions)

    o = verbs.add_parser("org", help="one organization: what it holds and who manages it; add a person, grant, "
                         "admit, set its billing")
    o.add_argument("org", metavar="<org>", help="the organization's id")
    _json_flag(o)
    o.set_defaults(fn=cmd_admin_org)
    org = o.add_subparsers(dest="org_verb", metavar="<verb>")
    d = org.add_parser("add", help="put a person in it in a role, moving them from another organization")
    d.add_argument("email", type=_email)
    d.add_argument("--role", choices=accounts.ROLES, default="member", help="default member")
    d.set_defaults(fn=cmd_admin_org_add)
    g = org.add_parser("grant", help="grant it a scope its keys may then hold")
    u = org.add_parser("ungrant", help="take a scope back, from every key of it too")
    for verb in (g, u):
        verb.add_argument("scope", help="e.g. kennet:twin")
        verb.set_defaults(fn=cmd_admin_org_grant)
    m = org.add_parser("admit", help="approve it for Kennet; its owners are mailed")
    n = org.add_parser("unadmit", help="take it out of Kennet, and what admission gave it from every key")
    for verb in (m, n):
        verb.set_defaults(fn=cmd_admin_org_admit)
    b = org.add_parser("billing", help="how its spend is bounded: prepaid (its balance), invoiced (a credit limit "
                       "below zero), internal (ours; each member's limit bounds them)")
    b.add_argument("mode", choices=admin.BILLING)
    b.add_argument("--credit-limit", type=_amount, metavar="USD", help="invoiced: how far below zero it may go")
    b.set_defaults(fn=cmd_admin_org_billing)
    f = org.add_parser("funds-members", help="whether it pays for its members' spend from its balance")
    f.add_argument("state", choices=("on", "off"))
    f.set_defaults(fn=cmd_admin_org_funds)
    t = org.add_parser("transactions", help="its ledger entries, newest first, and its balance")
    t.add_argument("--limit", type=int, default=20, help="how many (default 20, at most 100)")
    t.add_argument("--type", choices=admin.TRANSACTION_TYPES)
    t.set_defaults(fn=cmd_admin_org_transactions)
    for verb in (d, g, u, m, n, b, f, t):
        _json_flag(verb)

    v = verbs.add_parser("grants", help="what a person holds on every resource, and their admin role")
    v.add_argument("email", type=_email)
    v.set_defaults(fn=cmd_admin_grants)
    g = verbs.add_parser("grant", help="give a person a scope (a resource's, or metaphi:admin[:read])")
    r = verbs.add_parser("revoke", help="take a scope back from a person")
    for verb in (g, r):
        verb.add_argument("email", type=_email)
        verb.add_argument("scope")
        verb.set_defaults(fn=cmd_admin_grants)
    for verb in (v, g, r):
        _json_flag(verb)

    c = verbs.add_parser("credit", help="credit a person or an organization from our side, for any spend")
    c.add_argument("subject", type=_subject, metavar="<email|org:id>")
    c.add_argument("usd", type=_amount, metavar="<usd>", help="dollars; below zero takes credit back")
    c.add_argument("--days", type=_days, metavar="N", help="how long it lasts (default: it never ends)")
    c.add_argument("--note", help="why, kept on the grant")
    c.set_defaults(fn=cmd_admin_credit)
    e = verbs.add_parser("end", help="end a grant we gave, now: what it has left is gone")
    e.add_argument("subject", type=_subject, metavar="<email|org:id>")
    e.add_argument("grant", type=int, metavar="<grant id>", help="as `kennet admin credit --json` lists it")
    e.set_defaults(fn=cmd_admin_end)
    li = verbs.add_parser("limits", help="a person's or an organization's monthly caps; set one, or take it away")
    li.add_argument("subject", type=_subject, metavar="<email|org:id>")
    set_ = li.add_mutually_exclusive_group()
    set_.add_argument("--usd", type=_amount, metavar="N", help="the month's cap in dollars (UTC month)")
    set_.add_argument("--clear", action="store_true", help="take the cap away")
    li.add_argument("--product", default="*", help="hum, kennet, route:<route>, or * for every product (default *)")
    li.set_defaults(fn=cmd_admin_limits)
    mo = verbs.add_parser("models", help="the models a person or an organization may call; set the list, or take "
                          "it away")
    mo.add_argument("subject", type=_subject, metavar="<email|org:id>")
    mo.add_argument("patterns", nargs="*", metavar="<pattern>", help="a catalog id, or one with * (openai/*)")
    set_ = mo.add_mutually_exclusive_group()
    set_.add_argument("--clear", action="store_true", help="take the list away: any model the product offers")
    set_.add_argument("--none", action="store_true", help="no model at all")
    mo.add_argument("--product", default="hum", help="the product that holds calls to the list (default hum)")
    mo.set_defaults(fn=cmd_admin_models)
    au = verbs.add_parser("audit", help="the changes admins made, newest first")
    au.add_argument("--actor", type=_email, metavar="EMAIL", help="only this person's")
    au.add_argument("--days", type=int, default=30, help="how far back (default 30)")
    au.add_argument("--limit", type=int, default=200, help="how many (default 200)")
    au.set_defaults(fn=cmd_admin_audit)
    for verb in (c, e, li, mo, au):
        _json_flag(verb)

    s = sub.add_parser("twin",help="twin a trace (JSON, JSON Lines or text)")
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
        v.add_argument("id", metavar="<name>[@<version>]", help="a name alone is its newest version")
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

    s = sub.add_parser("agents", help="Kennet in your coding agents: the skill and Kennet's MCP server, in each "
                       "harness here (" + ", ".join(agents.NAMES) + ")")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    _agents_install(verbs.add_parser("install", help="write the skill and register the server in each harness on "
                                     "this machine"))
    _agents_remove(verbs.add_parser("remove", help="take away what install added, and only that"))
    v = verbs.add_parser("list", help="each harness: here or not, the skill, the server")
    v.add_argument("--project", action="store_true", help="this repository's files")
    _json_flag(v)
    v.set_defaults(fn=cmd_agents_list)

    s = sub.add_parser("skill", help="the Kennet skill: the guide a coding agent follows")
    s.set_defaults(fn=lambda a, s=s: s.print_help())
    verbs = s.add_subparsers(dest="verb", metavar="<verb>")
    verbs.add_parser("show", help="print the guide an agent follows, for this version").set_defaults(
        fn=lambda a: print(skill.GUIDE, end=""))
    _agents_install(verbs.add_parser("install", help="the same as `agents install`"))
    _agents_remove(verbs.add_parser("remove", help="the same as `agents remove`"))

    s = sub.add_parser("update", help="install the newest release now (the release binary), or print the command "
                       "that upgrades this install")
    _json_flag(s)
    s.set_defaults(fn=cmd_update)

    sub.add_parser("help", help="this help").set_defaults(fn=lambda a: p.print_help())
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a line is read when it is said, also down a pipe
        with contextlib.suppress(Exception):
            stream.reconfigure(line_buffering=True)
    said = sys.argv[1:] if argv is None else argv
    if said == [update.WORKER]:   # the detached self-update
        return update.worker()
    if said == [update.PRUNE]:   # install.sh, after it flips the symlink: old versions nobody runs go
        if update.install_method() == "binary":
            update.prune(update.running())
        return 0
    p = parser()
    a = p.parse_args(argv)
    update.hold()   # a running version is never pruned from under this process
    notifier = None if a.command in (None, "update") else update.Notifier(getattr(a, "json", False))
    try:
        return _run(p, a)
    finally:
        if notifier is not None:
            with contextlib.suppress(Exception):   # the notice is never the command's error
                notifier.done()


def _run(p: argparse.ArgumentParser, a: argparse.Namespace) -> int:
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
        advice = getattr(error, "advice", None)   # what clears it when no command does: said after `Next:`
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
        elif error.next_step or advice:
            print(f"Next: {advice or error.next_step}", file=sys.stderr)
        elif error.retry_after_s is not None:
            print(f"Try again in {error.retry_after_s:.0f}s.", file=sys.stderr)
        return status(error)
    except KeyboardInterrupt:
        if getattr(a, "json", False):   # stdout is still one JSON object: the stop, said as an error
            stopped = errors.MetaphiError("interrupted", code="interrupted")
            print(json.dumps({"error": stopped.to_dict()}, indent=2))
        return 130
    except httpx.TransportError as e:   # the network, not the service: the same command may work in a moment
        return _said(a, network(e), "Next: check your connection, then run the same command again.")
    except Exception as e:  # noqa: BLE001 - whatever else: one line and the log, never a traceback
        error = _bug(e)
        return _said(a, error, f"This is a bug; please report it with `kennet --version` and the debug log: "
                               f"{error.context['debug_log']}")
    return 0


NETWORK = {httpx.ConnectTimeout: "the connection timed out", httpx.ReadTimeout: "it stopped answering",
           httpx.WriteTimeout: "sending timed out", httpx.PoolTimeout: "too many connections at once",
           httpx.ConnectError: "no connection", httpx.RemoteProtocolError: "the connection dropped",
           httpx.ReadError: "the connection dropped", httpx.WriteError: "the connection dropped",
           httpx.ProxyError: "the proxy refused it"}


def network(e: httpx.TransportError) -> errors.MetaphiError:
    """A request that never got an answer: `network`, retryable, naming the host it was for."""
    try:
        host = e.request.url.host
    except RuntimeError:   # an error raised before its request was made
        host = None
    reason = next((said for kind, said in NETWORK.items() if isinstance(e, kind)), None) or type(e).__name__
    return errors.APIError(f"can't reach {host or 'the service'}: {reason}", code="network", retryable=True)


def _bug(e: BaseException) -> errors.MetaphiError:
    """Something the CLI did not expect: its traceback in the debug log, one line here."""
    path = config.home() / "debug.log"
    with contextlib.suppress(OSError):
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(_redacted(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} kennet {__version__} "
                              f"{' '.join(_unsecret(sys.argv[1:]))}\n"
                              + "".join(traceback.format_exception(type(e), e, e.__traceback__))))
    error = errors.MetaphiError(f"kennet stopped on something it did not expect ({type(e).__name__})",
                                code="internal", type="api_error")
    error.context = {"debug_log": str(path)}
    return error


SECRET_FLAGS = ("--api-key", "--key")   # their values are keys: never written down


def _unsecret(argv: list[str]) -> list[str]:
    """The command line with every secret flag's value replaced."""
    out, hide = [], False
    for word in argv:
        flag = word.split("=", 1)[0]
        out.append("<redacted>" if hide else f"{flag}=<redacted>" if flag in SECRET_FLAGS and "=" in word else word)
        hide = word in SECRET_FLAGS
    return out


def _redacted(text: str) -> str:
    """Any organization key in `text` replaced."""
    return re.sub(rf"{re.escape(config.KEY_PREFIX)}[A-Za-z0-9_\-]*", f"{config.KEY_PREFIX}<redacted>", text)


def _said(a: argparse.Namespace, error: errors.MetaphiError, then: str) -> int:
    """An error caught at the top: the error object under --json, one line and the next thing to do on stderr."""
    if getattr(a, "json", False):
        print(json.dumps({"error": error.to_dict(), **error.context}, indent=2))
    print(f"error: {error}", file=sys.stderr)
    print(then, file=sys.stderr)
    return status(error)


if __name__ == "__main__":
    sys.exit(main())
