"""Staying current, as `gh` and Claude Code do: a server's version floor, a notice when a newer release is out, and a
release binary that updates itself.

    426 upgrade_required   a server refuses this version: the error names the upgrade command for this install
    the notice             at most once a day, after the command, on stderr; never under --json, off a terminal, in CI,
                           or with KENNET_NO_UPDATE_NOTIFIER=1
    the self-update        the binary install.sh installs, never Homebrew's or pip's; off in CI and with
                           KENNET_DISABLE_AUTOUPDATE=1

The layout is Claude Code's. Each version is its own file, ~/.local/share/kennet/versions/<version>/kennet, and the
command on the PATH (~/.local/bin/kennet) is a symlink to one of them. The release binary is PyInstaller's one-file
build, whose bootloader re-opens its own file by its resolved path for every import it loads lazily: a version's
file is never replaced or removed while a process runs it (each holds a shared lock on its version), so an update
is a new directory and an atomic flip of the symlink, and a command already running finishes on the version it
started with.

An update is installed only when the release's checksums.txt carries Metaphi AI's release signature
(metaphi/signature.py), the archive matches it, and the new binary answers `--version` with the version expected.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from metaphi import __version__, config, errors, signature

REPO = "metaphi-labs/metaphi-ai-cli"   # where release.yml publishes the binaries, checksums.txt and its signature
RELEASES = f"https://api.github.com/repos/{REPO}/releases?per_page=10"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
INSTALL_SH = f"curl -fsSL https://github.com/{REPO}/releases/latest/download/install.sh | sh"
HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
EVERY_S = 24 * 3600   # one look at the releases a day, and one notice
STALE_S = 3600        # a temporary file this old was left by a run that died
KEEP = 2              # versions kept: the current one and the one before it, beside any still running
WORKER = "__self-update"   # the argument the detached updater runs under
PRUNE = "__prune"          # the argument install.sh runs the new version with


def _version(tag: object) -> tuple[int, ...] | None:
    """`1.2.3` or `v1.2.3` as numbers; anything else is no version."""
    if not isinstance(tag, str) or not re.fullmatch(r"v?[0-9]{1,6}(\.[0-9]{1,6}){0,3}", tag):
        return None
    return tuple(int(x) for x in tag.lstrip("v").split("."))


def newer(latest: object, current: str = __version__) -> bool:
    got, have = _version(latest), _version(current)
    return got is not None and have is not None and got > have


# --------------------------------------------------------------------------------------------------
# Where things live
# --------------------------------------------------------------------------------------------------

def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "kennet"


def versions_dir() -> Path:
    return data_dir() / "versions"


def running() -> Path:
    return Path(os.path.realpath(sys.executable))


def _in_versions(path: Path) -> bool:
    return path.parent.parent == Path(os.path.realpath(versions_dir()))


def link_path() -> Path | None:
    """The symlink on the PATH that install.sh made (it names it in ~/.local/share/kennet/link), while it still
    points into the versions."""
    try:
        link = Path(Path(data_dir() / "link").read_text().strip())
    except OSError:
        return None
    if not link.is_absolute() or not link.is_symlink() or not _in_versions(Path(os.path.realpath(link))):
        return None
    return link


# --------------------------------------------------------------------------------------------------
# How this copy was installed, and the command that upgrades it
# --------------------------------------------------------------------------------------------------

def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_method(executable: str | None = None, prefix: str | None = None, frozen: bool | None = None) -> str:
    """"brew"; "binary", install.sh's layout, which updates itself; "download", a release binary put somewhere by
    hand; "pipx", "uv" (`uv tool`) or "pip"."""
    frozen = _frozen() if frozen is None else frozen
    said = executable or sys.executable
    if any("/Cellar/" in w or w.startswith(("/opt/homebrew/", "/home/linuxbrew/.linuxbrew/"))
           for w in (said, os.path.realpath(said))):
        return "brew"
    if frozen:
        return "binary" if _in_versions(Path(os.path.realpath(said))) else "download"
    prefix = (prefix or sys.prefix).replace(os.sep, "/")
    if "/pipx/venvs/" in prefix:
        return "pipx"
    if "/uv/tools/" in prefix:
        return "uv"
    return "pip"


UPGRADE = {"brew": "brew upgrade metaphi-labs/tap/kennet", "binary": "kennet update", "download": INSTALL_SH,
           "pipx": "pipx upgrade metaphi-ai", "uv": "uv tool upgrade metaphi-ai", "pip": "pip install -U metaphi-ai"}


def upgrade_command(method: str | None = None) -> str:
    return UPGRADE[method or install_method()]


def autoupdates(method: str | None = None) -> bool:
    """This copy updates itself: install.sh's layout, unless KENNET_DISABLE_AUTOUPDATE=1 or in CI."""
    return ((method or install_method()) == "binary" and os.environ.get("KENNET_DISABLE_AUTOUPDATE") != "1"
            and not os.environ.get("CI"))


# --------------------------------------------------------------------------------------------------
# The version floor: 426 from a server
# --------------------------------------------------------------------------------------------------

class UpgradeRequired(errors.MetaphiError):
    """A server no longer serves this version; `next_step` upgrades this install."""
    type = "upgrade_error"


def floor(r: httpx.Response) -> None:
    """An httpx response hook: a 426 is the server's version floor. The sentence is the CLI's own, from
    `min_version` and this install's upgrade command; the server's is not shown."""
    if r.status_code != 426:
        return
    r.read()
    try:
        detail = r.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    least = detail.get("min_version") if isinstance(detail, dict) else None
    least = least if _version(least) else None
    raise UpgradeRequired(f"kennet {__version__} is too old for this service"
                          + (f"; it needs {least} or later" if least else ""),
                          code="upgrade_required", next_step=upgrade_command())


# --------------------------------------------------------------------------------------------------
# State, the debug log, and what a dead run left behind
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
    tmp = d / f".update.json.{secrets.token_hex(8)}.tmp"
    try:
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp.write_text(json.dumps(s) + "\n")
        os.replace(tmp, _state_path())
    except OSError:
        tmp.unlink(missing_ok=True)


def debug(line: str) -> None:
    """A failed look or self-update says so here and nowhere else."""
    try:
        with open(config.home() / "update.log", "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
    except OSError:
        pass


def clean_stale(now: float | None = None) -> None:
    """Remove what a run that died left: half-written state, versions, symlinks."""
    now = time.time() if now is None else now
    link = link_path()
    for d, prefixes in ((config.home(), (".update.json.",)), (versions_dir(), (".tmp-", ".stale-")),
                        (link.parent if link else None, (".kennet.link.",))):
        if d is None or not d.is_dir():
            continue
        for p in d.iterdir():
            try:
                if p.name.startswith(prefixes) and now - p.lstat().st_mtime > STALE_S:
                    shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
            except OSError:
                pass


# --------------------------------------------------------------------------------------------------
# The daily look at the releases, and the notice
# --------------------------------------------------------------------------------------------------

class UpdateError(errors.MetaphiError):
    """The look or the self-update failed; nothing was installed."""
    type = "update_error"


def _hosts(request: httpx.Request) -> None:
    """An httpx request hook: every request, redirects too, goes to GitHub over https."""
    if request.url.scheme != "https" or request.url.host not in HOSTS:
        raise UpdateError(f"a release download went to {request.url.host}, not GitHub; refused",
                          code="update_refused")


def _client(timeout: float) -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(timeout, connect=5.0), follow_redirects=True,
                        event_hooks={"request": [_hosts]},
                        headers={"accept": "application/vnd.github+json", "user-agent": f"metaphi-cli/{__version__}"})


def _unreachable(e: Exception) -> UpdateError:
    return UpdateError(f"GitHub could not be reached for the release ({type(e).__name__}); try again later",
                       code="update_unreachable", retryable=True, next_step="kennet update")


def latest_release(client: httpx.Client | None = None) -> str | None:
    """The newest published version: by version, since GitHub's own `releases/latest` is by date."""
    h = client or _client(2.0)
    try:
        r = h.get(RELEASES)
        r.raise_for_status()
        tags = [x.get("tag_name") for x in r.json() if isinstance(x, dict)
                and not x.get("draft") and not x.get("prerelease")]
    except UpdateError:
        raise
    except (httpx.HTTPError, ValueError, AttributeError, TypeError) as e:
        raise _unreachable(e) from None
    finally:
        if client is None:
            h.close()
    found = [t for t in tags if _version(t)]
    return max(found, key=_version).lstrip("v") if found else None


def check(now: float | None = None, force: bool = False) -> dict[str, Any]:
    """Look at the releases if the last look is a day old: {checked_at, latest}. The time is kept before the look,
    so a look that fails, or a command that ends first, waits a day too (GitHub allows 60 looks an hour)."""
    now = time.time() if now is None else now
    s = state()
    if not force and now - float(s.get("checked_at") or 0) < EVERY_S:
        return s
    s["checked_at"] = now
    _save_state(s)
    try:
        latest = latest_release()
    except UpdateError as e:
        if force:
            raise
        debug(f"release check failed: {e.message}")
        return s
    s = {**state(), "checked_at": now}
    if latest:
        s["latest"] = latest
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
        return f"kennet {latest} is out (you have {__version__}); it installs itself for your next command."
    return f"kennet {latest} is out (you have {__version__}). Upgrade: {upgrade_command(method)}"


class Notifier:
    """Started before the command, said after it: the look runs on a thread nobody waits for, and what it finds
    is told next time. The notice is shown once a day at most."""

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
        except Exception as e:  # noqa: BLE001 - a look that fails is a debug line, never the command's error
            debug(f"release check failed: {e!r}")

    def done(self, now: float | None = None) -> None:
        if self.thread is not None and self.thread.is_alive():
            return
        now = time.time() if now is None else now
        s = state()
        if self.say and now - float(s.get("noticed_at") or 0) >= EVERY_S and (line := notice(s, self.method)):
            print(line, file=sys.stderr)
            s["noticed_at"] = now
            _save_state(s)
        if self.update and newer(s.get("latest")) and s.get("tried") != s.get("latest"):
            s["tried"] = s["latest"]   # one attempt a release; `kennet update` tries again by hand
            _save_state(s)
            spawn()


# --------------------------------------------------------------------------------------------------
# The release binary updates itself
# --------------------------------------------------------------------------------------------------

def target() -> str:
    """The release's platform: darwin-arm64, darwin-x86_64, linux-arm64, linux-x86_64. An x86_64 copy under Rosetta
    on Apple silicon fetches the arm64 build."""
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    if sys.platform == "darwin" and machine == "x86_64":
        try:
            arm = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True,
                                 timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            arm = ""
        machine = "arm64" if arm == "1" else machine
    return f"{sys.platform}-{machine}"


_held: Any = None   # this process's shared lock on the version it runs


def hold() -> None:
    """A running version says so: a shared lock on its version's `.lock`, held until the process ends."""
    global _held
    if _held is not None or not _frozen() or not _in_versions(running()):
        return
    try:
        import fcntl
        f = open(running().parent / ".lock", "a")   # noqa: SIM115 - held for the life of the process
        fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)
        _held = f
    except (OSError, ImportError):
        pass


def _in_use(version_dir: Path) -> bool:
    """Some process runs this version (it holds the lock `hold` takes)."""
    try:
        import fcntl
        with open(version_dir / ".lock", "a") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                return True
            fcntl.flock(f, fcntl.LOCK_UN)
            return False
    except (OSError, ImportError):
        return True


def prune(current: Path) -> list[str]:
    """Remove all but the newest KEEP versions; never the current one, the one running, or one in use."""
    d = versions_dir()
    found = sorted((p for p in d.iterdir() if p.is_dir() and _version(p.name)), key=lambda p: _version(p.name),
                   reverse=True)
    keep = {p.name for p in found[:KEEP]} | {current.parent.name, running().parent.name}
    gone = []
    for p in found:
        if p.name not in keep and not _in_use(p):
            shutil.rmtree(p, ignore_errors=True)
            gone.append(p.name)
    return gone


def spawn() -> None:
    """The self-update, detached: it outlives this command and nobody waits on it."""
    try:
        subprocess.Popen([sys.executable, WORKER], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
    except OSError as e:
        debug(f"self-update did not start: {e!r}")


def _get(h: httpx.Client, url: str) -> bytes:
    try:
        r = h.get(url)
    except UpdateError:
        raise
    except httpx.HTTPError as e:
        raise _unreachable(e) from None
    if r.status_code == 404:
        raise UpdateError(f"the release has no {url.rsplit('/', 1)[-1]}; nothing was installed",
                          code="update_missing", next_step=INSTALL_SH)
    if r.status_code >= 400:
        raise _unreachable(httpx.HTTPStatusError(str(r.status_code), request=r.request, response=r))
    return r.content


def _no_write(where: Path) -> UpdateError:
    return UpdateError(f"kennet cannot write to {where}: it was installed by root. Reinstall it with install.sh, "
                       "or use brew", code="update_not_permitted", next_step=INSTALL_SH)


def _answers(exe: Path, version: str) -> bool:
    try:
        said = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return said.returncode == 0 and said.stdout.strip() == f"kennet {version}"


def install(version: str, link: Path, client: httpx.Client | None = None) -> Path:
    """Fetch `version` for this platform into its own directory, check its signature, checksum and `--version`,
    then flip `link` to it. Returns the new version's file."""
    if not _version(version):
        raise UpdateError(f"{version!r} is not a version", code="update_missing")
    clean_stale()
    name = f"kennet_{version}_{target()}.tar.gz"
    h = client or _client(60.0)
    try:
        sums = _get(h, f"{DOWNLOADS}v{version}/checksums.txt")
        sig = _get(h, f"{DOWNLOADS}v{version}/checksums.txt.sig")
        archive = _get(h, f"{DOWNLOADS}v{version}/{name}")
    finally:
        if client is None:
            h.close()
    try:
        signature.verify(sums, sig.decode("utf-8", "replace"))
    except signature.BadSignature as e:
        raise UpdateError(f"checksums.txt of v{version} is not signed by Metaphi AI's release key ({e}); refused",
                          code="update_unsigned") from None
    want = {f: s for s, _, f in (line.strip().partition("  ") for line in sums.decode().splitlines()) if f}.get(name)
    if want is None:
        raise UpdateError(f"v{version} has no build for {target()}", code="update_missing")
    if hashlib.sha256(archive).hexdigest() != want.lower():
        raise UpdateError(f"{name} does not match checksums.txt; refused", code="update_mismatch")
    d = versions_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / f".tmp-{secrets.token_hex(8)}"
        tmp.mkdir()
    except PermissionError:
        raise _no_write(d) from None
    try:
        try:
            with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as t:
                member = t.getmember("kennet")
                if not member.isfile():
                    raise KeyError("kennet")
                (tmp / "kennet").write_bytes(t.extractfile(member).read())
        except (tarfile.TarError, KeyError, OSError):
            raise UpdateError(f"{name} holds no kennet binary", code="update_missing") from None
        (tmp / "kennet").chmod(0o755)
        if not _answers(tmp / "kennet", version):
            raise UpdateError(f"the kennet {version} binary did not start here; nothing was changed",
                              code="update_broken", next_step=INSTALL_SH)
        final = d / version
        if final.exists() and not _answers(final / "kennet", version):
            final.rename(d / f".stale-{secrets.token_hex(8)}")
        if not final.exists():
            tmp.rename(final)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    flip = link.parent / f".kennet.link.{secrets.token_hex(8)}"
    try:
        flip.symlink_to(final / "kennet")
        os.replace(flip, link)   # atomic: a new command runs the new version, one running keeps its own file
    except PermissionError:
        flip.unlink(missing_ok=True)
        raise _no_write(link.parent) from None
    except OSError:
        flip.unlink(missing_ok=True)
        raise
    prune(final / "kennet")
    return final / "kennet"


def now(method: str | None = None) -> dict[str, Any]:
    """`kennet update`: look at the releases now. install.sh's binary installs the newest; any other install is
    told its own command. A look that fails is a retryable error, never "the newest"."""
    method = method or install_method()
    latest = check(force=True).get("latest")
    out = {"version": __version__, "latest": latest, "method": method, "updated": False}
    if not newer(latest):
        return out
    if method != "binary":
        return {**out, "next_step": upgrade_command(method)}
    link = link_path()
    if link is None:
        raise UpdateError("the kennet on your PATH is not the one install.sh manages; reinstall it",
                          code="update_missing", next_step=INSTALL_SH)
    install(latest, link)
    return {**out, "updated": True}


def worker() -> int:
    """The detached self-update: silent, its failures a debug line."""
    s = state()
    try:
        clean_stale()
        link = link_path()
        if newer(s.get("latest")) and install_method() == "binary" and link is not None:
            install(s["latest"], link)
            debug(f"updated to {s['latest']}")
    except Exception as e:  # noqa: BLE001
        debug(f"self-update to {s.get('latest')} failed: {getattr(e, 'message', None) or repr(e)}")
    return 0
