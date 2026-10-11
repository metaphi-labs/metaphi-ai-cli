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
    """A refusal, in the server's own sentence, with no code: accounts' own, or one the CLI makes itself."""


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
    """Sign the person in: show a code, open the browser on it, wait for the approval, keep the token and the
    organization's keys that come with it (`api_key`, `test_api_key`; `no_api_key` says why none came)."""
    with _http() as h:
        r = h.post("/device/code", data={"client_id": CLIENT_ID})
        if r.status_code != 200:
            raise Error(_said(r))
        code = _json(r)
        if not all(isinstance(code.get(k), str) and code[k] for k in ("device_code", "user_code", "verification_uri")):
            raise Error("the sign-in server sent an incomplete device code")
        # RFC 8628 §3.3: the person types the code at verification_uri; verification_uri_complete is optional.
        uri, complete = code["verification_uri"], code.get("verification_uri_complete")
        config.check_url(uri, "the sign-in page")
        say(f"Open {uri} and enter the code {code['user_code']}")
        if open_browser:
            webbrowser.open(config.check_url(complete, "the sign-in page") if isinstance(complete, str) and complete
                            else uri)
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
                cfg.pop("test_api_key", None)  # the server revoked the last login's keys
                if all(str(body.get(k) or "").startswith(config.KEY_PREFIX) for k in ("api_key", "test_api_key")):
                    cfg.update({"api_key": body["api_key"], "test_api_key": body["test_api_key"]})
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
    for name in ("email", "api_key", "test_api_key"):
        cfg.pop(name, None)
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
