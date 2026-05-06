# Draftboard API Starter

Context kit for AI assistants to help you build mini-apps on top of the Draftboard Integration API.

## Who this is for

You have a Draftboard API key (Settings → API Keys → "Generate API key" in the Draftboard web app) and you want your AI assistant to help you build something useful with it — a daily Slack alert, a CRM enrichment script, a custom dashboard, anything.

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
