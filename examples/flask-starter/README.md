# Flask starter — Draftboard API reference implementation

A working Flask app that implements the foundation build described in [`CONTEXT.md`](../../CONTEXT.md). Pull your existing targets from Draftboard, browse them by target / account / connector, see which paths are owned by you vs. teammates, get notified about high-quality new paths as they're discovered, and click any path to see relationship details with one-click Compose-email.

Use this as the reference for what you're building, or as a starting point you can fork and customize.

## What it does

| Page | What you see |
|---|---|
| `GET /` (Targets view) | Every target in your workspace, sorted by score, paginated 100/page, searchable. Click any row → drawer with paths to that target. Filter by which teammate owns the path. |
| `GET /accounts` | Targets bundled by company, sorted by best path score, paginated 50/page. Click any row → two-column drawer (Targets on left, Mutual Connectors on right). Filter by owner. |
| `GET /connections` | Every connector in your network who can intro to at least one target, sorted by intro count + top score. Searchable. Click any row → drawer showing the targets that connector can intro to, grouped by company, with Compose-email and Assign-to-teammate per target. |
| `GET /new-paths` | Paths discovered recently (default: last 7 days, score ≥ 30). Filterable by lookback window, minimum score, and which teammate owns the path. Empty state has a "Force re-sync all paths" button to seed it from your existing data. |
| `GET /import` | Paste LinkedIn URLs to add as new Targets, with tag suggestions pulled from your existing tags. |

Plus:

- **Background sync** — on first page load (and every `SYNC_INTERVAL_HOURS` hours, default 12), the app fans out 5 parallel workers to fetch every target's connections via `GET /targets/{id}/connections` and persists to a local SQLite file. Drawers open in milliseconds after that. Progress is shown as a pill in the nav.
- **Team-aware ownership** — every connector card shows whether the path is yours, a teammate's, or shared. The "Assign to teammate" dropdown drafts a Gmail message asking a teammate to ping their connector instead of pinging directly.
- **Compose email** — friendly intro request drafts (template + scoreDetails rewritten from API third-person to second-person addressed to the connector). Clicking it opens Gmail compose with subject + plain-text body filled in. If your browser supports the Clipboard API, an HTML version with the target's name hyperlinked to LinkedIn is also copied to the clipboard so you can paste over the body to upgrade.
- **Compose email to teammate** — when the path is owned by a teammate, the Assign-to-teammate dropdown lists the teammates and drafts a "hey, can you ping {connector} for an intro to {target}" email when you click one.

## Setup

### 1. Install

```bash
cd examples/flask-starter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Add your API key

Get one in the Draftboard web app at **Settings → API Keys → "Generate API key"**.

The app checks for the key in three places (in priority order). Pick whichever fits your workflow:

**Option A — `.env` file in this folder** (most common):

```bash
cp env.example .env
# then edit .env to paste your real db-api_... key
```

`.env` is gitignored.

**Option B — `~/.draftboard-secrets/` shared secrets dir** (if you have multiple Draftboard tools that all need the same key):

```bash
mkdir -p ~/.draftboard-secrets
echo "db-api_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" > ~/.draftboard-secrets/draftboard-api-starter
chmod 600 ~/.draftboard-secrets/draftboard-api-starter
```

**Option C — environment variable** (for CI, Docker, etc.):

```bash
export DRAFTBOARD_API_KEY=db-api_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
```

The startup log prints which source the key was loaded from so you can verify.

### 3. Run

```bash
.venv/bin/python app.py
# (override the port with PORT=5051 if 5050 is busy)
```

Open http://localhost:5050.

The first page load takes ~1 minute on a workspace with thousands of targets (the app fetches every target's metadata in one pass). Background sync of per-target connections runs after that — you can browse while it works. With ~4,000 targets it takes ~5 minutes; with the 10–100 targets a typical new user has, it's done in seconds.

## Files

- `app.py` — Flask app, all routes, SQLite-backed connection cache, parallel sync worker + scheduler, ownership logic, message templating
- `templates/` — Jinja templates. `_nav.html` is the top bar with the sync-progress pill. `_drawer_skeleton.html` is the slide-in drawer container plus its JS (drawer routing, clipboard copy, toast, account-drawer target switching). `_connector_card.html` is the path detail card shared between every drawer. `_owner_filter.html` is the team-member filter dropdown shared by Targets, Accounts, and New paths.
- `requirements.txt` — Flask + requests
- `env.example` — copy to `.env` and fill in your real key
- `data.db` — SQLite cache, created on first run. Gitignored. Delete it to force a full re-sync.

## SQLite tables

| Table | What's in it |
|---|---|
| `connections` | One row per Target with the full JSON of `GET /targets/{id}/connections` and a `fetched_at` timestamp. 24-hour TTL. |
| `target_owners` | Denormalized index of `(target_id, owner_id)` pairs with each owner's name and LinkedIn URL. Powers the owner filter. |
| `discovered_paths` | One row per `(target_id, connection_id)` pair, with `first_seen_at` set on first discovery. Powers the New paths tab. **Never backfilled** — only fills as new sync data arrives. |
| `connector_paths` | Connector-first index: one row per `(connector_key, target_id)` pair where `connector_key` is the connector's normalized LinkedIn URL (or name fallback). Lets the Connections view group all of "Cory Moelis"'s 60+ paths under one row even though the API mints a fresh `connection_id` for each (connector, target) pair. Backfilled from existing data on startup. |
| `targets_cache` | Persisted /targets metadata (one row per target, full JSON). Populated on every successful `fetch_all_targets()` call. With this populated, the app runs entirely from SQLite and survives `AUTO_SYNC_ENABLED=false`. Until first bootstrap, the Targets / Accounts / New-paths views show empty states. |
| `intro_requests` | Local-only "Mark as requested" state (currently hidden in the UI but the toggle endpoint and table are wired up). |
| `app_state` | Key-value store. Currently records `last_sync_completed_at` for the "Since last sync" filter. |
| `linkedin_resolutions` | Per-email cache for the LinkedIn resolver. One row per `email` with the resolved URL, confidence, source (apollo / cse / none), and reasoning. 30-day TTL. Avoids re-paying for Apollo/Google CSE/OpenAI calls when the same person gets resolved twice. |

## LinkedIn resolver setup

When the Google Workspace integration is connected, the Candidates page lists high-engagement contacts pulled from your Gmail and Calendar. To import any of them as a Draftboard Target you need their LinkedIn URL — the resolver finds it for you so you don't have to copy-paste.

Two methods, tried in order:

1. **Apollo `/people/match`** — fastest. POST `(email, first_name, last_name)`, get the LinkedIn URL back when Apollo knows the person. Hits ~50–70% of business contacts.
2. **Google Custom Search + gpt-4o-mini** — fallback. Searches `"{first_name} {company} linkedin"`, recovers profile URLs (also from `linkedin.com/posts/...` URLs that Google ranks above the actual profile), dedupes results with snippet merging to bypass LinkedIn's cookie-consent boilerplate, and asks gpt-4o-mini to pick the right candidate.

If both methods miss, the candidate row stays usable — you paste the LinkedIn URL manually before clicking Import.

**All three keys are optional.** Configure none, some, or all via the in-app wizard at [`/settings/linkedin-resolver`](http://localhost:5050/settings/linkedin-resolver) — it has step-by-step setup links for Apollo, Google CSE, and OpenAI. Or set `APOLLO_API_KEY`, `GOOGLE_CSE_API_KEY`, `GOOGLE_CSE_ID`, and `OPENAI_API_KEY` in `.env` (see `env.example`). Wizard saves to `~/.draftboard-secrets/draftboard-api-starter-resolver.json` (mode 0600, owner-only).

Resolutions are cached in SQLite for 30 days so re-resolving the same email is free. The wizard's "Test it" panel calls the resolver with `force: true` so you can verify your keys without waiting for a cache miss.

For bulk lookups (e.g., resolving every contact from an uploaded scanner JSON in one shot), POST a list of `{name, email}` objects to `/candidates/resolve/batch`. Returns one result per input row in the same order, runs up to 5 lookups in parallel, capped at 500 contacts per call. Per-row failures (malformed email, missing name) come back with `error` set on that row only — they don't abort the batch.

## Running fully offline

Once `targets_cache` is populated by at least one successful sync, the app
can run with **zero API calls** by setting `AUTO_SYNC_ENABLED=false`. In that
mode:

- `fetch_all_targets()`, `fetch_me()`, `fetch_tags()` all read exclusively
  from SQLite — no HTTP requests to Draftboard's API.
- `fetch_target_connections()` returns cached data (even stale), no API call.
- The scheduled sync daemon doesn't start.
- The on-page-load auto-trigger is gated off.
- Manual `/sync/start` still works if you explicitly want to refresh.

This is useful for read-only testing, demoing on a flight, or when you've
hit Draftboard's rate limits and need the app to keep working.

## Slack assignments (optional)

Out of the box, the "Assign to teammate" dropdown on each connector card
opens a Gmail compose window with a pre-drafted ask. That works for everyone
with zero setup. Want one-click "ping the teammate in Slack" instead? Visit
**Settings → Slack** in the running app — a 7-step wizard walks you through:

1. Pick a Slack channel (defaults to `#warm-intros`)
2. Create a Slack app + Incoming Webhook for that channel (~3 minutes,
   click-by-click instructions in the wizard)
3. Paste the webhook URL
4. Map your teammates' Slack user IDs in **Settings → Team** (one-time;
   the same page where you map their email addresses for Gmail pre-fill)
5. Send a test message to confirm it works

Once configured, every connector card's "Assign to teammate" dropdown grows a
new row per mapped teammate:

- `📧 Email Sarah` (Gmail compose, always)
- `💬 Slack Sarah in #warm-intros` (only when both Slack is configured AND
  Sarah's Slack ID is mapped)

The Slack message uses Block Kit and includes a two-perspective "why":

- **Why your teammate → the connector**: their relationship score from
  `Connection.owners[].score`, rendered as `"score 95 — strong"`
- **Why the connector → the prospect**: derived from the `scoreDetails` the
  Draftboard API returns ("Mindy worked with Bogdan at Microsoft for 26
  months, most recently in 2009")

Plus LinkedIn buttons for both the prospect and the connector. No bot, no
OAuth — just the Incoming Webhook URL, which is one-way and safe to revoke
from the Slack app dashboard at any time.

Storage: webhook URL + setup state live in the `slack_config` SQLite table;
per-teammate Slack user IDs and emails live in `team_members`. Both are
single-row-per-key tables so they're easy to inspect or wipe.

## What it's missing (deliberately)

- **No auth.** The app is single-tenant — whoever can hit `localhost:5050` sees everything in the workspace your API key belongs to. If you fork this for a multi-user product, layer your own auth.
- **No automatic teammate email lookup.** The Draftboard API doesn't expose Member emails, so you fill them in once at **Settings → Team**. After that the "Assign to teammate" Gmail draft pre-fills the `To:` field automatically. A future endpoint (`GET /organization/members` or similar) would skip that step.
- **No "Mark as requested" UI.** The SQLite table and toggle endpoint exist but the button is hidden — re-enable it in `templates/_connector_card.html` if you want.
- **No LLM-generated messages.** Drafts are template-based — no API key for OpenAI/Anthropic needed. To plug an LLM in, replace `_build_messages()` in `app.py`.
- **No two-way Slack reactions.** A teammate can't 👍 the Slack message to mark the path "I'll do it" — that requires a Slack bot + public HTTPS endpoint, which would change the kit's "runs on your laptop" shape. If demand emerges, it ships as a separate `examples/flask-hosted/` example.

## License / use

Use it as you like. Fork, modify, ship.
