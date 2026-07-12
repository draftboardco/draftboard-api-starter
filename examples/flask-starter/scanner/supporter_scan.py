#!/usr/bin/env python3
"""Draftboard Supporter Scan — standalone portable scanner.

Run on YOUR laptop. Authenticates with Google in your browser, scans the
last 12 months of Gmail metadata + Calendar events, scores per-contact
engagement, and writes a JSON file you can send to whoever set up your
team's Draftboard kit. They'll import it; their kit will pool your network
into the team's Supporter candidates view.

Privacy:
- Reads message metadata + subject lines + Gmail's short preview snippet,
  locally on this laptop. Never opens full message bodies.
- Subjects/snippets are used ONLY to label each relationship (friend, investor,
  customer, vendor, etc.) and to spot who has made you introductions. They are
  NEVER written to the output file.
- The shareable JSON contains only contact emails + names + counts + a
  relationship label. No subjects, no snippets, no message text.
- All data stays on this laptop until you explicitly send the JSON.

Usage:
    python3 supporter_scan.py

If the Google libraries aren't installed yet, this script offers to
install them with pip on first run.
"""

import argparse
import json
import math
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
    "calendar-notification", "auto-confirm", "unsubscribe", "unsub", "bounce",
    "bounces", "mailer-daemon", "postmaster", "mailer", "sales", "accounting",
    "accounts", "careers", "jobs", "security", "abuse", "webmaster", "digest",
    "cs", "care", "customerservice", "customer-service", "reply",
)

# Exact domains that are never a real person.
_NOISE_DOMAINS_EXACT = (
    "googlegroups.com", "calendar.google.com", "resource.calendar.google.com",
)

# Substrings that mark bulk-email / ESP / unsubscribe / test infrastructure —
# any domain containing one of these is machine mail, not a person.
_NOISE_DOMAIN_MARKERS = (
    "customer.io", "beehiiv", "mailjet", "mailpool.io", "sendgrid", "mailgun",
    "amazonses", "sparkpost", "mailchimp", "mcsv.net", "mcdlv.net", "hubspot",
    "sendinblue", "constantcontact", "unsubscribe", "unsub.", "bnc3",
    "terrapinn", "postmarkapp", "sparkpostmail", ".test", "example.com",
)


def is_noise_email(email):
    if not email or "@" not in email:
        return True
    local, _, domain = email.partition("@")
    local = local.lower()
    domain = domain.lower()
    if domain in _NOISE_DOMAINS_EXACT:
        return True
    if any(marker in domain for marker in _NOISE_DOMAIN_MARKERS):
        return True
    if local.startswith(_NOREPLY_PREFIXES):
        return True
    # Plus-addressed bounce/unsubscribe/test variants (e.g. bounce+abc@…).
    if "+" in local and local.startswith(("bounce", "bounces", "unsub", "unsubscribe")):
        return True
    return False


# -----------------------------------------------------------------------
# Self-identity + intro detection + relationship classification
#
# These read subject lines and Gmail's short snippet preview (never full
# message bodies) purely to (a) spot who has introduced you to others and
# (b) label each relationship. None of this text is ever written out.
# -----------------------------------------------------------------------

def _is_me(email, me, my_local, my_domain):
    """True for my own address, including plus-addressed variants
    (zach+test@…) which are typically automated self-mail."""
    if not email:
        return False
    if email == me:
        return True
    local, _, domain = email.partition("@")
    if domain == my_domain and (local == my_local or local.startswith(my_local + "+")):
        return True
    return False


_INTRO_MARKERS = (
    "intro:", "introduction", "introducing", "introduce you", "connecting you",
    "connect you", " <> ", " x ", "meet my", "pls meet", "please meet",
    "putting you in touch", "happy to intro", "happy to connect",
    "let me know other intros", "i'll let you two", "let you two take it",
    "take it from here", "cc'ing", "adding ", "loop in", "looping in",
    "warm intro",
)


def _has_intro_signal(text):
    t = (text or "").lower()
    return any(m in t for m in _INTRO_MARKERS)


# Keyword banks for relationship classification. Kept generic (not
# Draftboard-specific) so the scanner works on any teammate's mailbox.
_PERSONAL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
    "aol.com", "hey.com", "live.com", "msn.com", "gmx.com",
})
_INVESTOR_DOMAIN_MARKERS = ("capital", "ventures", "venture", "partners", "vc",
                            "equity", "fund", "invest")
_INVESTOR_WORDS = ("investing", "invest in", "raising", "fundraise", "fundraising",
                   "term sheet", "portfolio", "cap table", "valuation", "our fund",
                   "led the round", "check size", "the raise", "your round")
_PERSONAL_WORDS = ("dinner", "lunch", "coffee", "drinks", "weekend", "family",
                   "kids", "birthday", "vacation", "holiday", "shabbat", "congrats",
                   "congratulations", "haha", "lol", "love you", "miss you",
                   "catch up", "how are you", "great to see you", "good to see you",
                   "<3", "❤", "🙏", "hope you", "your trip", "how's the family")
_SALES_WORDS = ("demo", "free trial", "pricing", "onboard", "sign up", "signup",
                "get started", "book a time", "proposal", "quote", "great chatting",
                "thanks for starting", "schedule a call", "get you set up",
                "onboarding", "trial", "would love to chat", "reschedule our call")
_SUPPORT_WORDS = ("error", "bug", "not working", "doesn't work", "isn't working",
                  "issue", "how do i", "can't", "cannot", "reset", "log in",
                  "login", "broken", "help with", "having trouble", "question about",
                  "not showing", "won't load", "stuck")
# Strong, specific churn phrases (avoid bare "cancel" — it fires on
# "cancel the meeting" and every calendar cancellation).
_CANCEL_WORDS = ("refund", "delete my account", "cancel my account",
                 "cancel my subscription", "unsubscribe me", "stop billing",
                 "not renewing", "you cancelled", "you canceled",
                 "saw you cancelled", "saw you canceled", "downgrade my")
_VENDOR_WORDS = ("invoice", "receipt", "payment due", "statement", "reservation",
                 "booking", "order confirmation", "shipping", "renewal notice",
                 "past due", "wire transfer", "payroll", "tax", "utility",
                 "electric bill", "water bill", "outstanding")

# Supporter-likelihood multiplier per relationship type. Warm human ties
# score full; transactional/cold ties are heavily discounted even when
# they generate a lot of email.
TYPE_WEIGHTS = {
    "friend": 1.0,
    "colleague": 0.9,
    "advisor": 1.0,
    "investor_warm": 1.0,
    "other": 0.7,
    "customer": 0.55,
    "investor_inbound": 0.4,
    "sales_prospect": 0.15,
    "vendor": 0.1,
    "customer_churned": 0.1,
}

# Human-readable labels for display.
TYPE_LABELS = {
    "friend": "Friend / personal",
    "colleague": "Colleague",
    "advisor": "Advisor / mentor",
    "investor_warm": "Investor (warm)",
    "investor_inbound": "Investor (inbound)",
    "customer": "Customer / user",
    "customer_churned": "Customer (churned)",
    "sales_prospect": "Sales prospect",
    "vendor": "Vendor / service",
    "other": "Other",
}


def classify_relationship(email, text, emails_sent, replies_received, my_domain):
    """Best-effort relationship type from domain + subjects/snippets + direction.

    Heuristic (no LLM) — deterministic and local. Returns one of the keys in
    TYPE_WEIGHTS. Priority order matters: transactional signals are checked
    before warm ones so a churn/support thread isn't mislabeled 'friend'."""
    t = (text or "").lower()
    domain = email.partition("@")[2].lower()
    local = email.partition("@")[0].lower()

    def hits(words):
        return sum(1 for w in words if w in t)

    sent = emails_sent or 0
    rep = replies_received or 0
    reciprocity = (rep / sent) if sent else (1.0 if rep else 0.0)

    # Vendors / life-admin — transactional counterparties.
    if hits(_VENDOR_WORDS) >= 2 or local in ("cs", "billing", "accounting", "invoices", "payments"):
        return "vendor"

    # Churned customers — strong cancel language, not a personal thread.
    if hits(_CANCEL_WORDS) >= 1 and hits(_PERSONAL_WORDS) == 0:
        return "customer_churned"

    # Investors. "Warm" means I actually engaged and it's roughly balanced;
    # if they email far more than I reply (a VC chasing me), that's inbound.
    inv_domain = any(m in domain for m in _INVESTOR_DOMAIN_MARKERS)
    if inv_domain or hits(_INVESTOR_WORDS) >= 1:
        warm = sent >= 2 and rep >= 1 and rep <= 3 * sent
        return "investor_warm" if warm else "investor_inbound"

    # Sales prospects — my templated outbound, mostly one-directional.
    if hits(_SALES_WORDS) >= 2 and sent >= rep:
        return "sales_prospect"

    # Active customers reaching in for support/help.
    if hits(_SUPPORT_WORDS) >= 2:
        return "customer"

    # Friends / personal.
    personal_domain = domain in _PERSONAL_DOMAINS
    if hits(_PERSONAL_WORDS) >= 2 or (
        personal_domain
        and (hits(_SALES_WORDS) + hits(_SUPPORT_WORDS) + hits(_VENDOR_WORDS)) == 0
        and rep >= 1
    ):
        return "friend"

    # Same-org colleagues.
    if my_domain and domain == my_domain:
        return "colleague"

    return "other"


# -----------------------------------------------------------------------
# Gmail + Calendar fetchers
# -----------------------------------------------------------------------

def fetch_gmail(creds, my_email, days, cap):
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    me = (my_email or "").lower()
    my_local = me.partition("@")[0]
    my_domain = me.partition("@")[2]
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
                metadataHeaders=["From", "To", "Cc", "Date", "Subject"],
            ).execute()
        except HttpError:
            if (i + 1) % 50 == 0:
                _progress("Gmail", i + 1, len(thread_ids))
            continue

        messages = thread.get("messages", []) or []
        thread_contacts = {}       # em -> {name, sent_by_me, replies}
        participants = set()       # non-me, non-noise humans on the thread
        text_parts = []            # subjects + snippets, for classification only
        intro_senders = set()      # who sent an intro-signal message
        most_recent_ts = 0
        months = set()

        for msg in messages:
            headers = {h.get("name", "").lower(): h.get("value", "") for h in
                       (msg.get("payload", {}).get("headers", []) or [])}
            subject = headers.get("subject", "")
            snippet = msg.get("snippet", "") or ""
            text_parts.append(subject)
            text_parts.append(snippet)
            from_addrs = parse_addresses(headers.get("from", ""))
            to_addrs = parse_addresses(headers.get("to", ""))
            cc_addrs = parse_addresses(headers.get("cc", ""))
            try:
                msg_ts = int(msg.get("internalDate", "0")) // 1000
            except (TypeError, ValueError):
                msg_ts = 0
            if msg_ts > most_recent_ts:
                most_recent_ts = msg_ts
            if msg_ts:
                months.add(datetime.utcfromtimestamp(msg_ts).strftime("%Y-%m"))

            sender_email = from_addrs[0][1] if from_addrs else ""
            sent_by_me = _is_me(sender_email, me, my_local, my_domain)
            if _has_intro_signal(subject + " " + snippet) and sender_email and not sent_by_me:
                intro_senders.add(sender_email)

            for nm, em in from_addrs + to_addrs + cc_addrs:
                if _is_me(em, me, my_local, my_domain) or is_noise_email(em):
                    continue
                participants.add(em)
                rec = thread_contacts.setdefault(em, {"name": "", "sent_by_me": 0, "replies": 0})
                if nm and not rec["name"]:
                    rec["name"] = nm
                if sent_by_me:
                    rec["sent_by_me"] += 1
                elif em == sender_email:
                    rec["replies"] += 1

        n_others = len(participants)          # distinct people besides me
        is_one_to_one = (n_others == 1)
        is_deep = len(messages) >= 4          # real multi-turn back-and-forth
        thread_text = " ".join(text_parts).lower()
        thread_has_intro = _has_intro_signal(thread_text)

        for em, rec in thread_contacts.items():
            agg = contacts.setdefault(em, {
                "name": "", "emails_sent": 0, "replies_received": 0,
                "threads_count": 0, "one_to_one_threads": 0, "deep_threads": 0,
                "last_contact_at": 0, "months": set(), "text_parts": [],
                "text_len": 0, "intro_maker": False,
            })
            if rec["name"] and not agg["name"]:
                agg["name"] = rec["name"]
            agg["emails_sent"] += rec["sent_by_me"]
            agg["replies_received"] += rec["replies"]
            agg["threads_count"] += 1
            if is_one_to_one:
                agg["one_to_one_threads"] += 1
            if is_deep and (rec["sent_by_me"] + rec["replies"]) >= 1:
                agg["deep_threads"] += 1
            agg["months"].update(months)
            if most_recent_ts > agg["last_contact_at"]:
                agg["last_contact_at"] = most_recent_ts
            # Keep a bounded bag of subject/snippet text for classification.
            if agg["text_len"] < 4000:
                chunk = thread_text[:500]
                agg["text_parts"].append(chunk)
                agg["text_len"] += len(chunk)
            # Intro-maker: they explicitly sent intro language, OR they took
            # part in a multi-party thread that reads like an introduction.
            if em in intro_senders or (thread_has_intro and n_others >= 2
                                       and (rec["sent_by_me"] + rec["replies"]) >= 1):
                agg["intro_maker"] = True

        if (i + 1) % 25 == 0:
            _progress("Gmail", i + 1, len(thread_ids))

    # Finalize: collapse the transient month-set + text-bag into the fields we
    # keep. relationship_type is computed here (needs the text); the text bag
    # itself is discarded and never leaves this process.
    for em, agg in contacts.items():
        agg["active_months"] = len(agg.pop("months"))
        text = " ".join(agg.pop("text_parts"))
        agg.pop("text_len", None)
        agg["relationship_type"] = classify_relationship(
            em, text, agg["emails_sent"], agg["replies_received"], my_domain)

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
            # A small meeting (<=5 attendees) is a real relationship signal; a
            # 30-person all-hands is noise. Track both, weight only the small.
            human_attendees = [a for a in attendees if not a.get("resource")]
            is_small = len(human_attendees) <= 5
            for a in attendees:
                email = (a.get("email") or "").lower()
                if not email or email == me or is_noise_email(email):
                    continue
                if a.get("responseStatus") == "declined":
                    continue
                rec = contacts.setdefault(email, {
                    "name": "", "meetings_count": 0, "small_meetings": 0, "last_met_at": 0})
                nm = a.get("displayName") or ""
                if nm and not rec["name"]:
                    rec["name"] = nm
                rec["meetings_count"] += 1
                if is_small:
                    rec["small_meetings"] += 1
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
# Scoring — supporter-likelihood, 0-100. Rewards relationship QUALITY over
# raw volume: reciprocity, 1:1 vs group, conversation depth, how long the
# relationship has been sustained, small meetings, and a big boost for
# people who've actually introduced you to others. A relationship-type
# multiplier discounts sales/vendor/support/inbound-VC/churned ties even
# when they're high-volume.
#
# The JSON still carries the raw counts so the Flask kit can re-score
# authoritatively on import; this score drives the local review page.
# -----------------------------------------------------------------------

def score(d):
    sent = d.get("emails_sent", 0)
    rep = d.get("replies_received", 0)
    threads = d.get("threads_count", 0)
    o2o = d.get("one_to_one_threads", 0)
    deep = d.get("deep_threads", 0)
    months = d.get("active_months", 0)
    small_meet = d.get("small_meetings", 0)
    last = d.get("last_contact_at", 0)
    rtype = d.get("relationship_type", "other")
    intro = d.get("intro_maker", False)

    responsiveness = min(1.0, rep / sent) if sent else (1.0 if rep else 0.0)
    o2o_ratio = (o2o / threads) if threads else 0.0
    depth = min(1.0, deep / threads) if threads else 0.0
    span = min(1.0, months / 6.0)
    volume = min(1.0, math.log1p(threads) / math.log(11)) if threads else 0.0
    meeting_sig = min(1.0, small_meet / 3.0)

    base = 100.0 * (
        0.28 * responsiveness + 0.20 * o2o_ratio + 0.14 * depth +
        0.14 * span + 0.09 * volume + 0.15 * meeting_sig
    )
    now = int(time.time())
    if last:
        days_ago = (now - last) / 86400.0
        recency = max(0.35, 1.0 - days_ago / 365.0)  # gentle floor — old-but-strong ties survive
    else:
        recency = 0.35
    s = base * recency * TYPE_WEIGHTS.get(rtype, 0.7)
    if intro:
        s += 22.0
    return int(max(0, min(100, round(s))))


def rank_all(gmail_contacts, calendar_contacts, my_email):
    """Merge Gmail + Calendar per person, keep everyone eligible (a two-way
    email relationship OR at least one meeting), score, and return the FULL
    ranked list (no cap), highest score first."""
    my_domain = (my_email or "").partition("@")[2].lower()
    merged = {}
    for em, c in gmail_contacts.items():
        merged[em] = {
            "email": em,
            "name": c.get("name") or em,
            "emails_sent": c.get("emails_sent", 0),
            "replies_received": c.get("replies_received", 0),
            "threads_count": c.get("threads_count", 0),
            "one_to_one_threads": c.get("one_to_one_threads", 0),
            "deep_threads": c.get("deep_threads", 0),
            "active_months": c.get("active_months", 0),
            "meetings_count": 0,
            "small_meetings": 0,
            "last_contact_at": c.get("last_contact_at", 0),
            "intro_maker": bool(c.get("intro_maker", False)),
            "relationship_type": c.get("relationship_type", "other"),
        }
    for em, c in calendar_contacts.items():
        d = merged.setdefault(em, {
            "email": em, "name": c.get("name") or em,
            "emails_sent": 0, "replies_received": 0, "threads_count": 0,
            "one_to_one_threads": 0, "deep_threads": 0, "active_months": 0,
            "meetings_count": 0, "small_meetings": 0, "last_contact_at": 0,
            "intro_maker": False,
            # Calendar-only contacts have no message text — classify by domain.
            "relationship_type": classify_relationship(em, "", 0, 0, my_domain),
        })
        d["meetings_count"] = c.get("meetings_count", 0)
        d["small_meetings"] = c.get("small_meetings", 0)
        d["last_contact_at"] = max(d["last_contact_at"], c.get("last_met_at", 0))
        if c.get("name") and (not d["name"] or d["name"] == em):
            d["name"] = c["name"]

    ranked = []
    for em, d in merged.items():
        gmail_ok = d["emails_sent"] >= 1 and d["replies_received"] >= 1
        meet_ok = d["meetings_count"] >= 1
        if not (gmail_ok or meet_ok):
            continue
        sent = d["emails_sent"]
        d["reply_rate"] = round(min(1.0, d["replies_received"] / sent), 2) if sent else 0.0
        d["type_label"] = TYPE_LABELS.get(d["relationship_type"], "Other")
        d["score"] = score(d)
        ranked.append(d)
    ranked.sort(key=lambda x: (-x["score"], x["name"].lower()))
    return ranked


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
        Everyone below had a real back-and-forth or a meeting with you in the
        last {payload["history_days"]} days, ranked by how likely they are to
        make a warm intro for you (relationship quality, not email volume) and
        labelled by type. A <span class="text-amber-500">★</span> means they've
        introduced you to someone before. <strong>Untick anyone you don't want
        shared</strong> with the person who asked you to run this, then click
        <strong>Save Filtered JSON</strong> at the top right and send THAT file
        back to them — not this HTML.
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
            <th data-sort="type_label" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider cursor-pointer hover:text-slate-700">Type</th>
            <th data-sort="threads_count" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Threads</th>
            <th data-sort="replies_received" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Replies</th>
            <th data-sort="meetings_count" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Meetings</th>
            <th data-sort="last_contact_at" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700">Last contact</th>
            <th data-sort="score" class="px-3 py-2 font-medium text-[11px] uppercase tracking-wider text-right cursor-pointer hover:text-slate-700"
                title="Supporter score 0-100 — rewards reciprocity, 1:1 threads, conversation depth, how long you've stayed in touch, small meetings, and intro history. Discounts sales / vendor / support / inbound-VC / churned ties.">
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

    // Rows arrive pre-scored, pre-classified and pre-ranked from the scan
    // (payload.ranked_contacts). No merging or re-scoring here — we just
    // render them; the checkboxes control what gets saved.
    const now = Math.floor(Date.now() / 1000);
    const rows = (payload.ranked_contacts || []).map(r => Object.assign({{}}, r, {{
      name: r.name || r.email,
      type_label: r.type_label || 'Other',
      included: true,  // default ticked
    }}));

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
            <div class="font-medium text-slate-900">${{escapeHtml(r.name)}}${{r.intro_maker ? ' <span title="Has introduced you to someone before" class="text-amber-500">★</span>' : ''}}</div>
            <div class="text-xs text-slate-500">${{escapeHtml(r.email)}}</div>
          </td>
          <td class="px-3 py-2 align-middle">
            <span class="inline-flex items-center rounded-full bg-slate-100 text-slate-600 px-2 py-0.5 text-xs">${{escapeHtml(r.type_label || 'Other')}}</span>
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
        ranked_contacts: (payload.ranked_contacts || []).filter(c => includedEmails.has(c.email)),
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
    print("This scans the last 12 months of your Gmail + Calendar to find the")
    print("people most likely to make a warm intro for you — ranked by")
    print("relationship quality (reciprocity, 1:1s, real meetings, who's")
    print("introduced you before), not just email volume. We'll produce an HTML")
    print("review page so you can untick anyone you don't want shared.")
    print()
    print("Reads metadata + subject lines + short snippet previews, all locally")
    print("(never full message bodies). Only counts + a relationship label are")
    print("ever saved — no message text. Nothing leaves your laptop until you")
    print("send the file yourself.")
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
    print("Scanning your network. This usually takes 5-10 minutes depending on")
    print("how many threads + events you have in your last 12 months.")
    print()

    started = time.time()
    gmail_contacts = fetch_gmail(creds, my_email, args.days, args.threads_cap)
    calendar_contacts = fetch_calendar(creds, my_email, args.days, args.events_cap)
    elapsed = int(time.time() - started)

    # Rank everyone eligible (no cap) — this drives both the review page and
    # the terminal preview. Computed before rendering so the HTML can embed it.
    ranked = rank_all(gmail_contacts, calendar_contacts, my_email)

    # Build the output JSON. schema_version stays 1 (the Flask kit importer
    # requires it); new fields are additive and ignored by older importers.
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
                "one_to_one_threads": c.get("one_to_one_threads", 0),
                "deep_threads": c.get("deep_threads", 0),
                "active_months": c.get("active_months", 0),
                "last_contact_at": c.get("last_contact_at", 0),
                "intro_maker": bool(c.get("intro_maker", False)),
                "relationship_type": c.get("relationship_type", "other"),
            }
            for em, c in gmail_contacts.items()
        ],
        "calendar_contacts": [
            {
                "email": em,
                "name": c.get("name", ""),
                "meetings_count": c.get("meetings_count", 0),
                "small_meetings": c.get("small_meetings", 0),
                "last_met_at": c.get("last_met_at", 0),
            }
            for em, c in calendar_contacts.items()
        ],
        # Pre-scored, pre-classified, fully ranked — what the review page shows.
        "ranked_contacts": ranked,
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

    # Preview top candidates so the person running it sees value immediately.
    qualifying_total = len(ranked)
    top = ranked[:12]
    print()
    print("=" * 60)
    print(f"✓ Scan complete in {elapsed//60}m {elapsed%60}s")
    print("=" * 60)
    print(f"  Gmail contacts found:       {len(gmail_contacts):>6}")
    print(f"  Calendar contacts found:    {len(calendar_contacts):>6}")
    print(f"  Eligible + ranked:          {qualifying_total:>6}")
    print()
    if top:
        print("Top likely supporters (all eligible are in the review file):")
        print(f"  {'Score':>5}  {'Intro':>5}  {'Type':<18}  Contact")
        for c in top:
            display_name = c["name"] if c["name"] != c["email"] else c["email"]
            intro_flag = " ★ " if c.get("intro_maker") else "   "
            label = (c.get("type_label") or "Other")[:18]
            print(f"  {c['score']:>5}  {intro_flag:>5}  {label:<18}  {display_name} <{c['email']}>")
        print()
        print("  ★ = has introduced you to someone before (strongest supporter signal)")
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
