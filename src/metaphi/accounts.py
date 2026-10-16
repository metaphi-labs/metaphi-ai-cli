"""Your Metaphi AI account: sign in from the terminal (RFC 8628, as `stripe login` does) and be handed your
organization's keys, the organizations you manage, and their prepaid credit."""
from __future__ import annotations

import contextlib
import time
import webbrowser
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx

from metaphi import config
from metaphi.errors import MetaphiError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "metaphi-cli"


CHECKOUT = "https://checkout.stripe.com/"  # the one place a checkout link may send the browser
MIN_INTERVAL = 5.0  # RFC 8628 §3.5: the default, and the floor whatever the server says
ROLES = ("owner", "admin", "billing", "member")   # an organization member's roles, as accounts says them (orgs.ROLES)
MANAGERS = ("owner", "admin")   # the roles that manage an organization's members


class Error(MetaphiError):
    """A refusal in the server's own sentence, or one the CLI makes itself; the CLI's own carry a `code` and the
    `next_step` that clears them (`need`)."""


def need(message: str, code: str, next_step: str | None) -> Error:
    """A refusal the CLI makes because a credential is missing or spent: its code, and the command that clears it
    (None when only someone else can)."""
    return Error(message, code=code, type="auth_error", next_step=next_step)


UNAVAILABLE = (502, 503, 504)   # a gateway or proxy in front of accounts, not accounts' own answer


def _unanswered(r: httpx.Response) -> Error:
    """An answer that is not accounts' JSON: a proxy's HTML error page, a captive portal's 200. Its body is never
    the message. A gateway's 5xx, or a 200 that is not JSON, is `service_unavailable` and worth the same call again."""
    again = r.status_code in UNAVAILABLE or r.status_code < 400
    e = Error(f"the service answered HTTP {r.status_code}; try again in a minute",
              code="service_unavailable" if again else None, type="api_error", retryable=again)
    e.status = r.status_code
    held = r.headers.get("retry-after", "").strip()
    with contextlib.suppress(ValueError):
        e.retry_after_s = max(0.0, float(held)) if held else None
    return e


def _refused(r: httpx.Response) -> Error:
    """A refusal: the server's sentence and code when it answered JSON, else `_unanswered`."""
    try:
        r.json()
    except ValueError:
        return _unanswered(r)
    e = Error(_said(r), code=_code(r))
    e.status = r.status_code   # what a command may act on beside the sentence: 503 on an invitation
    return e


def _said(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return _unanswered(r).message
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("message"), str):
            return detail["message"]   # a coded refusal: {"detail": {"code", "message"}}
        return str(detail or body.get("error_description") or body.get("error") or body)
    return str(body)


def _code(r: httpx.Response) -> str | None:
    """The code of a coded refusal, at the top level ({"detail": "<sentence>", "code"}) or inside `detail`
    ({"detail": {"code", "message"}}), else None."""
    try:
        body = r.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    detail = body.get("detail")
    code = detail.get("code") if isinstance(detail, dict) else body.get("code")
    return code if isinstance(code, str) and code else None


def _json(r: httpx.Response) -> dict[str, Any]:
    """The body as a JSON object; anything else is the server's sentence as an error."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if body is None:
        raise _unanswered(r)
    if not isinstance(body, dict):
        raise Error(_said(r))
    return body


def _http() -> httpx.Client:
    from metaphi.update import floor
    return httpx.Client(base_url=config.accounts_url(), timeout=30.0, event_hooks={"response": [floor]},
                        headers={"user-agent": f"metaphi-cli/{__import__('metaphi').__version__}"})


def start() -> dict[str, Any]:
    """Ask for a device code (RFC 8628 §3.1) and keep it as the sign-in under way: what the person is shown
    (`verification_uri`, `user_code`) and what `finish` polls with."""
    with _http() as h:
        r = h.post("/device/code", data={"client_id": CLIENT_ID})
    if r.status_code != 200:
        raise _refused(r)
    code = _json(r)
    if not all(isinstance(code.get(k), str) and code[k] for k in ("device_code", "user_code", "verification_uri")):
        raise Error("the sign-in server sent an incomplete device code")
    # RFC 8628 §3.3: the person types the code at verification_uri; verification_uri_complete is optional.
    config.check_url(code["verification_uri"], "the sign-in page")
    complete = code.get("verification_uri_complete")
    try:
        pending = {"user_code": code["user_code"], "verification_uri": code["verification_uri"],
                   "open": config.check_url(complete, "the sign-in page") if isinstance(complete, str) and complete
                   else code["verification_uri"],
                   "interval": max(MIN_INTERVAL, float(code.get("interval") or MIN_INTERVAL)),
                   "expires": time.time() + min(float(code.get("expires_in") or 600), 1800.0)}
    except (TypeError, ValueError):
        raise Error("the sign-in server sent an unreadable device code") from None
    cfg = config.load()
    cfg.update({"pending": pending, "device_code": code["device_code"]})
    config.save(cfg)
    return pending


def pending() -> dict[str, Any] | None:
    """The sign-in `start` left under way, while its code still stands."""
    cfg = config.load()
    held = cfg.get("pending")
    if not isinstance(held, dict) or not cfg.get("device_code") or float(held.get("expires") or 0) <= time.time():
        return None
    return held


def finish(sleep: Callable[[float], None] = time.sleep, wait_s: float | None = None,
           at_once: bool = False) -> dict[str, Any]:
    """Wait for the approval of the sign-in under way, then keep the token and the organization's keys that come
    with it (`api_key`, `test_api_key`; `no_api_key` says why none came). `wait_s` bounds the wait: past it the
    sign-in stays under way and the refusal (`authorization_pending`, retryable) says to call again. `at_once`
    asks first and waits after: for a call that comes back to a code shown a while ago."""
    held = pending()
    if held is None:
        raise need("no sign-in is under way, or its code expired; run `kennet login`", "login_expired",
                   "kennet login")
    device_code, interval = config.load()["device_code"], float(held["interval"])
    until = held["expires"] if wait_s is None else min(held["expires"], time.time() + wait_s)
    first = at_once
    with _http() as h:
        while first or time.time() + interval < until:
            if not first:
                sleep(interval)
            first = False
            r = h.post("/oauth/token", data={"grant_type": DEVICE_GRANT, "device_code": device_code,
                                              "client_id": CLIENT_ID})
            body = _json(r)
            if r.status_code == 200:
                if not isinstance(body.get("access_token"), str):
                    raise Error("the sign-in server sent no access token")
                _keep(body, body["access_token"])
                return body
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            _forget()
            raise need({"expired_token": "the code expired; run `kennet login` again",
                        "access_denied": "the sign-in was declined"}.get(error, _said(r)),
                       "login_expired" if error == "expired_token" else "login_refused", "kennet login")
    if time.time() + interval < held["expires"]:
        e = need(f"not approved yet: the code {held['user_code']} waits at {held['open']}", "authorization_pending",
                 "kennet login --complete")
        e.retryable = True
        raise e
    _forget()
    raise need("the code expired; run `kennet login` again", "login_expired", "kennet login")


def _keep(body: dict[str, Any], token: str) -> None:
    """Save what the server handed this machine: the token, and the organization's keys or why none came."""
    cfg = config.load()
    for name in ("pending", "device_code", "test_api_key", "org", "no_api_key", "no_api_key_code", "no_api_key_org",
                 "invitation", "step_up"):
        cfg.pop(name, None)  # the server revoked the keys it handed last; a fresh sign-in is the step-up
    cfg.update({"token": token, "email": body.get("email")})
    if all(str(body.get(k) or "").startswith(config.KEY_PREFIX) for k in ("api_key", "test_api_key")):
        cfg.update({"api_key": body["api_key"], "test_api_key": body["test_api_key"], "api_key_from": "login"})
        if isinstance(body.get("org"), str) and body["org"]:
            cfg["org"] = body["org"]
    else:
        # handed no key: a key the last sign-in handed speaks for an organization this one is not given, so it goes
        # (one the person saved by hand stays theirs), and what is kept is why (and the organization it is about),
        # which whoami and twin say again as it was said
        if cfg.get("api_key_from") == "login":
            cfg.pop("api_key", None)
            cfg.pop("api_key_from", None)
        if isinstance(body.get("org"), str) and body["org"]:
            cfg["no_api_key_org"] = body["org"]
    cfg.update({k: body[k] for k in ("no_api_key", "no_api_key_code") if isinstance(body.get(k), str) and body[k]})
    if (invitation := invited(body)) is not None:   # `invitation_pending`: the organization `kennet org join` accepts
        cfg["invitation"] = invitation
    config.save(cfg)


def invited(body: dict[str, Any]) -> dict[str, Any] | None:
    """The open invitation an answer carries ({id, org, org_name, role, expires}), when it carries one."""
    held = body.get("invitation")
    if not (isinstance(held, dict) and isinstance(held.get("org"), str) and held["org"]):
        return None
    return {k: held.get(k) for k in ("id", "org", "org_name", "role", "expires") if held.get(k) is not None}


def pickup() -> dict[str, Any] | None:
    """A machine that is signed in asks for its organization's keys again, by the token it holds: what a sign-in
    hands over, with no browser. The organization was made, or approved, after the sign-in, and its keys come
    here. None when this machine is not signed in or its sign-in has ended (401, 403): then the person signs in.
    Any other failure is the error, never a reason to start a sign-in afresh."""
    token = config.load().get("token")
    if not token:
        return None
    with _http() as h:
        r = h.post("/keys/cli", headers={"authorization": f"Bearer {token}"})
    if r.status_code in (401, 403):
        return None
    if r.status_code != 200:
        raise _refused(r)
    body = _json(r)
    _keep(body, token)
    return body


def _forget() -> None:
    cfg = config.load()
    cfg.pop("pending", None)
    cfg.pop("device_code", None)
    config.save(cfg)


def login(say: Callable[[str], None] = print, open_browser: bool = True,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Sign the person in: show a code, open the browser on it, wait for the approval (`start`, then `finish`)."""
    held = start()
    say(f"Open {held['verification_uri']} and enter the code {held['user_code']}")
    if open_browser:
        webbrowser.open(held["open"])
    return finish(sleep)


def logout() -> None:
    cfg = config.load()
    token = cfg.pop("token", None)
    for name in ("email", "api_key", "api_key_from", "test_api_key", "org", "no_api_key", "no_api_key_code",
                 "no_api_key_org", "invitation", "pending", "device_code", "step_up"):
        cfg.pop(name, None)
    config.save(cfg)
    if token:
        with _http() as h:
            h.post("/logout", headers={"authorization": f"Bearer {token}"})


def _person() -> dict[str, str]:
    token = config.load().get("token")
    if not token:
        raise need("not signed in; run `kennet login`", "not_signed_in", "kennet login")
    return {"authorization": f"Bearer {token}"}


def call(method: str, path: str, **kw: Any) -> Any:
    """One request as the signed-in person."""
    with _http() as h:
        r = h.request(method, path, headers=_person(), **kw)
    if r.status_code == 401 and _code(r) == "step_up_required":
        # signed in, too long ago to change an organization: the next `kennet login` signs in afresh, not by pickup
        cfg = config.load()
        cfg["step_up"] = True
        config.save(cfg)
        raise need(_said(r), "step_up_required", "kennet login")
    if r.status_code == 401:
        raise need("your sign-in has ended; run `kennet login`", "not_signed_in", "kennet login")
    if r.status_code >= 400:
        raise _refused(r)   # the server's code, when its refusal carries one
    try:
        return r.json()
    except ValueError:
        raise _unanswered(r) from None


def orgs() -> list[dict[str, Any]]:
    return call("GET", "/keys/orgs")


def create_org(name: str) -> dict[str, Any]:
    """Make the signed-in person's organization: they own it, and it asks Metaphi AI to be approved for Kennet
    (`admission` is "pending" until it is, "admitted" after). What a later command says of the sign-in follows."""
    made = call("POST", "/keys/orgs", json={"name": name})
    if not (isinstance(made, dict) and isinstance(made.get("id"), str)):
        raise Error("the server sent no organization")
    cfg = config.load()
    cfg["org"] = made["id"]
    if isinstance(made.get("message"), str):
        cfg["no_api_key"] = made["message"].rstrip(".")
    cfg["no_api_key_code"] = "no_keys_yet" if made.get("admission") == "admitted" else "awaiting_approval"
    config.save(cfg)
    return made


def expired(invitation: dict[str, Any], now: float | None = None) -> bool:
    at = invitation.get("expires")
    return isinstance(at, (int, float)) and at <= (time.time() if now is None else now)


def invitations() -> list[dict[str, Any]]:
    """The invitations open to the signed-in person, as accounts lists them now ({id, org, org_name, role,
    expires}; `id` from accounts that send it)."""
    got = call("GET", "/keys/invitations")
    held = [invited({"invitation": {"org_name": i.get("name"), **i}}) for i in got if isinstance(i, dict)]
    return [i for i in held if i is not None and not expired(i)]


def _invitation_org(target: str) -> str:
    """The organization an invitation is from: `target` itself, or the one the invitation of that id (inv_...) is
    from, as accounts lists them now; one open invitation listed without ids is that one."""
    if not target.startswith("inv_"):
        return target
    open_ = invitations()
    for i in open_:
        if i.get("id") == target:
            return str(i["org"])
    if len(open_) == 1 and not open_[0].get("id"):   # an accounts that lists no ids: the one there is
        return str(open_[0]["org"])
    raise Error(f"you have no open invitation {target}; `kennet whoami` shows the ones you have",
                code="no_invitation")


def join(target: str) -> dict[str, Any]:
    """Accept the invitation of the organization named (or of the invitation id given), as the signed-in person:
    they join it in the role it named. The keys come after, by `pickup`."""
    target = target.strip()
    org_id = _invitation_org(target)
    try:   # an invitation named by its id says which one; by the organization, the one open to this address
        got = call("POST", f"/keys/invitations/{quote(org_id, safe='')}",
                   **({"json": {"id": target}} if target.startswith("inv_") else {}))
    except Error as e:
        if e.code == "invitation_for_another_address":   # sent to another address than this sign-in's
            e.next_step = "kennet logout"
            e.advice = "run `kennet logout`, then `kennet login` as the address the invitation was sent to"
            raise
        if getattr(e, "status", None) != 404:
            raise
        e = Error(f"{e.message}; it may have expired or been taken back", code="no_invitation")
        e.advice = f"ask an owner of {org_id} to invite you again"
        raise e from None
    if not isinstance(got, dict):
        raise Error("the server sent no membership")
    return {"org": str(got.get("org") or org_id), "role": got.get("role")}


def managed(name: str | None, what: str = "members") -> dict[str, Any]:
    """The organization a members or keys command acts on: the one named, or the only one the person manages (owner
    or admin). Accounts lists every organization the person is in; only those they manage are theirs to act on.
    `what` is what the refusal says they may not see or change."""
    every = orgs()
    mine = [o for o in every if o.get("role") in MANAGERS]
    unmanaged = not mine or name and name not in [o["id"] for o in mine]
    if unmanaged and (theirs := next((o for o in every if not name or o["id"] == name), None)) is not None:
        raise _not_manager(theirs, what)   # in it, not its owner or admin
    if name:
        for o in mine:
            if o["id"] == name:
                return o
        raise Error(f"you manage no organization {name!r}; yours: {', '.join(o['id'] for o in mine) or 'none'}")
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Error("you are an owner or admin of no organization")
    raise Error(f"you manage {len(mine)} organizations; name one with --org ({', '.join(o['id'] for o in mine)})")


def _not_manager(o: dict[str, Any], what: str = "members") -> Error:
    """A person in an organization who is not its owner or admin, asking for its members or keys: who they are
    there."""
    role = o.get("role") or "member"
    return Error(f"only owners and admins of {o['id']} see or change its {what}; you're "
                 f"{'an' if role[0] in 'aeiou' else 'a'} {'billing member' if role == 'billing' else role}",
                 code="not_manager")


def _member(org_id: str, email: str) -> str:
    return f"/keys/orgs/{quote(org_id, safe='')}/members/{quote(email.strip().lower(), safe='@')}"


def set_member(org_id: str, email: str, role: str) -> dict[str, Any]:
    """Add a person in a role, or change a member's role. An address off the organization's domains is invited
    instead: the answer carries the `link` they accept, signed in as that address."""
    return call("PUT", _member(org_id, email), json={"role": role})


def remove_member(org_id: str, email: str) -> dict[str, Any]:
    """Take a member out, or take back an open invitation to that address."""
    return call("DELETE", _member(org_id, email))


def create_key(org_id: str, name: str, scopes: list[str], test: bool = False) -> dict[str, Any]:
    """A service key of the organization holding `scopes`: the answer carries the key itself (`key`), the one time
    accounts ever says it."""
    made = call("POST", f"/keys/orgs/{quote(org_id, safe='')}", json={"name": name, "scopes": scopes, "test": test})
    if not (isinstance(made, dict) and str(made.get("key") or "").startswith(config.KEY_PREFIX)):
        raise Error("the server sent no key")
    return made


def revoke_key(org_id: str, key_id: str) -> dict[str, Any]:
    """The key stops working at its next check; accounts keeps its row, so what it spent stays named."""
    return call("DELETE", f"/keys/orgs/{quote(org_id, safe='')}/{quote(key_id, safe='')}")


def org_credits() -> list[dict[str, Any]]:
    """The prepaid balance of each organization the person may see it for (owners, admins, billing)."""
    return call("GET", "/credits/orgs")


def credit_of(name: str | None) -> dict[str, Any]:
    """The organization a credits command acts on: the one named, or the only one whose balance the person sees."""
    mine = org_credits()
    if name:
        for o in mine:
            if o["id"] == name:
                return o
        raise Error(f"you see the balance of no organization {name!r}; yours: "
                    f"{', '.join(o['id'] for o in mine) or 'none'}")
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Error("you are an owner, admin or billing member of no organization")
    raise Error(f"you see {len(mine)} organizations; name one with --org ({', '.join(o['id'] for o in mine)})")


def buy(org_id: str, usd: float, say: Callable[[str], None] = print, open_browser: bool = True,
        sleep: Callable[[float], None] = time.sleep, wait_s: float = 1800.0) -> dict[str, Any]:
    """Open a Stripe Checkout for `usd` of the organization's credit, then wait until it is paid."""
    made = call("POST", f"/credits/orgs/{org_id}/checkout", json={"usd": usd})
    if not (isinstance(made, dict) and isinstance(made.get("url"), str) and isinstance(made.get("id"), str)):
        raise Error("the server sent no checkout link")
    url = made["url"]
    if not url.startswith(CHECKOUT):
        raise Error(f"the checkout link is not Stripe's: {url!r}")
    say(f"Pay in the browser:\n  {url}")
    if open_browser:
        webbrowser.open(url)
    deadline = time.time() + wait_s
    while time.time() < deadline:
        sleep(MIN_INTERVAL)
        got = call("GET", f"/credits/orgs/{org_id}/checkout/{made['id']}")
        if got.get("paid"):
            return got
    raise Error("the checkout was not paid in time; run `kennet credits` to see the balance")
