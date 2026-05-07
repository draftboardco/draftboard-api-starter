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

## Google Workspace setup (Candidates page)

The **Candidates** page (`/supporters/candidates`) ranks the people you actually engage with — by Gmail thread frequency + Calendar meetings — so likely Supporters surface automatically.

> **Single-user main app, multi-user via portable scanner.** This Flask kit runs on one laptop. By default the Candidates page shows contacts from **your** Gmail + Calendar only. To pool a teammate's network, send them the portable scanner at `scanner/supporter_scan.py` — they OAuth on their laptop, export a JSON, send it back, you import via `/supporters/import-teammate`. Each contact gets badged with whose network it came from. See **"Pooling teammate networks"** below for the full flow.

**Customer flow is one click:** open `/settings/google` → click **Connect Google** → consent → 5-10 minute sync runs → candidates ready. No setup ceremony, no per-customer Google Cloud project. All Gmail + Calendar data stays on your laptop in `data.db` — Draftboard's infrastructure never touches it.

### Configuring the OAuth client (one-time, you do this once)

The Flask app uses a single Draftboard-owned Google OAuth client (Testing mode, capped at 100 test-user emails per Google's rules). To run this kit yourself you need a `client_id` + `client_secret` for that client, set in any of:

- `GOOGLE_CLIENT_ID` + `GOOGLE_CLIENT_SECRET` env vars
- `.env` in this directory (see `env.example`)
- `~/.draftboard-secrets/google.env` — looks for `DRAFTBOARD_STARTER_GOOGLE_CLIENT_ID` / `_SECRET` first, falls back to plain `GOOGLE_CLIENT_ID` / `_SECRET`

If you haven't already created the OAuth client, here's the one-time setup (~10 min):

1. Open https://console.cloud.google.com/projectcreate → name it `Draftboard Supporters`
2. Enable [Gmail API](https://console.cloud.google.com/apis/library/gmail.googleapis.com) + [Calendar API](https://console.cloud.google.com/apis/library/calendar-json.googleapis.com)
3. [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent): External → Testing → app name `Draftboard` → support + dev contact emails → add yourself (and any future customers) under **Test users**
4. [Credentials](https://console.cloud.google.com/apis/credentials) → **+ Create credentials** → **OAuth client ID** → **Web application** → add `http://localhost:5050/auth/google/callback` to **Authorized redirect URIs** → Create → copy the resulting `client_id` + `client_secret`
5. Save them under one of the locations above and restart the app.

Once that's in place, customers see a "Connect Google" button at `/settings/google` and `/supporters/candidates`. They never see any of step 1-4.

### Adding customer test users

While the OAuth client is in Testing mode (no Google verification), only emails on your test-users allowlist can complete the OAuth flow — others get "Access blocked." Add new customers by editing the test-users list at https://console.cloud.google.com/apis/credentials/consent (under your project). Hard cap of 100 total. Past that, you'd need to push the consent screen to "In production" and go through Google's verification: ~2-4 weeks of paperwork for `calendar.readonly` only, or a paid CASA security audit ($15-75k, 2-6 months) for the full `gmail.readonly` scope. Defer that until you have customer demand.

### What happens during sync

The OAuth scopes are `gmail.readonly` + `calendar.readonly` (plus `userinfo.email` for display). After consent the app:

1. Fetches up to `GOOGLE_THREADS_CAP` (default 2000) of your most-recent Gmail threads, scoped to the last `GOOGLE_HISTORY_DAYS` (default 365). Pulls metadata only — From/To/Cc/Date headers, no message bodies.
2. Fetches up to `GOOGLE_EVENTS_CAP` (default 2500) Calendar events from the same window. Pulls attendee lists only.
3. Aggregates into `gmail_contacts` + `calendar_contacts` tables. **The OAuth tokens are held in memory only and discarded the moment the sync finishes** — there's no persistent refresh token, no encryption ceremony, no daily-sync daemon.

To re-sync (e.g., after a few months), the customer clicks **Re-sync (re-consent)** which kicks off a fresh OAuth flow.

### Scoring

For each contact: `(emails_sent + replies × 2 + threads × 3 + meetings × 5) × recency_decay`, where `recency_decay = max(0.1, 1 - days_since_last_contact / 365)`. Email-only contacts must show **bidirectional engagement** (≥ 1 email you sent AND ≥ 1 reply from them) — cold-outreach prospects who never replied and inbound newsletters you ignored are filtered out. Calendar-only contacts (shared meeting, but no email signal) are kept since attending a meeting together is bidirectional by definition.

## Pooling teammate networks (portable scanner)

A Draftboard team is multiple people. Each person's Gmail + Calendar history is a different slice of the team's collective network. To pool them, the kit ships a **standalone Python scanner** in `scanner/` that a teammate runs on their own laptop — OAuth + 5-10 minutes — and exports a JSON file you import into your kit.

**Setup (you do this once):**

1. Create a second OAuth client in the same Google Cloud project — type **Desktop app** (not Web). The Desktop client uses PKCE so its `client_secret` isn't truly confidential and can be embedded in the script we ship to teammates.
2. Save the credentials in `~/.draftboard-secrets/google.env`:
   ```bash
   export DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID="..."
   export DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET="GOCSPX-..."
   ```
3. Add each teammate's Google email to the project's **test users** allowlist in the [OAuth consent screen](https://console.cloud.google.com/apis/credentials/consent). Hard cap of 100 emails for testing-mode apps.

**Per-teammate flow:**

1. **Build the distributable**: `python3 scanner/build_dist.py` (reads the secrets file, writes `scanner/dist/supporter_scan.py` with the credentials baked in). The `dist/` directory is gitignored — never commit it.
2. **DM the file to your teammate** — Slack attachment, email, private gist. Don't post publicly: anyone with the file can use your OAuth client to consent on their own Google.
3. **They run** `python3 supporter_scan.py` on their laptop. The script auto-installs missing packages, opens a browser for Google sign-in, scans 12 months of metadata, then writes a `supporter_scan_<email>_<date>.html` file next to itself.
4. **They open that HTML in their browser**, see every contact the scan found in a sortable table with checkboxes, untick anyone they don't want shared, click **Save Filtered JSON** — a `_filtered.json` downloads.
5. **They send the filtered JSON back** to you (Slack DM, email). Nothing leaves their laptop until they explicitly send the file.
6. **You import** at `/supporters/import-teammate` — drag-drop the file, click Import. Their contacts merge into your Candidates page, badged with their name in the **From** column.

Re-imports from the same teammate replace their prior data (idempotent). The "Imported scans" section on the import page lists all your contributors with a Remove button per teammate. The contributor filter on the Candidates page lets you slice the list to just one person's network.

The scanner reads metadata only — From/To/Cc/Date headers, attendee lists. Never message bodies. The HTML and the resulting JSON both contain contact emails, names, and aggregate counts; nothing else. Privacy stays equivalent to what the kit already does: Draftboard's infrastructure never touches any of it. The HTML stays on the teammate's laptop unless they choose to send it; the filtered JSON only travels between them and you via whatever channel you DM through.

Full teammate-facing instructions are in [`scanner/README.md`](scanner/README.md).

## What it's missing (deliberately)

- **No auth.** The app is single-tenant — whoever can hit `localhost:5050` sees everything in the workspace your API key belongs to. If you fork this for a multi-user product, layer your own auth.
- **No teammate email lookup.** The API doesn't expose Member emails, so the "Assign to teammate" Gmail draft has an empty `To:` field — you fill it in. A future Draftboard endpoint (`GET /organization/members` or similar) would let us pre-fill it.
- **No "Mark as requested" UI.** The SQLite table and toggle endpoint exist but the button is hidden — re-enable it in `templates/_connector_card.html` if you want.
- **No LLM-generated messages.** Drafts are template-based — no API key for OpenAI/Anthropic needed. To plug an LLM in, replace `_build_messages()` in `app.py`.

## License / use

Use it as you like. Fork, modify, ship.
