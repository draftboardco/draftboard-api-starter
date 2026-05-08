# Changelog

All notable changes to this kit. Format: each section heads a date; bullets are the user-visible changes.

For the full git history, see [the commit log](https://github.com/draftboardco/draftboard-api-starter/commits/main).

## Unreleased

- (in progress)

## 2026-05-08

- **Added `/setup` onboarding wizard.** First-run page that asks "are you already a Draftboard customer?", walks new users to signup, and walks existing users through pasting an API key + configuring optional integrations. Auto-redirects from `/` when no API key is configured.
- **Added in-browser Google OAuth credential setup** at `/settings/google`. The kit no longer ships OAuth credentials in the repo (GitHub's secret scanner blocks that path); customers click an "Email Zach" button with their Draftboard identity prefilled, the kit author replies with two values, customer pastes them into a form. Credentials live locally in `oauth_client.json` (gitignored).
- **Added LinkedIn resolver rate limits** — `RESOLVE_BATCH_WORKERS=2` (was 5) and `RESOLVE_BATCH_DELAY_SEC=0.5` between calls per worker. Prevents bursting Google CSE's 100/day free tier or tripping Apollo's per-minute throttle. Both env-tunable.
- **Added Supporter cross-reference badge** on connector cards (purple `📇 On Sarah's list`). When path data flows in, matches each connector against teammate-uploaded supporter lists (resolved LinkedIn URLs); flags overlap inline.
- **Added "Copy LinkedIn URLs" button** on the Supporters page. One click copies all filter-matching resolved URLs to clipboard for paste into Draftboard's production "Add Supporters" form.
- **Added top-level `Quick start`** in the README. Customers cloning the repo see the 4-line happy path right at the top.
- **Broadened the README persona** — kit serves both developers building on the API AND sales teams who want a network-mapping tool that runs locally.

## 2026-05-07

- **Added Slack assignment** — connector-card "Assign to teammate" dropdown gets a `💬 Slack` row when configured. 7-step in-app wizard at `/settings/slack` for setup. Block Kit messages with two-perspective "why" (teammate→connector and connector→target).
- **Added Google Workspace integration** — `/settings/google` Connect button, ~5-min one-time Gmail+Calendar sync, populates a Supporters page (`/supporters/candidates`) ranking contacts by engagement.
- **Added portable scanner** — `scanner/supporter_scan.py` lets teammates run a standalone OAuth flow on their own laptop, export a JSON of their high-engagement contacts, and the kit author imports via `/supporters/import-teammate`. Each contact is badged with whose network it came from.
- **Added LinkedIn resolver** — Apollo + Google Custom Search + gpt-4o-mini ranking. Resolves names + emails to LinkedIn URLs. BYO-keys wizard at `/settings/linkedin-resolver`. Shared cache in `linkedin_resolutions` SQLite table (30-day TTL).
- **Added `bootstrap_cache.py`** — paced first-run helper that populates the local cache with the top N targets without bursting the Draftboard API. Default: 50 targets, 1.5s between calls (~80s total).
- **Added `CLAUDE.md` guardrail** — repo-root file that tells AI agents which SQLite tables hold customer data and must not be wiped without explicit user instruction.

## 2026-05-06 and earlier

- Initial Flask reference implementation: Targets / Accounts / Connections / New paths views, drawer-based path detail, Compose-email button, scheduled-sync daemon, single-user trust model, full-offline mode (`AUTO_SYNC_ENABLED=false`).
