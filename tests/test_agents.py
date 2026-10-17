"""`kennet agents`: the skill and the MCP server into each harness here, against stand-ins for the harnesses' own
commands that keep what `mcp add` was given in a file and behave as each real one does on a name it has already."""
import json
from pathlib import Path

import pytest

from metaphi import agents, cli, skill

FAKE = """#!/bin/sh
PATH=/bin:/usr/bin
n=$(basename "$0"); st="$FAKE_STATE/$n"
for last; do :; done
echo "$n $*" >> "$FAKE_STATE/log"
case "$1 $2" in
"mcp add")
  if [ -f "$st" ]; then
    case $n in codex|copilot) ;; *) echo "MCP server kennet already exists in user config" >&2; exit 1 ;; esac
  fi
  echo "$last" > "$st" ;;
"mcp get")
  if [ -f "$st" ]; then cat "$st"; exit 0; fi
  echo 'No MCP server named "kennet".'
  [ "$n" = claude ] && exit 0; exit 1 ;;
"mcp remove") [ -f "$st" ] && rm "$st" || exit 1 ;;
esac
"""


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "HUM_HOME", "GEMINI_CLI_HOME", "COPILOT_HOME"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "repo").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "bin").mkdir()
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


def test_install_writes_the_skill_once_per_folder_and_registers_the_server_with_each_harness(home, capsys):
    _harness(home, "claude", "codex", "hum")
    (home / "home" / ".cursor").mkdir()   # Cursor, by its folder alone: no command, so its file is written
    code, got = _run(capsys, "install")
    assert code == 0
    rows = {r["harness"]: r for r in got["harnesses"]}
    assert set(rows) == {"claude", "codex", "hum", "cursor"}
    h = home / "home"
    assert rows["claude"]["skill"] == str(h / ".claude/skills/kennet/SKILL.md")
    assert rows["hum"]["skill"] == str(h / ".hum/skills/kennet/SKILL.md")
    assert rows["codex"]["skill"] == rows["cursor"]["skill"] == str(h / ".agents/skills/kennet/SKILL.md")
    for r in rows.values():
        assert Path(r["skill"]).read_text() == skill.STUB and r["mcp"] == "added", r
    for n in ("claude", "hum"):
        assert (home / "state" / n).read_text().strip() == agents.MCP_URL
    assert (h / ".codex/config.toml").read_text() == f'[mcp_servers.kennet]\nurl = "{agents.MCP_URL}"\n'
    assert json.loads((h / ".cursor/mcp.json").read_text()) == {"mcpServers": {"kennet": {"url": agents.MCP_URL}}}
    assert rows["codex"]["sign_in"] == "codex mcp login kennet" and rows["hum"]["sign_in"] == "hum mcp login kennet"
    assert got["next_step"] is None   # one sign-in a harness: each row says its own

    # again: nothing changes, and what kennet added before is its own
    code, got = _run(capsys, "install")
    assert code == 0 and {r["mcp"] for r in got["harnesses"]} == {"installed"}
    assert sum("mcp add" in x for x in (home / "state" / "log").read_text().splitlines()) == 2

    code, got = _run(capsys, "remove")
    assert code == 0 and got["left"] == []
    assert not (h / ".agents/skills/kennet").exists() and not (h / ".claude/skills/kennet").exists()
    assert not (h / ".cursor/mcp.json").exists()
    assert not any((home / "state" / n).exists() for n in ("claude", "hum")) and not (h / ".codex/config.toml").exists()
    assert "agents" not in json.loads((home / "cfg" / "config.json").read_text())


def test_a_server_or_skill_kennet_did_not_write_is_left_as_it_is(home, capsys):
    _harness(home, "claude", "codex")
    (home / "state" / "claude").write_text("https://elsewhere.example/mcp/\n")
    codex = home / "home" / ".codex/config.toml"
    codex.parent.mkdir()
    codex.write_text('[mcp_servers."kennet"]\nurl = "https://elsewhere.example/mcp/"\n')
    mine = home / "home" / ".agents/skills/kennet/SKILL.md"
    mine.parent.mkdir(parents=True)
    mine.write_text("mine")
    code, got = _run(capsys, "install")
    rows = {r["harness"]: r for r in got["harnesses"]}
    assert code == 0 and rows["claude"]["mcp"] == rows["codex"]["mcp"] == "present"
    assert "sign_in" not in rows["codex"] and rows["codex"]["skill_state"] == "kept" and mine.read_text() == "mine"
    assert (home / "state" / "claude").read_text().startswith("https://elsewhere") and "elsewhere" in codex.read_text()
    code, got = _run(capsys, "remove")
    assert code == 0 and mine.read_text() == "mine"
    assert (home / "state" / "claude").exists() and "elsewhere" in codex.read_text()
    code, got = _run(capsys, "install", "--harness", "codex", "--force")
    assert mine.read_text() == skill.STUB


def test_harness_chooses_and_a_harness_not_here_is_said(home, capsys):
    code, got = _run(capsys, "install")
    assert code == cli.FAILED and got["error"]["code"] == "no_agent_found"
    code, got = _run(capsys, "install", "--harness", "hum")
    (row,) = got["harnesses"]
    assert code == 0 and row["mcp"] == "skipped" and "not on the PATH" in row["mcp_detail"]
    assert Path(row["skill"]).is_file()   # the skill needs no command: it is a file the harness reads


def test_project_writes_the_repositorys_files_once_each_and_remove_takes_exactly_that_away(home, capsys):
    repo = home / "repo"
    (repo / ".codex").mkdir()
    (repo / ".codex/config.toml").write_text('model = "o3"\n')
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"type": "http", "url": "https://o/"}}}))
    code, got = _run(capsys, "install", "--project", *(f"--harness={n}" for n in agents.NAMES))
    assert code == 0 and {r["mcp"] for r in got["harnesses"]} == {"added"}
    assert {r["skill"] for r in got["harnesses"]} == {str(repo / ".claude/skills/kennet/SKILL.md"),
                                                       str(repo / ".agents/skills/kennet/SKILL.md")}
    assert json.loads((repo / ".mcp.json").read_text())["mcpServers"]["kennet"] == {"type": "http",
                                                                                    "url": agents.MCP_URL}
    assert (repo / ".codex/config.toml").read_text() == (f'model = "o3"\n\n[mcp_servers.kennet]\n'
                                                          f'url = "{agents.MCP_URL}"\n')
    assert 'name = "kennet"' in (repo / ".hum/config.toml").read_text()
    assert json.loads((repo / ".gemini/settings.json").read_text()) == {"mcpServers": {"kennet": {
        "httpUrl": agents.MCP_URL}}}
    assert json.loads((repo / "opencode.json").read_text()) == {"mcp": {"kennet": {"type": "remote",
                                                                                   "url": agents.MCP_URL}}}
    code, got = _run(capsys, "remove", "--project")
    assert code == 0 and got["left"] == []
    assert (repo / ".codex/config.toml").read_text() == 'model = "o3"\n'
    assert json.loads((repo / ".mcp.json").read_text()) == {"mcpServers": {"other": {"type": "http",
                                                                                     "url": "https://o/"}}}
    assert not any((repo / p).exists() for p in (".hum/config.toml", ".gemini/settings.json", "opencode.json",
                                                 ".cursor/mcp.json", ".agents/skills/kennet", ".claude/skills/kennet"))


def test_a_config_that_is_not_plain_json_is_never_rewritten(home, capsys):
    (home / "home" / ".cursor").mkdir()
    cfg = home / "home" / ".cursor/mcp.json"
    cfg.write_text('{\n  // mine\n  "mcpServers": {}\n}\n')
    code, got = _run(capsys, "install")
    (row,) = got["harnesses"]
    assert code == 0 and row["mcp"] == "skipped" and "by hand" in row["mcp_detail"]
    assert cfg.read_text().startswith('{\n  // mine')


def test_list_reads_the_files_alone(home, capsys):
    _harness(home, "hum")
    _run(capsys, "install")
    code, got = _run(capsys, "list")
    rows = {r["harness"]: r for r in got["harnesses"]}
    assert code == 0 and rows["hum"] == {"harness": "hum", "present": True, "skill_state": "written",
                                         "mcp": "installed", "skill": str(home / "home/.hum/skills/kennet/SKILL.md")}
    assert rows["claude"]["present"] is False and rows["claude"]["mcp"] == "absent"


def test_a_codex_config_that_declares_kennet_in_another_shape_is_left_as_it_is(home, capsys):
    (home / "home" / ".codex").mkdir()
    cfg = home / "home" / ".codex/config.toml"
    cfg.write_text('[mcp_servers]\nkennet = { url = "https://elsewhere.example/mcp/" }\n')
    code, got = _run(capsys, "install", "--harness", "codex")
    assert code == 0 and got["harnesses"][0]["mcp"] == "present"
    assert "[mcp_servers.kennet]" not in cfg.read_text()
