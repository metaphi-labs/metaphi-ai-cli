"""Your Metaphi AI account: sign in from the terminal (RFC 8628, as `stripe login` does), the organizations you
manage, their people and their keys, and an organization key exchanged for a short-lived token on one product."""
from __future__ import annotations

import time
import webbrowser
from collections.abc import Callable
from typing import Any

import httpx

from metaphi import config

DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
CLIENT_ID = "metaphi-cli"


CHECKOUT = "https://checkout.stripe.com/"  # the one place a checkout link may send the browser
MIN_INTERVAL = 5.0  # RFC 8628 §3.5: the default, and the floor whatever the server says


class Error(config.Error):
    """A refusal, in the server's own sentence."""


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


def login(say: Callable[[str], None] = print, open_browser: bool = True,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Sign the person in: show a code, open the browser on it, wait for the approval, keep the token."""
    with _http() as h:
        r = h.post("/device/code", data={"client_id": CLIENT_ID})
        if r.status_code != 200:
            raise Error(_said(r))
        code = _json(r)
        if not all(isinstance(code.get(k), str) for k in ("device_code", "user_code", "verification_uri_complete")):
            raise Error("the sign-in server sent an incomplete device code")
        say(f"Your code is {code['user_code']}. Confirm it in the browser:\n  {code['verification_uri_complete']}")
        if open_browser:
            webbrowser.open(code["verification_uri_complete"])
        try:
            interval = max(MIN_INTERVAL, float(code.get("interval") or MIN_INTERVAL))
            deadline = time.time() + min(float(code.get("expires_in") or 600), 1800.0)
        except (TypeError, ValueError):
            raise Error("the sign-in server sent an unreadable device code") from None
        while time.time() < deadline:
            sleep(interval)
            r = h.post("/oauth/token", data={"grant_type": DEVICE_GRANT, "device_code": code["device_code"],
                                              "client_id": CLIENT_ID})
            body = _json(r)
            if r.status_code == 200:
                if not isinstance(body.get("access_token"), str):
                    raise Error("the sign-in server sent no access token")
                cfg = config.load()
                cfg.update({"token": body["access_token"], "email": body.get("email")})
                config.save(cfg)
                return body
            error = body.get("error")
            if error == "authorization_pending":
                continue
            if error == "slow_down":
                interval += 5
                continue
            raise Error({"expired_token": "the code expired; run `metaphi login` again",
                         "access_denied": "the sign-in was declined"}.get(error, _said(r)))
    raise Error("the code expired; run `metaphi login` again")


def logout() -> None:
    cfg = config.load()
    token = cfg.pop("token", None)
    cfg.pop("email", None)
    config.save(cfg)
    if token:
        with _http() as h:
            h.post("/logout", headers={"authorization": f"Bearer {token}"})


def _person() -> dict[str, str]:
    token = config.load().get("token")
    if not token:
        raise Error("not signed in; run `metaphi login`")
    return {"authorization": f"Bearer {token}"}


def call(method: str, path: str, **kw: Any) -> Any:
    """One request as the signed-in person."""
    with _http() as h:
        r = h.request(method, path, headers=_person(), **kw)
    if r.status_code == 401:
        raise Error("your sign-in has ended; run `metaphi login`")
    if r.status_code >= 400:
        raise Error(_said(r))
    return r.json()


def orgs() -> list[dict[str, Any]]:
    return call("GET", "/keys/orgs")


def org(name: str | None) -> dict[str, Any]:
    """The organization a command acts on: the one named, or the only one the person manages."""
    mine = orgs()
    if name:
        for o in mine:
            if o["id"] == name:
                return o
        raise Error(f"you manage no organization {name!r}; yours: {', '.join(o['id'] for o in mine) or 'none'}")
    if len(mine) == 1:
        return mine[0]
    if not mine:
        raise Error("you are an owner or admin of no organization; ask your organization's owner to make you one")
    raise Error(f"you manage {len(mine)} organizations; name one with --org ({', '.join(o['id'] for o in mine)})")


def create_key(org_id: str, name: str, scopes: list[str]) -> dict[str, Any]:
    return call("POST", f"/keys/orgs/{org_id}", json={"name": name, "scopes": scopes})


def revoke_key(org_id: str, key_id: str) -> dict[str, Any]:
    return call("DELETE", f"/keys/orgs/{org_id}/{key_id}")


def add_member(org_id: str, email: str, role: str) -> dict[str, Any]:
    return call("PUT", f"/keys/orgs/{org_id}/members/{email}", json={"role": role})


def remove_member(org_id: str, email: str) -> dict[str, Any]:
    return call("DELETE", f"/keys/orgs/{org_id}/members/{email}")


def token_for(key: str, resource: str) -> str:
    """An organization key exchanged for an hour-long token on one product (RFC 6749 §4.4)."""
    if not key.startswith(config.KEY_PREFIX):
        raise Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
    with _http() as h:
        r = h.post("/oauth/token", data={"grant_type": "client_credentials", "client_secret": key,
                                          "resource": resource})
    if r.status_code != 200:
        raise Error(_said(r))
    token = _json(r).get("access_token")
    if not isinstance(token, str):
        raise Error("the sign-in server sent no access token")
    return token


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
    raise Error("the checkout was not paid in time; run `metaphi credits` to see the balance")
