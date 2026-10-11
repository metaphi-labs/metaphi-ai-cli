"""The CLI against stand-ins for accounts and the twin API: sign-in, keys, and one trace end to end."""
import json

import httpx
import pytest
import respx

from metaphi import accounts, cli, config, errors, twin

A = config.DEFAULT_ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_NO_KEYRING", "1")
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
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
    assert json.loads(capsys.readouterr().out) == {"output": str(home / "trace.twin.json")}


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
        assert _run(home, monkeypatch, "--json") == 1
        assert not got.called
    out = capsys.readouterr()
    assert json.loads(out.out) == {"error": failed}
    assert "(code: twin_unclean)" in out.err
    assert [p.name for p in home.iterdir() if p.is_file()] == ["trace.json"]


OUT_OF_CREDIT = {"type": "billing_error", "code": "out_of_credit", "retryable": False, "request_id": "req_1",
                 "message": "This organization's prepaid balance is used up; add credits to continue."}


def test_a_refusal_raises_its_type_and_keeps_its_code(home, monkeypatch, capsys):
    with respx.mock:
        _api(created=httpx.Response(402, json={"error": OUT_OF_CREDIT}, headers={"request-id": "req_1"}))
        assert _run(home, monkeypatch) == 1
    err = capsys.readouterr().err
    assert err.endswith(f"error: {OUT_OF_CREDIT['message']} (code: out_of_credit, request: req_1)\n")
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
        assert _run(home, monkeypatch, "--json") == 1
        assert made.call_count == 1          # a rate limit is the caller's to wait out
    assert json.loads(capsys.readouterr().out) == {"error": limited}


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
    assert slept[:2] == [1.0, 2.0]


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


@respx.mock
def test_keys_create_test_makes_a_test_mode_key(capsys):
    config.save({"token": "tok"})
    respx.get(f"{A}/keys/orgs").respond(json=[{"id": "acme", "role": "owner", "grants": ["kennet:twin"],
                                               "members": {}, "keys": []}])
    made = respx.post(f"{A}/keys/orgs/acme").respond(json={"id": "key_2", "name": "dev", "key": "mai_sk_test_x",
                                                           "scopes": ["kennet:twin"]})
    assert cli.main(["keys", "create", "dev", "--test"]) == 0
    assert json.loads(made.calls[0].request.content) == {"name": "dev", "scopes": ["kennet:twin"], "test": True}
    assert "test-mode key_2" in capsys.readouterr().out


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
