"""Who made a release: its checksums.txt is signed with Metaphi AI's release key, as an OpenSSH signature (SSHSIG,
`ssh-keygen -Y sign -n kennet-release`), and checked here against the key this program carries before anything it
downloads is run or installed. install.sh checks the same file with `ssh-keygen -Y verify`.

Ed25519 verification is RFC 8032's reference algorithm (section 6), in Python: a public key and a signature are all
it handles, so it holds no secret to leak through timing, and the one-file binary needs no other library."""
from __future__ import annotations

import base64
import hashlib

NAMESPACE = "kennet-release"

# The release key: `ssh-keygen -t ed25519`, its private half only in the `pypi` environment's RELEASE_SIGNING_KEY.
# install.sh carries the same line. Empty: nothing verifies, so nothing is installed or swapped (fail closed).
PUBLIC_KEY = ""


class BadSignature(Exception):
    """The file is not signed by the release key."""


# --------------------------------------------------------------------------------------------------
# Ed25519 (RFC 8032 section 6)
# --------------------------------------------------------------------------------------------------

_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P)
    if x2 % _P == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _I % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
_G = (_GX, _GY, 1, _GX * _GY % _P)


def _add(a: tuple, b: tuple) -> tuple:
    p, q = a, b
    x1 = (p[1] - p[0]) * (q[1] - q[0]) % _P
    x2 = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c, d = 2 * p[3] * q[3] * _D % _P, 2 * p[2] * q[2] % _P
    e, f, g, h = x2 - x1, d - c, d + c, x2 + x1
    return e * f, g * h, f * g, e * h


def _mul(s: int, p: tuple) -> tuple:
    q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def _equal(p: tuple, q: tuple) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (p[1] * q[2] - q[1] * p[2]) % _P == 0


def _point(s: bytes) -> tuple | None:
    if len(s) != 32:
        return None
    y = int.from_bytes(s, "little")
    sign, y = y >> 255, y & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _P)


def ed25519_verify(public: bytes, message: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    a, r = _point(public), _point(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if a is None or r is None or s >= _L:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + public + message).digest(), "little") % _L
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))


# --------------------------------------------------------------------------------------------------
# OpenSSH signatures (PROTOCOL.sshsig)
# --------------------------------------------------------------------------------------------------

def _strings(blob: bytes) -> list[bytes]:
    out, i = [], 0
    while i < len(blob):
        if i + 4 > len(blob):
            raise BadSignature("truncated")
        n = int.from_bytes(blob[i:i + 4], "big")
        if i + 4 + n > len(blob):
            raise BadSignature("truncated")
        out.append(blob[i + 4:i + 4 + n])
        i += 4 + n
    return out


def _string(b: bytes) -> bytes:
    return len(b).to_bytes(4, "big") + b


def _ed25519_key(line: str) -> bytes:
    """The 32 bytes of an `ssh-ed25519 AAAA...` public key line."""
    parts = line.split()
    if len(parts) < 2 or parts[0] != "ssh-ed25519":
        raise BadSignature("no release key")
    kind, key = _strings(base64.b64decode(parts[1]))
    if kind != b"ssh-ed25519" or len(key) != 32:
        raise BadSignature("not an ed25519 key")
    return key


def verify(message: bytes, armored: str, public_key: str | None = None, namespace: str = NAMESPACE) -> None:
    """Raise BadSignature unless `armored` (an SSH SIGNATURE block) signs `message` with the release key."""
    try:
        key = _ed25519_key(PUBLIC_KEY if public_key is None else public_key)
        body = "".join(line.strip() for line in armored.strip().splitlines()
                       if line.strip() and not line.startswith("-----"))
        blob = base64.b64decode(body, validate=True)
        if blob[:6] != b"SSHSIG" or int.from_bytes(blob[6:10], "big") != 1:
            raise BadSignature("not an SSH signature")
        signer, ns, reserved, hash_alg, sig = _strings(blob[10:])
        if _strings(signer) != [b"ssh-ed25519", key]:
            raise BadSignature("signed by another key")
        if ns != namespace.encode():
            raise BadSignature(f"signed for {ns!r}, not {namespace}")
        kind, raw = _strings(sig)
        if kind != b"ssh-ed25519" or hash_alg not in (b"sha512", b"sha256"):
            raise BadSignature("not an ed25519 signature")
        digest = hashlib.new(hash_alg.decode(), message).digest()
        signed = b"SSHSIG" + _string(ns) + _string(reserved) + _string(hash_alg) + _string(digest)
    except (ValueError, TypeError) as e:
        raise BadSignature(f"unreadable: {e}") from None
    if not ed25519_verify(key, signed, raw):
        raise BadSignature("the signature does not match")
