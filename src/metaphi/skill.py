"""The Kennet skill, which teaches a coding agent this CLI: `metaphi skill install` puts it where Claude Code reads skills.

The file installed is a stub: when to reach for the CLI, and one instruction, run `metaphi skill show`. The guide
itself is printed by the binary, so it is always the guide of the version installed and never goes stale on disk.
Kept as text in this module, not a data file, so the one-file binary carries it."""
from __future__ import annotations

import os
from pathlib import Path

from metaphi import __version__, config

NAME = "kennet"   # the skill is Kennet's, the infrastructure customers reach through this CLI; the CLI is Metaphi's
MARK = "<!-- written by `metaphi skill install`; edits are overwritten -->"

STUB = f"""---
name: {NAME}
description: Kennet, Metaphi AI's RL infrastructure, driven through the `metaphi` CLI. Today that is synthetic twinning of agent traces. Use when the user wants real people, companies, accounts or secrets in a trace, log or transcript file replaced with consistent synthetic stand-ins (a "synthetic twin") before sharing it or training on it, or mentions Metaphi, Kennet twins, `metaphi twin`, or a `mai_sk_` key.
---
{MARK}

# Kennet, through the Metaphi AI CLI

Run `metaphi skill show` and follow what it prints: it is the guide for the version of the CLI on this machine.

If `metaphi` is not found, install it, then run `metaphi skill show`:

```sh
brew install metaphi-labs/tap/metaphi    # macOS and Linux
pip install metaphi-ai                   # anywhere with Python 3.10+
```
"""

GUIDE = f"""# Kennet through the Metaphi AI CLI (metaphi {__version__})

Kennet is Metaphi AI's RL infrastructure. What it offers customers through this CLI today is synthetic twinning.

`metaphi twin <file>` uploads one trace file (JSON, JSON Lines or text) to Metaphi AI's service and writes its
synthetic twin beside it: the same file, same structure, with every real person, private organization, account
and secret replaced by one consistent synthetic value of the same kind. It is called a twin, never a redaction.
The file leaves this machine to be twinned, on a test run as on a live one: say so if the user's concern is where
their data goes.

## Rules

- Add `--json` to every command. stdout is then one JSON object: the result, or `{{"error": {{...}}}}`. Progress
  is on stderr. Exit 0 is success, 1 is an error.
  Read stdout only: stderr repeats an error in words, for a person.
- On an error, read `error.code` and find it in the table in step 4. `error.next_step` is the next command to run
  (add `--json` to it). Never decide from the message text.
- Never ask the user for an organization key (`mai_sk_...`), never print one, never pass one as an argument. They
  are saved by `metaphi login` or come from `METAPHI_API_KEY`.
- Start with a test run (`--test`): it is free, and it is a real twin of the user's file through the same
  pipeline, only limited in size and runs a day. A live run draws the organization's prepaid credit: ask the
  user before the first one, even after a good test run.
- When a step needs the user and they are not there, stop and hand back what they have to do. Do not work around it.
- Quote `error.request_id` to the user when something fails that you cannot clear.

## 1. Where things stand

```sh
metaphi whoami --json
```

- `error.code` `not_signed_in` (from this or any command): go to step 2.
- `key` is null and `no_api_key` is set: the user is signed in but the organization has no key. Tell the user the
  `no_api_key` sentence as it is. Signing in again does not help. Stop.
- `key` is set: go to step 3. `organizations[].credits_usd` is the prepaid balance, when the user may see it.

## 2. Sign in (needs the user, once)

```sh
metaphi login --json
```

It returns at once with `"status": "pending"`: nobody is signed in yet. Show the user `verification_uri_complete`
and `user_code`, and ask them to approve it in their browser within `expires_in` seconds. Running it again while
that code stands returns the same code. Then:

```sh
metaphi login --complete --json
```

This waits up to 60 seconds for the approval (`--timeout <seconds>` changes that). `error.code`
`authorization_pending` means not approved yet: run it again, or stop and wait for the user to say they have.
`"keys": true` means the organization's live and test keys are saved on this machine. `"keys": false` comes with
`no_api_key`, the reason: tell the user, and stop.

On a server or in CI there is no sign-in: `METAPHI_API_KEY` is set to a key made in the console.

## 3. Twin a file

```sh
metaphi twin trace.json --test --json        # free; file at most 256 KiB; 20 test runs a day per organization
metaphi twin trace.json --json               # live; draws prepaid credit
```

Result: `{{"output": "trace.twin.json", "id": "twin_..."}}`. The output is `<name>.twin<extension>` beside the input
(`a.jsonl` gives `a.twin.jsonl`) and replaces a file already there; `-o <path>` chooses another. The input file is
never changed.

The result does not list what was replaced. Before telling the user a twin is safe to share, read the output
against the input and say what you checked.

Options that change the result:
- `--confidential <organization>`: public organizations (a well-known company) are left as they are unless named
  here. When the trace names the user's customers or partners, ask the user which to replace. Repeatable.
- `--key <64 hex>`: a consistency key, not an organization key. The same one gives the same person the same
  stand-in in every file, so use one for a set of files that belong together (`openssl rand -hex 32` makes one;
  the user keeps it, as a secret). Without it each file gets a fresh one.

A twin can take minutes. If your command may time out, start it and come back:

```sh
metaphi twin trace.json --no-wait --json     # {{"id": "twin_...", "status": "queued", "next_step": "metaphi twins wait twin_..."}}
metaphi twins wait twin_... --json           # {{"output", "id"}} once it ends; safe to run again
metaphi twins get twin_... --json            # its status now: queued, running, completed, failed
metaphi twins list --json                    # the organization's twins, newest first
```

Add `--test` to `twins` commands for twins started with `--test`. A wait that stops (timeout, interrupt) never
stops the twin: `error.next_step` is the command that comes back for it.

Several files: one `metaphi twin` per file, with one `--key` for all of them. Start each with `--no-wait`, then
`twins wait` each id. On `rate_limited_concurrent`, wait for one to end before starting the next.

## 4. Errors

| `error.code` | What to do |
| --- | --- |
| `not_signed_in`, `login_expired`, `login_refused` | Step 2, from its start. |
| `authorization_pending` | The user has not approved the code yet. Run `metaphi login --complete --json` again, or wait for them. |
| `no_api_key` | Signed in, but the organization has no key. The message says why: tell the user, stop. Signing in again does not help. |
| `out_of_credit` | Nothing was started. Give the user `error.action_url` to add credit, or run `metaphi credits buy --amount <dollars> --no-browser`, show them the Stripe link it prints, and let it wait for the payment. Then run the same command again. Never choose the amount yourself. |
| `rate_limited_concurrent`, `rate_limited_daily` | Too many runs at once, or today. Wait for a run to end, or tell the user. |
| `timed_out` | The twin goes on. Run `next_step`. |
| `twin_unclean`, `session_failed` | The run failed and cost nothing. If `retryable`, run it once more; else tell the user with the `request_id`. |
| `not_text`, `too_large` | The file cannot be twinned as it is. Tell the user. |
| anything else | If `retryable` is true, try once more after a pause. Else tell the user the message and `request_id`. |

## 5. Balance

```sh
metaphi credits --json                       # {{"org", "credits_usd", "can_buy"}}
```

Only owners, admins and billing members see it; anyone else gets an error saying so.

Usage, keys, members and billing are in the console: {config.DEFAULT_ACCOUNTS}/console
"""


def directory(project: bool = False) -> Path:
    """Where Claude Code reads this skill: the person's skills, or this project's with `project`."""
    base = Path.cwd() / ".claude" if project else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "skills" / NAME


def install(project: bool = False, force: bool = False) -> Path:
    """Write the stub; a file there that this command did not write is left alone unless `force`."""
    path = directory(project) / "SKILL.md"
    if path.exists() and MARK not in path.read_text(errors="replace") and not force:
        raise config.Error(f"{path} exists and was not written by metaphi; --force replaces it")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STUB)
    return path


def remove(project: bool = False) -> Path | None:
    """Take the stub away, if this command wrote it; the path removed, or None when there was nothing of ours."""
    path = directory(project) / "SKILL.md"
    if not path.exists() or MARK not in path.read_text(errors="replace"):
        return None
    path.unlink()
    if not any(path.parent.iterdir()):
        path.parent.rmdir()
    return path


def installed() -> bool:
    return any((directory(p) / "SKILL.md").exists() for p in (False, True))
