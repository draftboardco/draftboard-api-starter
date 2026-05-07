#!/usr/bin/env python3
"""Draftboard Supporter Scan — standalone portable scanner.

Run on YOUR laptop. Authenticates with Google in your browser, scans the
last 12 months of Gmail metadata + Calendar events, scores per-contact
engagement, and writes a JSON file you can send to whoever set up your
team's Draftboard kit. They'll import it; their kit will pool your network
into the team's Supporter candidates view.

Privacy:
- Reads metadata only (sender/recipient + dates) — never message bodies.
- All data stays on this laptop until you explicitly send the JSON.
- The JSON contains contact emails + names + counts. No message text.

Usage:
    python3 supporter_scan.py

If the Google libraries aren't installed yet, this script offers to
install them with pip on first run.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

# -----------------------------------------------------------------------
# OAuth client identifiers.
#
# This source file is the TEMPLATE. The real client_id + client_secret get
# substituted in by `build_dist.py` (which reads from the kit author's
# ~/.draftboard-secrets/google.env), producing a populated copy under
# scanner/dist/ that's safe to ship to teammates. The dist file is
# .gitignored so the template is the only thing that lives in version control.
#
# As a fallback for development / advanced users, the script also reads
# DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID and DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET
# from the environment, so you can run this template directly without building.
# -----------------------------------------------------------------------
EMBEDDED_CLIENT_ID = "__CLIENT_ID__"
EMBEDDED_CLIENT_SECRET = "__CLIENT_SECRET__"


def _resolve_oauth_client():
    """Pick credentials from the build-time substitution OR the environment.

    Returns (client_id, client_secret) and exits with a clear message if
    neither is available."""
    cid = EMBEDDED_CLIENT_ID
    cs = EMBEDDED_CLIENT_SECRET
    if cid.startswith("__") or cs.startswith("__"):
        cid = os.environ.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID", "").strip()
        cs = os.environ.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET", "").strip()
    if not cid or not cs:
        sys.stderr.write(
            "\nThis is the template version of supporter_scan.py — it has no\n"
            "OAuth credentials baked in.\n\n"
            "If you got this file directly from your team's kit author, ask\n"
            "them to send you the BUILT version (built with scanner/build_dist.py)\n"
            "instead. That copy has the credentials embedded.\n\n"
            "If you ARE the kit author and want to test this template, set:\n"
            "  export DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID=...\n"
            "  export DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET=...\n"
            "then re-run.\n"
        )
        sys.exit(1)
    return cid, cs

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

DEFAULT_HISTORY_DAYS = 365
DEFAULT_THREADS_CAP = 2000
DEFAULT_EVENTS_CAP = 2500
SCHEMA_VERSION = 1


def _ensure_deps():
    """Lazy-install google-auth-oauthlib + google-api-python-client if missing.
    Asks the user once before running pip; falls back to a clear error."""
    try:
        import google_auth_oauthlib  # noqa: F401
        import googleapiclient  # noqa: F401
        return
    except ImportError:
        pass
    print("This scanner needs two Python packages that aren't installed yet:")
    print("  - google-auth-oauthlib")
    print("  - google-api-python-client")
    print()
    answer = input("Install them now with pip? [Y/n] ").strip().lower()
    if answer and answer not in ("y", "yes"):
        print("OK — install them yourself with:")
        print("  pip install google-auth-oauthlib google-api-python-client")
        sys.exit(1)
    import subprocess
    subprocess.check_call([
        sys.executable, "-m", "pip", "install", "--quiet",
        "google-auth-oauthlib>=1.2", "google-api-python-client>=2.120",
    ])
    print("✓ Installed. Continuing…")
    print()


_ensure_deps()

from email.utils import getaddresses
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# -----------------------------------------------------------------------
# OAuth — InstalledAppFlow opens a local browser tab, captures the auth
# code via a one-shot localhost web server (port chosen at runtime).
# -----------------------------------------------------------------------

def authenticate():
    client_id, client_secret = _resolve_oauth_client()
    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }
    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)
    print("Opening your browser for Google sign-in…")
    print("(If a browser tab doesn't open, copy the URL printed below.)")
    print()
    creds = flow.run_local_server(
        port=0,
        prompt="consent",
        success_message="✓ Connected. You can close this tab — go back to the terminal.",
    )
    return creds


# -----------------------------------------------------------------------
# Header parsing + noise filter (mirrors the main kit's logic)
# -----------------------------------------------------------------------

def parse_addresses(header_value):
    if not header_value:
        return []
    out = []
    for name, email in getaddresses([header_value]):
        email = (email or "").strip().lower()
        if not email or "@" not in email:
            continue
        out.append(((name or "").strip(), email))
    return out


_NOREPLY_PREFIXES = (
    "noreply", "no-reply", "donotreply", "do-not-reply", "notifications",
    "notification", "alert", "alerts", "support", "help", "info", "hello",
    "team", "billing", "receipts", "invoice", "invoices", "hr", "press",
    "marketing", "newsletter", "news", "updates", "system", "automated",
    "calendar-notification", "auto-confirm",
)


def is_noise_email(email):
    if not email or "@" not in email:
        return True
    local, _, domain = email.partition("@")
    local = local.lower()
    domain = domain.lower()
    if domain in ("googlegroups.com", "calendar.google.com", "resource.calendar.google.com"):
        return True
    if local.startswith(_NOREPLY_PREFIXES):
        return True
    if "+" in local and any(local.startswith(p) for p in ("bounce", "bounces")):
        return True
    return False


# -----------------------------------------------------------------------
# Gmail + Calendar fetchers
# -----------------------------------------------------------------------

def fetch_gmail(creds, my_email, days, cap):
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    me = (my_email or "").lower()
    print(f"  Fetching Gmail thread IDs from the last {days} days…")
    thread_ids = []
    page_token = None
    q = f"newer_than:{days}d"
    while len(thread_ids) < cap:
        page_size = min(500, cap - len(thread_ids))
        resp = service.users().threads().list(
            userId="me", q=q, maxResults=page_size, pageToken=page_token
        ).execute()
        for t in resp.get("threads", []) or []:
            thread_ids.append(t["id"])
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    print(f"  Found {len(thread_ids)} threads. Pulling metadata…")

    contacts = {}
    for i, tid in enumerate(thread_ids):
        try:
            thread = service.users().threads().get(
                userId="me", id=tid, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Date"],
            ).execute()
        except HttpError:
            if (i + 1) % 50 == 0:
                _progress("Gmail", i + 1, len(thread_ids))
            continue

        thread_contacts = {}
        most_recent_ts = 0
        for msg in thread.get("messages", []) or []:
            headers = {h.get("name", "").lower(): h.get("value", "") for h in
                       (msg.get("payload", {}).get("headers", []) or [])}
            from_addrs = parse_addresses(headers.get("from", ""))
            to_addrs = parse_addresses(headers.get("to", ""))
            cc_addrs = parse_addresses(headers.get("cc", ""))
            try:
                msg_ts = int(msg.get("internalDate", "0")) // 1000
            except (TypeError, ValueError):
                msg_ts = 0
            if msg_ts > most_recent_ts:
                most_recent_ts = msg_ts

            sender_email = from_addrs[0][1] if from_addrs else ""
            sent_by_me = sender_email == me

            for nm, em in from_addrs + to_addrs + cc_addrs:
                if em == me or is_noise_email(em):
                    continue
                rec = thread_contacts.setdefault(em, {"name": "", "sent_by_me": 0, "replies": 0})
                if nm and not rec["name"]:
                    rec["name"] = nm
                if sent_by_me:
                    rec["sent_by_me"] += 1
                elif em == sender_email:
                    rec["replies"] += 1

        for em, rec in thread_contacts.items():
            agg = contacts.setdefault(em, {
                "name": "", "emails_sent": 0, "replies_received": 0,
                "threads_count": 0, "last_contact_at": 0,
            })
            if rec["name"] and not agg["name"]:
                agg["name"] = rec["name"]
            agg["emails_sent"] += rec["sent_by_me"]
            agg["replies_received"] += rec["replies"]
            agg["threads_count"] += 1
            if most_recent_ts > agg["last_contact_at"]:
                agg["last_contact_at"] = most_recent_ts

        if (i + 1) % 25 == 0:
            _progress("Gmail", i + 1, len(thread_ids))

    _progress("Gmail", len(thread_ids), len(thread_ids))
    print()
    return contacts


def fetch_calendar(creds, my_email, days, cap):
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    me = (my_email or "").lower()
    time_min = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    time_max = datetime.now(timezone.utc).isoformat()
    print(f"  Fetching Calendar events from the last {days} days…")

    contacts = {}
    page_token = None
    processed = 0
    while True:
        page_size = min(2500, cap - processed)
        if page_size <= 0:
            break
        resp = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=page_size,
            pageToken=page_token,
        ).execute()
        events = resp.get("items", []) or []
        for ev in events:
            attendees = ev.get("attendees", []) or []
            if not attendees:
                processed += 1
                continue
            my_resp = next(
                (a.get("responseStatus") for a in attendees if (a.get("email") or "").lower() == me),
                None,
            )
            if my_resp == "declined":
                processed += 1
                continue
            ts_str = (ev.get("start") or {}).get("dateTime") or (ev.get("start") or {}).get("date") or ""
            ts = 0
            if ts_str:
                try:
                    if "T" in ts_str:
                        ts = int(datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp())
                    else:
                        ts = int(datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc).timestamp())
                except (TypeError, ValueError):
                    ts = 0
            for a in attendees:
                email = (a.get("email") or "").lower()
                if not email or email == me or is_noise_email(email):
                    continue
                if a.get("responseStatus") == "declined":
                    continue
                rec = contacts.setdefault(email, {"name": "", "meetings_count": 0, "last_met_at": 0})
                nm = a.get("displayName") or ""
                if nm and not rec["name"]:
                    rec["name"] = nm
                rec["meetings_count"] += 1
                if ts > rec["last_met_at"]:
                    rec["last_met_at"] = ts
            processed += 1
        page_token = resp.get("nextPageToken")
        if not page_token or processed >= cap:
            break
        if processed % 100 == 0:
            _progress("Calendar", processed, processed)

    _progress("Calendar", processed, processed)
    print()
    return contacts


def _progress(label, done, total):
    if total <= 0:
        sys.stdout.write(f"\r  {label}: {done}…")
    else:
        sys.stdout.write(f"\r  {label}: {done} / {total}    ")
    sys.stdout.flush()


# -----------------------------------------------------------------------
# Scoring — same formula as the Flask kit, applied here only to give
# Marcus a preview of his top candidates. The JSON includes raw counts
# so the kit can re-score authoritatively on import.
# -----------------------------------------------------------------------

def score(emails_sent, replies, threads, meetings, days_ago):
    base = (emails_sent or 0) + 2 * (replies or 0) + 3 * (threads or 0) + 5 * (meetings or 0)
    if days_ago is None:
        return 0
    recency = max(0.1, 1.0 - (days_ago / 365.0))
    return int(base * recency)


def merged_top_n(gmail_contacts, calendar_contacts, n=10):
    now = int(time.time())
    merged = {}
    for em, c in gmail_contacts.items():
        merged[em] = {
            "email": em,
            "name": c.get("name") or em,
            "emails_sent": c.get("emails_sent", 0),
            "replies_received": c.get("replies_received", 0),
            "threads_count": c.get("threads_count", 0),
            "meetings_count": 0,
            "last_contact_at": c.get("last_contact_at", 0),
        }
    for em, c in calendar_contacts.items():
        d = merged.setdefault(em, {
            "email": em, "name": c.get("name") or em,
            "emails_sent": 0, "replies_received": 0, "threads_count": 0,
            "meetings_count": 0, "last_contact_at": 0,
        })
        d["meetings_count"] = c.get("meetings_count", 0)
        d["last_contact_at"] = max(d["last_contact_at"], c.get("last_met_at", 0))
        if c.get("name") and not d["name"]:
            d["name"] = c["name"]

    # Apply the same bidirectional filter the kit uses: gmail-only contacts
    # need both directions; calendar is bidirectional by definition.
    qualifying = []
    for em, d in merged.items():
        gmail_ok = d["emails_sent"] >= 1 and d["replies_received"] >= 1
        meet_ok = d["meetings_count"] >= 1
        if not (gmail_ok or meet_ok):
            continue
        days_ago = ((now - d["last_contact_at"]) / 86400.0) if d["last_contact_at"] else None
        d["score"] = score(d["emails_sent"], d["replies_received"], d["threads_count"], d["meetings_count"], days_ago)
        qualifying.append(d)
    qualifying.sort(key=lambda x: -x["score"])
    return qualifying[:n], len(qualifying)


# -----------------------------------------------------------------------
# HTML review page — what we actually emit. Marcus opens this file in his
# browser, reviews, unticks anyone he doesn't want shared, clicks "Save
# Filtered JSON" → that download is what he sends to the kit author.
#
# Self-contained: Tailwind is the only external load. All contact data
# is embedded in a <script type="application/json"> tag the page's JS
# parses on load.
# -----------------------------------------------------------------------

def _render_review_html(payload):
    """Wrap a scan payload (the dict that would have been written to JSON)
    into a self-contained HTML review page."""
    # Embed the data safely — escape closing-tag sequences so the JSON can't
    # break out of its <script> wrapper.
    data_blob = json.dumps(payload).replace("</", "<\\/")
    scanned_email = payload["scanned_by"]["email"]
    scanned_name = payload["scanned_by"]["name"] or scanned_email
    safe_filename_email = re.sub(r"[^A-Za-z0-9]+", "_", scanned_email)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filtered_filename = f"supporter_scan_{safe_filename_email}_{date_str}_filtered.json"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Review your Supporter Scan</title>
  <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-50 text-slate-900 min-h-screen">
  <div class="max-w-5xl mx-auto px-6 py-8">

    <header class="mb-6">
      <h1 class="text-2xl font-semibold tracking-tight">Review your Supporter Scan</h1>
      <p class="text-sm text-slate-600 mt-2">
        Below are the contacts your scan found in your Gmail + Calendar over
        the last {payload["history_days"]} days. <strong>Untick anyone you
        don't want shared</strong> with the person who asked you to run this.
        Then click <strong>Save Filtered JSON</strong> at the top right and
        send THAT file back to them — not this HTML.
      </p>
      <p class="text-xs text-slate-500 mt-2">
        Signed in as <strong>{scanned_name}</strong> &lt;{scanned_email}&gt;.
        This page is local to your laptop. Nothing is sent anywhere when you
        click Save — it just downloads the filtered file.
      </p>
    </header>

    <div class="sticky top-0 z-10 bg-slate-50 pb-3 mb-3 border-b border-slate-200 flex items-center gap-3 flex-wrap">
      <input id="search" type="search" placeholder="Search name or email…"
             class="flex-1 min-w-[200px] rounded-md border border-slate-300 px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-indigo-500" />
      <button id="check-all" type="button"
              class="text-xs px-3 py-1.5 rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-50">
        Tick all visible
      </button>
      <button id="uncheck-all" type="button"
              class="text-xs px-3 py-1.5 rounded-md border border-slate-300 bg-white text-slate-700 hover:bg-slate-50">
        Untick all visible
      </button>
      <span id="count" class="text-xs text-slate-500"></span>
      <button id="save" type="button"
              class="ml-auto inline-flex items-center text-sm px-4 py-1.5 rounded-md bg-indigo-600 text-white font-medium shadow hover:bg-indigo-700">
        Save Filtered JSON →
      </button>
    </div>

    <div class="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-slate-500 select-none">
          <tr class="text-left">
            <th class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider w-10">
              <input id="head-check" type="checkbox" checked class="h-4 w-4 rounded border-slate-300 text-indigo-600" />
            </th>
            <th data-sort="name" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider cursor-pointer hover:text-slate-700">Contact</th>
            <th data-sort="threads_count" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Threads</th>
            <th data-sort="replies_received" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Replies</th>
            <th data-sort="meetings_count" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Meetings</th>
            <th data-sort="last_contact_at" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Last contact</th>
            <th data-sort="score" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700"
                title="(emails + replies×2 + threads×3 + meetings×5) × recency_decay">
              Score ↓
            </th>
          </tr>
        </thead>
        <tbody id="rows" class="divide-y divide-slate-100"></tbody>
      </table>
    </div>

    <p class="mt-6 text-xs text-slate-400 text-center">
      Draftboard supporter scan · Local review · Nothing leaves your laptop unless you click Save and send the file yourself.
    </p>
  </div>

  <script type="application/json" id="data">{data_blob}</script>
  <script>
  (function () {{
    const payload = JSON.parse(document.getElementById('data').textContent);

    // Merge gmail + calendar contacts by email so each row represents a
    // unique person. Apply the same bidirectional filter the kit uses.
    const merged = new Map();
    for (const c of payload.gmail_contacts || []) {{
      merged.set(c.email, {{
        email: c.email,
        name: c.name || c.email,
        emails_sent: c.emails_sent || 0,
        replies_received: c.replies_received || 0,
        threads_count: c.threads_count || 0,
        meetings_count: 0,
        last_contact_at: c.last_contact_at || 0,
      }});
    }}
    for (const c of payload.calendar_contacts || []) {{
      const existing = merged.get(c.email) || {{
        email: c.email, name: c.name || c.email,
        emails_sent: 0, replies_received: 0, threads_count: 0,
        meetings_count: 0, last_contact_at: 0,
      }};
      existing.meetings_count = c.meetings_count || 0;
      existing.last_contact_at = Math.max(existing.last_contact_at, c.last_met_at || 0);
      if (c.name && (!existing.name || existing.name === existing.email)) existing.name = c.name;
      merged.set(c.email, existing);
    }}

    // Bidirectional filter — same logic as the kit's db_query_candidates.
    const now = Math.floor(Date.now() / 1000);
    function score(d) {{
      const base = d.emails_sent + 2*d.replies_received + 3*d.threads_count + 5*d.meetings_count;
      if (!d.last_contact_at) return 0;
      const daysAgo = (now - d.last_contact_at) / 86400;
      const recency = Math.max(0.1, 1.0 - (daysAgo / 365));
      return Math.round(base * recency);
    }}
    const rows = [];
    for (const d of merged.values()) {{
      const gmailOk = d.emails_sent >= 1 && d.replies_received >= 1;
      const meetOk = d.meetings_count >= 1;
      if (!gmailOk && !meetOk) continue;
      d.score = score(d);
      d.included = true;  // default ticked
      rows.push(d);
    }}
    rows.sort((a, b) => b.score - a.score);

    let sortKey = 'score', sortDir = -1;
    let searchQuery = '';

    function relTime(ts) {{
      if (!ts) return '—';
      const days = Math.floor((now - ts) / 86400);
      if (days < 1) return 'today';
      if (days < 7) return days + 'd ago';
      if (days < 30) return Math.floor(days/7) + 'w ago';
      if (days < 365) return Math.floor(days/30) + 'mo ago';
      return (days/365).toFixed(1) + 'y ago';
    }}

    function escapeHtml(s) {{
      return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}})[c]);
    }}

    function render() {{
      const tbody = document.getElementById('rows');
      const q = searchQuery.toLowerCase();
      let visible = rows;
      if (q) visible = rows.filter(r => r.email.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q));
      visible.sort((a, b) => {{
        const av = a[sortKey], bv = b[sortKey];
        if (av < bv) return -1 * sortDir;
        if (av > bv) return 1 * sortDir;
        return 0;
      }});
      tbody.innerHTML = visible.map(r => `
        <tr class="hover:bg-slate-50">
          <td class="px-3 py-2 align-middle">
            <input type="checkbox" data-email="${{escapeHtml(r.email)}}" ${{r.included ? 'checked' : ''}}
                   class="row-check h-4 w-4 rounded border-slate-300 text-indigo-600" />
          </td>
          <td class="px-3 py-2 align-middle">
            <div class="font-medium text-slate-900">${{escapeHtml(r.name)}}</div>
            <div class="text-xs text-slate-500">${{escapeHtml(r.email)}}</div>
          </td>
          <td class="px-3 py-2 text-right tabular-nums">${{r.threads_count}}</td>
          <td class="px-3 py-2 text-right tabular-nums">${{r.replies_received}}</td>
          <td class="px-3 py-2 text-right tabular-nums">${{r.meetings_count}}</td>
          <td class="px-3 py-2 text-right text-xs text-slate-500">${{relTime(r.last_contact_at)}}</td>
          <td class="px-3 py-2 text-right">
            <span class="inline-flex items-center rounded-full bg-indigo-50 text-indigo-700 px-2 py-0.5 text-xs font-medium tabular-nums">${{r.score}}</span>
          </td>
        </tr>
      `).join('');
      updateCount();
    }}

    function updateCount() {{
      const total = rows.length;
      const checked = rows.filter(r => r.included).length;
      document.getElementById('count').textContent =
        `${{checked}} / ${{total}} will be sent`;
    }}

    // Wire up
    document.getElementById('rows').addEventListener('change', e => {{
      if (!e.target.classList.contains('row-check')) return;
      const email = e.target.dataset.email;
      const row = rows.find(r => r.email === email);
      if (row) {{ row.included = e.target.checked; updateCount(); }}
    }});

    document.getElementById('head-check').addEventListener('change', e => {{
      // Only flips currently-visible rows (respects search filter)
      const q = searchQuery.toLowerCase();
      const visible = q ? rows.filter(r => r.email.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q)) : rows;
      for (const r of visible) r.included = e.target.checked;
      render();
    }});

    document.getElementById('check-all').addEventListener('click', () => {{
      const q = searchQuery.toLowerCase();
      const visible = q ? rows.filter(r => r.email.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q)) : rows;
      for (const r of visible) r.included = true;
      render();
    }});

    document.getElementById('uncheck-all').addEventListener('click', () => {{
      const q = searchQuery.toLowerCase();
      const visible = q ? rows.filter(r => r.email.toLowerCase().includes(q) || (r.name || '').toLowerCase().includes(q)) : rows;
      for (const r of visible) r.included = false;
      render();
    }});

    document.getElementById('search').addEventListener('input', e => {{
      searchQuery = e.target.value;
      render();
    }});

    document.querySelectorAll('th[data-sort]').forEach(th => {{
      th.addEventListener('click', () => {{
        const k = th.dataset.sort;
        if (sortKey === k) sortDir *= -1;
        else {{ sortKey = k; sortDir = (k === 'name' ? 1 : -1); }}
        document.querySelectorAll('th[data-sort]').forEach(other => {{
          other.textContent = other.textContent.replace(/[↓↑]\\s*$/, '').trim();
        }});
        const arrow = sortDir === -1 ? ' ↓' : ' ↑';
        th.textContent = th.textContent.trim() + arrow;
        render();
      }});
    }});

    document.getElementById('save').addEventListener('click', () => {{
      const includedEmails = new Set(rows.filter(r => r.included).map(r => r.email));
      const filtered = {{
        ...payload,
        gmail_contacts: (payload.gmail_contacts || []).filter(c => includedEmails.has(c.email)),
        calendar_contacts: (payload.calendar_contacts || []).filter(c => includedEmails.has(c.email)),
        filtered: true,
        original_total: rows.length,
        included_total: includedEmails.size,
      }};
      const blob = new Blob([JSON.stringify(filtered, null, 2)], {{ type: 'application/json' }});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = {filtered_filename!r};
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);

      // Tiny visual confirmation
      const btn = document.getElementById('save');
      const orig = btn.textContent;
      btn.textContent = '✓ Downloaded — send the file';
      btn.classList.remove('bg-indigo-600', 'hover:bg-indigo-700');
      btn.classList.add('bg-emerald-600');
      setTimeout(() => {{
        btn.textContent = orig;
        btn.classList.remove('bg-emerald-600');
        btn.classList.add('bg-indigo-600', 'hover:bg-indigo-700');
      }}, 3000);
    }});

    render();
  }})();
  </script>
</body>
</html>
"""


# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Draftboard Supporter Scan — outputs an HTML review page for filtering before sharing")
    ap.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS, help="History window in days (default 365)")
    ap.add_argument("--threads-cap", type=int, default=DEFAULT_THREADS_CAP, help="Max Gmail threads to fetch (default 2000)")
    ap.add_argument("--events-cap", type=int, default=DEFAULT_EVENTS_CAP, help="Max Calendar events to fetch (default 2500)")
    ap.add_argument("--out", help="Output HTML path (default: supporter_scan_<email>_<date>.html next to script)")
    args = ap.parse_args()

    print("=" * 60)
    print("Draftboard Supporter Scan")
    print("=" * 60)
    print()
    print("This scans the last 12 months of your Gmail + Calendar to find")
    print("the people you actually engage with — back-and-forth emails and")
    print("real meetings. We'll then produce an HTML review page so you can")
    print("untick anyone you don't want shared before sending the result.")
    print()
    print("Reads metadata only (no message contents). All data stays local.")
    print()

    creds = authenticate()

    # Resolve the user's email so we can correctly classify "sent vs received".
    try:
        oauth2 = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        profile = oauth2.userinfo().get().execute() or {}
        my_email = (profile.get("email") or "").lower()
        my_name = profile.get("name") or ""
    except Exception:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        my_email = (gmail.users().getProfile(userId="me").execute() or {}).get("emailAddress", "").lower()
        my_name = ""

    if not my_email:
        print("Could not determine your Google email. Aborting.")
        sys.exit(1)

    print(f"Signed in as: {my_email}")
    print()
    print("Scanning your network. This usually takes 3-5 minutes.")
    print()

    started = time.time()
    gmail_contacts = fetch_gmail(creds, my_email, args.days, args.threads_cap)
    calendar_contacts = fetch_calendar(creds, my_email, args.days, args.events_cap)
    elapsed = int(time.time() - started)

    # Build the output JSON.
    output = {
        "schema_version": SCHEMA_VERSION,
        "scan_type": "draftboard_supporter_scan",
        "scanned_by": {"email": my_email, "name": my_name},
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "history_days": args.days,
        "gmail_contacts": [
            {
                "email": em,
                "name": c.get("name", ""),
                "emails_sent": c.get("emails_sent", 0),
                "replies_received": c.get("replies_received", 0),
                "threads_count": c.get("threads_count", 0),
                "last_contact_at": c.get("last_contact_at", 0),
            }
            for em, c in gmail_contacts.items()
        ],
        "calendar_contacts": [
            {
                "email": em,
                "name": c.get("name", ""),
                "meetings_count": c.get("meetings_count", 0),
                "last_met_at": c.get("last_met_at", 0),
            }
            for em, c in calendar_contacts.items()
        ],
    }

    out_path = args.out
    if not out_path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        safe_email = re.sub(r"[^A-Za-z0-9]+", "_", my_email)
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                f"supporter_scan_{safe_email}_{date_str}.html")

    html = _render_review_html(output)
    with open(out_path, "w") as f:
        f.write(html)

    # Preview top candidates so Marcus sees value at the terminal.
    top, qualifying_total = merged_top_n(gmail_contacts, calendar_contacts, n=10)
    print()
    print("=" * 60)
    print(f"✓ Scan complete in {elapsed//60}m {elapsed%60}s")
    print("=" * 60)
    print(f"  Gmail contacts found:       {len(gmail_contacts):>6}")
    print(f"  Calendar contacts found:    {len(calendar_contacts):>6}")
    print(f"  Qualifying after filter:    {qualifying_total:>6}")
    print()
    if top:
        print("Top candidates by engagement:")
        print(f"  {'Score':>5}  {'Threads':>7}  {'Replies':>7}  {'Meetings':>8}  Contact")
        for c in top:
            display_name = c["name"] if c["name"] != c["email"] else c["email"]
            print(f"  {c['score']:>5}  {c['threads_count']:>7}  {c['replies_received']:>7}  {c['meetings_count']:>8}  {display_name} <{c['email']}>")
    print()
    print("=" * 60)
    print(" NEXT STEPS — please do these")
    print("=" * 60)
    print()
    print(f"  1. Open this file in your web browser:")
    print(f"     {out_path}")
    print()
    print("     (Double-click it in Finder/Explorer, or drag it onto a browser tab.)")
    print()
    print("  2. Skim the table. Untick the checkbox next to ANYONE you don't want")
    print("     shared with the person who asked you to run this. Use the search")
    print("     box to narrow down quickly.")
    print()
    print("  3. Click the blue 'Save Filtered JSON →' button at the top right.")
    print("     A file like supporter_scan_*_filtered.json will download.")
    print()
    print("  4. Send THAT downloaded JSON file back to whoever asked you")
    print("     (Slack, email, whatever). They'll import it on their end.")
    print()
    print("  Nothing leaves your laptop until you send the file yourself.")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
