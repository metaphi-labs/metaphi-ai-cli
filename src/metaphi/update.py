"""Staying current, as `gh` and Claude Code do: a server's version floor, a notice when a newer release is out, and a
standalone binary that replaces itself.

    426 upgrade_required        a server refuses this version: the error names the upgrade command for this install
    the notice                  at most once a day, after the command, on stderr; never under --json, off a terminal,
                                in CI, or with KENNET_NO_UPDATE_NOTIFIER=1
    the self-update             the release binary only (never Homebrew's or pip's): the platform's archive, checked
                                against the release's checksums.txt, put beside the executable and renamed over it;
                                the next run is the new version. KENNET_DISABLE_AUTOUPDATE=1 turns it off.

The release binary is PyInstaller's one-file build: the bootloader unpacks itself at start and runs from that copy,
so renaming a new file over the path (a new inode) never touches the process running; on macOS the archive keeps
the binary's ad-hoc signature, and a file a command line writes gets no quarantine attribute.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import secrets
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from metaphi import __version__, config, errors

REPO = "metaphi-labs/metaphi-ai-cli"   # where release.yml publishes the binaries and checksums.txt
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
EVERY_S = 24 * 3600   # one look at the releases a day
CHECK_TIMEOUT = httpx.Timeout(2.0)
WORKER = "__self-update"   # the argument the detached updater runs under


def _version(tag: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in tag.lstrip("v").split("."))
    except (AttributeError, ValueError):
        return None


def newer(latest: str | None, current: str = __version__) -> bool:
    got, have = _version(latest or ""), _version(current)
    return got is not None and have is not None and got > have


# --------------------------------------------------------------------------------------------------
# How this copy was installed, and the command that upgrades it
# --------------------------------------------------------------------------------------------------

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_method(executable: str | None = None, prefix: str | None = None, frozen: bool | None = None) -> str:
    """"brew", "binary" (the release's one-file build), "pipx", "uv" (`uv tool`) or "pip"."""
    frozen = _frozen() if frozen is None else frozen
    said = executable or sys.executable
    if any("/Cellar/" in w or w.startswith(("/opt/homebrew/", "/home/linuxbrew/.linuxbrew/"))
           for w in (said, os.path.realpath(said))):
        return "brew"
    if frozen:
        return "binary"
    prefix = (prefix or sys.prefix).replace(os.sep, "/")
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    return "pip"


UPGRADE = {"brew": "brew upgrade metaphi-labs/tap/kennet", "binary": "kennet update",
           "pipx": "pipx upgrade metaphi-ai", "uv": "uv tool upgrade metaphi-ai", "pip": "pip install -U metaphi-ai"}


def upgrade_command(method: str | None = None) -> str:
    return UPGRADE[method or install_method()]


def autoupdates(method: str | None = None) -> bool:
    """This copy replaces itself: the release binary, unless KENNET_DISABLE_AUTOUPDATE=1 or in CI."""
    return ((method or install_method()) == "binary" and os.environ.get("KENNET_DISABLE_AUTOUPDATE") != "1"
            and not os.environ.get("CI"))


# --------------------------------------------------------------------------------------------------
# The version floor: 426 from a server
# --------------------------------------------------------------------------------------------------

class UpgradeRequired(errors.MetaphiError):
    """A server no longer serves this version; `next_step` upgrades this install."""
    type = "upgrade_error"


def floor(r: httpx.Response) -> None:
    """An httpx response hook: a 426 is the server's version floor, raised with this install's upgrade command."""
    if r.status_code != 426:
        return
    r.read()
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    detail = detail if isinstance(detail, dict) else {}
    least = detail.get("min_version") if isinstance(detail.get("min_version"), str) else None
    said = detail.get("message") if isinstance(detail.get("message"), str) else None
    raise UpgradeRequired(said or f"kennet {__version__} is too old for this service"
                          + (f"; {least} or later is needed" if least else ""),
                          code="upgrade_required", next_step=upgrade_command())


# --------------------------------------------------------------------------------------------------
# The daily look at the releases, and the notice
# --------------------------------------------------------------------------------------------------

def _state_path() -> Path:
    return config.home() / "update.json"


def state() -> dict[str, Any]:
    try:
        got = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _save_state(s: dict[str, Any]) -> None:
    d = config.home()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = d / f".update.json.{secrets.token_hex(8)}.tmp"
    try:
        tmp.write_text(json.dumps(s) + "\n")
        os.replace(tmp, _state_path())
    except OSError:
        tmp.unlink(missing_ok=True)


def debug(line: str) -> None:
    """A failed check or self-update says so here and nowhere else."""
    try:
        with open(config.home() / "update.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def latest_release(client: httpx.Client | None = None) -> dict[str, Any] | None:
    """The newest published release by version: GitHub's own `releases/latest` is ordered by date, not version."""
    h = client or httpx.Client(timeout=CHECK_TIMEOUT, follow_redirects=True,
                               headers={"accept": "application/vnd.github+json",
                                        "user-agent": f"metaphi-cli/{__version__}"})
    try:
        r = h.get(RELEASES)
        r.raise_for_status()
        out = [x for x in r.json() if isinstance(x, dict) and not x.get("draft") and not x.get("prerelease")
               and _version(str(x.get("tag_name"))) is not None]
    finally:
        if client is None:
            h.close()
    return max(out, key=lambda x: _version(x["tag_name"]), default=None)


def check(now: float | None = None) -> dict[str, Any]:
    """Look at the releases if the last look is a day old; the state kept: {checked_at, latest}."""
    now = time.time() if now is None else now
    s = state()
    if now - float(s.get("checked_at") or 0) < EVERY_S:
        return s
    s["checked_at"] = now   # a failed look waits a day too: no command pays for an unreachable GitHub
    try:
        got = latest_release()
        if got is not None:
            s["latest"] = got["tag_name"].lstrip("v")
    except Exception as e:  # noqa: BLE001 - a look that fails is a debug line, never the command's error
        debug(f"release check failed: {e!r}")
    _save_state(s)
    return s


def quiet(json_out: bool) -> bool:
    """No notice: --json, stderr not a terminal, CI, or KENNET_NO_UPDATE_NOTIFIER=1."""
    return (json_out or os.environ.get("KENNET_NO_UPDATE_NOTIFIER") == "1" or bool(os.environ.get("CI"))
            or not (sys.stderr is not None and sys.stderr.isatty()))


def notice(s: dict[str, Any] | None = None, method: str | None = None) -> str | None:
    """The line a newer release earns, naming this install's upgrade command; None when this is the newest."""
    s = state() if s is None else s
    latest = s.get("latest")
    if not newer(latest):
        return None
    method = method or install_method()
    if autoupdates(method):
        return (f"kennet {latest} is out (you have {__version__}); it installs itself in the background and runs "
                f"from the next command. Now: kennet update")
    return f"kennet {latest} is out (you have {__version__}). Upgrade: {upgrade_command(method)}"


class Notifier:
    """Started before the command, said after it: the look runs on a thread and nothing waits for it. A look that
    has not finished by then is told next time, from the state it leaves."""

    def __init__(self, json_out: bool) -> None:
        self.method = install_method()
        self.say = not quiet(json_out)
        self.update = autoupdates(self.method)
        self.thread: threading.Thread | None = None
        stale = time.time() - float(state().get("checked_at") or 0) >= EVERY_S
        if stale and (self.say or self.update):
            self.thread = threading.Thread(target=self._look, daemon=True)
            self.thread.start()

    def _look(self) -> None:
        try:
            check()
        except Exception as e:  # noqa: BLE001
            debug(f"release check failed: {e!r}")

    def done(self) -> None:
        if self.thread is not None and self.thread.is_alive():
            return   # never slow the command: what it finds is told next time
        s = state()
        if self.say and (line := notice(s, self.method)):
            print(line, file=sys.stderr)
        if self.update and newer(s.get("latest")) and s.get("tried") != s.get("latest"):
            s["tried"] = s["latest"]   # one attempt per release; `kennet update` tries again by hand
            _save_state(s)
            spawn()


# --------------------------------------------------------------------------------------------------
# The release binary replaces itself
# --------------------------------------------------------------------------------------------------

def target() -> str:
    """The release's platform name: darwin-arm64, darwin-x86_64, linux-arm64, linux-x86_64."""
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{sys.platform}-{machine}"


def spawn() -> None:
    """The self-update, detached: it outlives this command and nobody waits on it."""
    try:
        subprocess.Popen([sys.executable, WORKER], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as e:
        debug(f"self-update did not start: {e!r}")


class UpdateError(config.Error):
    """The self-update refused or failed; nothing was replaced."""


def _get(h: httpx.Client, url: str) -> bytes:
    if not url.startswith(DOWNLOADS):
        raise UpdateError(f"not a release download of {REPO}: {url}")
    r = h.get(url)
    r.raise_for_status()
    return r.content


def install(version: str, executable: str | None = None, client: httpx.Client | None = None) -> Path:
    """Download `version` for this platform, check it against checksums.txt, and rename it over the executable."""
    exe = Path(os.path.realpath(executable or sys.executable))
    name = f"kennet_{version}_{target()}.tar.gz"
    h = client or httpx.Client(timeout=httpx.Timeout(60.0, connect=5.0), follow_redirects=True,
                               headers={"user-agent": f"metaphi-cli/{__version__}"})
    try:
        sums = _get(h, f"{DOWNLOADS}v{version}/checksums.txt").decode()
        archive = _get(h, f"{DOWNLOADS}v{version}/{name}")
    finally:
        if client is None:
            h.close()
    want = {f: s for s, _, f in (line.strip().partition("  ") for line in sums.splitlines()) if f}.get(name)
    if want is None:
        raise UpdateError(f"checksums.txt of v{version} names no {name}")
    if hashlib.sha256(archive).hexdigest() != want.lower():
        raise UpdateError(f"{name} does not match checksums.txt of v{version}; refused")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
        member = t.getmember("kennet")
        if not member.isfile():
            raise UpdateError(f"{name} holds no kennet binary")
        binary = t.extractfile(member).read()
    tmp = exe.parent / f".kennet.{secrets.token_hex(8)}.new"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o755)
        with os.fdopen(fd, "wb") as f:
            f.write(binary)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, exe)   # atomic: the next run is the new version, this one runs on
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return exe


def now(method: str | None = None) -> dict[str, Any]:
    """`kennet update`: look at the releases now; the release binary installs the newest, any other install is told
    its own command."""
    method = method or install_method()
    s = state()
    s.pop("checked_at", None)
    _save_state(s)
    s = check()
    latest = s.get("latest")
    out = {"version": __version__, "latest": latest, "method": method, "updated": False}
    if not newer(latest):
        return out
    if method != "binary":
        return {**out, "next_step": upgrade_command(method)}
    install(latest)
    return {**out, "updated": True}


def worker() -> int:
    """The detached self-update: silent, its failures a debug line."""
    s = state()
    try:
        if newer(s.get("latest")) and install_method() == "binary":
            install(s["latest"])
            debug(f"updated to {s['latest']}")
    except Exception as e:  # noqa: BLE001
        debug(f"self-update to {s.get('latest')} failed: {e!r}")
    return 0
