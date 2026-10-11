"""Anonymize a trace: upload it, run the anonymizer, download the twin. One file in, one file out, same format.

The anonymizer is served over MCP (the tools upload_trace, anonymize_trace, get_task, pull_trace); this is a
client of those tools, authenticated by an organization key exchanged for an hour-long token."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import httpx

from metaphi import accounts, config

DONE = ("completed", "failed", "cancelled")


def _structured(result: Any) -> Any:
    """What a tool answered, as data; a tool error raises its own sentence."""
    text = " ".join(getattr(c, "text", "") for c in (getattr(result, "content", None) or [])).strip()
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise accounts.Error(text or "the call failed")
    data = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if data is not None:
        return data
    try:
        return json.loads(text)
    except ValueError:
        return text


def find(value: Any, key: str) -> Any:
    """The first value under `key` anywhere in a nested answer."""
    if isinstance(value, dict):
        if key in value:
            return value[key]
        value = list(value.values())
    if isinstance(value, list):
        for v in value:
            got = find(v, key)
            if got is not None:
                return got
    if isinstance(value, str) and value[:1] in "{[":
        try:
            return find(json.loads(value), key)
        except ValueError:
            return None
    return None


def connect(token: str) -> Any:
    """The anonymizer's MCP endpoint, as the key's token."""
    import httpx2
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client

    http = httpx2.AsyncClient(headers={"authorization": f"Bearer {token}"}, timeout=httpx2.Timeout(60.0, read=300.0))
    return Client(streamable_http_client(config.kennet_url(), http_client=http))


async def _run(token: str, trace: Path, out: Path, key_hex: str, confidential: list[str],
               say: Callable[[str], None]) -> dict[str, Any]:
    async with connect(token) as mcp:
        async def tool(name: str, args: dict[str, Any]) -> Any:
            return _structured(await mcp.call_tool(name, args))

        up = await tool("upload_trace", {"filename": trace.name})
        with httpx.Client(timeout=600.0) as h:
            r = h.put(config.check_url(up["url"], "the upload link"), content=trace.read_bytes())
            if r.status_code >= 400:
                raise accounts.Error(f"upload refused: {r.text.strip() or r.status_code}")
        say(f"uploaded {trace.name} ({trace.stat().st_size:,} bytes)")
        args: dict[str, Any] = {"upload_id": up["upload_id"]}
        if key_hex:
            args["key"] = key_hex
        if confidential:
            args["confidential"] = confidential
        handle = await tool("anonymize_trace", args)
        task_id, wait = handle["task_id"], handle.get("poll_interval_ms", 5000) / 1000
        say(f"anonymizing (task {task_id})")
        last = ""
        while True:
            await anyio.sleep(wait)
            task = await tool("get_task", {"task_id": task_id})
            status = task.get("status", "")
            message = task.get("statusMessage") or ""
            if message and message != last:
                say(f"  {message}")
                last = message
            if status in DONE:
                break
        output = find(task, "output_path")
        if status != "completed" or not output:
            raise accounts.Error(f"anonymization {status}: {find(task, 'error') or message or status}")
        link = await tool("pull_trace", {"trace_path": output})
        with httpx.Client(timeout=600.0) as h:
            r = h.get(config.check_url(link["url"], "the download link"))
        if r.status_code >= 400:
            raise accounts.Error(f"download refused: {r.text.strip() or r.status_code}")
        out.write_bytes(r.content)
        say(f"wrote {out}")
        return {"task": task, "output": str(out)}


def _refusal(e: BaseException) -> config.Error | None:
    """The first CLI error inside a (possibly nested) exception group."""
    if isinstance(e, config.Error):
        return e
    for inner in getattr(e, "exceptions", ()):
        found = _refusal(inner)
        if found is not None:
            return found
    return None


def anonymize(trace: Path, out: Path | None = None, api_key: str | None = None, key_hex: str = "",
              confidential: list[str] | None = None, say: Callable[[str], None] = print) -> dict[str, Any]:
    key = config.api_key(api_key)
    if not key:
        raise accounts.Error("no key: set METAPHI_API_KEY, or run `metaphi login --with-api-key` or `metaphi keys create`")
    if not trace.is_file():
        raise accounts.Error(f"no file {trace}")
    out = out or trace.with_name(f"{trace.stem}.anonymized{trace.suffix}")
    token = accounts.token_for(key, config.kennet_url())
    started = time.time()
    try:
        got = anyio.run(_run, token, trace, out, key_hex, list(confidential or []), say)
    except Exception as e:  # the MCP client's task groups wrap a refusal; say the refusal, not the traceback
        refusal = _refusal(e)
        if refusal is None:
            raise
        raise refusal from None
    say(f"done in {time.time() - started:.0f}s")
    return got
