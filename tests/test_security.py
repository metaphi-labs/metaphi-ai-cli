"""Hosts the CLI will talk to, how it keeps secrets, and how it takes a key."""
import io
import json
import os
import stat

import httpx
import pytest
import respx

from metaphi import accounts, cli, config, twin

A = config.DEFAULT_ACCOUNTS


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("METAPHI_NO_KEYRING", "1")
    for var in ("METAPHI_API_KEY", "METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST"):
        monkeypatch.delenv(var, raising=False)
    config._warned.clear()
    return tmp_path


# Hosts

@pytest.mark.parametrize("url", [
    "https://app.metaphi.ai", "https://metaphi.ai/x", "https://a.b.metaphi.ai/mcp/", "https://APP.METAPHI.AI."])
def test_metaphi_hosts_over_https_pass(url):
    assert config.check_url(url, "u") == url


@pytest.mark.parametrize("url", [
    "http://app.metaphi.ai", "https://evil.test", "https://metaphi.ai.evil.test", "https://evilmetaphi.ai",
    "https://app.metaphi.ai@evil.test/", "https://user:pw@app.metaphi.ai/", "ftp://app.metaphi.ai", "app.metaphi.ai",
    "", None, 7])
def test_other_hosts_and_schemes_are_refused(url):
    with pytest.raises(config.Error):
        config.check_url(url, "u")


def test_custom_host_needs_the_opt_in_and_still_needs_https(monkeypatch, capsys):
    monkeypatch.setenv("METAPHI_ACCOUNTS", "https://accounts.example.test")
    with pytest.raises(config.Error, match="METAPHI_ALLOW_CUSTOM_HOST"):
        config.accounts_url()
    monkeypatch.setenv("METAPHI_ALLOW_CUSTOM_HOST", "1")
    assert config.accounts_url() == "https://accounts.example.test"
    assert "warning: METAPHI_ACCOUNTS" in capsys.readouterr().err
    assert config.check_url("http://localhost:8000/", "u")
    with pytest.raises(config.Error):
        config.check_url("http://accounts.example.test", "u")


def test_a_bad_endpoint_is_an_error_not_a_traceback(monkeypatch, capsys):
    monkeypatch.setenv("METAPHI_ACCOUNTS", "http://app.metaphi.ai")
    config.save({"token": "tok"})
    assert cli.main(["keys", "list"]) == 1
    assert "must be https" in capsys.readouterr().err


def test_default_hosts_say_nothing(capsys):
    config.accounts_url(), config.api_url()
    assert capsys.readouterr().err == ""


def test_an_api_off_metaphi_is_refused_before_any_bytes_leave(home, monkeypatch):
    trace = home / "trace.json"
    trace.write_text("{}")
    monkeypatch.setenv("METAPHI_API", "https://collector.example.test/v1")
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_live")
    with respx.mock(assert_all_called=False) as m:
        put = m.post("https://collector.example.test/v1/twins").respond(202)
        with pytest.raises(config.Error, match="METAPHI_ALLOW_CUSTOM_HOST"):
            twin.twin(trace, say=lambda s: None)
    assert not put.called


def test_a_key_that_is_not_an_organization_key_never_leaves(home, monkeypatch):
    trace = home / "trace.json"
    trace.write_text("{}")
    monkeypatch.setenv("METAPHI_API_KEY", "sk_live_other")
    with respx.mock(assert_all_called=False) as m:
        made = m.post(f"{config.DEFAULT_API}/twins").respond(202)
        with pytest.raises(config.Error, match="not an organization key"):
            twin.twin(trace, say=lambda s: None)
    assert not made.called


# Device sign-in

@respx.mock
def test_a_non_json_device_answer_is_an_error():
    respx.post(f"{A}/device/code").respond(200, text="<html>gateway</html>")
    with pytest.raises(accounts.Error):
        accounts.login(say=lambda s: None, open_browser=False, sleep=lambda s: None)


@respx.mock
def test_a_non_json_poll_answer_is_an_error():
    respx.post(f"{A}/device/code").respond(json={"device_code": "dc", "user_code": "U", "interval": 1,
                                                 "expires_in": 60, "verification_uri": f"{A}/device"})
    respx.post(f"{A}/oauth/token").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(accounts.Error, match="bad gateway"):
        accounts.login(say=lambda s: None, open_browser=False, sleep=lambda s: None)


def _sign_in(code_body, monkeypatch):
    opened, said = [], []
    monkeypatch.setattr(accounts.webbrowser, "open", opened.append)
    respx.post(f"{A}/device/code").respond(json=code_body)
    respx.post(f"{A}/oauth/token").respond(json={"access_token": "tok"})
    accounts.login(say=said.append, open_browser=True, sleep=lambda s: None)
    return opened, said


@respx.mock
def test_device_sign_in_without_a_prefilled_link_has_the_person_type_the_code(monkeypatch):
    opened, said = _sign_in({"device_code": "dc", "user_code": "WDJB-MJHT", "verification_uri": f"{A}/device"},
                            monkeypatch)
    assert opened == [f"{A}/device"]
    assert said[0] == f"Open {A}/device and enter the code WDJB-MJHT"


@respx.mock
def test_device_sign_in_opens_the_prefilled_link_when_there_is_one(monkeypatch):
    opened, said = _sign_in({"device_code": "dc", "user_code": "WDJB-MJHT", "verification_uri": f"{A}/device",
                             "verification_uri_complete": f"{A}/device?user_code=WDJB-MJHT"}, monkeypatch)
    assert opened == [f"{A}/device?user_code=WDJB-MJHT"]
    assert "WDJB-MJHT" in said[0] and f"{A}/device" in said[0]


@pytest.mark.parametrize("body", [
    {"device_code": "dc", "user_code": "U"},
    {"device_code": "dc", "verification_uri": f"{A}/device"},
    {"user_code": "U", "verification_uri": f"{A}/device"},
    {"device_code": "dc", "user_code": "U", "verification_uri": "https://phish.example.test/device"},
    {"device_code": "dc", "user_code": "U", "verification_uri": f"{A}/device",
     "verification_uri_complete": "https://phish.example.test/device"},
])
@respx.mock
def test_an_incomplete_or_off_domain_device_code_is_refused(body, monkeypatch):
    opened = []
    monkeypatch.setattr(accounts.webbrowser, "open", opened.append)
    respx.post(f"{A}/device/code").respond(json=body)
    with pytest.raises(config.Error):
        accounts.login(say=lambda s: None, open_browser=True, sleep=lambda s: None)
    assert opened == []


# Secrets at rest

def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_the_file_is_600_in_a_700_directory_and_written_whole(home):
    config.save({"api_key": "mai_sk_a", "email": "alice@example.test"})
    assert _mode(config.path()) == 0o600 and _mode(config.home()) == 0o700
    assert config.load() == {"api_key": "mai_sk_a", "email": "alice@example.test"}
    assert [p.name for p in config.home().iterdir()] == ["config.json"]  # no temp file left behind


def test_save_replaces_a_symlink_instead_of_writing_through_it(home):
    config.home().mkdir(mode=0o700, parents=True)
    target = home / "elsewhere"
    target.write_text("keep")
    config.path().symlink_to(target)
    config.save({"token": "t"})
    assert target.read_text() == "keep" and not config.path().is_symlink()


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_a_corrupt_file_is_an_error_not_an_empty_sign_in(home, text, capsys):
    config.home().mkdir(parents=True)
    config.path().write_text(text)
    with pytest.raises(config.Error, match="config.json"):
        config.load()
    assert cli.main(["whoami"]) == 1 and "config.json" in capsys.readouterr().err


class FakeKeyring:
    priority = 5

    def __init__(self):
        self.store = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        del self.store[(service, name)]


def test_secrets_go_to_the_keychain_and_the_file_keeps_the_rest(home, monkeypatch):
    kr = FakeKeyring()
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    config.save({"token": "tok", "api_key": "mai_sk_k", "email": "alice@example.test"})
    assert json.loads(config.path().read_text()) == {"email": "alice@example.test"}
    assert kr.store == {("metaphi-ai", "token"): "tok", ("metaphi-ai", "api_key"): "mai_sk_k"}
    assert config.load() == {"email": "alice@example.test", "token": "tok", "api_key": "mai_sk_k"}
    cfg = config.load()
    cfg.pop("token")
    config.save(cfg)
    assert ("metaphi-ai", "token") not in kr.store


def test_a_secret_already_in_the_file_moves_to_the_keychain_on_the_next_save(home, monkeypatch):
    config.save({"api_key": "mai_sk_old"})
    kr = FakeKeyring()
    monkeypatch.setattr(config, "_keyring", lambda: kr)
    config.save(config.load())
    assert "api_key" not in json.loads(config.path().read_text())
    assert kr.store[("metaphi-ai", "api_key")] == "mai_sk_old"


def test_a_refusing_keychain_falls_back_to_the_file(home, monkeypatch):
    class Refuses(FakeKeyring):
        def set_password(self, *a):
            raise RuntimeError("locked")

    monkeypatch.setattr(config, "_keyring", lambda: Refuses())
    config.save({"token": "tok"})
    assert json.loads(config.path().read_text()) == {"token": "tok"}


# Taking a key

def test_login_with_api_key_reads_stdin_not_argv(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("mai_sk_piped\n"))
    assert cli.main(["login", "--with-api-key"]) == 0
    assert config.load()["api_key"] == "mai_sk_piped"
    assert "warning" not in capsys.readouterr().err


def test_login_with_api_key_prompts_without_echo_on_a_terminal(home, monkeypatch):
    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Tty())
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "mai_sk_typed")
    assert cli.main(["login", "--with-api-key"]) == 0
    assert config.load()["api_key"] == "mai_sk_typed"


def test_login_refuses_what_is_not_a_key(home, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("hunter2\n"))
    assert cli.main(["login", "--with-api-key"]) == 1
    assert "api_key" not in config.load()


def test_a_key_on_argv_still_works_but_warns(home, capsys):
    assert cli.main(["login", "--api-key", "mai_sk_argv"]) == 0
    assert config.load()["api_key"] == "mai_sk_argv"
    assert "shell history" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.parser().parse_args(["twin", "--help"])
    assert "deprecated" in capsys.readouterr().out


def test_debug_keyring_says_where_secrets_go(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--debug-keyring"])
    assert e.value.code == 0 and capsys.readouterr().out.startswith("file: ")
