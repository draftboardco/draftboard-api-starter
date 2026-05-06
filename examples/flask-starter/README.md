# Flask starter — Draftboard API reference implementation

A working Flask app that implements the foundation build described in [`CONTEXT.md`](../../CONTEXT.md) — Steps 1 through 7. Pull your existing targets from Draftboard, browse them by target / account / connector, and click any path to see the relationship details with a one-click Compose-email flow.

Use this as the reference for what you're building, or as a starting point you can fork and customize.

## What it does

| Page | What you see |
|---|---|
| `GET /` (Targets view) | Every target in your workspace, sorted by score, paginated 100/page, searchable. Click any row → drawer with the paths to that target. |
| `GET /accounts` | Targets bundled by company, sorted by best path score, paginated 50/page. Click any row → two-column drawer (Targets on left, Mutual Connectors on right). |
| `GET /import` | Paste LinkedIn URLs to import as new Targets, with tag suggestions pulled from your existing tags. |

Plus:

- **Background sync** — once on first page load, the app fans out 5 parallel workers to fetch every target's connections via `GET /targets/{id}/connections`, persists to a local SQLite file. After that, drawers open in milliseconds. Progress is shown as a pill in the nav.
- **Compose email** — generates a friendly intro request draft (using `scoreDetails` rewritten from third-person to second-person) and opens Gmail compose with subject + body pre-filled. If your browser supports it, an HTML version with the target's name hyperlinked to LinkedIn is also copied to the clipboard so you can paste over the body.

## Run

```bash
# 1. Get an API key from Draftboard → Settings → API Keys → "Generate API key"
export DRAFTBOARD_API_KEY=db-api_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx

# 2. Install deps in a venv
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Start the server
.venv/bin/python app.py
# (override the port with PORT=5051 if 5050 is busy)

# 4. Open http://localhost:5050
```

The first page load takes ~1 minute on a workspace with thousands of targets (the app fetches every target's metadata in one pass). Background sync of per-target connections runs after that — you can browse while it works. With 4,400 targets it takes ~5 minutes; with the 10–100 targets a typical new user has, it's done in seconds.

## Files

- `app.py` — Flask app, all routes, SQLite-backed connection cache, parallel sync worker
- `templates/` — Jinja templates. `_nav.html` is the top bar with the sync-progress pill. `_drawer_skeleton.html` is the slide-in drawer container plus its JS (drawer routing, clipboard copy, toast). `_connector_card.html` is the path detail card shared between the target and account drawers.
- `requirements.txt` — Flask + requests
- `data.db` — SQLite cache, created on first run. Gitignored. Delete it to force a full re-sync.

## What it's missing (deliberately)

- **No auth.** The app is single-tenant — whoever can hit `localhost:5050` sees everything in the workspace your API key belongs to. If you fork this for a multi-user product, layer your own auth on top.
- **No incremental real-time updates.** The 5-minute targets cache TTL and 24-hour connections TTL are intentional — Draftboard's mapping process runs on the order of hours, so polling more often is wasteful. Hit Refresh in the UI when you want fresh data.
- **No "Mark as requested" UI.** The SQLite table and toggle endpoint exist, but the button is hidden. Re-enable it in `templates/_connector_card.html` if you want to use it.
- **No LLM-generated messages.** Drafts are template-based — no API key for OpenAI/Anthropic needed. To plug an LLM in, replace `_build_messages()` in `app.py`.

## License / use

Use it as you like. This is starter code — fork, modify, ship.
