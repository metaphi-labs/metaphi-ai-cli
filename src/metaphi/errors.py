"""Typed errors, as the twin service says them: switch on the class or on `code`, never on the message.

Every error the service answers carries {type, code, message, retryable, request_id}, run_id for a failed run, and
action_url when there is a page that fixes it (`out_of_credit`: where credit is added).
`type` picks the class (`BillingError` for `billing_error`, and so on); `code` is the stable reason, as `out_of_credit`
or `rate_limited_concurrent`; `request_id` is what support looks the call up by. An error the CLI says itself, or one a
server answers without a code, is a plain `MetaphiError` with `code` None."""
from __future__ import annotations

from typing import Any

from metaphi import config


class MetaphiError(config.Error):
    """An error with the service's code, type, retryability and request id, when it gave them."""

    type: str | None = None

    def __init__(self, message: str, code: str | None = None, type: str | None = None, retryable: bool = False,
                 request_id: str | None = None, run_id: str | None = None, action_url: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.type = type or self.type
        self.retryable = retryable
        self.request_id = request_id
        self.run_id = run_id
        self.action_url = action_url

    def to_dict(self) -> dict[str, Any]:
        """The error object, as the service said it."""
        out: dict[str, Any] = {"type": self.type, "code": self.code, "message": self.message,
                               "retryable": self.retryable}
        out.update({k: v for k, v in (("request_id", self.request_id), ("run_id", self.run_id),
                                      ("action_url", self.action_url)) if v})
        return out

    def __str__(self) -> str:
        said = [f"code: {self.code}"] if self.code else []
        said += [f"request: {self.request_id}"] if self.request_id else []
        return f"{self.message} ({', '.join(said)})" if said else self.message


class BillingError(MetaphiError):
    """The organization cannot pay for the call: `out_of_credit`."""
    type = "billing_error"


class AuthError(MetaphiError):
    """The credential cannot make the call: `no_organization` and the like."""
    type = "auth_error"


class RateLimitError(MetaphiError):
    """Too many runs at once or in a day; retryable once one ends."""
    type = "rate_limit_error"


class InvalidRequestError(MetaphiError):
    """The call's arguments are wrong, or an upload is spent; retrying unchanged will not help."""
    type = "invalid_request_error"


class IdempotencyError(MetaphiError):
    """An Idempotency-Key reused with a different request, or still in flight (`retryable`)."""
    type = "idempotency_error"


class RunError(MetaphiError):
    """The run started and failed; nothing was written or charged."""
    type = "run_error"


class APIError(MetaphiError):
    """The service could not carry the call out; `retryable` says whether trying again may help."""
    type = "api_error"


BY_TYPE: dict[str, type[MetaphiError]] = {c.type: c for c in (BillingError, AuthError, RateLimitError,
                                                                 InvalidRequestError, IdempotencyError, RunError,
                                                                 APIError)}


def from_object(error: dict[str, Any]) -> MetaphiError:
    """The typed exception for an error object; an unknown type is a plain `MetaphiError` keeping it."""
    kind = error.get("type")
    cls = BY_TYPE.get(kind, MetaphiError) if isinstance(kind, str) else MetaphiError

    def text(key: str) -> str | None:
        value = error.get(key)
        return value if isinstance(value, str) and value else None

    return cls(text("message") or text("code") or "the call failed", code=text("code"),
               type=kind if isinstance(kind, str) else None, retryable=error.get("retryable") is True,
               request_id=text("request_id"), run_id=text("run_id"), action_url=text("action_url"))
