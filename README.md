# cc-paid-fix

A Claude Code plugin that runs a task as a separate `claude -p` job billed to a
Console API key, so it uses API credits instead of your chat subscription. The
result comes back into your current chat.

Requires macOS or Linux, Python 3.9+ and Claude Code 2.1.286+.

## Install

1. Add the plugin inside Claude Code:

   ```text
   /plugin marketplace add soupdiver/cc-paid-fix
   /plugin install cc-paid-fix@cc-paid-fix
   ```

   If the commands don't show up, restart Claude or run `/reload-plugins`.

2. Store a dedicated Console API key once. Use a key with a spend limit, and never
   paste it into the chat. From a clone of this repo:

   ```bash
   python3 scripts/cc_paid_fix.py setup
   ```

   It asks for the key with hidden input and stores it in the macOS Keychain, or
   in `~/.claude/credits/key` (mode 0600) elsewhere. With no terminal, pipe it in:
   `pbpaste | python3 scripts/cc_paid_fix.py setup`.

3. Check it: `/cc-paid-fix:status` shows the CLI version, whether a key was found
   and where, and recent runs. It makes no API call.

The key is looked up in this order: the `CLAUDE_CREDITS_API_KEY` environment
variable, the Keychain entry `cc-paid-fix`, then the key file. The Keychain is
skipped if `CLAUDE_CREDITS_HOME` is set. `logout` deletes the stored key and
leaves an exported variable alone. Don't set `ANTHROPIC_API_KEY` for your normal
session; the worker never uses it.

## Use

```text
/cc-paid-fix:run Review the migration we discussed and list correctness risks.
/cc-paid-fix:run --budget 20 --effort max --full-context Second opinion on the design.
/cc-paid-fix:run --fresh --budget 5 Explain the architecture of this repository.
/cc-paid-fix:run --edit --allow-bash Implement the agreed change and run the tests.
/cc-paid-fix:followup <run-id> --budget 5 Recheck your second finding against the tests.
/cc-paid-fix:status
```

| Flag | Effect |
| --- | --- |
| `--budget <usd>` | Spending threshold for the run. Default $10. |
| `--model <name>` | Default `opus`. |
| `--effort <level>` | `low`, `medium`, `high` (default), `xhigh` or `max`. |
| `--full-context` | Fork the saved chat transcript instead of a short handoff. Sends the whole saved conversation to the API and can cost more. |
| `--fresh` | Send only the task, no chat context. |
| `--edit` | Add Edit and Write. Edits to `.git/`, `.claude/`, `.mcp.json` and `.envrc` stay denied. |
| `--allow-bash` | Add the Bash tool. |

By default the worker is read-only (Read, Glob, Grep, scoped to the working
directory), limited to 50 turns and 30 minutes. `followup` resumes one specific
worker with the same permissions under a new run ID.

Run records are saved in `~/.claude/credits/runs/<run-id>/`. The runner also works
directly: `python3 scripts/cc_paid_fix.py doctor | list | show <run-id>`, and
`run --dry-run` with a JSON request on stdin prints the plan without any API call.

## Caveats

- The budget is a stopping threshold the CLI checks, not a hard billing cap. A
  single request can go over it, so also set a spend limit in the Console.
- `--allow-bash` gives the worker a shell: any command, file writes, network.
  Tool restrictions are not an OS sandbox. The worker is asked to hide the key from
  shell commands, but that is not verified.
- Anthropic's real billing decides whether a run counts against promotional API
  credits. The plugin can't check your balance. Run it from the Claude Code
  terminal, not from IDE, desktop or GitHub Action integrations.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a fake `claude` and make no API calls. `CC_PAID_FIX_REAL_CLI=1`
also checks the installed `claude --help` for every flag the runner uses.
