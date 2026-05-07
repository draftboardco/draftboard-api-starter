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
# Main
# -----------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Draftboard Supporter Scan — exports your network as a JSON")
    ap.add_argument("--days", type=int, default=DEFAULT_HISTORY_DAYS, help="History window in days (default 365)")
    ap.add_argument("--threads-cap", type=int, default=DEFAULT_THREADS_CAP, help="Max Gmail threads to fetch (default 2000)")
    ap.add_argument("--events-cap", type=int, default=DEFAULT_EVENTS_CAP, help="Max Calendar events to fetch (default 2500)")
    ap.add_argument("--out", help="Output JSON path (default: supporter_scan_<email>_<date>.json next to script)")
    args = ap.parse_args()

    print("=" * 60)
    print("Draftboard Supporter Scan")
    print("=" * 60)
    print()
    print("This scans the last 12 months of your Gmail + Calendar to find")
    print("the people you actually engage with — back-and-forth emails and")
    print("real meetings. Output: a JSON file your teammate imports into")
    print("their Draftboard Flask app.")
    print()
    print("Reads metadata only (no message contents). All data stays local")
    print("until you explicitly share the JSON.")
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
                                f"supporter_scan_{safe_email}_{date_str}.json")

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    # Preview top candidates so Marcus sees value before sending.
    top, qualifying_total = merged_top_n(gmail_contacts, calendar_contacts, n=10)
    print()
    print("=" * 60)
    print(f"✓ Scan complete in {elapsed//60}m {elapsed%60}s")
    print("=" * 60)
    print(f"  Gmail contacts found:       {len(gmail_contacts):>6}")
    print(f"  Calendar contacts found:    {len(calendar_contacts):>6}")
    print(f"  Qualifying after filter:    {qualifying_total:>6}")
    print(f"  Output:                     {out_path}")
    print()
    if top:
        print("Top candidates by engagement:")
        print(f"  {'Score':>5}  {'Threads':>7}  {'Replies':>7}  {'Meetings':>8}  Contact")
        for c in top:
            display_name = c["name"] if c["name"] != c["email"] else c["email"]
            print(f"  {c['score']:>5}  {c['threads_count']:>7}  {c['replies_received']:>7}  {c['meetings_count']:>8}  {display_name} <{c['email']}>")
    print()
    print("→ Send the JSON to your teammate. They'll upload it to their")
    print("  Flask kit at /supporters/import-teammate. Your network will be")
    print("  pooled into the team's Supporter candidates view, tagged as")
    print(f"  contributed by you ({my_email}).")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)
