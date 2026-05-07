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

from linkedin_resolver import resolve_linkedin, is_cacheable as _resolver_is_cacheable

API_BASE = "https://intros.draftboard.com/api/v1/integration"

# Where the LinkedIn resolver wizard saves the customer's Apollo / Google CSE /
# OpenAI keys when env vars aren't set. Mirrors the storage pattern used for
# the Draftboard API key (~/.draftboard-secrets/draftboard-api-starter).
RESOLVER_SECRETS_PATH = os.path.expanduser(
    "~/.draftboard-secrets/draftboard-api-starter-resolver.json"
)
RESOLVER_KEY_NAMES = ("apollo_api_key", "google_cse_api_key", "google_cse_id", "openai_api_key")
RESOLVER_ENV_MAP = {
    "apollo_api_key":     "APOLLO_API_KEY",
    "google_cse_api_key": "GOOGLE_CSE_API_KEY",
    "google_cse_id":      "GOOGLE_CSE_ID",
    "openai_api_key":     "OPENAI_API_KEY",
}
# Cache hits within this window skip re-paying for Apollo/CSE/OpenAI calls.
RESOLUTION_CACHE_TTL = 30 * 24 * 3600  # 30 days


def _load_api_key():
    """Load the Draftboard API key, in priority order:

      1. DRAFTBOARD_API_KEY environment variable
      2. .env file in the app directory (KEY=value, KEY=value lines)
      3. ~/.draftboard-secrets/draftboard-api-starter (raw key, or
         DRAFTBOARD_API_KEY=...)

    Returns (key_string, source_label). Source label is shown at startup so
    the user knows where the key came from.
    """
    # 1. Env var
    env_key = os.environ.get("DRAFTBOARD_API_KEY", "").strip()
    if env_key:
        return env_key, "DRAFTBOARD_API_KEY env var"

    def _extract(line):
        """Pull a value out of a 'KEY=value' or just-value line."""
        line = line.strip()
        if not line or line.startswith("#"):
            return None
        if line.startswith("DRAFTBOARD_API_KEY"):
            _, _, val = line.partition("=")
            return val.strip().strip('"').strip("'") or None
        return line.strip().strip('"').strip("'") or None

    # 2. .env in app dir
    app_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(app_dir, ".env")
    if os.path.exists(env_path):
        try:
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith("DRAFTBOARD_API_KEY"):
                        v = _extract(line)
                        if v:
                            return v, env_path
        except OSError:
            pass

    # 3. ~/.draftboard-secrets/draftboard-api-starter
    secrets_path = os.path.expanduser("~/.draftboard-secrets/draftboard-api-starter")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path) as f:
                content = f.read().strip()
            if content:
                # First non-comment line — raw key or KEY=value
                for line in content.splitlines():
                    v = _extract(line)
                    if v:
                        return v, secrets_path
        except OSError:
            pass

    return "", None


def _load_resolver_keys() -> dict:
    """Load the LinkedIn-resolver API keys, preferring env vars over the
    secrets file. Returns a dict with all four `RESOLVER_KEY_NAMES`; missing
    keys are empty strings.

    Priority per key:
      1. Environment variable (APOLLO_API_KEY etc.)
      2. ~/.draftboard-secrets/draftboard-api-starter-resolver.json
    """
    keys = {name: "" for name in RESOLVER_KEY_NAMES}
    file_data = {}
    if os.path.exists(RESOLVER_SECRETS_PATH):
        try:
            with open(RESOLVER_SECRETS_PATH) as f:
                file_data = json.load(f) or {}
        except (OSError, ValueError):
            file_data = {}
    for name in RESOLVER_KEY_NAMES:
        env_val = os.environ.get(RESOLVER_ENV_MAP[name], "").strip()
        if env_val:
            keys[name] = env_val
        else:
            keys[name] = (file_data.get(name) or "").strip()
    return keys


def _save_resolver_keys(updates: dict) -> None:
    """Merge `updates` into the resolver secrets JSON file. Empty-string values
    leave existing keys unchanged (use a literal "__clear__" sentinel to wipe
    a key). Creates the secrets dir if it doesn't exist."""
    existing = {}
    if os.path.exists(RESOLVER_SECRETS_PATH):
        try:
            with open(RESOLVER_SECRETS_PATH) as f:
                existing = json.load(f) or {}
        except (OSError, ValueError):
            existing = {}
    for name in RESOLVER_KEY_NAMES:
        if name not in updates:
            continue
        val = updates[name]
        if val == "__clear__":
            existing.pop(name, None)
        elif val:
            existing[name] = val
        # else: empty string — leave existing untouched

    secrets_dir = os.path.dirname(RESOLVER_SECRETS_PATH)
    # mode= only takes effect when the dir is *created* (not when it already
    # exists), but on first run this prevents the secrets dir from being made
    # group/world-readable.
    os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
    # Write to a temp file then rename (atomic on POSIX) so a crash mid-write
    # can't truncate the file. Open with mode=0o600 directly via os.open so the
    # file is owner-only from the moment it exists — a separate chmod after
    # write leaves a microsecond window where the file is world-readable.
    tmp = RESOLVER_SECRETS_PATH + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, RESOLVER_SECRETS_PATH)


API_KEY, _api_key_source = _load_api_key()
if API_KEY:
    print(f"[draftboard-starter] Loaded API key from: {_api_key_source}")
else:
    print("[draftboard-starter] No API key found. Set one of:")
    print("  - DRAFTBOARD_API_KEY environment variable")
    print("  - .env file in the app directory (DRAFTBOARD_API_KEY=db-api_...)")
    print("  - ~/.draftboard-secrets/draftboard-api-starter (file containing the key)")
    print("  Then restart the server.")
TARGETS_CACHE_TTL = 300  # 5 minutes; Refresh button forces a re-fetch
TAGS_CACHE_TTL = 600  # 10 minutes
CONNECTIONS_CACHE_TTL = 24 * 3600  # 24 hours; SQLite-persisted connections
ACCOUNT_FANOUT_LIMIT = 50  # max targets to fan out (mostly cache hits after sync)
# Sync politeness — concurrency dropped from 5→2 + per-request delay added
# after Draftboard's eng team flagged that bursting 5 parallel calls hit the
# API too hard. Net rate ~3-4 req/sec instead of 5+.
SYNC_CONCURRENCY = int(os.environ.get("SYNC_CONCURRENCY", "2"))
SYNC_DELAY_SEC = float(os.environ.get("SYNC_DELAY_SEC", "0.3"))
SYNC_INTERVAL_HOURS = float(os.environ.get("SYNC_INTERVAL_HOURS", "12"))  # background re-sync cadence
# AUTO_SYNC_ENABLED gates BOTH the scheduled daemon AND the on-page-load
# auto-trigger. Set to "false"/"0" to fully disable polling for read-only
# testing on already-cached data. Manual /sync/start still works.
AUTO_SYNC_ENABLED = os.environ.get("AUTO_SYNC_ENABLED", "true").strip().lower() not in ("false", "0", "no", "off")

app = Flask(__name__)
# Cap request bodies at 2 MiB. Without this, a malicious or malformed payload
# to /candidates/resolve/batch (or any other endpoint) can balloon the
# customer's laptop process. The kit's normal request bodies are tiny
# (form posts, small JSON), so 2 MiB is generous.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# Per-field input caps for resolve endpoints. Apollo / CSE / OpenAI all reject
# huge values, but we cap before the network call so the customer's process
# can't be DOS'd by a payload full of multi-MB strings.
RESOLVE_NAME_MAX_LEN = 256
RESOLVE_EMAIL_MAX_LEN = 320  # RFC 5321 max email length


def _load_or_create_local_secret(filename, length=32):
    """Read or generate a per-install secret stored in ~/.draftboard-secrets/.

    Used for the Flask session cookie (which carries the Google OAuth `state`
    nonce across the consent redirect). All local — never leaves the machine.
    Files are 600-permissioned on creation.
    """
    secrets_dir = os.path.expanduser("~/.draftboard-secrets")
    path = os.path.join(secrets_dir, filename)
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                data = f.read().strip()
            if data:
                return data
        except OSError:
            pass
    try:
        os.makedirs(secrets_dir, exist_ok=True)
        data = os.urandom(length)
        with open(path, "wb") as f:
            f.write(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data
    except OSError:
        # If we can't write to ~/.draftboard-secrets, fall back to in-memory.
        # The Flask session will reset on every restart and Google OAuth tokens
        # won't persist across restarts — but the app still works mid-session.
        return os.urandom(length)


app.secret_key = _load_or_create_local_secret("flask_session_secret")

# In-memory caches for the lightweight endpoints (targets list, tags list, /me).
_targets_cache = {"data": None, "error": None, "fetched_at": 0}
_tags_cache = {"data": None, "error": None, "fetched_at": 0}
_me_cache = {"data": None, "error": None, "fetched_at": 0}
ME_CACHE_TTL = 600

# SQLite-backed persistent caches: per-target connections + intro_requests state.
# DB_PATH override exists so tests can point at a throwaway file (e.g.
# /tmp/test.db) without ever touching the production data.db. Default is
# data.db next to this file. NEVER hardcode a test path here.
DB_PATH = os.environ.get("DRAFTBOARD_DB_PATH") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
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
_scheduled_thread = None


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
        # Denormalized index: which team-member owners can intro to which target.
        # Populated alongside `connections` in db_put_connections. Lets us filter
        # the Targets/Accounts views by owner without scanning every JSON blob.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_owners (
                target_id TEXT NOT NULL,
                owner_id TEXT NOT NULL,
                owner_first TEXT NOT NULL,
                owner_last TEXT NOT NULL,
                owner_linkedin TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (target_id, owner_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_owners_owner ON target_owners(owner_id)")
        # Add owner_linkedin column to existing tables created before this migration
        try:
            conn.execute("ALTER TABLE target_owners ADD COLUMN owner_linkedin TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # already exists

        # Per-path discovery log. One row per (target_id, connection_id) pair.
        # `first_seen_at` is set on insert; `last_seen_at` is bumped on every sync
        # that re-confirms the pair. Powers the "New paths" tab. Intentionally
        # NOT backfilled from existing connections — only new paths discovered
        # after this table starts being populated will appear in the tab.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS discovered_paths (
                target_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                score INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                PRIMARY KEY (target_id, connection_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_paths_first_seen ON discovered_paths(first_seen_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_discovered_paths_score ON discovered_paths(score DESC)")

        # Simple persistent key-value app state (last sync completion, etc.)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        # Persistent cache of /targets metadata. Until this table has data, the
        # Targets/Accounts/New-paths views can't render (they need names, titles,
        # companies). One successful fetch_all_targets() call populates it; from
        # then on the app can run fully offline (AUTO_SYNC_ENABLED=false) without
        # any API calls.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS targets_cache (
                target_id TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                fetched_at INTEGER NOT NULL
            )
        """)

        # Connector-first index: one row per (connector_key, target_id) pair.
        # The Draftboard API mints a fresh `connection_id` for every
        # (connector, target) combo — so to group "the same person across many
        # targets" we use a stable `connector_key` (normalized LinkedIn URL,
        # falling back to "name:firstname-lastname" when LinkedIn is missing).
        # Migration first: if an older shape exists (no connector_key column),
        # drop it so the CREATE TABLE below builds the new shape and the
        # backfill repopulates from existing `connections` rows.
        try:
            cur = conn.execute("PRAGMA table_info(connector_paths)")
            cols = {r[1] for r in cur.fetchall()}
            if cols and "connector_key" not in cols:
                conn.execute("DROP TABLE connector_paths")
        except sqlite3.OperationalError:
            pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS connector_paths (
                connector_key TEXT NOT NULL,
                target_id TEXT NOT NULL,
                connection_id TEXT NOT NULL,
                connector_first TEXT NOT NULL DEFAULT '',
                connector_last TEXT NOT NULL DEFAULT '',
                connector_linkedin TEXT NOT NULL DEFAULT '',
                connector_title TEXT NOT NULL DEFAULT '',
                connector_company TEXT NOT NULL DEFAULT '',
                score INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (connector_key, target_id)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_connector_paths_key ON connector_paths(connector_key)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_connector_paths_score ON connector_paths(score DESC)")

        # --- Google Workspace integration tables ---
        # One-time scoring use case — the user clicks Connect Google, we run a
        # single sync, populate the candidate tables, throw the access token
        # away. No refresh tokens stored (avoids the 7-day testing-mode token
        # expiry and the encryption ceremony around it). Re-syncing is just
        # a fresh OAuth consent flow.
        #
        # Sync timing + last-account info live in the existing `app_state` K/V
        # table under keys `google_account_email` and `google_last_synced_at`.

        # Aggregated per-contact stats from the last 12 months of Gmail.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS gmail_contacts (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                emails_sent INTEGER NOT NULL DEFAULT 0,
                replies_received INTEGER NOT NULL DEFAULT 0,
                threads_count INTEGER NOT NULL DEFAULT 0,
                last_contact_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gmail_contacts_threads ON gmail_contacts(threads_count DESC)")

        # Aggregated per-contact stats from the last 12 months of Calendar.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS calendar_contacts (
                email TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                meetings_count INTEGER NOT NULL DEFAULT 0,
                last_met_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_calendar_contacts_meetings ON calendar_contacts(meetings_count DESC)")

        # Imported teammate scans — populated by /supporters/import-teammate.
        # Each row carries the contributor (whose Gmail/Calendar this came from)
        # so the candidates page can badge "From <teammate>". Composite primary
        # key on (contributor_email, email) means re-importing from the same
        # teammate UPDATEs in place; importing from a different teammate adds
        # a parallel row even for the same contact (you might both know Alice).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS teammate_contacts (
                contributor_email TEXT NOT NULL,
                contributor_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL,
                name TEXT NOT NULL DEFAULT '',
                emails_sent INTEGER NOT NULL DEFAULT 0,
                replies_received INTEGER NOT NULL DEFAULT 0,
                threads_count INTEGER NOT NULL DEFAULT 0,
                meetings_count INTEGER NOT NULL DEFAULT 0,
                last_contact_at INTEGER NOT NULL DEFAULT 0,
                imported_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (contributor_email, email)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teammate_contacts_contributor ON teammate_contacts(contributor_email)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_teammate_contacts_email ON teammate_contacts(email)")

        # Per-candidate triage state (set by the user from the candidates page).
        # `status` is one of: 'starred', 'hidden', 'supporter', or absent.
        # Default view filters out 'hidden'. 'supporter' = "I've already added
        # this person as a Draftboard Supporter manually" (informational badge).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_status (
                email TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_status_status ON candidate_status(status)")

        # Migration: drop the older `oauth_tokens` + `google_sync_state` tables
        # from the BYO architecture. Their data was always per-install and
        # ephemeral, so dropping is safe.
        conn.execute("DROP TABLE IF EXISTS oauth_tokens")
        conn.execute("DROP TABLE IF EXISTS google_sync_state")

        # LinkedIn resolution cache. One row per email — when /candidates/resolve
        # finds a person's LinkedIn URL via Apollo or Google CSE, we cache the
        # answer here so subsequent calls don't re-pay for API credits.
        # `error` lets us cache "we tried and got nothing" to avoid retrying
        # hopeless lookups on every page render.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS linkedin_resolutions (
                email TEXT PRIMARY KEY,
                name TEXT,
                linkedin_url TEXT,
                full_name TEXT,
                confidence TEXT,
                source TEXT,
                reasoning TEXT,
                resolved_at INTEGER NOT NULL,
                error TEXT
            )
        """)
        conn.commit()


def _compute_connector_key(connection):
    """Stable identifier for a connector PERSON across all their (target, connection)
    pairs. Prefers normalized LinkedIn URL; falls back to a hashed name.

    Why we need this: Draftboard's API returns a fresh `connection_id` UUID for
    every (connector, target) pair, so the same person Cory Moelis appears with
    a different `id` for every target he can intro to. Grouping by `id` would
    list each pair as a separate "connector". Grouping by `connector_key`
    correctly clusters all of Cory's paths under one row in the Connections
    list view.
    """
    linkedin = (connection.get("linkedinUrl") or "").strip()
    if linkedin:
        norm = _normalize_linkedin(linkedin)  # lowercase, no trailing slash
        if norm:
            return f"li:{norm}"
    first = (connection.get("firstName") or "").strip().lower()
    last = (connection.get("lastName") or "").strip().lower()
    if first or last:
        # Replace whitespace with dashes so the key is URL-safe.
        return "name:" + re.sub(r"\s+", "-", f"{first}-{last}".strip("-"))
    # Last resort — use the connection_id itself (unique per pair, which means
    # this connector won't be deduped, but that's better than crashing).
    return f"id:{connection.get('id') or 'unknown'}"


def backfill_connector_paths():
    """Populate connector_paths from existing rows in `connections`.

    Always rebuilds when the table has rows missing connector_key (i.e. on
    schema migration). Unlike discovered_paths, this is a pure index so
    backfilling is fine — it's just rebuilding queryable structure from data
    we already have.
    """
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM connector_paths")
        existing = cur.fetchone()[0]
        if existing > 0:
            # Sanity: if every row has a connector_key already, skip. Otherwise rebuild.
            cur = conn.execute("SELECT COUNT(*) FROM connector_paths WHERE connector_key = ''")
            missing = cur.fetchone()[0]
            if missing == 0:
                return
        conn.execute("DELETE FROM connector_paths")
        cur = conn.execute("SELECT target_id, connections_json FROM connections WHERE error IS NULL")
        rows = cur.fetchall()
        for target_id, json_str in rows:
            try:
                conns = json.loads(json_str) or []
            except Exception:
                continue
            for c in conns:
                cid = c.get("id")
                if not cid:
                    continue
                key = _compute_connector_key(c)
                pos = c.get("position") or {}
                conn.execute(
                    "INSERT OR REPLACE INTO connector_paths "
                    "(connector_key, target_id, connection_id, connector_first, connector_last, "
                    " connector_linkedin, connector_title, connector_company, score) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        key, target_id, cid,
                        c.get("firstName") or "",
                        c.get("lastName") or "",
                        c.get("linkedinUrl") or "",
                        pos.get("title") or "",
                        pos.get("companyName") or "",
                        c.get("score") or 0,
                    ),
                )
        conn.commit()


def backfill_target_owners():
    """Populate target_owners from existing rows in `connections` (one-time on boot).

    Re-runs if any rows are missing the owner_linkedin field (from a pre-migration
    state). Cheap on a fresh DB; takes a few seconds on thousands of cached targets.
    """
    with _db_lock, _db_connect() as conn:
        # Check if backfill is needed: empty table OR any row with empty linkedin
        cur = conn.execute("SELECT COUNT(*) FROM target_owners WHERE owner_linkedin = ''")
        missing_linkedin = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM target_owners")
        total = cur.fetchone()[0]
        if total > 0 and missing_linkedin == 0:
            return  # already populated and migrated
        # Wipe and rebuild — simpler than diffing
        conn.execute("DELETE FROM target_owners")
        cur = conn.execute("SELECT target_id, connections_json FROM connections WHERE error IS NULL")
        rows = cur.fetchall()
        for target_id, json_str in rows:
            try:
                conns = json.loads(json_str) or []
            except Exception:
                continue
            for c in conns:
                for o in (c.get("owners") or []):
                    oid = o.get("id")
                    if not oid:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO target_owners (target_id, owner_id, owner_first, owner_last, owner_linkedin) VALUES (?, ?, ?, ?, ?)",
                        (target_id, oid, o.get("firstName") or "", o.get("lastName") or "", o.get("linkedinUrl") or "")
                    )
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
    """Persist a target's connections + maintain the target_owners index +
    record (target_id, connection_id) pairs in discovered_paths.

    Newly-seen pairs get first_seen_at = now (this powers the "New paths" tab).
    Already-seen pairs refresh last_seen_at and current score.
    """
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO connections (target_id, connections_json, fetched_at, error) VALUES (?, ?, ?, ?)",
            (target_id, json.dumps(connections or []), now, error),
        )
        # Refresh the owner index for this target.
        conn.execute("DELETE FROM target_owners WHERE target_id = ?", (target_id,))
        for c in (connections or []):
            for o in (c.get("owners") or []):
                oid = o.get("id")
                if not oid:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO target_owners (target_id, owner_id, owner_first, owner_last, owner_linkedin) VALUES (?, ?, ?, ?, ?)",
                    (target_id, oid, o.get("firstName") or "", o.get("lastName") or "", o.get("linkedinUrl") or "")
                )
        # Discovered paths: INSERT preserves first_seen_at for known pairs,
        # UPDATE refreshes last_seen_at + score for everything in this batch.
        for c in (connections or []):
            cid = c.get("id")
            if not cid:
                continue
            score = c.get("score") or 0
            conn.execute(
                "INSERT OR IGNORE INTO discovered_paths "
                "(target_id, connection_id, score, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (target_id, cid, score, now, now)
            )
            conn.execute(
                "UPDATE discovered_paths SET last_seen_at = ?, score = ? "
                "WHERE target_id = ? AND connection_id = ?",
                (now, score, target_id, cid)
            )

        # Connector-first index: refresh rows for this target. Delete-then-
        # insert is simpler than diffing.
        conn.execute("DELETE FROM connector_paths WHERE target_id = ?", (target_id,))
        for c in (connections or []):
            cid = c.get("id")
            if not cid:
                continue
            key = _compute_connector_key(c)
            pos = c.get("position") or {}
            conn.execute(
                "INSERT OR REPLACE INTO connector_paths "
                "(connector_key, target_id, connection_id, connector_first, connector_last, "
                " connector_linkedin, connector_title, connector_company, score) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    key, target_id, cid,
                    c.get("firstName") or "",
                    c.get("lastName") or "",
                    c.get("linkedinUrl") or "",
                    pos.get("title") or "",
                    pos.get("companyName") or "",
                    c.get("score") or 0,
                ),
            )
        conn.commit()


def db_query_new_paths(since_ts, min_score, owner_id=None, limit=200):
    """Return new paths (target_id, connection_id, score, timestamps) sorted by
    score desc, then first_seen_at desc."""
    args = [since_ts, min_score]
    sql = (
        "SELECT dp.target_id, dp.connection_id, dp.score, dp.first_seen_at, dp.last_seen_at "
        "FROM discovered_paths dp "
        "WHERE dp.first_seen_at >= ? AND dp.score >= ? "
    )
    if owner_id:
        sql += "AND EXISTS (SELECT 1 FROM target_owners ot WHERE ot.target_id = dp.target_id AND ot.owner_id = ?) "
        args.append(owner_id)
    sql += "ORDER BY dp.score DESC, dp.first_seen_at DESC LIMIT ?"
    args.append(limit)
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(sql, args)
        return [
            {
                "target_id": r[0],
                "connection_id": r[1],
                "score": r[2],
                "first_seen_at": r[3],
                "last_seen_at": r[4],
            }
            for r in cur.fetchall()
        ]


def db_list_connectors(query=None, limit=200, offset=0):
    """List unique connector PEOPLE (deduped by connector_key) who can intro to
    at least one target. Sorted by intro_count desc, top_score desc.

    `query` is a substring match against connector name + title + company.
    """
    args = []
    where_sql = ""
    if query:
        like = f"%{query.lower()}%"
        where_sql = (
            " WHERE LOWER(connector_first || ' ' || connector_last) LIKE ? "
            "    OR LOWER(connector_title) LIKE ? "
            "    OR LOWER(connector_company) LIKE ? "
        )
        args.extend([like, like, like])
    sql = (
        "SELECT connector_key, "
        "       MAX(connector_first) AS first, "
        "       MAX(connector_last) AS last, "
        "       MAX(connector_linkedin) AS linkedin, "
        "       MAX(connector_title) AS title, "
        "       MAX(connector_company) AS company, "
        "       COUNT(DISTINCT target_id) AS intro_count, "
        "       MAX(score) AS top_score "
        "FROM connector_paths "
        + where_sql +
        "GROUP BY connector_key "
        "ORDER BY intro_count DESC, top_score DESC "
        "LIMIT ? OFFSET ?"
    )
    args.extend([limit, offset])
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(sql, args)
        return [
            {
                "connector_key": r[0],
                "first": r[1] or "",
                "last": r[2] or "",
                "name": (f"{r[1] or ''} {r[2] or ''}").strip() or "(unnamed)",
                "linkedin": r[3] or "",
                "title": r[4] or "",
                "company": r[5] or "",
                "intro_count": r[6],
                "top_score": r[7],
            }
            for r in cur.fetchall()
        ]


def db_count_connectors(query=None):
    args = []
    where_sql = ""
    if query:
        like = f"%{query.lower()}%"
        where_sql = (
            " WHERE LOWER(connector_first || ' ' || connector_last) LIKE ? "
            "    OR LOWER(connector_title) LIKE ? "
            "    OR LOWER(connector_company) LIKE ? "
        )
        args.extend([like, like, like])
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT COUNT(DISTINCT connector_key) FROM connector_paths" + where_sql,
            args,
        )
        return cur.fetchone()[0]


def db_targets_for_connector(connector_key):
    """All (target_id, connection_id, score) tuples this connector PERSON can
    intro to. Sorted by score desc. `connection_id` is needed to find the
    matching record inside the per-target cached JSON when rendering the drawer.
    """
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT target_id, connection_id, score FROM connector_paths "
            "WHERE connector_key = ? ORDER BY score DESC",
            (connector_key,),
        )
        return [{"target_id": r[0], "connection_id": r[1], "score": r[2]} for r in cur.fetchall()]


def db_save_targets_cache(targets):
    """Persist the /targets list to SQLite. Idempotent — INSERT OR REPLACE per id."""
    if not targets:
        return
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        for t in targets:
            tid = t.get("id")
            if not tid:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO targets_cache (target_id, data_json, fetched_at) VALUES (?, ?, ?)",
                (tid, json.dumps(t), now),
            )
        conn.commit()


def db_load_targets_cache():
    """Read the persisted /targets list. Returns ([], None) when empty."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("SELECT data_json FROM targets_cache")
        rows = cur.fetchall()
    if not rows:
        return [], None
    try:
        return [json.loads(r[0]) for r in rows], None
    except Exception as e:
        return [], f"corrupt targets_cache: {e}"


def db_app_state_get(key, default=None):
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def db_app_state_set(key, value):
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), int(time.time()))
        )
        conn.commit()


def db_get_resolution(email: str, ttl_sec: int = RESOLUTION_CACHE_TTL):
    """Return a cached resolution dict for this email, or None if missing /
    expired. Email is normalized (lowercased) before lookup so 'Bogdan@X.com'
    and 'bogdan@x.com' share a row.

    Returned shape matches the fresh-resolve shape exactly (same keys,
    `cached: True`) so callers can index either branch without a shape check.
    """
    if not email:
        return None
    key = email.strip().lower()
    cutoff = int(time.time()) - ttl_sec
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT name, linkedin_url, full_name, confidence, source, reasoning, resolved_at, error "
            "FROM linkedin_resolutions WHERE email = ? AND resolved_at >= ?",
            (key, cutoff),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "email": key,
        "name": row[0],
        "linkedin_url": row[1],
        "full_name": row[2],
        "confidence": row[3],
        "source": row[4],
        "reasoning": row[5],
        "query": "",        # not persisted; present so the shape matches fresh
        "resolved_at": row[6],
        "error": row[7],
        "cached": True,
    }


def db_put_resolution(email: str, name: str, result: dict):
    """Persist a resolver result. Skips non-definitive results (transient API
    errors, "no keys configured") so the cache can't be poisoned with a stale
    no-match that survives the customer adding keys later. See
    `linkedin_resolver.is_cacheable` for the exact rule."""
    if not email:
        return
    if not _resolver_is_cacheable(result):
        return
    key = email.strip().lower()
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO linkedin_resolutions "
            "(email, name, linkedin_url, full_name, confidence, source, reasoning, resolved_at, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                key, name or "",
                result.get("linkedin_url"),
                result.get("full_name"),
                result.get("confidence") or "none",
                result.get("source") or "none",
                result.get("reasoning") or "",
                now,
                result.get("error"),
            ),
        )
        conn.commit()


def db_unique_owners():
    """All distinct team members who appear as a Connection.owner across the cache.

    Returns a list of dicts sorted by how many targets each can intro to (desc).
    """
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT owner_id, owner_first, owner_last, owner_linkedin, COUNT(DISTINCT target_id) "
            "FROM target_owners GROUP BY owner_id, owner_first, owner_last, owner_linkedin "
            "ORDER BY 5 DESC"
        )
        return [
            {
                "id": r[0],
                "first": r[1],
                "last": r[2],
                "linkedin": r[3] or "",
                "name": f"{r[1]} {r[2]}".strip() or "(unnamed)",
                "target_count": r[4],
            }
            for r in cur.fetchall()
        ]


def _normalize_linkedin(url):
    """Lowercase + strip trailing slash + strip query/anchor — for fuzzy URL match."""
    if not url:
        return ""
    u = url.strip().lower()
    u = u.split("?", 1)[0].split("#", 1)[0]
    return u.rstrip("/")


def db_owner_id_by_linkedin(linkedin_url):
    """Return the owner_id from target_owners that matches a LinkedIn URL.

    Used to map the /me user (whose user.id may not match the in-org member.id)
    to the actual owner_id used in connection.owners[]. Tolerant to trailing
    slash and case differences (Draftboard's /me sometimes returns the URL
    without the trailing slash that appears inside connection.owners[]).
    """
    norm = _normalize_linkedin(linkedin_url)
    if not norm:
        return None
    with _db_lock, _db_connect() as conn:
        # Compare normalized forms by stripping trailing slash on the DB side too.
        cur = conn.execute(
            "SELECT owner_id FROM target_owners "
            "WHERE LOWER(RTRIM(owner_linkedin, '/')) = ? LIMIT 1",
            (norm,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def db_target_ids_for_owner(owner_id):
    """Set of target_ids where this owner is on at least one Connection."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT DISTINCT target_id FROM target_owners WHERE owner_id = ?",
            (owner_id,),
        )
        return {r[0] for r in cur.fetchall()}


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


# Initialize DB on import + backfill the target_owners index from existing rows.
# (start_scheduled_sync() is called at the bottom of this module, after its
# definition — Python doesn't hoist.)
init_db()
backfill_target_owners()
backfill_connector_paths()


def _auth_headers():
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def fetch_me(force=False):
    """GET /me -> the customer + the API-key owner's user record.

    Persists to app_state for offline use. When AUTO_SYNC_ENABLED is false or
    the API key is missing, reads from SQLite (no API call).
    """
    now = time.time()
    if (
        not force
        and _me_cache["data"] is not None
        and (now - _me_cache["fetched_at"]) < ME_CACHE_TTL
    ):
        return _me_cache["data"], _me_cache["error"]

    def _from_sqlite():
        raw = db_app_state_get("me_data")
        if not raw:
            return None, None
        try:
            return json.loads(raw), None
        except Exception:
            return None, None

    if not AUTO_SYNC_ENABLED or not API_KEY:
        cached, _err = _from_sqlite()
        if cached:
            _me_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        if not API_KEY:
            return None, "DRAFTBOARD_API_KEY not set and no cached /me data."
        return None, "AUTO_SYNC_ENABLED=false and /me not yet cached."

    try:
        r = requests.get(f"{API_BASE}/me", headers=_auth_headers(), timeout=10)
        if r.status_code != 200:
            cached, _err = _from_sqlite()
            if cached:
                _me_cache.update({"data": cached, "error": None, "fetched_at": now})
                return cached, None
            err = f"GET /me returned {r.status_code}."
            _me_cache.update({"data": None, "error": err, "fetched_at": now})
            return None, err
        data = r.json() or {}
        customer = data.get("customer") or {}
        user = customer.get("user") or {}
        result = {
            "customer_id": customer.get("id"),
            "customer_name": customer.get("name"),
            "user_id": user.get("id"),
            "user_first": user.get("firstName") or "",
            "user_last": user.get("lastName") or "",
            "user_linkedin": user.get("linkedinUrl") or "",
        }
        db_app_state_set("me_data", json.dumps(result))
        _me_cache.update({"data": result, "error": None, "fetched_at": now})
        return result, None
    except requests.RequestException as e:
        cached, _err = _from_sqlite()
        if cached:
            _me_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        return None, f"Network error fetching /me: {e}"


def get_my_user_id():
    """Convenience: just the user.id, or None if /me hasn't been fetched."""
    me, _ = fetch_me()
    return (me or {}).get("user_id")


def get_my_owner_id():
    """The owner_id used in connection.owners[] that represents the current user.

    The /me endpoint returns user.id, which is *supposed* to match the owner_id
    used inside connections. In some workspaces those are different UUIDs
    (separate User vs Member entities). We try the user.id first; if it doesn't
    appear in target_owners, fall back to LinkedIn-URL match.
    """
    me, _ = fetch_me()
    if not me:
        return None
    user_id = me.get("user_id")
    if user_id:
        with _db_lock, _db_connect() as conn:
            cur = conn.execute(
                "SELECT 1 FROM target_owners WHERE owner_id = ? LIMIT 1",
                (user_id,),
            )
            if cur.fetchone():
                return user_id
    # Fall back to LinkedIn URL
    linkedin = me.get("user_linkedin")
    fallback = db_owner_id_by_linkedin(linkedin)
    if fallback:
        return fallback
    # Last resort: return user.id even if no owner row matches yet
    return user_id


def fetch_tags(force=False):
    """GET /tags -> list of tag titles. Persisted to app_state for offline use.

    Returns ([], err) when AUTO_SYNC_ENABLED=false and nothing's cached — the
    import page just shows no suggestion chips."""
    now = time.time()
    if (
        not force
        and _tags_cache["data"] is not None
        and (now - _tags_cache["fetched_at"]) < TAGS_CACHE_TTL
    ):
        return _tags_cache["data"], _tags_cache["error"]

    def _from_sqlite():
        raw = db_app_state_get("tags_data")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    if not AUTO_SYNC_ENABLED or not API_KEY:
        cached = _from_sqlite()
        if cached is not None:
            _tags_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        return [], "Tags not yet cached (offline mode)."

    try:
        r = requests.get(
            f"{API_BASE}/tags",
            headers=_auth_headers(),
            params={"resultPerPage": 200},
            timeout=10,
        )
        if r.status_code != 200:
            cached = _from_sqlite()
            if cached is not None:
                _tags_cache.update({"data": cached, "error": None, "fetched_at": now})
                return cached, None
            return [], f"GET /tags returned {r.status_code}."
        data = r.json()
        tags = [t.get("title") for t in (data.get("tags") or []) if t.get("title")]
        db_app_state_set("tags_data", json.dumps(tags))
        _tags_cache.update({"data": tags, "error": None, "fetched_at": now})
        return tags, None
    except requests.RequestException as e:
        cached = _from_sqlite()
        if cached is not None:
            _tags_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        return [], f"Network error fetching tags: {e}"


def fetch_all_targets(force=False):
    """Get the /targets list. Persists results to SQLite so the app can run
    fully offline once bootstrapped.

    Source priority:
      1. In-memory cache, if fresh (<5 min old)
      2. If AUTO_SYNC_ENABLED is false → SQLite only, no API call ever
      3. API call → on success, persist to SQLite + memory
      4. API failure → fall back to SQLite (stale-but-better-than-nothing)
    """
    now = time.time()
    if (
        not force
        and _targets_cache["data"] is not None
        and (now - _targets_cache["fetched_at"]) < TARGETS_CACHE_TTL
    ):
        return _targets_cache["data"], _targets_cache["error"]

    # Offline mode (or no key): read from SQLite, no API call.
    if not AUTO_SYNC_ENABLED or not API_KEY:
        sqlite_targets, sqlite_err = db_load_targets_cache()
        if sqlite_targets:
            _targets_cache.update({"data": sqlite_targets, "error": None, "fetched_at": now})
            return sqlite_targets, None
        if not API_KEY:
            return [], "No API key set and no cached target list in SQLite. Add a key OR populate targets_cache."
        # AUTO_SYNC_ENABLED is false but SQLite is empty.
        return [], (
            "AUTO_SYNC_ENABLED=false and targets_cache is empty. The app can't render "
            "Targets/Accounts/New-paths views without target metadata. Either set "
            "AUTO_SYNC_ENABLED=true once to bootstrap from /targets, or pre-populate the "
            "targets_cache table."
        )

    # Online mode — try the API, persist on success, fall back to SQLite on failure.
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
            if r.status_code != 200:
                # API failure — fall back to SQLite if we have anything cached.
                sqlite_targets, _ = db_load_targets_cache()
                if sqlite_targets:
                    _targets_cache.update({"data": sqlite_targets, "error": None, "fetched_at": now})
                    return sqlite_targets, f"API returned {r.status_code}; using cached target list from SQLite."
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
        # Persist the fresh list so we can survive future API outages.
        db_save_targets_cache(targets)
        _targets_cache.update({"data": targets, "error": None, "fetched_at": now})
        return targets, None
    except requests.RequestException as e:
        sqlite_targets, _ = db_load_targets_cache()
        if sqlite_targets:
            _targets_cache.update({"data": sqlite_targets, "error": None, "fetched_at": now})
            return sqlite_targets, f"Network error; using cached target list."
        return [], f"Network error fetching targets: {e}"


def cache_age_seconds():
    """How old (sec) is the cached targets data, or None if not cached."""
    if _targets_cache["fetched_at"] == 0:
        return None
    return int(time.time() - _targets_cache["fetched_at"])


def fetch_target_connections(target_id, force=False):
    """Paginate GET /targets/{id}/connections. SQLite-cached per-target.

    Returns (list, error_or_None). When `force=False`, returns the cached row
    if it's within CONNECTIONS_CACHE_TTL seconds. When AUTO_SYNC_ENABLED is
    false OR there's no API key, returns cached data even if stale (better
    than nothing) and never makes an API call.
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

    # Offline mode — never call the API. Return whatever we have, even stale.
    if not AUTO_SYNC_ENABLED or not API_KEY:
        if cached_data is not None:
            return cached_data, None
        return [], "Connections not cached for this target (offline mode)."

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
    the ThreadPoolExecutor in _sync_worker. Adds a small delay after each call
    to keep request rate friendly (Draftboard's eng team flagged bursting)."""
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
    if SYNC_DELAY_SEC > 0:
        time.sleep(SYNC_DELAY_SEC)


def _sync_worker(force_all=False):
    """Background worker: fetches and caches connections for every target in parallel.

    Skips targets already fresh in SQLite (unless force_all=True). Uses a
    ThreadPoolExecutor so multiple /targets/{id}/connections calls run
    concurrently — modest concurrency (SYNC_CONCURRENCY) keeps us friendly to
    the API while cutting sync time ~5x.
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
    # Filter to targets that actually need sync (skip fresh, skip missing id).
    # When force_all=True, sync every target regardless of cache freshness —
    # used by the "Force re-sync" button to populate discovered_paths from
    # all currently-cached connections.
    to_sync = []
    for t in targets:
        tid = t.get("id")
        if not tid:
            continue
        if not force_all:
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
    # Persist the completion timestamp for the "Since last sync" filter on the
    # New Paths page (and for any future "last synced X minutes ago" UI).
    db_app_state_set("last_sync_completed_at", int(time.time()))


def _scheduled_sync_loop():
    """Background daemon: kicks off start_sync() every SYNC_INTERVAL_HOURS.

    Skips if a sync is already running. Restarts naturally on server boot —
    state isn't persisted across restarts.
    """
    interval_seconds = max(60, int(SYNC_INTERVAL_HOURS * 3600))
    while True:
        time.sleep(interval_seconds)
        with _sync_lock:
            if _sync_state["running"]:
                continue
        start_sync()


def start_scheduled_sync():
    """Spawn the scheduled sync daemon. Idempotent (no-op if already started).

    Skipped entirely when AUTO_SYNC_ENABLED is false — useful for read-only
    testing on already-cached data."""
    global _scheduled_thread
    if not AUTO_SYNC_ENABLED:
        print("[draftboard-starter] AUTO_SYNC_ENABLED=false — scheduled sync daemon NOT started.")
        return
    if _scheduled_thread is not None and _scheduled_thread.is_alive():
        return
    _scheduled_thread = threading.Thread(target=_scheduled_sync_loop, daemon=True, name="scheduled-sync")
    _scheduled_thread.start()


def start_sync(force_all=False):
    """Kick off the background sync if it's not already running.

    `force_all=True` re-fetches every target regardless of cache freshness — useful
    for the "Force re-sync" button when the user wants discovered_paths to repopulate
    against current API data.
    """
    global _sync_thread
    with _sync_lock:
        if _sync_state["running"]:
            return False
        _sync_state["running"] = True
    _sync_thread = threading.Thread(
        target=_sync_worker, args=(force_all,), daemon=True, name="db-sync"
    )
    _sync_thread.start()
    return True


def maybe_auto_start_sync(targets):
    """Auto-start sync if we don't have full coverage and we're not already running.

    Gated by AUTO_SYNC_ENABLED — set that env var to false/0 to fully disable
    automatic polling (manual /sync/start still works)."""
    if not AUTO_SYNC_ENABLED:
        return
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


def _humanize_for_card(detail, connector_first, target_first):
    """Rewrite a scoreDetail string for the BULLET LIST shown to the user inside
    a connector card.

    The relationship described is between the connector and the target — both
    third parties from the user's POV — so the rewrite uses the connector's
    name as the subject. The user is reading "Mindy worked with Bogdan at X",
    not "you worked with Bogdan".

    Examples:
      'They overlapped for a long time (26 months) @ Microsoft, most recently in 2009'
        -> 'Mindy worked with Bogdan at Microsoft for 26 months, most recently in 2009'
      'They have 69 mutual connections'
        -> 'Mindy has 69 mutual connections'
      'They went to Stanford together'
        -> 'Mindy and Bogdan went to Stanford together'
      'They both worked @ Acme (but didn't overlap)'
        -> 'Mindy and Bogdan both worked at Acme (but didn't overlap)'
    """
    if not detail:
        return ""
    d = detail.strip()
    connector = connector_first or "they"
    target = target_first or "them"

    m = _MUTUALS_RE.match(d)
    if m:
        return f"{connector} has {m.group(1)} mutual connections"

    m = _OVERLAP_RE.match(d)
    if m:
        duration = m.group(1).strip()
        wrap = _DURATION_WRAPPER_RE.match(duration)
        if wrap:
            duration = wrap.group(1).strip()
        company = m.group(2).strip()
        rest = m.group(4) or ""
        result = f"{connector} worked with {target} at {company}"
        if duration and duration.lower() not in ("a little while", "a long time"):
            result += f" for {duration}"
        ym = _YEAR_RE.search(rest)
        if ym:
            result += f", most recently in {ym.group(1)}"
        return result

    m = _SCHOOL_RE.match(d)
    if m:
        return f"{connector} and {target} went to {m.group(1).strip()} together"

    m = _BOTH_WORKED_RE.match(d)
    if m:
        company = m.group(1).strip()
        note = f" ({m.group(2)})" if m.group(2) else ""
        return f"{connector} and {target} both worked at {company}{note}"

    # Fallback: substitute "They" with the connector's name so we never leak raw API text
    rewrite = re.sub(r"\bThey\b", connector, d)
    rewrite = re.sub(r"\bthey\b", connector, rewrite)
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


def _build_assign_to_teammate_draft(target, connection, teammate):
    """Generate a Gmail draft asking a teammate to ping the connector for an
    intro to the target. Used by the 'Assigned to' dropdown."""
    target_first = (target.get("firstName") or "").strip() or "them"
    target_last = (target.get("lastName") or "").strip()
    target_full = f"{target_first} {target_last}".strip() if target_last else target_first
    target_company = (target.get("position") or {}).get("companyName") or ""
    target_linkedin = target.get("linkedinUrl") or ""

    connector_first = (connection.get("firstName") or "").strip()
    connector_last = (connection.get("lastName") or "").strip()
    connector_full = f"{connector_first} {connector_last}".strip() if connector_last else connector_first
    if not connector_full:
        connector_full = "your connection"

    teammate_first = (teammate.get("firstName") or "").strip() or "there"

    company_clause = f" at {target_company}" if target_company else ""
    body_parts = [
        f"Hey {teammate_first} - quick ask: any chance you could ping {connector_full} "
        f"about a warm intro to {target_full}{company_clause}? Draftboard shows you have a path "
        f"through {connector_first or 'them'}.",
    ]
    if target_linkedin:
        body_parts.append(f"{target_first}'s LinkedIn: {target_linkedin}")
    body_parts.append("Happy to coordinate or send a forwardable email.")
    body = "\n\n".join(body_parts)
    subject = f"Quick ask: warm intro to {target_full}?"

    return {
        "teammate_id": teammate.get("id"),
        "teammate_first": teammate_first,
        "teammate_last": (teammate.get("lastName") or "").strip(),
        "teammate_full": (
            f"{teammate_first} {(teammate.get('lastName') or '').strip()}"
        ).strip(),
        "teammate_initials": _initials(teammate.get("firstName"), teammate.get("lastName")),
        "subject": subject,
        "body": body,
        "gmail_url": _gmail_compose_url(subject, body),
    }


def _enrich_connection(target, connection, requested_set=None, my_user_id=None):
    """Format a Connection for the drawer template.

    `requested_set` is an optional pre-fetched set of connection_ids that have
    been Marked-as-requested for this target — saves a per-connection DB query.
    `my_user_id` is the current user's id (from /me). Used to flag which owners
    are "you" vs teammates.

    Generates the Compose-Email Gmail URL for the connector card AND the
    per-teammate assign-to drafts for any owners who aren't the current user.
    """
    pos = connection.get("position") or {}
    first = (connection.get("firstName") or "").strip()
    last = (connection.get("lastName") or "").strip()
    cid = connection.get("id")
    if requested_set is not None:
        is_requested = cid in requested_set
    else:
        is_requested = db_intro_request_get(target.get("id"), cid)

    if my_user_id is None:
        my_user_id = get_my_owner_id()

    # Bullet list shown to the user in each connector card. These describe the
    # connector-to-target relationship in third person — "Mindy worked with
    # Bogdan at Microsoft" — distinct from the email draft body which is in
    # second person addressed to the connector ("you worked with Bogdan").
    raw_details = connection.get("scoreDetails") or []
    connector_first_for_bullets = (connection.get("firstName") or "").strip() or "they"
    target_first_for_bullets = (target.get("firstName") or "").strip() or "them"
    humanized = [
        _humanize_for_card(d, connector_first_for_bullets, target_first_for_bullets)
        for d in raw_details
    ]
    humanized = [h for h in humanized if h]

    # Plain + HTML message variants for the drawer + Compose-email button.
    messages = _build_messages(target, connection)
    gmail_url = _gmail_compose_url(messages["subject"], messages["plain_fallback"])

    # Owner classification: which owners are me vs teammates.
    raw_owners = connection.get("owners") or []
    is_owned_by_me = False
    other_owners = []
    for o in raw_owners:
        if o.get("id") == my_user_id:
            is_owned_by_me = True
        else:
            other_owners.append(o)

    # Per-teammate draft for the "Assigned to" dropdown.
    assign_drafts = [
        _build_assign_to_teammate_draft(target, connection, o) for o in other_owners
    ]

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
        "owners": raw_owners,
        "is_owned_by_me": is_owned_by_me,
        "other_owners": other_owners,
        "assign_drafts": assign_drafts,
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

    # Owner filter: ?owner=<member_id>  (or "me" to mean the current user)
    owner_filter = (request.args.get("owner") or "").strip()
    me_id = get_my_owner_id()
    resolved_owner_id = me_id if owner_filter == "me" else owner_filter
    if resolved_owner_id:
        owner_target_ids = db_target_ids_for_owner(resolved_owner_id)
        targets = [t for t in targets if t.get("id") in owner_target_ids]

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

    me_for_template, _ = fetch_me()
    if me_for_template:
        me_for_template = dict(me_for_template, owner_id=me_id)
    owners_list = db_unique_owners()
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
        owners_list=owners_list,
        owner_filter=owner_filter,
        me=me_for_template,
    )


@app.route("/accounts", methods=["GET"])
def accounts_view():
    force_refresh = request.args.get("refresh") == "1"
    targets, error = fetch_all_targets(force=force_refresh)

    # Owner filter (same semantics as targets_view)
    owner_filter = (request.args.get("owner") or "").strip()
    me_id = get_my_user_id()
    resolved_owner_id = me_id if owner_filter == "me" else owner_filter
    if resolved_owner_id:
        owner_target_ids = db_target_ids_for_owner(resolved_owner_id)
        targets = [t for t in targets if t.get("id") in owner_target_ids]

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

    me_for_template, _ = fetch_me()
    if me_for_template:
        me_for_template = dict(me_for_template, owner_id=me_id)
    owners_list = db_unique_owners()
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
        owners_list=owners_list,
        owner_filter=owner_filter,
        me=me_for_template,
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


@app.route("/connections", methods=["GET"])
def connections_view():
    """List of every connector who can intro to at least one target. Searchable,
    paginated. Click any row → drawer with the targets they can intro to."""
    q = (request.args.get("q") or "").strip()
    try:
        page = max(1, int(request.args.get("page") or "1"))
    except ValueError:
        page = 1
    PAGE_SIZE = 100
    total = db_count_connectors(query=q or None)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE
    connectors = db_list_connectors(query=q or None, limit=PAGE_SIZE, offset=offset)

    me_for_template, _ = fetch_me()
    if me_for_template:
        me_for_template = dict(me_for_template, owner_id=get_my_owner_id())

    return render_template(
        "connections.html",
        connectors=connectors,
        total_count=total,
        showing_start=offset + 1 if total else 0,
        showing_end=min(offset + PAGE_SIZE, total),
        current_page=page,
        total_pages=total_pages,
        query=q,
        me=me_for_template,
        api_key_set=bool(API_KEY),
        active="connections",
    )


@app.route("/connector/<path:connector_key>/drawer", methods=["GET"])
def connector_drawer(connector_key):
    """Drawer for a single connector PERSON — lists every target they can intro
    to (across all the per-pair connection_ids the API mints), grouped by
    company. Re-uses the connector-card UI but each card is one TARGET."""
    paths = db_targets_for_connector(connector_key)
    if not paths:
        return ("<div class='p-6 text-rose-700'>No paths found for this connector. "
                "It may have been removed since the last sync.</div>"), 404

    targets_all, fetch_err = fetch_all_targets()
    if fetch_err and not targets_all:
        # The /targets API call failed (typical: 401/403 from a bad/expired key).
        # Render an honest error inside the drawer so the user sees what happened
        # instead of a silently-empty card list.
        return (
            "<div class='p-6 text-rose-700'>"
            "<strong>Couldn't load target list:</strong> " + html.escape(fetch_err) +
            "<p class='mt-2 text-sm'>The connector_paths index has the data, but we need the "
            "Target metadata (name, title, company) to render each card. Refresh after fixing the "
            "API key.</p>"
            "</div>"
        ), 200
    target_map = {t.get("id"): t for t in targets_all}
    me_id = get_my_owner_id()

    connector_info = None
    grouped = {}  # company_name (display) -> list of {target, card}
    for p in paths:
        target = target_map.get(p["target_id"])
        if not target:
            continue
        # Look up the EXACT connection in the cached per-target JSON. We use
        # the connection_id stored on this row (each (connector_person, target)
        # pair has its own UUID that matches the JSON) rather than re-matching
        # by linkedin URL, since linkedin is missing on some connections.
        target_conns, _err = fetch_target_connections(p["target_id"])
        conn_obj = next((c for c in target_conns if c.get("id") == p["connection_id"]), None)
        if not conn_obj:
            continue
        if connector_info is None:
            pos = conn_obj.get("position") or {}
            connector_info = {
                "key": connector_key,
                "first": (conn_obj.get("firstName") or "").strip(),
                "last": (conn_obj.get("lastName") or "").strip(),
                "name": (
                    (conn_obj.get("firstName") or "") + " " + (conn_obj.get("lastName") or "")
                ).strip() or "(unnamed)",
                "initials": _initials(conn_obj.get("firstName"), conn_obj.get("lastName")),
                "linkedin": conn_obj.get("linkedinUrl") or "",
                "title": pos.get("title") or "",
                "company": pos.get("companyName") or "",
            }
        company = ((target.get("position") or {}).get("companyName") or "").strip() or "(unknown company)"
        if company not in grouped:
            grouped[company] = []
        grouped[company].append({
            "target": _enrich_target(target),
            "target_id": p["target_id"],
            "card": _enrich_connection(target, conn_obj, my_user_id=me_id),
        })

    # Sort companies by best score within group; sort items inside each group
    # by score desc.
    sorted_groups = []
    for company, items in grouped.items():
        items.sort(key=lambda x: x["card"]["score"], reverse=True)
        sorted_groups.append({
            "company": company,
            "items": items,
            "best_score": items[0]["card"]["score"] if items else 0,
        })
    sorted_groups.sort(key=lambda g: g["best_score"], reverse=True)

    return render_template(
        "_drawer_connector.html",
        connector=connector_info,
        groups=sorted_groups,
        total_targets=len(paths),
    )


@app.route("/new-paths", methods=["GET"])
def new_paths_view():
    """Show paths discovered recently, sorted by score. Filterable by lookback
    window, minimum score, and which team-member owns the path."""
    # Lookback preset → epoch threshold
    since_param = (request.args.get("since") or "7d").strip()
    now = int(time.time())
    if since_param == "24h":
        since_ts = now - 86400
        since_label = "Last 24 hours"
    elif since_param == "30d":
        since_ts = now - 86400 * 30
        since_label = "Last 30 days"
    elif since_param == "all":
        since_ts = 0
        since_label = "All time"
    elif since_param == "last_sync":
        try:
            last_sync = int(db_app_state_get("last_sync_completed_at") or 0)
        except (TypeError, ValueError):
            last_sync = 0
        # "Since last sync" = paths whose first_seen_at is at or after the most
        # recent sync's start. We approximate by anchoring 1h before completion.
        since_ts = max(0, last_sync - 3600) if last_sync else 0
        since_label = "Since last sync"
    else:  # default 7d
        since_param = "7d"
        since_ts = now - 86400 * 7
        since_label = "Last 7 days"

    try:
        min_score = int(request.args.get("min_score") or "30")
    except ValueError:
        min_score = 30

    owner_filter = (request.args.get("owner") or "").strip()
    me_id = get_my_owner_id()
    resolved_owner_id = me_id if owner_filter == "me" else owner_filter

    rows = db_query_new_paths(since_ts, min_score, resolved_owner_id, limit=200)

    # Hydrate each row with the full target + connection objects so the
    # connector card can render with the same UI as the other drawers.
    targets_all, _ = fetch_all_targets()
    target_map = {t.get("id"): t for t in targets_all}
    enriched_paths = []
    seen_targets = set()
    for r in rows:
        target = target_map.get(r["target_id"])
        if not target:
            continue
        # Find the matching connection in the cached per-target list.
        target_conns, _err = fetch_target_connections(r["target_id"])
        conn_obj = next((c for c in target_conns if c.get("id") == r["connection_id"]), None)
        if not conn_obj:
            continue
        enriched_conn = _enrich_connection(target, conn_obj, my_user_id=me_id)
        enriched_paths.append({
            "target": _enrich_target(target),
            "target_id": r["target_id"],
            "connection": enriched_conn,
            "first_seen_at": r["first_seen_at"],
            "last_seen_at": r["last_seen_at"],
        })
        seen_targets.add(r["target_id"])

    me_for_template, _ = fetch_me()
    if me_for_template:
        me_for_template = dict(me_for_template, owner_id=me_id)
    owners_list = db_unique_owners()

    last_sync_completed = db_app_state_get("last_sync_completed_at")
    try:
        last_sync_completed = int(last_sync_completed) if last_sync_completed else None
    except (TypeError, ValueError):
        last_sync_completed = None

    return render_template(
        "new_paths.html",
        paths=enriched_paths,
        total=len(enriched_paths),
        since=since_param,
        since_label=since_label,
        min_score=min_score,
        owner_filter=owner_filter,
        owners_list=owners_list,
        me=me_for_template,
        last_sync_completed=last_sync_completed,
        active="new_paths",
        api_key_set=bool(API_KEY),
    )


@app.route("/sync/status", methods=["GET"])
def sync_status():
    """JSON snapshot of background sync state. Polled by the nav status pill."""
    return jsonify(sync_progress_snapshot())


@app.route("/sync/start", methods=["POST"])
def sync_start():
    """Manually trigger a sync. ?force=1 re-fetches every target regardless of cache age.

    Idempotent if one is already running.
    """
    force = request.args.get("force") == "1" or (request.get_json(silent=True) or {}).get("force") is True
    started = start_sync(force_all=force)
    return jsonify({"started": started, "force": force, "state": sync_progress_snapshot()})


# =====================================================================
# Google Workspace integration (centralized OAuth, one-time sync)
# =====================================================================
#
# Customer clicks "Connect Google" → Google consent → we exchange the auth
# code for an access token, run ONE sync (~5 min), persist aggregated
# per-contact stats to SQLite, throw the token away. No refresh tokens, no
# encryption, no daemons, no 7-day expiry to manage.
#
# Re-syncing is just clicking Connect again. Each click is a fresh OAuth
# flow + a fresh sync.
#
# Credentials: a SINGLE Draftboard-owned OAuth client (registered in the
# "Draftboard Supporters" Google Cloud project, in Testing mode). Customers
# whose email is on the project's test-users allowlist (managed by you in
# the GCP console) can connect; everyone else gets Google's standard
# "access blocked" message.
#
# Privacy: all Gmail + Calendar data stays on the customer's laptop in
# `data.db`. Nothing is sent to Draftboard's infrastructure. The only thing
# Draftboard sees during the OAuth flow is Google's confirmation that a
# user with email X granted access to client Y — standard OAuth telemetry.

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
]

GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_OAUTH_REDIRECT", "http://localhost:5050/auth/google/callback"
).strip()
GOOGLE_HISTORY_DAYS = int(os.environ.get("GOOGLE_HISTORY_DAYS", "365"))
GOOGLE_THREADS_CAP = int(os.environ.get("GOOGLE_THREADS_CAP", "2000"))
GOOGLE_EVENTS_CAP = int(os.environ.get("GOOGLE_EVENTS_CAP", "2500"))

# Lazy imports — google-auth-oauthlib and google-api-python-client are in
# requirements.txt; if the venv hasn't installed them yet we surface a clean
# error instead of crashing on boot.
_google_libs_error = None
try:
    from google_auth_oauthlib.flow import Flow as _GoogleFlow  # noqa: F401
    from googleapiclient.discovery import build as _google_build  # noqa: F401
    from googleapiclient.errors import HttpError as _GoogleHttpError  # noqa: F401
except ImportError as _e:
    _google_libs_error = (
        f"Google integration libraries not installed ({_e}). Run "
        "`pip install -r requirements.txt` to enable Gmail + Calendar sync."
    )

# Google's library refuses to issue a token over plain HTTP unless this is
# set. We only override it when the redirect URI is loopback — anyone forking
# this kit and deploying behind a real domain MUST not silently accept HTTP
# OAuth callbacks.
def _is_loopback_redirect(uri):
    return uri.startswith("http://localhost") or uri.startswith("http://127.0.0.1") or uri.startswith("http://[::1]")


if _is_loopback_redirect(GOOGLE_REDIRECT_URI):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
else:
    # Make sure a previous loopback run can't leak this setting into a deploy.
    os.environ.pop("OAUTHLIB_INSECURE_TRANSPORT", None)


def _google_libs_ready():
    return _google_libs_error is None


def _load_google_oauth_client():
    """Load the centralized Google OAuth client_id + client_secret.

    Priority order:
      1. GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET env vars
      2. .env file in this app's directory
      3. ~/.draftboard-secrets/google.env (DRAFTBOARD_STARTER_GOOGLE_CLIENT_ID +
         _SECRET, or plain GOOGLE_CLIENT_ID/_SECRET as a fallback)

    Returns (client_id, client_secret, source_label).
    """
    cid = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    cs = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if cid and cs:
        return cid, cs, "GOOGLE_CLIENT_ID/SECRET env vars"

    def _parse_env_file(path, accept_export=False):
        out = {}
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if accept_export and line.startswith("export "):
                        line = line[len("export "):].strip()
                    if "=" in line:
                        k, _, v = line.partition("=")
                        out[k.strip()] = v.strip().strip('"').strip("'")
        except OSError:
            pass
        return out

    app_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(app_dir, ".env")
    if os.path.exists(env_path):
        vals = _parse_env_file(env_path)
        cid = vals.get("GOOGLE_CLIENT_ID", "").strip()
        cs = vals.get("GOOGLE_CLIENT_SECRET", "").strip()
        if cid and cs:
            return cid, cs, env_path

    secrets_path = os.path.expanduser("~/.draftboard-secrets/google.env")
    if os.path.exists(secrets_path):
        vals = _parse_env_file(secrets_path, accept_export=True)
        cid = (vals.get("DRAFTBOARD_STARTER_GOOGLE_CLIENT_ID")
               or vals.get("GOOGLE_CLIENT_ID") or "").strip()
        cs = (vals.get("DRAFTBOARD_STARTER_GOOGLE_CLIENT_SECRET")
              or vals.get("GOOGLE_CLIENT_SECRET") or "").strip()
        if cid and cs:
            return cid, cs, secrets_path

    return "", "", None


GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, _google_creds_source = _load_google_oauth_client()
if GOOGLE_CLIENT_ID:
    print(f"[draftboard-starter] Loaded Google OAuth client from: {_google_creds_source}")
else:
    print("[draftboard-starter] No Google OAuth client configured. The Candidates feature will show a 'not connected' state until one is set.")


def _google_flow():
    if not _google_libs_ready():
        raise RuntimeError(_google_libs_error)
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        raise RuntimeError("Google OAuth client not configured.")
    client_config = {
        "web": {
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [GOOGLE_REDIRECT_URI],
        }
    }
    flow = _GoogleFlow.from_client_config(client_config, scopes=GOOGLE_SCOPES)
    flow.redirect_uri = GOOGLE_REDIRECT_URI
    return flow


def google_status():
    """Snapshot of integration state for the templates."""
    return {
        "libs_ready": _google_libs_ready(),
        "libs_error": _google_libs_error,
        "client_configured": bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        "client_source": _google_creds_source or "",
        "account_email": db_app_state_get("google_account_email", "") or "",
        "last_synced_at": int(db_app_state_get("google_last_synced_at", "0") or 0),
        "last_error": db_app_state_get("google_last_error", "") or "",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    }


# --- Header parsing helpers ----------------------------------------------

# Use stdlib email.utils — it correctly handles quoted display names with
# embedded commas (`"Last, First" <a@b.com>`), folded headers, and group
# syntax. The regex+split approach we tried first fractured those.
from email.utils import getaddresses as _getaddresses


def _parse_addresses(header_value):
    """Parse a From/To/Cc header into (name, email) tuples. Lowercases email."""
    if not header_value:
        return []
    out = []
    for name, email in _getaddresses([header_value]):
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


def _is_noise_email(email):
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


# --- Gmail fetcher --------------------------------------------------------

def fetch_gmail_threads(creds, my_email, days=GOOGLE_HISTORY_DAYS, cap=GOOGLE_THREADS_CAP, progress_cb=None):
    """Pull up to `cap` threads from the last `days` days, return per-contact stats."""
    service = _google_build("gmail", "v1", credentials=creds, cache_discovery=False)
    me = (my_email or "").lower()

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

    contacts = {}
    total = len(thread_ids)
    if progress_cb:
        progress_cb(0, total)

    for i, tid in enumerate(thread_ids):
        try:
            thread = service.users().threads().get(
                userId="me", id=tid, format="metadata",
                metadataHeaders=["From", "To", "Cc", "Date"],
            ).execute()
        except _GoogleHttpError:
            if (i + 1) % 50 == 0 and progress_cb:
                progress_cb(i + 1, total)
            continue

        thread_contacts = {}
        most_recent_ts = 0
        for msg in thread.get("messages", []) or []:
            headers = {h.get("name", "").lower(): h.get("value", "") for h in
                       (msg.get("payload", {}).get("headers", []) or [])}
            from_addrs = _parse_addresses(headers.get("from", ""))
            to_addrs = _parse_addresses(headers.get("to", ""))
            cc_addrs = _parse_addresses(headers.get("cc", ""))
            try:
                msg_ts = int(msg.get("internalDate", "0")) // 1000
            except (TypeError, ValueError):
                msg_ts = 0
            if msg_ts > most_recent_ts:
                most_recent_ts = msg_ts

            sender_email = from_addrs[0][1] if from_addrs else ""
            sent_by_me = sender_email == me

            participants = []
            for nm, em in from_addrs + to_addrs + cc_addrs:
                if em == me or _is_noise_email(em):
                    continue
                participants.append((nm, em))

            for nm, em in participants:
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

        if progress_cb and (i + 1) % 25 == 0:
            progress_cb(i + 1, total)

    if progress_cb:
        progress_cb(total, total)
    return contacts


# --- Calendar fetcher ----------------------------------------------------

def fetch_calendar_events(creds, my_email, days=GOOGLE_HISTORY_DAYS, cap=GOOGLE_EVENTS_CAP, progress_cb=None):
    """Pull up to `cap` events from the last `days` days, aggregate per-attendee."""
    service = _google_build("calendar", "v3", credentials=creds, cache_discovery=False)
    me = (my_email or "").lower()

    from datetime import datetime, timedelta, timezone
    time_min = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    time_max = datetime.now(timezone.utc).isoformat()

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
                if not email or email == me or _is_noise_email(email):
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
        if progress_cb and processed % 100 == 0:
            progress_cb(processed, processed)
        page_token = resp.get("nextPageToken")
        if not page_token or processed >= cap:
            break
    if progress_cb:
        progress_cb(processed, processed)
    return contacts


# --- Persistence ---------------------------------------------------------

def db_replace_gmail_contacts(contacts):
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        conn.execute("DELETE FROM gmail_contacts")
        for email, c in contacts.items():
            conn.execute(
                "INSERT INTO gmail_contacts (email, name, emails_sent, replies_received, "
                "threads_count, last_contact_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (email, c.get("name", ""), c.get("emails_sent", 0),
                 c.get("replies_received", 0), c.get("threads_count", 0),
                 c.get("last_contact_at", 0), now),
            )
        conn.commit()


def db_replace_calendar_contacts(contacts):
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        conn.execute("DELETE FROM calendar_contacts")
        for email, c in contacts.items():
            conn.execute(
                "INSERT INTO calendar_contacts (email, name, meetings_count, last_met_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (email, c.get("name", ""), c.get("meetings_count", 0),
                 c.get("last_met_at", 0), now),
            )
        conn.commit()


def db_clear_google_data():
    """Wipe synced Gmail + Calendar data + the recorded sync metadata + the
    in-memory sync-state pill (so it doesn't keep showing 'stage=done' from
    the prior sync)."""
    with _db_lock, _db_connect() as conn:
        conn.execute("DELETE FROM gmail_contacts")
        conn.execute("DELETE FROM calendar_contacts")
        conn.execute("DELETE FROM app_state WHERE key IN ('google_account_email', 'google_last_synced_at', 'google_last_error')")
        conn.commit()
    with _google_sync_lock:
        _google_sync_state.update({
            "running": False, "stage": "", "processed": 0, "total": 0,
            "started_at": 0, "ended_at": 0, "last_error": "", "account_email": "",
        })


# --- Scoring + candidate query ------------------------------------------

def score_contact(emails_sent, replies_received, threads_count, meetings_count, last_contact_days_ago):
    """Per-contact relationship-strength score. See README."""
    base = (emails_sent or 0) + 2 * (replies_received or 0) + 3 * (threads_count or 0) + 5 * (meetings_count or 0)
    if last_contact_days_ago is None:
        return 0
    recency = max(0.1, 1.0 - (last_contact_days_ago / 365.0))
    return int(base * recency)


def db_query_candidates(limit=200, offset=0, query="", contributor="", source="", status_filter="active"):
    """Merge gmail + calendar contacts (the local user's own scan) AND any
    imported teammate scans, score each, return top N.

    Each candidate row carries `contributor_email` + `contributor_name`. The
    local user's own contacts use the special contributor_email value
    `__self__` (the candidates page renders that as "you").

    BIDIRECTIONAL FILTER: an email-only contact must have at least one
    outbound email from the contributor AND at least one reply from them.
    This cuts cold-outreach prospects (sent → no reply) and inbound
    newsletters (received → never replied). Calendar-only contacts are kept
    unconditionally since attending a shared meeting IS bidirectional.

    Filters:
    - `contributor`: when non-empty, restricts to one contributor. "__self__"
      = local user only; teammate email = just that teammate.
    - `source`: "" all, "email" only contacts with bidirectional email,
      "calendar" only contacts with shared meetings.
    - `status_filter`: "active" (default — hides 'hidden'), "all", "starred",
      "supporter", "hidden", "unmarked".
    """
    now = int(time.time())
    me_email = (db_app_state_get("google_account_email", "") or "").strip().lower()
    me_name = ""  # We only persist email; the candidates page can show "you" instead.

    # Local user's own gmail+calendar (with bidirectional filter).
    sql_self_left = """
        SELECT
          '__self__' AS contributor_email,
          '' AS contributor_name,
          g.email AS email,
          COALESCE(NULLIF(g.name, ''), NULLIF(c.name, ''), '') AS name,
          g.emails_sent AS emails_sent,
          g.replies_received AS replies_received,
          g.threads_count AS threads_count,
          COALESCE(c.meetings_count, 0) AS meetings_count,
          g.last_contact_at AS last_emailed_at,
          COALESCE(c.last_met_at, 0) AS last_met_at
        FROM gmail_contacts g
        LEFT JOIN calendar_contacts c ON g.email = c.email
        WHERE
          (g.emails_sent >= 1 AND g.replies_received >= 1)
          OR COALESCE(c.meetings_count, 0) >= 1
    """
    sql_self_right = """
        SELECT
          '__self__' AS contributor_email,
          '' AS contributor_name,
          c.email AS email,
          COALESCE(NULLIF(c.name, ''), '') AS name,
          0 AS emails_sent,
          0 AS replies_received,
          0 AS threads_count,
          c.meetings_count AS meetings_count,
          0 AS last_emailed_at,
          c.last_met_at AS last_met_at
        FROM calendar_contacts c
        WHERE c.email NOT IN (SELECT email FROM gmail_contacts)
          AND c.meetings_count >= 1
    """
    # Imported teammate scans (same bidirectional filter applied per-contributor).
    sql_teammate = """
        SELECT
          tc.contributor_email AS contributor_email,
          tc.contributor_name AS contributor_name,
          tc.email AS email,
          tc.name AS name,
          tc.emails_sent AS emails_sent,
          tc.replies_received AS replies_received,
          tc.threads_count AS threads_count,
          tc.meetings_count AS meetings_count,
          tc.last_contact_at AS last_emailed_at,
          tc.last_contact_at AS last_met_at
        FROM teammate_contacts tc
        WHERE
          (tc.emails_sent >= 1 AND tc.replies_received >= 1)
          OR tc.meetings_count >= 1
    """
    full_sql = f"SELECT * FROM ({sql_self_left} UNION ALL {sql_self_right} UNION ALL {sql_teammate})"
    args = []
    where_clauses = []
    if query:
        where_clauses.append("(LOWER(email) LIKE ? OR LOWER(name) LIKE ?)")
        like = f"%{query.lower()}%"
        args.extend([like, like])
    if contributor:
        where_clauses.append("contributor_email = ?")
        args.append(contributor)
    if source == "email":
        where_clauses.append("(emails_sent >= 1 AND replies_received >= 1)")
    elif source == "calendar":
        where_clauses.append("meetings_count >= 1")
    if where_clauses:
        full_sql += " WHERE " + " AND ".join(where_clauses)
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(full_sql, args)
        rows = cur.fetchall()
        # Pull status map in one go so we can filter + label rows in Python.
        status_cur = conn.execute("SELECT email, status FROM candidate_status")
        status_map = {r[0]: r[1] for r in status_cur.fetchall()}
        # 1st-degree connection lookup: build a set of normalized LinkedIn URLs
        # from the user's existing Draftboard connector_paths network.
        # Powers the "1st°" badge on candidate rows.
        #
        # Normalization is done in Python (not SQL) so it matches
        # _normalize_linkedin exactly — that helper strips query strings + anchors
        # in addition to lowercasing and dropping trailing slashes. SQL-side
        # normalization would miss matches when connector_paths has tracking
        # suffixes (e.g. ?utm_source=...) that the resolver's URL doesn't.
        firstdegree_cur = conn.execute(
            "SELECT DISTINCT connector_linkedin FROM connector_paths "
            "WHERE connector_linkedin != ''"
        )
        firstdegree_set = {
            _normalize_linkedin(r[0]) for r in firstdegree_cur.fetchall() if r[0]
        }
        firstdegree_set.discard("")
        # And a separate lookup for resolved LinkedIn URLs from the resolver.
        resolved_cur = conn.execute(
            "SELECT email, linkedin_url FROM linkedin_resolutions WHERE linkedin_url IS NOT NULL AND linkedin_url != ''"
        )
        resolved_map = {r[0]: r[1] for r in resolved_cur.fetchall()}
    candidates = []
    for r in rows:
        contributor_email = r[0]
        contributor_name = r[1]
        email = r[2]
        name = r[3]
        emails_sent = r[4]
        replies_received = r[5]
        threads_count = r[6]
        meetings_count = r[7]
        last_emailed_at = r[8]
        last_met_at = r[9]
        last_contact_at = max(last_emailed_at, last_met_at)
        days_ago = ((now - last_contact_at) / 86400.0) if last_contact_at else None
        score = score_contact(emails_sent, replies_received, threads_count, meetings_count, days_ago)
        # Status filter — applied in Python so the SQL union stays simple.
        row_status = status_map.get(email, "")
        if status_filter == "active" and row_status == "hidden":
            continue
        if status_filter == "starred" and row_status != "starred":
            continue
        if status_filter == "hidden" and row_status != "hidden":
            continue
        if status_filter == "supporter" and row_status != "supporter":
            continue
        if status_filter == "unmarked" and row_status:
            continue
        # 1st-degree check: if this candidate has a resolved LinkedIn URL,
        # see if it appears in the user's connector_paths network.
        linkedin_url = resolved_map.get(email, "")
        is_first_degree = False
        if linkedin_url:
            norm = _normalize_linkedin(linkedin_url)
            if norm and norm in firstdegree_set:
                is_first_degree = True
        # Render-time labels for the contributor.
        if contributor_email == "__self__":
            contributor_label = "you"
        elif contributor_name:
            contributor_label = contributor_name
        else:
            contributor_label = contributor_email
        candidates.append({
            "email": email,
            "name": name or email,
            "emails_sent": emails_sent,
            "replies_received": replies_received,
            "threads_count": threads_count,
            "meetings_count": meetings_count,
            "last_contact_at": last_contact_at,
            "days_ago": int(days_ago) if days_ago is not None else None,
            "score": score,
            "contributor_email": contributor_email,
            "contributor_label": contributor_label,
            "row_status": row_status,
            "is_first_degree": is_first_degree,
        })
    candidates.sort(key=lambda c: (-c["score"], -c["threads_count"], c["email"]))
    total = len(candidates)
    return candidates[offset:offset + limit], total


def db_set_candidate_status(email, status):
    """Set or clear a candidate's triage status. Empty status removes the row."""
    email = (email or "").strip().lower()
    if not email:
        return
    with _db_lock, _db_connect() as conn:
        if status:
            conn.execute(
                "INSERT INTO candidate_status (email, status, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (email, status, int(time.time())),
            )
        else:
            conn.execute("DELETE FROM candidate_status WHERE email = ?", (email,))
        conn.commit()


def db_count_unresolved_candidates():
    """Count candidates that have NO row in linkedin_resolutions (i.e. never
    been tried). Used by the bulk-resolve toolbar button. Excludes hidden
    candidates by default since the user already triaged them out.

    The "qualifying candidate" definition here MUST stay equivalent to what
    db_query_candidates produces. The two queries use slightly different
    shapes (this one is a flat UNION of three table-specific SELECTs;
    db_query_candidates does a LEFT JOIN on the self side to surface the
    meeting count alongside email columns) but the resulting *email sets*
    are equivalent: a row qualifies if it's in gmail_contacts with
    bidirectional email engagement, OR in calendar_contacts with at least
    one meeting, OR in teammate_contacts meeting either rule. Keep these
    rules in lock-step or the bulk-resolve "N unresolved" counter will
    drift from the actual list of rows on the page."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("""
            SELECT COUNT(*) FROM (
                SELECT email FROM gmail_contacts WHERE emails_sent >= 1 AND replies_received >= 1
                UNION
                SELECT email FROM calendar_contacts WHERE meetings_count >= 1
                UNION
                SELECT email FROM teammate_contacts WHERE meetings_count >= 1 OR (emails_sent >= 1 AND replies_received >= 1)
            ) AS c
            WHERE c.email NOT IN (SELECT email FROM linkedin_resolutions)
              AND c.email NOT IN (SELECT email FROM candidate_status WHERE status = 'hidden')
        """)
        return cur.fetchone()[0]


def db_unresolved_candidate_emails(limit=500):
    """Return up to `limit` (email, name) tuples for candidates that haven't
    been resolved yet. Names come from gmail_contacts, calendar_contacts, or
    teammate_contacts in that priority. Used to feed the bulk-resolve flow."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("""
            SELECT c.email,
                   COALESCE(NULLIF(gn.name, ''), NULLIF(cn.name, ''), NULLIF(tn.name, ''), '') AS name
            FROM (
                SELECT email FROM gmail_contacts WHERE emails_sent >= 1 AND replies_received >= 1
                UNION
                SELECT email FROM calendar_contacts WHERE meetings_count >= 1
                UNION
                SELECT email FROM teammate_contacts WHERE meetings_count >= 1 OR (emails_sent >= 1 AND replies_received >= 1)
            ) AS c
            LEFT JOIN gmail_contacts    gn ON c.email = gn.email
            LEFT JOIN calendar_contacts cn ON c.email = cn.email
            LEFT JOIN teammate_contacts tn ON c.email = tn.email
            WHERE c.email NOT IN (SELECT email FROM linkedin_resolutions)
              AND c.email NOT IN (SELECT email FROM candidate_status WHERE status = 'hidden')
            ORDER BY c.email
            LIMIT ?
        """, (limit,))
        return [(row[0], row[1] or row[0]) for row in cur.fetchall()]


def db_list_contributors():
    """Return all contributors who have rows in candidates: '__self__' (if the
    local user has synced) plus every imported teammate."""
    out = []
    with _db_lock, _db_connect() as conn:
        # Self
        cur = conn.execute("SELECT COUNT(*) FROM gmail_contacts")
        gc = cur.fetchone()[0]
        cur = conn.execute("SELECT COUNT(*) FROM calendar_contacts")
        cc = cur.fetchone()[0]
        if gc > 0 or cc > 0:
            out.append({"email": "__self__", "name": "you", "row_count": gc + cc, "imported_at": 0})
        # Teammates
        cur = conn.execute(
            "SELECT contributor_email, MAX(contributor_name), COUNT(*), MAX(imported_at) "
            "FROM teammate_contacts GROUP BY contributor_email"
        )
        for row in cur.fetchall():
            out.append({"email": row[0], "name": row[1] or row[0], "row_count": row[2], "imported_at": row[3] or 0})
    return out


def db_import_teammate_scan(payload):
    """Validate + persist a parsed scanner JSON. Returns (count_imported,
    contributor_email, error_or_None). Re-imports from the same teammate
    UPDATE in place via INSERT OR REPLACE."""
    if not isinstance(payload, dict):
        return 0, "", "JSON root must be an object."
    if payload.get("scan_type") != "draftboard_supporter_scan":
        return 0, "", "Not a Draftboard supporter scan file (scan_type mismatch)."
    if int(payload.get("schema_version", 0)) != 1:
        return 0, "", f"Unsupported schema_version: {payload.get('schema_version')}"
    scanned_by = payload.get("scanned_by") or {}
    contributor_email = (scanned_by.get("email") or "").strip().lower()
    contributor_name = (scanned_by.get("name") or "").strip()
    if not contributor_email or "@" not in contributor_email:
        return 0, "", "scanned_by.email is missing or invalid."

    gmail_rows = payload.get("gmail_contacts") or []
    cal_rows = payload.get("calendar_contacts") or []
    # Merge by email — a contact in both gets a single row with combined counts.
    merged = {}
    for c in gmail_rows:
        em = (c.get("email") or "").strip().lower()
        if not em or "@" not in em:
            continue
        merged[em] = {
            "name": c.get("name") or "",
            "emails_sent": int(c.get("emails_sent") or 0),
            "replies_received": int(c.get("replies_received") or 0),
            "threads_count": int(c.get("threads_count") or 0),
            "meetings_count": 0,
            "last_contact_at": int(c.get("last_contact_at") or 0),
        }
    for c in cal_rows:
        em = (c.get("email") or "").strip().lower()
        if not em or "@" not in em:
            continue
        d = merged.setdefault(em, {
            "name": c.get("name") or "",
            "emails_sent": 0, "replies_received": 0, "threads_count": 0,
            "meetings_count": 0, "last_contact_at": 0,
        })
        d["meetings_count"] = int(c.get("meetings_count") or 0)
        last_met = int(c.get("last_met_at") or 0)
        if last_met > d["last_contact_at"]:
            d["last_contact_at"] = last_met
        if not d["name"] and c.get("name"):
            d["name"] = c["name"]

    now = int(time.time())
    count = 0
    with _db_lock, _db_connect() as conn:
        # Wipe any existing rows for this contributor first so removed contacts
        # don't linger from a previous import.
        conn.execute("DELETE FROM teammate_contacts WHERE contributor_email = ?", (contributor_email,))
        for em, d in merged.items():
            conn.execute(
                "INSERT INTO teammate_contacts (contributor_email, contributor_name, email, name, "
                "emails_sent, replies_received, threads_count, meetings_count, last_contact_at, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (contributor_email, contributor_name, em, d["name"],
                 d["emails_sent"], d["replies_received"], d["threads_count"],
                 d["meetings_count"], d["last_contact_at"], now),
            )
            count += 1
        conn.commit()
    return count, contributor_email, None


def db_remove_teammate_contributor(contributor_email):
    """Wipe all rows for one contributor (the 'remove a teammate' button)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM teammate_contacts WHERE contributor_email = ?",
            (contributor_email.strip().lower(),),
        )
        conn.commit()
        return cur.rowcount


# --- One-time sync worker ------------------------------------------------

_google_sync_lock = threading.Lock()
_google_sync_state = {
    "running": False,
    "stage": "",
    "processed": 0,
    "total": 0,
    "started_at": 0,
    "ended_at": 0,
    "last_error": "",
    "account_email": "",
}
_google_sync_thread = None


def _set_google_sync_state(**kwargs):
    with _google_sync_lock:
        _google_sync_state.update(kwargs)


def google_sync_progress_snapshot():
    with _google_sync_lock:
        snap = dict(_google_sync_state)
    if snap["total"] > 0:
        snap["percent"] = int(100 * snap["processed"] / snap["total"])
    else:
        snap["percent"] = 0
    return snap


def _google_sync_worker(creds):
    """Run one sync end-to-end. Credentials are passed in memory and discarded
    when this function returns — never persisted."""
    started = int(time.time())
    _set_google_sync_state(running=True, stage="starting", processed=0, total=0,
                           started_at=started, ended_at=0, last_error="")
    try:
        try:
            oauth2 = _google_build("oauth2", "v2", credentials=creds, cache_discovery=False)
            profile = oauth2.userinfo().get().execute() or {}
            my_email = (profile.get("email") or "").lower()
        except Exception:
            gmail = _google_build("gmail", "v1", credentials=creds, cache_discovery=False)
            my_email = (gmail.users().getProfile(userId="me").execute() or {}).get("emailAddress", "").lower()

        if my_email:
            db_app_state_set("google_account_email", my_email)
            _set_google_sync_state(account_email=my_email)

        # Gmail
        _set_google_sync_state(stage="gmail", processed=0, total=0)
        def _gmail_progress(p, t):
            _set_google_sync_state(stage="gmail", processed=p, total=t)
        gmail_contacts = fetch_gmail_threads(creds, my_email, progress_cb=_gmail_progress)
        db_replace_gmail_contacts(gmail_contacts)

        # Calendar
        _set_google_sync_state(stage="calendar", processed=0, total=0)
        def _cal_progress(p, t):
            _set_google_sync_state(stage="calendar", processed=p, total=t)
        calendar_contacts = fetch_calendar_events(creds, my_email, progress_cb=_cal_progress)
        db_replace_calendar_contacts(calendar_contacts)

        ended = int(time.time())
        db_app_state_set("google_last_synced_at", str(ended))
        db_app_state_set("google_last_error", "")
        _set_google_sync_state(running=False, stage="done", ended_at=ended, last_error="")
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        db_app_state_set("google_last_error", msg)
        _set_google_sync_state(running=False, stage="error", ended_at=int(time.time()), last_error=msg)


def start_google_sync(creds):
    """Kick off the one-time sync. Returns True if started, False if one's
    already in flight."""
    global _google_sync_thread
    with _google_sync_lock:
        if _google_sync_state["running"]:
            return False
        _google_sync_state["running"] = True
    _google_sync_thread = threading.Thread(
        target=_google_sync_worker, args=(creds,), daemon=True, name="google-sync"
    )
    _google_sync_thread.start()
    return True


# --- Routes --------------------------------------------------------------

@app.route("/settings/google", methods=["GET"])
def settings_google_view():
    """Status page: 'Connect Google' button OR last-synced banner + Re-sync."""
    status = google_status()
    sync_state = google_sync_progress_snapshot()
    return render_template(
        "settings_google.html",
        status=status,
        sync_state=sync_state,
        active="settings_google",
        api_key_set=bool(API_KEY),
    )


@app.route("/settings/google/clear-data", methods=["POST"])
def settings_google_clear_data():
    """Wipe the synced contact data + last-synced metadata."""
    db_clear_google_data()
    return redirect(url_for("settings_google_view") + "?cleared=1")


@app.route("/auth/google/start", methods=["GET"])
def auth_google_start():
    if not _google_libs_ready():
        return redirect(url_for("settings_google_view") + "?error=libs_missing")
    if not (GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET):
        return redirect(url_for("settings_google_view") + "?error=client_not_configured")
    flow = _google_flow()
    auth_url, state = flow.authorization_url(
        access_type="online",  # one-time use, no refresh token needed
        include_granted_scopes="true",
    )
    from flask import session
    session["google_oauth_state"] = state
    # PKCE: persist the code_verifier across the redirect. The Flow object is
    # ephemeral; without saving the verifier, the callback's fresh Flow can't
    # complete the token exchange and Google rejects with "Missing code verifier".
    session["google_oauth_code_verifier"] = flow.code_verifier
    return redirect(auth_url)


@app.route("/auth/google/callback", methods=["GET"])
def auth_google_callback():
    if not _google_libs_ready():
        return redirect(url_for("settings_google_view") + "?error=libs_missing")
    from flask import session
    # Pop the state nonce up front so it can't be replayed by a second callback.
    expected_state = session.pop("google_oauth_state", "")
    got_state = request.args.get("state", "")

    err = request.args.get("error")
    if err:
        # Map Google's standard error codes to friendly internal codes the
        # /settings/google template can render with a recovery message.
        if err == "access_denied":
            # Two cases: customer cancelled, OR their email isn't on the
            # test-users allowlist. Google's `error_description` distinguishes
            # them; pass it through so the template can switch on it.
            desc = (request.args.get("error_description") or "").lower()
            if "test users" in desc or "verification" in desc or "blocked" in desc:
                return redirect(url_for("settings_google_view") + "?error=access_blocked")
            return redirect(url_for("settings_google_view") + "?error=access_cancelled")
        return redirect(url_for("settings_google_view") + f"?error={err}")

    # Reject if state is missing on either side OR doesn't match. The previous
    # logic short-circuited the comparison when either side was empty, leaving
    # the callback open to CSRF.
    if not expected_state or not got_state or expected_state != got_state:
        return redirect(url_for("settings_google_view") + "?error=state_mismatch")

    # PKCE: restore the code_verifier saved during /auth/google/start. Pop so
    # it can't be replayed by a second callback.
    code_verifier = session.pop("google_oauth_code_verifier", None)
    try:
        flow = _google_flow()
        flow.code_verifier = code_verifier
        flow.fetch_token(authorization_response=request.url)
        creds = flow.credentials
        # Kick off sync — credentials live only inside the worker thread, never persisted.
        start_google_sync(creds)
    except Exception as e:
        # Log the full traceback to the server console so we can diagnose
        # callback failures. The user-facing banner stays generic.
        import traceback
        print("=" * 70, flush=True)
        print(f"[google/callback] {type(e).__name__}: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        print("=" * 70, flush=True)
        return redirect(url_for("settings_google_view") + f"?error=callback:{type(e).__name__}")
    return redirect(url_for("settings_google_view") + "?connected=1")


@app.route("/google/sync/status", methods=["GET"])
def google_sync_status():
    return jsonify(google_sync_progress_snapshot())


@app.route("/supporters/candidates", methods=["GET"])
def supporters_candidates_view():
    """Ranked list of high-engagement contacts from the local user's Gmail
    + Calendar, plus any imported teammate scans."""
    try:
        page = max(1, int(request.args.get("page") or "1"))
    except ValueError:
        page = 1
    per_page = 50
    query = (request.args.get("q") or "").strip()
    contributor = (request.args.get("contributor") or "").strip()
    source = (request.args.get("source") or "").strip()
    status_filter = (request.args.get("status_filter") or "active").strip()
    candidates, total = db_query_candidates(
        limit=per_page, offset=(page - 1) * per_page,
        query=query, contributor=contributor, source=source, status_filter=status_filter,
    )
    # Hydrate each candidate with its cached LinkedIn-resolution result (if any)
    # so already-resolved rows render with the URL inline. Fresh candidates
    # render the "Resolve" button; the JS handler hits /candidates/resolve and
    # mutates the row in place on success.
    #
    # `resolution_attempted` distinguishes "we tried and got nothing" (show
    # Retry) from "we never tried" (show Resolve). The resolver leaves
    # `error` NULL on plain "no match" results, so we have to look at the
    # cache row's existence, not just at the error column.
    for c in candidates:
        cached = db_get_resolution(c["email"])
        if cached:
            c["linkedin_url"] = cached.get("linkedin_url") or ""
            c["resolution_source"] = cached.get("source") or ""
            c["resolution_confidence"] = cached.get("confidence") or ""
            c["resolution_error"] = cached.get("error") or cached.get("reasoning") or ""
            c["resolution_attempted"] = True
        else:
            c["linkedin_url"] = ""
            c["resolution_source"] = ""
            c["resolution_confidence"] = ""
            c["resolution_error"] = ""
            c["resolution_attempted"] = False
    total_pages = max(1, (total + per_page - 1) // per_page)
    status = google_status()
    sync_state = google_sync_progress_snapshot()
    contributors = db_list_contributors()
    resolver_keys = _load_resolver_keys()
    resolver_status = _resolver_status(resolver_keys)
    unresolved_count = db_count_unresolved_candidates()
    return render_template(
        "supporters_candidates.html",
        candidates=candidates,
        total=total,
        page=page,
        total_pages=total_pages,
        per_page=per_page,
        query=query,
        contributor=contributor,
        contributors=contributors,
        source=source,
        status_filter=status_filter,
        unresolved_count=unresolved_count,
        status=status,
        sync_state=sync_state,
        resolver_status=resolver_status,
        active="candidates",
        api_key_set=bool(API_KEY),
    )


@app.route("/candidates/status", methods=["POST"])
def candidates_set_status():
    """Set or toggle a candidate's triage status. JSON body:
       {"email": "...", "status": "starred"|"hidden"|"supporter"|""}
       Empty status clears."""
    if not request.is_json:
        return jsonify({"error": "JSON body required"}), 400
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    status = (body.get("status") or "").strip()
    if not email:
        return jsonify({"error": "email required"}), 400
    # RFC 5321 caps email at 254 chars; we keep some headroom but reject
    # obviously-wrong inputs so a curl-loop or scripted-misuse can't bloat
    # the candidate_status table to a ridiculous size.
    if len(email) > 320:
        return jsonify({"error": "email too long"}), 400
    if status and status not in ("starred", "hidden", "supporter"):
        return jsonify({"error": f"unknown status: {status}"}), 400
    db_set_candidate_status(email, status)
    return jsonify({"ok": True, "email": email, "status": status})


@app.route("/candidates/unresolved", methods=["GET"])
def candidates_unresolved_list():
    """Return up to 500 (name, email) pairs the bulk-resolve button can feed
    into /candidates/resolve/batch. The JS pulls a chunk, posts it, then
    pulls the next chunk until the count drops to 0."""
    rows = db_unresolved_candidate_emails(limit=500)
    return jsonify({
        "count": len(rows),
        "remaining": db_count_unresolved_candidates(),
        "contacts": [{"email": e, "name": n} for e, n in rows],
    })


@app.route("/supporters/import-teammate", methods=["GET", "POST"])
def supporters_import_teammate_view():
    """Upload a teammate's supporter_scan_*.json. Validates schema, replaces
    any prior import from the same teammate, redirects with a summary."""
    error = None
    summary = None
    if request.method == "POST":
        upload = request.files.get("scan_file")
        if not upload or not upload.filename:
            error = "No file selected. Pick the supporter_scan_*.json a teammate sent you."
        else:
            try:
                payload = json.loads(upload.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as e:
                error = f"Couldn't parse JSON: {e}"
                payload = None
            if payload is not None:
                count, contributor, err = db_import_teammate_scan(payload)
                if err:
                    error = err
                else:
                    return redirect(
                        url_for("supporters_candidates_view")
                        + f"?contributor={contributor}&imported=1&imported_count={count}"
                    )

    contributors = db_list_contributors()
    return render_template(
        "import_teammate.html",
        error=error,
        summary=summary,
        contributors=[c for c in contributors if c["email"] != "__self__"],
        active="candidates",
        api_key_set=bool(API_KEY),
    )


@app.route("/supporters/remove-teammate", methods=["POST"])
def supporters_remove_teammate():
    contributor_email = (request.form.get("contributor_email") or "").strip().lower()
    if not contributor_email:
        return redirect(url_for("supporters_import_teammate_view"))
    removed = db_remove_teammate_contributor(contributor_email)
    return redirect(url_for("supporters_import_teammate_view") + f"?removed={removed}")


# =====================================================================
# LinkedIn resolver wiring (from main; tied to linkedin_resolver.py)
# =====================================================================

def _resolver_status(keys: dict) -> dict:
    """Summarize which resolver methods are usable given the configured keys.
    Drives the "Apollo: configured" / "Google search: not configured" labels
    on the wizard."""
    apollo_ready = bool(keys.get("apollo_api_key"))
    cse_ready = bool(keys.get("google_cse_api_key") and keys.get("google_cse_id") and keys.get("openai_api_key"))
    return {
        "apollo_ready": apollo_ready,
        "cse_ready": cse_ready,
        "any_ready": apollo_ready or cse_ready,
        # Surface presence (not the value) so the template can show "configured"
        # without exposing the secret. Env-sourced keys are also reflected here.
        "apollo_present": bool(keys.get("apollo_api_key")),
        "cse_key_present": bool(keys.get("google_cse_api_key")),
        "cse_id_present": bool(keys.get("google_cse_id")),
        "openai_present": bool(keys.get("openai_api_key")),
    }


@app.route("/settings/linkedin-resolver", methods=["GET"])
def linkedin_resolver_settings():
    """Render the resolver-keys wizard. Shows which methods are currently
    usable and a paste-and-save form for each key."""
    keys = _load_resolver_keys()
    return render_template(
        "settings_linkedin_resolver.html",
        status=_resolver_status(keys),
        secrets_path=RESOLVER_SECRETS_PATH,
    )


@app.route("/settings/linkedin-resolver", methods=["POST"])
def save_linkedin_resolver_settings():
    """Persist any non-empty resolver keys to the secrets JSON file. Empty
    fields leave existing values untouched. A `clear_<name>: true` field wipes
    the corresponding key.

    JSON-only by design: form-encoded POSTs are CORS "simple requests" that
    bypass preflight, so accepting them would let any malicious site the
    customer visits silently overwrite their API keys via a hidden form. JSON
    bodies trigger CORS preflight, which Flask doesn't answer to by default,
    so cross-origin POSTs are blocked. Same-origin fetch from the wizard JS
    works fine."""
    if not request.is_json:
        return jsonify({
            "error": "JSON body required (Content-Type: application/json)",
        }), 400
    body = request.get_json(silent=True) or {}
    updates = {}
    cleared_any = False
    for name in RESOLVER_KEY_NAMES:
        if body.get(f"clear_{name}") is True:
            updates[name] = "__clear__"
            cleared_any = True
            continue
        raw = body.get(name)
        if isinstance(raw, str) and raw.strip():
            updates[name] = raw.strip()
    if updates:
        _save_resolver_keys(updates)
    return jsonify({
        "ok": True,
        "saved": [n for n, v in updates.items() if v != "__clear__"],
        "cleared": [n for n, v in updates.items() if v == "__clear__"],
        "status": _resolver_status(_load_resolver_keys()),
    })


def _looks_like_email(value: str) -> bool:
    """Cheap shape check — not RFC 5322. Just enough to reject obvious garbage
    so we don't waste an Apollo credit on it."""
    if not isinstance(value, str):
        return False
    v = value.strip()
    if "@" not in v or v.startswith("@") or v.endswith("@"):
        return False
    local, _, domain = v.rpartition("@")
    return bool(local) and "." in domain


RESOLVE_BATCH_MAX = 500
RESOLVE_BATCH_WORKERS = 5


def _resolve_one_for_batch(name: str, email: str, keys: dict, force: bool) -> dict:
    """Single-row resolver used by the batch worker pool. Mirrors the
    cache-first logic of /candidates/resolve. Always returns the same shape
    so the caller can build a uniform response regardless of which branch
    fired (cache hit, malformed input, fresh resolution)."""
    out_email = email.strip().lower()
    base_err = {
        "email": out_email, "name": name,
        "linkedin_url": None, "full_name": None,
        "confidence": "none", "source": "none",
        "query": "", "resolved_at": int(time.time()),
        "cached": False,
    }
    if not name.strip() or not email.strip():
        return {**base_err, "error": "name and email are required", "reasoning": "Missing name or email."}
    if not _looks_like_email(email):
        return {**base_err, "error": "email is malformed", "reasoning": "Email looks malformed."}

    if not force:
        cached = db_get_resolution(email)
        if cached is not None:
            return cached  # already shape-matched

    result = resolve_linkedin(
        name, email,
        apollo_key=keys.get("apollo_api_key") or None,
        cse_key=keys.get("google_cse_api_key") or None,
        cse_id=keys.get("google_cse_id") or None,
        openai_key=keys.get("openai_api_key") or None,
    )
    db_put_resolution(email, name, result)
    result.pop("_transient", None)
    return {
        "email": out_email,
        "name": name,
        "resolved_at": int(time.time()),
        **result,
        "cached": False,
    }


@app.route("/candidates/resolve/batch", methods=["POST"])
def candidates_resolve_batch():
    """Resolve a list of (name, email) pairs concurrently.

    Body:
      {
        "contacts": [{"name": "...", "email": "..."}, ...],   # required, max 500
        "force":    false,                                      # optional
      }

    Returns:
      {
        "results": [
          {"email":"...", "linkedin_url":"...", "confidence":"...",
           "source":"...", "reasoning":"...", "cached": bool, "error": ...},
          ...
        ],
        "count":  N,
        "hits":   N_with_a_linkedin_url,
        "cached": N_served_from_cache,
      }

    Order of `results` matches the input order. Each result includes the
    lowercased `email` so callers can correlate. Per-row failures don't
    abort the batch — they come back with `error` set on that row only.
    """
    if not request.is_json:
        return jsonify({"error": "JSON body required (Content-Type: application/json)"}), 400
    body = request.get_json(silent=True) or {}
    contacts = body.get("contacts")
    if not isinstance(contacts, list) or not contacts:
        return jsonify({"error": "contacts must be a non-empty list"}), 400
    if len(contacts) > RESOLVE_BATCH_MAX:
        return jsonify({
            "error": f"batch capped at {RESOLVE_BATCH_MAX} contacts; chunk and re-call",
        }), 400
    force = bool(body.get("force"))

    # Load keys once for the whole batch instead of re-reading the secrets
    # file per row.
    keys = _load_resolver_keys()

    # Each row keeps its input index so we can re-order at the end. Per-field
    # length caps prevent a single fat row from ballooning the worker process
    # before it ever hits Apollo/CSE/OpenAI.
    indexed = []
    for i, c in enumerate(contacts):
        if not isinstance(c, dict):
            indexed.append((i, "", ""))
            continue
        nm = str(c.get("name") or "")[:RESOLVE_NAME_MAX_LEN]
        em = str(c.get("email") or "")[:RESOLVE_EMAIL_MAX_LEN]
        indexed.append((i, nm, em))

    results: list[dict | None] = [None] * len(indexed)
    with ThreadPoolExecutor(max_workers=RESOLVE_BATCH_WORKERS) as pool:
        future_to_idx = {
            pool.submit(_resolve_one_for_batch, name, email, keys, force): i
            for (i, name, email) in indexed
        }
        for fut in as_completed(future_to_idx):
            i = future_to_idx[fut]
            try:
                results[i] = fut.result()
            except Exception as e:  # defense-in-depth — _resolve_one_for_batch shouldn't raise
                results[i] = {
                    "email": indexed[i][2].strip().lower(),
                    "error": "resolver crashed",
                    "linkedin_url": None, "confidence": "none", "source": "none",
                    "reasoning": f"Unhandled error: {type(e).__name__}",
                    "cached": False,
                }

    hits = sum(1 for r in results if r and r.get("linkedin_url"))
    cached_n = sum(1 for r in results if r and r.get("cached"))
    return jsonify({
        "results": results,
        "count": len(results),
        "hits": hits,
        "cached": cached_n,
    })


@app.route("/candidates/resolve", methods=["POST"])
def candidates_resolve():
    """Resolve a single (name, email) pair to a LinkedIn URL.

    Body: {"name": "...", "email": "...", "force": false}
    Returns: full resolver result dict + a `cached` flag.

    Cache-first: a fresh row in `linkedin_resolutions` (within
    RESOLUTION_CACHE_TTL) is returned immediately without calling Apollo or
    Google. Pass `force: true` to skip the cache.
    """
    if not request.is_json:
        return jsonify({"error": "JSON body required (Content-Type: application/json)"}), 400
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()[:RESOLVE_NAME_MAX_LEN]
    email = (body.get("email") or "").strip()[:RESOLVE_EMAIL_MAX_LEN]
    force = bool(body.get("force"))

    if not name or not email:
        return jsonify({"error": "name and email are required"}), 400
    if not _looks_like_email(email):
        return jsonify({"error": "email is malformed"}), 400

    if not force:
        cached = db_get_resolution(email)
        if cached is not None:
            return jsonify(cached)

    keys = _load_resolver_keys()
    result = resolve_linkedin(
        name, email,
        apollo_key=keys.get("apollo_api_key") or None,
        cse_key=keys.get("google_cse_api_key") or None,
        cse_id=keys.get("google_cse_id") or None,
        openai_key=keys.get("openai_api_key") or None,
    )
    # db_put_resolution reads result["_transient"] internally to decide
    # whether to cache. Strip it before responding so the implementation
    # detail doesn't leak into the public JSON.
    db_put_resolution(email, name, result)
    result.pop("_transient", None)
    # Match the cached-response shape exactly so callers can index either
    # branch without a shape check.
    return jsonify({
        "email": email.strip().lower(),
        "name": name,
        "resolved_at": int(time.time()),
        **result,
        "cached": False,
    })


# Kick off the scheduled-sync daemon on module import (fires every
# SYNC_INTERVAL_HOURS in addition to the auto-trigger on first page load).
start_scheduled_sync()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
