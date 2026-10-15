"""The Kennet skill, which teaches a coding agent this CLI: `kennet skill install` puts it where Claude Code reads skills.

The file installed is a stub: when to reach for the CLI, and one instruction, run `kennet skill show`. The guide
itself is printed by the binary, so it is always the guide of the version installed and never goes stale on disk.
Kept as text in this module, not a data file, so the one-file binary carries it."""
from __future__ import annotations

import os
from pathlib import Path

from metaphi import __version__, config

NAME = "kennet"   # the skill is Kennet's, as the CLI is
MARK = "<!-- written by `kennet skill install`; edits are overwritten -->"

STUB = f"""---
name: {NAME}
description: Kennet, Metaphi AI's RL infrastructure, driven through its CLI, `kennet`: synthetic twinning of agent traces, and the RL task sets (deliveries) an organization holds, and the organization's members. Use when the user wants to add, remove or change the role of a member of their Metaphi organization (`kennet org members`), or when the user wants to list, pull, verify or update delivered RL tasks (`kennet delivery`), or when the user wants real people, companies, accounts or secrets in a trace, log or transcript file replaced with consistent synthetic stand-ins (a "synthetic twin") before sharing it or training on it, or when the user is authoring, checking or running RL tasks (Harbor bundles) on Kennet: task CI, `eval_ready`, rollouts, wrong solutions; or mentions Metaphi, Kennet twins, `kennet twin`, or a `mai_sk_` key.
---
{MARK}

# Kennet

Run `kennet skill show` and follow what it prints: it is the guide for the version of the CLI on this machine.

If `kennet` is not found, install it, then run `kennet skill show`:

```sh
curl -fsSL https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest/download/install.sh | sh   # macOS, Linux
brew install metaphi-labs/tap/kennet     # or Homebrew
pip install metaphi-ai                   # or anywhere with Python 3.10+
```
"""

GUIDE = f"""# Kennet from the command line (kennet {__version__})

Kennet is Metaphi AI's RL infrastructure. Through this CLI it offers synthetic twinning (steps 3 to 5) and the
task sets your organization holds, its deliveries (step 6). Steps 1 and 2, the sign-in, are the same for both.
Joining an organization that invited the user, and managing its members, is step 7; authoring and checking tasks
is step 8.

`kennet twin <file>` uploads one trace file (JSON, JSON Lines or text) to Metaphi AI's service and writes its
synthetic twin beside it: the same file, same structure, with every real person, private organization, account
and secret replaced by one consistent synthetic value of the same kind. It is called a twin, never a redaction.
The file leaves this machine to be twinned, on a test run as on a live one: say so if the user's concern is where
their data goes.

## Rules

- Add `--json` to every command. stdout is then one JSON object: the result, or `{{"error": {{...}}}}`. Progress
  is on stderr. Read stdout only: stderr repeats an error in words, for a person. Under `--json` nothing opens a
  browser: `kennet login --json` prints the page and the code for the user to open.
- The exit status says what kind of end it was:

  | Exit | Meaning |
  | --- | --- |
  | 0 | Done. |
  | 1 | Failed. Read `error.code`. |
  | 2 | The command line was wrong (no JSON is printed). |
  | 3 | A sign-in or a key is needed. `error.next_step` is the command, or null when only the user can clear it. |
  | 4 | The same call may work later: the service is unavailable, a limit is reached, or a wait ran out. |
  | 5 | This version of `kennet` is too old for the service (`upgrade_required`). |

- On an error, read `error.code` and find it in the table in step 4. `error.next_step` is always there: the next
  command to run (add `--json` to it), or null when no command clears it. `error.advice`, when there, is what
  the user does that one command does not (sign in as another address, ask an owner): tell them. `error.retry_after_s`, when there, is
  how long the service said to wait before the same call. Never decide from the message text.
- A command retries a failed request for at most 5 seconds by itself. Past that it returns the error, so a
  command that fails does so quickly; waiting longer is yours to do.
- Never ask the user to paste a token, key or code into the chat, never print one, never pass one as an
  argument. The one code they handle is the sign-in's `user_code`, which they check on the page. Keys are saved by
  `kennet login` or come from `METAPHI_API_KEY`.
- An organization has one free test run in all (`--test`): a real twin through the same pipeline, of a file up
  to 2 MiB. Spend it on a representative file, and ask the user which one before you do. Once it is used the
  refusal is `test_runs_used` (exit 1; `next_step` is the same command without `--test`): runs from then on are
  live. A live run draws the organization's prepaid credit:
  ask the user before the first one, even after a good test run.
- `kennet` says nothing about new releases under `--json`. What install.sh installed updates itself in the
  background, only to a release signed by Metaphi AI (`KENNET_DISABLE_AUTOUPDATE=1` stops it). `kennet update
  --json` answers `version`, `latest`, `method` and `next_step`, the upgrade command, when this install is not
  the newest.
- When a step needs the user and they are not there, stop and hand back what they have to do. Do not work around it.
- When something fails that you cannot clear, quote `error.request_id` to the user, or `error.run_id` (the twin's
  id) for a run that failed.

## 1. Where things stand

```sh
kennet whoami --json
```

It asks the servers, and asks the twin service whether each key works: `key.status` and `test_key.status` are
`"ok"` or the code a call would meet now. Exit 0 means the key in use works.

- `key.source` is `METAPHI_API_KEY`: the environment holds a key. No sign-in is needed, on this machine or in
  CI; go to step 3. (That key decides live or test by itself: `mai_sk_test_...` is a test key.)
- `error.code` `not_signed_in` (from this or any command): go to step 2.
- `error.code` `no_api_key`: the user is signed in, and the sign-in was handed no key. `reason` beside `error`
  says why:

  | `reason` | Means | What to do |
  | --- | --- | --- |
  | `no_organization` | The user is in no organization. | Ask whether their company already uses Kennet. If it does, an owner or admin there adds them (`kennet org members add`, step 7), then `kennet login --json`. If not, ask what theirs is called and make it: `kennet org create "Acme Robotics" --json`. They own it. `"admission": "pending"`: Metaphi AI approves it, and the user is mailed; stop until they say it came, then `kennet login --json`. `"admission": "admitted"`: run that now. |
  | `awaiting_approval` | Their organization waits for Metaphi AI to approve it. | Tell the user they are mailed when it is; then `kennet login --json`. Nothing else clears it. |
  | `no_keys_yet` | The organization was just made or approved. | `kennet login --json` once. |
  | `no_access` | Their organization holds no access to this yet. | Tell the user to ask their Metaphi AI contact, then stop. |
  | `service_account` | The sign-in is a service account, not a person. | A service account's key is made in the console and set as `METAPHI_API_KEY`; a person signs in as themselves (step 2). |
  | `invitation_pending` | An organization has invited the user and they have not accepted. `invitation` beside `error` names it (`org`, `org_name`, `role`, `expires`). | Ask the user whether to join it; on yes, run `error.next_step`, `kennet org join <org> --json` (step 7). Never make an organization while an invitation waits. |
  | `removed` | An owner or admin took the user out of their organization. | Only an owner of that organization can add them back: tell the user so, with the message, and stop. `error.next_step` is null. |

  Any other `reason`, or none: run `kennet login --json` once (a signed-in machine opens no browser); if it ends in
  `no_api_key` again, tell the user the message as it is, and stop.
- Any other `error.code`: the table in step 4. `email`, `org` and `organizations` are beside `error`.
- `invitations`, when there, are the invitations waiting for the user, as accounts lists them now, each with its
  `id` when accounts sends one: `kennet org join <org> --json` (or the `id`) accepts one.
- When the saved key is refused, `whoami` reads why without changing any key: an invitation waiting is
  `kennet org join <org>`; otherwise the message says whether the key was replaced by a sign-in elsewhere or the
  user is in no organization, and `next_step` is `kennet login`, which says the rest.
- Exit 0: go to step 3. `org` is the organization the keys belong to. `organizations` lists each organization the
  user is in, with their `role` and what it holds (`grants`), and `credits_usd`, the prepaid balance, when they may
  see it.

## 2. Sign in (needs the user, once)

```sh
kennet login --json
```

It returns at once with `"status": "pending"`: nobody is signed in yet. With `--json` it never opens a browser
(at a terminal, without `--json`, it opens the page; `--no-browser` only prints it). Tell the user, with the page
and code exactly as printed:

- Open `verification_uri_complete` in any browser: this machine's, or another device's (a phone, a laptop) when
  this one has none, as over SSH or in a sandbox. The code ties the page to this terminal.
- Sign in with Google or GitHub as their work email, the address their organization knows them by. The address
  must be verified with that provider.
- Before approving, check that the page names the Kennet CLI and shows the same code as `user_code`; if either
  is off, do not approve. Approve within `expires_in` seconds.

Running it again while that code stands returns the same code. Then:

```sh
kennet login --complete --json
```

This waits up to 60 seconds for the approval (`--timeout <seconds>` changes that). `error.code`
`authorization_pending` means not approved yet: run it again, or stop and wait for the user to say they have.
`"keys": true` means the organization's live and test keys are saved on this machine: run `kennet whoami --json`
and tell the user the `email` and `org` it names. `error.code` `no_api_key` means the sign-in worked and no key
came with it: `reason` beside `error` says why (step 1's table). Each sign-in replaces the keys the one before it
saved on any machine. On a machine that is signed in already, `kennet login --json` opens no browser and returns
this result at once.

When the page does not get there:

| The user sees | What to do |
| --- | --- |
| Nothing opens, or the sign-in window closes at once | The browser blocked the popup: allow popups for the page, and click sign-in again. |
| "email not verified" | The address is not verified with that provider: verify it there (GitHub: Settings, Emails), or sign in with the other provider. |
| "that code is unknown or expired" (`unknown_or_expired`) | Run `kennet login --json` for a new code, and start again. |
| "that sign-in is too old to approve a terminal" (`sign_in_too_old`) | Sign in again on the page with the button, then approve. |
| `whoami` names the wrong address | Run `kennet logout`, then step 2 again; ask them to sign out of that account in the browser, or use a private window. |

Servers and CI never sign in: an owner or admin makes the organization a key in the console
(https://app.metaphi.ai/console), and it is set as `METAPHI_API_KEY`, or saved on the machine by
`kennet login --with-api-key`, which reads it from stdin. The user does that; you never see the key.

## 3. Twin a file

```sh
kennet twin trace.json --test --json        # free; file at most 2 MiB; one test run in all per organization
kennet twin trace.json --json               # live; draws prepaid credit
```

Result: `{{"output": "trace.twin.json", "id": "twin_..."}}`. The output is `<name>.twin<extension>` beside the input
(`a.jsonl` gives `a.twin.jsonl`, `notes.txt` gives `notes.twin.txt`) and replaces a file already there.
`--out <directory>` writes it there under the same name instead, which keeps twins apart from the originals;
`-o <path>` names the file itself. The input file is never changed.

The result does not list what was replaced. Before telling the user a twin is safe to share, read the output
against the input and say what you checked.

Options that change the result:
- `--confidential <organization>`: public organizations (a well-known company) are left as they are unless named
  here. When the trace names the user's customers or partners, ask the user which to replace. Repeatable.
- `--key-file <path>`: a file holding a consistency key (64 hex characters), which is not an organization key.
  The same one gives the same person the same stand-in in every file, so use one for a set of files that belong
  together. `(umask 077; openssl rand -hex 32 > twin.key)` makes one; the user keeps it, as a secret. The
  `KENNET_CONSISTENCY_KEY` variable is read when no file is named. Without either, each file gets a fresh one.

A twin can take minutes. If your command may time out, start it and come back:

```sh
kennet twin trace.json --no-wait --json     # {{"id": "twin_...", "status": "queued", "next_step": "kennet twins wait twin_..."}}
kennet twins wait twin_... --json           # {{"output", "id"}} once it ends; safe to run again
kennet twins get twin_... --json            # its status now: queued, running, completed, failed
kennet twins list --json                    # the organization's twins, newest first
```

Add `--test` to `twins` commands for twins started with `--test`, and `--out <directory>` to `twins wait` as to
`twin`. A wait that stops (timeout, interrupt) never stops the twin: `error.next_step` is the command that
comes back for it. `--no-wait` returns once the file is uploaded and the twin exists, which takes seconds; a
twin that then fails says so at `twins wait` or `twins get`, with the same error object.

Several files: one `kennet twin` per file, with one `--key-file` for all of them. Start each with `--no-wait`,
then `twins wait` each id. On `rate_limited_concurrent`, wait for one to end before starting the next.

## 4. Errors

| `error.code` | What to do |
| --- | --- |
| `network` | This machine could not reach the service (`retryable`; the message names the host and why). Nothing was changed. Wait a few seconds, run the same command once more, then tell the user to check their connection. |
| `service_unavailable` | Something in front of the service (a gateway, a proxy, a captive portal) answered instead of it (`retryable`). Nothing was changed. Wait `retry_after_s` (30 seconds when absent), run the same command once more, then tell the user. |
| `interrupted` | The command was stopped (Ctrl-C, or the process was signalled) before it ended; exit 130. Run it again if it should finish. |
| `internal` | `kennet` stopped on something it did not expect: a bug. Do not retry in a loop. Tell the user, with `kennet --version` and the `debug_log` path beside `error`, so it can be reported. |
| `upgrade_required` | This `kennet` is too old for the service. `next_step` is the upgrade command for how it was installed (`brew upgrade ...`, `pip install -U ...`, `kennet update`, or the install.sh line): ask the user, run it, then the same command again. |
| `update_unreachable` | `kennet update` could not reach GitHub (`retryable`). Try once more later; nothing changed. |
| `update_unsigned`, `update_mismatch`, `update_refused` | The download was not Metaphi AI's signed release, or went elsewhere; nothing was installed. Tell the user; do not retry or work around it. |
| `update_missing`, `update_broken`, `update_not_permitted` | The update could not be installed here; nothing changed. `next_step` is the install.sh line: ask the user before running it. |
| `not_signed_in`, `login_expired`, `login_refused` | Step 2, from its start. |
| `step_up_required` | A change to the organization needs a recent sign-in. Step 2, from its start, then the same command once more. |
| `authorization_pending` | The user has not approved the code yet. Run `kennet login --complete --json` again, or wait for them. |
| `no_api_key` | Signed in, and no key came with the sign-in. If `next_step` is `kennet org create <name>`, the user is in no organization: make theirs (step 1). If it is `kennet login`, run `kennet login --json` once: it picks the keys up, with no browser, if access was given since. If it comes back `no_api_key` again, tell the user the message and stop. |
| `invalid_api_key`, `missing_api_key` | The key was revoked or replaced, often by a later sign-in elsewhere. Step 2, or the user sets a new `METAPHI_API_KEY`. |
| `insufficient_scope` | The key does not hold access to what the command reaches. Run `kennet login --json` once: a signed-in machine is handed new keys with no browser. If it repeats, tell the user. |
| `auth_unavailable`, `unavailable`, `balance_unavailable`, `unreachable` | The service could not answer; nothing was started. Wait `retry_after_s` (30 seconds when absent), run the same command once more, then stop and tell the user with the `request_id`. |
| `run_unavailable` | The twin started and failed on Metaphi AI's side; nothing was written or charged. "Upload the trace again" means run `next_step`, `kennet twin <file>`, with the options you used before. Do that once. If it fails the same way, stop and give the user both `run_id`s. |
| `out_of_credit` | Nothing was started. Give the user `error.action_url` to add credit, or run `kennet credits buy --amount <dollars> --no-browser`, show them the Stripe link it prints, and let it wait for the payment. Then run the same command again. Never choose the amount yourself. |
| `rate_limited_concurrent`, `rate_limited_daily` | Too many runs at once, or today. Wait for a run to end, or tell the user. |
| `timed_out` | The twin goes on. Run `next_step`. |
| `twin_unclean`, `session_failed` | The run failed and cost nothing. If `retryable`, run it once more; else tell the user with the `run_id`. |
| `not_text`, `too_large` | The file cannot be twinned as it is. Tell the user. |
| anything else | If `retryable` is true, wait `retry_after_s` (10 seconds when absent) and try once more. Else, or if it fails again, tell the user the message and `request_id`. |

## 5. Balance

```sh
kennet credits --json                       # {{"org", "credits_usd", "can_buy"}}
```

Only owners, admins and billing members see it; anyone else gets an error saying so.

## 6. Task deliveries

A delivery is a versioned set of RL tasks your organization holds: `<name>@<version>`, as `eswe-mini@0.1.0`. A
version never changes; a fix is a new version. Each task is a Harbor bundle (`task.toml`, `instruction.md`,
`environment/`, `tests/`, `solution/`) whose images are pinned by digest.

```sh
kennet delivery list --json                         # {{"data": [{{"id", "delivered", "task_count", "image_count", "bytes", "newer", "registry"}}]}}
kennet delivery show eswe-mini@0.1.0 --json         # the same, and "tasks": [{{"name", "tree", "images", "cpus", "memory_mb", "agent_timeout_s", "verifier_timeout_s", ...}}]
kennet delivery pull eswe-mini@0.1.0 --json         # into ./eswe-mini-0.1.0 (or --to <dir>); {{"dir", "lock", "fetched", "unchanged", "bytes", "next_step"}}
kennet delivery verify ./eswe-mini-0.1.0 --json     # offline; exit 0 and "ok": true, or exit 1 with "differ" (per file: "delivered" and "here" sha256) and "added"
kennet registry login --json                        # signs docker in to your image repository for an hour; {{"server", "repository", "client", "expires_at"}}
kennet delivery diff eswe-mini@0.1.0 eswe-mini@0.1.1 --json   # {{"added", "removed", "changed": [{{"name", "grading_changed", "files"}}]}}; either side can be a pulled directory
```

From nothing to a set ready to run, in order: `kennet login --json` (and `--complete`), `delivery list`,
`delivery show <id>` to size the machines, `delivery pull <id>`, `delivery verify <dir>`, `registry login`, then
pull each image the lock names. Each answer's `next_step` is the command that follows.

- `pull` is safe to run again: it fetches only the tasks the directory does not already hold as the lock names
  them. A task that was changed is put back whole, as delivered, and anything added inside it goes: keep your
  own files (results, notes, solutions) outside the delivery's directory. `--dry-run` names the tasks it would
  fetch and so replace. A pull that stopped is finished by the same command.
- The pulled directory is `delivery.lock.json`, `SHA256SUMS` and `tasks/<task>/`. The lock is the record: each
  task's `tree` (a SHA-256 over every file's hash, mode and path) and its `images` by digest. `verify` reads that directory
  and nothing else, so it works with no key and no network, on the training machine too. Run it before a run,
  and treat exit 1 as "these are not the tasks that were delivered": pull again before running.
- Size the sandboxes from `show`: a task's `cpus`, `memory_mb`, `storage_mb`, `agent_timeout_s` and
  `verifier_timeout_s` are what it asks. A sandbox smaller than that fails for reasons that are not the model's.
- Images are in `registry`, your organization's own image repository, and a task runs only against the digests
  its lock names: never pull one by tag. `kennet registry login --json` signs docker (or podman, nerdctl) in to
  that repository for an hour; it stores no key, so run it again when a pull is refused or `expires_at` has
  passed. An image is `<registry>/<image>` for each entry of a task's `images`. For anything else that pulls
  (a cluster's pull secret, crane, skopeo), `kennet registry token --json` answers `server`, `username`,
  `token` and `expires_at`: hand the token over on stdin or in memory, never in a file, a log or a command
  line. Fetch every image before a run, since the tasks run with no internet. An organization that gave
  Metaphi AI a service account of its own pulls as that account too, with no token from here.
- Run the tasks with Harbor on your own machines; nothing of Metaphi AI is called at run time. Keep the bundle
  as it is: `tests/` and `solution/` must never be put where the agent being trained can read them.
- Before the first sampling run on a new delivery, check it on those machines with Harbor's two built-in
  agents, which call no model: `oracle` applies each task's `solution/` and must score 1 on every task; `nop`
  changes nothing and must score 0. A task that disagrees does not run here as it was delivered (an image that
  did not pull, a sandbox too small, another Harbor version): leave it out of the run and tell the user the
  task, the delivery id, the task's `tree` and the verifier's output, so it can be reported.
- With every result you keep, record the delivery id and the task's `tree`. Scores compare only within one
  version, and `diff` says, per task, whether its grading changed (`grading_changed`): if it did, scores on
  that task from the older version do not carry over.
- A trial whose verifier output starts with `verifier error:` or `ungradable:` was not graded, although its
  reward reads 0. Do not train on it and do not count it as a wrong answer: run it again, and if it repeats,
  tell the user which task and which delivery.
- `newer` on a delivery names a later version your organization holds. A held-out set, when there is one, is a
  delivery of its own and is never part of a training pull.

Errors: `resource_missing` (no such delivery for your organization; `kennet delivery list`),
`insufficient_scope` (this machine's keys predate deliveries; `kennet login --json` replaces them, with no
browser), `directory_in_use` (the directory holds another delivery; `next_step` names a free one),
`tree_mismatch` (a download was not the locked tree and was thrown away; run the pull again),
`not_a_delivery` (`verify` was pointed at a directory with no lock it can read), `invalid_delivery` (the id is
not `<name>@<version>`), `no_container_client` (nothing to sign in; use `kennet registry token`),
`registry_login_failed` (the client refused; its reason is in the message), `lock_invalid` and `lock_too_new` (tell the user; the second says to upgrade `kennet`).

## 7. Your organization's members

An organization that invited the user (`reason` `invitation_pending`, or `invitation` in `whoami`) is joined with:

```sh
kennet org join acme --json        # or the invitation's id: kennet org join inv_... --json
```

It accepts the invitation, signed in as the address it was sent to, and saves the organization's keys:
`{{"email", "org", "keys": true, "joined": {{"org", "role"}}, "next_step"}}`. Ask the user first: a person is in one
organization. `error.code` `no_invitation`: there is no open invitation from it (expired or taken back); tell the
user to ask an owner of it for a new one. `invitation_for_another_address`: the invitation was mailed to another address than the one signed in;
`next_step` is `kennet logout`, then step 2 signed in as that address (`advice`). `step_up_required`: as below. `no_api_key` after
joining: step 1's table.

Owners and admins manage who is in the organization. Ask the user before every change: whom, and in which role.

```sh
kennet org members --json                             # {{"org", "role", "members": [{{"email", "role"}}], "invitations": [{{"id", "email", "role", "expires"}}]}}
kennet org members add bob@acme.com --role admin --json   # {{"org", "email", "role", "status": "member" or "invited", "invitation" when invited, "members"}}
kennet org members role bob@acme.com billing --json   # the same answer as add
kennet org members remove bob@acme.com --json         # {{"org", "email", "status": "removed" or "uninvited", "members"}}
```

- Roles are exactly these four; `add` defaults to `member`:

  | Role | May |
  | --- | --- |
  | `owner` | everything: members, owners, keys, credit. Only an owner makes, changes or removes an owner, and the organization keeps one. |
  | `admin` | add, change and remove members other than owners; manage keys; see the balance. |
  | `billing` | see the balance and buy credit; nothing else. |
  | `member` | use the organization's keys. |

- `"status": "invited"`: the address is off the organization's domains, so it is not a member yet. Metaphi AI
  mails them an invitation, which they accept signed in as that address, within a week. Tell the user so; there
  is no link to pass on. `remove` on that address takes the invitation back. `error.code` `mail_unavailable`: the
  invitation could not be mailed; the user invites them on the keys page the message names.
- `--org <id>` names the organization when the user manages several; the error lists them.
- `error.code` `not_manager`: the user is in the organization but not an owner or admin, so its members are not
  theirs to see or change. Tell them; there is no command for it.
- `error.code` `step_up_required`: the change needs a recent sign-in. Run `kennet login --json` (and
  `--complete`, step 2) with the user, then the same command once more.
- Any other refusal (only an owner changes an owner; the organization keeps one owner; the address belongs to
  another organization) is the server's sentence in `error.message`: tell the user, and do not retry.

## 8. Author and check tasks

Authoring runs through Kennet's MCP tools, not this CLI. Connect them once, then sign in from `/mcp`:

```sh
claude mcp add --transport http kennet https://kennet.metaphi.ai/mcp/
```

The loop: write a Harbor bundle, check it (`run_task_ci_checks`), run models on it (`run_harbor_tasks`), read
the rollouts (`harbor_runs`, `harbor_run_report`, `harbor_rollouts`), fix, repeat. Long calls return a task:
poll `get_task`.

- A bundle is named by `repo` (owner/name), `ref` and the path to its `task.toml` directory. Kennet reads
  GitHub, never the local checkout: commit and push the ref first. The call needs push on the repository.
- The bundle is a Harbor task: `task.toml`, `instruction.md`, `environment/`, `solution/`, `tests/`. Nothing
  else at its root except `wrong/` and `right/`.
- Wrong solutions (must score below 1.0) go in `wrong/`, right ones (must score 1.0) in `right/`, each a
  `.patch` or a directory laid over the working directory. They sit in the problem directory, never under
  `tests/`: beside the bundle when it is `<problem>/harbor/`, else at the bundle's root. At least one wrong
  solution is needed for `eval_ready`.
- A GPU task names one type: `gpus` and `gpu_types = ["L40S"]` under `[environment]`.
- CI: `eval_ready` true is the verdict. Otherwise `gate.blocking_checks` names what refused, and each finding
  says the fix; `gate.advisory_failed` is advice. Fix the bundle, push, run again.
- Rollouts: `mode` `conduct`, `models`, `harness` and `reasoning_effort` (required; Kennet has no default),
  `k` trials. The models you may run are the `models` field's enum. A refused call says what it needs: tell
  the user, never swap in another model or setting yourself.
- A trial that failed for an infrastructure reason was not graded, whatever its reward reads: run it again,
  and do not count it as the model's failure.
- What no check settles, or a fault in Kennet itself: open an issue on the task's repository with the run id,
  the finding and what was tried, and tell the user.
"""


def directory(project: bool = False) -> Path:
    """Where Claude Code reads this skill: the person's skills, or this project's with `project`."""
    base = Path.cwd() / ".claude" if project else Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "skills" / NAME


def install(project: bool = False, force: bool = False) -> Path:
    """Write the stub; a file there that this command did not write is left alone unless `force`."""
    path = directory(project) / "SKILL.md"
    if path.exists() and MARK not in path.read_text(errors="replace") and not force:
        raise config.Error(f"{path} exists and was not written by kennet; --force replaces it")
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
