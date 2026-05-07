# Supporter Scan — portable scanner for teammates

A standalone Python script that lets a teammate scan **their** Gmail + Calendar history and export it as a JSON file you can import into the main Flask kit. The point: every team member contributes their network, the kit pools all of them into one ranked Supporters view.

The flow is asymmetric: **you** (the kit author) build the script once with your OAuth credentials baked in, then DM the resulting file to teammates. They run it cold, no Python setup beyond what they already have.

## How it fits together

```
    YOUR LAPTOP                          TEAMMATE'S LAPTOP
    ───────────                          ─────────────────
    scanner/supporter_scan.py
       │ python build_dist.py
       ▼
    scanner/dist/supporter_scan.py  ──DM──►  supporter_scan.py
                                                │ python3
                                                ▼
                                             Google OAuth
                                                │ (in browser)
                                                ▼
                                             supporter_scan_*.json
                                                │
                                       ◄──email/Slack─┘
       │
       ▼
    /supporters/import-teammate
       │
       ▼
    Their contacts pooled into the
    candidates page, tagged "from <teammate>"
```

## One-time setup (kit author)

You'll have done this already if you set up the main `/settings/google` flow and added `DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID` / `_SECRET` to `~/.draftboard-secrets/google.env`. If not, see the main `examples/flask-starter/README.md` "Google Workspace setup" — note the scanner needs a **Desktop**-type OAuth client (separate from the Web client the Flask app uses).

## Building a distributable

```bash
cd examples/flask-starter
python3 scanner/build_dist.py
```

This produces `scanner/dist/supporter_scan.py` with your `client_id` + `client_secret` substituted in. The `dist/` directory is gitignored — never commit the populated file.

## Sending it to a teammate

DM `scanner/dist/supporter_scan.py` directly: Slack attachment, email, private GitHub gist. **Don't post it publicly** — anyone with the file can use your OAuth client to consent on their own Google account, which fills your test-users allowlist (capped at 100) and could trigger Google to suspend your client if abused.

Send them this short paste (or a link to this README's "What teammates do" section):

> Hey, here's a Python script that scans your Gmail/Calendar (last 12 months,
> metadata only — no message contents) and outputs a JSON of your top
> contacts. Save it somewhere on your laptop and run:
>
> ```
> python3 supporter_scan.py
> ```
>
> A browser tab will open for Google sign-in. Grant access. After ~5 minutes
> it'll print a summary and drop a file like `supporter_scan_you_2026-05-07.json`
> next to the script. Send that file back to me — I'll import it into our
> Draftboard kit and your network will show up in our shared Supporters list.

## What teammates do

1. **Save the file** somewhere on their laptop (e.g., `~/Downloads/supporter_scan.py`).
2. **Open Terminal** (macOS) or PowerShell (Windows) or any shell.
3. **Run** `python3 supporter_scan.py` (or `python supporter_scan.py` on Windows).
4. **First-time only**: the script asks permission to install two Python packages (`google-auth-oauthlib`, `google-api-python-client`). Say yes.
5. **A browser tab opens** for Google sign-in. They'll see "Google hasn't verified this app" — that's expected. Click **Advanced → Go to Draftboard (unsafe)**. (Their email needs to be on the kit author's test-users allowlist — see "Heads up" below.)
6. **Grant the read-only permissions.**
7. The script runs ~3-5 minutes, prints a top-10 preview, and writes `supporter_scan_<email>_<date>.json` next to itself.
8. **Send that JSON file back** to whoever asked them to run it.

### Heads up

The OAuth client is in **Testing mode** by default — Google requires the kit author to explicitly add each teammate's email to a test-users allowlist (capped at 100 emails). If a teammate sees "**Access blocked**" on the Google sign-in page, they're not on the list yet. Tell them to ping the kit author with their Google email and try again.

## What's in the JSON

```json
{
  "schema_version": 1,
  "scan_type": "draftboard_supporter_scan",
  "scanned_by": {"email": "marcus@team.com", "name": "Marcus Marshall"},
  "scanned_at": "2026-05-07T15:30:00Z",
  "history_days": 365,
  "gmail_contacts": [
    {"email": "alice@foo.com", "name": "Alice F", "emails_sent": 12, "replies_received": 8, "threads_count": 15, "last_contact_at": 1715000000}
  ],
  "calendar_contacts": [
    {"email": "bob@bar.com", "name": "Bob B", "meetings_count": 6, "last_met_at": 1714900000}
  ]
}
```

No message content. No subject lines. Just per-contact aggregate counts + the most-recent contact timestamp + display names from headers.

## Running it locally vs in the cloud

The scanner uses Google's "InstalledAppFlow," which spins up a one-shot localhost web server to capture the OAuth callback. **This works on a real laptop but not in cloud notebooks** (Replit, Colab, GitHub Codespaces) because their localhost is sandboxed and the browser can't reach it. If your teammate doesn't have Python installed, the simplest thing is for them to install it (https://www.python.org/downloads/) — takes 2 minutes, less friction than wrestling with a cloud notebook's networking.

## Importing on your end

Once the JSON lands in your inbox, head to your kit's `/supporters/import-teammate` page and upload it. The import:

- Validates the schema
- Stores rows in `teammate_contacts(contributor_email, email, …)` keyed by both
- Re-imports from the same teammate UPDATE in place (idempotent)
- Pools into `/supporters/candidates` alongside your own contacts, with a "From <teammate>" badge
