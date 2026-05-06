"""
Draftboard API starter — Targets view + Import page.

A minimal Flask app that:
  - Targets view at GET /  : paginates GET /targets and renders a list
                            sorted by score desc. Empty state links to /import.
  - Import page at GET /import: renders the LinkedIn-URL paste form with
                            tag suggestion chips populated from GET /tags.
  - POST /import        : submits to POST /targets/import and renders the result.
  - GET /tags           : convenience JSON pass-through.

Env:
  DRAFTBOARD_API_KEY=db-api_<UUID>   # required for real calls; the page still
                                      renders with a placeholder/missing key.

Run:
  export DRAFTBOARD_API_KEY=db-api_xxxxx
  python app.py
  # then open http://localhost:5050
"""

import os
import re
import time
import json
import html
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, jsonify, redirect, url_for
import requests

API_BASE = "https://intros.draftboard.com/api/v1/integration"
API_KEY = os.environ.get("DRAFTBOARD_API_KEY", "")
TARGETS_CACHE_TTL = 300  # 5 minutes; Refresh button forces a re-fetch
TAGS_CACHE_TTL = 600  # 10 minutes
CONNECTIONS_CACHE_TTL = 24 * 3600  # 24 hours; SQLite-persisted connections
ACCOUNT_FANOUT_LIMIT = 50  # max targets to fan out (mostly cache hits after sync)
SYNC_CONCURRENCY = 5  # parallel /targets/{id}/connections fetches during bulk sync

app = Flask(__name__)

# In-memory caches for the lightweight endpoints (targets list, tags list).
_targets_cache = {"data": None, "error": None, "fetched_at": 0}
_tags_cache = {"data": None, "error": None, "fetched_at": 0}

# SQLite-backed persistent caches: per-target connections + intro_requests state.
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
_db_lock = threading.Lock()

# Background sync worker state.
_sync_lock = threading.Lock()
_sync_state = {
    "running": False,
    "completed": 0,
    "total": 0,
    "errors": 0,
    "started_at": 0,
    "ended_at": 0,
    "last_target_name": "",
}
_sync_thread = None


def _db_connect():
    """Open a fresh SQLite connection. Caller closes."""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _db_lock, _db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connections (
                target_id TEXT PRIMARY KEY,
                connections_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL,
                error TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS intro_requests (
                target_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                requested_at INTEGER NOT NULL,
                PRIMARY KEY (target_id, connection_id)
            )
        """)
        conn.commit()


def db_get_connections(target_id):
    """Returns (data_list_or_None, fetched_at_or_0, error_or_None)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT connections_json, fetched_at, error FROM connections WHERE target_id = ?",
            (target_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, 0, None
    return json.loads(row[0]), row[1], row[2]


def db_put_connections(target_id, connections, error):
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connections (target_id, connections_json, fetched_at, error) VALUES (?, ?, ?, ?)",
            (target_id, json.dumps(connections or []), int(time.time()), error),
        )
        conn.commit()


def db_count_fresh_connections(target_ids, ttl=CONNECTIONS_CACHE_TTL):
    """Count how many of the given target_ids have a fresh row in connections."""
    if not target_ids:
        return 0
    threshold = int(time.time()) - ttl
    placeholders = ",".join(["?"] * len(target_ids))
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            f"SELECT COUNT(*) FROM connections WHERE fetched_at >= ? AND error IS NULL AND target_id IN ({placeholders})",
            (threshold, *target_ids),
        )
        return cur.fetchone()[0]


def db_intro_request_get(target_id, connection_id):
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM intro_requests WHERE target_id = ? AND connection_id = ?",
            (target_id, connection_id),
        )
        return cur.fetchone() is not None


def db_intro_request_toggle(target_id, connection_id):
    """Toggle and return the new state (True = requested, False = cleared)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM intro_requests WHERE target_id = ? AND connection_id = ?",
            (target_id, connection_id),
        )
        if cur.fetchone():
            conn.execute(
                "DELETE FROM intro_requests WHERE target_id = ? AND connection_id = ?",
                (target_id, connection_id),
            )
            conn.commit()
            return False
        conn.execute(
            "INSERT INTO intro_requests (target_id, connection_id, requested_at) VALUES (?, ?, ?)",
            (target_id, connection_id, int(time.time())),
        )
        conn.commit()
        return True


def db_intro_requests_for_target(target_id):
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT connection_id FROM intro_requests WHERE target_id = ?",
            (target_id,),
        )
        return {row[0] for row in cur.fetchall()}


# Initialize DB on import
init_db()


def _auth_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def fetch_tags(force=False):
    """GET /tags -> list of tag titles. Cached. Returns (list, error_or_None)."""
    now = time.time()
    if (
        not force
        and _tags_cache["data"] is not None
        and (now - _tags_cache["fetched_at"]) < TAGS_CACHE_TTL
    ):
        return _tags_cache["data"], _tags_cache["error"]

    if not API_KEY:
        return [], "DRAFTBOARD_API_KEY not set — suggestion chips will be empty."
    try:
        r = requests.get(
            f"{API_BASE}/tags",
            headers=_auth_headers(),
            params={"resultPerPage": 200},
            timeout=10,
        )
        if r.status_code == 401:
            return [], "API key was rejected (401). Suggestion chips will be empty."
        if r.status_code != 200:
            return [], f"GET /tags returned {r.status_code}."
        data = r.json()
        tags = [t.get("title") for t in (data.get("tags") or []) if t.get("title")]
        _tags_cache.update({"data": tags, "error": None, "fetched_at": now})
        return tags, None
    except requests.RequestException as e:
        err = f"Network error fetching tags: {e}"
        _tags_cache.update({"data": [], "error": err, "fetched_at": now})
        return [], err


def fetch_all_targets(force=False):
    """Paginate GET /targets and return (list, error_or_None). Cached.

    Loops pageNumber until nextPage == 0. Pulls 200/page (vs default 20) to
    cut the number of round-trips on big workspaces.
    """
    now = time.time()
    if (
        not force
        and _targets_cache["data"] is not None
        and (now - _targets_cache["fetched_at"]) < TARGETS_CACHE_TTL
    ):
        return _targets_cache["data"], _targets_cache["error"]

    if not API_KEY:
        return [], "DRAFTBOARD_API_KEY not set."

    targets = []
    page = 1
    try:
        while True:
            r = requests.get(
                f"{API_BASE}/targets",
                headers=_auth_headers(),
                params={"pageNumber": page, "resultPerPage": 200},
                timeout=30,
            )
            if r.status_code == 401:
                return [], "API key was rejected (401)."
            if r.status_code != 200:
                return [], f"GET /targets returned {r.status_code}."
            data = r.json()
            page_targets = data.get("targets") or []
            targets.extend(page_targets)
            next_page = data.get("nextPage") or 0
            if next_page == 0:
                break
            page = next_page
            if page > 50:
                break
        _targets_cache.update({"data": targets, "error": None, "fetched_at": now})
        return targets, None
    except requests.RequestException as e:
        err = f"Network error fetching targets: {e}"
        return [], err


def cache_age_seconds():
    """How old (sec) is the cached targets data, or None if not cached."""
    if _targets_cache["fetched_at"] == 0:
        return None
    return int(time.time() - _targets_cache["fetched_at"])


def fetch_target_connections(target_id, force=False):
    """Paginate GET /targets/{id}/connections. SQLite-cached per-target.

    Returns (list, error_or_None). When `force=False`, returns the cached row
    if it's within CONNECTIONS_CACHE_TTL seconds.
    """
    now = time.time()
    cached_data, fetched_at, cached_error = db_get_connections(target_id)
    if (
        not force
        and cached_data is not None
        and (now - fetched_at) < CONNECTIONS_CACHE_TTL
        and not cached_error
    ):
        return cached_data, None

    if not API_KEY:
        return [], "DRAFTBOARD_API_KEY not set."

    connections = []
    page = 1
    try:
        while True:
            r = requests.get(
                f"{API_BASE}/targets/{target_id}/connections",
                headers=_auth_headers(),
                params={"pageNumber": page, "resultPerPage": 200},
                timeout=20,
            )
            if r.status_code == 401:
                err = "API key was rejected (401)."
                db_put_connections(target_id, [], err)
                return [], err
            if r.status_code != 200:
                err = f"GET /targets/{target_id}/connections returned {r.status_code}."
                db_put_connections(target_id, [], err)
                return [], err
            data = r.json()
            page_conns = data.get("connections") or []
            connections.extend(page_conns)
            next_page = data.get("nextPage") or 0
            if next_page == 0:
                break
            page = next_page
            if page > 20:
                break
        db_put_connections(target_id, connections, None)
        return connections, None
    except requests.RequestException as e:
        return [], f"Network error: {e}"


def _sync_one_target(t):
    """Fetch+cache connections for a single target; update sync state. Used by
    the ThreadPoolExecutor in _sync_worker."""
    tid = t.get("id")
    if not tid:
        return
    first = (t.get("firstName") or "").strip()
    last = (t.get("lastName") or "").strip()
    name = f"{first} {last}".strip() or "(no name)"
    with _sync_lock:
        _sync_state["last_target_name"] = name
    _, err = fetch_target_connections(tid, force=True)
    with _sync_lock:
        _sync_state["completed"] += 1
        if err:
            _sync_state["errors"] += 1


def _sync_worker():
    """Background worker: fetches and caches connections for every target in parallel.

    Skips targets already fresh in SQLite. Uses a ThreadPoolExecutor so multiple
    /targets/{id}/connections calls run concurrently — modest concurrency
    (SYNC_CONCURRENCY) keeps us friendly to the API while cutting sync time ~5x.
    """
    targets, _err = fetch_all_targets()
    if not targets:
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["ended_at"] = int(time.time())
        return

    target_ids = [t.get("id") for t in targets if t.get("id")]
    fresh_count = db_count_fresh_connections(target_ids)

    with _sync_lock:
        _sync_state["total"] = len(target_ids)
        _sync_state["completed"] = fresh_count
        _sync_state["errors"] = 0
        _sync_state["started_at"] = int(time.time())
        _sync_state["ended_at"] = 0
        _sync_state["last_target_name"] = ""

    threshold = int(time.time()) - CONNECTIONS_CACHE_TTL
    # Filter to targets that actually need sync (skip fresh, skip missing id)
    to_sync = []
    for t in targets:
        tid = t.get("id")
        if not tid:
            continue
        cached_data, fetched_at, cached_error = db_get_connections(tid)
        if cached_data is not None and fetched_at >= threshold and not cached_error:
            continue
        to_sync.append(t)

    # Concurrent fetches. ThreadPoolExecutor handles the worker pool;
    # _db_lock inside fetch_target_connections serializes writes.
    with ThreadPoolExecutor(max_workers=SYNC_CONCURRENCY) as pool:
        futures = [pool.submit(_sync_one_target, t) for t in to_sync]
        for _f in as_completed(futures):
            pass  # progress is tracked inside _sync_one_target

    with _sync_lock:
        _sync_state["running"] = False
        _sync_state["ended_at"] = int(time.time())


def start_sync(force_all=False):
    """Kick off the background sync if it's not already running.

    If force_all=True, marks all current rows as stale by passing through (the worker
    re-fetches anything older than CONNECTIONS_CACHE_TTL — to fully re-sync we'd need
    to truncate; for now use the TTL knob).
    """
    global _sync_thread
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _sync_state["running"] = True
    _sync_thread = threading.Thread(target=_sync_worker, daemon=True, name="db-sync")
    _sync_thread.start()
    return True


def maybe_auto_start_sync(targets):
    """Auto-start sync if we don't have full coverage and we're not already running."""
    with _sync_lock:
        if _sync_state["running"]:
            return
    if not targets:
        return
    target_ids = [t.get("id") for t in targets if t.get("id")]
    fresh = db_count_fresh_connections(target_ids)
    if fresh < len(target_ids):
        start_sync()


def sync_progress_snapshot():
    """Return a copy of the sync state for JSON serialization."""
    with _sync_lock:
        snap = dict(_sync_state)
    if snap["total"] > 0:
        snap["percent"] = int(100 * snap["completed"] / snap["total"])
    else:
        snap["percent"] = 0
    return snap


_OVERLAP_RE = re.compile(
    r"^They overlapped for (.+?) @ ([^,]+?)(?:\s+in\s+([^,]+))?(?:,\s*(.*))?$",
    re.IGNORECASE,
)
_DURATION_WRAPPER_RE = re.compile(
    r"^a (?:long time|little while|short time|short period)\s*\((.+)\)$", re.IGNORECASE
)
_MUTUALS_RE = re.compile(
    r"^They have (?:a (?:lot|good number)\s*\()?(\d+)\)?\s*(?:of\s+)?mutual connections",
    re.IGNORECASE,
)
_BOTH_WORKED_RE = re.compile(
    r"^They both worked @ (.+?)(?:\s*\(([^)]+)\))?$", re.IGNORECASE
)
_SCHOOL_RE = re.compile(r"^They went to (.+?) together", re.IGNORECASE)
_YEAR_RE = re.compile(r"most recently in (\d{4})", re.IGNORECASE)


def _humanize_score_detail(detail, target_first):
    """Rewrite an API scoreDetail string from third-person ('they') to second-person
    ('you'), from the user's POV writing to the connector.

    Examples:
      'They overlapped for a long time (26 months) @ Microsoft, most recently in 2009'
        -> 'you worked with Bogdan at Microsoft for 26 months, most recently in 2009'
      'They have a good number (15) of mutual connections'
        -> 'you have 15 mutual connections'
      'They have a lot (590) of mutual connections'
        -> 'you have 590 mutual connections'
      'They went to Stanford together'
        -> 'you both went to Stanford'
      'They both worked @ Acme (but didn't overlap)'
        -> 'you both worked at Acme (but didn't overlap)'
    """
    if not detail:
        return ""
    d = detail.strip()
    target_first = target_first or "them"

    m = _MUTUALS_RE.match(d)
    if m:
        return f"you have {m.group(1)} mutual connections"

    m = _OVERLAP_RE.match(d)
    if m:
        duration = m.group(1).strip()
        wrap = _DURATION_WRAPPER_RE.match(duration)
        if wrap:
            duration = wrap.group(1).strip()
        company = m.group(2).strip()
        rest = m.group(4) or ""
        result = f"you worked with {target_first} at {company}"
        if duration and duration.lower() not in ("a little while", "a long time"):
            result += f" for {duration}"
        ym = _YEAR_RE.search(rest)
        if ym:
            result += f", most recently in {ym.group(1)}"
        return result

    m = _SCHOOL_RE.match(d)
    if m:
        return f"you both went to {m.group(1).strip()}"

    m = _BOTH_WORKED_RE.match(d)
    if m:
        company = m.group(1).strip()
        note = f" ({m.group(2)})" if m.group(2) else ""
        return f"you both worked at {company}{note}"

    # Fallback: simple pronoun substitution so we never leak raw "They"
    rewrite = re.sub(r"\bThey\b", "you", d)
    rewrite = re.sub(r"\bthey\b", "you", rewrite)
    return rewrite


def _build_messages(target, connection):
    """Generate friendly intro request drafts in three forms:
      - plain: what the drawer displays (and the Copy button copies)
      - html:  rich-text version for clipboard with the target's first name
               wrapped in <a href> to their LinkedIn
      - plain_fallback: plain + a LinkedIn URL footer line (used when
               clipboard isn't available, so the recipient still gets the URL)

    Tone notes (from user feedback):
      - Use plain hyphen ('-'), never em-dash
      - Never quote exact mutual-connection counts ("a bunch of mutuals")
      - Don't lead a message with the mutuals line ("Hey X - you have 395
        mutuals" reads awkward); reword as "saw that you have a bunch of
        mutuals with {target}"
      - Lead with the strongest signal: work overlap > school > both worked
      - Append mutuals as a secondary clause ("and that you have a bunch
        of mutuals") only when there's also a primary signal
    """
    target_first = (target.get("firstName") or "").strip() or "them"
    target_last = (target.get("lastName") or "").strip()
    target_full = f"{target_first} {target_last}".strip() if target_last else target_first
    target_linkedin = target.get("linkedinUrl") or ""
    target_company = (target.get("position") or {}).get("companyName") or ""
    connector_first = (connection.get("firstName") or "").strip() or "there"

    raw_details = connection.get("scoreDetails") or []
    work_clause = None
    mutuals_present = False
    school_clause = None
    other_clause = None

    for d in raw_details:
        m = _OVERLAP_RE.match(d)
        if m and not work_clause:
            company = m.group(2).strip()
            rest = m.group(4) or ""
            ym = _YEAR_RE.search(rest)
            year = ym.group(1) if ym else None
            clause = f"you worked with {{TGT}} at {company}"
            if year:
                clause += f" in {year}"
            clause += " for a while"
            work_clause = clause
            continue
        if _MUTUALS_RE.match(d):
            mutuals_present = True
            continue
        m = _SCHOOL_RE.match(d)
        if m and not school_clause:
            school_clause = f"you both went to {m.group(1).strip()}"
            continue
        m = _BOTH_WORKED_RE.match(d)
        if m and not other_clause:
            company = m.group(1).strip()
            note = f" ({m.group(2)})" if m.group(2) else ""
            other_clause = f"you both worked at {company}{note}"

    if work_clause:
        detail = f"saw {work_clause}"
        if mutuals_present:
            detail += " and that you have a bunch of mutuals"
        elif school_clause:
            detail += f" and that {school_clause}"
        elif other_clause:
            detail += f" and that {other_clause}"
    elif mutuals_present:
        detail = "saw that you have a bunch of mutuals with {TGT}"
        if school_clause:
            detail += f" and that {school_clause}"
        elif other_clause:
            detail += f" and that {other_clause}"
    elif school_clause:
        detail = f"saw {school_clause}"
    elif other_clause:
        detail = f"saw {other_clause}"
    else:
        company_clause = f" at {target_company}" if target_company else ""
        detail = f"saw you're connected to {{TGT}}{company_clause}"

    template = (
        f"Hey {connector_first} - quick question: {detail}.\n\n"
        f"Any chance you'd be open to pinging {{TGT}} for a quick warm intro? "
        f"Happy to send a forwardable email."
    )

    plain = template.replace("{TGT}", target_first)

    # Build the HTML version. Escape the surrounding text first so the
    # connector's name etc. is safe, then substitute {TGT} with an <a> tag.
    if target_linkedin:
        link = (
            f'<a href="{html.escape(target_linkedin, quote=True)}">'
            f'{html.escape(target_first)}</a>'
        )
        # Escape the template (the literal "{TGT}" placeholder is safe — html.escape
        # only touches & < > " ' chars, not braces).
        escaped_template = html.escape(template)
        html_body = escaped_template.replace("{TGT}", link)
    else:
        html_body = html.escape(plain)
    # Convert newlines into paragraph breaks for nicer clipboard paste.
    html_body = "<p>" + html_body.replace("\n\n", "</p><p>").replace("\n", "<br>") + "</p>"

    plain_fallback = plain
    if target_linkedin:
        plain_fallback += f"\n\n({target_first}'s LinkedIn: {target_linkedin})"

    return {
        "plain": plain,
        "html": html_body,
        "plain_fallback": plain_fallback,
        "subject": f"Intro to {target_full}?",
    }


def _gmail_compose_url(subject, body):
    """Build a Gmail compose URL the user's browser can open in their logged-in account."""
    from urllib.parse import quote
    return (
        "https://mail.google.com/mail/?view=cm&fs=1"
        f"&su={quote(subject)}"
        f"&body={quote(body)}"
    )


def _enrich_connection(target, connection, requested_set=None):
    """Format a Connection for the drawer template.

    `requested_set` is an optional pre-fetched set of connection_ids that have
    been Marked-as-requested for this target — saves a per-connection DB query.

    Also generates the Compose-Email Gmail URL for the connector card.
    """
    pos = connection.get("position") or {}
    first = (connection.get("firstName") or "").strip()
    last = (connection.get("lastName") or "").strip()
    cid = connection.get("id")
    if requested_set is not None:
        is_requested = cid in requested_set
    else:
        is_requested = db_intro_request_get(target.get("id"), cid)

    # Humanize scoreDetails into 2nd-person bullets the user can read in the drawer.
    # (Bullets keep precise data like exact mutual counts — drafts don't.)
    raw_details = connection.get("scoreDetails") or []
    humanized = [_humanize_score_detail(d, (target.get("firstName") or "").strip() or "them")
                 for d in raw_details]
    humanized = [h for h in humanized if h]

    # Plain + HTML message variants for the drawer + Compose-email button.
    messages = _build_messages(target, connection)
    # Fallback Gmail URL (used if clipboard write fails) — uses the plain_fallback
    # body which includes the LinkedIn URL inline.
    gmail_url = _gmail_compose_url(messages["subject"], messages["plain_fallback"])

    return {
        "id": cid,
        "name": f"{first} {last}".strip() or "(no name)",
        "initials": _initials(first, last),
        "linkedinUrl": connection.get("linkedinUrl") or "",
        "title": pos.get("title") or "",
        "company": pos.get("companyName") or "",
        "score": connection.get("score") or 0,
        "score_details": connection.get("scoreDetails") or [],
        "humanized_details": humanized,
        "owners": connection.get("owners") or [],
        "draft_message": messages["plain"],
        "draft_html": messages["html"],
        "draft_plain_fallback": messages["plain_fallback"],
        "compose_subject": messages["subject"],
        "gmail_url": gmail_url,
        "requested": is_requested,
    }


_LI_RE = re.compile(r"linkedin\.com/in/[^\s,/?#]+", re.IGNORECASE)


def normalize_linkedin_urls(raw: str):
    """Accept comma- or newline-separated input. Normalize each entry to https://www.linkedin.com/in/<slug>."""
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[\n,]+", raw) if p.strip()]
    out = []
    seen = set()
    for p in parts:
        m = _LI_RE.search(p)
        if not m:
            continue
        match = m.group(0)
        slug = match.split("/in/", 1)[1].rstrip("/")
        normalized = f"https://www.linkedin.com/in/{slug}"
        if normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _initials(first: str, last: str) -> str:
    a = (first or "").strip()
    b = (last or "").strip()
    return ((a[:1] or "?") + (b[:1] or "")).upper()


TARGETS_PAGE_SIZE = 100
ACCOUNTS_PAGE_SIZE = 50


def _matches_query(t, q_lower):
    """Substring match across name, title, company, tags. Case-insensitive."""
    haystack = " ".join([
        t.get("firstName") or "",
        t.get("lastName") or "",
        (t.get("position") or {}).get("title") or "",
        (t.get("position") or {}).get("companyName") or "",
        " ".join(t.get("tags") or []),
    ]).lower()
    return q_lower in haystack


def _enrich_target(t):
    position = t.get("position") or {}
    return {
        "id": t.get("id"),
        "name": f"{t.get('firstName') or ''} {t.get('lastName') or ''}".strip() or "(no name)",
        "initials": _initials(t.get("firstName"), t.get("lastName")),
        "title": position.get("title") or "",
        "company": position.get("companyName") or "",
        "linkedinUrl": t.get("linkedinUrl") or "",
        "score": t.get("score") or 0,
        "connections_number": t.get("connectionsNumber") or 0,
        "tags": t.get("tags") or [],
        "status": t.get("status") or "",
        "updated_at": t.get("updatedAt") or "",
    }


@app.route("/", methods=["GET"])
def targets_view():
    force_refresh = request.args.get("refresh") == "1"
    targets, error = fetch_all_targets(force=force_refresh)

    # Search filter (case-insensitive substring across name/title/company/tags)
    q = (request.args.get("q") or "").strip()
    q_lower = q.lower()
    if q_lower:
        targets = [t for t in targets if _matches_query(t, q_lower)]

    # Sort by score desc
    targets.sort(key=lambda t: (t.get("score") or 0), reverse=True)

    # Pagination — 100 per page
    total = len(targets)
    total_pages = max(1, (total + TARGETS_PAGE_SIZE - 1) // TARGETS_PAGE_SIZE)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * TARGETS_PAGE_SIZE
    end = start + TARGETS_PAGE_SIZE
    page_targets = targets[start:end]

    enriched = [_enrich_target(t) for t in page_targets]

    # Auto-start background sync of per-target connections if we don't have full
    # coverage yet. Non-blocking — runs in a daemon thread.
    maybe_auto_start_sync(targets)

    return render_template(
        "targets.html",
        targets=enriched,
        total_count=total,
        showing_start=start + 1 if total else 0,
        showing_end=min(end, total),
        current_page=page,
        total_pages=total_pages,
        query=q,
        error=error,
        api_key_set=bool(API_KEY),
        active="targets",
        cache_age=cache_age_seconds(),
    )


@app.route("/accounts", methods=["GET"])
def accounts_view():
    force_refresh = request.args.get("refresh") == "1"
    targets, error = fetch_all_targets(force=force_refresh)

    # Group by company name (case-insensitive key, preserve display case)
    accounts = {}
    for t in targets:
        position = t.get("position") or {}
        company = (position.get("companyName") or "").strip() or "(unknown company)"
        key = company.lower()
        if key not in accounts:
            accounts[key] = {
                "name": company,
                "company_linkedin": position.get("companyLinkedinUrl") or "",
                "targets": [],
                "max_score": 0,
                "total_paths": 0,
            }
        accounts[key]["targets"].append(_enrich_target(t))
        score = t.get("score") or 0
        if score > accounts[key]["max_score"]:
            accounts[key]["max_score"] = score
        accounts[key]["total_paths"] += t.get("connectionsNumber") or 0

    # Optional search filter (company name only)
    q = (request.args.get("q") or "").strip()
    q_lower = q.lower()
    sorted_accounts = list(accounts.values())
    if q_lower:
        sorted_accounts = [a for a in sorted_accounts if q_lower in a["name"].lower()]

    # Sort: max_score desc, then target count desc
    sorted_accounts.sort(
        key=lambda a: (a["max_score"], len(a["targets"])),
        reverse=True,
    )

    # Pagination — 50 per page (accounts are taller cards)
    total = len(sorted_accounts)
    total_pages = max(1, (total + ACCOUNTS_PAGE_SIZE - 1) // ACCOUNTS_PAGE_SIZE)
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * ACCOUNTS_PAGE_SIZE
    end = start + ACCOUNTS_PAGE_SIZE
    page_accounts = sorted_accounts[start:end]

    # Sort each account's targets by score desc for nicer display when expanded
    for a in page_accounts:
        a["targets"].sort(key=lambda x: x["score"], reverse=True)

    # Auto-start background sync of per-target connections.
    maybe_auto_start_sync(targets)

    return render_template(
        "accounts.html",
        accounts=page_accounts,
        total_count=total,
        showing_start=start + 1 if total else 0,
        showing_end=min(end, total),
        current_page=page,
        total_pages=total_pages,
        query=q,
        error=error,
        api_key_set=bool(API_KEY),
        active="accounts",
        cache_age=cache_age_seconds(),
    )


@app.route("/import", methods=["GET"])
def import_form():
    tags, tag_error = fetch_tags()
    return render_template(
        "import.html",
        suggested_tags=tags,
        tag_error=tag_error,
        api_key_set=bool(API_KEY),
        result=None,
        active="import",
    )


@app.route("/import", methods=["POST"])
def do_import():
    raw_urls = request.form.get("linkedin_urls", "")
    raw_tags = request.form.get("tags", "")
    no_tags = request.form.get("no_tags") == "on"

    urls = normalize_linkedin_urls(raw_urls)
    tags = []
    if not no_tags and raw_tags.strip():
        tags = [t.strip() for t in re.split(r"[\n,]+", raw_tags) if t.strip()]

    result = {
        "submitted_count": len(urls),
        "urls": urls,
        "tags_used": tags,
        "warning": None,
        "imported": None,
        "notImported": None,
        "errors": [],
        "ok": False,
        "raw_response": None,
    }

    if not urls:
        result["errors"] = ["No valid LinkedIn URLs found in input."]
    elif not API_KEY:
        result["errors"] = [
            "DRAFTBOARD_API_KEY not set. Set it in your shell and restart the server."
        ]
    else:
        if len(urls) > 100:
            result["warning"] = (
                "Heads up: Core plans cap at ~300 active targets/month, "
                "Growth at ~1,000. Check Settings -> Billing in Draftboard if unsure."
            )
        try:
            payload = {"linkedinUrls": urls}
            if tags:
                payload["tags"] = tags
            r = requests.post(
                f"{API_BASE}/targets/import",
                headers=_auth_headers(),
                json=payload,
                timeout=30,
            )
            try:
                data = r.json()
            except Exception:
                data = {"raw": r.text}
            result["raw_response"] = data
            if r.status_code == 200:
                result["ok"] = True
                result["imported"] = data.get("imported")
                result["notImported"] = data.get("notImported")
                result["errors"] = data.get("errors") or []
            else:
                result["errors"] = [
                    f"POST /targets/import returned {r.status_code}: {data}"
                ]
        except requests.RequestException as e:
            result["errors"] = [f"Network error: {e}"]

    suggested_tags, tag_error = fetch_tags()
    return render_template(
        "import.html",
        suggested_tags=suggested_tags,
        tag_error=tag_error,
        api_key_set=bool(API_KEY),
        result=result,
        prev_urls=raw_urls,
        prev_tags=raw_tags,
        active="import",
    )


@app.route("/tags", methods=["GET"])
def tags_json():
    """Convenience: GET /tags as JSON."""
    tags, err = fetch_tags()
    return jsonify({"tags": tags, "error": err})


@app.route("/target/<target_id>/drawer", methods=["GET"])
def target_drawer(target_id):
    """HTML fragment: target detail drawer with paths to this target."""
    targets, _ = fetch_all_targets()
    target = next((t for t in targets if t.get("id") == target_id), None)
    if not target:
        return ("<div class='p-6 text-rose-700'>Target not found in your workspace cache. "
                "Try clicking Refresh on the Targets page.</div>"), 404

    connections, error = fetch_target_connections(target_id)
    connections.sort(key=lambda c: c.get("score") or 0, reverse=True)
    requested_set = db_intro_requests_for_target(target_id)
    enriched_conns = [_enrich_connection(target, c, requested_set) for c in connections]

    return render_template(
        "_drawer_target.html",
        target=_enrich_target(target),
        target_id=target_id,
        connections=enriched_conns,
        error=error,
    )


def _connectors_panel_for_target(target):
    """Build the data structure for the right-column connectors panel for one target."""
    target_id = target.get("id")
    connections, error = fetch_target_connections(target_id)
    connections.sort(key=lambda c: c.get("score") or 0, reverse=True)
    requested_set = db_intro_requests_for_target(target_id)
    enriched = [_enrich_connection(target, c, requested_set) for c in connections]
    return {
        "target": _enrich_target(target),
        "target_id": target_id,
        "connections": enriched,
        "error": error,
    }


@app.route("/account/<path:account_key>/drawer", methods=["GET"])
def account_drawer(account_key):
    """HTML fragment: account detail drawer with two columns — Targets on left,
    Mutual Connections (for the selected target) on right.

    By default the highest-scoring target is selected. The right column updates
    via JS when the user clicks a different target on the left (no full
    drawer reload).
    """
    targets_all, _ = fetch_all_targets()
    key_lower = account_key.lower()

    matching = []
    for t in targets_all:
        company = ((t.get("position") or {}).get("companyName") or "").strip()
        if not company:
            company = "(unknown company)"
        if company.lower() == key_lower:
            matching.append(t)

    if not matching:
        return ("<div class='p-6 text-rose-700'>No targets found for this account in the cache.</div>"), 404

    matching.sort(key=lambda t: t.get("score") or 0, reverse=True)

    # Pre-load the highest-scoring target's connectors for the initial right column.
    initial_panel = _connectors_panel_for_target(matching[0])
    enriched_targets = [_enrich_target(t) for t in matching]
    selected_id = matching[0].get("id")

    display_name = ((matching[0].get("position") or {}).get("companyName") or "").strip() or "(unknown company)"

    return render_template(
        "_drawer_account.html",
        account_name=display_name,
        targets=enriched_targets,
        selected_target_id=selected_id,
        initial_panel=initial_panel,
        total_target_count=len(matching),
    )


@app.route("/target/<target_id>/connectors-panel", methods=["GET"])
def target_connectors_panel(target_id):
    """HTML fragment for just the right-column 'Mutual Connections' panel.

    Used by the account drawer's JS to swap the right column when the user
    clicks a different target on the left.
    """
    targets, _ = fetch_all_targets()
    target = next((t for t in targets if t.get("id") == target_id), None)
    if not target:
        return ("<div class='p-6 text-rose-700'>Target not found.</div>"), 404

    panel = _connectors_panel_for_target(target)
    return render_template("_connectors_panel.html", panel=panel)


@app.route("/intro_requests/toggle", methods=["POST"])
def toggle_intro_request():
    """Toggle SQLite-persisted 'Mark as requested' state for a (target, connection) pair."""
    data = request.get_json(silent=True) or {}
    target_id = data.get("target_id")
    connection_id = data.get("connection_id")
    if not target_id or not connection_id:
        return jsonify({"error": "missing target_id or connection_id"}), 400
    new_state = db_intro_request_toggle(target_id, connection_id)
    return jsonify({"requested": new_state})


@app.route("/sync/status", methods=["GET"])
def sync_status():
    """JSON snapshot of background sync state. Polled by the nav status pill."""
    return jsonify(sync_progress_snapshot())


@app.route("/sync/start", methods=["POST"])
def sync_start():
    """Manually trigger a sync. Idempotent if one is already running."""
    started = start_sync()
    return jsonify({"started": started, "state": sync_progress_snapshot()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
