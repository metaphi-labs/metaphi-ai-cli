"""The CLI against stand-ins for accounts and the twinner: sign-in, keys, and one trace end to end."""
import json
from typing import Any

import httpx
import pytest
import respx
from mcp.server.mcpserver import MCPServer

from metaphi import accounts, cli, config, errors, twin

A = config.DEFAULT_ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_NO_KEYRING", "1")
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "METAPHI_KENNET", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


@respx.mock
def test_login_polls_the_device_grant_until_approved_and_keeps_the_token(home):
    code = respx.post(f"{A}/device/code").respond(json={"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 0,
                                                 "expires_in": 60, "verification_uri": f"{A}/device",
                                                 "verification_uri_complete": f"{A}/device?x"})
    polls = respx.post(f"{A}/oauth/token").mock(side_effect=[
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={"access_token": "tok", "token_type": "Bearer", "email": "alice@example.test"})])
    said, slept = [], []
    got = accounts.login(say=said.append, open_browser=False, sleep=slept.append)
    assert got["email"] == "alice@example.test" and "ABCD-EFGH" in said[0]
    assert code.calls[0].request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert code.calls[0].request.content == b"client_id=metaphi-cli"
    assert slept == [5.0, 5.0]  # RFC 8628: never faster than 5 s, whatever the server says
    body = dict(x.split("=") for x in polls.calls[0].request.content.decode().split("&"))
    assert body["device_code"] == "dc" and body["client_id"] == "metaphi-cli"
    assert config.load()["token"] == "tok" and oct(config.path().stat().st_mode)[-3:] == "600"


@respx.mock
def test_keys_create_shows_the_key_once_on_the_only_org(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": ["kennet:twin"],
                                               "members": {}, "keys": []}])
    made = respx.post(f"{A}/keys/orgs/acme").respond(json={"id": "key_1", "name": "prod", "key": "mai_sk_x",
                                                           "scopes": ["kennet:twin"]})
    assert cli.main(["keys", "create", "prod"]) == 0
    assert json.loads(made.calls[0].request.content) == {"name": "prod", "scopes": ["kennet:twin"]}
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


def _twinner(seen):
    m = MCPServer("stand-in")

    @m.tool()
    def upload_trace(filename: str) -> dict:
        seen["filename"] = filename
        return {"url": "https://files.metaphi.ai/up", "upload_id": "u" * 24, "expires_at": 0, "command": ""}

    @m.tool()
    def twin_trace(upload_id: str, key: str = "", confidential: list[str] | None = None) -> Any:
        seen["args"] = {"upload_id": upload_id, "key": key, "confidential": confidential}
        if "refusal" in seen:
            return seen["refusal"]
        return {"task_id": "t1", "status": "working", "poll_interval_ms": 1}

    @m.tool()
    def get_task(task_id: str) -> dict:
        seen["polls"] = seen.get("polls", 0) + 1
        if seen["polls"] < 2:
            return {"taskId": task_id, "status": "working", "statusMessage": "reading"}
        outcome = seen.get("outcome", {"run_id": "r", "status": "completed", "output_path": "k/u/r/trace.json"})
        return {"taskId": task_id, "status": "completed",
                "result": {"structuredContent": {"calls": [{"outcome": outcome}]}}}

    @m.tool()
    def pull_trace(trace_path: str) -> dict:
        return {"url": f"https://files.metaphi.ai/{trace_path}", "expires_at": 0, "command": ""}

    return m


@respx.mock
def test_twin_uploads_runs_and_downloads(home, monkeypatch, capsys):
    from mcp import Client
    trace = home / "trace.json"
    trace.write_text('{"user": "Alice"}')
    seen = {}
    token = respx.post(f"{A}/oauth/token").respond(json={"access_token": "jwt", "token_type": "Bearer"})
    put = respx.put("https://files.metaphi.ai/up").respond(200)
    respx.get("https://files.metaphi.ai/k/u/r/trace.json").respond(content=b'{"user": "Morgan"}')
    monkeypatch.setattr(twin, "connect", lambda tok: seen.update(token=tok) or Client(_twinner(seen)))
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    assert cli.main(["twin", str(trace), "--confidential", "Acme", "--json"]) == 0
    form = token.calls[0].request.content.decode()
    assert "client_credentials" in form and "mai_sk_live" in form and seen["token"] == "jwt"
    assert put.calls[0].request.content == b'{"user": "Alice"}' and seen["filename"] == "trace.json"
    assert seen["args"] == {"upload_id": "u" * 24, "key": "", "confidential": ["Acme"]}
    assert (home / "trace.twin.json").read_text() == '{"user": "Morgan"}'
    assert sorted(p.name for p in home.iterdir() if p.is_file()) == ["trace.json", "trace.twin.json"]
    assert json.loads(capsys.readouterr().out) == {"output": str(home / "trace.twin.json")}


def test_a_failed_run_says_why_and_writes_nothing(home, monkeypatch, capsys):
    from mcp import Client
    (home / "trace.json").write_text("{}")
    seen = {"outcome": {"run_id": "r", "status": "failed", "error": "the twin still held an original value"}}
    with respx.mock:
        respx.post(f"{A}/oauth/token").respond(json={"access_token": "jwt", "token_type": "Bearer"})
        respx.put("https://files.metaphi.ai/up").respond(200)
        monkeypatch.setattr(twin, "connect", lambda tok: Client(_twinner(seen)))
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
        assert cli.main(["twin", str(home / "trace.json")]) == 1
    assert "the twin still held an original value" in capsys.readouterr().err
    assert [p.name for p in home.iterdir() if p.is_file()] == ["trace.json"]


OUT_OF_CREDIT = {"type": "billing_error", "code": "out_of_credit", "retryable": False, "request_id": "req_1",
                 "message": "This organization's prepaid balance is used up; add credits to continue."}


def _refused(error: dict) -> Any:
    from mcp.types import CallToolResult, TextContent
    return CallToolResult(content=[TextContent(type="text", text=error["message"])],
                          structured_content={"error": error}, is_error=True)


def _twin(home, monkeypatch, seen, *argv, run=None) -> Any:
    """`metaphi twin` against the stand-in, or `run()` in its place."""
    from mcp import Client
    (home / "trace.json").write_text("{}")
    with respx.mock:
        respx.post(f"{A}/oauth/token").respond(json={"access_token": "jwt", "token_type": "Bearer"})
        respx.put("https://files.metaphi.ai/up").respond(200)
        monkeypatch.setattr(twin, "connect", lambda tok: Client(_twinner(seen)))
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
        return run() if run else cli.main(["twin", str(home / "trace.json"), *argv])


def test_a_refusal_raises_its_type_and_keeps_its_code(home, monkeypatch, capsys):
    seen = {"refusal": _refused(OUT_OF_CREDIT)}
    assert _twin(home, monkeypatch, seen) == 1
    err = capsys.readouterr().err
    assert err.endswith(f"error: {OUT_OF_CREDIT['message']} (code: out_of_credit, request: req_1)\n")

    def run():
        with pytest.raises(errors.BillingError) as e:
            twin.twin(home / "trace.json", say=lambda s: None)
        return e.value

    got = _twin(home, monkeypatch, seen, run=run)
    assert (got.code, got.request_id, got.retryable) == ("out_of_credit", "req_1", False)
    assert isinstance(got, errors.MetaphiError) and isinstance(got, config.Error)


def test_json_puts_the_error_object_on_stdout(home, monkeypatch, capsys):
    limited = {"type": "rate_limit_error", "code": "rate_limited_concurrent", "retryable": True,
               "request_id": "req_2", "message": "3 runs are already going for this organization"}
    assert _twin(home, monkeypatch, {"refusal": _refused(limited)}, "--json") == 1
    assert json.loads(capsys.readouterr().out) == {"error": limited}


def test_a_failed_run_raises_its_code_and_run_id(home, monkeypatch, capsys):
    failed = {"type": "run_error", "code": "twin_unclean", "retryable": False, "request_id": "req_3",
              "run_id": "r9", "message": "the twin did not come out clean; nothing was written or charged"}
    seen = {"outcome": {"run_id": "r9", "status": "failed", "error": failed}}
    assert _twin(home, monkeypatch, seen, "--json") == 1
    out = capsys.readouterr()
    assert json.loads(out.out) == {"error": failed}
    assert "(code: twin_unclean, request: req_3)" in out.err
    assert [p.name for p in home.iterdir() if p.is_file()] == ["trace.json"]


def test_an_uncoded_tool_error_is_a_plain_metaphi_error():
    from mcp.types import CallToolResult, TextContent
    with pytest.raises(errors.MetaphiError) as e:
        twin._structured(CallToolResult(content=[TextContent(type="text", text="twin_trace needs L3")],
                                        is_error=True))
    assert type(e.value) is errors.MetaphiError and e.value.code is None and str(e.value) == "twin_trace needs L3"
    got = errors.from_object({"type": "new_error", "code": "later", "message": "m"})
    assert type(got) is errors.MetaphiError and got.code == "later" and got.type == "new_error"
    assert type(errors.from_object({"type": "api_error", "code": "unavailable", "message": "m",
                                    "retryable": True})) is errors.APIError


def test_twin_without_a_key_says_how_to_get_one(home, capsys):
    (home / "t.json").write_text("{}")
    assert cli.main(["twin", str(home / "t.json")]) == 1
    assert "METAPHI_API_KEY" in capsys.readouterr().err


ORG = {"id": "acme", "name": "Acme", "role": "billing", "can_buy": True, "credits_usd": 12.5}


@respx.mock
def test_credits_shows_the_balance(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/credits/orgs").respond(json=[ORG])
    assert cli.main(["credits"]) == 0
    assert capsys.readouterr().out.strip() == "acme: $12.50 prepaid credit"
    respx.get(f"{A}/credits/orgs").respond(json=[{**ORG, "credits_usd": -3.0, "can_buy": False}])
    assert cli.main(["credits", "--org", "acme"]) == 0
    assert "ask an owner or billing member" in capsys.readouterr().out
    respx.get(f"{A}/credits/orgs").respond(json=[])
    assert cli.main(["credits"]) == 1


@respx.mock
def test_credits_buy_opens_checkout_and_waits_until_paid(monkeypatch, capsys):
    config.save({"token": "tok"})
    monkeypatch.setattr(accounts.time, "sleep", lambda s: None)
    respx.get(f"{A}/credits/orgs").respond(json=[ORG])
    made = respx.post(f"{A}/credits/orgs/acme/checkout").respond(
        json={"url": "https://checkout.stripe.com/c/pay/cs_1", "id": "cs_1"})
    respx.get(f"{A}/credits/orgs/acme/checkout/cs_1").mock(side_effect=[
        httpx.Response(200, json={"paid": False, "credits_usd": 12.5}),
        httpx.Response(200, json={"paid": True, "credits_usd": 62.5})])
    assert cli.main(["credits", "buy", "--amount", "50", "--no-browser"]) == 0
    assert json.loads(made.calls[0].request.content) == {"usd": 50.0}
    assert made.calls[0].request.headers["authorization"] == "Bearer tok"
    out = capsys.readouterr().out
    assert "https://checkout.stripe.com/c/pay/cs_1" in out and "acme: $62.50 prepaid credit" in out


@respx.mock
def test_only_owners_and_billing_buy_and_only_through_stripe(monkeypatch, capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/credits/orgs").respond(json=[{**ORG, "role": "admin", "can_buy": False}])
    assert cli.main(["credits", "buy", "--no-browser"]) == 1
    assert "only an owner or billing member" in capsys.readouterr().err
    respx.get(f"{A}/credits/orgs").respond(json=[ORG])
    respx.post(f"{A}/credits/orgs/acme/checkout").respond(json={"url": "https://evil.test/pay", "id": "cs_1"})
    assert cli.main(["credits", "buy", "--no-browser"]) == 1
    assert "not Stripe's" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.main(["credits", "buy", "--amount", "0"])


@respx.mock
def test_a_billing_member_is_added_by_role():
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": [], "members": {}, "keys": []}])
    put = respx.put(f"{A}/keys/orgs/acme/members/pay@acme.test").respond(json={})
    assert cli.main(["members", "add", "pay@acme.test", "--role", "billing"]) == 0
    assert json.loads(put.calls[0].request.content) == {"role": "billing"}
