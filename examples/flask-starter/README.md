# Flask starter — Draftboard API reference implementation

A working Flask app that implements the foundation build described in [`CONTEXT.md`](../../CONTEXT.md). Pull your existing targets from Draftboard, browse them by target / account / connector, see which paths are owned by you vs. teammates, get notified about high-quality new paths as they're discovered, and click any path to see relationship details with one-click Compose-email.

Use this as the reference for what you're building, or as a starting point you can fork and customize.

## What it does

| Page | What you see |
|---|---|
| `GET /` (Targets view) | Every target in your workspace, sorted by score, paginated 100/page, searchable. Click any row → drawer with paths to that target. Filter by which teammate owns the path. |
| `GET /accounts` | Targets bundled by company, sorted by best path score, paginated 50/page. Click any row → two-column drawer (Targets on left, Mutual Connectors on right). Filter by owner. |
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
| `intro_requests` | Local-only "Mark as requested" state (currently hidden in the UI but the toggle endpoint and table are wired up). |
| `app_state` | Key-value store. Currently records `last_sync_completed_at` for the "Since last sync" filter. |

## What it's missing (deliberately)

- **No auth.** The app is single-tenant — whoever can hit `localhost:5050` sees everything in the workspace your API key belongs to. If you fork this for a multi-user product, layer your own auth.
- **No teammate email lookup.** The API doesn't expose Member emails, so the "Assign to teammate" Gmail draft has an empty `To:` field — you fill it in. A future Draftboard endpoint (`GET /organization/members` or similar) would let us pre-fill it.
- **No "Mark as requested" UI.** The SQLite table and toggle endpoint exist but the button is hidden — re-enable it in `templates/_connector_card.html` if you want.
- **No LLM-generated messages.** Drafts are template-based — no API key for OpenAI/Anthropic needed. To plug an LLM in, replace `_build_messages()` in `app.py`.

## License / use

Use it as you like. Fork, modify, ship.
