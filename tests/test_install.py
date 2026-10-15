"""install.sh against a release directory of its own (KENNET_RELEASE_URL): it installs, upgrades, and refuses a
download that does not match checksums.txt. CI runs it on Linux and macOS."""
import hashlib
import os
import platform
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "install.sh"
pytestmark = pytest.mark.skipif(shutil.which("sh") is None or os.name != "posix", reason="POSIX sh")


def _target() -> str:
    machine = {"aarch64": "arm64", "amd64": "x86_64"}.get(platform.machine().lower(), platform.machine().lower())
    return f"{'darwin' if platform.system() == 'Darwin' else 'linux'}-{machine}"


def _release(d: Path, version: str, bad: bool = False) -> None:
    """A release as release.yml makes it: kennet_<version>_<target>.tar.gz for each platform, and checksums.txt."""
    d.mkdir(parents=True, exist_ok=True)
    for p in d.iterdir():
        p.unlink()
    stub = d / "kennet"
    stub.write_text(f'#!/bin/sh\n[ "$1" = skill ] && exit 0\necho "kennet {version}"\n')
    stub.chmod(0o755)
    lines = []
    for target in sorted({_target(), "linux-x86_64", "darwin-arm64"}):
        name = f"kennet_{version}_{target}.tar.gz"
        with tarfile.open(d / name, "w:gz") as t:
            t.add(stub, arcname="kennet")
        digest = hashlib.sha256((d / name).read_bytes()).hexdigest()
        lines.append(f"{'0' * 64 if bad else digest}  {name}")
    stub.unlink()
    (d / "checksums.txt").write_text("\n".join(lines) + "\n")


TOOLS = ("sh", "uname", "curl", "wget", "sha256sum", "shasum", "perl", "mktemp", "rm", "grep", "head", "cut", "tar",
         "gzip", "mkdir", "cp", "chmod", "mv")


def _bare_path(tmp_path: Path) -> str:
    """A PATH with only what install.sh calls: no `claude` on it, whatever this machine has."""
    d = tmp_path / "tools"
    d.mkdir(exist_ok=True)
    for tool in TOOLS:
        if (found := shutil.which(tool)) and not (d / tool).exists():
            (d / tool).symlink_to(found)
    return str(d)


def _install(tmp_path: Path, release: Path, path: str | None = None, claude: bool = False):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "PATH": path or _bare_path(tmp_path), "SHELL": "/bin/zsh",
           "KENNET_RELEASE_URL": release.as_uri(), "KENNET_INSTALL_DIR": str(tmp_path / "bin")}
    if claude:
        (home / ".claude").mkdir(exist_ok=True)
    return subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_install_puts_the_binary_in_the_directory_and_says_the_path_line(tmp_path):
    release = tmp_path / "release"
    _release(release, "0.1.16")
    got = _install(tmp_path, release)
    assert got.returncode == 0, got.stderr
    exe = tmp_path / "bin" / "kennet"
    assert subprocess.run([str(exe)], capture_output=True, text=True, check=True).stdout == "kennet 0.1.16\n"
    assert f"Installed kennet 0.1.16 to {exe}" in got.stdout
    assert f'export PATH="{tmp_path / "bin"}:$PATH"' in got.stdout and ".zshrc" in got.stdout
    assert got.stdout.splitlines()[-1] == "Next: run `kennet login`"
    assert [p.name for p in exe.parent.iterdir()] == ["kennet"]


def test_running_it_again_upgrades_and_with_claude_code_names_the_ask(tmp_path):
    release = tmp_path / "release"
    _release(release, "0.1.16")
    assert _install(tmp_path, release).returncode == 0
    _release(release, "0.1.17")
    got = _install(tmp_path, release, path=f"{tmp_path / 'bin'}:{_bare_path(tmp_path)}", claude=True)
    assert got.returncode == 0, got.stderr
    assert subprocess.run([str(tmp_path / "bin" / "kennet")], capture_output=True, text=True, check=True).stdout == \
        "kennet 0.1.17\n"
    assert "not on your PATH" not in got.stdout
    assert got.stdout.splitlines()[-1] == "Ask Claude: log me into Kennet"


def test_a_checksum_mismatch_is_refused_and_nothing_is_installed(tmp_path):
    release = tmp_path / "release"
    _release(release, "0.1.16", bad=True)
    got = _install(tmp_path, release)
    assert got.returncode == 1 and "does not match checksums.txt" in got.stderr
    assert not (tmp_path / "bin" / "kennet").exists()
