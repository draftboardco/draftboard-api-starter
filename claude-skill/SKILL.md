---
name: draftboard-api
description: Use this skill when the user mentions the Draftboard API, intros.draftboard.com, warm intros via Draftboard, or wants to build a mini-app or integration on top of Draftboard data. Auto-invoke on phrases like "Draftboard API", "intro path", "warm intro mapping", "Targets / Accounts / Connections view", or when the user says they just got an API key for Draftboard. Provides a prescriptive walkthrough of the recommended first build (paste-import + daily sync + 3-view dashboard) plus the full API reference, capacity caps, and known limitations.
---

# Draftboard API — build your first integration

You're an AI assistant (Claude, Cursor, ChatGPT, etc.) helping a customer build on the Draftboard Integration API. This doc tells you what to build first, why, and how. Read it end to end before you write code.

The user is going to give you their stack and their goals. Your job is to walk them through the build below, step by step. Don't dump a wall of code — ship one step, get it working, then move to the next.

---

## What Draftboard is

Draftboard is a B2B network mapping platform. Users connect their professional network — LinkedIn, Gmail, Calendar, contact exports — and Draftboard maps which of their existing contacts can warm-introduce them to specific prospects. The user adds a prospect (a **Target**), Draftboard finds **Paths** through their **Connections** (or their teammates' Connections in team plans), and ranks each path 0–100 by relationship strength. The product is at https://intros.draftboard.com.

## Vocabulary

Use these exact terms. They're the same words used in the product UI.

| Term | Definition |
|---|---|
| **Customer** | An individual user of Draftboard |
| **Organization** | A team of Customers (used interchangeably with "team") |
| **Target** | A prospect — someone the user wants to reach |
| **Account** | The Target's company |
| **Connection** | A person who can provide a warm intro |
| **Path** | A potential warm intro: Customer → Connection → Target |
| **Supporter** | A Connection the user has explicitly tagged as someone willing to make intros |
| **Score / Rank** | A 0–100+ number measuring relationship strength |

---

## What you're going to build

A simple internal web app on top of the Draftboard API. Think of it as a stripped-down clone of Draftboard itself, but living in the user's stack — their database, their auth, their Slack and email — so it's a foundation they can extend with anything else (CRM sync, daily Slack digests, weekly outreach batches, etc.).

It has three pages:

1. **Import** — a textarea where the user pastes LinkedIn URLs, plus a tag input. Submit pushes those URLs to Draftboard for mapping.
2. **Sync (background)** — a once-a-day job that pulls all targets and their connections from the API and stores them in a local database.
3. **Browse** — a UI to review the imported targets and the paths Draftboard found, with three tabs:
   - **Targets View** — every target, sorted by score
   - **Accounts View** — targets bundled by company
   - **Connections View** — the connectors (people who can intro), sorted by how many targets each can introduce
   - From any of these, the user clicks into a **path detail panel** showing the relationship highlights, a pre-drafted intro request message, and a "Mark as requested" button.

If you want to mirror the look and feel: open https://intros.draftboard.com and study the **Targets View / Accounts View / Connections View** tabs. That's the reference design — you're not cloning it pixel-for-pixel, you're giving the user a usable tool with the same mental model.

## Why this is the right first build (and not, say, a Slack slash command)

Two reasons.

**1. Latency is real.** When a user imports a brand-new target, Draftboard takes **hours, sometimes most of a day**, to fully map paths to that target. So anything that promises "instant" results — a Slack slash command that returns warm paths the moment you paste a URL, an on-demand "who can intro me to this person right now" tool — hits a wall. The data isn't there yet. The right pattern, given how the product is designed, is **import → wait overnight → review in the morning**. This build leans into that pattern instead of fighting it.

**2. Universal foundation.** Once this app works, every other integration is a small extension on top. Nightly Slack digest of new high-quality paths? It's a job that reads the local DB and posts to a channel. CRM enrichment? Read the same DB, push to HubSpot. Weekly outreach batch? Same DB, plus an email-send step. Don't start with one of those — start with the foundation that makes them all easy.

---

## The data model

Persist four tables in the user's local database (SQLite is fine for v1; switch to Postgres at scale):

- **`targets`** — one row per import. Mirrors the API's Target shape (id, firstName, lastName, linkedinUrl, status, position, score, connectionsNumber, tags, createdAt, updatedAt).
- **`connections`** — one row per unique Connection (a person who can intro). Mirrors the Connection shape minus per-target fields. De-dupe by `connection.id`.
- **`paths`** — the join table. One row per (target_id, connection_id) pair, with `score` and `scoreDetails`. **This is the table you query for all three views.**
- **`intro_requests`** — local-only tracking of which paths the user has marked "requested" or "intro made." The API has no way to write this back to Draftboard, so this state lives only in the user's DB.

Plus one tiny **`sync_state`** record holding `last_synced_at` so each daily sync is incremental.

---

## How to build it (step by step)

Build one step at a time. Get each step running end to end before starting the next. After each step, ask the user to run it and confirm before moving on.

**Before Step 1: ask the user what stack they're using.** Python (Flask/FastAPI/Django)? TypeScript (Next.js/Remix/Express)? Something else? Pick the most idiomatic minimal-viable framework for their answer. Don't assume.

### Step 1 — Confirm the API key works

The user gets a key in the Draftboard web app at **Settings → API Keys → "Generate API key"**. The key looks like `db-api_<UUID>`. Have them set it as an env var (`DRAFTBOARD_API_KEY`).

Test with a single curl or one-liner:

```
curl -H "Authorization: Bearer $DRAFTBOARD_API_KEY" \
  https://intros.draftboard.com/api/v1/integration/me
```

If you get a 401, send them back to provision a key. If you get a 200 with a `customer.id` in the response, you're good.

### Step 2 — Import page

A single page with three things:

- A `<textarea>` for LinkedIn URLs. Accept comma- or newline-separated values. Be lenient about input variations: `linkedin.com/in/X`, `www.linkedin.com/in/X`, `https://www.linkedin.com/in/X` — normalize on submit.
- A tag input. Show suggestions as clickable chips, populated from `GET /tags` so the user sees their existing tags. Include an "I don't want to add any tags" checkbox.
- A submit button.

After submit, call `POST /targets/import` with `{ linkedinUrls, tags }` and show the result inline:

> "Imported X targets. Y already existed in your workspace. Z failed (with reasons). They'll be fully mapped within ~24 hours — check back tomorrow."

**Two warnings to surface in the UI:**

- **Capacity caps.** Core plans cap at **~300 active targets/month**, Growth plans at **~1,000/month**. Enterprise is custom. The API does not currently expose remaining capacity. If `linkedinUrls.length` is large (say > 100), show a warning: *"Core plans cap at ~300 active targets/month, Growth at ~1,000. Check Settings → Billing in Draftboard if you're unsure how much room you have."* Also: when `notImported > 0` in the response, show the `errors[]` strings — that's where cap-hit errors will surface.
- **Tags are write-once.** A small line under the tag input: *"Tags can't be changed after import. Pick carefully — re-importing the same URL with new tags doesn't update existing tags."*

Reference for the UI layout: in the Draftboard web app, the import flow has a textarea labeled "Paste their LinkedIn URLs" plus a tag input with suggestion chips like "Onboarding", "Q3 Prospects", "Mid-Market", "SMB", "High Priority", "Conference Attendees", "Fortune 100". Mimic that pattern (with the user's *own* tags as the suggestions, fetched from `GET /tags`).

**Stop here. Have the user import a small test batch (3–5 URLs) and confirm they appear in the Draftboard web app.** Don't build Step 3 until Step 2 works.

### Step 3 — Daily sync job

A background job that runs once a day. The user picks the scheduler that fits their stack: crontab, GitHub Actions schedule, Vercel cron, Cloud Scheduler, Render cron — whatever they're already using.

Pseudocode:

```
since = sync_state.last_synced_at  # null on first run
targets = paginate(GET /targets, updatedSince=since)
upsert into targets table

for each target in targets:
    connections = paginate(GET /targets/{target.id}/connections)
    upsert into connections + paths tables

sync_state.last_synced_at = now
```

Notes:
- **Pagination.** Default `resultPerPage` is 20. Loop with `pageNumber++` until `nextPage == 0`.
- **Pace.** Stay well under 1 request/second across the whole job. The API has no documented rate limits — be conservative. If you start seeing non-200s, back off.
- **First run.** No `updatedSince` — fetch every target. Subsequent runs use the timestamp, so they're much faster.
- **Connections per target.** This is the most expensive part. You're calling `/targets/{id}/connections` once per changed target, paginating each call. Plan for this to take minutes, not seconds, on a real workspace.

### Step 4 — Targets view

The default page after the sync runs. List of every target in the local DB, sorted by `score` descending.

Each row:
- Initials avatar (the API doesn't return profile photos, so use initials on a colored background)
- Name + title at company
- Score badge (e.g., "100% / 145")
- Connection count ("12 paths")
- Click row → opens path detail panel (Step 7)

If the local DB is empty (sync hasn't run yet), show a "no targets yet — go import some" message linking back to Step 2's page.

### Step 5 — Accounts view

A tab at the top of the browse page. Group targets by `position.companyName`. Each row:
- Company name + (optional) website
- "Targets: N" badge
- Highest score across all paths to anyone at the company
- Click row → expand to show targets at this company, then click a target → path detail panel

In the live Draftboard UI this is the **Accounts View** tab — same mental model. Companies sorted by their best score. When the user clicks an account, they see the targets within it; when they click a target, they see the paths.

### Step 6 — Connections view

A tab at the top. **The API has no native connector-first endpoint** — you build this client-side from your local `paths` table:

```
SELECT connection_id, COUNT(target_id), MAX(score)
FROM paths
GROUP BY connection_id
ORDER BY COUNT(target_id) DESC, MAX(score) DESC
```

Each row:
- Initials avatar, name, title at company
- "X intros, top score Y"
- Click row → drawer/modal listing the targets this connection can intro to, grouped by account

This is the most useful view for power users — it answers "who's my single most valuable connector this week, and which prospects can they reach?" Mirrors the **Connections View** tab in the live Draftboard app.

### Step 7 — Path detail panel (the most important screen)

Every "click a target" or "click a connector → click a target" leads here. A side panel or modal showing **one specific path** (one Customer ↔ Connection ↔ Target relationship).

Show:
- The connection ↔ target relationship at the top
- Score (e.g., "100% / 145 points")
- Relationship highlights from `scoreDetails[]` — these are free-text strings from the API like `"They overlapped for 24 months @ Walnut, most recently in 2023"` or `"They have 567 mutual connections"`. Render them as a bullet list under "Why they know each other."
- **Pre-drafted intro request message.** Generate one inline using a template plus the relationship highlights. Example template:
  > "Hey {connection_first_name} — noticed you're connected to {target_first_name} {target_last_name} @ {target_company}. Any chance you'd be open to a potential intro? {one-line context from scoreDetails}. Happy to send a forwardable email."

  Use the user's LLM of choice (Anthropic, OpenAI — the API has no message-generation endpoint). Offer a regenerate button and a tone selector ("Casual" / "Formal" / "Short").
- Buttons: **Copy for LinkedIn DM**, **Copy for email**, **Mark as requested**

"Mark as requested" writes to the local `intro_requests` table. Pure local state — nothing goes back to Draftboard. Use it to gray out paths the user has already asked about so they don't double-ask.

### Step 8 (optional, Growth/Enterprise only) — Ping a teammate via Slack

For users on Growth or Enterprise where `Connection.owners[]` has more than just the user themselves: each path shows which teammate "owns" the strongest connection to that target. Add a button per path: **"Ask {teammate name} to intro"**.

Clicking it posts to a Slack channel mentioning that teammate, with the target details and the relationship highlights.

One-time setup the user has to do:
- A Slack incoming webhook URL (from a Slack app they create)
- A JSON map of Draftboard `Member.id` → Slack user ID (e.g., `@U123ABC`). Document this clearly in the app's settings page so the user knows where to find each teammate's Slack ID.

If the user is on a Core plan, skip this step entirely — `owners[]` only contains the user themselves, so there's nobody else to ping.

---

## API reference

### Auth

Header: `Authorization: Bearer db-api_<UUID>` on every request.
Base URL: `https://intros.draftboard.com/api/v1/integration`

### Plan tiers

All plans (**Core / Growth / Enterprise**) get full read+write API access. The Growth/Enterprise differentiator: `Connection.owners[]` lists every Member in the Organization who can intro to a target, and the `ownerIds[]` filter on `/targets/{id}/connections` lets you narrow to a specific teammate. On Core, `owners[]` is just the user themselves.

### Capacity caps (NOT exposed via API)

- **Core**: ~300 active targets/month
- **Growth**: ~1,000 active targets/month
- **Enterprise**: custom

The API doesn't return remaining capacity. When a customer hits the cap, `POST /targets/import` returns `notImported > 0` with a message in `errors[]`. Surface this to the user.

### Response envelope

Every endpoint returns this shape:

```json
{
  "status": 200,
  "errors": [],
  "<data field>": ...,
  "count": 1234,
  "nextPage": 2
}
```

`nextPage` is `0` when there are no more pages.

### `GET /me`
Verify the API key.

**Response:**
```json
{
  "status": 200,
  "errors": [],
  "customer": {
    "id": "uuid",
    "name": "Customer or Company name",
    "user": {
      "id": "uuid",
      "firstName": "Zach",
      "lastName": "Roseman",
      "linkedinUrl": "https://linkedin.com/in/zachroseman"
    }
  }
}
```

**Note:** this endpoint does NOT return the user's plan tier. There's no API surface today that tells you whether the user is on Core, Growth, or Enterprise. If your UI needs tier-specific copy (e.g., capacity warnings), either show the same generic copy for all tiers ("Core ~300/mo, Growth ~1,000/mo") or ask the user once and persist it locally.

### `GET /targets`
List the customer's Targets.

Query params: `pageNumber` (default 1), `resultPerPage` (default 20), `updatedSince` (ISO 8601), `tagIds[]` or `tagNames[]`, `statuses[]` (one of `new`, `completed`, `stopped`).

Response: `{ status, errors, targets: Target[], count, nextPage }`.

`Target`:
```json
{
  "id": "uuid",
  "firstName": "Jane",
  "lastName": "Doe",
  "linkedinUrl": "https://linkedin.com/in/...",
  "status": "new",
  "position": {
    "title": "VP Sales",
    "companyName": "Acme",
    "companyLinkedinUrl": "https://linkedin.com/company/acme"
  },
  "headline": "Help companies win",
  "score": 87,
  "connectionsNumber": 12,
  "tags": ["Q2-prospects"],
  "createdAt": "2026-01-15T10:00:00Z",
  "updatedAt": "2026-04-30T14:22:00Z"
}
```

`score` here is the maximum Connection→Target score across all paths to this target.

### `POST /targets/import`
Create one or more Targets from LinkedIn URLs.

**Request:**
```json
{
  "linkedinUrls": ["https://linkedin.com/in/jane-doe", "https://linkedin.com/in/john-smith"],
  "tags": ["Q2-prospects", "ICP-A"]
}
```

**Response:**
```json
{
  "status": 200,
  "errors": [],
  "imported": 4,
  "notImported": 1
}
```

`imported` and `notImported` are integer counts (not arrays of objects). The `errors[]` array contains plain text strings — when the user hits the monthly capacity cap, expect the message there. The exact error string format isn't stable yet, so render `errors[]` items as plain strings to the user and let them interpret.

**"I don't want to add any tags" semantics:** when this checkbox is checked, omit the `tags` field from the request body entirely (don't send `tags: []`). Sending an empty array is fine technically, but omission is cleaner and matches the user's intent.

**Tags are write-once.** Re-importing an existing target with new tags returns `imported: 1` but does NOT update tags. There's no PATCH/PUT/DELETE for targets.

The response does NOT include the new target IDs. To find them after import, query `GET /targets?tagNames=<the tag you used>&updatedSince=<5 min ago>`.

### `GET /targets/{id}/connections`
Get all warm paths to a Target.

Query params: `pageNumber`, `resultPerPage`, `updatedSince`, `ownerIds[]` (Growth/Enterprise — filter by teammate).

Response: `{ status, errors, connections: Connection[], count, nextPage }`.

`Connection`:
```json
{
  "id": "uuid",
  "firstName": "Sarah",
  "lastName": "Chen",
  "linkedinUrl": "https://linkedin.com/in/sarahchen",
  "position": {
    "title": "Founder",
    "companyName": "Bright Harbor",
    "companyLinkedinUrl": "https://linkedin.com/company/bright-harbor"
  },
  "headline": "Founder & CEO at Bright Harbor",
  "score": 145,
  "scoreDetails": [
    "They overlapped for 26 months @ MRY in Company leadership, most recently in 2016",
    "They have a good number (15) of mutual connections"
  ],
  "owners": [
    {
      "id": "uuid",
      "firstName": "Zach",
      "lastName": "Roseman",
      "linkedinUrl": "https://linkedin.com/in/zachroseman",
      "score": 90
    }
  ]
}
```

Notes:
- `score` can exceed 100 (combines connection-target rank + signal bonuses). Don't hardcode upper bounds.
- `scoreDetails` is free-form text. Common patterns: *"They overlapped for X months @ Company"*, *"They have N mutual connections"*, *"They went to School together"*, *"They both worked @ Company (but didn't overlap)"*. You can substring-match these for richer logic.
- `owners[]` in Core is just the user. In Growth/Enterprise it's all teammates who share that connection.

### `GET /tags`
List the customer's tags.

Query params: `pageNumber`, `resultPerPage`, `query` (substring), `type` (`manual`, `automatic`, `icp`).
Response: `{ status, errors, tags: [{ id, title, type }], count, nextPage }`.

---

## What the API can't do

Treat these as hard constraints. They will fail or silently no-op:

- **No webhooks.** Pull-only. Detect changes by polling `/targets?updatedSince=<ts>` and diffing against your local DB.
- **No PATCH/PUT/DELETE on Targets.** No way to update tags, status, or any field after creation. No archive, no delete. Tags are write-once.
- **No native connector-first endpoint.** Only target-first queries exist. You build the connector view client-side (Step 6).
- **No accounts/company-search endpoint.** No `/accounts?query=Stripe`. Build it client-side by aggregating `position.companyName` from your imported targets (Step 5).
- **No supporter-list endpoint.** Supporters tagged in the Draftboard UI are not API-readable. If the user wants to filter by Supporter, they maintain the list locally.
- **No intro-status mutation.** Can't write "requested" or "intro made" back to Draftboard via API. Track in your `intro_requests` table.
- **No AI message generation endpoint.** Generate messages locally with whatever LLM the user prefers.
- **No degree-of-separation, profile images, or company logos** in responses. Use initials avatars.
- **No documented rate limits** — be conservative when polling.

---

## What to build next (after this is working)

Once the foundation app from Steps 1–7 (or 1–8 with Slack ping) is shipping, the user can extend it cheaply. Common follow-ons, in roughly increasing complexity:

- **Daily Slack/email digest of new high-quality paths.** A small job that reads new rows from the `paths` table (added since yesterday's sync) where score is above some threshold and posts to Slack or emails the user.
- **CRM enrichment** (HubSpot, Attio, Salesforce). For each Contact in the user's CRM with a LinkedIn URL, look up the matching target+best path and write it to a custom field — sales reps see "warm intro via Sarah" inside their CRM with no context switch.
- **Weekly intro-request batch.** Rank paths by score+recency, pick top N, draft an email per Supporter bundling multiple intros, let the user approve/edit, send via Gmail compose URL.
- **Bulk supporter discovery via Gmail MCP.** Connect the user's Gmail MCP, scan correspondents from the last 12 months, push the unmapped ones into Draftboard as targets via `POST /targets/import` so they get path-mapped.

All of these read from the same local DB you've already built.

---

## Out of bounds

Don't propose or build:

- **Sales Navigator integrations.** Not supported by this API surface.
- **Anything requiring Chrome DevTools Protocol or browser automation.** Customers shouldn't need to drive a real Chrome to use this API.

---

## Final instructions for you, the AI

Three things you must do:

1. **Ask the user their stack first.** Don't assume Python or anything else. Pick a minimal idiomatic framework for their answer.
2. **Build incrementally.** Ship Step 1 → confirm with the user → Step 2 → confirm → Step 3. Never dump 1,500 lines covering all 8 steps at once.
3. **Address gotchas in the right step.** When you reach Step 2's import code, mention the cap warning and write-once tag warning *there*. When you reach Step 3, mention the polling pace + first-run vs incremental. Don't bury them in a separate gotchas list.

If the user says "I want to build something different" — listen. The data model in this doc (local DB of targets + connections + paths + intro_requests, daily sync) is the foundation for almost any UI or output channel. Bend this skeleton to their request before starting from scratch.

If the user contradicts something in this doc about their setup, the user wins. The API surface is the source of truth for what's possible; the user is the source of truth for what they need.
