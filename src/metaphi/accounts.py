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


class Error(Exception):
    """A refusal, in the server's own sentence."""


def _said(r: httpx.Response) -> str:
    try:
        body = r.json()
    except ValueError:
        return r.text.strip() or f"HTTP {r.status_code}"
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("error_description") or body.get("error") or body)
    return str(body)


def _http() -> httpx.Client:
    return httpx.Client(base_url=config.ACCOUNTS, timeout=30.0,
                        headers={"user-agent": f"metaphi-cli/{__import__('metaphi').__version__}"})


def login(say: Callable[[str], None] = print, open_browser: bool = True,
          sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Sign the person in: show a code, open the browser on it, wait for the approval, keep the token."""
    with _http() as h:
        r = h.post("/device/code", json={"client": CLIENT_ID})
        if r.status_code != 200:
            raise Error(_said(r))
        code = r.json()
        say(f"Your code is {code['user_code']}. Confirm it in the browser:\n  {code['verification_uri_complete']}")
        if open_browser:
            webbrowser.open(code["verification_uri_complete"])
        interval, deadline = float(code.get("interval") or 3), time.time() + float(code.get("expires_in") or 600)
        while time.time() < deadline:
            sleep(interval)
            r = h.post("/oauth/token", data={"grant_type": DEVICE_GRANT, "device_code": code["device_code"],
                                              "client_id": CLIENT_ID})
            body = r.json()
            if r.status_code == 200:
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
    return r.json()["access_token"]
