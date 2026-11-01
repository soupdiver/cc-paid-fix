---
name: status
description: Check cc-paid-fix setup and recent local runs without spending API credits.
disable-model-invocation: true
---

Run these local, non-billed checks:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cc_paid_fix.py" doctor
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cc_paid_fix.py" list
```

Explain any setup issue and show recent tasks, run IDs, statuses, and estimated
costs concisely. If `flags_missing_from_help` is not empty, warn that the
installed Claude Code may not accept those flags (hidden flags can also appear
there). `run_cost_usd_estimate` is per invocation; `session_cost_usd_estimate`
is cumulative per worker conversation, so don't sum it across follow-ups.
`list` repairs records whose runner died: status `orphaned` means a worker may
still be running and billing. Report it; don't kill processes yourself. Local
estimates are not the Console credit balance. For the real
remaining promotional balance, point to https://platform.claude.com/settings/billing.
Never print or request credentials. The key comes from CLAUDE_CREDITS_API_KEY, the Keychain or the key file (`setup`), deliberately
not ANTHROPIC_API_KEY, so normal interactive chat keeps its existing billing.
