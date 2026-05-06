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

# In-memory caches for the lightweight endpoints (targets list, tags list, /me).
_targets_cache = {"data": None, "error": None, "fetched_at": 0}
_tags_cache = {"data": None, "error": None, "fetched_at": 0}
_me_cache = {"data": None, "error": None, "fetched_at": 0}
ME_CACHE_TTL = 600

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
    """GET /me -> the customer + the API-key owner's user record. Cached.

    Returns (data_dict, error). data_dict has keys: customer (with id, name)
    and user (with id, firstName, lastName, linkedinUrl). The user.id is
    what shows up in Connection.owners[] for paths "owned by you".
    """
    now = time.time()
    if (
        not force
        and _me_cache["data"] is not None
        and (now - _me_cache["fetched_at"]) < ME_CACHE_TTL
    ):
        return _me_cache["data"], _me_cache["error"]
    if not API_KEY:
        return None, "DRAFTBOARD_API_KEY not set."
    try:
        r = requests.get(f"{API_BASE}/me", headers=_auth_headers(), timeout=10)
        if r.status_code != 200:
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
        _me_cache.update({"data": result, "error": None, "fetched_at": now})
        return result, None
    except requests.RequestException as e:
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


# Kick off the scheduled-sync daemon on module import (fires every
# SYNC_INTERVAL_HOURS in addition to the auto-trigger on first page load).
start_scheduled_sync()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False)
