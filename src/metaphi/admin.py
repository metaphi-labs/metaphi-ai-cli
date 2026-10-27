"""Metaphi AI's own admin of accounts: people, organizations and money, on the sign-in `kennet login` keeps.

Every call is accounts' /admin API with the signed-in person's bearer (`accounts.call`). Accounts decides who may
make it: an account holding `metaphi:admin:read` reads, `metaphi:admin` also changes, and every change is written to
accounts' audit under the person's name. A bearer needs no fresh sign-in for these; the role is the check.

A subject (a limit's, a model list's, a grant's) is a person's address or `org:<id>`, as accounts says it."""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from metaphi.accounts import call

ORG = "org:"   # a subject that is an organization's, as accounts names it (`ledger.ORG`)
BILLING = ("prepaid", "invoiced", "internal")   # how an organization's spend is bounded (credits.py `admin_billing`)
TRANSACTION_TYPES = ("topup", "grant", "refund", "usage", "adjustment")   # ledger.TRANSACTION_TYPES


def _q(part: str) -> str:
    return quote(part, safe="@")


def _org(org_id: str) -> str:
    return f"/admin/orgs/{_q(org_id)}"


def _whose(subject: str) -> str:
    """The route a credit or a grant's end is on: the organization's for `org:<id>`, else the person's."""
    return _org(subject[len(ORG):]) if subject.startswith(ORG) else f"/admin/users/{_q(subject.lower())}"


def orgs() -> list[dict[str, Any]]:
    """Every organization: who is in it and as what, its domains, admission, grants, credit and keys."""
    return call("GET", "/admin/accounts/orgs")


def admissions() -> list[dict[str, Any]]:
    """The organizations waiting to be approved, the longest wait first."""
    return call("GET", "/admin/admissions")


def access(org_id: str) -> dict[str, Any]:
    """What Metaphi grants the organization, who manages it, and its keys ({org, admission, grants, roles, keys})."""
    return call("GET", f"{_org(org_id)}/access")


def set_role(org_id: str, email: str, role: str) -> dict[str, Any]:
    """A person in the organization in a role, moved from another organization if they were in one."""
    return call("PUT", f"{_org(org_id)}/roles/{_q(email.strip().lower())}", json={"role": role})


def grant_org(org_id: str, scope: str) -> dict[str, Any]:
    return call("PUT", f"{_org(org_id)}/grants/{_q(scope)}")


def ungrant_org(org_id: str, scope: str) -> dict[str, Any]:
    """The grant goes, and that scope from every key of the organization with it."""
    return call("DELETE", f"{_org(org_id)}/grants/{_q(scope)}")


def admit(org_id: str) -> dict[str, Any]:
    """Approve the organization for Kennet; its owners are told."""
    return call("PUT", f"{_org(org_id)}/admission")


def unadmit(org_id: str) -> dict[str, Any]:
    return call("DELETE", f"{_org(org_id)}/admission")


def billing(org_id: str, mode: str, credit_limit_usd: float | None) -> dict[str, Any]:
    return call("PUT", f"{_org(org_id)}/billing", json={"mode": mode, "credit_limit_usd": credit_limit_usd})


def funds_members(org_id: str, on: bool) -> dict[str, Any]:
    return call("PUT", f"{_org(org_id)}/funds-members", json={"funds_members": on})


def transactions(org_id: str, limit: int, type: str | None = None) -> dict[str, Any]:
    """The organization's ledger entries, newest first, with its balance now."""
    return call("GET", f"{_org(org_id)}/transactions", params={"limit": limit, **({"type": type} if type else {})})


def spend(email: str | None, month: str | None) -> dict[str, Any]:
    """One person's month as they read it, or with no `email` every person's in total."""
    return call("GET", "/admin/spend", params={k: v for k, v in (("email", email), ("month", month)) if v})


def grants(email: str) -> dict[str, Any]:
    """What a person holds on every resource, the grants set by hand, and their admin tier."""
    return call("GET", f"/admin/users/{_q(email.lower())}/grants")


def grant(email: str, scope: str) -> dict[str, Any]:
    return call("PUT", f"/admin/users/{_q(email.lower())}/grants/{_q(scope)}")


def revoke(email: str, scope: str) -> dict[str, Any]:
    return call("DELETE", f"/admin/users/{_q(email.lower())}/grants/{_q(scope)}")


def credit(subject: str, usd: float, days: float | None, note: str | None) -> dict[str, Any]:
    """Credit from our side on a person or an organization, for any spend; `days` bounds it, else it never ends."""
    return call("POST", f"{_whose(subject)}/credits", json={"usd": usd, "days": days, "note": note})


def end(subject: str, grant_id: int) -> dict[str, Any]:
    """End a grant we gave, now: what it had left is gone."""
    return call("POST", f"{_whose(subject)}/credits/{grant_id}/end")


def limits(subject: str) -> dict[str, Any]:
    return call("GET", "/admin/limits", params={"subject": subject})


def set_limit(subject: str, product: str, usd: float | None) -> dict[str, Any]:
    """A month's cap on the subject for a product (`*` every product, `route:<route>` one route); None takes it away."""
    return call("PUT", "/admin/limits", json={"subject": subject, "product": product, "usd": usd})


def models(subject: str) -> dict[str, Any]:
    return call("GET", "/admin/models", params={"subject": subject})


def set_models(subject: str, product: str, patterns: list[str] | None) -> dict[str, Any]:
    """The model patterns the subject may call in a product; None takes the list away (any model it offers)."""
    return call("PUT", "/admin/models", json={"subject": subject, "product": product, "models": patterns})


def audit(actor: str | None, days: int, limit: int) -> list[dict[str, Any]]:
    """The changes admins made, newest first."""
    return call("GET", "/admin/audit", params={"days": days, "limit": limit, **({"actor": actor} if actor else {})})
