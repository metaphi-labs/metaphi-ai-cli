"""Staying current: the server's version floor, the daily notice, how this copy was installed, the self-update."""
import hashlib
import io
import json
import os
import tarfile

import httpx
import pytest
import respx

from metaphi import __version__, accounts, cli, config, update

A = config.DEFAULT_ACCOUNTS
NEXT = "9.9.9"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "CI", "KENNET_NO_UPDATE_NOTIFIER", "KENNET_DISABLE_AUTOUPDATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.mark.parametrize(("exe", "prefix", "frozen", "method", "command"), [
    ("/opt/homebrew/Cellar/kennet/0.1.16/bin/kennet", "/x", True, "brew", "brew upgrade metaphi-labs/tap/kennet"),
    ("/home/linuxbrew/.linuxbrew/bin/kennet", "/x", True, "brew", "brew upgrade metaphi-labs/tap/kennet"),
    ("/home/a/.local/bin/kennet", "/x", True, "binary", "kennet update"),
    ("/usr/bin/python3", "/home/a/.local/pipx/venvs/metaphi-ai", False, "pipx", "pipx upgrade metaphi-ai"),
    ("/usr/bin/python3", "/home/a/.local/share/uv/tools/metaphi-ai", False, "uv", "uv tool upgrade metaphi-ai"),
    ("/usr/bin/python3", "/home/a/venv", False, "pip", "pip install -U metaphi-ai"),
])
def test_install_method_names_the_upgrade_command(exe, prefix, frozen, method, command):
    assert update.install_method(exe, prefix, frozen) == method
    assert update.upgrade_command(method) == command
    assert update.autoupdates(method) == (method == "binary")


def test_autoupdate_is_off_by_the_variable_and_in_ci(monkeypatch):
    monkeypatch.setenv("KENNET_DISABLE_AUTOUPDATE", "1")
    assert not update.autoupdates("binary")
    monkeypatch.delenv("KENNET_DISABLE_AUTOUPDATE")
    monkeypatch.setenv("CI", "true")
    assert not update.autoupdates("binary")


@respx.mock
def test_426_is_upgrade_required_with_this_installs_command(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(426, json={"detail": {
        "code": "upgrade_required", "message": "kennet 0.1.16 is too old; 0.2.0 or later is needed",
        "min_version": "0.2.0"}})
    assert cli.main(["org", "members", "--json"]) == cli.UPGRADE
    got = json.loads(capsys.readouterr().out)["error"]
    assert (got["code"], got["type"], got["next_step"]) == ("upgrade_required", "upgrade_error",
                                                           update.upgrade_command())
    assert cli.main(["org", "members"]) == cli.UPGRADE
    err = capsys.readouterr().err
    assert err.startswith("error: kennet 0.1.16 is too old") and "Traceback" not in err
    assert err.splitlines()[-1] == f"Next: {update.upgrade_command()}"


def _releases(*tags):
    return [{"tag_name": t, "draft": False, "prerelease": False} for t in tags]


@respx.mock
def test_the_check_takes_the_newest_by_version_once_a_day():
    gh = respx.get(update.RELEASES).respond(json=_releases("v0.1.1", f"v{NEXT}", "v0.1.15")
                                            + [{"tag_name": "v10.0.0", "draft": True}])
    assert update.check(now=1e9)["latest"] == NEXT
    assert update.check(now=1e9 + update.EVERY_S - 1)["latest"] == NEXT and gh.call_count == 1
    gh.respond(500)   # a failed look is a debug line, and waits a day too
    assert update.check(now=1e9 + update.EVERY_S + 1)["latest"] == NEXT and gh.call_count == 2
    assert "release check failed" in (config.home() / "update.log").read_text()


def test_the_notice_names_the_command_and_is_quiet_where_it_must_be(monkeypatch):
    assert update.notice({"latest": __version__}, "pip") is None
    assert update.notice({"latest": NEXT}, "pip") == (f"kennet {NEXT} is out (you have {__version__}). "
                                                      "Upgrade: pip install -U metaphi-ai")
    assert "brew upgrade metaphi-labs/tap/kennet" in update.notice({"latest": NEXT}, "brew")
    assert "kennet update" in update.notice({"latest": NEXT}, "binary")
    monkeypatch.setattr(update.sys.stderr, "isatty", lambda: True, raising=False)
    assert not update.quiet(False)
    assert update.quiet(True)   # --json
    monkeypatch.setenv("CI", "1")
    assert update.quiet(False)
    monkeypatch.delenv("CI")
    monkeypatch.setenv("KENNET_NO_UPDATE_NOTIFIER", "1")
    assert update.quiet(False)
    monkeypatch.delenv("KENNET_NO_UPDATE_NOTIFIER")
    monkeypatch.setattr(update.sys.stderr, "isatty", lambda: False, raising=False)
    assert update.quiet(False)


def test_the_notice_comes_after_the_command_on_stderr_from_the_kept_state(monkeypatch, capsys):
    monkeypatch.setattr(update, "quiet", lambda json_out: json_out)
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "pip")
    update._save_state({"checked_at": 9e12, "latest": NEXT})   # a fresh look: none is made now
    assert cli.main(["skill", "show"]) == 0
    got = capsys.readouterr()
    assert got.out.startswith("# Kennet") and got.err == (f"kennet {NEXT} is out (you have {__version__}). "
                                                          "Upgrade: pip install -U metaphi-ai\n")
    assert cli.main(["skill", "remove", "--json"]) == 0
    assert capsys.readouterr().err == ""


def test_the_notice_never_waits_for_a_slow_look(monkeypatch, capsys):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(update, "quiet", lambda json_out: False)
    monkeypatch.setattr(update, "check", lambda: gate.wait(5))
    n = update.Notifier(False)
    n.done()   # returns at once, says nothing; the look is told next time
    gate.set()
    assert capsys.readouterr().err == ""


def _release(version, binary=b"#!/bin/sh\necho new\n", sums=None):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("kennet")
        info.size, info.mode = len(binary), 0o755
        t.addfile(info, io.BytesIO(binary))
    name = f"kennet_{version}_{update.target()}.tar.gz"
    digest = sums or hashlib.sha256(buf.getvalue()).hexdigest()
    base = f"{update.DOWNLOADS}v{version}/"
    respx.get(base + "checksums.txt").respond(text=f"{digest}  {name}\n0000  kennet_{version}_other.tar.gz\n")
    respx.get(base + name).respond(content=buf.getvalue())


@respx.mock
def test_the_binary_replaces_itself_after_checking_the_checksum(tmp_path):
    exe = tmp_path / "bin" / "kennet"
    exe.parent.mkdir()
    exe.write_bytes(b"old")
    _release(NEXT)
    assert update.install(NEXT, str(exe)) == exe
    assert exe.read_bytes() == b"#!/bin/sh\necho new\n" and os.access(exe, os.X_OK)
    assert [p.name for p in exe.parent.iterdir()] == ["kennet"]   # nothing left beside it


@respx.mock
def test_a_checksum_mismatch_is_refused_and_nothing_is_replaced(tmp_path):
    exe = tmp_path / "kennet"
    exe.write_bytes(b"old")
    _release(NEXT, sums="ab" * 32)
    with pytest.raises(update.UpdateError, match="does not match checksums.txt"):
        update.install(NEXT, str(exe))
    assert exe.read_bytes() == b"old" and [p.name for p in tmp_path.iterdir()] == ["kennet"]


@respx.mock
def test_the_worker_is_silent_and_logs_a_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "binary")
    monkeypatch.setattr(update.sys, "executable", str(tmp_path / "kennet"))
    (tmp_path / "kennet").write_bytes(b"old")
    update._save_state({"checked_at": 9e12, "latest": NEXT})
    _release(NEXT, sums="cd" * 32)
    assert cli.main([update.WORKER]) == 0
    assert "self-update to 9.9.9 failed" in (config.home() / "update.log").read_text()
    assert (tmp_path / "kennet").read_bytes() == b"old"


def test_the_self_update_starts_once_per_release_and_never_for_brew_or_pip(monkeypatch):
    started = []
    monkeypatch.setattr(update, "spawn", lambda: started.append(1))
    monkeypatch.setattr(update, "quiet", lambda json_out: True)
    monkeypatch.setattr(update, "check", lambda: None)
    update._save_state({"checked_at": 9e12, "latest": NEXT})
    for method in ("brew", "pip", "binary", "binary"):
        monkeypatch.setattr(update, "install_method", lambda *a, m=method, **k: m)
        n = update.Notifier(True)
        n.thread and n.thread.join()
        n.done()
    assert started == [1] and update.state()["tried"] == NEXT


@respx.mock
def test_update_prints_the_command_for_an_install_it_does_not_replace(monkeypatch, capsys):
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "brew")
    respx.get(update.RELEASES).respond(json=_releases(f"v{NEXT}"))
    assert cli.main(["update", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["latest"], got["updated"], got["next_step"]) == (NEXT, False, "brew upgrade metaphi-labs/tap/kennet")


def test_version_prints_the_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out == f"kennet {__version__}\n"


def test_accounts_requests_carry_the_floor_hook():
    with accounts._http() as h:
        assert update.floor in h.event_hooks["response"]
    assert isinstance(httpx.Response(200), httpx.Response)
