"""Kennet in the coding agents on this machine: the Kennet skill where each harness reads skills, and Kennet's MCP
server registered with each the way that harness takes one.

    harness   skills (user | project)                  MCP server (user | project)
    claude    $CLAUDE_CONFIG_DIR/skills | .claude/skills `claude mcp add --scope user` | .mcp.json
    codex     ~/.agents/skills | .agents/skills         $CODEX_HOME/config.toml | .codex/config.toml
    hum       $HUM_HOME/skills | .agents/skills         `hum mcp add` | .hum/config.toml
    gemini    ~/.agents/skills | .agents/skills         `gemini mcp add --scope user` | .gemini/settings.json
    copilot   ~/.agents/skills | .agents/skills         `copilot mcp add` | .mcp.json
    cursor    ~/.agents/skills | .agents/skills         ~/.cursor/mcp.json | .cursor/mcp.json
    opencode  ~/.agents/skills | .agents/skills         ~/.config/opencode/opencode.json | opencode.json

A folder several harnesses read (~/.agents/skills, the open standard's; .mcp.json) is written once. A person's own
config goes through the harness's own `mcp add` where it has one that returns without asking anything, since that
file is the harness's to lay out (`codex mcp add` signs in before it returns, so Codex's file is written); a
repository's is the documented file, committed for a team whichever harness each member runs. A harness counts as
present when its command is on the PATH or its config directory exists.

Nothing of someone else's is replaced: a skill file without MARK is left alone, and a server named kennet that this
command did not add is left as it is. What it added is kept in the config file (`agents`), so `remove` takes away
exactly that and nothing more.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metaphi import config, errors, skill

SERVER = "kennet"
MCP_URL = "https://kennet.metaphi.ai/mcp/"
NOT_PRESENT = "not on this machine"


def _home(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


@dataclass(frozen=True)
class Harness:
    name: str                       # what --harness takes
    title: str
    commands: tuple[str, ...]       # its command; any one on the PATH means it is here
    home: Callable[[], Path]        # its config directory, the other sign it is here
    skills: Callable[[], Path]      # the person's skills folder
    project_skills: str             # the repository's, from its root
    add: tuple[str, ...] = ()       # `mcp add` for the person's config, MCP_URL appended where URL stands
    remove: tuple[str, ...] = ()
    get: tuple[str, ...] = ()       # prints the server declared under that name (none: its settings file is read)
    user_file: Callable[[], Path] | None = None   # the person's MCP config, where there is no `mcp add`
    project_file: str = ""          # the repository's MCP config
    sign_in: str | None = None      # the shell command that signs in to the server
    sign_in_here: str = ""          # where it is done inside a session, when no shell command does it
    entry: dict[str, Any] = field(default_factory=dict)   # what a JSON config holds for the server

    def present(self) -> bool:
        return any(shutil.which(c) for c in self.commands) or self.home().is_dir()

    def command(self) -> str | None:
        return next((found for c in self.commands if (found := shutil.which(c))), None)


_AGENTS = lambda: Path.home() / ".agents" / "skills"   # the open standard's folder, read by all but two
URL = "{url}"

# Sources for every path and command: the PR that added this table (docs and source of each harness).
HARNESSES = (
    Harness("claude", "Claude Code", ("claude",), lambda: _home("CLAUDE_CONFIG_DIR", ".claude"),
            lambda: _home("CLAUDE_CONFIG_DIR", ".claude") / "skills", ".claude/skills",
            add=("mcp", "add", "--transport", "http", "--scope", "user", SERVER, URL),
            remove=("mcp", "remove", "--scope", "user", SERVER), get=("mcp", "get", SERVER),
            project_file=".mcp.json", sign_in=f"claude mcp login {SERVER}", entry={"type": "http", "url": URL}),
    Harness("codex", "Codex", ("codex",), lambda: _home("CODEX_HOME", ".codex"), _AGENTS, ".agents/skills",
            # not `codex mcp add`: on a server that takes OAuth it goes on to sign in, and waits on a browser
            user_file=lambda: _home("CODEX_HOME", ".codex") / "config.toml", project_file=".codex/config.toml",
            sign_in=f"codex mcp login {SERVER}"),
    Harness("hum", "Hum", ("hum",), lambda: _home("HUM_HOME", ".hum"), lambda: _home("HUM_HOME", ".hum") / "skills",
            ".agents/skills", add=("mcp", "add", SERVER, URL), remove=("mcp", "remove", SERVER),
            get=("mcp", "get", SERVER), project_file=".hum/config.toml", sign_in=f"hum mcp login {SERVER}"),
    Harness("gemini", "Gemini CLI", ("gemini",), lambda: _home("GEMINI_CLI_HOME", "") / ".gemini", _AGENTS,
            ".agents/skills", add=("mcp", "add", "--scope", "user", "--transport", "http", SERVER, URL),
            remove=("mcp", "remove", "--scope", "user", SERVER), project_file=".gemini/settings.json", sign_in_here=f"/mcp auth {SERVER} in Gemini CLI",
            entry={"httpUrl": URL}),
    Harness("copilot", "GitHub Copilot CLI", ("copilot",), lambda: _home("COPILOT_HOME", ".copilot"), _AGENTS,
            ".agents/skills", add=("mcp", "add", "--transport", "http", SERVER, URL),
            remove=("mcp", "remove", SERVER), get=("mcp", "get", SERVER),
            project_file=".mcp.json", sign_in_here=f"/mcp auth {SERVER} in Copilot CLI", entry={"type": "http", "url": URL}),
    Harness("cursor", "Cursor", ("cursor-agent", "cursor"), lambda: Path.home() / ".cursor", _AGENTS,
            ".agents/skills", user_file=lambda: Path.home() / ".cursor" / "mcp.json", project_file=".cursor/mcp.json",
            sign_in_here=f"Cursor's MCP settings (or `cursor-agent mcp login {SERVER}`)", entry={"url": URL}),
    Harness("opencode", "opencode", ("opencode",), lambda: Path.home() / ".config" / "opencode", _AGENTS,
            ".agents/skills", user_file=lambda: Path.home() / ".config" / "opencode" / "opencode.json",
            project_file="opencode.json", sign_in=f"opencode mcp auth {SERVER}",
            entry={"type": "remote", "url": URL}),
)
NAMES = tuple(h.name for h in HARNESSES)
BY_NAME = {h.name: h for h in HARNESSES}

# The TOML a repository's config holds for the server, whole: `remove` takes exactly this text away again.
_TOML = {
    "codex": f'[mcp_servers.{SERVER}]\nurl = "{URL}"\n',
    "hum": f'[[mcp]]\nname = "{SERVER}"\nurl = "{URL}"\n',
}
_TOML_DECLARES = {
    "codex": re.compile(rf'^\s*\[\s*mcp_servers\s*\.\s*"?{SERVER}"?\s*\]', re.MULTILINE),
    "hum": re.compile(rf'^\s*name\s*=\s*"{SERVER}"\s*$', re.MULTILINE),
}


def chosen(names: list[str] | None) -> list[Harness]:
    """The harnesses `--harness` names, or every one present on this machine."""
    if names:
        return [BY_NAME[n] for n in dict.fromkeys(names)]
    return [h for h in HARNESSES if h.present()]


# --- the skill ---

def skill_dir(h: Harness, project: bool) -> Path:
    return (Path.cwd() / h.project_skills if project else h.skills()) / skill.NAME


def _write_skill(path: Path, force: bool) -> str:
    if path.exists() and skill.MARK not in path.read_text(errors="replace") and not force:
        return "kept"   # someone else's file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill.STUB)
    return "written"


def _remove_skill(path: Path) -> bool:
    if not path.exists() or skill.MARK not in path.read_text(errors="replace"):
        return False
    path.unlink()
    if not any(path.parent.iterdir()):
        path.parent.rmdir()
    return True


def skill_installed() -> bool:
    return any((skill_dir(h, p) / "SKILL.md").exists() for h in HARNESSES for p in (False, True))


# --- the MCP server ---

def _ours() -> list[dict[str, Any]]:
    got = config.load().get("agents")
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def _keep(entries: list[dict[str, Any]]) -> None:
    cfg = config.load()
    if entries:
        cfg["agents"] = entries
    else:
        cfg.pop("agents", None)
    config.save(cfg)


def _target(h: Harness, project: bool) -> str:
    """What one registration is: a file for a repository's config and a harness without `mcp add`, else the
    harness's own store. Two harnesses that read one file share its target, and it is written once."""
    if project:
        return str(Path.cwd() / h.project_file)
    return str(h.user_file()) if h.user_file else f"{h.name}:user"


def _run(cmd: str, args: tuple[str, ...], u: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([cmd, *(u if a == URL else a for a in args)], capture_output=True, text=True,
                          timeout=30, check=False, stdin=subprocess.DEVNULL)


def _said(ran: subprocess.CompletedProcess[str]) -> str:
    """The last line a harness's command said, which is where each puts its reason."""
    lines = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
    return lines[-1].strip() if lines else f"exit {ran.returncode}"


def _json_add(path: Path, key: str, entry: dict[str, Any]) -> str:
    """Add the server to a JSON config under `key`, every other key as it was. `added`, `present` (a server of
    that name is there already) or a refusal in words."""
    try:
        doc = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
    except ValueError:
        return f"{path} is not plain JSON (comments?); add {json.dumps({key: {SERVER: entry}})} to it by hand"
    servers = doc.setdefault(key, {}) if isinstance(doc, dict) else None
    if not isinstance(servers, dict):
        return f"{path} does not hold a JSON object under {key!r}; add the server to it by hand"
    if SERVER in servers:
        return "present"
    servers[SERVER] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n")
    return "added"


def _json_remove(path: Path, key: str, entry: dict[str, Any]) -> bool:
    """Take the server out of a JSON config, only while it is still the entry this command wrote; a file left
    holding nothing is removed."""
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    servers = doc.get(key) if isinstance(doc, dict) else None
    if not isinstance(servers, dict) or servers.get(SERVER) != entry:
        return False
    del servers[SERVER]
    if not servers:
        del doc[key]
    if doc:
        path.write_text(json.dumps(doc, indent=2) + "\n")
    else:
        path.unlink()
    return True


def _toml_add(path: Path, kind: str, u: str) -> str:
    text = path.read_text() if path.exists() else ""
    if _TOML_DECLARES[kind].search(text):
        return "present"
    pad = "" if not text or text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + pad + _TOML[kind].replace(URL, u))
    return "added"


def _toml_remove(path: Path, kind: str, u: str) -> bool:
    block = _TOML[kind].replace(URL, u)
    text = path.read_text() if path.exists() else ""
    if block not in text:
        return False
    kept = text.replace(block, "", 1).rstrip("\n")
    if kept.strip():
        path.write_text(kept + "\n")
    else:
        path.unlink()
    return True


def _file_kind(h: Harness, path: str) -> tuple[str, str]:
    """How a config file holds servers: ("json", its key) or ("toml", whose layout)."""
    if path.endswith(".toml"):
        return "toml", h.name
    return "json", "mcp" if h.name == "opencode" else "mcpServers"


def _entry(h: Harness, u: str) -> dict[str, Any]:
    return {k: (u if v == URL else v) for k, v in h.entry.items()}


def _add_mcp(h: Harness, project: bool, u: str) -> tuple[str, str]:
    """Register the server with one harness: (state, detail). state is `added`, `present` (a server named
    kennet is declared there already, left as it is) or `skipped` (detail says why)."""
    target = _target(h, project)
    if project or h.user_file:
        path = Path(target)
        kind, key = _file_kind(h, target)
        try:
            got = _toml_add(path, key, u) if kind == "toml" else _json_add(path, key, _entry(h, u))
        except OSError as e:
            return "skipped", f"cannot write {path}: {e.strerror or e}"
        return (got, target) if got in ("added", "present") else ("skipped", got)
    cmd = h.command()
    if cmd is None:
        return "skipped", f"`{h.commands[0]}` is not on the PATH; run `kennet agents install` again where it is"
    try:
        if _declared(h, cmd, None):   # Codex's, Gemini's and Copilot's `add` would replace it without a word
            return "present", f"a server named {SERVER} is declared in {h.title} already"
        ran = _run(cmd, h.add, u)
    except (OSError, subprocess.SubprocessError) as e:
        return "skipped", f"`{h.commands[0]} mcp add` did not run: {e}"
    if ran.returncode == 0:
        return "added", f"`{h.commands[0]} mcp add`"
    said = _said(ran)
    if re.search(r"already (exists|names|configured)", said):
        return "present", said
    return "skipped", f"`{h.commands[0]} mcp add` refused: {said}"


def _declared(h: Harness, cmd: str, u: str | None) -> bool:
    """Whether the person's config of a harness declares the server (at `u`, when given). Gemini CLI has no
    `mcp get`: its settings file is read."""
    if not h.get:
        try:
            doc = json.loads((h.home() / "settings.json").read_text())
        except (OSError, ValueError):
            return False
        got = doc.get("mcpServers", {}).get(SERVER) if isinstance(doc, dict) and isinstance(
            doc.get("mcpServers"), dict) else None
        return isinstance(got, dict) and (u is None or u in (got.get("httpUrl"), got.get("url")))
    ran = _run(cmd, h.get, u or "")
    said = ran.stdout + ran.stderr
    # `claude mcp get` exits 0 on a name it does not know: the url in what it printed is the answer
    return ran.returncode == 0 and (u in said if u else "No MCP server named" not in said)


def _remove_mcp(h: Harness, target: str, u: str) -> tuple[bool, str]:
    if target.endswith(":user"):
        cmd = h.command()
        if cmd is None:
            return False, f"`{h.commands[0]}` is not on the PATH"
        try:
            if not _declared(h, cmd, u):
                return False, f"{h.title} no longer declares kennet's server at {u}; left as it is"
            ran = _run(cmd, h.remove, u)
        except (OSError, subprocess.SubprocessError) as e:
            return False, f"`{h.commands[0]} mcp remove` did not run: {e}"
        return ran.returncode == 0, "" if ran.returncode == 0 else _said(ran)
    kind, key = _file_kind(h, target)
    try:
        done = _toml_remove(Path(target), key, u) if kind == "toml" else _json_remove(Path(target), key,
                                                                                       _entry(h, u))
    except OSError as e:
        return False, f"cannot write {target}: {e.strerror or e}"
    return done, "" if done else "changed since kennet wrote it; left as it is"


# --- the two verbs ---

def install(names: list[str] | None = None, project: bool = False, force: bool = False) -> list[dict[str, Any]]:
    """The skill and the server into each harness: one row a harness, saying what was done there.

    skill_state: `written`, or `kept` (a file there kennet did not write; --force replaces it). mcp: `added`,
    `installed` (added by kennet before), `present` (a server named kennet that kennet did not add, left as it
    is) or `skipped` (mcp_detail says why)."""
    harnesses = chosen(names)
    if not harnesses:
        raise errors.MetaphiError("no coding agent found on this machine (looked for " + ", ".join(NAMES) + ")",
                                  code="no_agent_found", next_step="kennet agents install --harness claude")
    u = MCP_URL
    ours = _ours()
    mine = {str(x.get("target")) for x in ours}
    skills: dict[str, str] = {}
    targets: dict[str, tuple[str, str]] = {}
    rows = []
    for h in harnesses:
        path = skill_dir(h, project) / "SKILL.md"
        if str(path) not in skills:
            skills[str(path)] = _write_skill(path, force)
        row: dict[str, Any] = {"harness": h.name, "skill": str(path), "skill_state": skills[str(path)]}
        target = _target(h, project)
        if target not in targets:
            state, detail = _add_mcp(h, project, u)
            if state == "present" and target in mine:
                state = "installed"
            elif state == "added":
                ours.append({"target": target, "url": u})
                mine.add(target)
            targets[target] = (state, detail)
        row["mcp"], row["mcp_detail"] = targets[target]
        if row["mcp"] in ("added", "installed"):
            row["sign_in"] = h.sign_in or h.sign_in_here
        rows.append(row)
    _keep(ours)
    return rows


def remove(names: list[str] | None = None, project: bool = False) -> dict[str, Any]:
    """Take away what `install` put in each harness named (every one, by default), and only that: `removed`, the
    files and registrations taken away, and `left`, what kennet added and could not take away, with why."""
    harnesses = [BY_NAME[n] for n in dict.fromkeys(names)] if names else list(HARNESSES)
    removed: list[str] = []
    for h in harnesses:
        path = skill_dir(h, project) / "SKILL.md"
        if str(path) not in removed and _remove_skill(path):
            removed.append(str(path))
    targets = {_target(h, project): h for h in harnesses}
    keep, left = [], []
    for x in _ours():
        target = str(x.get("target") or "")
        h = targets.get(target)
        if h is None:
            keep.append(x)   # another harness's, or another repository's
            continue
        done, why = _remove_mcp(h, target, str(x.get("url") or MCP_URL))
        if done:
            removed.append(f"{h.name}: mcp server {SERVER}" if target.endswith(":user") else target)
            continue
        if "not on the PATH" in why or "did not run" in why:   # try again where its command runs
            keep.append(x)
        left.append({"harness": h.name, "target": target, "why": why})
    _keep(keep)
    return {"removed": removed, "left": left}


def status(project: bool = False) -> list[dict[str, Any]]:
    """Each harness: whether it is here, whether the skill is in its folder (and kennet's), and whether kennet
    added the server to it. Read from the files alone; no harness is run."""
    mine = {str(x.get("target")) for x in _ours()}
    rows = []
    for h in HARNESSES:
        path = skill_dir(h, project) / "SKILL.md"
        try:
            text = path.read_text(errors="replace") if path.is_file() else None
        except OSError:
            text = None
        rows.append({"harness": h.name, "present": h.present(), "skill": str(path),
                     "skill_state": "absent" if text is None else "written" if skill.MARK in text else "kept",
                     "mcp": "installed" if _target(h, project) in mine else "absent"})
    return rows
