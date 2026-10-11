"""Twin a trace: upload it, wait for its twin, download it. One file in, one file out, same format.

A client of the Kennet API (METAPHI_API, default https://api.metaphi.ai/v1/kennet), authenticated by the organization
key as a bearer, at the API version it was written for (Metaphi-Version): POST /twins with the file, GET /twins/{id}
until it ends (backing off, for at most `deadline_s`: METAPHI_TWIN_TIMEOUT or `--timeout`, default two hours), GET
its content, written whole and synced before it takes the output's name. The upload
carries an Idempotency-Key drawn once per call, so a retry after a dropped connection or a 5xx never starts a second
run. Every refusal or failed run raises the typed error the API said (metaphi.errors), its code and request id kept."""
from __future__ import annotations

import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from metaphi import accounts, config, errors

ENDED = ("completed", "failed")
TRIES = 4                   # attempts of one request that failed in transit or answered a retryable error
FIRST_WAIT_S = 1.0          # the first poll's wait; each next one 1.5 times longer, up to MAX_WAIT_S
MAX_WAIT_S = 15.0
MAX_RETRY_AFTER_S = 120.0
API_VERSION = "2026-10-11"  # the API version this client speaks
DEADLINE_S = 2 * 3600.0     # longest a call waits for its twin to end, unless told otherwise


def _error(r: httpx.Response) -> errors.MetaphiError:
    """The typed error an answer carries; an answer without one is a plain error with its status."""
    try:
        body = r.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return errors.from_object({"request_id": r.headers.get("request-id"), **error})
    return errors.APIError(f"the twin service answered HTTP {r.status_code}", retryable=r.status_code >= 500,
                           request_id=r.headers.get("request-id"))


def _wait(r: httpx.Response | None, attempt: int) -> float:
    held = r.headers.get("retry-after", "") if r is not None else ""
    if held.isdigit():
        return min(float(held), MAX_RETRY_AFTER_S)
    return min(FIRST_WAIT_S * 2 ** attempt, MAX_WAIT_S)


def _send(h: httpx.Client, method: str, path: str, sleep: Callable[[float], None], **kw: Any) -> httpx.Response:
    """One request, tried again when it failed in transit or the API says waiting may help; a refusal raises."""
    for attempt in range(TRIES):
        try:
            r = h.request(method, path, **kw)
        except httpx.TransportError as e:
            if attempt == TRIES - 1:
                raise errors.APIError(f"the twin service did not answer ({type(e).__name__})", retryable=True) from None
            sleep(_wait(None, attempt))
            continue
        if r.status_code < 400:
            return r
        error = _error(r)
        # a rate limit is the caller's to wait out; a 409 in flight or a 5xx clears in seconds
        if attempt == TRIES - 1 or not error.retryable or error.type == "rate_limit_error":
            raise error
        sleep(_wait(r, attempt))
    raise AssertionError("unreachable")


def _client(key: str) -> httpx.Client:
    return httpx.Client(base_url=config.api_url(), timeout=httpx.Timeout(60.0, read=600.0),
                        headers={"authorization": f"Bearer {key}", "metaphi-version": API_VERSION,
                                 "user-agent": f"metaphi-cli/{__import__('metaphi').__version__}"})


def _download(h: httpx.Client, twin_id: str, out: Path, sleep: Callable[[float], None]) -> None:
    """The twin's content streamed into `out`, written whole or not at all."""
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    for attempt in range(TRIES):
        try:
            with h.stream("GET", f"/twins/{twin_id}/content") as r:
                if r.status_code >= 400:
                    r.read()
                    error = _error(r)
                    if attempt == TRIES - 1 or not error.retryable:
                        raise error
                else:
                    with open(tmp, "wb") as f:
                        f.writelines(r.iter_bytes())
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, out)
                    _sync_dir(out.parent)
                    return
        except httpx.TransportError as e:
            tmp.unlink(missing_ok=True)
            if attempt == TRIES - 1:
                raise errors.APIError(f"the twin service did not answer ({type(e).__name__})", retryable=True) from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        sleep(_wait(None, attempt))


def _sync_dir(d: Path) -> None:
    """The rename itself on disk, where the system lets a directory be synced."""
    try:
        fd = os.open(d, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def deadline() -> float:
    """Seconds a call waits for its twin: METAPHI_TWIN_TIMEOUT, else DEADLINE_S."""
    said = os.environ.get("METAPHI_TWIN_TIMEOUT", "").strip()
    try:
        got = float(said) if said else DEADLINE_S
    except ValueError:
        raise accounts.Error(f"METAPHI_TWIN_TIMEOUT is {said!r}; it is a number of seconds") from None
    if got <= 0:
        raise accounts.Error("METAPHI_TWIN_TIMEOUT is a number of seconds above zero")
    return got


def twin(trace: Path, out: Path | None = None, api_key: str | None = None, key_hex: str = "",
         confidential: list[str] | None = None, say: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, deadline_s: float | None = None,
         clock: Callable[[], float] = time.monotonic, test: bool = False) -> dict[str, Any]:
    """Twin one trace file into `out` (default <name>.twin<ext>); answers {"output", "id"}. Waiting past
    `deadline_s` (default `deadline()`) raises a retryable APIError naming the twin, which goes on running.
    `test` runs on the test key `metaphi login` saved: free, small, apart from live twins."""
    key = config.api_key(api_key, test)
    if not key:
        raise accounts.Error("no test key: run `metaphi login`, or set METAPHI_API_KEY to a test key" if test else
                             "no key: run `metaphi login`, or set METAPHI_API_KEY")
    if not key.startswith(config.KEY_PREFIX):
        raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
    if not trace.is_file():
        raise accounts.Error(f"no file {trace}")
    out = out or trace.with_name(f"{trace.stem}.twin{trace.suffix}")
    started = time.time()
    wait_s = deadline() if deadline_s is None else deadline_s
    form: dict[str, Any] = {"key": key_hex} if key_hex else {}
    if confidential:
        form["confidential"] = list(confidential)
    with _client(key) as h:
        body = trace.read_bytes()
        made = _send(h, "POST", "/twins", sleep, files={"file": (trace.name, body)}, data=form,
                     headers={"idempotency-key": uuid.uuid4().hex}).json()
        say(f"uploaded {trace.name} ({len(body):,} bytes)")
        twin_id, status, wait = made["id"], made["status"], FIRST_WAIT_S
        say(f"twinning ({twin_id})")
        last, until = status, clock() + wait_s
        while status not in ENDED:
            if clock() + wait > until:
                raise errors.APIError(f"{twin_id} did not end within {wait_s:.0f}s; it goes on: "
                                      f"GET /twins/{twin_id} for it", code="timed_out", retryable=True,
                                      run_id=twin_id)
            sleep(wait)
            wait = min(wait * 1.5, MAX_WAIT_S)
            made = _send(h, "GET", f"/twins/{twin_id}", sleep).json()
            status = made["status"]
            if status != last:
                say(f"  {status}")
                last = status
        if status != "completed":
            error = made.get("error") if isinstance(made.get("error"), dict) else {}
            raise errors.from_object({"type": "run_error", "code": None, "message": f"twinning {status}",
                                      **error})
        _download(h, twin_id, out, sleep)
    say(f"wrote {out}")
    say(f"done in {time.time() - started:.0f}s")
    return {"output": str(out), "id": twin_id}
