"""Follow a run live: each trial's timeline, the step it is on, and the last lines its sandboxes wrote.

A run's events (GET /runs/{id}/events, from a cursor, waiting up to `WAIT_S` for one where none is new) are what
Kennet's environments and Harbor's trial hooks put on the run's stream as they happen: a trial's phases (start,
environment-start, agent-start, agent-end, verification-start, end, cancel), the steps that set its sandbox up
(image, sandbox, ready: each start, running, done or failed, with seconds), and each chunk a command in its
sandbox wrote. `Board` folds them into each trial's state; `draw` makes the lines a terminal shows of it.

A trial's row is its time on the run's clock: setting up (`SETUP`), the agent (`AGENT`), grading (`VERIFY`), drawn
in glyphs a terminal without colour tells apart. Nothing the sandbox wrote reaches the terminal with a control
character in it: a command's output is the task's, and an escape in it would be the task's to redraw this screen."""
from __future__ import annotations

import json
import re
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from metaphi import errors
from metaphi.twin import _client, _key, _send

WAIT_S = 20             # the longest the API waits for an event where none is new
TAIL_LINES = 6          # the output lines shown under the rows
NAME_MAX = 30           # a trial's name is cut to this
SETUP, AGENT, VERIFY = "░", "█", "▓"
ENDED = ("end", "cancel")
CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|[\x00-\x08\x0b-\x1f\x7f-\x9f]")
COLOURS = {SETUP: "33", AGENT: "34", VERIFY: "35", "ok": "32", "bad": "31", "dim": "2"}


def read(repo: str, run_id: str, since: int = 0, trial: str = "", wait_s: int = 0, test: bool = False,
         sleep: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """One page of the run's events from `since`: `{data, has_more, next_since}`."""
    params = {"repo": repo, "since": since, "wait_s": max(0, min(wait_s, WAIT_S)), **({"trial": trial} if trial else {})}
    with _client(_key(None, test)) as h:
        try:
            return _send(h, "GET", f"/runs/{run_id}/events", sleep, params=params, timeout=WAIT_S + 30.0).json()
        except errors.MetaphiError as e:
            if e.code == "resource_missing":
                e.message = f"{repo} holds no run {run_id} of your organization's"
            elif e.code == "insufficient_scope":
                e.message += "; `kennet login` hands this machine keys that follow runs"
                e.next_step = "kennet login"
            raise


def clean(text: str) -> str:
    """`text` with no control character or escape left in it."""
    return CONTROL.sub("", text.replace("\t", "    "))


def _at(said: str) -> float:
    try:
        return datetime.fromisoformat(said.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return time.time()


def _json(body: str) -> dict:
    try:
        got = json.loads(body or "{}")
    except ValueError:
        return {}
    return got if isinstance(got, dict) else {}


@dataclass
class Trial:
    name: str
    phases: dict[str, float] = field(default_factory=dict)   # each phase, when it began
    step: tuple[str, str, float] | None = None              # the setup step under way: (step, sandbox, since)
    said: list[str] = field(default_factory=list)            # what the setup steps came to: "image cached 0:04"
    ended: dict = field(default_factory=dict)                # its end: rewards and what it raised

    @property
    def done(self) -> bool:
        return any(p in self.phases for p in ENDED)

    def what(self, t: float) -> str:
        """The glyph of what the trial was doing at `t`, or a space."""
        p = self.phases
        if t < min(p.values(), default=t + 1) or (self.done and t >= max(p[e] for e in ENDED if e in p)):
            return " "
        if "agent-end" in p and t >= p["agent-end"] or "verification-start" in p and t >= p["verification-start"]:
            return VERIFY
        if "agent-start" in p and t >= p["agent-start"]:
            return AGENT
        return SETUP


class Board:
    """A run's trials, as its events have them so far."""

    def __init__(self, run_id: str) -> None:
        self.run_id, self.trials, self.lines = run_id, {}, deque(maxlen=TAIL_LINES)
        self.first: float | None = None
        self.partial: dict[tuple[str, str], str] = {}

    def add(self, e: dict) -> None:
        name, at = clean(str(e.get("trial") or "?")), _at(e.get("at"))
        self.first = at if self.first is None else min(self.first, at)
        t = self.trials.setdefault(name, Trial(name))
        kind, phase, body = e.get("kind"), str(e.get("phase") or ""), str(e.get("body") or "")
        if kind == "phase":
            t.phases.setdefault(phase, at)
            if phase in ENDED:
                t.ended, t.step = _json(body), None
        elif kind == "stage":
            said, sandbox = _json(body), clean(str(e.get("sandbox") or "env"))
            state = said.get("state")
            if state in ("start", "running"):
                t.step = (clean(phase), sandbox, at - float(said.get("s") or 0))
            else:
                t.step = None
                how = clean(str(said.get("how") or said.get("error") or ""))
                where = "" if sandbox == "env" else f"{sandbox} "
                t.said.append(f"{where}{clean(phase)} {how + ' ' if how else ''}{'failed ' if state == 'failed' else ''}"
                              f"{clock(float(said.get('s') or 0))}")
        elif kind == "output":
            key = (name, str(e.get("stream") or ""))
            text = self.partial.pop(key, "") + body
            *whole, rest = text.split("\n")
            if rest:
                self.partial[key] = rest[-2000:]
            for line in whole:
                if clean(line).strip():
                    self.lines.append((name, clean(line).rstrip()))
        elif kind == "cut":
            self.lines.append((name, "(its output goes on in the run's files)"))

    @property
    def done(self) -> bool:
        return bool(self.trials) and all(t.done for t in self.trials.values())


def clock(s: float) -> str:
    s = max(0, int(s))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _paint(text: str, colour: str | None, on: bool) -> str:
    return f"\x1b[{COLOURS[colour]}m{text}\x1b[0m" if on and colour else text


def _status(t: Trial, now: float) -> tuple[str, str | None]:
    """What a trial is doing, or what it came to, and its colour."""
    p = t.phases
    if t.done:
        rewards, raised = t.ended.get("rewards"), t.ended.get("exception")
        if raised:
            return f"✗ {clean(str(raised))}", "bad"
        if "cancel" in p:
            return "cancelled", "bad"
        if isinstance(rewards, dict) and rewards:
            shown = ", ".join(f"{clean(str(k))} {v:.2f}" if isinstance(v, (int, float)) else f"{clean(str(k))} {v}"
                              for k, v in list(rewards.items())[:2])
            return f"✓ {shown}", "ok"
        return "✓ ended", "ok"
    if t.step:
        step, sandbox, since = t.step
        return f"{'' if sandbox == 'env' else sandbox + ' '}{step} {clock(now - since)}", SETUP
    for phase, said, colour in (("verification-start", "grading", VERIFY), ("agent-end", "grading", VERIFY),
                                ("agent-start", "agent", AGENT), ("environment-start", "setting up", SETUP),
                                ("start", "starting", SETUP)):
        if phase in p:
            return f"{said} {clock(now - p[phase])}", colour
    return "queued", None


def draw(board: Board, now: float, width: int, colour: bool) -> list[str]:
    """The screen: a header, a row per trial, and the last lines their sandboxes wrote."""
    trials = sorted(board.trials.values(), key=lambda t: (min(t.phases.values(), default=now), t.name))
    ended = sum(t.done for t in trials)
    first = board.first if board.first is not None else now
    end = max((max(t.phases.values()) for t in trials if t.phases), default=now) if board.done else now
    span = max(end - first, 1.0)
    head = f"{board.run_id}  {clock(span)}  {len(trials)} trials · {ended} ended · {len(trials) - ended} running"
    out = [_paint(head, None, colour), ""]
    if not trials:
        return [*out, "  waiting for the run's first trial…"]
    named = min(NAME_MAX, max(len(t.name) for t in trials))
    statuses = [_status(t, now) for t in trials]
    status_w = min(30, max(len(s) for s, _ in statuses))
    bar = max(10, width - named - status_w - 6)
    for t, (said, tint) in zip(trials, statuses):
        cells = [t.what(first + (i + 0.5) * span / bar) for i in range(bar)]
        drawn, run = [], ""
        for c in cells:   # one escape per stretch of a glyph, not per cell
            if run and c != run[0]:
                drawn.append(_paint(run, run[0] if run[0] != " " else None, colour))
                run = ""
            run += c
        drawn.append(_paint(run, run[0] if run and run[0] != " " else None, colour))
        name = t.name if len(t.name) <= named else t.name[:named - 1] + "…"
        out.append(f"  {name:<{named}}  {''.join(drawn)}  {_paint(said[:status_w], tint, colour)}")
        if t.said:
            out.append(_paint(f"  {'':<{named}}  {' · '.join(t.said[-3:])}"[:width], "dim", colour))
    out += ["", _paint(f"  {SETUP} setting up   {AGENT} agent   {VERIFY} grading", "dim", colour)]
    if board.lines:
        out.append("")
        for name, line in board.lines:
            short = name.rsplit("__", 1)[-1] if "__" in name else name[:12]
            out.append(f"  {_paint(short[:12], 'dim', colour)} │ {line}"[:width + (9 if colour else 0)])
    return out


def plain(e: dict) -> str | None:
    """One event as a line of its own, for a log or a pipe; None for output that is only whitespace."""
    trial, kind, phase = clean(str(e.get("trial") or "?")), e.get("kind"), clean(str(e.get("phase") or ""))
    at = clean(str(e.get("at") or ""))[11:19]
    if kind == "output":
        text = clean(str(e.get("body") or "")).rstrip()
        return f"{at} {trial} │ {text}" if text.strip() else None
    if kind == "stage":
        said = _json(str(e.get("body") or ""))
        extra = " ".join(clean(str(said[k])) for k in ("how", "error") if said.get(k))
        sandbox = clean(str(e.get("sandbox") or "env"))
        return (f"{at} {trial} {'' if sandbox == 'env' else sandbox + ' '}{phase} {said.get('state', '?')} "
                f"{clock(float(said.get('s') or 0))}{' ' + extra if extra else ''}")
    if kind == "cut":
        return f"{at} {trial} output cut: the rest is in the run's files"
    body = clean(str(e.get("body") or ""))
    return f"{at} {trial} {phase}{' ' + body if body and body != '{}' else ''}"
