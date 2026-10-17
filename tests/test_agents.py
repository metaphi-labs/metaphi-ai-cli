"""`kennet agents`: the skill and the MCP server into each harness here, against stand-ins for the harnesses' own
commands. Each stand-in keeps the person's servers in the file the real one does, and does on a name it has already
what the real one does: Claude Code and Hum refuse it, Gemini CLI and Copilot CLI replace it."""
import json
import sys
from pathlib import Path

import pytest

from metaphi import agents, cli, skill

U = agents.MCP_URL
FAKE = """#!/bin/sh
PATH=/bin:/usr/bin
n=$(basename "$0")
for last; do :; done
echo "$n $*" >> "$FAKE_STATE/log"
case $n in
  claude) f="${CLAUDE_CONFIG_DIR:+$CLAUDE_CONFIG_DIR/.claude.json}"; f="${f:-$HOME/.claude.json}"
          body='{"mcpServers": {"kennet": {"type": "http", "url": "'"$last"'"}}}' ;;
  hum) f="$HOME/.hum/config.toml"; body='[[mcp]]
name = "kennet"
url = "'"$last"'"' ;;
  gemini) f="$HOME/.gemini/settings.json"; body='{"mcpServers": {"kennet": {"url": "'"$last"'", "type": "http"}}}' ;;
  copilot) f="$HOME/.copilot/mcp-config.json"; body='{"mcpServers": {"kennet": {"type": "http", "url": "'"$last"'"}}}' ;;
  *) exit 0 ;;
esac
case "$1 $2" in
"mcp add")
  if [ -f "$f" ] && [ "$n" = claude -o "$n" = hum ]; then
    echo "MCP server kennet already exists in user config" >&2; exit 1
  fi
  mkdir -p "$(dirname "$f")"; printf '%s\\n' "$body" > "$f" ;;
"mcp get") [ -f "$f" ] || [ -f "$FAKE_STATE/$n.project" ] ;;
"mcp remove") rm "$f" ;;
esac
"""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HUM_HOME", "HUM_CONFIG", "GEMINI_CLI_HOME", "COPILOT_HOME",
                "XDG_CONFIG_HOME", "OPENCODE_CONFIG"):
        monkeypatch.delenv(var, raising=False)
    for d in ("home", "repo/.git", "state", "bin"):
        (tmp_path / d).mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.setenv("FAKE_STATE", str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path / "repo")
    return tmp_path


def _harness(home: Path, *names: str) -> None:
    for n in names:
        (home / "bin" / n).write_text(FAKE)
        (home / "bin" / n).chmod(0o755)


def _run(capsys, *argv: str) -> tuple[int, dict]:
    got = cli.main(["agents", *argv, "--json"])
    out = capsys.readouterr().out
    return got, json.loads(out) if out.strip() else {}


def _rows(got: dict) -> dict:
    return {r["harness"]: r for r in got["harnesses"]}


def _adds(home: Path) -> list[str]:
    log = home / "state" / "log"
    return [x for x in log.read_text().splitlines() if " mcp add " in x] if log.exists() else []


def test_install_writes_each_shared_folder_once_and_registers_the_server_with_each_harness(home, capsys):
    _harness(home, "claude", "codex", "hum")
    (home / "home" / ".cursor").mkdir()   # Cursor, by its folder alone: it has no `mcp add`, so its file is written
    code, got = _run(capsys, "install")
    rows, h = _rows(got), home / "home"
    assert code == 0 and set(rows) == {"claude", "codex", "hum", "cursor"}
    assert rows["claude"]["skill"] == str(h / ".claude/skills/kennet/SKILL.md")
    assert rows["hum"]["skill"] == str(h / ".hum/skills/kennet/SKILL.md")
    assert rows["codex"]["skill"] == rows["cursor"]["skill"] == str(h / ".agents/skills/kennet/SKILL.md")
    for r in rows.values():
        assert Path(r["skill"]).read_text() == skill.STUB and r["mcp"] == "added", r
    assert json.loads((h / ".claude.json").read_text())["mcpServers"]["kennet"]["url"] == U
    assert f'url = "{U}"' in (h / ".hum/config.toml").read_text()
    assert (h / ".codex/config.toml").read_text() == f'[mcp_servers.kennet]\nurl = "{U}"\n'
    assert json.loads((h / ".cursor/mcp.json").read_text()) == {"mcpServers": {"kennet": {"url": U}}}
    assert rows["codex"]["sign_in"] == "codex mcp login kennet" and rows["hum"]["sign_in"] == "hum mcp login kennet"
    assert got["next_step"] == "claude mcp login kennet"   # the first sign-in that is a command

    code, got = _run(capsys, "install")   # again: nothing changes, and what kennet added before is its own
    assert code == 0 and {r["mcp"] for r in got["harnesses"]} == {"installed"} and len(_adds(home)) == 4
    ledger = json.loads((home / "cfg" / "config.json").read_text())["agents"]
    assert len(ledger) == 8   # a skill and a server a harness, never twice

    code, got = _run(capsys, "remove")
    assert code == 0 and got["left"] == []
    assert not (h / ".agents/skills/kennet").exists() and not (h / ".claude/skills/kennet").exists()
    assert not any((h / p).exists() for p in (".cursor/mcp.json", ".codex/config.toml", ".claude.json",
                                              ".hum/config.toml"))
    assert "agents" not in json.loads((home / "cfg" / "config.json").read_text())


def test_a_server_or_skill_kennet_did_not_write_is_left_as_it_is(home, capsys):
    _harness(home, "claude", "codex")
    (home / "home/.claude.json").write_text('{"mcpServers": {"kennet": {"type": "http", "url": "https://else/"}}}')
    codex = home / "home/.codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers."kennet"]\nurl = "https://else/"\n')
    mine = home / "home/.agents/skills/kennet/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")
    code, got = _run(capsys, "install")
    rows = _rows(got)
    assert code == 0 and rows["claude"]["mcp"] == rows["codex"]["mcp"] == "present"
    assert "sign_in" not in rows["codex"] and rows["codex"]["skill_state"] == "kept" and mine.read_text() == "mine"
    code, got = _run(capsys, "remove")
    assert code == 0 and mine.read_text() == "mine" and "else" in codex.read_text()
    assert "else" in (home / "home/.claude.json").read_text()
    _run(capsys, "install", "--harness", "codex", "--force")
    assert mine.read_text() == skill.STUB


def test_toml_that_would_not_parse_with_the_server_added_is_never_written(home, capsys):
    (home / "home/.codex").mkdir()
    codex = home / "home/.codex/config.toml"
    codex.write_text('mcp_servers = { other = { url = "https://o/" } }\n')   # inline: TOML forbids adding a table
    hum = home / "repo/.hum/config.toml"
    hum.parent.mkdir()
    hum.write_text('mcp = [{ name = "other", url = "https://o/" }]\n')       # an inline array, likewise
    _, got = _run(capsys, "install", "--harness", "codex")
    (row,) = got["harnesses"]
    assert row["mcp"] == "skipped" and "by hand" in row["mcp_detail"]
    assert codex.read_text() == 'mcp_servers = { other = { url = "https://o/" } }\n'
    _, got = _run(capsys, "install", "--harness", "hum", "--project")
    assert _rows(got)["hum"]["mcp"] == "skipped" and hum.read_text().startswith("mcp = [{")


def test_remove_leaves_a_table_the_person_added_to_and_keeps_tables_after_it(home, capsys):
    (home / "home/.codex").mkdir()
    codex = home / "home/.codex/config.toml"
    codex.write_text('model = "o3"\n')
    _run(capsys, "install", "--harness", "codex")
    codex.write_text(codex.read_text() + 'bearer_token_env_var = "T"\n')   # the person's key, in kennet's table
    _, got = _run(capsys, "remove")
    assert got["left"] and "differently" in got["left"][0]["why"] and 'bearer_token_env_var = "T"' in codex.read_text()
    assert codex.read_text().count("[mcp_servers.kennet]") == 1

    codex.write_text('model = "o3"\n')
    _run(capsys, "install", "--harness", "codex")
    codex.write_text(codex.read_text() + '\n[mcp_servers.other]\nurl = "https://o/"\n')
    _, got = _run(capsys, "remove")
    assert got["left"] == [] and codex.read_text() == 'model = "o3"\n\n[mcp_servers.other]\nurl = "https://o/"\n'


def test_a_project_server_does_not_stop_the_persons_own_being_added(home, capsys):
    """`claude mcp get` and `hum mcp get` see a repository's .mcp.json too: only the person's own config counts."""
    _harness(home, "claude", "hum")
    for n in ("claude", "hum"):
        (home / "state" / f"{n}.project").write_text("")
    (home / "repo/.mcp.json").write_text('{"mcpServers": {"kennet": {"type": "http", "url": "https://p/"}}}')
    code, got = _run(capsys, "install")
    assert code == 0 and {r["mcp"] for r in got["harnesses"]} == {"added"}


def test_remove_keeps_what_another_installed_harness_still_reads(home, capsys):
    _harness(home, "codex")
    (home / "home/.cursor").mkdir()
    _run(capsys, "install")
    shared = home / "home/.agents/skills/kennet/SKILL.md"
    _, got = _run(capsys, "remove", "--harness", "codex")
    assert shared.is_file() and got["left"][0]["why"] == "still read by cursor"
    assert not (home / "home/.codex/config.toml").exists()
    _run(capsys, "remove", "--harness", "cursor")
    assert not shared.exists()

    _run(capsys, "install", "--project", "--harness", "claude", "--harness", "copilot")
    mcp = home / "repo/.mcp.json"
    _run(capsys, "remove", "--project", "--harness", "claude")
    assert json.loads(mcp.read_text())["mcpServers"]["kennet"]["url"] == U   # Copilot CLI reads it too
    _run(capsys, "remove", "--project", "--harness", "copilot")
    assert not mcp.exists()


def test_project_writes_the_repositorys_root_and_only_inside_a_repository(home, capsys, monkeypatch):
    repo = home / "repo"
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text('model = "o3"\n')
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://o/"}}}))
    (repo / "src/deep").mkdir(parents=True)
    monkeypatch.chdir(repo / "src/deep")
    code, got = _run(capsys, "install", "--project", *(f"--harness={n}" for n in agents.NAMES))
    assert code == 0 and {r["mcp"] for r in got["harnesses"]} == {"added"}
    assert {r["skill"] for r in got["harnesses"]} == {str(repo / ".claude/skills/kennet/SKILL.md"),
                                                       str(repo / ".agents/skills/kennet/SKILL.md")}
    assert json.loads((repo / ".mcp.json").read_text())["mcpServers"]["kennet"] == {"type": "http", "url": U}
    assert (repo / ".codex/config.toml").read_text() == f'model = "o3"\n\n[mcp_servers.kennet]\nurl = "{U}"\n'
    assert 'name = "kennet"' in (repo / ".hum/config.toml").read_text()
    assert json.loads((repo / ".gemini/settings.json").read_text()) == {"mcpServers": {"kennet": {"httpUrl": U}}}
    assert json.loads((repo / "opencode.json").read_text()) == {"mcp": {"kennet": {"type": "remote", "url": U}}}
    code, got = _run(capsys, "remove", "--project")
    assert code == 0 and got["left"] == []
    assert (repo / ".codex/config.toml").read_text() == 'model = "o3"\n'
    assert json.loads((repo / ".mcp.json").read_text()) == {"mcpServers": {"other": {"type": "http",
                                                                                     "url": "https://o/"}}}
    assert not any((repo / p).exists() for p in (".hum/config.toml", ".gemini/settings.json", "opencode.json",
                                                 ".cursor/mcp.json", ".agents/skills/kennet", ".claude/skills/kennet"))

    monkeypatch.chdir(home)
    code, got = _run(capsys, "install", "--project", "--harness", "codex")
    assert code == cli.FAILED and got["error"]["code"] == "not_a_repository"
    (home / "home/.git").mkdir()   # a home directory under git is still not a project
    monkeypatch.chdir(home / "home")
    code, got = _run(capsys, "install", "--project", "--harness", "codex")
    assert got["error"]["code"] == "not_a_repository" and not (home / "home/.agents").exists()


def test_nothing_installed_fails_and_no_harness_is_said(home, capsys):
    code, got = _run(capsys, "install")
    assert code == cli.FAILED and got["error"]["code"] == "no_agent_found"
    code, got = _run(capsys, "install", "--harness", "hum")
    (row,) = got["harnesses"]
    assert code == 0 and row["mcp"] == "skipped" and "not on the PATH" in row["mcp_detail"]
    assert Path(row["skill"]).is_file()   # the skill needs no command: it is a file the harness reads
    skills = home / "home/.hum/skills/kennet/SKILL.md"
    skills.write_text("mine")
    code, got = _run(capsys, "install", "--harness", "hum")
    assert code == cli.FAILED and got["error"]["code"] == "nothing_installed"
    assert got["harnesses"][0]["skill_state"] == "kept"


def test_opencode_writes_the_config_in_effect(home, capsys, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
    (home / "xdg/opencode").mkdir(parents=True)
    jsonc = home / "xdg/opencode/opencode.jsonc"
    jsonc.write_text('{\n    "theme": "dark"\n}\n')
    code, got = _run(capsys, "install", "--harness", "opencode")
    assert code == 0 and _rows(got)["opencode"]["mcp"] == "added"
    assert not (home / "xdg/opencode/opencode.json").exists()
    assert jsonc.read_text() == ('{\n    "theme": "dark",\n    "mcp": {\n        "kennet": {\n            "type": '
                                 f'"remote",\n            "url": "{U}"\n        }}\n    }}\n}}\n')
    _run(capsys, "remove")
    assert jsonc.read_text() == '{\n    "theme": "dark"\n}\n'   # kennet did not make it: only the entry goes
    monkeypatch.setenv("OPENCODE_CONFIG", str(home / "custom.json"))
    _run(capsys, "install", "--harness", "opencode")
    assert json.loads((home / "custom.json").read_text())["mcp"]["kennet"]["url"] == U


def test_the_ledger_is_of_the_config_dir_in_effect(home, capsys, monkeypatch):
    _harness(home, "claude")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "a"))
    _run(capsys, "install", "--harness", "claude")
    assert json.loads((home / "a/.claude.json").read_text())["mcpServers"]["kennet"]["url"] == U
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "b"))
    _, got = _run(capsys, "remove")
    assert got["removed"] == [] and (home / "a/.claude.json").exists() and (home / "a/skills/kennet").exists()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / "a"))
    _, got = _run(capsys, "remove")
    assert not (home / "a/.claude.json").exists() and not (home / "a/skills/kennet").exists()


def test_json_is_rewritten_atomically_in_its_own_layout_and_unreadable_files_are_skipped(home, capsys):
    (home / "home/.cursor").mkdir()
    cfg = home / "home/.cursor/mcp.json"
    cfg.write_text('{\n\t"名前": "値",\n\t"mcpServers": {}\n}\n')
    cfg.chmod(0o600)
    _run(capsys, "install")
    assert cfg.read_text().startswith('{\n\t"名前": "値",\n\t"mcpServers": {\n\t\t"kennet"')
    assert cfg.stat().st_mode & 0o777 == 0o600 and [p.name for p in cfg.parent.iterdir()] == ["mcp.json"]
    _run(capsys, "remove")
    assert cfg.exists() and json.loads(cfg.read_text()) == {"名前": "値"}

    cfg.write_bytes(b'{"x": "\xff"}')
    _, got = _run(capsys, "install", "--harness", "cursor")
    assert _rows(got)["cursor"]["mcp"] == "skipped" and "not UTF-8" in _rows(got)["cursor"]["mcp_detail"]
    cfg.write_text('{\n  // mine\n  "mcpServers": {}\n}\n')
    _, got = _run(capsys, "install", "--harness", "cursor")
    assert "by hand" in _rows(got)["cursor"]["mcp_detail"] and cfg.read_text().startswith('{\n  // mine')


def test_gemini_settings_that_cannot_be_read_are_not_handed_to_its_add(home, capsys):
    _harness(home, "gemini")
    settings = home / "home/.gemini/settings.json"
    settings.parent.mkdir()
    settings.write_text('{\n  // mine\n  "theme": "x"\n}\n')
    _, got = _run(capsys, "install", "--harness", "gemini")
    assert _rows(got)["gemini"]["mcp"] == "skipped" and "by hand" in _rows(got)["gemini"]["mcp_detail"]
    assert _adds(home) == [] and settings.read_text().startswith('{\n  // mine')
    settings.write_text('{"mcpServers": {"kennet": {"httpUrl": "https://else/"}}}')
    _, got = _run(capsys, "install", "--harness", "gemini")   # its add would replace it
    assert _rows(got)["gemini"]["mcp"] == "present" and _adds(home) == []


def test_list_reads_the_files_alone_and_says_nothing_of_a_harness_not_here(home, capsys):
    _harness(home, "hum")
    _run(capsys, "install")
    code, got = _run(capsys, "list")
    rows = _rows(got)
    assert code == 0 and rows["hum"] == {"harness": "hum", "present": True, "skill_state": "written",
                                         "mcp": "installed", "skill": str(home / "home/.hum/skills/kennet/SKILL.md")}
    assert rows["claude"] == {"harness": "claude", "present": False}


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TOML is read with tomllib")
def test_a_codex_config_that_declares_kennet_in_another_shape_is_left_as_it_is(home, capsys):
    (home / "home/.codex").mkdir()
    cfg = home / "home/.codex/config.toml"
    cfg.write_text('[mcp_servers]\nkennet = { url = "https://else/" }\n')
    code, got = _run(capsys, "install", "--harness", "codex")
    assert code == 0 and got["harnesses"][0]["mcp"] == "present" and "[mcp_servers.kennet]" not in cfg.read_text()
