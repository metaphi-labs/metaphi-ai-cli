"""Kennet in the coding agents on this machine: the Kennet skill where each harness reads skills, and Kennet's MCP
server registered with each the way that harness takes one.

    harness   skills (you | repository)               MCP server (you | repository)
    claude    $CLAUDE_CONFIG_DIR/skills | .claude/skills `claude mcp add --scope user` | .mcp.json
    codex     ~/.agents/skills | .agents/skills         $CODEX_HOME/config.toml | .codex/config.toml
    hum       $HUM_HOME/skills | .agents/skills         `hum mcp add` | .hum/config.toml
    gemini    ~/.agents/skills | .agents/skills         `gemini mcp add --scope user` | .gemini/settings.json
    copilot   ~/.agents/skills | .agents/skills         `copilot mcp add` | .mcp.json
    cursor    ~/.agents/skills | .agents/skills         ~/.cursor/mcp.json | .cursor/mcp.json
    opencode  ~/.agents/skills | .agents/skills         $XDG_CONFIG_HOME/opencode/opencode.json[c] | opencode.json[c]

A folder or file several harnesses read (~/.agents/skills, the open standard's; .mcp.json) is written once. A
person's own config goes through the harness's own `mcp add` where it has one that returns without asking anything,
since that file is the harness's to lay out (`codex mcp add` signs in before it returns, so Codex's file is written);
a repository's is the documented file at the repository's root, committed for a team whichever harness each member
runs. A harness counts as present when its command is on the PATH or its config directory exists.

Nothing of anyone else's is replaced or removed. A skill file without MARK is left alone; a server named kennet that
this command did not add is left as it is; a config file that does not parse, or that would not parse with the
server added, is not written, and the row says what to add by hand. What it added is kept in the config file
(`agents`, one row a harness and thing, with the config directory then in effect), so `remove` takes away exactly
that: a file or folder another installed harness still reads stays until the last one is removed, an entry changed
since is left, and a file is deleted only when kennet made it and nothing else is left in it.
"""
from __future__ import annotations

import copy
import json
import os
import re
import secrets
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from metaphi import config, errors, skill

try:
    import tomllib
except ImportError:   # Python 3.10: TOML configs are then not written, and the row says what to add by hand
    tomllib = None

SERVER = "kennet"
MCP_URL = "https://kennet.metaphi.ai/mcp/"
URL = "{url}"   # where the server's url goes in a command or an entry


def _dir(var: str, default: Path) -> Path:
    return Path(os.environ.get(var) or default)


def _claude() -> Path:
    return _dir("CLAUDE_CONFIG_DIR", Path.home() / ".claude")


def _claude_json() -> Path:
    # the person's servers: ~/.claude.json, or .claude.json inside CLAUDE_CONFIG_DIR when that is set
    d = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(d) / ".claude.json" if d else Path.home() / ".claude.json"


def _hum() -> Path:
    return _dir("HUM_HOME", Path.home() / ".hum")


def _gemini() -> Path:
    return _dir("GEMINI_CLI_HOME", Path.home()) / ".gemini"


def _opencode() -> Path:
    return _dir("XDG_CONFIG_HOME", Path.home() / ".config") / "opencode"


def _jsonc_or_json(d: Path, stem: str) -> Path:
    """opencode reads opencode.jsonc or opencode.json: the one there, so no sibling is made beside it."""
    return d / f"{stem}.jsonc" if (d / f"{stem}.jsonc").exists() else d / f"{stem}.json"


@dataclass(frozen=True)
class Harness:
    name: str                       # what --harness takes
    title: str
    commands: tuple[str, ...]       # its command; any one on the PATH means it is here
    home: Callable[[], Path]        # its config directory, the other sign it is here
    skills: Callable[[], Path]      # the person's skills folder
    project_skills: str             # the repository's, from its root
    user_config: Callable[[], Path]  # the file that holds the person's servers
    user_layout: str                # how it holds them: json:<key>, or toml:codex / toml:hum
    project_config: Callable[[Path], Path]   # the repository's, from its root
    project_layout: str
    entry: dict[str, Any] = field(default_factory=dict)   # what a JSON config holds for the server
    add: tuple[str, ...] = ()       # its `mcp add` for the person's config; none: the file is written
    remove: tuple[str, ...] = ()
    overwrites: bool = False        # its `add` replaces a server of that name without a word: the file is read first
    sign_in: str | None = None      # the shell command that signs in to the server
    sign_in_here: str = ""          # where it is done inside a session, when no shell command does it

    def present(self) -> bool:
        return any(shutil.which(c) for c in self.commands) or self.home().is_dir()

    def command(self) -> str | None:
        return next((found for c in self.commands if (found := shutil.which(c))), None)


def _agents() -> Path:
    return Path.home() / ".agents" / "skills"   # the open standard's folder, read by all but Claude Code and Hum


_HTTP = {"type": "http", "url": URL}

# Sources for every path and command: the PR that added this table (each harness's docs and source).
HARNESSES = (
    Harness("claude", "Claude Code", ("claude",), _claude, lambda: _claude() / "skills", ".claude/skills",
            _claude_json, "json:mcpServers", lambda r: r / ".mcp.json", "json:mcpServers", _HTTP,
            add=("mcp", "add", "--transport", "http", "--scope", "user", SERVER, URL),
            remove=("mcp", "remove", "--scope", "user", SERVER), sign_in=f"claude mcp login {SERVER}"),
    # not `codex mcp add`: on a server that takes OAuth it goes on to sign in, and waits on a browser
    Harness("codex", "Codex", ("codex",), lambda: _dir("CODEX_HOME", Path.home() / ".codex"), _agents,
            ".agents/skills", lambda: _dir("CODEX_HOME", Path.home() / ".codex") / "config.toml", "toml:codex",
            lambda r: r / ".codex" / "config.toml", "toml:codex", sign_in=f"codex mcp login {SERVER}"),
    Harness("hum", "Hum", ("hum",), _hum, lambda: _hum() / "skills", ".agents/skills",
            lambda: _dir("HUM_CONFIG", _hum() / "config.toml"), "toml:hum", lambda r: r / ".hum" / "config.toml",
            "toml:hum", add=("mcp", "add", SERVER, URL), remove=("mcp", "remove", SERVER),
            sign_in=f"hum mcp login {SERVER}"),
    Harness("gemini", "Gemini CLI", ("gemini",), _gemini, _agents, ".agents/skills",
            lambda: _gemini() / "settings.json", "json:mcpServers", lambda r: r / ".gemini" / "settings.json",
            "json:mcpServers", {"httpUrl": URL},
            add=("mcp", "add", "--scope", "user", "--transport", "http", SERVER, URL),
            remove=("mcp", "remove", "--scope", "user", SERVER), overwrites=True,
            sign_in_here=f"/mcp auth {SERVER} in Gemini CLI"),
    Harness("copilot", "GitHub Copilot CLI", ("copilot",), lambda: _dir("COPILOT_HOME", Path.home() / ".copilot"),
            _agents, ".agents/skills", lambda: _dir("COPILOT_HOME", Path.home() / ".copilot") / "mcp-config.json",
            "json:mcpServers", lambda r: r / ".mcp.json", "json:mcpServers", _HTTP,
            add=("mcp", "add", "--transport", "http", SERVER, URL), remove=("mcp", "remove", SERVER),
            overwrites=True, sign_in_here=f"/mcp auth {SERVER} in Copilot CLI"),
    Harness("cursor", "Cursor", ("cursor-agent", "cursor"), lambda: Path.home() / ".cursor", _agents,
            ".agents/skills", lambda: Path.home() / ".cursor" / "mcp.json", "json:mcpServers",
            lambda r: r / ".cursor" / "mcp.json", "json:mcpServers", {"url": URL},
            sign_in_here=f"Cursor's MCP settings (or `cursor-agent mcp login {SERVER}`)"),
    Harness("opencode", "opencode", ("opencode",), _opencode, _agents, ".agents/skills",
            lambda: _dir("OPENCODE_CONFIG", _jsonc_or_json(_opencode(), "opencode")), "json:mcp",
            lambda r: _jsonc_or_json(r, "opencode"), "json:mcp", {"type": "remote", "url": URL},
            sign_in=f"opencode mcp auth {SERVER}"),
)
NAMES = tuple(h.name for h in HARNESSES)
BY_NAME = {h.name: h for h in HARNESSES}

# The TOML each layout appends for the server, whole, and what it reads back as.
_TOML = {"codex": f'[mcp_servers.{SERVER}]\nurl = "{URL}"\n', "hum": f'[[mcp]]\nname = "{SERVER}"\nurl = "{URL}"\n'}


def chosen(names: list[str] | None) -> list[Harness]:
    """The harnesses `--harness` names, or every one present on this machine."""
    if names:
        return [BY_NAME[n] for n in dict.fromkeys(names)]
    return [h for h in HARNESSES if h.present()]


def project_root() -> Path:
    """The repository this directory is in: its root, which is where each harness looks. Your home directory is
    never one, even when it is under git."""
    here = Path.cwd().resolve()
    home = Path.home().resolve()
    for d in (here, *here.parents):
        if d == home:
            break
        if (d / ".git").exists():
            return d
    raise errors.MetaphiError("--project writes a repository's files: run it inside a repository (not your home "
                              "directory)", code="not_a_repository")


# --- files: read whole, written atomically, never one that would not parse afterwards ---

class _Unreadable(Exception):
    """A config that cannot be read as its layout: it is left as it is, and the reason said."""


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        raise _Unreadable(f"{path} is not UTF-8 text") from None


def _write(path: Path, text: str) -> None:
    """Replace a file in one rename, keeping its mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else None
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _indent(text: str) -> str | int:
    """The indent a JSON file is laid out with, so a rewrite keeps it."""
    m = re.search(r"^([ \t]+)\S", text, re.MULTILINE)
    return m.group(1) if m else 2


def _json(text: str | None, path: Path) -> dict[str, Any]:
    if text is None or not text.strip():
        return {}
    try:
        doc = json.loads(text)
    except ValueError:
        raise _Unreadable(f"{path} is not plain JSON (comments?)") from None
    if not isinstance(doc, dict):
        raise _Unreadable(f"{path} does not hold a JSON object")
    return doc


def _toml(text: str | None, path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise _Unreadable(f"Python 3.10 cannot read TOML, so {path} is not changed")
    try:
        return tomllib.loads(text or "")
    except ValueError:
        raise _Unreadable(f"{path} is not valid TOML") from None


def _container(layout: str) -> str:
    """The key a config keeps its servers under."""
    return {"toml:codex": "mcp_servers", "toml:hum": "mcp"}.get(layout) or layout.split(":")[1]


def _servers(doc: dict[str, Any], layout: str) -> dict[str, Any]:
    """The servers a parsed config declares, by name. Hum's are a list of tables, each with its name."""
    got = doc.get(_container(layout))
    if layout == "toml:hum":
        return {m["name"]: m for m in got or [] if isinstance(m, dict) and isinstance(m.get("name"), str)}
    return got if isinstance(got, dict) else {}


def _without(doc: dict[str, Any], layout: str) -> dict[str, Any]:
    """The parsed config with the server taken out, and a container it leaves empty with it."""
    doc = copy.deepcopy(doc)
    name = _container(layout)
    if layout == "toml:hum" and isinstance(doc.get(name), list):
        doc[name] = [m for m in doc[name] if not (isinstance(m, dict) and m.get("name") == SERVER)]
    elif isinstance(doc.get(name), dict):
        doc[name].pop(SERVER, None)
    if name in doc and not doc[name]:
        del doc[name]
    return doc


def _entry(h: Harness, layout: str, u: str) -> dict[str, Any]:
    """What the config holds for the server once kennet has added it."""
    if layout == "toml:codex":
        return {"url": u}
    if layout == "toml:hum":
        return {"name": SERVER, "url": u}
    return {k: (u if v == URL else v) for k, v in h.entry.items()}


def declared(path: Path, layout: str) -> dict[str, Any] | None:
    """The server as a config declares it, or None; _Unreadable when the file cannot be read as its layout."""
    text = _read(path)
    doc = _toml(text, path) if layout.startswith("toml") else _json(text, path)
    got = _servers(doc, layout).get(SERVER)
    return None if got is None else got if isinstance(got, dict) else {}


def _file_add(path: Path, layout: str, entry: dict[str, Any]) -> tuple[str, bool]:
    """Add the server to a config file, every other line or key as it was: (`added` or `present`, made the file)."""
    text = _read(path)
    made = text is None
    if layout.startswith("toml"):
        doc = _toml(text, path)
        if SERVER in _servers(doc, layout):
            return "present", False
        old = text or ""
        pad = "" if not old or old.endswith("\n\n") else ("\n" if old.endswith("\n") else "\n\n")
        new = old + pad + _TOML[layout.split(":")[1]].replace(URL, entry["url"])
        after = _toml(new, path)
        # what the file reads as afterwards has to be what it was, with the server: else the text would land
        # inside a table of the person's, or beside an inline one TOML forbids adding to
        if _servers(after, layout).get(SERVER) != entry or _without(after, layout) != _without(doc, layout):
            raise _Unreadable(f"{path} declares its servers in a shape kennet does not add to")
        _write(path, new)
        return "added", made
    doc = _json(text, path)
    key = _container(layout)
    servers = doc.setdefault(key, {})
    if not isinstance(servers, dict):
        raise _Unreadable(f"{path} does not hold a JSON object under {key!r}")
    if SERVER in servers:
        return "present", False
    servers[SERVER] = entry
    _write(path, json.dumps(doc, indent=_indent(text or ""), ensure_ascii=False) + "\n")
    return "added", made


def _file_remove(path: Path, layout: str, entry: dict[str, Any], made: bool) -> str:
    """Take the server out of a config file while it is still exactly what kennet wrote: "" when done, else why
    not. The file goes only when kennet made it and nothing else is left in it."""
    text = _read(path)
    if text is None:
        return ""
    if layout.startswith("toml"):
        doc = _toml(text, path)
        if _servers(doc, layout).get(SERVER) != entry:
            return f"{path} declares {SERVER} differently from what kennet wrote; left as it is"
        block = _TOML[layout.split(":")[1]].replace(URL, entry["url"])
        if block not in text:
            return f"{path} declares {SERVER} in other words than kennet's; left as it is"
        i = text.index(block)
        before, after = text[:i], text[i + len(block):]
        if before.endswith("\n\n") and (not after or after.startswith("\n")):
            before = before[:-1]   # the blank line kennet put before it
        new = before + after
        new = new.rstrip("\n") + "\n" if new.strip() else ""
        if _toml(new, path) != _without(doc, layout):
            return f"{path} holds more under {SERVER}'s table than kennet wrote; left as it is"
    else:
        doc = _json(text, path)
        key = _container(layout)
        if not isinstance(doc.get(key), dict) or doc[key].get(SERVER) != entry:
            return f"{path} declares {SERVER} differently from what kennet wrote; left as it is"
        left = _without(doc, layout)
        new = json.dumps(left, indent=_indent(text), ensure_ascii=False) + "\n" if left or not made else ""
    if not new and made:
        path.unlink()
    else:
        _write(path, new)
    return ""


# --- the ledger: what kennet added, one row a harness and thing ---

def _ledger() -> list[dict[str, Any]]:
    got = config.load().get("agents")
    return [x for x in got if isinstance(x, dict) and x.get("harness") in BY_NAME] if isinstance(got, list) else []


def _save(rows: list[dict[str, Any]]) -> None:
    cfg = config.load()
    if rows:
        cfg["agents"] = rows
    else:
        cfg.pop("agents", None)
    config.save(cfg)


def _note(rows: list[dict[str, Any]], row: dict[str, Any]) -> None:
    if not any(x["harness"] == row["harness"] and x["kind"] == row["kind"] and x["target"] == row["target"]
               for x in rows):
        rows.append(row)


# --- what each harness gets ---

def skill_path(h: Harness, root: Path | None) -> Path:
    return (root / h.project_skills if root else h.skills()) / skill.NAME / "SKILL.md"


def _write_skill(path: Path, force: bool) -> str:
    text = _read(path) if path.exists() else None
    if text is not None and skill.MARK not in text and not force:
        return "kept"   # someone else's file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(skill.STUB, encoding="utf-8")
    return "written"


def _remove_skill(path: Path) -> bool:
    try:
        text = _read(path)
    except _Unreadable:
        return False
    if text is None or skill.MARK not in text:
        return False
    path.unlink()
    if not any(path.parent.iterdir()):
        path.parent.rmdir()
    return True


def skill_installed() -> bool:
    return any(skill_path(h, None).exists() for h in HARNESSES)


def _mcp_target(h: Harness, root: Path | None) -> tuple[str, str]:
    """(target, via): the file a registration is in, or for one made through the harness's own command, the
    harness and the config it wrote. Two harnesses that read one file share its target."""
    if root:
        return str(h.project_config(root)), h.project_layout
    if not h.add:
        return str(h.user_config()), h.user_layout
    return f"{h.name}:{h.user_config()}", "cli"


def _run(cmd: str, args: tuple[str, ...], u: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([cmd, *(u if a == URL else a for a in args)], capture_output=True, text=True,
                          timeout=30, check=False, stdin=subprocess.DEVNULL)


def _said(ran: subprocess.CompletedProcess[str]) -> str:
    """The last line a harness's command said, which is where each puts its reason."""
    lines = (ran.stderr.strip() or ran.stdout.strip()).splitlines()
    return lines[-1].strip() if lines else f"exit {ran.returncode}"


def _by_hand(h: Harness, layout: str, u: str) -> str:
    """The entry, for a person to add to a file kennet left alone."""
    if layout.startswith("toml"):
        return _TOML[layout.split(":")[1]].replace(URL, u).strip().replace("\n", " ")
    return json.dumps({_container(layout): {SERVER: _entry(h, layout, u)}})


def _add_mcp(h: Harness, root: Path | None, u: str) -> tuple[str, str, bool]:
    """Register the server with one harness: (state, detail, made a file). state is `added`, `present` (a
    server named kennet is declared there already, left as it is) or `skipped` (detail says why)."""
    target, via = _mcp_target(h, root)
    if via != "cli":
        path = Path(target)
        try:
            state, made = _file_add(path, via, _entry(h, via, u))
        except _Unreadable as e:
            return "skipped", f"{e}; add {_by_hand(h, via, u)} to it by hand", False
        except OSError as e:
            return "skipped", f"cannot write {path}: {e.strerror or e}", False
        return state, target, made
    cmd = h.command()
    if cmd is None:
        return "skipped", f"`{h.commands[0]}` is not on the PATH; run `kennet agents install` again where it is", False
    if h.overwrites:   # its `add` would replace a server of that name: the person's own file says whether there is one
        try:
            if declared(h.user_config(), h.user_layout) is not None:
                return "present", f"{h.user_config()} declares {SERVER} already", False
        except _Unreadable as e:
            return "skipped", (f"{e}, so whether it declares {SERVER} cannot be told; add "
                               f"{_by_hand(h, h.user_layout, u)} to it by hand"), False
        except OSError as e:
            return "skipped", f"cannot read {h.user_config()}: {e.strerror or e}", False
    try:
        ran = _run(cmd, h.add, u)
    except (OSError, subprocess.SubprocessError) as e:
        return "skipped", f"`{h.commands[0]} mcp add` did not run: {e}", False
    if ran.returncode == 0:
        return "added", f"`{h.commands[0]} mcp add`", False
    said = _said(ran)
    if re.search(r"already (exists|names|configured)", said):   # Claude Code's and Hum's, on the person's own config
        return "present", said, False
    return "skipped", f"`{h.commands[0]} mcp add` refused: {said}", False


def _remove_mcp(h: Harness, row: dict[str, Any]) -> tuple[str, bool]:
    """Take one registration away: (why not, or "" when done; whether to try again later)."""
    target, via, u = str(row["target"]), str(row.get("via") or "cli"), str(row.get("url") or MCP_URL)
    if via != "cli":
        try:
            return _file_remove(Path(target), via, _entry(h, via, u), bool(row.get("made"))), False
        except _Unreadable as e:
            return f"{e}; left as it is", False
        except OSError as e:
            return f"cannot write {target}: {e.strerror or e}", True
    cmd = h.command()
    if cmd is None:
        return f"`{h.commands[0]}` is not on the PATH", True
    try:
        got = declared(h.user_config(), h.user_layout)
    except _Unreadable as e:
        return f"{e}; left as it is", False
    except OSError as e:
        return f"cannot read {h.user_config()}: {e.strerror or e}", True
    if got is None:
        return "", False   # gone already
    if u not in (got.get("url"), got.get("httpUrl")):
        return f"{h.title}'s {SERVER} is no longer at {u}; left as it is", False
    try:
        ran = _run(cmd, h.remove, u)
    except (OSError, subprocess.SubprocessError) as e:
        return f"`{h.commands[0]} mcp remove` did not run: {e}", True
    return ("", False) if ran.returncode == 0 else (_said(ran), False)


# --- the verbs ---

def install(names: list[str] | None = None, project: bool = False, force: bool = False) -> list[dict[str, Any]]:
    """The skill and the server into each harness: one row a harness, saying what was done there.

    skill_state: `written`, `kept` (a file there kennet did not write; --force replaces it) or `skipped`. mcp:
    `added`, `installed` (added by kennet before), `present` (a server named kennet that kennet did not add,
    left as it is) or `skipped` (mcp_detail says why). Raises `nothing_installed` when no harness got either."""
    root = project_root() if project else None
    harnesses = chosen(names)
    if not harnesses:
        raise errors.MetaphiError("no coding agent found on this machine (looked for " + ", ".join(NAMES) + ")",
                                  code="no_agent_found", next_step="kennet agents install --harness claude")
    u = MCP_URL
    ledger = _ledger()
    skills: dict[str, str] = {}
    targets: dict[str, tuple[str, str, bool]] = {}
    rows = []
    for h in harnesses:
        path = skill_path(h, root)
        if str(path) not in skills:
            try:
                skills[str(path)] = _write_skill(path, force)
            except _Unreadable:
                skills[str(path)] = "kept"   # not text kennet wrote, so not kennet's
            except OSError as e:
                skills[str(path)] = f"skipped: cannot write {path}: {e.strerror or e}"
        state = skills[str(path)]
        row: dict[str, Any] = {"harness": h.name, "skill": str(path), "skill_state": state.split(":")[0]}
        if state.startswith("skipped"):
            row["skill_detail"] = state.split(": ", 1)[1]
        if row["skill_state"] == "written":
            _note(ledger, {"harness": h.name, "kind": "skill", "target": str(path)})
        target, via = _mcp_target(h, root)
        if target not in targets:
            state, detail, made = _add_mcp(h, root, u)
            if state == "present" and any(x["kind"] == "mcp" and x["target"] == target for x in ledger):
                state = "installed"
            targets[target] = (state, detail, made)
        state, detail, made = targets[target]
        row["mcp"], row["mcp_detail"] = state, detail
        if state in ("added", "installed"):
            # a shared file is each reader's: the first one's row says whether kennet made it
            first = next((x for x in ledger if x["kind"] == "mcp" and x["target"] == target), None)
            _note(ledger, {"harness": h.name, "kind": "mcp", "target": target, "via": via, "url": u,
                           "made": first.get("made", False) if first else made})
            row["sign_in"] = h.sign_in or h.sign_in_here
        rows.append(row)
    _save(ledger)
    if not any(r["skill_state"] == "written" or r["mcp"] in ("added", "installed") for r in rows):
        e = errors.MetaphiError("nothing was installed: " + "; ".join(
            f"{r['harness']}: skill {r['skill_state']}, server {r['mcp']} ({r['mcp_detail']})" for r in rows),
            code="nothing_installed")
        e.context = {"harnesses": rows}
        raise e
    return rows


def _in_scope(row: dict[str, Any], h: Harness, root: Path | None) -> bool:
    """Whether a ledger row is of this scope, as things are now: the person's config in effect, or this
    repository's."""
    if row["kind"] == "skill":
        return row["target"] == str(skill_path(h, root))
    return row["target"] == _mcp_target(h, root)[0]


def remove(names: list[str] | None = None, project: bool = False) -> dict[str, Any]:
    """Take away what `install` put in each harness named (every one, by default), and only that.

    `removed`: what was taken away. `left`: what kennet added and did not take away, and why; a file or folder
    another installed harness still reads is left until that one goes too."""
    root = project_root() if project else None
    harnesses = [BY_NAME[n] for n in dict.fromkeys(names)] if names else list(HARNESSES)
    ledger = _ledger()
    mine = [x for x in ledger if (h := BY_NAME[x["harness"]]) in harnesses and _in_scope(x, h, root)]
    others = [x for x in ledger if x not in mine]
    removed: list[str] = []
    left: list[dict[str, str]] = []
    keep: list[dict[str, Any]] = []
    # a skill file of an earlier kennet, from before the ledger, is the chosen harnesses' to remove
    legacy = [{"harness": h.name, "kind": "skill", "target": str(skill_path(h, root))} for h in harnesses]
    done: set[tuple[str, str]] = set()
    for x in mine + [x for x in legacy if not any(y["target"] == x["target"] for y in ledger)]:
        key = (x["kind"], x["target"])
        if key in done:
            continue
        done.add(key)
        users = sorted({y["harness"] for y in others if (y["kind"], y["target"]) == key})
        if users:
            left.append({"harness": x["harness"], "target": x["target"], "why": f"still read by {', '.join(users)}"})
            continue
        if x["kind"] == "skill":
            if _remove_skill(Path(x["target"])):
                removed.append(x["target"])
            continue
        why, again = _remove_mcp(BY_NAME[x["harness"]], x)
        if not why:
            removed.append(x["target"] if x.get("via") != "cli" else f"{x['harness']}: mcp server {SERVER}")
            continue
        left.append({"harness": x["harness"], "target": x["target"], "why": why})
        if again:
            keep += [y for y in mine if (y["kind"], y["target"]) == key]
    _save(others + keep)
    return {"removed": removed, "left": left}


def status(project: bool = False) -> list[dict[str, Any]]:
    """Each harness: whether it is here, and for one that is (or that kennet installed into), whether the skill is
    in its folder and kennet's, and whether kennet added the server to it. Read from the files alone; no harness
    is run."""
    root = project_root() if project else None
    ledger = _ledger()
    rows = []
    for h in HARNESSES:
        ours = [x for x in ledger if x["harness"] == h.name and _in_scope(x, h, root)]
        row: dict[str, Any] = {"harness": h.name, "present": h.present()}
        if row["present"] or ours:
            path = skill_path(h, root)
            try:
                text = _read(path)
            except (_Unreadable, OSError):
                text = ""
            row["skill"] = str(path)
            row["skill_state"] = "absent" if text is None else "written" if skill.MARK in text else "kept"
            row["mcp"] = "installed" if any(x["kind"] == "mcp" for x in ours) else "absent"
        rows.append(row)
    return rows
