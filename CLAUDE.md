# Claude Code instructions for this repository

This is a public starter kit. Customers fork or clone it to use as a reference
implementation. Anyone working in this repo with an AI agent should follow the
rules below — most are about not destroying the customer's local data.

## ABSOLUTE RULE: never destroy customer data without explicit instruction

The Flask app at `examples/flask-starter/` persists the customer's actual
workspace data to a local SQLite file (`data.db`). Re-syncing requires API
calls that cost time, money, and quota — and some data (teammate scanner
uploads, Gmail/Calendar history) cannot be reproduced from any API at all.
**Treat their `data.db` as production data.**

### Tables that hold customer data — NEVER `DELETE FROM` or `DROP` without an explicit user instruction

| Table | Why it's expensive or impossible to restore |
|---|---|
| `connections` | Full per-target connector data; minutes of API calls to re-sync |
| `target_owners` | Index built from connections; lost if connections is wiped |
| `targets_cache` | Full /targets list; always re-fetchable but eats quota |
| `connector_paths` | Connector-first path index |
| `discovered_paths` | First-seen timestamps; LOST FOREVER if deleted |
| `intro_requests` | User-flagged "marked as requested" state |
| `app_state` | Sync timestamps, OAuth-pinned email, etc. |
| `gmail_contacts` | Customer's Gmail history (re-OAuth + ~5 min re-sync) |
| `calendar_contacts` | Customer's Calendar history (same) |
| `teammate_contacts` | Uploaded by teammates via portable scanner — CANNOT be re-fetched without that teammate re-running the scan |
| `linkedin_resolutions` | Cached Apollo / CSE / OpenAI lookups; each costs API quota |
| `slack_config` | Webhook URL; revocable but the 7-step Slack-app setup is annoying |
| `team_members` | Per-teammate Slack IDs + emails; manually entered |
| `candidate_status` | User's triage state on Supporters page (star, hide, mark) |

If you find yourself wanting to reset state for a test, **copy `data.db` to a
temp path first** (`cp data.db /tmp/data.db.bak`) or use
`DRAFTBOARD_DB_PATH=/tmp/test.db` to point the app at a throwaway DB. Never
`DELETE FROM connections` or any of the tables above to "get a clean state."

### Tables you may freely wipe IF your branch added them and you're testing

A brand-new feature you're building can `DELETE FROM` rows it itself wrote —
e.g. seeded test rows you inserted with a unique tag. Use a `WHERE` clause
scoped to those rows only. Never use `DELETE FROM <table>` without a `WHERE`.

### What "explicit instruction" looks like

Required: the user says something like *"delete my data.db"*, *"wipe the
connections table"*, *"reset the cache"*, or names a specific table to clear.

NOT enough: *"let's clean this up"*, *"let's start fresh"*, *"reset the
state"*. These are ambiguous — ask first.

## Other rules

- Never commit secrets. `.env` and `~/.draftboard-secrets/` are gitignored — keep it that way.
- All work goes through a PR. Don't push directly to `main`.
- Don't run destructive git operations (`reset --hard`, `clean -fd`, `branch -D`,
  worktree removal) on branches the user might still need without asking first.
