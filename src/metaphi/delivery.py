"""Task deliveries: list what the organization holds, read one, pull it, and check it against its lock.

A delivery is `<name>@<version>` and never changes; a fix is a new version. Pulled, it is a directory as it was cut:

    <dir>/delivery.lock.json     the record: each task's tree hash, and its images by digest
    <dir>/SHA256SUMS             `<sha256>  <path>` for every file under tasks/
    <dir>/tasks/<task>/...       each task's bundle

A task's tree hash is the SHA-256 of its own SHA256SUMS lines (`<sha256>  <path>`, in path order), so `sha256sum`
alone can check one. `pull` fetches a task only when the directory does not already hold the tree the lock names,
checks every task it unpacks before it puts it in place, and deletes nothing it did not write. `verify` reads the
directory and nothing else: no key, no network."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tarfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from metaphi import errors
from metaphi.twin import UPLOAD, _client, _error, _key, _retry_after, _send, _Tries

LOCK, SUMS, TASKS = "delivery.lock.json", "SHA256SUMS", "tasks"
AT_ONCE = 8             # tasks fetched at a time
KINDS = (1, 2)          # the lock formats this reads: what a task's tree hash is taken over
GRADING = "tests/"      # a task's files that grade: a change there voids earlier scores on the task


def _need(message: str, code: str, next_step: str | None = None, retryable: bool = False) -> errors.MetaphiError:
    return errors.InvalidRequestError(message, code=code, next_step=next_step, retryable=retryable)


def _named(e: errors.MetaphiError) -> errors.MetaphiError:
    """The service's refusal with the command that clears it."""
    if e.code == "resource_missing":
        e.next_step = "kennet delivery list"
    elif e.code == "insufficient_scope":
        e.message += "; `kennet login` hands this machine keys that do"
        e.next_step = "kennet login"
    return e


def _get(path: str, sleep: Callable[[float], None] = time.sleep) -> httpx.Response:
    with _client(_key(None, False)) as h:
        try:
            return _send(h, "GET", path, sleep)
        except errors.MetaphiError as e:
            raise _named(e) from None


def held(sleep: Callable[[float], None] = time.sleep) -> list[dict[str, Any]]:
    """The deliveries the organization holds, newest sent first."""
    return _get("/deliveries", sleep).json()["data"]


def show(delivery: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One delivery with its tasks."""
    return _get(f"/deliveries/{delivery}", sleep).json()


# -- the tree, as the cut counts it ------------------------------------------------------------------------------


def files(tree: Path) -> list[tuple[str, str, str]]:
    """(path, sha256, mode) for every file under `tree`, in path order. A link's content is its target; a mode is
    git's: 100755 where the owner may execute, 100644, or 120000 for a link."""
    out = []
    for home, dirs, names in os.walk(tree):
        for name in names + [d for d in dirs if (Path(home) / d).is_symlink()]:
            path = Path(home) / name
            if path.is_symlink():
                body, mode = os.readlink(path).encode(), "120000"
            else:
                body = path.read_bytes()
                mode = "100755" if path.stat().st_mode & 0o100 else "100644"
            out.append((path.relative_to(tree).as_posix(), hashlib.sha256(body).hexdigest(), mode))
    return sorted(out)


def sums(tree: Path) -> list[str]:
    """`<sha256>  <path>` for every file under `tree`, in path order."""
    return [f"{digest}  {rel}" for rel, digest, _ in files(tree)]


def tree_hash(tree: Path, kind: int = 2) -> str:
    """A task's tree as a lock of that kind names it (the lock's `lock` field, 1 where it has none): the first
    kind over its SHA256SUMS lines, the second over `<sha256>  <mode>  <path>`, so a mode is held too."""
    lines = [f"{d}  {m}  {p}" for p, d, m in files(tree)] if kind >= 2 else sums(tree)
    return "sha256:" + hashlib.sha256("".join(line + "\n" for line in lines).encode()).hexdigest()


def _kind(lock: dict[str, Any]) -> int:
    kind = lock.get("lock", 1)
    if kind not in KINDS:
        raise _need(f"{lock.get('delivery')} has a lock of kind {kind!r}, newer than this kennet reads; upgrade "
                    "kennet (pip install -U metaphi-ai, or brew upgrade kennet)", "lock_too_new")
    return kind


def _holds(bundle: Path, tree: str, kind: int) -> bool:
    try:
        return bundle.is_dir() and not bundle.is_symlink() and tree_hash(bundle, kind) == tree
    except OSError:
        return False


def _unpack(tar_path: Path, into: Path) -> None:
    """A task's tar written under `into`: files, directories and links that stay inside it, and nothing else."""
    with tarfile.open(tar_path) as tar:
        members = tar.getmembers()
        for m in members:
            name = PurePosixPath(m.name)
            if name.is_absolute() or ".." in name.parts or not (m.isreg() or m.isdir() or m.issym()):
                raise ValueError(f"{m.name!r} is not a file of a task")
            if m.issym():
                to = PurePosixPath(os.path.normpath(str(name.parent / m.linkname)))
                if PurePosixPath(m.linkname).is_absolute() or ".." in to.parts:
                    raise ValueError(f"{m.name!r} links outside the task")
        into.mkdir(parents=True)
        for m in members:
            at = into / m.name
            if m.isdir():
                at.mkdir(parents=True, exist_ok=True)
            elif m.issym():
                at.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(m.linkname, at)
            else:
                at.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(m) as src, open(at, "wb") as dst:   # type: ignore[union-attr]
                    shutil.copyfileobj(src, dst)
                at.chmod(stat.S_IMODE(m.mode) & 0o755 | 0o600)
        for m in members:   # a directory's mode last: one that is read-only still took its files
            if m.isdir():
                (into / m.name).chmod(stat.S_IMODE(m.mode) & 0o755 | 0o700)


def _fetch(h: httpx.Client, delivery: str, task: dict[str, Any], home: Path, kind: int,
           sleep: Callable[[float], None]) -> int:
    """One task fetched, checked against its tree hash and put in place. Returns the bytes downloaded."""
    name, mark = task["name"], uuid.uuid4().hex[:8]
    tar_path, fresh = home / f".{name}.{mark}.tar", home / f".{name}.{mark}.part"
    tries = _Tries(sleep)
    try:
        while True:
            try:
                with h.stream("GET", f"/deliveries/{delivery}/tasks/{name}/content", timeout=UPLOAD) as r:
                    if r.status_code >= 400:
                        r.read()
                        error = _error(r)
                        error.retry_after_s = _retry_after(r)
                        if not error.retryable or not tries.again(error.retry_after_s):
                            raise _named(error)
                        continue
                    with open(tar_path, "wb") as f:
                        f.writelines(r.iter_bytes())
                break
            except httpx.TransportError as e:
                if not tries.again():
                    raise errors.APIError(f"the service did not answer ({type(e).__name__})", code="unreachable",
                                          retryable=True, next_step=f"kennet delivery pull {delivery}") from None
        size = tar_path.stat().st_size
        try:
            _unpack(tar_path, fresh)
            good = tree_hash(fresh, kind) == task["tree"]
        except (OSError, ValueError, tarfile.TarError):
            good = False
        if not good:
            raise errors.APIError(f"{name}: what was downloaded is not the tree the lock names; nothing was put "
                                  "in place", code="tree_mismatch", retryable=True)
        bundle, old = home / name, home / f".{name}.{mark}.old"
        if bundle.exists() or bundle.is_symlink():
            bundle.rename(old)
        try:
            fresh.rename(bundle)
        except OSError:
            if old.exists():   # what was there stays there
                old.rename(bundle)
            raise
        return size
    finally:
        tar_path.unlink(missing_ok=True)
        for left in (fresh, home / f".{name}.{mark}.old"):
            if left.exists():
                shutil.rmtree(left, ignore_errors=True)


def default_dir(delivery: str) -> Path:
    return Path(delivery.replace("@", "-"))


def pull(delivery: str, to: Path | None = None, dry_run: bool = False, say: Callable[[str], None] = print,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """Make `to` the delivery: the lock, the sums, and every task whose tree is not already there."""
    to = to or default_dir(delivery)
    with _client(_key(None, False)) as h:
        try:
            lock_raw = _send(h, "GET", f"/deliveries/{delivery}/lock", sleep).content
            sums_raw = _send(h, "GET", f"/deliveries/{delivery}/sums", sleep).content
        except errors.MetaphiError as e:
            raise _named(e) from None
        lock = json.loads(lock_raw)
        there = to / LOCK
        if there.exists() and there.read_bytes() != lock_raw:
            try:
                other = json.loads(there.read_bytes()).get("delivery")
            except ValueError:
                other = "another lock"
            raise _need(f"{to} holds {other}, not {delivery}; a delivery has its own directory", "directory_in_use",
                        f"kennet delivery pull {delivery} --to {default_dir(delivery)}")
        home, kind = to / TASKS, _kind(lock)
        want = [t for t in lock["tasks"] if not _holds(home / t["name"], t["tree"], kind)]
        result: dict[str, Any] = {"id": delivery, "dir": str(to), "lock": str(there), "dry_run": dry_run,
                                  "tasks": len(lock["tasks"]), "unchanged": len(lock["tasks"]) - len(want),
                                  "fetched": [t["name"] for t in want], "bytes": 0}
        if dry_run:
            return result
        home.mkdir(parents=True, exist_ok=True)
        done = [0]

        def one(task: dict[str, Any]) -> int:
            size = _fetch(h, delivery, task, home, kind, sleep)
            done[0] += 1
            say(f"  {done[0]}/{len(want)}  {task['name']}")
            return size

        if want:
            say(f"Pulling {delivery}: {len(want)} of {len(lock['tasks'])} tasks to fetch")
            with ThreadPoolExecutor(AT_ONCE) as pool:
                result["bytes"] = sum(pool.map(one, want))
        (to / SUMS).write_bytes(sums_raw)
        there.write_bytes(lock_raw)
    return result


# -- verify: the directory against its own lock, with no call anywhere -------------------------------------------


def verify(to: Path) -> dict[str, Any]:
    """Every task of the directory against the lock beside it: as delivered, changed or missing, and what is there
    that the delivery did not bring."""
    try:
        lock = json.loads((to / LOCK).read_bytes())
    except (OSError, ValueError):
        raise _need(f"{to} holds no {LOCK}: it is not a pulled delivery", "not_a_delivery",
                    "kennet delivery list") from None
    home, delivery, kind = to / TASKS, lock["delivery"], _kind(lock)
    want: dict[str, str] = {}
    if (to / SUMS).exists():
        for line in (to / SUMS).read_text(encoding="utf-8").splitlines():
            digest, _, rel = line.partition("  ")
            want[rel] = digest
    tasks, differ = [], []
    for t in lock["tasks"]:
        bundle = home / t["name"]
        if not bundle.is_dir() or bundle.is_symlink():
            state, differing = "missing", []
        else:
            have = dict(reversed(line.split("  ", 1)) for line in sums(bundle))
            state = "as_delivered" if tree_hash(bundle, kind) == t["tree"] else "changed"
            mine = {rel[len(t["name"]) + 1:]: d for rel, d in want.items() if rel.startswith(t["name"] + "/")}
            differing = [] if state == "as_delivered" or not mine else [
                {"path": f, "state": "missing" if f not in have else "added" if f not in mine else "changed"}
                for f in sorted(mine.keys() | have.keys()) if mine.get(f) != have.get(f)]
        tasks.append({"name": t["name"], "tree": t["tree"], "state": state})
        if state != "as_delivered":
            # every byte as delivered and the tree is not: a file's mode differs, which the sums do not hold
            differ.append({"name": t["name"], "state": state, "files": differing,
                           "modes_only": state == "changed" and bool(mine) and not differing})
    named = {t["name"] for t in lock["tasks"]}
    added = sorted(p.name for p in home.iterdir() if p.name not in named and not p.name.startswith(".")
                   ) if home.is_dir() else []
    ok = not differ and not added
    return {"id": delivery, "dir": str(to), "ok": ok, "tasks": len(tasks),
            "as_delivered": len(tasks) - len(differ), "differ": differ, "added": added,
            "next_step": None if ok else f"kennet delivery pull {delivery} --to {to}"}


# -- diff: two versions, task by task ----------------------------------------------------------------------------


def _files(delivery: str, sleep: Callable[[float], None]) -> dict[str, dict[str, str]]:
    """{task: {path: sha256}} of a delivery's SHA256SUMS."""
    out: dict[str, dict[str, str]] = {}
    for line in _get(f"/deliveries/{delivery}/sums", sleep).text.splitlines():
        digest, _, rel = line.partition("  ")
        task, _, path = rel.partition("/")
        out.setdefault(task, {})[path] = digest
    return out


def diff(a: str, b: str, sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """What changed from delivery `a` to `b`, per task, and whether its grading did: a file under tests/ or an
    image that grades. A task whose grading changed is a different measure, and scores on it do not carry over."""
    one, two = show(a, sleep), show(b, sleep)
    old, new = {t["name"]: t for t in one["tasks"]}, {t["name"]: t for t in two["tasks"]}
    changed_names = [n for n in old if n in new and (old[n]["tree"] != new[n]["tree"]
                                                     or old[n]["images"] != new[n]["images"])]
    files_a, files_b = (_files(a, sleep), _files(b, sleep)) if changed_names else ({}, {})
    changed = []
    for n in changed_names:
        fa, fb = files_a.get(n, {}), files_b.get(n, {})
        paths = sorted(p for p in fa.keys() | fb.keys() if fa.get(p) != fb.get(p))
        graders = (old[n].get("grader_images"), new[n].get("grader_images"))
        images = old[n]["images"] != new[n]["images"]
        grading = any(p.startswith(GRADING) for p in paths) or (
            graders[0] != graders[1] if None not in graders else images)
        changed.append({"name": n, "grading_changed": grading, "images_changed": images, "files": paths})
    return {"from": a, "to": b, "added": sorted(set(new) - set(old)), "removed": sorted(set(old) - set(new)),
            "changed": changed, "unchanged": len(set(old) & set(new)) - len(changed),
            "grading_changed": [c["name"] for c in changed if c["grading_changed"]]}
