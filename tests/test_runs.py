"""`kennet tail`: a run's events folded into each trial's timeline, drawn, and followed against a stand-in for the API."""
import json
from datetime import datetime, timezone

import httpx
import pytest
import respx

from metaphi import cli, config, runs

API = config.DEFAULT_API
RUN = "20261028T072322Z-db0eea"
T0 = 1_800_000_000
A, B = "route-driver__wZRsaKP", "cheat__uhdxFKp"


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METAPHI_HOME", str(tmp_path / "cfg"))
    for var in ("METAPHI_ACCOUNTS", "METAPHI_API", "METAPHI_ALLOW_CUSTOM_HOST", "NO_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("METAPHI_API_KEY", "mai_sk_" + "a" * 43)
    return tmp_path


def event(s: float, trial: str, kind: str, phase: str = "", body: object = "", **kw) -> dict:
    at = datetime.fromtimestamp(T0 + s, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"seq": 0, "trial": trial, "kind": kind, "phase": phase, "stream": kw.get("stream"),
            "sandbox": kw.get("sandbox"), "body": body if isinstance(body, str) else json.dumps(body), "at": at}


def a_run() -> list[dict]:
    """Two trials: one ends with a reward, one is still grading, its route lines streaming."""
    return [event(0, A, "phase", "start"), event(0, A, "phase", "environment-start"),
            event(0, A, "stage", "image", {"state": "start", "s": 0}, sandbox="env"),
            event(4, A, "stage", "image", {"state": "done", "s": 4, "how": "cached"}, sandbox="env"),
            event(4, A, "stage", "sandbox", {"state": "start", "s": 0}, sandbox="env"),
            event(0, B, "phase", "start"),
            event(300, B, "phase", "agent-start"), event(302, B, "phase", "agent-end"),
            event(310, B, "phase", "end", {"rewards": {"reward": 1.0}}),
            event(370, A, "stage", "sandbox", {"state": "done", "s": 366}, sandbox="env"),
            event(371, A, "phase", "agent-start"), event(373, A, "phase", "agent-end"),
            event(374, A, "phase", "verification-start"),
            event(400, A, "output", "verification-start", "route 1/10 score 7", stream="stdout", sandbox="verifier"),
            event(401, A, "output", "verification-start", "1.2\nroute 2/10 ", stream="stdout", sandbox="verifier")]


def board(events: list[dict]) -> runs.Board:
    b = runs.Board(RUN)
    for e in events:
        b.add(e)
    return b


def test_each_trial_is_a_row_of_what_it_did_and_the_step_it_is_on():
    b = board(a_run())
    screen = runs.draw(b, T0 + 600, 100, colour=False)
    assert screen[0].startswith(f"{RUN}  10:00  2 trials · 1 ended · 1 running")
    [a_row] = [line for line in screen if line.lstrip().startswith(A)]
    [b_row] = [line for line in screen if line.lstrip().startswith(B)]
    assert a_row.endswith("grading 3:46") and b_row.endswith("✓ reward 1.00")
    # setting up for most of its first 6 minutes, then grading; nothing after the other trial ended
    bar = a_row.split(A, 1)[1].strip().split("  ")[0]
    assert bar[0] == runs.SETUP and bar[-1] == runs.VERIFY and runs.AGENT not in b_row.split(B)[1][-30:]
    assert "image cached 0:04 · sandbox 6:06" in "\n".join(screen)
    # output is shown a whole line at a time, a line split across chunks joined
    assert "  wZRsaKP │ route 1/10 score 71.2" in screen and not any("route 2/10" in line for line in screen)
    assert len(max(screen, key=len)) <= 100


def test_a_step_under_way_and_an_ended_trials_failure_are_said():
    b = board([event(0, A, "phase", "start"),
               event(30, A, "stage", "sandbox", {"state": "running", "s": 30}, sandbox="verifier"),
               event(0, B, "phase", "start"), event(9, B, "phase", "end", {"exception": "SandboxBuildFailedError"})])
    said = dict(runs._status(t, T0 + 95) for t in b.trials.values())
    assert said == {"verifier sandbox 1:35": runs.SETUP, "✗ SandboxBuildFailedError": "bad"}
    assert not b.done
    b.add(event(99, A, "phase", "cancel", {}))
    assert b.done and runs._status(b.trials[A], T0 + 99) == ("cancelled", "bad")


def test_nothing_the_sandbox_writes_reaches_the_terminal_as_a_control_sequence():
    hostile = "\x1b[2J\x1b]0;owned\x07\x1b[31mred\x1b[0m\rdone\x00\n"
    b = board([event(0, f"t\x1b[2J{A}", "output", "agent-start", hostile, stream="stdout")])
    drawn = "\n".join(runs.draw(b, T0 + 1, 100, colour=False))
    assert "\x1b" not in drawn and "\r" not in drawn and "\x00" not in drawn and "reddone" in drawn
    assert "\x1b" not in runs.plain(event(0, A, "output", "agent-start", hostile))


def test_colour_paints_each_stretch_once():
    screen = runs.draw(board(a_run()), T0 + 600, 100, colour=True)
    [a_row] = [line for line in screen if A in line]
    assert a_row.count("\x1b[33m") == 1 and a_row.count("\x1b[35m") >= 1


def test_plain_says_one_event_a_line():
    lines = [runs.plain(e) for e in a_run()]
    assert lines[3] == "08:00:04 route-driver__wZRsaKP image done 0:04 cached"
    assert lines[8] == '08:05:10 cheat__uhdxFKp end {"rewards": {"reward": 1.0}}'
    assert runs.plain(event(0, A, "output", "x", "  \n")) is None


@respx.mock
def test_tail_follows_until_every_trial_has_ended_and_the_run_is_quiet(capsys, monkeypatch):
    monkeypatch.setattr(runs, "WAIT_S", 0)
    pages = [{"object": "list", "data": a_run()[:9], "has_more": True, "next_since": 9},
             {"object": "list", "data": [event(500, A, "phase", "end", {"rewards": {"reward": 0.5}})],
              "has_more": False, "next_since": 10},
             {"object": "list", "data": [], "has_more": False, "next_since": 10}]
    route = respx.get(f"{API}/runs/{RUN}/events").mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    assert cli.main(["tail", "acme/tasks", RUN, "--plain"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "08:00:00 route-driver__wZRsaKP start" and out[-1].endswith('end {"rewards": {"reward": 0.5}}')
    sent = [dict(c.request.url.params) for c in route.calls]
    assert sent[0] == {"repo": "acme/tasks", "since": "0", "wait_s": "0"} and sent[1]["since"] == "9"
    assert route.calls[0].request.headers["authorization"].startswith("Bearer mai_sk_")


@respx.mock
def test_tail_json_is_the_events_a_line_each(capsys, monkeypatch):
    monkeypatch.setattr(runs, "WAIT_S", 0)
    ended = [event(0, B, "phase", "start"), event(9, B, "phase", "end", {})]
    respx.get(f"{API}/runs/{RUN}/events").respond(json={"data": ended, "has_more": False, "next_since": 2})
    assert cli.main(["tail", "acme/tasks", RUN, "--json", "--trial", B]) == 0
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == ended


@respx.mock
def test_a_run_the_organization_does_not_own_is_said_plainly(capsys):
    respx.get(f"{API}/runs/{RUN}/events").respond(404, json={"error": {
        "type": "invalid_request_error", "code": "resource_missing", "message": f"no run {RUN} here"}})
    assert cli.main(["tail", "acme/tasks", RUN]) == cli.FAILED
    assert f"acme/tasks holds no run {RUN} of your organization's" in capsys.readouterr().err


MODAL, THUNDER = "20261029T200817Z-f02a9c", "20261029T200846Z-cfd296"


def two_providers() -> tuple[list[dict], list[dict]]:
    """The same task on Modal and on Thunder: Modal named its image and graded, Thunder's verifier was refused."""
    def one(provider: str, gpu: str, image: tuple[str, float], placing: float, verifier: dict) -> list[dict]:
        spec = {"provider": provider, "gpu": {"type": gpu, "count": 1}, "cpus": 8, "memory_gb": 32.0}
        return [event(0, A, "phase", "start"), event(0, A, "phase", "environment-start"),
                event(0, A, "stage", "image", {"state": "start", "s": 0}, sandbox="env"),
                event(image[1], A, "stage", "image", {"state": "done", "s": image[1], "how": image[0]}, sandbox="env"),
                event(image[1], A, "spec", "", spec, sandbox="env"),
                event(image[1], A, "stage", "sandbox", {"state": "start", "s": 0}, sandbox="env"),
                event(image[1] + placing, A, "stage", "sandbox", {"state": "done", "s": placing}, sandbox="env"),
                event(10, A, "stage", "ready", {"state": "done", "s": 2}, sandbox="env"),
                event(20, A, "usage", "", {"cpus": 8, "cpu_cores": 5.5, "mem_bytes": 4 << 30,
                                          "gpu": {"util_pct": 40, "mem_used_mb": 2048}}, sandbox="env"),
                event(20, A, "phase", "agent-start"), event(320, A, "phase", "agent-end"),
                event(330, A, "phase", "verification-start"),
                event(331, A, "stage", "sandbox", verifier, sandbox="verifier"),
                event(400, A, "phase", "end", {"exception": "AgentTimeoutError", "failed_in": {"phase": "agent"},
                                               "rewards": {"reward": 0.0}, "model_usd": 0.3, "total_usd": 0.7,
                                               "sandbox_usd": {"env": 0.39, "verifier": 0.01}})]
    return (one("modal", "A100-80GB", ("named", 0.0), 0.9, {"state": "done", "s": 1.0}),
            one("thunder", "A100XL", ("cached", 1.9), 3.4, {"state": "failed", "s": 1.2, "error": "SpecNotCarriedError"}))


def test_runs_side_by_side_have_a_row_per_stage_and_a_column_per_run():
    modal, thunder = two_providers()
    b1, b2 = runs.Board(MODAL), runs.Board(THUNDER)
    for e in modal:
        b1.add(e)
    for e in thunder:
        b2.add(e)
    rows = {line.split()[0]: line for line in runs.compare([b1, b2], T0 + 500, 120, colour=False)[3:] if line.strip()}
    assert "modal · A100-80GB" in rows["sandbox"] and "thunder · A100XL" in rows["sandbox"]
    assert "named 0:00" in rows["image"] and "cached 0:01" in rows["image"]
    assert rows["agent"].count("5:00") == 2 and "5.5/8" in rows["cpu"] and "40% · 2.0 GB" in rows["gpu"]
    assert "$0.70 (model $0.30 · sandbox $0.40)" in rows["cost"]


def test_towers_stand_on_one_ground_a_slab_per_stage_in_the_providers_hue():
    modal, thunder = two_providers()
    b1, b2 = runs.Board(MODAL), runs.Board(THUNDER)
    for e in modal:
        b1.add(e)
    for e in thunder:
        b2.add(e)
    plain = runs.towers([b1, b2], T0 + 500, 40, colour=False)
    assert plain[0].startswith("modal · A100-80GB") and "thunder · A100XL" in plain[0]
    said = "\n".join(plain)
    for stage in ("image 0:00", "image 0:01", "placing 0:03", "ready 0:02", "agent 5:00 ⏱", "grading 1:10 ✗"):
        assert stage in said, stage
    assert "grading 1:10\n" in said + "\n" or "grading 1:10 " in said     # Modal graded: no mark
    assert plain[-1].startswith("6:40")
    painted = "\n".join(runs.towers([b1, b2], T0 + 500, 40, colour=True))
    assert "48;2;127;238;100" in painted and "48;2;84;160;255" in painted          # Modal green, Thunder blue


def test_a_provider_with_no_hue_of_its_own_is_given_one_and_keeps_it():
    assert runs.hue("modal") == (127, 238, 100) and runs.hue("thunder") == (84, 160, 255)
    assert runs.hue("docker") == runs.hue("docker") and runs.hue("docker") in runs.OTHER_HUES


def test_a_stage_under_way_grows_its_slab_as_the_clock_runs():
    modal, _ = two_providers()
    b = board(modal[:10])
    early = runs.slabs(b.trials[A], T0 + 60)
    late = runs.slabs(b.trials[A], T0 + 200)
    assert early[-1][0] == "agent" and early[-1][2] == "live" and late[-1][1] > early[-1][1]


def test_replay_plays_each_event_once_at_speed():
    modal, _ = two_providers()
    due = runs.replay(modal, speed=100)
    first, at = due(0.5)                 # 50 s of the run
    assert at == T0 + 50 and all(runs._at(e["at"]) <= T0 + 50 for e in first)
    rest, _ = due(10)
    assert len(first) + len(rest) == len(modal) and not set(map(id, first)) & set(map(id, rest))


@respx.mock
def test_tail_follows_runs_side_by_side_and_says_each_line_with_its_run(capsys, monkeypatch):
    monkeypatch.setattr(runs, "WAIT_S", 0)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    modal, thunder = two_providers()
    for run, events in ((MODAL, modal), (THUNDER, thunder)):
        pages = [{"data": events, "has_more": False, "next_since": 99}, {"data": [], "has_more": False, "next_since": 99}]
        respx.get(f"{API}/runs/{run}/events").mock(side_effect=[httpx.Response(200, json=p) for p in pages])
    assert cli.main(["tail", "acme/tasks", MODAL, THUNDER, "--plain"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert any(line.startswith(f"{MODAL} ") for line in out) and any(line.startswith(f"{THUNDER} ") for line in out)


@respx.mock
def test_tail_speed_plays_ended_runs_back(capsys, monkeypatch):
    modal, thunder = two_providers()
    respx.get(f"{API}/runs/{MODAL}/events").respond(json={"data": modal, "has_more": False, "next_since": 99})
    respx.get(f"{API}/runs/{THUNDER}/events").respond(json={"data": thunder, "has_more": False, "next_since": 99})
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    assert cli.main(["tail", "acme/tasks", MODAL, THUNDER, "--speed", "100000"]) == 0
    out = capsys.readouterr().out
    assert "thunder · A100XL" in out and "grading 1:10 ✗" in out
