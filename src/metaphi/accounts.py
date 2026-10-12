"""Your Metaphi AI account: sign in from the terminal (RFC 8628, as `stripe login` does) and be handed your
organization's keys, the organizations you manage, and their prepaid credit."""
from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from typing import Any

import httpx

from metaphi import config
from metaphi.errors import MetaphiError

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "metaphi-cli"


CHECKOUT = "https://checkout.stripe.com/"  # the one place a checkout link may send the browser
MIN_INTERVAL = 5.0  # RFC 8628 §3.5: the default, and the floor whatever the server says


class Error(MetaphiError):
    """A refusal in the server's own sentence, or one the CLI makes itself; the CLI's own carry a `code` and the
    `next_step` that clears them (`need`)."""


def need(message: str, code: str, next_step: str | None) -> Error:
    """A refusal the CLI makes because a credential is missing or spent: its code, and the command that clears it
    (None when only someone else can)."""
    return Error(message, code=code, type="auth_error", next_step=next_step)


def _said(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error_description") or body.get("error") or body)
    return str(body)


def _json(r: httpx.Response) -> dict[str, Any]:
    """The body as a JSON object; anything else is the server's sentence as an error."""
    try:
        body = r.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise Error(_said(r))
    return body


def _http() -> httpx.Client:
    return httpx.Client(base_url=config.accounts_url(), timeout=30.0,
                        headers={"user-agent": f"metaphi-cli/{__import__('metaphi').__version__}"})


def start() -> dict[str, Any]:
    """Ask for a device code (RFC 8628 §3.1) and keep it as the sign-in under way: what the person is shown
    (`verification_uri`, `user_code`) and what `finish` polls with."""
    with _http() as h:
        r = h.post("/device/code", data={"client_id": CLIENT_ID})
    if r.status_code != 200:
        raise Error(_said(r))
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
    for name in ("pending", "device_code", "test_api_key", "org", "no_api_key", "no_api_key_code"):
        cfg.pop(name, None)  # the server revoked the keys it handed last
    cfg.update({"token": token, "email": body.get("email")})
    if all(str(body.get(k) or "").startswith(config.KEY_PREFIX) for k in ("api_key", "test_api_key")):
        cfg.update({"api_key": body["api_key"], "test_api_key": body["test_api_key"]})
    # why whoami and twin can say, later, what this sign-in was and was not given
    cfg.update({k: body[k] for k in ("org", "no_api_key", "no_api_key_code")
                if isinstance(body.get(k), str) and body[k]})
    config.save(cfg)


def pickup() -> dict[str, Any] | None:
    """A machine that is signed in asks for its organization's keys again, by the token it holds: what a sign-in
    hands over, with no browser. The organization was made, or approved, after the sign-in, and its keys come
    here. None when this machine is not signed in or its sign-in has ended: then the person signs in."""
    token = config.load().get("token")
    if not token:
        return None
    with _http() as h:
        r = h.post("/keys/cli", headers={"authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return None
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
    for name in ("email", "api_key", "test_api_key", "org", "no_api_key", "no_api_key_code", "pending",
                 "device_code"):
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
    if r.status_code == 401:
        raise need("your sign-in has ended; run `kennet login`", "not_signed_in", "kennet login")
    if r.status_code >= 400:
        raise Error(_said(r))
    return r.json()


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
