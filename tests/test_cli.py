"""The CLI against stand-ins for accounts and the twin API: sign-in, the keys it hands over, and one trace end to end."""
import json

import httpx
import pytest
import respx

from metaphi import accounts, cli, config, errors, skill, twin

A = config.DEFAULT_ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.chdir(tmp_path)
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


CODE = {"device_code": "dc", "user_code": "ABCD-EFGH", "interval": 0, "expires_in": 60,
        "verification_uri": f"{A}/device"}
SIGNED = {"access_token": "tok", "token_type": "Bearer", "email": "alice@example.test"}


def _login(monkeypatch, **handed) -> int:
    respx.post(f"{A}/device/code").respond(json=CODE)
    respx.post(f"{A}/oauth/token").respond(json={**SIGNED, **handed})
    monkeypatch.setattr(accounts.time, "sleep", lambda s: None)
    return cli.main(["login", "--no-browser", "--wait"])


@respx.mock
def test_login_saves_the_live_key_and_the_test_key_it_is_handed(monkeypatch, capsys):
    assert _login(monkeypatch, org="acme", api_key="mai_sk_live", test_api_key="mai_sk_test_t") == 0
    cfg = config.load()
    assert (cfg["token"], cfg["api_key"], cfg["test_api_key"]) == ("tok", "mai_sk_live", "mai_sk_test_t")
    out = capsys.readouterr().out
    assert "Ready for acme" in out and "Next: kennet twin <file>" in out
    assert "console" not in out and "mai_sk_" not in out


@respx.mock
def test_login_with_no_key_handed_over_says_why_and_still_signs_in(monkeypatch, capsys):
    config.save({"api_key": "mai_sk_mine", "test_api_key": "mai_sk_test_old"})
    # signed in, and no key: the one refusal every command makes of it, here with nothing left to run
    assert _login(monkeypatch, no_api_key="Acme has no synthetic twinning access yet; ask Metaphi AI for it") == cli.AUTH
    got = capsys.readouterr()
    out = got.out
    assert "No key was saved: Acme has no synthetic twinning access yet" in out and "Ready for" not in out
    assert out.splitlines()[-1] == "`kennet login` picks the keys up once that changes, with no second sign-in."
    assert "Next:" not in out + got.err and "(code: no_api_key)" in got.err
    cfg = config.load()   # signed in, and no key left saved: one from before speaks for what this sign-in was not given
    assert cfg["token"] == "tok" and "api_key" not in cfg and "test_api_key" not in cfg


@respx.mock
def test_login_away_from_a_terminal_hands_the_code_over_and_returns(monkeypatch, capsys):
    code = respx.post(f"{A}/device/code").respond(json=CODE)
    token = respx.post(f"{A}/oauth/token").mock(side_effect=[
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(200, json={**SIGNED, "org": "acme", "api_key": "mai_sk_live",
                                  "test_api_key": "mai_sk_test_t"})])
    monkeypatch.setattr(accounts.time, "sleep", lambda s: None)
    assert cli.main(["login", "--no-browser", "--json"]) == 0   # pytest is no terminal: no --no-wait needed
    out = capsys.readouterr()
    got = json.loads(out.out)
    assert (got["verification_uri"], got["user_code"]) == (f"{A}/device", "ABCD-EFGH")
    assert got["next_step"] == "kennet login --complete" and 0 < got["expires_in"] <= 60
    assert got["status"] == "pending"
    assert code.call_count == 1 and not token.called and "token" not in config.load()
    assert cli.main(["login", "--no-browser", "--json"]) == 0   # asked again: the code a person may hold stands
    assert json.loads(capsys.readouterr().out)["user_code"] == "ABCD-EFGH" and code.call_count == 1
    assert cli.main(["login", "--complete", "--json", "--timeout", "1"]) == cli.AUTH   # not approved yet, and said so
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["code"], error["retryable"], error["next_step"]) == ("authorization_pending", True,
                                                                         "kennet login --complete")
    assert token.call_count == 1 and config.load()["device_code"] == "dc"   # still under way
    assert cli.main(["login", "--complete", "--json"]) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"email": "alice@example.test", "org": "acme", "keys": True,
                                   "next_step": "kennet twin <file> --test"}
    assert "ABCD-EFGH" in out.err and "mai_sk_" not in out.out + out.err
    cfg = config.load()
    assert cfg["api_key"] == "mai_sk_live" and "pending" not in cfg and "device_code" not in cfg
    assert code.call_count == 1   # the same code was finished, not a new one drawn


def test_login_complete_with_nothing_under_way_names_the_next_command(capsys):
    assert cli.main(["login", "--complete", "--json"]) == cli.AUTH
    out = capsys.readouterr()
    error = json.loads(out.out)["error"]
    assert (error["type"], error["code"], error["next_step"]) == ("auth_error", "login_expired", "kennet login")
    assert out.err.endswith("Next: kennet login\n")


@respx.mock
def test_whoami_asks_the_server_and_says_the_org_the_balance_and_the_key(monkeypatch, capsys):
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "not_signed_in"
    config.save({"token": "tok", "email": "alice@example.test", "org": "acme", "api_key": "mai_sk_live_abcd",
                 "test_api_key": "mai_sk_test_wxyz"})
    checked = respx.get(f"{API}/twins").respond(json={"object": "list", "data": []})
    orgs = respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": ["twin"]}])
    respx.get(f"{A}/credits/orgs").respond(json=[{"id": "acme", "credits_usd": 12.5, "can_buy": True}])
    respx.get(f"{A}/keys/invitations").respond(json=[])
    assert cli.main(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "email": "alice@example.test", "org": "acme",
        "organizations": [{"id": "acme", "role": "owner", "grants": ["twin"], "credits_usd": 12.5, "can_buy": True}],
        "key": {"source": "saved", "hint": "…abcd", "status": "ok"},
        "test_key": {"source": "saved", "hint": "…wxyz", "status": "ok"}}
    # each key was asked of the twin service, as itself
    assert sorted(c.request.headers["authorization"] for c in checked.calls) == [   # asked side by side
        "Bearer mai_sk_live_abcd", "Bearer mai_sk_test_wxyz"]
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_env_9999")
    assert cli.main(["whoami"]) == 0
    out = capsys.readouterr().out
    assert "org:    acme (owner) holds twin; $12.50 prepaid credit" in out
    assert "key:    …9999 (METAPHI_API_KEY): works" in out and "mai_sk_" not in out
    orgs.respond(401, json={"detail": "no"})   # a sign-in the server has ended is not "signed in"
    assert cli.main(["whoami"]) == cli.AUTH
    assert capsys.readouterr().err.endswith("Next: kennet login\n")


@respx.mock
def test_signed_in_without_a_key_is_one_refusal_everywhere_and_names_the_sign_in_that_picks_keys_up(home, capsys):
    """Keys are handed over at sign-in alone: once the organization has access, `kennet login` is what saves them."""
    (home / "t.json").write_text("{}")
    why = "Acme has no synthetic twinning access yet; ask Metaphi AI for it"
    config.save({"token": "tok", "email": "alice@example.test", "org": "acme", "no_api_key": why})
    respx.get(f"{A}/keys/orgs").respond(json=[])   # a member manages no organization
    respx.get(f"{A}/credits/orgs").respond(403, json={"detail": "no"})
    respx.get(f"{A}/keys/invitations").respond(json=[])
    seen = []
    for argv in (["twin", str(home / "t.json"), "--json"], ["twins", "list", "--json"], ["whoami", "--json"]):
        assert cli.main(argv) == cli.AUTH
        got = json.loads(capsys.readouterr().out)
        seen.append(got)
        error = got["error"]
        assert (error["type"], error["code"], error["next_step"]) == ("auth_error", "no_api_key", "kennet login")
        assert why in error["message"]
    # whoami still says who and where: the organization the sign-in is in, though the person manages none
    assert (seen[2]["email"], seen[2]["org"], seen[2]["organizations"], seen[2]["key"]) == (
        "alice@example.test", "acme", [], None)


@respx.mock
def test_a_person_in_no_organization_makes_theirs_and_is_told_it_waits_for_approval(monkeypatch, capsys):
    why = "alice@example.test is not in an organization; make yours with `kennet org create <name>`"
    assert _login(monkeypatch, no_api_key=why, no_api_key_code="no_organization") == cli.AUTH
    got = capsys.readouterr()
    assert got.err.endswith("Next: kennet org create <name>\n")   # the sign-in names what clears it
    waits = "Acme is made and you own it. It is waiting for Metaphi AI to approve it for Kennet."
    made = respx.post(f"{A}/keys/orgs").respond(json={"id": "acme", "name": "Acme", "role": "owner",
                                                      "admission": "pending", "message": waits})
    assert cli.main(["org", "create", "Acme", "--json"]) == 0
    assert json.loads(made.calls[-1].request.content) == {"name": "Acme"}
    assert made.calls[-1].request.headers["authorization"] == "Bearer tok"
    # waiting for the approval: nothing to run until the mail comes, so no next step
    assert json.loads(capsys.readouterr().out) == {"id": "acme", "name": "Acme", "role": "owner",
                                                   "admission": "pending", "message": waits, "next_step": None}
    made.respond(json={"id": "acme", "name": "Acme", "role": "owner", "admission": "pending"})
    assert cli.main(["org", "create", "Acme"]) == 0
    assert capsys.readouterr().out == f"{cli.APPROVAL}\n"
    made.respond(json={"id": "acme", "name": "Acme", "role": "owner", "admission": "pending", "message": waits})
    # until the approval whoami says the organization waits, and names the command that picks the keys up
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": [], "admission": "pending"}])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    respx.get(f"{A}/keys/invitations").respond(json=[])
    assert cli.main(["whoami"]) == cli.AUTH
    got = capsys.readouterr()
    assert "org:    acme (owner) waits for Metaphi AI to approve it" in got.out
    assert "waiting for Metaphi AI to approve it" in got.err and got.err.endswith("Next: kennet login\n")
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert seen["error"]["next_step"] == "kennet login" and seen["organizations"][0]["admission"] == "pending"
    # signed in already, `kennet login` opens no browser: it asks for the keys by the token this machine holds
    pick = respx.post(f"{A}/keys/cli").respond(json={
        "email": "alice@example.test", "org": "acme", "no_api_key_code": "awaiting_approval",
        "no_api_key": "Acme is waiting for Metaphi AI to approve it for Kennet"})
    assert cli.main(["login", "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["reason"] == "awaiting_approval"
    assert pick.calls[-1].request.headers["authorization"] == "Bearer tok"
    pick.respond(json={"email": "alice@example.test", "org": "acme", "api_key": "mai_sk_live",
                       "test_api_key": "mai_sk_test_t"})
    assert cli.main(["login", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"email": "alice@example.test", "org": "acme", "keys": True,
                                                   "next_step": "kennet twin <file> --test"}
    cfg = config.load()
    assert (cfg["token"], cfg["api_key"], cfg["test_api_key"]) == ("tok", "mai_sk_live", "mai_sk_test_t")
    assert "no_api_key" not in cfg and "pending" not in cfg
    # a sign-in that has ended, or a server that knows no pickup, falls through to the browser sign-in
    pick.respond(401, json={"detail": "sign in"})
    respx.post(f"{A}/device/code").respond(json=CODE)
    assert cli.main(["login", "--no-browser", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending"
    config.save({k: v for k, v in config.load().items() if k not in ("pending", "device_code")})
    # a resource that asks for no approval admits it at once: the sign-in is what is left
    made.respond(json={"id": "acme", "name": "Acme", "role": "owner", "admission": "admitted",
                       "message": "Acme is made and you own it. Run `kennet login` again for its keys."})
    assert cli.main(["org", "create", "Acme"]) == 0
    assert capsys.readouterr().out.endswith("Next: kennet login\n")
    made.respond(409, json={"detail": "alice@example.test is in Acme (acme) already; a person is in one organization"})
    assert cli.main(["org", "create", "Other"]) == cli.FAILED
    assert "is in Acme (acme) already" in capsys.readouterr().err


def test_a_group_command_alone_prints_its_own_help(capsys):
    for group, verb in (("org", "create"), ("twins", "wait"), ("skill", "install")):
        assert cli.main([group]) == 0
        out = capsys.readouterr().out
        assert out.startswith(f"usage: kennet {group}") and verb in out


@respx.mock
def test_whoami_does_not_call_a_key_fine_that_the_twin_service_refuses(capsys):
    config.save({"token": "tok", "email": "alice@example.test", "org": "acme", "api_key": "mai_sk_live_abcd",
                 "test_api_key": "mai_sk_test_wxyz"})
    respx.get(f"{A}/keys/orgs").respond(json=[])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    respx.get(f"{A}/keys/invitations").respond(json=[])
    down = {"type": "api_error", "code": "auth_unavailable", "retryable": True, "message": "could not be checked"}
    checked = respx.get(f"{API}/twins").respond(503, json={"error": down}, headers={"retry-after": "30"})
    assert cli.main(["whoami", "--json"]) == cli.LATER
    got = json.loads(capsys.readouterr().out)
    assert got["error"] == {**down, "next_step": None, "retry_after_s": 30.0}
    assert got["key"] == {"source": "saved", "hint": "…abcd", "status": "auth_unavailable"}
    assert checked.call_count == 2   # each key once: a 30 s wait is not waited out
    checked.respond(401, json={"error": {"type": "auth_error", "code": "invalid_api_key", "message": "revoked"}})
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["error"]["next_step"] == "kennet login"


@respx.mock
def test_logout_clears_the_token_and_both_keys():
    config.save({"token": "tok", "email": "alice@example.test", "api_key": "mai_sk_live",
                 "test_api_key": "mai_sk_test_t"})
    out = respx.post(f"{A}/logout").respond(json={"revoked": 1})
    assert cli.main(["logout"]) == 0
    assert out.calls[0].request.headers["authorization"] == "Bearer tok" and config.load() == {}


def test_bare_kennet_prints_the_help_and_the_next_step(capsys):
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: kennet") and out.splitlines()[-1] == "Get started: kennet login"
    config.save({"token": "tok", "api_key": "mai_sk_live"})
    assert cli.main([]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "Next: kennet twin <file>"


@pytest.mark.parametrize("command", ["keys", "members"])
def test_keys_and_members_are_not_commands(command, capsys):
    with pytest.raises(SystemExit):
        cli.main([command, "list"])
    assert "invalid choice" in capsys.readouterr().err


@respx.mock
def test_a_refusal_is_the_servers_sentence(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/credits/orgs").respond(403, json={"detail": "alice@example.test is closed; ask Metaphi AI"})
    assert cli.main(["credits"]) == 1
    assert "is closed" in capsys.readouterr().err


API = config.DEFAULT_API
TWIN = {"id": "twin_t1", "object": "twin", "status": "queued", "created": 1, "completed": None,
        "filename": "trace.json", "error": None, "livemode": True}


def _api(statuses=("running", "completed"), error=None, content=b'{"user": "Morgan"}', created=None):
    """The twin API stand-in: the create answer, then one GET per status, then the content."""
    made = respx.post(f"{API}/twins").mock(return_value=created or httpx.Response(202, json=TWIN))
    polls = respx.get(f"{API}/twins/twin_t1").mock(side_effect=[
        httpx.Response(200, json={**TWIN, "status": s, "error": error if s == "failed" else None}) for s in statuses])
    got = respx.get(f"{API}/twins/twin_t1/content").respond(content=content,
                                                           headers={"content-type": "application/json"})
    return made, polls, got


def _run(home, monkeypatch, *argv) -> int:
    (home / "trace.json").write_text('{"user": "Alice"}')
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    monkeypatch.setattr(twin.time, "sleep", lambda s: None)
    return cli.main(["twin", str(home / "trace.json"), *argv])


@respx.mock
def test_twin_uploads_polls_and_downloads(home, monkeypatch, capsys):
    made, polls, got = _api()
    assert _run(home, monkeypatch, "--confidential", "Acme", "--confidential", "Initech", "--key", "ab" * 32,
                "--json") == 0
    request = made.calls[0].request
    assert request.headers["authorization"] == "Bearer mai_sk_live"
    assert request.headers["metaphi-version"] == "2026-10-11"
    assert str(request.url) == "https://api.metaphi.ai/v1/kennet/twins"
    assert len(request.headers["idempotency-key"]) == 32
    body = request.content
    assert b'filename="trace.json"' in body and b'{"user": "Alice"}' in body
    assert body.count(b'name="confidential"') == 2 and b"Initech" in body and b"ab" * 32 in body
    assert polls.call_count == 2 and got.called
    assert (home / "trace.twin.json").read_text() == '{"user": "Morgan"}'
    assert sorted(p.name for p in home.iterdir() if p.is_file()) == ["trace.json", "trace.twin.json"]
    assert json.loads(capsys.readouterr().out) == {"output": str(home / "trace.twin.json"), "id": "twin_t1"}


@respx.mock
def test_the_python_api_answers_the_output_and_the_twin(home, monkeypatch):
    _api(statuses=("completed",))
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    got = twin.twin(home / "trace.json", home / "out.json", say=lambda s: None, sleep=lambda s: None)
    assert got == {"output": str(home / "out.json"), "id": "twin_t1"}


@respx.mock
def test_polling_backs_off(home, monkeypatch):
    _api(statuses=("queued", "running", "running", "running", "completed"))
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    slept = []
    twin.twin(home / "trace.json", say=lambda s: None, sleep=slept.append)
    assert slept == [1.0, 1.5, 2.25, 3.375, 5.0625]


def test_a_failed_run_raises_its_code_and_writes_nothing(home, monkeypatch, capsys):
    failed = {"type": "run_error", "code": "twin_unclean", "retryable": False,
              "message": "the twin did not come out clean; nothing was written or charged"}
    with respx.mock:
        _, _, got = _api(statuses=("failed",), error=failed)
        assert _run(home, monkeypatch, "--json") == cli.FAILED
        assert not got.called
    out = capsys.readouterr()
    assert json.loads(out.out) == {"error": {**failed, "run_id": "twin_t1", "next_step": None}}   # the twin, named
    assert "(code: twin_unclean)" in out.err
    assert [p.name for p in home.iterdir() if p.is_file()] == ["trace.json"]


OUT_OF_CREDIT = {"type": "billing_error", "code": "out_of_credit", "retryable": False, "request_id": "req_1",
                 "message": "This organization's prepaid balance is used up; add credits to continue."}


def test_a_refusal_raises_its_type_and_keeps_its_code(home, monkeypatch, capsys):
    with respx.mock:
        _api(created=httpx.Response(402, json={"error": OUT_OF_CREDIT}, headers={"request-id": "req_1"}))
        assert _run(home, monkeypatch) == 1
    err = capsys.readouterr().err   # not a terminal: the error, then where credit is added (an older server: ours)
    assert err.endswith(f"error: {OUT_OF_CREDIT['message']} (code: out_of_credit, request: req_1)\n"
                        f"Add credit: {A}/console?add=credit  or run: kennet credits buy --amount <amount>\n")
    with respx.mock:
        _api(created=httpx.Response(402, json={"error": OUT_OF_CREDIT}))
        with pytest.raises(errors.BillingError) as e:
            twin.twin(home / "trace.json", say=lambda s: None, sleep=lambda s: None)
    got = e.value
    assert (got.code, got.request_id, got.retryable) == ("out_of_credit", "req_1", False)
    assert isinstance(got, errors.MetaphiError) and isinstance(got, config.Error)


def test_json_puts_the_error_object_on_stdout(home, monkeypatch, capsys):
    limited = {"type": "rate_limit_error", "code": "rate_limited_concurrent", "retryable": True,
               "request_id": "req_2", "message": "3 runs are already going for this organization"}
    with respx.mock:
        made, _, _ = _api(created=httpx.Response(429, json={"error": limited}, headers={"retry-after": "60"}))
        assert _run(home, monkeypatch, "--json") == cli.LATER
        assert made.call_count == 1          # a rate limit is the caller's to wait out
    assert json.loads(capsys.readouterr().out) == {"error": {**limited, "next_step": None, "retry_after_s": 60.0}}


@respx.mock
def test_a_dropped_upload_is_retried_with_the_same_idempotency_key(home, monkeypatch):
    made, _, _ = _api(statuses=("completed",))
    made.mock(side_effect=[httpx.ConnectError("reset"),
                           httpx.Response(503, json={"error": {"type": "api_error", "code": "unavailable",
                                                               "retryable": True, "message": "try again"}},
                                          headers={"retry-after": "2"}),
                           httpx.Response(202, json=TWIN)])
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    slept = []
    twin.twin(home / "trace.json", say=lambda s: None, sleep=slept.append)
    keys = {c.request.headers["idempotency-key"] for c in made.calls}
    assert made.call_count == 3 and len(keys) == 1
    assert 0 <= slept[0] <= twin.BASE_S and slept[1] == 2.0   # full jitter, then the wait the service named


UNAVAILABLE = {"type": "api_error", "code": "auth_unavailable", "retryable": True,
               "message": "the API key could not be checked just now; nothing was started; try again in a minute"}


def test_an_unavailable_service_is_said_at_once_with_how_long_to_wait(home, monkeypatch, capsys):
    """An agent's own clock is short: a 30 s Retry-After is handed over, not slept through four times."""
    with respx.mock:
        made, _, _ = _api(created=httpx.Response(503, json={"error": UNAVAILABLE}, headers={"retry-after": "30"}))
        assert _run(home, monkeypatch, "--json", "--no-wait") == cli.LATER
        assert made.call_count == 1
    out = capsys.readouterr()
    assert json.loads(out.out) == {"error": {**UNAVAILABLE, "next_step": None, "retry_after_s": 30.0}}
    assert out.err.endswith("Try again in 30s.\n")


@respx.mock
def test_retries_are_few_jittered_and_end_within_the_budget(home, monkeypatch):
    made, _, _ = _api(created=httpx.Response(503, json={"error": UNAVAILABLE}))
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    slept = []
    with pytest.raises(errors.APIError) as e:
        twin.twin(home / "trace.json", say=lambda s: None, sleep=slept.append)
    assert made.call_count == 1 + twin.RETRIES and e.value.retry_after_s is None
    assert len({c.request.headers["idempotency-key"] for c in made.calls}) == 1
    assert [w <= twin.BASE_S * 2 ** n for n, w in enumerate(slept)] == [True] * twin.RETRIES
    assert sum(twin.BASE_S * 2 ** n for n in range(twin.RETRIES)) < twin.BUDGET_S
    # a budget already spent (a slow answer) leaves no retry
    monkeypatch.setattr(twin, "BUDGET_S", 0.0)
    before = made.call_count
    with pytest.raises(errors.APIError):
        twin.twin(home / "trace.json", say=lambda s: None, sleep=slept.append)
    assert made.call_count == before + 1


def test_an_idempotency_conflict_is_its_own_type():
    got = errors.from_object({"type": "idempotency_error", "code": "idempotency_key_reused", "message": "m"})
    assert type(got) is errors.IdempotencyError


def test_an_uncoded_answer_is_a_plain_error(home, monkeypatch):
    with respx.mock:
        _api(created=httpx.Response(502, text="bad gateway"))
        (home / "trace.json").write_text("{}")
        monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
        with pytest.raises(errors.APIError) as e:
            twin.twin(home / "trace.json", say=lambda s: None, sleep=lambda s: None)
    assert e.value.code is None and "HTTP 502" in str(e.value)
    got = errors.from_object({"type": "new_error", "code": "later", "message": "m"})
    assert type(got) is errors.MetaphiError and got.code == "later" and got.type == "new_error"


def test_twin_without_a_key_says_how_to_get_one(home, capsys):
    (home / "t.json").write_text("{}")
    assert cli.main(["twin", str(home / "t.json")]) == cli.AUTH
    assert "no key: run `kennet login`, or set METAPHI_API_KEY" in capsys.readouterr().err
    config.save({"api_key": "mai_sk_live"})   # a live key alone never stands in for the test key
    assert cli.main(["twin", str(home / "t.json"), "--test"]) == cli.AUTH
    assert "no test key: run `kennet login`" in capsys.readouterr().err


@respx.mock
def test_twin_test_runs_on_the_saved_test_key_and_the_environment_overrides_both(home, monkeypatch):
    (home / "trace.json").write_text('{"user": "Alice"}')
    monkeypatch.setattr(twin.time, "sleep", lambda s: None)
    config.save({"api_key": "mai_sk_live", "test_api_key": "mai_sk_test_t"})
    used = []
    for argv, env in ((["--test"], None), ([], None), (["--test"], "mai_sk_env")):
        if env:
            monkeypatch.setenv("METAPHI_API_KEY", env)
        made, _polls, _got = _api()
        assert cli.main(["twin", str(home / "trace.json"), *argv]) == 0
        used.append(made.calls[-1].request.headers["authorization"])
    assert used == ["Bearer mai_sk_test_t", "Bearer mai_sk_live", "Bearer mai_sk_env"]


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


ADD = "https://app.metaphi.ai/console?org=acme&add=credit"
REFUSED = httpx.Response(402, json={"error": {**OUT_OF_CREDIT, "action_url": ADD}})


def _out_of_credit(monkeypatch, answers, org=None, then=None):
    """A refused twin at a terminal: the organization accounts shows, what the person types, and Checkout paying."""
    config.save({"token": "tok"})
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr(accounts.time, "sleep", lambda s: None)
    monkeypatch.setattr(accounts.webbrowser, "open", lambda url: True)
    asked = []
    said = iter(answers)

    def ask(prompt):
        asked.append(prompt)
        got = next(said)
        if isinstance(got, BaseException):
            raise got
        return got
    monkeypatch.setattr("builtins.input", ask)
    respx.get(f"{A}/credits/orgs").respond(json=[{**ORG, "credits_usd": 0.0, **(org or {})}])
    checkout = respx.post(f"{A}/credits/orgs/acme/checkout").respond(
        json={"url": "https://checkout.stripe.com/c/pay/cs_1", "id": "cs_1"})
    respx.get(f"{A}/credits/orgs/acme/checkout/cs_1").respond(json={"paid": True, "credits_usd": 100.0})
    made, _, _ = _api()
    made.mock(side_effect=[REFUSED, then or httpx.Response(202, json=TWIN)])
    return made, checkout, asked


@respx.mock
def test_out_of_credit_at_a_terminal_pays_and_carries_on(home, monkeypatch, capsys):
    made, checkout, asked = _out_of_credit(monkeypatch, [""])
    assert _run(home, monkeypatch) == 0
    assert asked == ["Add credit now? Amount in $ [100]: "]
    assert json.loads(checkout.calls[0].request.content) == {"usd": 100.0}
    assert made.call_count == 2 and len({c.request.headers["idempotency-key"] for c in made.calls}) == 1
    out = capsys.readouterr()
    assert out.out.splitlines()[:5] == ["acme is out of prepaid credit.", "Pay in the browser:",
                                        "  https://checkout.stripe.com/c/pay/cs_1",
                                        "Paid. acme: $100.00 prepaid credit", "uploaded trace.json (17 bytes)"]
    assert "error" not in out.err and (home / "trace.twin.json").read_text() == '{"user": "Morgan"}'


@pytest.mark.parametrize("answers", [["n"], [KeyboardInterrupt()], [EOFError()], ["0", "lots", "no"]])
def test_declining_the_top_up_is_the_refusal_and_no_retry(home, monkeypatch, capsys, answers):
    with respx.mock:
        made, checkout, asked = _out_of_credit(monkeypatch, answers)
        assert _run(home, monkeypatch) == 1
        assert made.call_count == 1 and not checkout.called and len(asked) == len(answers)
    assert capsys.readouterr().err.endswith(
        f"(code: out_of_credit, request: req_1)\nAdd credit: {ADD}  or run: kennet credits buy --amount <amount>\n")


@respx.mock
def test_someone_who_may_not_buy_is_told_who_can_and_is_not_asked(home, monkeypatch, capsys):
    made, checkout, asked = _out_of_credit(monkeypatch, [], org={"role": "admin", "can_buy": False})
    assert _run(home, monkeypatch) == 1
    assert made.call_count == 1 and not checkout.called and not asked
    assert capsys.readouterr().err.endswith(f"An owner or billing member of acme has to add credit: {ADD}\n")


@pytest.mark.parametrize("argv", [[], ["--json"]])
def test_out_of_credit_away_from_a_terminal_never_asks(home, monkeypatch, capsys, argv):
    with respx.mock:
        made, checkout, asked = _out_of_credit(monkeypatch, [])
        if not argv:
            monkeypatch.setattr(cli, "_interactive", lambda: False)   # a pipe, CI
        assert _run(home, monkeypatch, *argv) == 1
        assert made.call_count == 1 and not checkout.called and not asked
    out = capsys.readouterr()
    assert out.err.endswith(f"Add credit: {ADD}  or run: kennet credits buy --amount <amount>\n")
    if argv:
        error = json.loads(out.out)["error"]
        assert error["action_url"] == ADD and error["next_step"] == "kennet credits buy"


@respx.mock
def test_a_retry_refused_again_stops(home, monkeypatch, capsys):
    made, checkout, asked = _out_of_credit(monkeypatch, ["25"], then=REFUSED)
    assert _run(home, monkeypatch) == 1
    assert made.call_count == 2 and checkout.call_count == 1 and len(asked) == 1
    assert json.loads(checkout.calls[0].request.content) == {"usd": 25.0}
    assert "code: out_of_credit" in capsys.readouterr().err


def test_the_library_raises_with_the_action_url_and_never_asks(home, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: pytest.fail("the library asked"))
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    with respx.mock:
        made, _, _ = _api(created=REFUSED)
        with pytest.raises(errors.BillingError) as e:
            twin.twin(home / "trace.json", say=lambda s: None, sleep=lambda s: None, idempotency_key="k1")
        assert made.calls[0].request.headers["idempotency-key"] == "k1"
    assert e.value.action_url == ADD and e.value.to_dict()["action_url"] == ADD
    assert errors.from_object(OUT_OF_CREDIT).action_url is None
    assert cli._credit_url(errors.from_object({**OUT_OF_CREDIT, "action_url": "https://evil.test/pay"})) == (
        f"{A}/console?add=credit")


@respx.mock
def test_polling_stops_at_its_deadline_naming_the_twin(home, monkeypatch):
    _api(statuses=("running",) * 50)
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    now = [0.0]

    def sleep(s):
        now[0] += s
    with pytest.raises(errors.APIError) as e:
        twin.twin(home / "trace.json", say=lambda s: None, sleep=sleep, deadline_s=30, clock=lambda: now[0])
    assert e.value.code == "timed_out" and e.value.retryable and e.value.run_id == "twin_t1"
    assert e.value.next_step == "kennet twins wait twin_t1" and e.value.next_step in str(e.value)
    assert now[0] <= 30
    assert not (home / "trace.twin.json").exists()
    monkeypatch.setenv("METAPHI_TWIN_TIMEOUT", "90")
    assert twin.deadline() == 90
    monkeypatch.setenv("METAPHI_TWIN_TIMEOUT", "soon")
    with pytest.raises(config.Error):
        twin.deadline()
    monkeypatch.delenv("METAPHI_TWIN_TIMEOUT")
    assert twin.deadline() == twin.DEADLINE_S == 7200


@respx.mock
def test_the_output_is_synced_before_it_takes_its_name(home, monkeypatch):
    _api(statuses=("completed",))
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    order = []
    sync, replace = twin.os.fsync, twin.os.replace
    monkeypatch.setattr(twin.os, "fsync", lambda fd: order.append("fsync") or sync(fd))
    monkeypatch.setattr(twin.os, "replace", lambda a, b: order.append("replace") or replace(a, b))
    twin.twin(home / "trace.json", home / "out.json", say=lambda s: None, sleep=lambda s: None)
    assert order[:2] == ["fsync", "replace"]


def test_twin_timeout_flag_is_seconds_above_zero(home, monkeypatch, capsys):
    (home / "trace.json").write_text("{}")
    with pytest.raises(SystemExit):
        cli.main(["twin", str(home / "trace.json"), "--timeout", "0"])


@respx.mock
def test_no_wait_starts_a_twin_and_twins_wait_comes_back_for_it(home, monkeypatch, capsys):
    made, polls, got = _api(statuses=("running", "running", "completed"))
    assert _run(home, monkeypatch, "--no-wait", "--json") == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == {"id": "twin_t1", "status": "queued", "next_step": "kennet twins wait twin_t1"}
    assert made.call_count == 1 and not polls.called and not (home / "trace.twin.json").exists()
    monkeypatch.chdir(home)
    assert cli.main(["twins", "get", "twin_t1"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "Next: kennet twins wait twin_t1"
    assert cli.main(["twins", "wait", "twin_t1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"output": "trace.twin.json", "id": "twin_t1"}
    assert (home / "trace.twin.json").read_text() == '{"user": "Morgan"}' and got.call_count == 1
    assert made.call_count == 1   # coming back never starts a second run


@respx.mock
def test_twins_list_is_the_organizations_twins_newest_first(home, monkeypatch, capsys):
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    listed = respx.get(f"{API}/twins").respond(json={"object": "list", "has_more": False, "url": "/v1/kennet/twins",
                                                    "data": [{**TWIN, "status": "completed"}]})
    assert cli.main(["twins", "list", "--limit", "3", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"][0]["id"] == "twin_t1"
    assert listed.calls[0].request.url.params["limit"] == "3"
    assert cli.main(["twins", "list"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("twin_t1  completed") and line.endswith("trace.json")


@respx.mock
def test_stopping_the_wait_says_the_twin_goes_on_and_how_to_come_back(home, monkeypatch):
    _api(statuses=("running",) * 5)
    (home / "trace.json").write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")

    def stop(s):
        raise KeyboardInterrupt
    said = []
    with pytest.raises(KeyboardInterrupt):
        twin.twin(home / "trace.json", say=said.append, sleep=stop)
    assert said[-1] == "stopped waiting; twin_t1 goes on. Come back for it: kennet twins wait twin_t1"


def test_help_is_a_command_and_shows_the_examples(capsys):
    assert cli.main(["help"]) == 0
    assert "kennet twins wait" in capsys.readouterr().out


def test_skill_install_writes_a_stub_that_sends_the_agent_to_the_binarys_own_guide(home, capsys):
    assert cli.main(["skill", "install", "--json"]) == 0
    path = home / "claude" / "skills" / "kennet" / "SKILL.md"
    assert json.loads(capsys.readouterr().out) == {"path": str(path), "next_step": "kennet skill show"}
    text = path.read_text()
    assert text.startswith("---\nname: kennet\ndescription: ") and "kennet skill show" in text
    assert cli.main(["skill", "install"]) == 0   # again: ours, so replaced
    assert cli.main(["skill", "install", "--project"]) == 0
    assert (home / ".claude" / "skills" / "kennet" / "SKILL.md").read_text() == text
    capsys.readouterr()
    assert cli.main(["skill", "remove"]) == 0 and not path.parent.exists()
    path.parent.mkdir(parents=True)
    path.write_text("mine")   # someone else's file is never overwritten or removed without being told to
    assert cli.main(["skill", "install"]) == 1 and path.read_text() == "mine"
    assert cli.main(["skill", "remove"]) == 0 and path.read_text() == "mine"
    assert cli.main(["skill", "install", "--force"]) == 0 and path.read_text() == text


def test_the_guide_names_only_commands_and_flags_the_parser_takes(capsys):
    """The guide is the contract an agent follows: every `kennet ...` line in it has to parse."""
    assert cli.main(["skill", "show"]) == 0
    guide = capsys.readouterr().out
    assert guide == skill.GUIDE and f"kennet {cli.__version__}" in guide.splitlines()[0]
    lines = [x.split("#")[0].strip() for x in guide.splitlines() if x.startswith("kennet ")]
    assert len(lines) >= 10
    for line in lines:
        argv = [{"twin_...": "twin_t1", "<dollars>": "5"}.get(w, w) for w in line.split()[1:]]
        cli.parser().parse_args(argv)   # exits on a command or flag that is not there
    for code in ("not_signed_in", "no_api_key", "login_expired", "login_refused", "authorization_pending",
                 "timed_out", "out_of_credit"):
        assert f"`{code}`" in guide   # each code the CLI or the service says has its row


@respx.mock
def test_the_consistency_key_comes_from_a_file_or_the_environment_and_stays_off_the_command_line(home, monkeypatch):
    made, _, _ = _api(statuses=("completed",) * 3)
    sent = lambda: made.calls[-1].request.content
    (home / "k").write_text("ab" * 32 + "\n")
    assert _run(home, monkeypatch, "--key-file", str(home / "k"), "--no-wait") == 0
    assert b"ab" * 32 in sent()
    monkeypatch.setenv("KENNET_CONSISTENCY_KEY", "cd" * 32)
    assert _run(home, monkeypatch, "--no-wait") == 0
    assert b"cd" * 32 in sent()
    assert _run(home, monkeypatch, "--key-file", str(home / "k"), "--no-wait") == 0   # the file named wins
    assert b"ab" * 32 in sent() and b"cd" * 32 not in sent()
    assert _run(home, monkeypatch, "--key-file", str(home / "none"), "--no-wait") == cli.FAILED
    with pytest.raises(SystemExit) as e:   # one or the other
        _run(home, monkeypatch, "--key", "ab" * 32, "--key-file", str(home / "k"))
    assert e.value.code == cli.USAGE


@respx.mock
def test_out_names_the_directory_a_twin_is_written_in(home, monkeypatch, capsys):
    _api(statuses=("completed", "completed", "completed"))
    assert _run(home, monkeypatch, "--out", str(home / "twins" / "today"), "--json") == 0
    out = home / "twins" / "today" / "trace.twin.json"
    assert json.loads(capsys.readouterr().out) == {"output": str(out), "id": "twin_t1"}
    assert out.read_bytes() == b'{"user": "Morgan"}' and not (home / "trace.twin.json").exists()
    assert cli.main(["twins", "wait", "twin_t1", "--out", str(home / "later"), "--json"]) == 0
    assert (home / "later" / "trace.twin.json").exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["twin", "trace.json", "-o", "a.json", "--out", "d"])
    assert e.value.code == cli.USAGE


def test_a_retryable_failed_run_names_the_command_that_twins_the_file_again(home, monkeypatch, capsys):
    failed = {"type": "api_error", "code": "run_unavailable", "retryable": True,
              "message": "the run could not be carried out; nothing was written or charged; upload the trace again "
                         "to retry"}
    with respx.mock:
        _api(statuses=("failed",), error=failed)
        assert _run(home, monkeypatch, "--json", "--test") == cli.LATER
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["run_id"] == "twin_t1" and error["next_step"] == f"kennet twin {home / 'trace.json'} --test"


def test_exit_status_says_what_kind_of_failure(home):
    assert (cli.FAILED, cli.USAGE, cli.AUTH, cli.LATER) == (1, 2, 3, 4)
    assert cli.status(accounts.need("m", "not_signed_in", "kennet login")) == cli.AUTH
    assert cli.status(errors.AuthError("m", code="invalid_api_key")) == cli.AUTH
    assert cli.status(errors.APIError("m", code="auth_unavailable", retryable=True)) == cli.LATER
    assert cli.status(errors.RateLimitError("m", code="rate_limited_daily")) == cli.LATER
    assert cli.status(errors.BillingError("m", code="out_of_credit")) == cli.FAILED
    assert cli.status(errors.RunError("m", code="twin_unclean")) == cli.FAILED
    with pytest.raises(SystemExit) as e:
        cli.main(["twin"])
    assert e.value.code == cli.USAGE
    for line in ("3 sign-in or key needed", "4 try the same call later"):
        assert line in cli.EXAMPLES
    assert "| 3 |" in skill.GUIDE and "| 4 |" in skill.GUIDE


def test_every_error_object_says_its_next_step_even_when_there_is_none():
    assert errors.APIError("m").to_dict()["next_step"] is None
    assert errors.from_object({"type": "api_error", "code": "unavailable", "message": "m"}).to_dict() == {
        "type": "api_error", "code": "unavailable", "message": "m", "retryable": False, "next_step": None}


ACME = {"id": "acme", "name": "Acme", "role": "owner", "grants": ["twin"],
        "members": {"bob@acme.test": "member", "alice@acme.test": "owner"},
        "invited": {"carol@gmail.test": "admin"},
        "invitations": [{"id": "inv_1", "email": "carol@gmail.test", "role": "admin", "scopes": [], "by": "alice@acme.test",
                         "expires": 1_900_000_000}]}
STEP_UP = {"detail": {"code": "step_up_required",
                      "message": "confirm it's you: run `kennet login` again, then retry"}}


@respx.mock
def test_org_members_lists_members_owners_first_and_open_invitations(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[ACME])
    assert cli.main(["org", "members", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "org": "acme", "role": "owner",
        "members": [{"email": "alice@acme.test", "role": "owner"}, {"email": "bob@acme.test", "role": "member"}],
        "invitations": [{"id": "inv_1", "email": "carol@gmail.test", "role": "admin", "expires": 1_900_000_000}]}
    assert cli.main(["org", "members"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "acme (you: owner)" and out[1].split() == ["alice@acme.test", "owner"]
    assert out[3].split()[:4] == ["carol@gmail.test", "admin", "invited,", "until"]
    respx.get(f"{A}/keys/orgs").respond(json=[])   # a member or billing member manages none
    assert cli.main(["org", "members"]) == cli.FAILED
    assert "owner or admin of no organization" in capsys.readouterr().err
    respx.get(f"{A}/keys/orgs").respond(json=[ACME, {**ACME, "id": "beta"}])
    assert cli.main(["org", "members"]) == cli.FAILED
    assert "name one with --org (acme, beta)" in capsys.readouterr().err
    assert cli.main(["org", "members", "--org", "beta"]) == 0


@respx.mock
def test_org_members_add_role_and_remove_call_the_member_route_with_the_bearer(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[ACME])
    put = respx.put(f"{A}/keys/orgs/acme/members/dan@acme.test").respond(
        json={"members": {**ACME["members"], "dan@acme.test": "member"}, "invited": {}})
    assert cli.main(["org", "members", "add", "Dan@Acme.test"]) == 0
    assert capsys.readouterr().out == "dan@acme.test is member of acme\n"
    assert json.loads(put.calls[0].request.content) == {"role": "member"}
    assert put.calls[0].request.headers["authorization"] == "Bearer tok"
    put.respond(json={"members": {**ACME["members"], "dan@acme.test": "billing"}, "invited": {}})
    assert cli.main(["org", "members", "role", "dan@acme.test", "billing", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["status"], got["role"], json.loads(put.calls[1].request.content)) == ("member", "billing",
                                                                                        {"role": "billing"})
    assert {"email": "dan@acme.test", "role": "billing"} in got["members"]
    gone = respx.delete(f"{A}/keys/orgs/acme/members/dan@acme.test").respond(json={"members": ACME["members"]})
    assert cli.main(["org", "members", "remove", "dan@acme.test", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "removed" and gone.call_count == 1
    respx.delete(f"{A}/keys/orgs/acme/members/carol@gmail.test").respond(
        json={"members": ACME["members"], "invited": {}})
    assert cli.main(["org", "members", "remove", "carol@gmail.test"]) == 0
    assert capsys.readouterr().out == "carol@gmail.test's invitation to acme is taken back\n"
    with pytest.raises(SystemExit):   # roles are exactly accounts' four
        cli.main(["org", "members", "add", "dan@acme.test", "--role", "viewer"])


@respx.mock
def test_org_members_add_off_domain_says_the_invitation_is_mailed_and_prints_no_link(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[ACME])
    respx.put(f"{A}/keys/orgs/acme/members/erin@gmail.test").respond(
        json={"members": ACME["members"], "invited": {"erin@gmail.test": "admin"}, "id": "inv_2",
              "message": "invitation mailed to erin@gmail.test"})
    assert cli.main(["org", "members", "add", "erin@gmail.test", "--role", "admin"]) == 0
    assert capsys.readouterr().out == "invitation mailed to erin@gmail.test; they accept it signed in as that address\n"
    assert cli.main(["org", "members", "add", "erin@gmail.test", "--role", "admin", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["status"], got["invitation"], "link" in got) == ("invited", "inv_2", False)
    # a deployment that cannot mail it: the keys page is where the person invites them
    respx.put(f"{A}/keys/orgs/acme/members/erin@gmail.test").respond(
        503, json={"detail": "invitations cannot be mailed from here; invite erin@gmail.test on the keys page"})
    assert cli.main(["org", "members", "add", "erin@gmail.test", "--json"]) == cli.FAILED
    got = json.loads(capsys.readouterr().out)["error"]
    assert got["code"] == "mail_unavailable" and got["message"].endswith(f"on the keys page: {A}/keys")


def test_the_skill_names_every_error_code_the_cli_says_or_acts_on():
    """The codes are read from the source, so the guide and the code cannot drift apart."""
    import re
    from pathlib import Path

    src = "".join(p.read_text() for p in Path(cli.__file__).parent.glob("*.py") if p.name != "skill.py")
    codes = set(re.findall(r'code="([a-z_]+)"', src))
    for call in re.findall(r'\bneed\(((?:[^()]|\([^()]*\))*)\)', src):   # accounts.need(message, code, next_step)
        codes |= set(re.findall(r'"([a-z_]+)",\s*(?:"kennet[^"]*"|None|step)\s*$', call.strip()))
    codes |= set(re.findall(r'"([a-z_]+)",\s*"kennet[^"]*"\)', src))   # any refusal (message, code, "kennet ...")
    codes |= set(re.findall(r'code [!=]= "([a-z_]+)"', src))
    for group in re.findall(r'code in \(([^)]*)\)', src):
        codes |= set(re.findall(r'"([a-z_]+)"', group))
    codes |= set(twin.REASONS)
    codes |= {"unknown_or_expired", "sign_in_too_old"}   # what the approval page says; the CLI never sees them
    assert {"step_up_required", "upgrade_required", "not_signed_in", "no_access", "service_account",
            "invitation_pending", "removed", "no_invitation", "network", "internal"} <= codes
    missing = sorted(c for c in codes if f"`{c}`" not in skill.GUIDE)
    assert not missing, f"kennet skill show does not explain: {missing}"


@respx.mock
def test_server_refusals_on_members_are_one_line_errors(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{**ACME, "role": "admin"}])
    put = respx.put(f"{A}/keys/orgs/acme/members/alice@acme.test")
    put.respond(403, json={"detail": "only an owner makes or changes an owner"})
    assert cli.main(["org", "members", "role", "alice@acme.test", "member"]) == cli.FAILED
    assert capsys.readouterr().err == "error: only an owner makes or changes an owner\n"
    put.respond(409, json={"detail": "the organization keeps one owner; make another owner first"})
    assert cli.main(["org", "members", "role", "alice@acme.test", "member", "--json"]) == cli.FAILED
    got = capsys.readouterr()
    assert json.loads(got.out)["error"]["message"] == "the organization keeps one owner; make another owner first"


@respx.mock
def test_a_stale_sign_in_is_step_up_required_and_the_next_login_signs_in_afresh(monkeypatch, capsys):
    config.save({"token": "tok", "email": "alice@acme.test"})
    respx.get(f"{A}/keys/orgs").respond(json=[ACME])
    respx.put(f"{A}/keys/orgs/acme/members/dan@acme.test").respond(401, json=STEP_UP)
    assert cli.main(["org", "members", "add", "dan@acme.test", "--json"]) == cli.AUTH
    got = json.loads(capsys.readouterr().out)["error"]
    assert (got["code"], got["next_step"]) == ("step_up_required", "kennet login")
    assert got["message"] == "confirm it's you: run `kennet login` again, then retry"
    # `kennet login` does not pick the keys up by the stale token: it starts a new sign-in
    pick = respx.post(f"{A}/keys/cli").respond(json={"email": "alice@acme.test"})
    respx.post(f"{A}/device/code").respond(json=CODE)
    assert cli.main(["login", "--no-browser", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending" and pick.call_count == 0
    respx.post(f"{A}/oauth/token").respond(json=SIGNED)
    cli.main(["login", "--complete", "--json"])
    assert "step_up" not in config.load()


@respx.mock
def test_at_a_terminal_a_stale_sign_in_offers_a_fresh_one_and_retries_once(monkeypatch, capsys):
    config.save({"token": "tok"})
    monkeypatch.setattr(cli, "_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    signed = []
    monkeypatch.setattr(accounts, "login", lambda: signed.append(1) or SIGNED)
    respx.get(f"{A}/keys/orgs").respond(json=[ACME])
    put = respx.put(f"{A}/keys/orgs/acme/members/dan@acme.test").mock(side_effect=[
        httpx.Response(401, json=STEP_UP), httpx.Response(200, json={"members": {"dan@acme.test": "member"}})])
    assert cli.main(["org", "members", "add", "dan@acme.test"]) == 0
    assert signed == [1] and put.call_count == 2
    assert capsys.readouterr().out.endswith("dan@acme.test is member of acme\n")
    put.mock(side_effect=[httpx.Response(401, json=STEP_UP), httpx.Response(401, json=STEP_UP)])
    assert cli.main(["org", "members", "add", "dan@acme.test"]) == cli.AUTH   # once: a second refusal is the error
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    put.mock(side_effect=[httpx.Response(401, json=STEP_UP)])
    assert cli.main(["org", "members", "add", "dan@acme.test"]) == cli.AUTH and signed == [1, 1]


INVITED = {"email": "bob@gmail.test", "no_api_key_code": "invitation_pending",
           "no_api_key": "Acme Robotics has invited you to join it as member; accept with `kennet org join acme`",
           "invitation": {"id": "inv_9", "org": "acme", "org_name": "Acme Robotics", "role": "member",
                          "expires": 1_900_000_000}}


@respx.mock
def test_login_with_an_invitation_waiting_names_org_join_and_never_org_create(monkeypatch, capsys):
    assert _login(monkeypatch, **{k: v for k, v in INVITED.items() if k != "email"}) == cli.AUTH
    got = capsys.readouterr()
    assert got.out.endswith("Signed in as alice@example.test.\n") and "org create" not in got.err
    assert "has invited you to join it as member" in got.err
    assert got.err.endswith("Next: kennet org join acme\n")
    assert config.load()["invitation"]["org"] == "acme"
    respx.post(f"{A}/keys/cli").respond(json=INVITED)   # signed in already: the pickup says the same
    assert cli.main(["login", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert (seen["error"]["code"], seen["error"]["next_step"], seen["reason"]) == (
        "no_api_key", "kennet org join acme", "invitation_pending")
    assert seen["invitation"] == INVITED["invitation"]
    # an older accounts that says only `no_organization`, with the invitation beside it: joining, still
    respx.post(f"{A}/keys/cli").respond(json={**INVITED, "no_api_key_code": "no_organization",
                                              "no_api_key": "bob@gmail.test is not in an organization"})
    assert cli.main(["login", "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["error"]["next_step"] == "kennet org join acme"


@respx.mock
def test_org_join_accepts_the_invitation_by_bearer_then_picks_the_keys_up(monkeypatch, capsys):
    config.save({"token": "tok", "email": "bob@gmail.test", "invitation": INVITED["invitation"]})
    accept = respx.post(f"{A}/keys/invitations/acme").respond(json={"org": "acme", "role": "member"})
    pick = respx.post(f"{A}/keys/cli").respond(json={"email": "bob@gmail.test", "org": "acme",
                                                     "api_key": "mai_sk_live", "test_api_key": "mai_sk_test_t"})
    assert cli.main(["org", "join", "acme"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("You joined acme as member.\nSigned in as bob@gmail.test.\nReady for acme")
    assert accept.calls[0].request.headers["authorization"] == "Bearer tok" and pick.call_count == 1
    cfg = config.load()
    assert cfg["api_key"] == "mai_sk_live" and "invitation" not in cfg
    # by the invitation's id, as accounts lists it now; a saved invitation is never what decides
    listed = respx.get(f"{A}/keys/invitations").respond(json=[
        {"id": "inv_9", "org": "acme", "name": "Acme Robotics", "role": "member"},
        {"id": "inv_7", "org": "beta", "name": "Beta", "role": "admin"}])
    config.save({**cfg, "invitation": {**INVITED["invitation"], "id": "inv_7"}})
    assert cli.main(["org", "join", "inv_9", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["keys"], got["joined"]) == (True, {"org": "acme", "role": "member"})
    assert json.loads(accept.calls[-1].request.content) == {"id": "inv_9"}
    beta = respx.post(f"{A}/keys/invitations/beta").respond(json={"org": "beta", "role": "admin"})
    assert cli.main(["org", "join", "inv_7", "--json"]) == 0 and beta.call_count == 1
    capsys.readouterr()
    assert cli.main(["org", "join", "inv_0", "--json"]) == cli.FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_invitation"
    # an accounts that lists no ids: the one invitation open is the one meant; two are not guessed between
    listed.respond(json=[{"org": "beta", "name": "Beta", "role": "admin"}])
    assert cli.main(["org", "join", "inv_7", "--json"]) == 0 and beta.call_count == 2
    capsys.readouterr()
    listed.respond(json=[{"org": "beta", "name": "Beta"}, {"org": "acme", "name": "Acme"}])
    assert cli.main(["org", "join", "inv_7", "--json"]) == cli.FAILED
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "no_invitation"
    # gone: expired or taken back; who can send a new one
    accept.respond(404, json={"detail": "no invitation from acme for bob@gmail.test"})
    assert cli.main(["org", "join", "acme"]) == cli.FAILED
    err = capsys.readouterr().err
    assert err.startswith("error: no invitation from acme for bob@gmail.test; it may have expired")
    assert err.endswith("Next: ask an owner of acme to invite you again\n")
    # a sign-in too old to change an organization: the same refusal as for members, either shape
    for step_up in (STEP_UP, {"detail": "confirm it's you: run `kennet login` again, then retry",
                              "code": "step_up_required"}):
        accept.respond(401, json=step_up)
        assert cli.main(["org", "join", "acme", "--json"]) == cli.AUTH
        got = json.loads(capsys.readouterr().out)["error"]
        assert (got["code"], got["next_step"]) == ("step_up_required", "kennet login")
        assert got["message"] == "confirm it's you: run `kennet login` again, then retry"


REMOVED = {"email": "bob@acme.test", "org": "acme", "no_api_key_code": "removed",
           "no_api_key": "an owner of Acme took you out of it"}


@respx.mock
def test_a_removed_member_is_told_the_servers_sentence_and_who_can_add_them_back(monkeypatch, capsys):
    assert _login(monkeypatch, **{k: v for k, v in REMOVED.items() if k != "email"}) == cli.AUTH
    got = capsys.readouterr()
    assert got.err == ("error: no key: an owner of Acme took you out of it (code: no_api_key)\n"
                       "Next: ask an owner of acme to add you back\n")
    assert "kennet login" not in got.out and "org create" not in got.out + got.err
    respx.post(f"{A}/keys/cli").respond(json=REMOVED)
    assert cli.main(["login", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert (seen["error"]["next_step"], seen["reason"]) == (None, "removed")


@respx.mock
def test_whoami_says_why_a_saved_key_is_refused_from_reads_alone(capsys):
    """A status command never changes a key: no POST, so no machine's keys are replaced."""
    config.save({"token": "tok", "email": "bob@acme.test", "org": "acme", "api_key": "mai_sk_live_abcd",
                 "test_api_key": "mai_sk_test_wxyz"})
    orgs = respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "name": "Acme", "role": "member",
                                                       "admission": "admitted", "grants": ["twin"]}])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    invitations = respx.get(f"{A}/keys/invitations").respond(json=[])
    respx.get(f"{API}/twins").respond(401, json={"error": {"type": "auth_error", "code": "invalid_api_key",
                                                           "message": "that key is unknown, revoked or mistyped"}})
    posted = respx.post(url__regex=r".*").respond(500)
    # in an organization: the key was replaced by a sign-in on another machine
    assert cli.main(["whoami"]) == cli.AUTH
    got = capsys.readouterr()
    assert "mistyped" not in got.out + got.err
    assert got.err.endswith("error: this machine's key was replaced by a sign-in elsewhere; run `kennet login` "
                            "(code: invalid_api_key)\nNext: kennet login\n")
    # in none, and invited to none
    orgs.respond(json=[])
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == ("this machine's key no longer works and you're not in an organization; run "
                                "`kennet login` to see why")
    assert error["next_step"] == "kennet login"
    # invited: the invitation is the answer, never `org create`
    invitations.respond(json=[{"org": "beta", "name": "Beta", "role": "member", "expires": 1_900_000_000}])
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert seen["error"]["next_step"] == "kennet org join beta" and seen["invitation"]["org"] == "beta"
    assert not posted.called and config.load()["api_key"] == "mai_sk_live_abcd"


@respx.mock
def test_whoami_with_no_key_names_an_invitation_sent_since_the_sign_in(capsys):
    config.save({"token": "tok", "email": "bob@gmail.test", "no_api_key_code": "no_organization",
                 "no_api_key": "bob@gmail.test is not in an organization"})
    respx.get(f"{A}/keys/orgs").respond(json=[])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    respx.get(f"{A}/keys/invitations").respond(json=[{"org": "acme", "name": "Acme Robotics", "role": "member",
                                                      "expires": 1_900_000_000}])
    assert cli.main(["whoami"]) == cli.AUTH
    got = capsys.readouterr()
    assert "invite: Acme Robotics has invited you to join it as member" in got.out
    assert got.err.endswith("Next: kennet org join acme\n") and "org create" not in got.out + got.err


def test_login_json_never_opens_a_browser(monkeypatch, capsys):
    opened = []
    monkeypatch.setattr(accounts.webbrowser, "open", opened.append)
    with respx.mock:
        respx.post(f"{A}/device/code").respond(json=CODE)
        assert cli.main(["login", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pending" and opened == []


@respx.mock
def test_a_network_failure_is_one_line_retryable_and_never_a_traceback(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").mock(side_effect=httpx.ConnectTimeout("timed out"))
    assert cli.main(["org", "members", "--json"]) == cli.LATER
    got = capsys.readouterr()
    error = json.loads(got.out)["error"]
    assert (error["code"], error["retryable"], error["type"]) == ("network", True, "api_error")
    host = httpx.URL(A).host
    assert error["message"] == f"can't reach {host}: the connection timed out"
    assert "Traceback" not in got.err and got.err.startswith(f"error: can't reach {host}:")
    respx.get(f"{A}/keys/orgs").mock(side_effect=httpx.ConnectError("nodename nor servname provided"))
    assert cli.main(["org", "members"]) == cli.LATER
    err = capsys.readouterr().err
    assert err.splitlines() == [f"error: can't reach {host}: no connection (code: network)",
                                "Next: check your connection, then run the same command again."]


def test_anything_unexpected_is_one_line_and_a_debug_log_never_a_traceback(monkeypatch, capsys):
    def broken(a):
        raise KeyError("org")
    monkeypatch.setattr(cli, "cmd_whoami", broken)
    for json_flag in ([], ["--json"]):
        assert cli.main(["whoami", *json_flag]) == cli.FAILED
        got = capsys.readouterr()
        assert "Traceback" not in got.out + got.err
        lines = got.err.splitlines()
        assert lines[0] == "error: kennet stopped on something it did not expect (KeyError) (code: internal)"
        assert lines[1].startswith("This is a bug; please report it with `kennet --version` and the debug log: ")
    seen = json.loads(got.out)
    assert seen["error"]["code"] == "internal" and seen["error"]["retryable"] is False
    log = config.home() / "debug.log"
    assert seen["debug_log"] == str(log) and lines[1].endswith(str(log))
    assert "Traceback" in log.read_text() and "KeyError: 'org'" in log.read_text()


@respx.mock
def test_org_create_says_what_accounts_says_comes_next(capsys):
    config.save({"token": "tok"})
    waits = "Metaphi AI approves new organizations; you're emailed when it's done, then run `kennet login`"
    made = respx.post(f"{A}/keys/orgs").respond(json={"id": "acme", "name": "Acme", "role": "owner",
                                                      "admission": "pending", "message": waits, "next_step": None})
    assert cli.main(["org", "create", "Acme"]) == 0
    assert capsys.readouterr().out == f"{waits}\n"   # no `Next:`: nothing to run until the mail comes
    made.respond(json={"id": "acme", "name": "Acme", "role": "owner", "admission": "admitted",
                       "message": "Acme is made and approved.", "next_step": "kennet login"})
    assert cli.main(["org", "create", "Acme", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert (got["message"], got["next_step"]) == ("Acme is made and approved.", "kennet login")


@respx.mock
def test_members_commands_act_only_on_organizations_the_person_manages_and_whoami_shows_every_one(capsys):
    config.save({"token": "tok", "email": "alice@acme.test", "org": "acme", "api_key": "mai_sk_live_abcd"})
    beta = {"id": "beta", "name": "Beta", "role": "member", "admission": "admitted", "grants": ["twin"]}
    respx.get(f"{A}/keys/orgs").respond(json=[beta, ACME])
    assert cli.main(["org", "members", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["org"] == "acme"
    assert cli.main(["org", "members", "--org", "beta"]) == cli.FAILED
    assert capsys.readouterr().err == ("error: only owners and admins of beta see its members; you're a member "
                                       "(code: not_manager)\n")
    respx.get(f"{A}/credits/orgs").respond(json=[])
    respx.get(f"{A}/keys/invitations").respond(json=[])
    respx.get(f"{API}/twins").respond(json={"object": "list", "data": []})
    assert cli.main(["whoami"]) == 0
    assert "org:    beta (member) holds twin" in capsys.readouterr().out


@respx.mock
def test_org_join_by_id_names_the_invitation_and_another_address_is_told_to_sign_in_as_it(capsys):
    config.save({"token": "tok", "email": "bob@gmail.test"})
    respx.get(f"{A}/keys/invitations").respond(json=[{"id": "inv_9", "org": "acme", "name": "Acme"}])
    accept = respx.post(f"{A}/keys/invitations/acme").respond(403, json={"detail": {
        "code": "invitation_for_another_address", "message": "this invitation was sent to bob@acme.test"}})
    assert cli.main(["org", "join", "inv_9"]) == cli.FAILED
    assert json.loads(accept.calls[0].request.content) == {"id": "inv_9"}
    then = "run `kennet logout`, then `kennet login` as the address the invitation was sent to"
    assert capsys.readouterr().err == (
        "error: this invitation was sent to bob@acme.test (code: invitation_for_another_address)\n"
        f"Next: {then}\n")
    assert cli.main(["org", "join", "inv_9", "--json"]) == cli.FAILED   # the same next step under --json
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["next_step"], error["advice"]) == ("kennet logout", then)
    assert cli.main(["org", "join", "acme"]) == cli.FAILED
    assert accept.calls[-1].request.content == b""   # by the organization: the invitation open to this address


GATEWAY = "<html><head><title>502 Bad Gateway</title></head><body><center>nginx</center></body></html>"


@respx.mock
def test_a_proxys_page_is_never_the_message_and_a_gateway_error_is_worth_retrying(capsys):
    config.save({"token": "tok"})
    orgs = respx.get(f"{A}/keys/orgs").respond(502, text=GATEWAY, headers={"retry-after": "20"})
    assert cli.main(["org", "members", "--json"]) == cli.LATER
    got = capsys.readouterr()
    error = json.loads(got.out)["error"]
    assert error == {"type": "api_error", "code": "service_unavailable", "retryable": True, "next_step": None,
                     "message": "the service answered HTTP 502; try again in a minute", "retry_after_s": 20.0}
    assert "nginx" not in got.out + got.err and "html" not in got.err
    for status in (503, 504):
        orgs.respond(status, text="upstream timed out")
        assert cli.main(["org", "members"]) == cli.LATER
        assert capsys.readouterr().err.startswith(f"error: the service answered HTTP {status}; try again in a minute")
    orgs.respond(418, text="<html>teapot</html>")   # any other page: the status, never the page, and no retry
    assert cli.main(["org", "members", "--json"]) == cli.FAILED
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["message"], error["code"], error["retryable"]) == (
        "the service answered HTTP 418; try again in a minute", None, False)
    # a 200 that is not JSON, as a captive portal answers: the same retryable error, never `internal`
    orgs.respond(200, text="<html>sign in to the hotel wifi</html>")
    assert cli.main(["org", "members", "--json"]) == cli.LATER
    error = json.loads(capsys.readouterr().out)["error"]
    assert (error["code"], error["message"]) == ("service_unavailable",
                                                 "the service answered HTTP 200; try again in a minute")
    respx.post(f"{A}/device/code").respond(502, text=GATEWAY)   # the sign-in's first call too
    config.save({})
    assert cli.main(["login", "--json"]) == cli.LATER
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "service_unavailable"


@respx.mock
def test_a_pickup_that_fails_is_the_error_and_never_a_fresh_sign_in(capsys):
    config.save({"token": "tok", "email": "alice@example.test"})
    pick = respx.post(f"{A}/keys/cli").respond(502, text=GATEWAY)
    device = respx.post(f"{A}/device/code").respond(json=CODE)
    assert cli.main(["login", "--json"]) == cli.LATER
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "service_unavailable"
    assert not device.called and config.load()["token"] == "tok"
    pick.respond(500, json={"detail": "the database is down"})
    assert cli.main(["login", "--json"]) == cli.FAILED and not device.called
    assert json.loads(capsys.readouterr().out)["error"]["message"] == "the database is down"
    for status in (401, 403):   # the sign-in has ended: a fresh one
        pick.respond(status, json={"detail": "sign in"})
        assert cli.main(["login", "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "pending"
        config.save({"token": "tok"})
    assert device.call_count == 2


@respx.mock
def test_whoami_lists_the_invitations_accounts_has_now_never_the_saved_one(capsys):
    stale = {"id": "inv_old", "org": "gone", "org_name": "Gone", "role": "member", "expires": 1_900_000_000}
    config.save({"token": "tok", "email": "bob@gmail.test", "no_api_key_code": "invitation_pending",
                 "no_api_key": "Gone has invited you", "invitation": stale})
    orgs = respx.get(f"{A}/keys/orgs").respond(json=[])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    listed = respx.get(f"{A}/keys/invitations").respond(json=[
        {"id": "inv_1", "org": "acme", "name": "Acme", "role": "member", "expires": 1_900_000_000},
        {"id": "inv_2", "org": "beta", "name": "Beta", "role": "admin"}])
    assert cli.main(["whoami"]) == cli.AUTH
    got = capsys.readouterr()
    assert "invite: Acme has invited you to join it as member, until" in got.out and "(inv_1)" in got.out
    assert "invite: Beta has invited you to join it as admin. (inv_2)" in got.out and "Gone" not in got.out
    assert got.err.endswith("Next: kennet org join acme\n")
    listed.respond(json=[])   # accounts has none open: the saved one is not shown
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert "invitations" not in seen and seen["error"]["next_step"] != "kennet org join gone"
    # accounts cannot say: the saved one stands in, unless it has expired or the person is in an organization
    listed.respond(500, json={"detail": "down"})
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    assert json.loads(capsys.readouterr().out)["invitations"][0]["id"] == "inv_old"
    config.save({**config.load(), "invitation": {**stale, "expires": 1_000}})
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    assert "invitations" not in json.loads(capsys.readouterr().out)
    config.save({**config.load(), "invitation": stale})
    orgs.respond(json=[{"id": "acme", "role": "member", "grants": []}])
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    assert "invitations" not in json.loads(capsys.readouterr().out)


def test_twin_names_a_saved_invitation_only_until_it_expires_and_sends_to_login(home, capsys):
    (home / "t.json").write_text("{}")
    invitation = {"id": "inv_1", "org": "acme", "org_name": "Acme", "role": "member", "expires": 1_900_000_000}
    config.save({"token": "tok", "email": "bob@gmail.test", "no_api_key_code": "invitation_pending",
                 "no_api_key": "Acme has invited you to join it", "invitation": invitation})
    assert cli.main(["twin", str(home / "t.json"), "--json"]) == cli.AUTH
    got = json.loads(capsys.readouterr().out)
    assert (got["error"]["next_step"], got["reason"]) == ("kennet login", "invitation_pending")   # it asks again
    config.save({**config.load(), "no_api_key_code": None, "invitation": {**invitation, "expires": 1_000}})
    assert cli.main(["twin", str(home / "t.json"), "--json"]) == cli.AUTH
    got = json.loads(capsys.readouterr().out)
    assert got["reason"] != "invitation_pending" and "invitation" not in got


def test_an_interrupt_under_json_is_still_one_json_object(monkeypatch, capsys):
    def stop(a):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "cmd_whoami", stop)
    assert cli.main(["whoami", "--json"]) == 130
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "interrupted"
    assert cli.main(["whoami"]) == 130 and capsys.readouterr().out == ""


def test_the_debug_log_never_holds_a_key(monkeypatch, capsys):
    held, flagged = (config.KEY_PREFIX + "live_" + tag for tag in ("canary1", "canary2"))   # made here, never written

    def broken(a):
        raise RuntimeError(f"refused {held} for this call")
    monkeypatch.setattr(cli, "cmd_twin", broken)
    monkeypatch.setattr(cli.sys, "argv", ["kennet", "twin", "t.json", "--api-key", flagged,
                                          "--key=consistency1", "--key", "consistency2"])
    assert cli.main(["twin", "t.json", "--api-key", flagged]) == cli.FAILED
    capsys.readouterr()
    log = (config.home() / "debug.log").read_text()
    assert "canary" not in log and "consistency" not in log
    assert "--api-key <redacted>" in log and "--key=<redacted>" in log and "mai_sk_<redacted>" in log


@respx.mock
def test_a_login_handed_no_key_drops_the_saved_one_and_whoami_says_the_same_reason(monkeypatch, capsys):
    """Removed, then login, then whoami: the reason login was given, said again, never back to `kennet login`."""
    config.save({"token": "old", "email": "bob@acme.test", "org": "acc-test-1015", "api_key": "mai_sk_live_TuyE",
                 "test_api_key": "mai_sk_test_TuyF"})
    why = "you were removed from Acc Test 1015; ask one of its owners to add you back"
    pick = respx.post(f"{A}/keys/cli").respond(json={"email": "bob@acme.test", "org": "acc-test-1015",
                                                     "no_api_key": why, "no_api_key_code": "removed"})
    assert cli.main(["login"]) == cli.AUTH and pick.call_count == 1
    capsys.readouterr()
    cfg = config.load()
    assert not {"api_key", "test_api_key", "org"} & cfg.keys()
    assert (cfg["no_api_key_code"], cfg["no_api_key"]) == ("removed", why)
    respx.get(f"{A}/keys/orgs").respond(json=[])
    respx.get(f"{A}/credits/orgs").respond(json=[])
    respx.get(f"{A}/keys/invitations").respond(json=[])
    twins = respx.get(f"{API}/twins").respond(json={"object": "list", "data": []})
    posted = respx.post(url__regex=r".*").respond(500)
    assert cli.main(["whoami"]) == cli.AUTH
    got = capsys.readouterr()
    assert "kennet login" not in got.out + got.err and "acc-test-1015" not in got.out
    assert "key:    none" in got.out and "invalid_api_key" not in got.out
    assert got.err == f"error: no key: {why} (code: no_api_key)\nNext: ask an owner of acc-test-1015 to add you back\n"
    assert cli.main(["whoami", "--json"]) == cli.AUTH
    seen = json.loads(capsys.readouterr().out)
    assert (seen["reason"], seen["error"]["next_step"], seen["key"]) == ("removed", None, None)
    assert not posted.called and not twins.called and pick.call_count == 1


def test_the_free_test_run_used_names_the_live_run_of_the_same_file(home, monkeypatch, capsys):
    (home / "t.json").write_text("{}")
    config.save({"token": "tok", "api_key": "mai_sk_live", "test_api_key": "mai_sk_test_t"})
    used = {"type": "rate_limit_error", "code": "test_runs_used", "retryable": False,
            "message": "this organization has used its 1 free test run(s); run it with your live key"}
    with respx.mock:
        respx.post(f"{API}/twins").respond(429, json={"error": used})
        assert cli.main(["twin", "t.json", "--test", "--json"]) == cli.FAILED
        error = json.loads(capsys.readouterr().out)["error"]
        assert (error["code"], error["next_step"], error["retryable"]) == ("test_runs_used", "kennet twin t.json",
                                                                           False)
        assert error["message"] == "its free test run is used; run it live: kennet twin t.json"
        assert cli.main(["twin", "t.json", "--test"]) == cli.FAILED
        assert capsys.readouterr().err.endswith("Next: kennet twin t.json\n")


@respx.mock
def test_a_plain_member_asking_for_members_is_told_who_may(capsys):
    config.save({"token": "tok"})
    org = {"id": "acme", "name": "Acme", "role": "member", "admission": "admitted", "grants": ["twin"]}
    listed = respx.get(f"{A}/keys/orgs").respond(json=[org])
    assert cli.main(["org", "members"]) == cli.FAILED
    assert capsys.readouterr().err == "error: only owners and admins of acme see its members; you're a member " \
                                      "(code: not_manager)\n"   # no `Next:`: no command clears it
    listed.respond(json=[{**org, "role": "billing", "owners": ["alice@acme.test"]}])
    assert cli.main(["org", "members", "--json"]) == cli.FAILED
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["message"] == ("only owners and admins of acme see its members; you're a billing member; its "
                                "owners: alice@acme.test")
    assert error["next_step"] is None
    listed.respond(json=[])   # in no organization at all: the sentence as before
    assert cli.main(["org", "members"]) == cli.FAILED
    assert "owner or admin of no organization" in capsys.readouterr().err
