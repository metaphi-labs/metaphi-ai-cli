# kennet

The command line for Kennet, Metaphi AI's RL infrastructure. Sign in and twin agent traces.

## Install

```sh
curl -fsSL https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest/download/install.sh | sh
```

macOS and Linux, x86_64 and arm64, no root; it needs `ssh-keygen` (OpenSSH), which both have. It checks that the
latest release's `checksums.txt` is signed by Metaphi AI's release key, that the binary matches it and that it
starts, then puts it in `~/.local/share/kennet/versions/<version>/` with `~/.local/bin/kennet` (or
`$KENNET_INSTALL_DIR/kennet`) a symlink to it. It says the line to add if that directory is not on your `PATH`, and
installs the Claude Code skill when Claude Code is there. Run it again to upgrade; a plain-file install from an
earlier version is moved to this layout. The binary then keeps itself current (below).

```sh
brew install metaphi-labs/tap/kennet    # Homebrew, macOS and Linux
pip install metaphi-ai                  # Python 3.10+, with the library
```

Or download the binary for your platform from the
[latest release](https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest), and check `checksums.txt.sig`
(`ssh-keygen -Y verify -n kennet-release`, the key is in `install.sh`) and `checksums.txt`. A binary put there by
hand does not update itself; the notice names install.sh.


### Staying current

`kennet --version` prints the version. Once a day `kennet` looks at the releases, on a thread that never holds a
command up, and when a newer one is out says so once, on stderr after the command's output, with the upgrade command
for how this copy was installed:

| Installed with | Upgrade |
| --- | --- |
| `install.sh` | nothing: it updates itself (below), or `kennet update` now |
| Homebrew | `brew upgrade metaphi-labs/tap/kennet` |
| `pip`, `pipx`, `uv tool` | `pip install -U metaphi-ai`, `pipx upgrade metaphi-ai`, `uv tool upgrade metaphi-ai` |
| a release binary put somewhere by hand | the install.sh line above |

The notice is never shown under `--json`, when stderr is not a terminal, in CI (`CI` set), or with
`KENNET_NO_UPDATE_NOTIFIER=1`, and at most once a day.

What install.sh installed updates itself in the background, as Claude Code does. The new version goes in a
directory of its own, only once its `checksums.txt` carries Metaphi AI's release signature, its archive matches,
and it answers `--version`; then the `kennet` symlink is flipped to it in one rename. A command already running
keeps running on its own version, and old versions are removed once nothing runs them (the last two are kept).
Downloads go to GitHub only. It never touches a Homebrew or pip install, is off in CI, and
`KENNET_DISABLE_AUTOUPDATE=1` turns it off for a pinned version. A failure says nothing on screen; it is a line in
`~/.config/metaphi/update.log`. `kennet update` looks and installs now, or prints the command for any other
install; when it cannot, it says why in one line with the next step (`update_unreachable` is worth a retry; a
signature or checksum refusal is not).

When the service no longer serves your version, every command fails with `upgrade_required` (exit 5), and
`next_step` is the upgrade command above.

## Quickstart (2 minutes)

```sh
kennet login                  # opens the browser; confirm the code you see in the terminal
kennet twin trace.json        # writes trace.twin.json
```

Working through Claude Code? `kennet skill install`, then ask it to twin the file: see
[Driving it from an agent](#driving-it-from-an-agent).

`kennet login` signs you in as yourself and hands this machine your organization's keys: a live key (`mai_sk_...`)
and a test key (`mai_sk_test_...`). They are kept in the config file (`~/.config/metaphi/config.json`, mode 600) and are never printed. No OS keychain is used: it prompts a person, and an agent cannot answer. Logging in again
replaces them; `kennet logout` revokes them. If your organization has no twinning access yet, the sign-in still
works and says why no key was saved.

`kennet twin trace.json --test` runs on the test key. Test runs go through the same pipeline but cost nothing; files
can be at most 2 MiB and an organization gets one test run in all, so use it on a representative file. Test twins and live twins never see each other.

## Driving it from an agent

```sh
kennet skill install          # once per machine: Claude Code now knows this CLI
```

That writes the `kennet` skill to `~/.claude/skills/kennet` (`--project` writes it to `./.claude/skills` instead, to commit
for a team). Then ask Claude for what you want: "twin `trace.jsonl` before I send it to the vendor". The skill is a
pointer: it tells the agent to run `kennet skill show`, which prints the guide for the version installed, so the
guide never goes stale. Any other agent can be handed the output of `kennet skill show`.

The CLI is made to be run by a coding agent (Claude Code and the like) as much as typed. Away from a terminal
nothing waits on a question, and every command takes `--json`: stdout is then the result, or the error object,
alone, and progress goes to stderr. An error that one command clears names it: `Next: <command>` on stderr,
`next_step` in the JSON.

```sh
kennet login --json           # {"verification_uri", "user_code", "expires_in", "next_step": "kennet login --complete"}
                               # the person opens the page and confirms the code
kennet login --complete       # waits for the approval (60 s away from a terminal) and saves the keys; safe to run again
kennet whoami --json          # asks the servers: email, organization, balance, and whether each key works
kennet twin trace.json --no-wait --json    # {"id", "status", "next_step": "kennet twins wait twin_..."}
kennet twins wait twin_... --json          # {"output", "id"} once it ends
kennet twins list --json      # the organization's twins, newest first
```

Away from a terminal `kennet login` is `--no-wait` by itself: it prints the page and the code and returns, so the
agent can show them to its person. `--wait` keeps the old behaviour, one call that waits for the approval.

A twin started is the service's, not the terminal's. Ctrl-C, a closed session or `--timeout` stop the waiting, never
the twin, and say how to come back: `kennet twins wait <id>` (add `--test` for a test twin), from any machine
holding the organization's key.

## Use it from your servers

A server or CI job has no browser: make it a key of its own in the console and hand it over as a secret.

```sh
export METAPHI_API_KEY=mai_sk_...
kennet twin trace.json -o out.json --json
```

`METAPHI_API_KEY` overrides whatever is saved. A key you already have can also be saved with
`kennet login --with-api-key`, which reads it from stdin (or prompts without echo), so it never lands in your shell
history or the process list.

One trace file in, one twinned file out, in the same format. `--json` prints `{"output", "id"}` on stdout, with
progress on stderr. Or from Python:

```python
from pathlib import Path
from metaphi.twin import twin

result = twin(Path("trace.json"), Path("out.json"), api_key="mai_sk_...")
result["output"]   # where the twinned trace was written
```

One key per environment (`prod`, `staging`) or per customer is a good default. Revoking one stops only that key.

### Twin options

| Flag | What it does |
| --- | --- |
| `-o, --output` | Where the twinned trace goes. Default: `<name>.twin<ext>` beside the input. |
| `--out <dir>` | The directory the twin goes in, as `<name>.twin<ext>`; made if missing. |
| `--key-file <path>` | A file holding the consistency key (64 hex characters). The same key gives a person the same stand-in in every trace, so a corpus stays consistent. `KENNET_CONSISTENCY_KEY` is read when no file is named. Default: a fresh key per trace. (`--key <64 hex>` takes it as an argument, where the process list shows it.) |
| `--confidential <org>` | Replaces an organization even though it is public, e.g. your customer. Repeatable. |
| `--test` | Runs on the test key `kennet login` saved: free, small, apart from live twins. |
| `--no-wait` | Starts the twin and returns its id; `kennet twins wait <id>` comes back for it. |
| `--timeout <seconds>` | How long to wait for the twin. The twin goes on past it. |

The organization key comes from `METAPHI_API_KEY`, then the saved key. (`--api-key` still works but is deprecated: an
argument is visible in shell history and to other processes.)

People, private organizations, accounts and secrets are always replaced, each with one consistent synthetic value of
the same kind, and the trace keeps its structure. An adversarial pass then reads the output for anything still real.

### Errors

Every error from the service carries a stable `code`. Switch on the code or the exception class, never on the
message. On a failure the CLI prints:

```
error: This organization's prepaid balance is used up; add credits to continue. Nothing was started. (code: out_of_credit, request: req_5f1c...)
```

With `--json`, stdout also gets the error object:
`{"error": {"type", "code", "message", "retryable", "next_step", "request_id"}}`, plus `run_id` when a run failed,
`action_url` when a page fixes it and `retry_after_s` when the service said how long to wait. `next_step` is the
command that clears the error, or `null` when none does. Quote the `request_id` to support.

The exit status says what kind of end it was:

| Exit | Meaning |
| --- | --- |
| 0 | Done. |
| 1 | Failed. |
| 2 | The command line was wrong. |
| 3 | A sign-in or a key is needed (`auth_error`). |
| 4 | The same call may work later: the service is unavailable, a limit is reached, or a wait ran out. |
| 5 | This version is too old for the service (`upgrade_required`); `next_step` upgrades it. |

A request that fails in transit or with a retryable error is sent again at most twice, within 5 seconds in all;
a longer `Retry-After` is handed back as `retry_after_s`, not waited out.

From Python, each error `type` raises its own class from `metaphi.errors`. All of them subclass `MetaphiError`:

```python
from pathlib import Path
from metaphi.errors import BillingError, MetaphiError, RateLimitError
from metaphi.twin import twin

try:
    twin(Path("trace.json"))
except BillingError:            # out_of_credit
    ...                         # add credit: `kennet credits buy`
except RateLimitError as e:     # rate_limited_concurrent, rate_limited_daily
    ...                         # e.retryable is True: try again when a run ends
except MetaphiError as e:
    print(e.code, e.request_id, e.retryable)
```

| Class | `type` | Codes, for example |
| --- | --- | --- |
| `BillingError` | `billing_error` | `out_of_credit` |
| `AuthError` | `auth_error` | `no_organization` |
| `RateLimitError` | `rate_limit_error` | `rate_limited_concurrent`, `rate_limited_daily` |
| `InvalidRequestError` | `invalid_request_error` | `invalid_key`, `upload_already_run`, `task_not_found` |
| `RunError` | `run_error` | `twin_unclean`, `not_text`, `too_large`, `session_failed`, `budget_exceeded` |
| `IdempotencyError` | `idempotency_error` | `idempotency_key_reused`, `idempotency_key_in_use` |
| `APIError` | `api_error` | `unavailable`, `balance_unavailable`, `run_unavailable` |

`retryable` says whether making the same call again later may succeed. For a failed run, it means uploading the same
trace again.

The CLI says a few errors itself, each with the command that clears it as `next_step`. They are `auth_error`s,
but for the last:

| Code | When | `next_step` |
| --- | --- | --- |
| `not_signed_in` | no sign-in on this machine, or the server has ended it | `kennet login` |
| `authorization_pending` | `login --complete` returned before the code was approved (`retryable`) | `kennet login --complete` |
| `no_api_key` | signed in, but the sign-in was handed no key; the message says why. Keys come with a sign-in, so `kennet login` picks them up once the organization has access | `kennet org create <name>` when you are in no organization; `null` while yours waits for approval, and from `login` itself; else `kennet login` |
| `unreachable` (`api_error`) | the twin service did not answer (`retryable`) | `null` |
| `login_expired`, `login_refused` | the sign-in code ran out, or was declined | `kennet login` |
| `upgrade_required` (`upgrade_error`) | the service no longer serves this version (exit 5) | the upgrade command for this install |
| `step_up_required` | a change to the organization's members needs a sign-in more recent than this one | `kennet login` (signs in afresh) |
| `timed_out` (`api_error`) | the wait ended before the twin did; it goes on | `kennet twins wait <id>` |

Any other error the CLI says itself, such as a file that is not there, is a plain `MetaphiError` with `code` `None`.

## Task deliveries

The task sets your organization holds, each a versioned set of Harbor tasks with its images pinned by digest.

```sh
kennet delivery list                         # what you hold, newest first
kennet delivery show eswe-mini@0.1.0         # tasks, images, download size, what a run asks
kennet pull eswe-mini@0.1.0                  # into ./eswe-mini-0.1.0; run again and it fetches only what differs
kennet verify ./eswe-mini-0.1.0              # against its lock, offline; exit 1 if anything differs
kennet delivery diff eswe-mini@0.1.0 eswe-mini@0.1.1    # what changed, and whether a task's grading did
kennet delivery diff ./eswe-mini-0.1.0 eswe-mini@0.1.1  # either side can be a pulled directory
kennet registry login                        # sign docker in to your image repository, for an hour; no key is stored
kennet registry token                        # the token alone, for a pull secret or another client
```

A pulled delivery is `delivery.lock.json`, `SHA256SUMS` and `tasks/<task>/`. The lock names each task's tree hash
and its images by digest; `verify` needs that directory and nothing else. A version never changes: a fix is a new
version, shown as `newer` by `list`. You run the tasks with Harbor on your own machines.

## Your organization

`kennet whoami` asks the servers who you are: your organization, the organizations you manage with your role and
what each may do, the prepaid balance when you may see it, and which key calls run on (the saved one, or
`METAPHI_API_KEY`), by its last four characters. It asks the twin service whether each key works, and exits 0 only
when the one in use does.
If you are in no organization, make yours: `kennet org create "Acme Robotics"`. You own it. During the private beta
Metaphi AI approves each new organization; you are mailed when yours is, and `kennet login` then saves its keys, with no second sign-in.
`kennet whoami` says so while it waits.

A sign-in the server has ended is an error, not a name read back from a file.

Owners and admins manage the organization's members from here (keys are made in the console at
<https://app.metaphi.ai/console>):

```sh
kennet org members                              # members and open invitations, with each one's role
kennet org members add bob@acme.com             # in as a member; --role owner|admin|billing|member
kennet org members role bob@acme.com admin      # change a member's role
kennet org members remove bob@acme.com          # take them out, or take back their invitation
```

An address on a domain your organization claims joins at once. Any other address is mailed an invitation, which
the person accepts signed in as that address, within a week. Only an owner makes, changes or removes an owner,
and the organization always keeps one. A change needs a recent sign-in: when yours is too old, `kennet` offers a
fresh one at a terminal and tries again; elsewhere it fails with `step_up_required` and `kennet login` signs in
afresh. `--org <id>` picks the organization when you manage several.

## Credits

Each completed trace draws your organization's prepaid credit; a run that fails costs nothing. When the balance
reaches zero, new runs are refused until credit is added.

```sh
kennet credits                       # the balance (owners, admins and billing members)
kennet credits buy --amount 200      # pay through Stripe Checkout in the browser; waits until it lands
```

Only owners and billing members can buy; a billing member sees the balance and buys credit, nothing else.

### Running out of credit

When `kennet twin` is refused with `out_of_credit` at a terminal, and you may buy for your organization, it offers to
add credit there and then, opens Stripe Checkout, and carries on with the same run once the payment lands:

```
$ kennet twin trace.json
acme is out of prepaid credit.
Add credit now? Amount in $ [100]:
Pay in the browser:
  https://checkout.stripe.com/c/pay/...
Paid. acme: $100.00 prepaid credit
uploaded trace.json (48,112 bytes)
```

Enter takes $100; `n` or Ctrl-C declines. The run is retried once, under the Idempotency-Key of the first attempt, so
it can never start twice. If you may not buy, it names who can and where.

In CI, a pipe or with `--json` nothing is asked. The command exits 1 with the error and one more line on stderr:

```
Add credit: https://app.metaphi.ai/console?add=credit  or run: kennet credits buy --amount <amount>
```

With `--json` the error object carries the same two things: `action_url`, the page, and `next_step`,
`kennet credits buy`, which prints a Stripe Checkout link and waits until it is paid.

The library never prompts: `twin()` raises `BillingError`, whose `action_url` is the add-credit page when the service
sent one.

## Configuration

| Variable | Default |
| --- | --- |
| `METAPHI_API_KEY` | the key saved by `kennet login` (or `kennet login --with-api-key`) |
| `METAPHI_API` | `https://api.metaphi.ai/v1/kennet`, the Kennet API |
| `METAPHI_HOME` | `~/.config/metaphi` (directory mode 700, config file mode 600) |
| `METAPHI_TWIN_TIMEOUT` | `7200`: seconds `kennet twin` waits for its twin to end (`--timeout` per call) |
| `METAPHI_ALLOW_CUSTOM_HOST` | unset: the CLI talks only to `https://*.metaphi.ai`; `1` allows another https host |
| `KENNET_NO_UPDATE_NOTIFIER` | unset: `1` turns the daily new-release notice off |
| `KENNET_DISABLE_AUTOUPDATE` | unset: `1` stops the release binary from updating itself |

Sign out with `kennet logout`. It ends this sign-in everywhere it is used, revokes the keys `kennet login`
handed out, and clears the saved keys from this machine.
