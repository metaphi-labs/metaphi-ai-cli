"""`kennet delivery`: list, show, pull, verify and diff against a stand-in for the API."""
import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
import respx

from metaphi import cli, config, delivery

API = config.DEFAULT_API
REGISTRY = "registry.example/images-a"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def served(tmp_path, name: str, tasks: dict[str, dict[str, str]]) -> dict:
    """A delivery behind the API: its routes mocked, its lock returned."""
    root, locked, tars = tmp_path / "cut" / name / "tasks", [], {}
    for task, files in tasks.items():
        for rel, text in files.items():
            path = root / task / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            path.chmod(0o755 if rel.endswith(".sh") else 0o644)
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w") as tar:
            for path in sorted((root / task).rglob("*")):
                tar.add(path, arcname=path.relative_to(root / task).as_posix(), recursive=False)
        tars[task] = raw.getvalue()
        locked.append({"name": task, "tree": delivery.tree_hash(root / task),
                       "images": [f"{task}-env@sha256:{'1' * 64}", f"grader@sha256:{'2' * 64}"]})
    lock = {"lock": 2, "delivery": name, "customer": "a", "delivered": "2026-10-12", "source": "0" * 40,
            "registry": REGISTRY, "tasks": locked}
    view = {"id": name, "object": "delivery", "name": name.split("@")[0], "version": name.split("@")[1],
            "delivered": "2026-10-12", "newer": None, "registry": REGISTRY, "task_count": len(locked),
            "image_count": len(locked) + 1, "bytes": sum(map(len, tars.values())),
            "tasks": [{**t, "bytes": len(tars[t["name"]]), "files": 2, "category": "software-engineering",
                       "difficulty": "medium", "agent_timeout_s": 7200.0, "verifier_timeout_s": 2400.0, "cpus": 4,
                       "memory_mb": 8192, "storage_mb": 20480, "grader_images": [t["images"][1]]} for t in locked]}
    base = f"{API}/deliveries/{name}"
    respx.get(base).respond(json=view)
    respx.get(f"{base}/lock").respond(content=(json.dumps(lock, indent=1) + "\n").encode())
    respx.get(f"{base}/sums").respond(text="".join(s + "\n" for s in delivery.sums(root)))
    hits = {}
    for task, body in tars.items():
        hits[task] = respx.get(f"{base}/tasks/{task}/content").respond(content=body)
    return {"lock": lock, "view": view, "hits": hits}


def run(capsys, *argv: str) -> tuple[int, dict, str]:
    code = cli.main([*argv, "--json"])
    out = capsys.readouterr()
    return code, json.loads(out.out), out.err


TASKS = {"one": {"instruction.md": "do one", "tests/test.sh": "#!/bin/sh\nexit 0\n"},
         "two": {"instruction.md": "do two", "tests/test.sh": "#!/bin/sh\nexit 0\n"}}


@respx.mock
def test_list_and_show_say_what_the_organization_holds(home, capsys):
    made = served(home, "set@0.1.0", TASKS)
    summary = {k: v for k, v in made["view"].items() if k != "tasks"}
    respx.get(f"{API}/deliveries").respond(json={"object": "list", "data": [summary], "has_more": False, "url": ""})
    code, got, _ = run(capsys, "delivery", "list")
    assert code == 0 and got == {"data": [summary]}
    assert cli.main(["delivery", "list"]) == 0
    said = capsys.readouterr().out
    assert "set@0.1.0  2026-10-12  2 tasks  3 images" in said and "Next: kennet delivery show set@0.1.0" in said
    code, got, _ = run(capsys, "delivery", "show", "set@0.1.0")
    assert code == 0 and got == made["view"]
    assert cli.main(["delivery", "show", "set@0.1.0"]) == 0
    said = capsys.readouterr().out
    assert "tasks     2  (software-engineering 2; medium 2)" in said
    assert "up to 4 CPUs, 8192 MB memory, 20480 MB disk; the agent up to 7200 s, grading up to 2400 s" in said
    assert f"each pinned by digest, in {REGISTRY}" in said and said.rstrip().endswith("kennet delivery pull set@0.1.0")
    assert len(said.splitlines()) < 12   # one screen


@respx.mock
def test_pull_fetches_what_differs_and_verify_checks_it_offline(home, capsys):
    made = served(home, "set@0.1.0", TASKS)
    code, got, _ = run(capsys, "delivery", "pull", "set@0.1.0", "--dry-run")
    assert code == 0 and got["dry_run"] and got["fetched"] == ["one", "two"] and not (home / "set-0.1.0").exists()
    code, got, err = run(capsys, "pull", "set@0.1.0")
    assert code == 0 and got["dir"] == "set-0.1.0" and got["fetched"] == ["one", "two"] and got["unchanged"] == 0
    assert got["next_step"] == "kennet delivery verify set-0.1.0" and "2/2" in err
    to = home / "set-0.1.0"
    assert json.loads((to / "delivery.lock.json").read_text()) == made["lock"]
    assert (to / "tasks" / "one" / "tests" / "test.sh").stat().st_mode & 0o111
    assert sorted(p.name for p in (to / "tasks").iterdir()) == ["one", "two"]   # nothing left half-written

    # again: nothing is fetched
    code, got, _ = run(capsys, "delivery", "pull", "set@0.1.0")
    assert code == 0 and got["fetched"] == [] and got["unchanged"] == 2 and got["bytes"] == 0
    assert [h.call_count for h in made["hits"].values()] == [1, 1]

    # verify reads the directory alone: no route is asked, and no key is needed
    respx.reset()
    code, got, _ = run(capsys, "verify", "set-0.1.0")
    assert code == 0 and got["ok"] and got["as_delivered"] == 2 and got["next_step"] is None

    (to / "tasks" / "one" / "tests" / "test.sh").write_text("#!/bin/sh\nexit 1\n")
    (to / "tasks" / "one" / "notes.txt").write_text("mine")
    (to / "tasks" / "stray").mkdir()
    code, got, _ = run(capsys, "delivery", "verify", "set-0.1.0")
    assert code == 1 and not got["ok"] and got["as_delivered"] == 1 and got["added"] == ["stray"]
    one = got["differ"][0]
    assert got["differ"] == [one] and (one["name"], one["state"], one["modes_only"]) == ("one", "changed", False)
    assert [(f["path"], f["state"]) for f in one["files"]] == [("notes.txt", "added"), ("tests/test.sh", "changed")]
    added, changed = one["files"]
    assert added["delivered"] is None and len(added["here"]) == 64     # both hashes: what came, what is there
    assert len(changed["delivered"]) == 64 and changed["delivered"] != changed["here"]
    assert got["next_step"] == "kennet delivery pull set@0.1.0 --to set-0.1.0"
    assert cli.main(["verify", "set-0.1.0"]) == 1
    said = capsys.readouterr().out
    assert "changed  one\n  added    notes.txt  here " in said
    assert f"  changed  tests/test.sh  delivered {changed['delivered'][:12]}  here {changed['here'][:12]}" in said
    assert "set@0.1.0: 1 of 2 tasks as delivered" in said

    # a mode is part of the tree: the bytes are as delivered, and the task is not
    (to / "tasks" / "two" / "tests" / "test.sh").chmod(0o644)
    code, got, _ = run(capsys, "verify", "set-0.1.0")
    assert code == 1 and {"name": "two", "state": "changed", "files": [], "modes_only": True} in got["differ"]
    (to / "tasks" / "two" / "tests" / "test.sh").chmod(0o755)

    # a pull puts the changed task back as delivered, and leaves what it did not bring
    made = served(home, "set@0.1.0", TASKS)
    code, got, _ = run(capsys, "pull", "set@0.1.0")
    assert code == 0 and got["fetched"] == ["one"] and got["unchanged"] == 1
    assert not (to / "tasks" / "one" / "notes.txt").exists() and (to / "tasks" / "stray").is_dir()


@respx.mock
def test_a_download_that_is_not_the_locked_tree_is_never_put_in_place(home, capsys):
    served(home, "set@0.1.0", TASKS)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        body = b"something else"
        info = tarfile.TarInfo("instruction.md")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    respx.get(f"{API}/deliveries/set@0.1.0/tasks/one/content").respond(content=raw.getvalue())
    code, got, _ = run(capsys, "pull", "set@0.1.0")
    assert code == 4 and got["error"]["code"] == "tree_mismatch" and got["error"]["retryable"]
    assert not (home / "set-0.1.0" / "tasks" / "one").exists()
    assert not (home / "set-0.1.0" / "delivery.lock.json").exists()   # no lock until every task is in place


@respx.mock
def test_refusals_name_the_next_command(home, capsys):
    respx.get(f"{API}/deliveries/none@1/lock").respond(404, json={"error": {
        "type": "invalid_request_error", "code": "resource_missing", "message": "no delivery none@1 here",
        "retryable": False}})
    code, got, err = run(capsys, "pull", "none@1")
    assert code == 1 and got["error"]["next_step"] == "kennet delivery list" and "Next: kennet delivery list" in err
    respx.get(f"{API}/deliveries").respond(403, json={"error": {
        "type": "auth_error", "code": "insufficient_scope", "retryable": False,
        "message": "this API key does not hold access to deliveries"}})
    code, got, _ = run(capsys, "delivery", "list")
    assert code == 3 and got["error"]["next_step"] == "kennet login"
    code, got, _ = run(capsys, "verify", "nowhere")
    assert code == 1 and got["error"]["code"] == "not_a_delivery"
    served(home, "set@0.1.0", TASKS)
    served(home, "set@0.1.1", TASKS)
    assert run(capsys, "pull", "set@0.1.0", "--to", "d")[0] == 0
    code, got, _ = run(capsys, "pull", "set@0.1.1", "--to", "d")
    assert code == 1 and got["error"]["code"] == "directory_in_use"
    assert got["error"]["next_step"] == "kennet delivery pull set@0.1.1 --to set-0.1.1"
    Path("a-file").write_text("x")                      # a path that is no directory is a refusal, not a crash
    code, got, _ = run(capsys, "pull", "set@0.1.0", "--to", "a-file")
    assert code == 1 and got["error"]["code"] == "directory_in_use"


@respx.mock
def test_diff_says_whose_grading_changed(home, capsys):
    served(home, "set@0.1.0", {**TASKS, "gone": {"instruction.md": "x"}})
    served(home, "set@0.1.1", {"one": {**TASKS["one"], "tests/test.sh": "#!/bin/sh\nexit 2\n"},
                               "two": {**TASKS["two"], "instruction.md": "do two, clearly"},
                               "new": {"instruction.md": "n"}})
    code, got, _ = run(capsys, "delivery", "diff", "set@0.1.0", "set@0.1.1")
    assert code == 0 and got["added"] == ["new"] and got["removed"] == ["gone"] and got["unchanged"] == 0
    assert got["changed"] == [
        {"name": "one", "grading_changed": True, "images_changed": False, "files": ["tests/test.sh"]},
        {"name": "two", "grading_changed": False, "images_changed": False, "files": ["instruction.md"]}]
    assert got["grading_changed"] == ["one"]

    # a pulled directory stands for its delivery on either side; two of them are compared with no call anywhere
    assert run(capsys, "pull", "set@0.1.0")[0] == 0 and run(capsys, "pull", "set@0.1.1")[0] == 0
    respx.reset()
    code, offline, _ = run(capsys, "delivery", "diff", "set-0.1.0", "set-0.1.1")
    assert code == 0 and offline == got
    code, bad, _ = run(capsys, "delivery", "diff", "set-0.1.0", "nowhere/")
    assert code == 1 and bad["error"]["code"] == "invalid_delivery"


def test_a_chain_of_links_never_carries_a_file_out_of_the_task(home):
    raw = home / "chain.tar"
    with tarfile.open(raw, "w") as tar:
        def add(name, link=None, kind=tarfile.DIRTYPE):
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE if link else kind
            info.linkname = link or ""
            tar.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE and not link else None)
        add("a")
        for name in ("a/b", "a/b/c", "a/b/c/d", "a/b/c/d/e"):
            add(name, link="..")
        add("a/b/c/d/e/PWNED.txt", kind=tarfile.REGTYPE)
    (home / "deep" / "er").mkdir(parents=True)
    with pytest.raises(ValueError, match="outside the task"):
        delivery._unpack(raw, home / "deep" / "er" / "into")
    assert not list(home.rglob("PWNED.txt"))


@respx.mock
def test_a_lock_whose_names_are_not_names_writes_nothing(home, capsys):
    (home / "victim").mkdir()
    (home / "victim" / "keep.txt").write_text("mine")
    for name in ("../../victim", "a/b", ".", ""):
        lock = {"lock": 2, "delivery": "set@0.1.0", "tasks": [{"name": name, "tree": "sha256:" + "0" * 64,
                                                               "images": []}]}
        respx.get(f"{API}/deliveries/set@0.1.0/lock").respond(json=lock)
        respx.get(f"{API}/deliveries/set@0.1.0/sums").respond(text="")
        code, got, _ = run(capsys, "pull", "set@0.1.0", "--to", "x/y")
        assert code == 1 and got["error"]["code"] == "lock_invalid", name
    assert (home / "victim" / "keep.txt").read_text() == "mine" and not (home / "x").exists()
    code, got, _ = run(capsys, "pull", "../set@1")
    assert code == 1 and got["error"]["code"] == "invalid_delivery"


@respx.mock
def test_verify_holds_the_sums_file_and_a_lock_it_cannot_read_is_no_delivery(home, capsys):
    served(home, "set@0.1.0", TASKS)
    assert run(capsys, "pull", "set@0.1.0")[0] == 0
    to = home / "set-0.1.0"
    kept = (to / "SHA256SUMS").read_text()
    (to / "SHA256SUMS").write_text(kept.replace("instruction.md", "instruction.txt", 1))
    code, got, _ = run(capsys, "verify", "set-0.1.0")
    assert code == 1 and got["sums"] == "changed" and got["as_delivered"] == 2
    (to / "SHA256SUMS").unlink()
    code, got, _ = run(capsys, "verify", "set-0.1.0")
    assert code == 1 and got["sums"] == "missing"
    (to / "SHA256SUMS").write_text(kept)
    (to / "tasks" / ".hidden").mkdir()
    code, got, _ = run(capsys, "verify", "set-0.1.0")
    assert code == 1 and got["added"] == [".hidden"] and got["sums"] == "as_delivered"
    for body in ("{}", "[]", "not json", '{"delivery": "x; rm -rf ~@1", "tasks": []}'):
        (to / "delivery.lock.json").write_text(body)
        code, got, _ = run(capsys, "verify", "set-0.1.0")
        assert code == 1 and got["error"]["code"] == "not_a_delivery", body


def test_a_tar_that_reaches_outside_the_task_is_refused(home):
    for name, link in (("../out", None), ("a", "/etc/passwd"), ("a", "../../out")):
        raw = home / "bad.tar"
        with tarfile.open(raw, "w") as tar:
            info = tarfile.TarInfo(name)
            if link:
                info.type, info.linkname = tarfile.SYMTYPE, link
            tar.addfile(info, None if link else io.BytesIO(b""))
        with pytest.raises(ValueError):
            delivery._unpack(raw, home / "into")
        assert not (home / "into").exists()
    assert httpx  # the transport the client uses


@respx.mock
def test_registry_login_hands_the_token_to_the_client_on_stdin_and_never_says_it(home, capsys, monkeypatch):
    token = {"object": "registry_token", "server": "registry.example", "repository": "registry.example/p/images",
             "username": "oauth2accesstoken", "token": "ya29.secret", "expires_at": 1790000000}
    respx.post(f"{API}/registry/token").respond(200, json=token)
    ran = []

    def run_(cmd, **kw):
        ran.append((cmd, kw["input"]))
        return type("R", (), {"returncode": 0, "stdout": "Login Succeeded", "stderr": ""})()
    monkeypatch.setattr(delivery.shutil, "which", lambda name: f"/bin/{name}" if name == "podman" else None)
    monkeypatch.setattr(delivery.subprocess, "run", run_)
    code = cli.main(["registry", "login", "--json"])
    said = capsys.readouterr()
    got = json.loads(said.out)
    assert code == 0 and got["client"] == "podman" and got["repository"] == token["repository"]
    assert ran == [(["/bin/podman", "login", "registry.example", "--username", "oauth2accesstoken",
                     "--password-stdin"], "ya29.secret")]
    assert "ya29" not in said.out + said.err and "ya29" not in " ".join(ran[0][0])

    assert cli.main(["registry", "token"]) == 0 and capsys.readouterr().out == "ya29.secret\n"
    code, got, _ = run(capsys, "registry", "token")
    assert code == 0 and got == token

    monkeypatch.setattr(delivery.shutil, "which", lambda name: None)
    code, got, _ = run(capsys, "registry", "login")
    assert code == 1 and got["error"]["code"] == "no_container_client"
    assert got["error"]["next_step"] == "kennet registry token"
    respx.post(f"{API}/registry/token").respond(404, json={"error": {
        "type": "invalid_request_error", "code": "resource_missing", "retryable": False,
        "message": "no image repository org here"}})
    code, got, _ = run(capsys, "registry", "token")
    assert code == 1 and got["error"]["code"] == "resource_missing"
