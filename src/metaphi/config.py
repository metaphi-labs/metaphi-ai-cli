"""Where the CLI keeps what it signed in with, and the hosts it talks to.

    METAPHI_API_KEY            an organization's key (mai_sk_...): overrides the saved one, for servers and CI
    METAPHI_HOME               the directory the config file lives in (default ~/.config/metaphi)
    METAPHI_ACCOUNTS           the accounts host (default https://app.metaphi.ai)
    METAPHI_API                the twin API (default https://api.metaphi.ai/v1/kennet)
    METAPHI_ALLOW_CUSTOM_HOST  1 lets the two above, and the file links they hand out, leave metaphi.ai
    METAPHI_TWIN_TIMEOUT       seconds `metaphi twin` waits for a twin to end (default 7200)
    METAPHI_NO_KEYRING         1 keeps secrets in the config file even when an OS keychain is available

Secrets (the saved keys and the sign-in token) go to the OS keychain through `keyring` when one is available;
otherwise into the config file, in a directory only its owner can enter, written atomically with mode 600.
"""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

DEFAULT_ACCOUNTS = "https://app.metaphi.ai"
DEFAULT_API = "https://api.metaphi.ai/v1/kennet"
ALLOWED_DOMAIN = "metaphi.ai"
KEY_PREFIX = "mai_sk_"
KEYRING_SERVICE = "metaphi-ai"
SECRETS = ("api_key", "test_api_key", "token", "device_code")
_LOOPBACK = ("localhost", "127.0.0.1", "::1")


class Error(Exception):
    """Something the person can fix, said in one sentence."""


def _custom_hosts() -> bool:
    return os.environ.get("METAPHI_ALLOW_CUSTOM_HOST") == "1"


def check_url(url: str, what: str) -> str:
    """`url`, if it is https on metaphi.ai; METAPHI_ALLOW_CUSTOM_HOST=1 also lets any https host, or http on
    loopback, through."""
    if not isinstance(url, str):
        raise Error(f"{what} is not a URL: {url!r}")
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        raise Error(f"{what} is not a URL: {url!r}") from None
    if not host or parts.username or parts.password:
        raise Error(f"{what} is not a usable URL: {url!r}")
    if _custom_hosts():
        if parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK):
            return url
        raise Error(f"{what} must be https (or http on localhost): {url!r}")
    if parts.scheme != "https":
        raise Error(f"{what} must be https: {url!r}")
    if host != ALLOWED_DOMAIN and not host.endswith("." + ALLOWED_DOMAIN):
        raise Error(f"{what} is not on {ALLOWED_DOMAIN}: {url!r} (set METAPHI_ALLOW_CUSTOM_HOST=1 to allow it)")
    return url


_warned: set[str] = set()


def _endpoint(var: str, default: str) -> str:
    url = os.environ.get(var) or default
    check_url(url, var)
    if url != default and var not in _warned:
        _warned.add(var)
        print(f"metaphi: warning: {var} points at {url}, not {default}", file=sys.stderr)
    return url


def accounts_url() -> str:
    return _endpoint("METAPHI_ACCOUNTS", DEFAULT_ACCOUNTS).rstrip("/")


def api_url() -> str:
    return _endpoint("METAPHI_API", DEFAULT_API).rstrip("/")


def home() -> Path:
    return Path(os.environ.get("METAPHI_HOME") or Path.home() / ".config" / "metaphi")


def path() -> Path:
    return home() / "config.json"


def _keyring() -> Any:
    """The keyring module when an OS keychain backs it, else None."""
    if os.environ.get("METAPHI_NO_KEYRING") == "1":
        return None
    try:
        import keyring
        from keyring.backends import fail
    except Exception:  # noqa: BLE001 - absent or broken: fall back to the file
        return None
    try:
        backend = keyring.get_keyring()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(backend, fail.Keyring) or getattr(backend, "priority", 1) <= 0:
        return None
    return keyring


def keyring_backend() -> str:
    """What holds secrets on this machine, for `metaphi --debug-keyring`."""
    kr = _keyring()
    return f"keyring: {kr.get_keyring()!r}" if kr is not None else f"file: {path()}"


def _read_file() -> dict[str, Any]:
    p = path()
    try:
        text = p.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise Error(f"cannot read {p}: {e.strerror or e}") from None
    try:
        cfg = json.loads(text)
    except ValueError:
        raise Error(f"{p} is not valid JSON; fix it or delete it and sign in again") from None
    if not isinstance(cfg, dict):
        raise Error(f"{p} does not hold a JSON object; fix it or delete it and sign in again")
    return cfg


def load() -> dict[str, Any]:
    cfg = _read_file()
    kr = _keyring()
    if kr is not None:
        for name in SECRETS:
            if name in cfg:
                continue
            try:
                value = kr.get_password(KEYRING_SERVICE, name)
            except Exception:  # noqa: BLE001 - a locked or broken keychain reads as empty
                value = None
            if value:
                cfg[name] = value
    return cfg


def _write_file(cfg: dict[str, Any]) -> None:
    d = home()
    if not d.is_dir():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(d, 0o700)
    elif os.name == "posix" and d.stat().st_mode & 0o077:
        print(f"metaphi: warning: {d} is readable by others; run `chmod 700 {d}`", file=sys.stderr)
    tmp = d / f".config.json.{secrets.token_hex(8)}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(cfg, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def save(cfg: dict[str, Any]) -> None:
    """Keep `cfg`: its secrets in the OS keychain when there is one, the rest (or everything) in the file."""
    plain = dict(cfg)
    kr = _keyring()
    if kr is not None:
        try:
            for name in SECRETS:
                value = plain.pop(name, None)
                if value:
                    kr.set_password(KEYRING_SERVICE, name, value)
                else:
                    with contextlib.suppress(Exception):  # nothing stored under that name
                        kr.delete_password(KEYRING_SERVICE, name)
        except Exception as e:  # noqa: BLE001 - the keychain refused: keep everything in the file instead
            print(f"metaphi: warning: the OS keychain refused the secret ({e}); keeping it in {path()}",
                  file=sys.stderr)
            plain = dict(cfg)
    _write_file(plain)


def api_key(flag: str | None = None, test: bool = False) -> str | None:
    """The key a call runs on: the argument, then METAPHI_API_KEY, then the saved one (`test`: the saved test key)."""
    return flag or os.environ.get("METAPHI_API_KEY") or load().get("test_api_key" if test else "api_key")
