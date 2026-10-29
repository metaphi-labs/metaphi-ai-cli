"""Follow a run live: each trial's timeline, the step it is on, and the last lines its sandboxes wrote.

A run's events (GET /runs/{id}/events, from a cursor, waiting up to `WAIT_S` for one where none is new) are what
Kennet's environments and Harbor's trial hooks put on the run's stream as they happen: a trial's phases (start,
environment-start, agent-start, agent-end, verification-start, end, cancel), the steps that set its sandbox up
(image, sandbox, ready: each start, running, done or failed, with seconds), and each chunk a command in its
sandbox wrote. `Board` folds them into each trial's state; `draw` makes the lines a terminal shows of it, and
`compare` sets runs side by side, a column each and a row per stage: the sandbox each was given, its image, its
placement (with each refusal its provider gave), the agent, the grading, what the sandbox used, and what it cost.
Over the table, `towers` builds each run as it goes: a slab per stage on a scale all towers share, in its provider's
hue. `replay` plays an ended run's events back, faster, from its own start.

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
    spec: dict[str, dict] = field(default_factory=dict)      # each sandbox as given: provider, GPU, CPUs, memory
    stages: dict[tuple[str, str], dict] = field(default_factory=dict)   # (sandbox, step): its latest record
    usage: dict[str, list[dict]] = field(default_factory=dict)          # each sandbox's readings, in order
    steps: set[str] = field(default_factory=set)             # the agent's steps, by name

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
            t.stages[(sandbox, clean(phase))] = {**said, "at": at}
            if isinstance(said.get("spec"), dict):
                t.spec[sandbox] = said["spec"]
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
        elif kind == "spec":
            got = _json(body)
            if got:
                t.spec[clean(str(e.get("sandbox") or "env"))] = got
        elif kind == "usage":
            got = _json(body)
            if got:
                t.usage.setdefault(clean(str(e.get("sandbox") or "env")), []).append(got)
        elif kind == "step":
            t.steps.add(phase)
        elif kind == "cut" and e.get("phase") in ("usage", "step"):
            pass
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


# --- runs side by side -------------------------------------------------------------------------------------------

SPARK = "▁▂▃▄▅▆▇█"
LABEL_W = 9             # the row names' column
SPARK_MAX = 16          # the readings a sparkline shows, the latest


def spark(values: list[float], top: float) -> str:
    """The last readings as a sparkline against `top`."""
    shown = values[-SPARK_MAX:]
    return "".join(SPARK[min(len(SPARK) - 1, int(v / top * len(SPARK)))] if top > 0 else SPARK[0] for v in shown)


def _gb(n: object) -> str:
    return f"{n / 1e9:.1f}" if isinstance(n, (int, float)) else "?"


def sandbox_said(spec: dict | None) -> tuple[str, str]:
    """A sandbox as given, in two short lines: "thunder · A100XL" and "8 CPU · 32 GB"."""
    if not spec:
        return "placing…", ""
    gpu = spec.get("gpu") or {}
    kind = clean(str(gpu.get("type") or "")).replace("NVIDIA ", "")
    count = gpu.get("count") or 0
    first = " · ".join(x for x in (clean(str(spec.get("provider") or "?")),
                                   f"{count}× {kind}" if count > 1 else kind) if x)
    cpus, mem = spec.get("cpus"), spec.get("memory_gb")
    return first, " · ".join(x for x in (f"{cpus:g} CPU" if isinstance(cpus, (int, float)) else "",
                                         f"{mem:g} GB" if isinstance(mem, (int, float)) else "") if x)


def _stage_cell(t: Trial, sandbox: str, step: str, now: float) -> tuple[str, str | None]:
    """One setup step of a trial's sandbox: what it came to and its time, or its clock while it runs."""
    st = t.stages.get((sandbox, step))
    if not st:
        return "", None
    refused = st.get("refused") or {}
    said = clean(str(st.get("how") or ""))
    if refused.get("n"):
        said = f"refused {refused['n']}×"
    if st.get("state") in ("start", "running"):
        began = st["at"] - float(st.get("s") or 0)
        stop = max((t.phases[p] for p in ENDED if p in t.phases), default=None)
        return f"{said + ' ' if said else ''}{clock((stop or now) - began)}", SETUP
    if st.get("state") == "failed":
        return f"{said + ' ' if said else ''}✗ {clock(float(st.get('s') or 0))}", "bad"
    return f"{said + ' ' if said else ''}{clock(float(st.get('s') or 0))}", None


def _span(t: Trial, a: str, b: tuple[str, ...], now: float) -> float | None:
    if a not in t.phases:
        return None
    ends = [t.phases[x] for x in b if x in t.phases and t.phases[x] >= t.phases[a]]
    return (min(ends) if ends else now) - t.phases[a]


def _rows(t: Trial | None, now: float) -> list[tuple[str, str, str | None]]:
    """A trial's column: (row, cell, colour) for each row `compare` draws."""
    if t is None:
        return [("", "waiting for its first trial…", "dim")]
    rows: list[tuple[str, str, str | None]] = []
    first, second = sandbox_said(t.spec.get("env"))
    rows += [("sandbox", first, None), ("", second, "dim")]
    for step in ("image", "sandbox", "ready"):
        cell, tint = _stage_cell(t, "env", step, now)
        rows.append((step if step != "sandbox" else "placing", cell, tint))
    agent = _span(t, "agent-start", ("agent-end", "verification-start", *ENDED), now)
    steps = f" · {len(t.steps)} steps" if t.steps else ""
    rows.append(("agent", f"{clock(agent)}{steps}" if agent is not None else "",
                 AGENT if agent is not None and "agent-end" not in t.phases and not t.done else None))
    verify = _span(t, "verification-start", ENDED, now)
    rows.append(("grading", clock(verify) if verify is not None else "",
                 VERIFY if verify is not None and not t.done else None))
    use = t.usage.get("env") or []
    cores = [float(u["cpu_cores"]) for u in use if isinstance(u.get("cpu_cores"), (int, float))]
    cpus = (t.spec.get("env") or {}).get("cpus") or (use[-1].get("cpus") if use else None)
    rows.append(("cpu", f"{spark(cores, float(cpus or max(cores, default=1)))} {cores[-1]:.1f}/{cpus or '?'}"
                 if cores else "", None))
    peak = max((u.get("mem_peak_bytes") or u.get("mem_bytes") or 0 for u in use), default=0)
    mem = (t.spec.get("env") or {}).get("memory_gb")
    rows.append(("memory", f"peak {_gb(peak)}/{mem:g} GB" if peak and isinstance(mem, (int, float))
                 else f"peak {_gb(peak)} GB" if peak else "", None))
    gpus = [u["gpu"] for u in use if isinstance(u.get("gpu"), dict)]
    if gpus or (t.spec.get("env") or {}).get("gpu"):
        util = [float(g.get("util_pct") or 0) for g in gpus]
        vram = max((g.get("mem_used_mb") or 0 for g in gpus), default=0)
        rows.append(("gpu", f"{spark(util, 100.0)} {util[-1]:.0f}% · {vram / 1024:.1f} GB" if util else "", None))
    end = t.ended
    if t.done:
        model, total = end.get("model_usd"), end.get("total_usd")
        boxes = end.get("sandbox_usd") or {}
        box = sum(v for v in boxes.values() if isinstance(v, (int, float))) if isinstance(boxes, dict) else None
        parts = [f"model ${model:.2f}" if isinstance(model, (int, float)) else "",
                 f"sandbox ${box:.2f}" if box else ""]
        cost = (f"${total:.2f}" if isinstance(total, (int, float)) else "") + \
            (f" ({' · '.join(p for p in parts if p)})" if any(parts) else "")
        rows.append(("cost", cost.strip() or "—", None))
        said, tint = _status(t, now)
        rows.append(("result", said, tint))
    else:
        rows += [("cost", "", None), ("result", _status(t, now)[0], "dim")]
    began = min(t.phases.values(), default=now)
    stop = max((t.phases[p] for p in ENDED if p in t.phases), default=now)
    rows.append(("total", clock(stop - began), None))
    return rows


def compare(boards: list[Board], now: float | list[float], width: int, colour: bool) -> list[str]:
    """Runs side by side: a column each (its first trial), a row per stage, the same rows in every column. `now`
    is each run's clock, one for all live, or one each where runs played back race from their own starts."""
    nows = now if isinstance(now, list) else [now] * len(boards)
    cols = [next(iter(sorted(b.trials.values(), key=lambda t: min(t.phases.values(), default=n))), None)
            for b, n in zip(boards, nows)]
    col_w = max(14, (width - LABEL_W - 2) // max(1, len(boards)) - 2)
    columns = [_rows(t, n) for t, n in zip(cols, nows)]
    head = " " * LABEL_W + "".join(f"  {_paint(b.run_id[-col_w:].ljust(col_w), None, colour)}" for b in boards)
    task = next((t.name.rsplit("__", 1)[0] for t in cols if t), "")
    out = [_paint(task, None, colour), head, ""]
    labels = [r[0] for r in max(columns, key=len)]
    for i, label in enumerate(labels):
        line = _paint(label.ljust(LABEL_W), "dim", colour)
        for rows in columns:
            cell, tint = (rows[i][1], rows[i][2]) if i < len(rows) else ("", None)
            cell = cell[:col_w]
            line += "  " + _paint(cell, tint, colour) + " " * (col_w - len(cell))
        out.append(line.rstrip())
    return out


# A tower per run, built bottom up a slab per stage as the run goes, each slab as tall as its time on a scale all
# towers share; a provider's hue, its stages in shades of it, so towers of two providers tell apart at a glance.
TOWER_STAGES = ("image", "placing", "ready", "agent", "grading")
HUES = {"modal": (127, 238, 100), "thunder": (84, 160, 255)}
OTHER_HUES = ((255, 176, 64), (190, 120, 255), (255, 110, 160))
FAILED_HUE = (235, 80, 80)
TOWER_W, TOWER_D = 14, 3    # a tower's front, in columns, and its depth, in rows (two columns a row)


def hue(provider: str) -> tuple[int, int, int]:
    """A provider's colour: Modal green, Thunder blue, any other one of the rest, the same one every time."""
    name = provider.lower()
    return HUES.get(name) or OTHER_HUES[sum(map(ord, name)) % len(OTHER_HUES)]


def _shade(rgb: tuple[int, int, int], k: float) -> tuple[int, int, int]:
    """`rgb` darker below 1, toward white above it."""
    if k <= 1:
        return tuple(int(c * k) for c in rgb)  # type: ignore[return-value]
    return tuple(int(c + (255 - c) * (k - 1)) for c in rgb)  # type: ignore[return-value]


def slabs(t: Trial, now: float) -> list[tuple[str, float, str]]:
    """A trial's stages so far, bottom up: (stage, seconds, state), state "live", "done", "failed" or "capped"."""
    out: list[tuple[str, float, str]] = []
    for step, name in (("image", "image"), ("sandbox", "placing"), ("ready", "ready")):
        st = t.stages.get(("env", step))
        if not st:
            continue
        live = st.get("state") in ("start", "running") and not t.done
        secs = now - (st["at"] - float(st.get("s") or 0)) if live else float(st.get("s") or 0)
        out.append((name, secs, "live" if live else "failed" if st.get("state") == "failed" else "done"))
    for name, a, b in (("agent", "agent-start", ("agent-end", "verification-start", *ENDED)),
                       ("grading", "verification-start", ENDED)):
        span = _span(t, a, b, now)
        if span is not None:
            live = not t.done and not any(x in t.phases and t.phases[x] >= t.phases[a] for x in b)
            out.append((name, span, "live" if live else "done"))
    # red is the infra's failing; an agent stopped at its time limit is the task's cap, not a failure
    raised = str(t.ended.get("exception") or "") if t.done else ""
    where = (t.ended.get("failed_in") or {}).get("phase") if t.done else None
    marks = {"agent": "capped"} if raised == "AgentTimeoutError" else \
        {{"agent": "agent", "verifier": "grading"}.get(str(where), str(where)): "failed"} if raised else {}
    if any(st.get("state") == "failed" for (box, _), st in t.stages.items() if box == "verifier"):
        marks["grading"] = "failed"
    return [(n, s, marks.get(n, state)) for n, s, state in out]


def _tower(stack: list[tuple[str, float, str]], rgb: tuple[int, int, int], scale: float,
           height: int) -> tuple[list[list[tuple[str, Any, Any]]], list[tuple[int, str]]]:
    """One tower in cells, (glyph, face colour or None, glyph colour or None) by row top down, `height` rows of front plus its top, and the
    rows its slabs' labels sit on."""
    rows: list[int] = []        # front rows each slab takes, bottom up
    for _, secs, _ in stack:
        rows.append(max(1, round(secs * scale)))
    front = sum(rows)
    stage_of: list[tuple[str, float, str, int]] = []    # per front row, top down
    for (name, secs, state), n in zip(reversed(stack), reversed(rows)):
        stage_of += [(name, secs, state, i) for i in range(n)]
    total_h = height + TOWER_D
    width = TOWER_W + 2 * TOWER_D
    grid: list[list[tuple[str, Any, Any]]] = [[(" ", None, None)] * width for _ in range(total_h)]
    labels: list[tuple[int, str]] = []
    if not front:
        return grid, labels
    top = total_h - front       # the screen row the front's top sits on

    def tint(row: int, face: float) -> tuple[int, int, int]:
        name, _, state, _ = stage_of[row]
        base = FAILED_HUE if state == "failed" else _shade(rgb, 0.55 + 0.6 * TOWER_STAGES.index(name) / 4)
        return _shade(base, face)

    for y in range(front):
        name, secs, state, i = stage_of[y]
        # a slab's top row is its seam with the one above, and blinks while its stage runs
        seam = i == 0 and y > 0
        glyph = "▒" if state == "live" and i == 0 and int(time.monotonic() * 3) % 2 else "▔" if seam else " "
        for x in range(TOWER_W):
            grid[top + y][x] = (glyph, tint(y, 1.0), _shade(tint(y, 1.0), 0.45) if glyph != " " else None)
        if i == 0:
            labels.append((top + y, f"{name} {clock(secs)}{' ✗' if state == 'failed' else ' ⏱' if state == 'capped' else ''}"))
    for k in range(1, TOWER_D + 1):            # the top face, the top slab's, lit
        for x in range(2 * k, 2 * k + TOWER_W):
            grid[top - k][x] = (" ", tint(0, 1.35), None)
    for j in range(1, 2 * TOWER_D + 1):        # the side face, each column a slab's, in shadow
        d = (j + 1) // 2
        for y in range(-d, front - d):
            if 0 <= y + d < front and top + y >= 0:
                grid[top + y][TOWER_W - 1 + j] = (" ", tint(y + d, 0.6), None)
    return grid, labels


def towers(boards: list[Board], now: float | list[float], height: int, colour: bool) -> list[str]:
    """Runs as towers side by side on one ground, each run's first trial: its provider and shape above it, a slab
    per stage with its time beside it, and its total and what it came to under it."""
    nows = now if isinstance(now, list) else [now] * len(boards)
    trials = [next(iter(sorted(b.trials.values(), key=lambda t: min(t.phases.values(), default=n))), None)
              for b, n in zip(boards, nows)]
    stacks = [slabs(t, n) if t else [] for t, n in zip(trials, nows)]
    longest = max((sum(s for _, s, _ in st) for st in stacks), default=0)
    # a stage of any length takes a row at least, so a tower is never shorter than its stages
    room = max(len(TOWER_STAGES), height - TOWER_D - 4)
    scale = room / longest if longest else 0

    def tallest(k: float) -> int:
        return max((sum(max(1, round(s * k)) for _, s, _ in st) for st in stacks), default=0)

    while tallest(scale) > room:    # the short stages' rows taken, shrink the rest until the tallest fits
        scale *= 0.9
    label_w = 16
    col_w = TOWER_W + 2 * TOWER_D + 1 + label_w
    built = []
    for t, st in zip(trials, stacks):
        spec = t.spec.get("env") if t else None
        rgb = hue(str((spec or {}).get("provider") or "?"))
        grid, labels = _tower(st, rgb, scale, room)
        built.append((t, rgb, grid, dict(labels)))
    out = []
    head = ["", ""]
    for t, rgb, _, _ in built:
        first, second = sandbox_said(t.spec.get("env") if t else None)
        head[0] += _rgb(first[:col_w].ljust(col_w), rgb, None, colour, bold=True) + "  "
        head[1] += _paint(second[:col_w].ljust(col_w), "dim", colour) + "  "
    out += [h.rstrip() for h in head] + [""]
    for y in range(room + TOWER_D):
        line = ""
        for _, rgb, grid, labels in built:
            line += "".join(_rgb(g, fg, bg, colour) if bg else g for g, bg, fg in grid[y])
            line += " " + _paint(labels.get(y, "")[:label_w].ljust(label_w), None, colour) + "  "
        out.append(line.rstrip())
    out.append("".join(_paint("▔" * (TOWER_W + 2 * TOWER_D), "dim", colour) + " " * (label_w + 3)
                       for _ in built).rstrip())
    foot = ""
    for t, n in zip(trials, nows):
        if t is None:
            foot += "waiting…".ljust(col_w) + "  "
            continue
        began = min(t.phases.values(), default=n)
        stop = max((t.phases[p] for p in ENDED if p in t.phases), default=n)
        said, tint = _status(t, n)
        cell = f"{clock(stop - began)}  {said}"[:col_w]
        foot += _paint(cell, tint, colour) + " " * (col_w - len(cell)) + "  "
    out.append(foot.rstrip())
    return out


def _rgb(text: str, fg: tuple[int, int, int] | None, bg: tuple[int, int, int] | None, on: bool,
         bold: bool = False) -> str:
    if not on:
        return text if bg is None else "█" if text == " " else "▀" if text == "▔" else text
    codes = (["1"] if bold else []) + ([f"38;2;{fg[0]};{fg[1]};{fg[2]}"] if fg else []) + \
        ([f"48;2;{bg[0]};{bg[1]};{bg[2]}"] if bg else [])
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m"



def side_by_side(boards: list[Board], now: float | list[float], width: int, height: int, colour: bool) -> list[str]:
    """The towers over the table: how each run was built, then what each stage came to."""
    table = compare(boards, now, width, colour)
    return towers(boards, now, max(10, height - len(table) - 2), colour) + [""] + table


def screen(boards: list[Board], now: float | list[float], size: Any, colour: bool) -> list[str]:
    """What a terminal of `size` shows: one run's timeline, or two and more side by side, cut to its height."""
    rows, cols = max(1, size.lines - 1), size.columns - 1
    if len(boards) == 1:
        return draw(boards[0], now[0] if isinstance(now, list) else now, cols, colour)[:rows]
    return side_by_side(boards, now, cols, rows, colour)[:rows]

def replay(events: list[dict], speed: float) -> Callable[[float], tuple[list[dict], float]]:
    """A played-back run: given seconds since the playback began, the events due by then (each once) and the
    run's clock at that moment, `speed` times as fast as it happened."""
    timed = sorted(events, key=lambda e: _at(e.get("at")))
    start = _at(timed[0].get("at")) if timed else time.time()
    sent = 0

    def due(elapsed: float) -> tuple[list[dict], float]:
        nonlocal sent
        clock_at = start + elapsed * speed
        upto = sent
        while upto < len(timed) and _at(timed[upto].get("at")) <= clock_at:
            upto += 1
        got, sent = timed[sent:upto], upto
        return got, clock_at

    return due
