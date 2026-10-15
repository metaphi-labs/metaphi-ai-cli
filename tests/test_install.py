"""install.sh against a release directory of its own (KENNET_RELEASE_URL), signed by a key of the test's own: it
installs a version directory behind a symlink, upgrades, migrates a plain-file install, and refuses an unsigned or
mismatched release. CI runs it on Linux and macOS."""
import hashlib
import os
import platform
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
pytestmark = pytest.mark.skipif(os.name != "posix" or shutil.which("ssh-keygen") is None,
                                reason="POSIX sh and ssh-keygen")
TOOLS = ("sh", "uname", "sysctl", "curl", "wget", "sha256sum", "shasum", "perl", "mktemp", "rm", "grep", "head", "cut",
         "tar", "gzip", "mkdir", "cp", "chmod", "mv", "ln", "ssh-keygen", "cat", "echo")


def _target() -> str:
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    if platform.system() == "Darwin" and machine == "x86_64":
        got = subprocess.run(["sysctl", "-n", "hw.optional.arm64"], capture_output=True, text=True, check=False)
        machine = "arm64" if got.stdout.strip() == "1" else machine
    return f"{'darwin' if platform.system() == 'Darwin' else 'linux'}-{machine}"


@pytest.fixture
def key(tmp_path):
    k = tmp_path / "keys" / "release"
    k.parent.mkdir()
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return k


def _script(tmp_path: Path, key: Path) -> Path:
    """install.sh with the test's key in place of the release key."""
    s = re.sub(r'^PUBLIC_KEY="[^"\n]*"$', f'PUBLIC_KEY="{(key.parent / "release.pub").read_text().strip()}"',
               SCRIPT.read_text(), count=1, flags=re.MULTILINE)
    out = tmp_path / "install.sh"
    out.write_text(s)
    return out


def _release(d: Path, key: Path, version: str, bad: bool = False) -> None:
    """A release as release.yml makes it: kennet_<version>_<target>.tar.gz, checksums.txt and its signature."""
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True)
    stub = d / "kennet"
    stub.write_text(f'#!/bin/sh\ncase "$1" in skill|__prune) exit 0 ;; esac\necho "kennet {version}"\n')
    stub.chmod(0o755)
    name = f"kennet_{version}_{_target()}.tar.gz"
    with tarfile.open(d / name, "w:gz") as t:
        t.add(stub, arcname="kennet")
    stub.unlink()
    digest = "0" * 64 if bad else hashlib.sha256((d / name).read_bytes()).hexdigest()
    (d / "checksums.txt").write_text(f"{digest}  {name}\n")
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(key), "-n", "kennet-release", str(d / "checksums.txt")],
                   check=True)


def _bare_path(tmp_path: Path) -> str:
    """A PATH with only what install.sh calls: no `claude` on it, whatever this machine has."""
    d = tmp_path / "tools"
    d.mkdir(exist_ok=True)
    for tool in TOOLS:
        if (found := shutil.which(tool)) and not (d / tool).exists():
            (d / tool).symlink_to(found)
    return str(d)


def _install(tmp_path: Path, key: Path, release: Path, path: str | None = None, claude: bool = False):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "PATH": path or _bare_path(tmp_path), "SHELL": "/bin/zsh",
           "KENNET_RELEASE_URL": release.as_uri(), "KENNET_INSTALL_DIR": str(tmp_path / "bin")}
    if claude:
        (home / ".claude").mkdir(exist_ok=True)
    return subprocess.run(["sh", str(_script(tmp_path, key))], env=env, capture_output=True, text=True, timeout=60,
                          check=False)


def _run(exe: Path) -> str:
    return subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout


def test_install_puts_a_version_behind_a_symlink_and_says_the_path_line(tmp_path, key):
    release = tmp_path / "release"
    _release(release, key, "0.1.16")
    got = _install(tmp_path, key, release)
    assert got.returncode == 0, got.stderr
    exe, versions = tmp_path / "bin" / "kennet", tmp_path / "home" / ".local" / "share" / "kennet" / "versions"
    assert exe.is_symlink() and os.readlink(exe) == str(versions / "0.1.16" / "kennet")
    assert _run(exe) == "kennet 0.1.16\n"
    assert (versions.parent / "link").read_text() == f"{exe}\n"
    assert f"Installed kennet 0.1.16 to {exe}" in got.stdout
    assert got.stdout.count("Signature verified (Metaphi AI release key)") == 1
    assert f'export PATH="{tmp_path / "bin"}:$PATH"' in got.stdout and ".zshrc" in got.stdout
    assert got.stdout.splitlines()[-1] == "Next: run `kennet login`"
    assert [p.name for p in exe.parent.iterdir()] == ["kennet"]


def test_running_it_again_upgrades_and_leaves_the_old_version_for_whoever_runs_it(tmp_path, key):
    release = tmp_path / "release"
    _release(release, key, "0.1.16")
    assert _install(tmp_path, key, release).returncode == 0
    versions = tmp_path / "home" / ".local" / "share" / "kennet" / "versions"
    old = (versions / "0.1.16" / "kennet").read_bytes()
    _release(release, key, "0.1.17")
    got = _install(tmp_path, key, release, path=f"{tmp_path / 'bin'}:{_bare_path(tmp_path)}", claude=True)
    assert got.returncode == 0, got.stderr
    assert _run(tmp_path / "bin" / "kennet") == "kennet 0.1.17\n"
    assert (versions / "0.1.16" / "kennet").read_bytes() == old   # a process running 0.1.16 keeps its file
    assert "not on your PATH" not in got.stdout
    assert got.stdout.splitlines()[-1] == "Ask Claude: log me into Kennet"


def test_a_plain_file_install_is_migrated_to_the_symlink(tmp_path, key):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "kennet").write_text("#!/bin/sh\necho old\n")
    release = tmp_path / "release"
    _release(release, key, "0.1.16")
    assert _install(tmp_path, key, release).returncode == 0
    assert (tmp_path / "bin" / "kennet").is_symlink() and _run(tmp_path / "bin" / "kennet") == "kennet 0.1.16\n"


@pytest.mark.parametrize("what", ["checksum", "signature", "no key"])
def test_an_unsigned_or_mismatched_release_is_refused_and_nothing_is_installed(tmp_path, key, what):
    release = tmp_path / "release"
    _release(release, key, "0.1.16", bad=what == "checksum")
    if what == "signature":   # signed by someone else
        other = tmp_path / "other"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(other)], check=True)
        (release / "checksums.txt.sig").unlink()
        subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(other), "-n", "kennet-release",
                        str(release / "checksums.txt")], check=True)
    if what == "no key":
        got = subprocess.run(["sh", str(SCRIPT)], capture_output=True, text=True, check=False, timeout=60,
                             env={"HOME": str(tmp_path), "PATH": _bare_path(tmp_path),
                                  "KENNET_RELEASE_URL": release.as_uri(), "KENNET_INSTALL_DIR": str(tmp_path / "bin")})
        expected = "carries no release key" if 'PUBLIC_KEY=""' in SCRIPT.read_text() else "not signed"
    else:
        got = _install(tmp_path, key, release)
        expected = {"checksum": "does not match checksums.txt", "signature": "not signed by Metaphi AI"}[what]
    assert got.returncode == 1 and expected in got.stderr
    assert ("Signature verified" in got.stdout) == (what == "checksum")   # said only of a signature that held
    assert not (tmp_path / "bin" / "kennet").exists()


def test_install_sh_and_the_cli_carry_the_same_release_key():
    from metaphi import signature
    assert f'PUBLIC_KEY="{signature.PUBLIC_KEY}"' in SCRIPT.read_text()
