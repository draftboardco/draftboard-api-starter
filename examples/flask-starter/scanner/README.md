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
                                             Google OAuth (browser)
                                                │
                                                ▼
                                             supporter_scan_*.html
                                                │ (open in browser, review)
                                                │ click "Save Filtered JSON"
                                                ▼
                                             supporter_scan_*_filtered.json
                                                │
                                       ◄──email/Slack─┘
       │
       ▼
    /supporters/import-teammate (upload)
       │
       ▼
    Their contacts pooled into the
    Supporters page, tagged "from <teammate>"
```

The HTML review step is intentional — your teammate sees every contact the
scanner found and decides individually who to share with you. The script
**only outputs HTML**; the actual JSON gets generated client-side when
they click Save.

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

> Hey, here's a Python script that scans your Gmail/Calendar (last 12 months)
> and ranks who you're closest to, so I can pool your network into our shared
> Supporters list. It reads subject lines locally to label each contact
> (friend, investor, customer, etc.) but never opens full emails — and only
> counts + a label are ever saved to the file, no message text. Save it
> somewhere on your laptop and run:
>
> ```
> python3 supporter_scan.py
> ```
>
> A browser tab will open for Google sign-in. Grant access. After ~5-10 minutes
> the script will write an HTML review file next to itself. **Open that HTML
> in your browser, untick anyone you don't want shared with me, then click
> "Save Filtered JSON".** A `..._filtered.json` file will download — send
> THAT file back to me. Nothing leaves your laptop until you click Save and
> attach the file yourself.

## What teammates do

1. **Save the file** somewhere on their laptop (e.g., `~/Downloads/supporter_scan.py`).
2. **Open Terminal** (macOS) or PowerShell (Windows) or any shell.
3. **Run** `python3 supporter_scan.py` (or `python supporter_scan.py` on Windows).
4. **First-time only**: the script asks permission to install two Python packages (`google-auth-oauthlib`, `google-api-python-client`). Say yes.
5. **A browser tab opens** for Google sign-in. They'll see "Google hasn't verified this app" — that's expected. Click **Advanced → Go to Draftboard (unsafe)**. (Their email needs to be on the kit author's test-users allowlist — see "Heads up" below.)
6. **Grant the read-only permissions.**
7. The script runs 5-10 minutes (depending on how many threads + events they have), prints a top preview to the terminal, and writes a file called `supporter_scan_<email>_<date>.html` next to itself.
8. **Open that HTML file in any browser** (double-click in Finder/Explorer, or drag onto a browser tab). It shows every eligible contact — ranked by how likely they are to make a warm intro, labelled by type (friend / investor / customer / vendor / …), with a ★ next to anyone who's introduced them to someone before. Sortable and searchable.
9. **Untick anyone they don't want shared.** They can search by name/email, untick all visible at once, etc. The default is everyone ticked.
10. **Click the blue "Save Filtered JSON →" button** at the top right. A `supporter_scan_<email>_<date>_filtered.json` file downloads.
11. **Send that JSON file** (the one with `_filtered` in the name) back to whoever asked them to run the script.

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
    {"email": "alice@foo.com", "name": "Alice F", "emails_sent": 12, "replies_received": 8,
     "threads_count": 15, "one_to_one_threads": 11, "deep_threads": 4, "active_months": 6,
     "last_contact_at": 1715000000, "intro_maker": true, "relationship_type": "friend"}
  ],
  "calendar_contacts": [
    {"email": "bob@bar.com", "name": "Bob B", "meetings_count": 6, "small_meetings": 5, "last_met_at": 1714900000}
  ],
  "ranked_contacts": [
    {"email": "alice@foo.com", "name": "Alice F", "relationship_type": "friend",
     "type_label": "Friend / personal", "reply_rate": 0.67, "intro_maker": true, "score": 88}
  ]
}
```

`schema_version` stays **1** for backward compatibility — the kit importer reads
the core `gmail_contacts` / `calendar_contacts` counts and ignores the newer
fields. `ranked_contacts` is the merged, classified, scored list (highest first)
that powers the HTML review page.

The file carries **no message content and no subject lines** — only per-contact
aggregate counts, timestamps, display names, a relationship label, and the
computed supporter score. Subjects and Gmail's short snippet previews are read
*locally, on the teammate's laptop*, purely to classify each relationship and
spot intro history; that text is never written to the file or sent anywhere.

### How the score works (supporter-likelihood, 0-100)

Rewards relationship **quality**, not email volume:

- **Reciprocity** — do they actually reply, and is it balanced (vs you blasting)
- **1:1 vs group** — just-the-two-of-you threads count far more than big CCs
- **Conversation depth** — real multi-message back-and-forths
- **Relationship span** — sustained across many months, not one burst
- **Small meetings** — 1:1s and small calls (a 30-person all-hands is ignored)
- **Intro history (★)** — a big boost for anyone who's introduced them to others

A relationship-type multiplier then discounts sales prospects, vendors,
support-only threads, inbound-VC pitches, and churned customers — even when
they generate a lot of email. Recency is a gentle tilt, not the main driver.

## Running it locally vs in the cloud

The scanner uses Google's "InstalledAppFlow," which spins up a one-shot localhost web server to capture the OAuth callback. **This works on a real laptop but not in cloud notebooks** (Replit, Colab, GitHub Codespaces) because their localhost is sandboxed and the browser can't reach it. If your teammate doesn't have Python installed, the simplest thing is for them to install it (https://www.python.org/downloads/) — takes 2 minutes, less friction than wrestling with a cloud notebook's networking.

## Importing on your end

Once the JSON lands in your inbox, head to your kit's `/supporters/import-teammate` page and upload it. The import:

- Validates the schema
- Stores rows in `teammate_contacts(contributor_email, email, …)` keyed by both
- Re-imports from the same teammate UPDATE in place (idempotent)
- Pools into the Supporters page alongside your own contacts, with a "From <teammate>" badge
