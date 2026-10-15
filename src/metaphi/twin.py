"""Twin a trace: upload it, wait for its twin, download it. One file in, one file out, same format. A twin started
is the service's: it is read (`get`, `recent`) and come back for (`wait`) by its id from any machine holding the key.

A client of the Kennet API (METAPHI_API, default https://api.metaphi.ai/v1/kennet), authenticated by the organization
key as a bearer, at the API version it was written for (Metaphi-Version): POST /twins with the file, GET /twins/{id}
until it ends (backing off, for at most `deadline_s`: METAPHI_TWIN_TIMEOUT or `--timeout`, default two hours), GET
its content, written whole and synced before it takes the output's name. The upload
carries an Idempotency-Key drawn once per call (or the caller's, to make the same call again), so a retry after a
dropped connection or a 5xx never starts a second run. Every refusal or failed run raises the typed error the API said (metaphi.errors), its code and request id kept.

A request that failed in transit or answered a retryable error is tried again at most `RETRIES` times, within
`BUDGET_S` in all: the caller is as often an agent with a clock of its own as a person, and a wait it cannot see
is a hang. Past that the error is the answer, with how long the service said to wait (`retry_after_s`)."""
from __future__ import annotations

import os
import random
import shlex
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from metaphi import accounts, config, errors

ENDED = ("completed", "failed")
RETRIES = 2                 # a request that failed in transit or answered a retryable error is sent again this often
BASE_S = 0.5                # a retry waits a random time up to this, doubled each time (full jitter)
BUDGET_S = 5.0              # all of one request's retries end within this; a longer Retry-After is not waited out
FIRST_WAIT_S = 1.0          # the first poll's wait; each next one 1.5 times longer, up to MAX_WAIT_S
MAX_WAIT_S = 15.0
CONSISTENCY_KEY = "KENNET_CONSISTENCY_KEY"   # the consistency key, for a caller that keeps it off the command line
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


def _retry_after(r: httpx.Response) -> float | None:
    """Seconds the answer says to wait, when it says."""
    held = r.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(held)) if held else None
    except ValueError:
        return None


class _Tries:
    """One request's retries: how many are left, and how much of the budget."""

    def __init__(self, sleep: Callable[[float], None]) -> None:
        self.sleep, self.made, self.until = sleep, 0, time.monotonic() + BUDGET_S

    def again(self, retry_after: float | None = None) -> bool:
        """Wait before one more attempt and say True; False once the retries or the budget are spent, or the
        service asks for a wait the budget does not hold."""
        left = self.until - time.monotonic()
        wait = random.uniform(0, BASE_S * 2 ** self.made) if retry_after is None else retry_after
        if self.made >= RETRIES or left <= 0 or wait > left:
            return False
        self.made += 1
        self.sleep(wait)
        return True


def _send(h: httpx.Client, method: str, path: str, sleep: Callable[[float], None], **kw: Any) -> httpx.Response:
    """One request, tried again when it failed in transit or the API says waiting may help; a refusal raises."""
    tries = _Tries(sleep)
    while True:
        try:
            r = h.request(method, path, **kw)
        except httpx.TransportError as e:
            if tries.again():
                continue
            raise errors.APIError(f"the twin service did not answer ({type(e).__name__})", code="unreachable",
                                  retryable=True) from None
        if r.status_code < 400:
            return r
        error = _error(r)
        error.retry_after_s = _retry_after(r)
        # a rate limit is the caller's to wait out; a 409 in flight or a 5xx clears in seconds
        if not error.retryable or error.type == "rate_limit_error" or not tries.again(error.retry_after_s):
            raise error


UPLOAD = httpx.Timeout(30.0, connect=5.0, read=600.0, write=600.0)   # a file's own time, up or down


def _client(key: str) -> httpx.Client:
    from metaphi.update import floor
    return httpx.Client(base_url=config.api_url(), timeout=httpx.Timeout(30.0, connect=5.0),
                        event_hooks={"response": [floor]}, headers={"authorization": f"Bearer {key}", "metaphi-version": API_VERSION,
                                 "user-agent": f"metaphi-cli/{__import__('metaphi').__version__}"})


def _download(h: httpx.Client, twin_id: str, out: Path, sleep: Callable[[float], None]) -> None:
    """The twin's content streamed into `out`, written whole or not at all."""
    tmp = out.with_name(f".{out.name}.{uuid.uuid4().hex[:8]}.part")
    tries = _Tries(sleep)
    while True:
        try:
            with h.stream("GET", f"/twins/{twin_id}/content", timeout=UPLOAD) as r:
                if r.status_code >= 400:
                    r.read()
                    error = _error(r)
                    error.retry_after_s = _retry_after(r)
                    if not error.retryable or not tries.again(error.retry_after_s):
                        raise error
                    continue
                with open(tmp, "wb") as f:
                    f.writelines(r.iter_bytes())
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, out)
                _sync_dir(out.parent)
                return
        except httpx.TransportError as e:
            tmp.unlink(missing_ok=True)
            if not tries.again():
                raise errors.APIError(f"the twin service did not answer ({type(e).__name__})", code="unreachable",
                                      retryable=True) from None
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise


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


def _key(api_key: str | None, test: bool) -> str:
    """The organization key a call runs on, or why there is none and what gets one."""
    key = config.api_key(api_key, test)
    if not key:
        cfg = config.load()
        if not cfg.get("token"):
            raise accounts.need("no test key: run `kennet login`, or set METAPHI_API_KEY to a test key" if test else
                                "no key: run `kennet login`, or set METAPHI_API_KEY", "not_signed_in",
                                "kennet login")
        raise no_key(cfg)
    if not key.startswith(config.KEY_PREFIX):
        raise accounts.Error(f"that is not an organization key ({config.KEY_PREFIX}...)")
    return key


def no_key(cfg: dict[str, Any], again: bool = True) -> accounts.Error:
    """Signed in, and no key came with the sign-in. `kennet login` picks the keys up once the organization has
    access, by the token this machine holds and with no browser; `again` is False where that was just run. A
    person in no organization makes theirs first."""
    why = cfg.get("no_api_key") or "your organization has no key for this machine yet"
    reason = cfg.get("no_api_key_code")
    step = {"no_organization": "kennet org create <name>"}.get(reason, "kennet login" if again else None)
    return accounts.need(f"no key: {why}. Signed in as {cfg.get('email')}; `kennet login` picks the keys up once "
                         "that changes, or set METAPHI_API_KEY", "no_api_key", step)


def check(key: str, sleep: Callable[[float], None] = time.sleep) -> errors.MetaphiError | None:
    """Whether the twin service takes `key` now: None, or the error a call on it would meet."""
    try:
        with _client(key) as h:
            _send(h, "GET", "/twins", sleep, params={"limit": 1})
    except errors.MetaphiError as e:
        return e
    return None


def consistency_key(flag: str | None = None, file: Path | None = None) -> str:
    """The consistency key a twin runs on: the argument, then the file named, then KENNET_CONSISTENCY_KEY; none
    draws a fresh one. A file or the variable keeps it out of the process list."""
    if flag:
        return flag
    if file is not None:
        try:
            return file.read_text().strip()
        except OSError as e:
            raise accounts.Error(f"cannot read the key file {file}: {e.strerror or e}") from None
    return os.environ.get(CONSISTENCY_KEY, "").strip()


def again(name: str, test: bool = False) -> str:
    """The command that twins a file once more."""
    return f"kennet twin {shlex.quote(name)}" + (" --test" if test else "")


def get(twin_id: str, api_key: str | None = None, test: bool = False,
        sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One twin as it stands now: {id, status, created, completed, filename, error, livemode}."""
    with _client(_key(api_key, test)) as h:
        return _send(h, "GET", f"/twins/{twin_id}", sleep).json()


def recent(limit: int = 10, api_key: str | None = None, test: bool = False,
           sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """The organization's twins, newest first; a test key lists test twins."""
    with _client(_key(api_key, test)) as h:
        return _send(h, "GET", "/twins", sleep, params={"limit": limit}).json()["data"]


def back(twin_id: str, test: bool = False) -> str:
    """The command that comes back for a twin."""
    return f"kennet twins wait {twin_id}" + (" --test" if test else "")


def _finish(h: httpx.Client, made: dict[str, Any], out: Path, say: Callable[[str], None],
            sleep: Callable[[float], None], wait_s: float, clock: Callable[[], float], back: str,
            again: str | None = None) -> None:
    """Wait for a twin to end and write its content to `out`; a failed run raises its typed error. Stopping the
    wait (its deadline, Ctrl-C) stops no twin: it says how to come back for it."""
    twin_id, status, wait = made["id"], made["status"], FIRST_WAIT_S
    last, until = status, clock() + wait_s
    try:
        while status not in ENDED:
            if clock() + wait > until:
                raise errors.APIError(f"{twin_id} did not end within {wait_s:.0f}s; it goes on: `{back}` for it",
                                      code="timed_out", retryable=True, run_id=twin_id, next_step=back)
            sleep(wait)
            wait = min(wait * 1.5, MAX_WAIT_S)
            made = _send(h, "GET", f"/twins/{twin_id}", sleep).json()
            status = made["status"]
            if status != last:
                say(f"  {status}")
                last = status
    except KeyboardInterrupt:
        say(f"stopped waiting; {twin_id} goes on. Come back for it: {back}")
        raise
    if status != "completed":
        error = made.get("error") if isinstance(made.get("error"), dict) else {}
        failed = errors.from_object({"type": "run_error", "code": None, "message": f"twinning {status}",
                                     "run_id": twin_id, **error})
        failed.next_step = again if failed.retryable else None   # a failed run is spent: the file is twinned anew
        raise failed
    _download(h, twin_id, out, sleep)
    say(f"wrote {out}")


def _into(out: Path | None, out_dir: Path | None, name: Path) -> Path:
    """Where a twin is written: `out`, else <name>.twin<ext> in `out_dir` (made if missing), else beside `name`."""
    if out is not None:
        return out
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{name.stem}.twin{name.suffix}"
    return name.with_name(f"{name.stem}.twin{name.suffix}")


def wait(twin_id: str, out: Path | None = None, api_key: str | None = None, say: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, deadline_s: float | None = None,
         clock: Callable[[], float] = time.monotonic, test: bool = False,
         out_dir: Path | None = None) -> dict[str, Any]:
    """Come back for a twin started earlier: wait until it ends and write it to `out` (default <name>.twin<ext>
    here, or in `out_dir`, by the name of the file it was made from); answers {"output", "id"}."""
    with _client(_key(api_key, test)) as h:
        made = _send(h, "GET", f"/twins/{twin_id}", sleep).json()
        name = Path(Path(str(made.get("filename") or "")).name or "trace.txt")
        out = _into(out, out_dir, name)
        say(f"twinning ({twin_id}): {made['status']}")
        _finish(h, made, out, say, sleep, deadline() if deadline_s is None else deadline_s, clock,
                back(twin_id, test), again(name.name, test))
    return {"output": str(out), "id": twin_id}


def twin(trace: Path, out: Path | None = None, api_key: str | None = None, key_hex: str = "",
         confidential: list[str] | None = None, say: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep, deadline_s: float | None = None,
         clock: Callable[[], float] = time.monotonic, test: bool = False,
         idempotency_key: str | None = None, detach: bool = False,
         out_dir: Path | None = None) -> dict[str, Any]:
    """Twin one trace file into `out` (default <name>.twin<ext> beside it, or in `out_dir`); answers {"output", "id"}. Waiting past
    `deadline_s` (default `deadline()`) raises a retryable APIError naming the twin, which goes on running.
    `test` runs on the test key `kennet login` saved: free, small, apart from live twins. `idempotency_key` makes
    a call the same call as an earlier one that was refused. `detach` answers {"id", "status", "next_step"} once the twin is
    started and writes nothing: `wait` comes back for it. Never prompts: a refusal raises its typed error."""
    key = _key(api_key, test)
    if not trace.is_file():
        raise accounts.Error(f"no file {trace}")
    out = _into(out, out_dir, trace)
    started = time.time()
    wait_s = deadline() if deadline_s is None else deadline_s
    form: dict[str, Any] = {"key": key_hex} if key_hex else {}
    if confidential:
        form["confidential"] = list(confidential)
    with _client(key) as h:
        body = trace.read_bytes()
        made = _send(h, "POST", "/twins", sleep, files={"file": (trace.name, body)}, data=form, timeout=UPLOAD,
                     headers={"idempotency-key": idempotency_key or uuid.uuid4().hex}).json()
        say(f"uploaded {trace.name} ({len(body):,} bytes)")
        twin_id = made["id"]
        say(f"twinning ({twin_id})")
        if detach:
            say(f"Come back for it: {back(twin_id, test)}")
            return {"id": twin_id, "status": made["status"], "next_step": back(twin_id, test)}
        _finish(h, made, out, say, sleep, wait_s, clock, back(twin_id, test), again(str(trace), test))
    say(f"done in {time.time() - started:.0f}s")
    return {"output": str(out), "id": twin_id}
