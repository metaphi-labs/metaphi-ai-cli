"""The CLI against stand-ins for accounts and the anonymizer: sign-in, keys, and one trace end to end."""
import json

import httpx
import pytest
import respx
from mcp.server.mcpserver import MCPServer

from metaphi import accounts, anonymize, cli, config

A = config.ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("METAPHI_API_KEY", raising=False)
    return tmp_path


@respx.mock
def test_login_polls_the_device_grant_until_approved_and_keeps_the_token(home):
    respx.post(f"{A}/device/code").respond(json={"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 0,
                                                 "expires_in": 60, "verification_uri_complete": f"{A}/device?x"})
    polls = respx.post(f"{A}/oauth/token").mock(side_effect=[
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "email": "alice@example.test"})])
    said = []
    got = accounts.login(say=said.append, open_browser=False, sleep=lambda s: None)
    assert got["email"] == "alice@example.test" and "ABCD-EFGH" in said[0]
    body = dict(x.split("=") for x in polls.calls[0].request.content.decode().split("&"))
    assert body["device_code"] == "dc" and body["client_id"] == "metaphi-cli"
    assert config.load()["token"] == "tok" and oct(config.path().stat().st_mode)[-3:] == "600"


@respx.mock
def test_keys_create_shows_the_key_once_on_the_only_org(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": ["kennet:anonymize"],
                                               "members": {}, "keys": []}])
    made = respx.post(f"{A}/keys/orgs/acme").respond(json={"id": "key_1", "name": "prod", "key": "mai_sk_x",
                                                           "scopes": ["kennet:anonymize"]})
    assert cli.main(["keys", "create", "prod"]) == 0
    assert json.loads(made.calls[0].request.content) == {"name": "prod", "scopes": ["kennet:anonymize"]}
    assert made.calls[0].request.headers["authorization"] == "Bearer tok"
    assert "mai_sk_x" in capsys.readouterr().out and "api_key" not in config.load()


@respx.mock
def test_a_refusal_is_the_servers_sentence(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "admin", "grants": [], "members": {}, "keys": []}])
    respx.put(f"{A}/keys/orgs/acme/members/x@lab.test").respond(
        409, json={"detail": "x@lab.test belongs to another organization"})
    assert cli.main(["members", "add", "x@lab.test"]) == 1
    assert "belongs to another organization" in capsys.readouterr().err
    assert cli.main(["keys", "list", "--org", "other"]) == 1


def _anonymizer(seen):
    m = MCPServer("stand-in")

    @m.tool()
    def upload_trace(filename: str) -> dict:
        seen["filename"] = filename
        return {"url": "https://files.test/up", "upload_id": "u" * 24, "expires_at": 0, "command": ""}

    @m.tool()
    def anonymize_trace(upload_id: str, key: str = "", confidential: list[str] | None = None) -> dict:
        seen["args"] = {"upload_id": upload_id, "key": key, "confidential": confidential}
        return {"task_id": "t1", "status": "working", "poll_interval_ms": 1}

    @m.tool()
    def get_task(task_id: str) -> dict:
        seen["polls"] = seen.get("polls", 0) + 1
        if seen["polls"] < 2:
            return {"taskId": task_id, "status": "working", "statusMessage": "reading"}
        return {"taskId": task_id, "status": "completed", "result": {"structuredContent": {"calls": [
            {"outcome": {"report": {"entities": 3}, "twin_table": {"Alice": "Morgan"},
                         "output_path": "k/u/r/trace.json", "report_path": "k/u/r/report.json"}}]}}}

    @m.tool()
    def pull_trace(trace_path: str) -> dict:
        return {"url": f"https://files.test/{trace_path}", "expires_at": 0, "command": ""}

    return m


@respx.mock
def test_anonymize_uploads_runs_and_downloads(home, monkeypatch, capsys):
    from mcp import Client
    trace = home / "trace.json"
    trace.write_text('{"user": "Alice"}')
    seen = {}
    token = respx.post(f"{A}/oauth/token").respond(json={"access_token": "jwt", "token_type": "Bearer"})
    put = respx.put("https://files.test/up").respond(200)
    respx.get("https://files.test/k/u/r/trace.json").respond(content=b'{"user": "Morgan"}')
    respx.get("https://files.test/k/u/r/report.json").respond(json={"entities": 3})
    monkeypatch.setattr(anonymize, "connect", lambda tok: seen.update(token=tok) or Client(_anonymizer(seen)))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    assert cli.main(["anonymize", str(trace), "--confidential", "Acme", "--json"]) == 0
    form = token.calls[0].request.content.decode()
    assert "client_credentials" in form and "mai_sk_live" in form and seen["token"] == "jwt"
    assert put.calls[0].request.content == b'{"user": "Alice"}' and seen["filename"] == "trace.json"
    assert seen["args"] == {"upload_id": "u" * 24, "key": "", "confidential": ["Acme"]}
    assert (home / "trace.anonymized.json").read_text() == '{"user": "Morgan"}'
    assert json.loads((home / "trace.anonymized.report.json").read_text()) == {"entities": 3}
    assert json.loads(capsys.readouterr().out)["twins"] == {"Alice": "Morgan"}


def test_anonymize_without_a_key_says_how_to_get_one(home, capsys):
    (home / "t.json").write_text("{}")
    assert cli.main(["anonymize", str(home / "t.json")]) == 1
    assert "METAPHI_API_KEY" in capsys.readouterr().err
