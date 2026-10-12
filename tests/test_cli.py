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
    monkeypatch.setenv("METAPHI_NO_KEYRING", "1")
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
    assert out.splitlines()[-1] == f"Usage, keys, members and billing: {A}/console"
    assert "mai_sk_" not in out


@respx.mock
def test_login_with_no_key_handed_over_says_why_and_still_signs_in(monkeypatch, capsys):
    config.save({"api_key": "mai_sk_mine", "test_api_key": "mai_sk_test_old"})
    # signed in, and no key: the one refusal every command makes of it, here with nothing left to run
    assert _login(monkeypatch, no_api_key="Acme has no synthetic twinning access yet; ask Metaphi AI for it") == cli.AUTH
    got = capsys.readouterr()
    out = got.out
    assert "No key was saved: Acme has no synthetic twinning access yet" in out and "Ready for" not in out
    assert out.splitlines()[-1] == f"Usage, keys, members and billing: {A}/console"
    assert "Next:" not in out + got.err and "(code: no_api_key)" in got.err
    cfg = config.load()   # signed in; a key saved by hand stays, the last login's test key is gone
    assert cfg["token"] == "tok" and cfg["api_key"] == "mai_sk_mine" and "test_api_key" not in cfg


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
                                   "console": f"{A}/console", "next_step": "kennet twin <file> --test"}
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
    assert cli.main(["whoami", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "email": "alice@example.test", "org": "acme",
        "organizations": [{"id": "acme", "role": "owner", "grants": ["twin"], "credits_usd": 12.5, "can_buy": True}],
        "key": {"source": "saved", "hint": "…abcd", "status": "ok"},
        "test_key": {"source": "saved", "hint": "…wxyz", "status": "ok"}, "console": f"{A}/console"}
    # each key was asked of the twin service, as itself
    assert [c.request.headers["authorization"] for c in checked.calls] == ["Bearer mai_sk_live_abcd",
                                                                           "Bearer mai_sk_test_wxyz"]
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
def test_whoami_does_not_call_a_key_fine_that_the_twin_service_refuses(capsys):
    config.save({"token": "tok", "email": "alice@example.test", "org": "acme", "api_key": "mai_sk_live_abcd",
                 "test_api_key": "mai_sk_test_wxyz"})
    respx.get(f"{A}/keys/orgs").respond(json=[])
    respx.get(f"{A}/credits/orgs").respond(json=[])
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
