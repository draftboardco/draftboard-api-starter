# Draftboard API Starter

Context kit for AI assistants + a working reference Flask app on top of the Draftboard Integration API.

## Quick start

If you have a Draftboard API key already and just want to run the reference Flask app:

```bash
cd examples/flask-starter
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Then open http://localhost:5050. The first page walks you through pasting your API key + setting up the optional integrations (Google Workspace, LinkedIn resolver, Slack notifications). Nothing leaves your machine — the kit runs fully on your laptop and persists data to a local SQLite file.

(Need an API key? Generate one at [intros.draftboard.com](https://intros.draftboard.com) → Settings → API Keys.)

## Who this is for

Two audiences:

- **Developers building on the Draftboard API.** You have a key and you want your AI assistant to help you build something on top of it — a daily Slack alert, a CRM enrichment script, a custom dashboard, anything. The context files in this repo (`CONTEXT.md`, `cursor-rules/draftboard.mdc`, `claude-skill/`) get your AI tool up to speed on the API surface in one paste.
- **Sales teams who want a self-hosted network-mapping tool.** The Flask app at `examples/flask-starter/` is a working reference UI — the same Targets / Accounts / Connections views you get on intros.draftboard.com, running on your own laptop, with optional add-ons the web app doesn't have: Slack-channel notifications when teammates assign paths, a Supporters page that ranks contacts you actually engage with via Gmail + Calendar, and a portable scanner so a teammate can contribute their own network without needing a paid Draftboard seat.

## How to use it

Pick the install method that matches your AI tool.

### Claude Code (CLI)

```bash
cp -r claude-skill ~/.claude/skills/draftboard
```

Restart Claude Code. The skill auto-invokes when you mention "Draftboard API" or related terms.

### Cursor

```bash
mkdir -p .cursor/rules && cp cursor-rules/draftboard.mdc .cursor/rules/
```

The rule loads automatically on project open.

### claude.ai web

Open `CONTEXT.md`, copy the entire contents, and paste into a Project's custom instructions (or paste at the top of a single conversation).

### ChatGPT or any other AI tool

Same as claude.ai web — paste `CONTEXT.md` as a system prompt or custom instructions.

## What's in this repo

- `CONTEXT.md` — the main artifact. Your AI's full briefing on Draftboard plus a step-by-step walkthrough of the recommended first build (a paste-import + daily-sync + 3-view dashboard that mirrors the Draftboard UI in the user's own stack). Includes the API reference, capacity caps, and what the API can't do.
- `claude-skill/SKILL.md` — Claude Code skill version (same content + skill frontmatter).
- `cursor-rules/draftboard.mdc` — Cursor rules version (same content + Cursor frontmatter).
- `examples/flask-starter/` — a working reference implementation in Python + Flask. Implements the full foundation app from `CONTEXT.md`: import page, daily sync, Targets/Accounts views, two-column path detail drawer, Compose-email button. Run it with your own API key in ~5 commands. See [`examples/flask-starter/README.md`](examples/flask-starter/README.md) for setup.

## What this is not

Not a pre-built app or starter scripts. It's the context and walkthrough your AI needs to help you build a real foundation app on the Draftboard API in an afternoon. Your AI reads it, asks what stack you're working in, and walks you through the 7-step build one piece at a time.

## After install

Try a prompt like:

> I just got an API key for Draftboard. Help me build the foundation app from your Draftboard context.

Your AI should ask what stack you're using, then start with Step 1 (confirm the API key works) before moving on.
