# cc-paid-fix

A Claude Code plugin that hands a task from your normal chat to a separate
`claude -p` run billed to a Console API key. That run uses API credits instead
of your chat subscription. Results come back into the same chat.

## What it does

- `/cc-paid-fix:run` starts a separate `claude -p --bare` worker. The worker gets
  `CLAUDE_CREDITS_API_KEY` as its `ANTHROPIC_API_KEY`. Your interactive session
  keeps its existing billing. The worker never falls back to subscription credentials.
- By default the worker is read-only: it gets Read, Glob and Grep, scoped to the
  working directory. Editing and shell access need explicit flags.
- Defaults: model `opus`, effort `high`, $10 estimated budget, 50 turns, 30 minutes.
- The worker gets a short handoff that Claude writes from the current chat.
  `--full-context` forks the saved chat transcript instead. `--fresh` sends only
  the task.
- Run records are saved under `~/.claude/credits/runs/<run-id>/`.

Requires macOS or Linux, Python 3.9+ and Claude Code 2.1.286+.

## Install

Store a dedicated Console API key once. Use a key with a spend limit, and don't
paste it into the chat.

```bash
python3 scripts/cc_paid_fix.py setup    # hidden prompt; stored in the macOS Keychain, else ~/.claude/credits/key (0600)
```

From inside Claude Code you can run the same command with the `!` prefix, using
the plugin's install path. If there is no terminal, pipe the key in:
`pbpaste | python3 scripts/cc_paid_fix.py setup`.

The runner looks for the key in this order: the `CLAUDE_CREDITS_API_KEY`
environment variable, the Keychain entry `cc-paid-fix` (macOS, default state
directory only), then the key file. `doctor` shows which source is used, never
the value. `logout` deletes the stored key; an exported variable is not affected.
Don't set `ANTHROPIC_API_KEY` for your normal session.

Then, inside Claude Code:

```text
/plugin marketplace add soupdiver/cc-paid-fix
/plugin install cc-paid-fix@cc-paid-fix
```

If the commands don't show up, restart Claude or run `/reload-plugins`.

## Usage

```text
/cc-paid-fix:run Review the migration we discussed and list correctness risks.
/cc-paid-fix:run --budget 20 --model opus --effort max --full-context Second opinion on the design.
/cc-paid-fix:run --fresh --budget 5 Explain the architecture of this repository.
/cc-paid-fix:run --edit --allow-bash --budget 15 Implement the agreed change and run the tests.
/cc-paid-fix:followup <run-id> --budget 5 Recheck your second finding against the tests.
/cc-paid-fix:status
```

- `--edit` adds Edit and Write, but denies edits to `.git/`, `.claude/`, `.mcp.json`
  and `.envrc`.
- `--allow-bash` adds the Bash tool.
- `--effort` accepts `low`, `medium`, `high`, `xhigh` or `max`.
- `followup` resumes that specific worker with the same permissions and gives it
  a new run ID.
- `status` runs `doctor` and `list` locally, without an API call. `doctor` checks
  the CLI version, whether a key is found and from where, and whether the CLI flags the runner
  needs are present.

The runner can also be used directly:
`python3 scripts/cc_paid_fix.py doctor | list | show <run-id>`, and
`run --dry-run` with a JSON request on stdin.

## Caveats

- The budget is an estimate that the CLI uses as a stopping threshold. It is not
  a hard billing cap, and a single request can go over it. Set spend limits in
  the Console as well.
- `--allow-bash` gives the worker a shell: it can run any command, write files and
  use the network. Tool restrictions are not an OS sandbox.
- Real Anthropic billing decides whether a run counts against promotional API
  credits. This plugin cannot check your balance. Run it from the Claude Code
  terminal, not from IDE, desktop or GitHub Action integrations.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a fake `claude` executable and make no API calls.
`CC_PAID_FIX_REAL_CLI=1` also checks the installed `claude --help` for every flag
the runner uses.
