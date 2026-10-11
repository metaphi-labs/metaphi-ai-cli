# metaphi

The Metaphi AI command line. Sign in, manage your organization's people and keys, and anonymize agent traces.

## Install

```sh
brew install metaphi-labs/tap/metaphi    # macOS and Linux
```

No Homebrew? Download the binary for your platform from the
[latest release](https://github.com/metaphi-labs/metaphi-ai-cli/releases/latest), unpack it and put `metaphi` on your
`PATH`. Each release lists its `checksums.txt`.

Calling it from Python instead: `pip install metaphi-ai` gives you the same command and the library (see below).

## Quickstart (2 minutes)

```sh
metaphi login                  # opens the browser; confirm the code you see in the terminal
metaphi keys create prod --save
metaphi credits                # your organization's prepaid balance
metaphi anonymize trace.json   # writes trace.anonymized.json
```

`metaphi login` signs you in as yourself. `metaphi keys create` makes an organization key (`mai_sk_...`). The key is
shown once, so store it as a secret. `--save` also keeps it on this machine: in the OS keychain when there is one
(macOS Keychain, Windows Credential Locker, Secret Service on Linux), otherwise in the config file.

A key you already have is saved with `metaphi login --with-api-key`, which reads it from stdin (or prompts without
echo), so it never lands in your shell history or the process list.

## Use it from your servers

Every call that does work runs on an organization key, never on a person's sign-in:

```sh
export METAPHI_API_KEY=mai_sk_...
metaphi anonymize trace.json -o out.json --json
```

One trace file in, one twinned file out, in the same format. `--json` prints `{"output"}` on stdout, with progress
on stderr. Or from Python:

```python
from pathlib import Path
from metaphi.anonymize import anonymize

result = anonymize(Path("trace.json"), Path("out.json"), api_key="mai_sk_...")
result["output"]   # where the twinned trace was written
```

One key per environment (`prod`, `staging`) or per customer is a good default. Revoking one stops only that key.

### Anonymize options

| Flag | What it does |
| --- | --- |
| `-o, --output` | Where the anonymized trace goes. Default: `<name>.anonymized<ext>`. |
| `--key <64 hex>` | Keys the stand-in values. The same key gives a person the same stand-in in every trace, so a corpus stays consistent. Default: a fresh key per trace. |
| `--confidential <org>` | Replaces an organization even though it is public, e.g. your customer. Repeatable. |

The organization key comes from `METAPHI_API_KEY`, then the saved key. (`--api-key` still works but is deprecated: an
argument is visible in shell history and to other processes.)

People, private organizations, accounts and secrets are always replaced, each with one consistent synthetic value of
the same kind, and the trace keeps its structure. An adversarial pass then reads the output for anything still real.

## Your organization

```sh
metaphi whoami
metaphi members list
metaphi members add dev@yourco.com --role admin   # admins manage keys and people; members don't
metaphi members remove dev@yourco.com
metaphi keys list
metaphi keys revoke key_0123abcd
```

If you manage more than one organization, name it with `--org <id>`.

## Credits

Each completed trace draws your organization's prepaid credit; a run that fails costs nothing. When the balance
reaches zero, new runs are refused until credit is added.

```sh
metaphi credits                       # the balance (owners, admins and billing members)
metaphi credits buy --amount 200      # pay through Stripe Checkout in the browser; waits until it lands
metaphi members add ap@yourco.com --role billing   # sees the balance and buys credit, nothing else
```

Only owners and billing members can buy.

## Configuration

| Variable | Default |
| --- | --- |
| `METAPHI_API_KEY` | the key saved by `metaphi login --with-api-key` or `keys create --save` |
| `METAPHI_HOME` | `~/.config/metaphi` (directory mode 700, config file mode 600) |
| `METAPHI_NO_KEYRING` | unset: secrets go to the OS keychain when one is available; `1` keeps them in the config file |
| `METAPHI_ALLOW_CUSTOM_HOST` | unset: the CLI talks only to `https://*.metaphi.ai`; `1` allows another https host |

Sign out with `metaphi logout`. It ends this sign-in everywhere it is used.
