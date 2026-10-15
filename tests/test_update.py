"""Staying current: the server's version floor, the daily notice, how this copy was installed, and the self-update
into its own version directory behind a symlink, signed, checked and never pulled from under a running process."""
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import threading

import httpx
import pytest
import respx

from metaphi import __version__, accounts, cli, config, signature, update

A = config.DEFAULT_ACCOUNTS
NEXT = "9.9.9"
needs_ssh = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen signs the test releases")


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "CI", "KENNET_NO_UPDATE_NOTIFIER", "KENNET_DISABLE_AUTOUPDATE"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@pytest.fixture
def key(tmp_path, monkeypatch):
    """A release key of the test's own, made by ssh-keygen as release.yml's is."""
    k = tmp_path / "keys" / "release"
    k.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    monkeypatch.setattr(signature, "PUBLIC_KEY", (k.parent / "release.pub").read_text().strip())
    return k


def _sign(k, data: bytes) -> str:
    f = k.parent / "checksums.txt"
    f.write_bytes(data)
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(k), "-n", "kennet-release", str(f)], check=True)
    sig = (k.parent / "checksums.txt.sig").read_text()
    (k.parent / "checksums.txt.sig").unlink()
    return sig


def _release(k, version, script=None, sums=None, sign_with=None):
    """A release as release.yml makes it: the platform's archive, checksums.txt and checksums.txt.sig."""
    binary = (script or f'#!/bin/sh\necho "kennet {version}"\n').encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        info = tarfile.TarInfo("kennet")
        info.size, info.mode = len(binary), 0o755
        t.addfile(info, io.BytesIO(binary))
    name = f"kennet_{version}_{update.target()}.tar.gz"
    text = f"{sums or hashlib.sha256(buf.getvalue()).hexdigest()}  {name}\n".encode()
    base = f"{update.DOWNLOADS}v{version}/"
    respx.get(base + "checksums.txt").respond(content=text)
    respx.get(base + "checksums.txt.sig").respond(text=_sign(sign_with or k, text))
    respx.get(base + name).respond(content=buf.getvalue())


def _installed(versions):
    """install.sh's layout: each version in its own directory, the PATH's kennet a symlink to the last."""
    d = update.versions_dir()
    for v in versions:
        (d / v).mkdir(parents=True)
        (d / v / "kennet").write_text(f'#!/bin/sh\necho "kennet {v}"\n')
        (d / v / "kennet").chmod(0o755)
    link = update.data_dir().parent.parent / "bin" / "kennet"
    link.parent.mkdir(parents=True)
    link.symlink_to(d / versions[-1] / "kennet")
    (update.data_dir() / "link").write_text(str(link))
    return link


# -- how this copy was installed ---------------------------------------------------------------------

@pytest.mark.parametrize(("exe", "prefix", "frozen", "method", "command"), [
    ("/opt/homebrew/Cellar/kennet/0.1.16/bin/kennet", "/x", True, "brew", "brew upgrade metaphi-labs/tap/kennet"),
    ("/home/linuxbrew/.linuxbrew/bin/kennet", "/x", True, "brew", "brew upgrade metaphi-labs/tap/kennet"),
    ("VERSIONS/0.1.16/kennet", "/x", True, "binary", "kennet update"),
    ("/home/a/Downloads/kennet", "/x", True, "download", update.INSTALL_SH),
    ("/usr/bin/python3", "/home/a/.local/pipx/venvs/metaphi-ai", False, "pipx", "pipx upgrade metaphi-ai"),
    ("/usr/bin/python3", "/home/a/.local/share/uv/tools/metaphi-ai", False, "uv", "uv tool upgrade metaphi-ai"),
    ("/usr/bin/python3", "/home/a/venv", False, "pip", "pip install -U metaphi-ai"),
])
def test_install_method_names_the_upgrade_command(exe, prefix, frozen, method, command):
    exe = exe.replace("VERSIONS", str(update.versions_dir()))
    assert update.install_method(exe, prefix, frozen) == method
    assert update.upgrade_command(method) == command
    assert update.autoupdates(method) == (method == "binary")


def test_autoupdate_is_off_by_the_variable_and_in_ci(monkeypatch):
    monkeypatch.setenv("KENNET_DISABLE_AUTOUPDATE", "1")
    assert not update.autoupdates("binary")
    monkeypatch.delenv("KENNET_DISABLE_AUTOUPDATE")
    monkeypatch.setenv("CI", "true")
    assert not update.autoupdates("binary")


def test_versions_are_read_strictly():
    assert update._version("v1.2.3") == (1, 2, 3) and update._version("0.1.16") == (0, 1, 16)
    for bad in ("1.2.3-rc1", "1..2", "١.٢", "1.2.3.4.5", "", None, "v", " 1.2"):
        assert update._version(bad) is None
    assert not update.newer("1.2.x") and update.newer(NEXT)


# -- the version floor -------------------------------------------------------------------------------

@respx.mock
def test_426_is_upgrade_required_in_the_clis_own_words(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(426, json={"detail": {
        "code": "upgrade_required", "message": "anything the server says", "min_version": "0.2.0"}})
    assert cli.main(["org", "members", "--json"]) == cli.UPGRADE
    got = json.loads(capsys.readouterr().out)["error"]
    assert got["message"] == f"kennet {__version__} is too old for this service; it needs 0.2.0 or later"
    assert (got["code"], got["next_step"]) == ("upgrade_required", update.upgrade_command())
    assert cli.main(["org", "members"]) == cli.UPGRADE
    err = capsys.readouterr().err
    assert "anything the server says" not in err and "Traceback" not in err
    assert err.splitlines()[-1] == f"Next: {update.upgrade_command()}"


def test_accounts_requests_carry_the_floor_hook():
    with accounts._http() as h:
        assert update.floor in h.event_hooks["response"]


# -- the daily look and the notice -------------------------------------------------------------------

def _releases(*tags):
    return [{"tag_name": t, "draft": False, "prerelease": False} for t in tags]


@respx.mock
def test_the_check_takes_the_newest_by_version_once_a_day():
    gh = respx.get(update.RELEASES).respond(json=_releases("v0.1.1", f"v{NEXT}", "v0.1.15", "v1.0.0-rc1")
                                            + [{"tag_name": "v10.0.0", "draft": True}])
    assert update.check(now=1e9)["latest"] == NEXT
    assert update.check(now=1e9 + update.EVERY_S - 1)["latest"] == NEXT and gh.call_count == 1
    gh.respond(500)
    assert update.check(now=1e9 + update.EVERY_S + 1)["latest"] == NEXT and gh.call_count == 2
    assert "release check failed" in (config.home() / "update.log").read_text()


@respx.mock
def test_the_time_of_a_look_is_kept_before_the_request(monkeypatch):
    seen = []

    def slow(request):
        seen.append(update.state().get("checked_at"))
        return httpx.Response(200, json=_releases(f"v{NEXT}"))
    respx.get(update.RELEASES).mock(side_effect=slow)
    update.check(now=5e9)
    assert seen == [5e9]   # a command that ends mid-look still waits a day for the next one


def test_the_notice_names_the_command_and_is_quiet_where_it_must_be(monkeypatch):
    assert update.notice({"latest": __version__}, "pip") is None
    assert update.notice({"latest": NEXT}, "pip") == (f"kennet {NEXT} is out (you have {__version__}). "
                                                      "Upgrade: pip install -U metaphi-ai")
    assert "brew upgrade metaphi-labs/tap/kennet" in update.notice({"latest": NEXT}, "brew")
    assert update.INSTALL_SH in update.notice({"latest": NEXT}, "download")
    assert "installs itself" in update.notice({"latest": NEXT}, "binary")
    monkeypatch.setattr(update.sys.stderr, "isatty", lambda: True, raising=False)
    assert not update.quiet(False)
    assert update.quiet(True)
    for var in ("CI", "KENNET_NO_UPDATE_NOTIFIER"):
        monkeypatch.setenv(var, "1")
        assert update.quiet(False)
        monkeypatch.delenv(var)
    monkeypatch.setattr(update.sys.stderr, "isatty", lambda: False, raising=False)
    assert update.quiet(False)


def test_the_notice_is_shown_once_a_day_after_the_command(monkeypatch, capsys):
    monkeypatch.setattr(update, "quiet", lambda json_out: json_out)
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "pip")
    update._save_state({"checked_at": 9e12, "latest": NEXT})
    assert cli.main(["skill", "show"]) == 0
    got = capsys.readouterr()
    assert got.out.startswith("# Kennet") and got.err == (f"kennet {NEXT} is out (you have {__version__}). "
                                                          "Upgrade: pip install -U metaphi-ai\n")
    assert cli.main(["skill", "show"]) == 0
    assert capsys.readouterr().err == ""   # said once; again only a day later
    n = update.Notifier(False)
    n.done(now=update.state()["noticed_at"] + update.EVERY_S)
    assert "is out" in capsys.readouterr().err


def test_the_notice_never_waits_for_a_slow_look(monkeypatch, capsys):
    gate = threading.Event()
    monkeypatch.setattr(update, "quiet", lambda json_out: False)
    monkeypatch.setattr(update, "check", lambda: gate.wait(5))
    update.Notifier(False).done()
    gate.set()
    assert capsys.readouterr().err == ""


def test_the_self_update_starts_once_per_release_and_never_for_brew_or_pip(monkeypatch):
    started = []
    monkeypatch.setattr(update, "spawn", lambda: started.append(1))
    monkeypatch.setattr(update, "quiet", lambda json_out: True)
    update._save_state({"checked_at": 9e12, "latest": NEXT})
    for method in ("brew", "pip", "download", "binary", "binary"):
        monkeypatch.setattr(update, "install_method", lambda *a, m=method, **k: m)
        update.Notifier(True).done()
    assert started == [1] and update.state()["tried"] == NEXT


# -- the self-update ---------------------------------------------------------------------------------

@needs_ssh
@respx.mock
def test_an_update_is_a_new_version_and_a_flipped_symlink_and_a_running_version_survives(key):
    link = _installed(["0.1.14", "0.1.15", "0.1.16"])
    d = update.versions_dir()
    import fcntl
    running = open(d / "0.1.14" / ".lock", "a")   # noqa: SIM115 - a `kennet twin` still running 0.1.14
    fcntl.flock(running, fcntl.LOCK_SH)
    _release(key, NEXT)
    assert update.install(NEXT, link) == d / NEXT / "kennet"
    assert os.readlink(link) == str(d / NEXT / "kennet")
    assert subprocess.run([str(link)], capture_output=True, text=True, check=True).stdout == f"kennet {NEXT}\n"
    # kept: the newest two, and 0.1.14, which a process still runs; 0.1.15 is gone
    assert sorted(p.name for p in d.iterdir()) == ["0.1.14", "0.1.16", NEXT]
    assert (d / "0.1.14" / "kennet").read_text() == '#!/bin/sh\necho "kennet 0.1.14"\n'
    running.close()
    assert [p.name for p in link.parent.iterdir()] == ["kennet"]   # no temporary symlink left


@needs_ssh
@respx.mock
def test_a_checksum_mismatch_is_refused_and_nothing_changes(key):
    link = _installed(["0.1.16"])
    _release(key, NEXT, sums="ab" * 32)
    with pytest.raises(update.UpdateError, match="does not match checksums.txt") as e:
        update.install(NEXT, link)
    assert e.value.code == "update_mismatch"
    assert os.readlink(link).endswith("0.1.16/kennet") and not (update.versions_dir() / NEXT).exists()


@needs_ssh
@respx.mock
def test_a_release_signed_by_another_key_is_refused(key, tmp_path):
    other = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
    link = _installed(["0.1.16"])
    _release(key, NEXT, sign_with=other)
    with pytest.raises(update.UpdateError, match="not signed by Metaphi AI's release key") as e:
        update.install(NEXT, link)
    assert e.value.code == "update_unsigned" and os.readlink(link).endswith("0.1.16/kennet")


def test_no_release_key_verifies_nothing(monkeypatch):
    monkeypatch.setattr(signature, "PUBLIC_KEY", "")
    with pytest.raises(signature.BadSignature):
        signature.verify(b"x", "-----BEGIN SSH SIGNATURE-----\nU1NIU0lH\n-----END SSH SIGNATURE-----")


def test_ed25519_verifies_rfc_8032_test_1():
    pub = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b4"
                        "6bd25bf5f0595bbe24655141438e7a100b")
    assert signature.ed25519_verify(pub, b"", sig) and not signature.ed25519_verify(pub, b"x", sig)


@needs_ssh
@respx.mock
def test_a_binary_that_does_not_answer_its_version_is_not_installed(key):
    link = _installed(["0.1.16"])
    _release(key, NEXT, script="#!/bin/sh\nexit 1\n")
    with pytest.raises(update.UpdateError, match="did not start here"):
        update.install(NEXT, link)
    assert os.readlink(link).endswith("0.1.16/kennet") and not (update.versions_dir() / NEXT).exists()


@respx.mock
def test_a_redirect_off_github_is_refused():
    respx.get(update.RELEASES).respond(302, headers={"location": "https://evil.example/releases"})
    with pytest.raises(update.UpdateError, match="not GitHub"):
        update.latest_release()


@needs_ssh
@respx.mock
def test_a_link_in_a_root_owned_directory_says_how_to_reinstall(key, monkeypatch):
    link = _installed(["0.1.16"])
    _release(key, NEXT)

    def denied(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(update.os, "replace", denied)
    with pytest.raises(update.UpdateError, match="installed by root. Reinstall it with install.sh, or use brew") as e:
        update.install(NEXT, link)
    assert e.value.next_step == update.INSTALL_SH


def test_stale_temporary_files_are_cleaned_on_the_next_run():
    link = _installed(["0.1.16"])
    d = update.versions_dir()
    (d / ".tmp-dead").mkdir()
    (link.parent / ".kennet.link.dead").symlink_to(d / "0.1.16" / "kennet")
    (d / ".tmp-fresh").mkdir()
    old = 1e9
    for p in (d / ".tmp-dead", link.parent / ".kennet.link.dead"):
        os.utime(p, (old, old), follow_symlinks=False)
    update.clean_stale()
    assert not (d / ".tmp-dead").exists() and not os.path.lexists(link.parent / ".kennet.link.dead")
    assert (d / ".tmp-fresh").exists()   # perhaps another run's, still at work


# -- kennet update -----------------------------------------------------------------------------------

@respx.mock
def test_update_prints_the_command_for_an_install_it_does_not_replace(monkeypatch, capsys):
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "brew")
    respx.get(update.RELEASES).respond(json=_releases(f"v{NEXT}"))
    assert cli.main(["update", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["latest"], got["updated"], got["next_step"]) == (NEXT, False, "brew upgrade metaphi-labs/tap/kennet")


@respx.mock
def test_update_never_claims_newest_when_the_look_fails(capsys):
    update._save_state({"checked_at": 9e12, "latest": __version__})
    respx.get(update.RELEASES).mock(side_effect=httpx.ConnectError("down"))
    assert cli.main(["update", "--json"]) == cli.LATER
    got = json.loads(capsys.readouterr().out)["error"]
    assert (got["code"], got["retryable"], got["type"]) == ("update_unreachable", True, "update_error")
    assert cli.main(["update"]) == cli.LATER
    err = capsys.readouterr()
    assert "newest" not in err.out and "Traceback" not in err.err and err.err.startswith("error: GitHub could not")


@needs_ssh
@respx.mock
def test_the_worker_is_silent_and_logs_a_failure(key, monkeypatch):
    link = _installed(["0.1.16"])
    monkeypatch.setattr(update, "install_method", lambda *a, **k: "binary")
    update._save_state({"checked_at": 9e12, "latest": NEXT})
    _release(key, NEXT, sums="cd" * 32)
    assert cli.main([update.WORKER]) == 0
    assert "self-update to 9.9.9 failed" in (config.home() / "update.log").read_text()
    assert os.readlink(link).endswith("0.1.16/kennet")


def test_version_prints_the_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out == f"kennet {__version__}\n"
