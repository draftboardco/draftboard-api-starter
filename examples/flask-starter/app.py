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
    print("[draftboard-starter] No API key found. Visit the /setup page to paste one,")
    print("  or set one of these and restart:")
    print("  - DRAFTBOARD_API_KEY environment variable")
    print("  - .env file in the app directory (DRAFTBOARD_API_KEY=db-api_...)")
    print("  - ~/.draftboard-secrets/draftboard-api-starter (file containing the key)")


# Cache of the current API key so a paste in /setup is picked up live (next
# request, no server restart). 5-second TTL means a manual edit to the secrets
# file is also seen quickly.
#
# All in-app code that needs the current key MUST call _current_api_key()
# (which goes through this cache). Reading the module-global API_KEY directly
# bypasses the cache and can return a stale value just after a paste —
# fetch_me / fetch_all_targets / fetch_target_connections / fetch_tags all
# call _current_api_key() now, after an adversarial review caught the gap.
_API_KEY_CACHE_TTL = 5
_api_key_cache = {"fetched_at": 0}


def _current_api_key(force=False):
    """Return the current Draftboard API key, reloading from disk if the
    in-memory copy is older than _API_KEY_CACHE_TTL seconds (or if force=True
    after a paste-and-save). Source-of-truth helper used by _auth_headers()."""
    global API_KEY, _api_key_source
    now = time.time()
    if not force and (now - _api_key_cache["fetched_at"]) < _API_KEY_CACHE_TTL:
        return API_KEY
    new_key, new_source = _load_api_key()
    API_KEY = new_key
    _api_key_source = new_source
    _api_key_cache["fetched_at"] = now
    return API_KEY


def _save_api_key_to_secrets(key: str) -> None:
    """Write a Draftboard API key to ~/.draftboard-secrets/draftboard-api-starter
    atomically. Reuses the exact 0600 + atomic-rename pattern as
    _save_resolver_keys so a crash mid-write can't leave the file truncated
    or world-readable. After the write, callers should invoke
    _current_api_key(force=True) to pick the new value up live."""
    secrets_dir = os.path.expanduser("~/.draftboard-secrets")
    secrets_path = os.path.join(secrets_dir, "draftboard-api-starter")
    os.makedirs(secrets_dir, mode=0o700, exist_ok=True)
    tmp = secrets_path + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write((key or "").strip() + "\n")
    os.replace(tmp, secrets_path)


def _flatten_me_payload(raw: dict) -> dict:
    """Reduce the nested /me API response to the six flat fields every
    consumer of `app_state.me_data` expects (`customer_id`, `customer_name`,
    `user_id`, `user_first`, `user_last`, `user_linkedin`). Persisting the
    raw response would break `get_my_owner_id`, the owner filter, the Slack
    test message identity line, and the Setup card's "Connected as X" line —
    they all read the flat keys.
    """
    customer = (raw or {}).get("customer") or {}
    user = customer.get("user") or {}
    return {
        "customer_id": customer.get("id"),
        "customer_name": customer.get("name") or "",
        "user_id": user.get("id"),
        "user_first": user.get("firstName") or "",
        "user_last": user.get("lastName") or "",
        "user_linkedin": user.get("linkedinUrl") or "",
    }


def _validate_api_key(key: str, timeout: int = 10):
    """Try GET /me with the given key. Returns (ok, me_or_reason).
    - On 200: (True, me_dict_with_user+org)
    - On 401/403: (False, "rejected")
    - On 429: (False, "rate_limited")
    - On any other HTTP: (False, "http_<code>")
    - On network/timeout: (False, "network")
    """
    try:
        r = requests.get(
            f"{API_BASE}/me",
            headers={
                "Authorization": f"Bearer {(key or '').strip()}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
    except requests.RequestException:
        return False, "network"
    if r.status_code == 200:
        try:
            return True, r.json()
        except ValueError:
            return False, "bad_response"
    if r.status_code in (401, 403):
        return False, "rejected"
    if r.status_code == 429:
        return False, "rate_limited"
    return False, f"http_{r.status_code}"
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
# AUTO_SYNC_ENABLED only gates AUTOMATIC bulk operations:
#   - the scheduled background daemon (every SYNC_INTERVAL_HOURS)
#   - the on-page-load auto-trigger (when fresh-cache count < target count)
# It does NOT block per-target on-demand fetches (drawer opens) or manual
# /sync/start clicks — those always work as long as an API key is set.
# Default is OFF: restart-time API hammering for customers with 4k+
# targets was a footgun. Pages render from cached data.db; the user clicks
# "Sync paths" in the nav to bulk-fetch new data, and drawers fetch on
# demand. Set AUTO_SYNC_ENABLED=true to re-enable the daemon + auto-trigger.
AUTO_SYNC_ENABLED = os.environ.get("AUTO_SYNC_ENABLED", "false").strip().lower() not in ("false", "0", "no", "off")

app = Flask(__name__)
# Cap request bodies at 2 MiB. Without this, a malicious or malformed payload
# to /candidates/resolve/batch (or any other endpoint) can balloon the
# customer's laptop process. The kit's normal request bodies are tiny
# (form posts, small JSON), so 2 MiB is generous.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024


# Kit version string — used in feedback emails so Zach knows which commit
# the customer is running. Read once at boot from the running git repo;
# falls back to "unknown" if git isn't available (e.g., the kit was
# downloaded as a zip).
def _read_kit_version():
    try:
        import subprocess
        app_dir = os.path.dirname(os.path.abspath(__file__))
        out = subprocess.check_output(
            ["git", "-C", app_dir, "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL, timeout=2,
        )
        return out.decode().strip() or "unknown"
    except Exception:
        return "unknown"


KIT_VERSION = _read_kit_version()


@app.context_processor
def _inject_globals():
    """Make feedback_mailto + kit_version available in every rendered
    template without each route passing them through. Reads the cached /me
    payload to prefill identity in the feedback email body so Zach knows
    which customer the report came from."""
    me_data = {}
    raw = db_app_state_get("me_data")
    if raw:
        try:
            me_data = json.loads(raw) or {}
        except (ValueError, TypeError):
            me_data = {}
    customer_name = (me_data.get("customer_name") or "").strip()
    full_name = f"{me_data.get('user_first') or ''} {me_data.get('user_last') or ''}".strip()
    # Customers reporting "I can't paste my API key" by definition have no /me
    # data yet — the placeholder text in those rows should match that reality
    # rather than a generic "(not loaded)" that reads like a bug.
    customer_label = customer_name or "(no Draftboard API key configured yet)"
    name_label = full_name or "(no Draftboard API key configured yet)"
    body_lines = [
        "Hi Zach,",
        "",
        "[Describe what's working, not working, or what you'd like to see]",
        "",
        "— Auto-filled context —",
        f"Draftboard customer: {customer_label}",
        f"My name on the account: {name_label}",
        f"Kit version: {KIT_VERSION}",
        "",
        "Thanks!",
    ]
    from urllib.parse import quote
    # quote() the email address so an attacker-controlled KIT_AUTHOR_EMAIL
    # env value can't inject a second `&body=` param that overrides ours.
    safe_email = quote(KIT_AUTHOR_EMAIL, safe="@")
    feedback_mailto = (
        f"mailto:{safe_email}"
        f"?subject={quote('Draftboard kit feedback / bug report')}"
        f"&body={quote(chr(10).join(body_lines))}"
    )
    return {
        "feedback_mailto": feedback_mailto,
        # Used by the nav sync pill: when no API key is set, the idle CTA
        # links to /setup instead of POSTing /sync/start (which would
        # silently no-op).
        "global_api_key_set": bool(API_KEY),
        # Status dropdown menu shares one meta dict across every connector
        # card render — no need for every route to pass it explicitly.
        "intro_status_meta_all": INTRO_STATUS_META,
    }

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
    # User clicked "Stop" — workers check this between API calls and bail out
    # without firing more requests. Cleared atomically at the start of every
    # new sync (inside start_sync's lock, before running=True).
    "stop_requested": False,
    # True if the last completed sync was halted via stop_requested. The nav
    # pill JS reads this to render the success message correctly ("⏸ Stopped"
    # vs "✓ All synced"). Cleared by the next sync's run.
    "last_run_stopped": False,
    # "incremental" (default — only never-cached targets) or "full" (every
    # target, regardless of cache state). Surfaced for the UI label.
    "mode": "incremental",
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
        # Status tracking columns (added later). Existing rows get
        # 'requested' as the default so the legacy "Mark as requested"
        # toggle still produces a usable status without a backfill query.
        try:
            conn.execute("ALTER TABLE intro_requests ADD COLUMN status TEXT NOT NULL DEFAULT 'requested'")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE intro_requests ADD COLUMN last_updated_at INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intro_requests_status ON intro_requests(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_intro_requests_target ON intro_requests(target_id)")

        # Local editable per-target tags. Layered ON TOP of the Draftboard
        # API's read-only tag taxonomy — users get to add their own labels
        # without touching the API-driven tag list. tag_type splits user-
        # typed tags ('user') from auto-applied date tags ('upload_date')
        # so each surfaces in its own filter dropdown.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS target_tags (
                target_id TEXT NOT NULL,
                tag TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (target_id, tag)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_tags_tag ON target_tags(tag)")
        try:
            conn.execute("ALTER TABLE target_tags ADD COLUMN tag_type TEXT NOT NULL DEFAULT 'user'")
        except sqlite3.OperationalError:
            pass
        conn.execute("CREATE INDEX IF NOT EXISTS idx_target_tags_type ON target_tags(tag_type)")
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

        # Slack assignment config. Stores webhook URL, channel display name,
        # setup completion timestamps, etc. Single-tenant — one Slack
        # destination per Flask install. Schema is intentionally a small
        # key/value store so we can add new keys (e.g., bot token for v2
        # two-way reactions) without migrations.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS slack_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            )
        """)

        # Per-teammate map: owner_id (from Connection.owners[]) → Slack user ID
        # and email. Shared by:
        #   - Slack assign feature (uses slack_user_id for @-mentions)
        #   - Existing Compose-email-to-teammate flow (uses email to pre-fill
        #     the Gmail compose URL's `to=` field — the field has been empty
        #     since that feature shipped because the Draftboard API doesn't
        #     expose Member emails).
        # Empty-string defaults so a teammate can have just one of the two
        # values populated (e.g., email mapped but no Slack ID).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS team_members (
                owner_id TEXT PRIMARY KEY,
                slack_user_id TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
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

        # Per-supporter relationship category (customer / investor / vendor /
        # friend / coworker / unclassified). Populated by the categorizer
        # which uses, in priority order: manual override > user-uploaded
        # match rules > built-in heuristics > LLM fallback (gpt-4o-mini if
        # OpenAI key is configured) > unclassified.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidate_categories (
                email TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                confidence TEXT NOT NULL,
                source TEXT NOT NULL,
                reasoning TEXT,
                classified_at INTEGER NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_candidate_categories_category ON candidate_categories(category)")

        # User-uploaded match rules feeding the categorizer. The customer
        # pastes lists of (names | domains | emails) per category at
        # /settings/categorization. Each row is one rule. Looked up in
        # the categorizer with simple case-insensitive matching.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS category_rules (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                rule_type TEXT NOT NULL,
                value TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                UNIQUE(category, rule_type, value)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_category_rules_value ON category_rules(value)")

        # Manual path lists — uploaded CSVs from external network owners
        # (typically the customer's investors). Each list is one upload;
        # owner_* columns describe the person whose connections are in the
        # CSV and double as the "connector" identity on the target drawer
        # when one of their connections matches a target by LinkedIn URL.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_path_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                label TEXT NOT NULL,
                owner_first TEXT NOT NULL DEFAULT '',
                owner_last TEXT NOT NULL DEFAULT '',
                owner_email TEXT NOT NULL DEFAULT '',
                owner_title TEXT NOT NULL DEFAULT '',
                owner_company TEXT NOT NULL DEFAULT '',
                owner_linkedin TEXT NOT NULL DEFAULT '',
                detected_columns TEXT NOT NULL DEFAULT '{}',
                row_count INTEGER NOT NULL DEFAULT 0,
                skipped_count INTEGER NOT NULL DEFAULT 0,
                uploaded_at INTEGER NOT NULL
            )
        """)

        # One row per (list, contact) pair. linkedin_url_normalized is the
        # match key against targets_cache. Original raw URL is preserved for
        # display + auditing.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS manual_path_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL,
                first_name TEXT NOT NULL DEFAULT '',
                last_name TEXT NOT NULL DEFAULT '',
                email TEXT NOT NULL DEFAULT '',
                company TEXT NOT NULL DEFAULT '',
                position TEXT NOT NULL DEFAULT '',
                connected_on TEXT NOT NULL DEFAULT '',
                linkedin_url TEXT NOT NULL DEFAULT '',
                linkedin_url_normalized TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (list_id) REFERENCES manual_path_lists(id) ON DELETE CASCADE
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_path_connections_url ON manual_path_connections(linkedin_url_normalized)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_manual_path_connections_list ON manual_path_connections(list_id)")

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
    """Persist the /targets list to SQLite. Idempotent — INSERT OR REPLACE per id.
    First-time targets also get an auto-applied YYYY-MM-DD tag (tag_type=
    'upload_date') so the Upload date filter has something to slice on.
    Re-saving an existing target preserves its original date — we only
    tag IDs that weren't already in targets_cache."""
    if not targets:
        return
    import datetime as _dt
    now = int(time.time())
    today_str = _dt.datetime.now().strftime("%Y-%m-%d")
    with _db_lock, _db_connect() as conn:
        # Identify which incoming IDs are net-new so we can date-tag them
        # (and only them). One round-trip — cheap.
        incoming_ids = [t.get("id") for t in targets if t.get("id")]
        existing: set = set()
        if incoming_ids:
            placeholders = ",".join("?" for _ in incoming_ids)
            cur = conn.execute(
                f"SELECT target_id FROM targets_cache WHERE target_id IN ({placeholders})",
                incoming_ids,
            )
            existing = {row[0] for row in cur.fetchall()}
        new_ids = [tid for tid in incoming_ids if tid not in existing]

        for t in targets:
            tid = t.get("id")
            if not tid:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO targets_cache (target_id, data_json, fetched_at) VALUES (?, ?, ?)",
                (tid, json.dumps(t), now),
            )
        if new_ids:
            conn.executemany(
                "INSERT OR IGNORE INTO target_tags (target_id, tag, created_at, tag_type) "
                "VALUES (?, ?, ?, 'upload_date')",
                [(tid, today_str, now) for tid in new_ids],
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


# ---------- Slack config + team_members ----------
# Both tables underpin the Slack-assignment feature AND the email-prefill
# upgrade to the existing Compose-email-to-teammate flow.

def db_get_slack_config(key, default=""):
    """Read a Slack config value. Known keys: 'webhook_url', 'channel_name',
    'setup_completed_at', 'last_test_at'. Empty string when unset."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("SELECT value FROM slack_config WHERE key = ?", (key,))
        row = cur.fetchone()
        return row[0] if row else default


def db_set_slack_config(key, value):
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO slack_config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, str(value), int(time.time())),
        )
        conn.commit()


def db_clear_slack_config(preserve_channel=True):
    """Reset Slack config. By default keeps `channel_name` so the user
    doesn't have to retype it after rotating a webhook — only the
    webhook + completion timestamps are cleared. Call with
    preserve_channel=False to wipe everything.

    `team_members` is never touched here; email mappings are useful even
    without Slack."""
    with _db_lock, _db_connect() as conn:
        if preserve_channel:
            conn.execute(
                "DELETE FROM slack_config WHERE key != 'channel_name'"
            )
        else:
            conn.execute("DELETE FROM slack_config")
        conn.commit()


def slack_is_configured():
    """True iff a real-looking Slack webhook URL is saved AND at least one
    teammate is mapped to a Slack user ID. Drives whether the 💬 Slack row
    appears in the Assign-to-teammate dropdown.

    The webhook shape check matters: a partially-completed wizard or a stale
    DB write could leave a non-empty-but-bogus value here, and we don't want
    to surface a Slack action that we know will fail."""
    webhook = db_get_slack_config("webhook_url") or ""
    if not _SLACK_WEBHOOK_RE.match(webhook):
        return False
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM team_members WHERE slack_user_id != '' LIMIT 1"
        )
        return cur.fetchone() is not None


def db_get_team_member(owner_id):
    """Returns dict {slack_user_id, email} or None if no row exists."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT slack_user_id, email FROM team_members WHERE owner_id = ?",
            (owner_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {"slack_user_id": row[0] or "", "email": row[1] or ""}


def db_set_team_member(owner_id, slack_user_id=None, email=None):
    """Atomic upsert. Only updates fields explicitly passed (None = leave
    unchanged). Empty string is a real value (clears the field).

    The read-modify-write happens inside a single _db_lock block so two
    concurrent saves of the same owner can't clobber each other's edits.
    """
    if slack_user_id is None and email is None:
        return
    if not owner_id:
        return  # caller bug guard — refuse to write empty PK
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT slack_user_id, email FROM team_members WHERE owner_id = ?",
            (owner_id,),
        )
        row = cur.fetchone()
        existing_slack = (row[0] if row else "") or ""
        existing_email = (row[1] if row else "") or ""
        new_slack = existing_slack if slack_user_id is None else slack_user_id.strip()
        new_email = existing_email if email is None else email.strip()
        conn.execute(
            "INSERT OR REPLACE INTO team_members "
            "(owner_id, slack_user_id, email, updated_at) VALUES (?, ?, ?, ?)",
            (owner_id, new_slack, new_email, int(time.time())),
        )
        conn.commit()


def db_all_team_members():
    """Returns dict keyed by owner_id of {slack_user_id, email}. Used by
    settings page rendering and by the assignment-flow lookups."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT owner_id, slack_user_id, email FROM team_members"
        )
        return {
            row[0]: {"slack_user_id": row[1] or "", "email": row[2] or ""}
            for row in cur.fetchall()
        }


# ---------- LinkedIn resolver cache ----------

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
    # New resolution may unlock a supporter→connector match — drop the cache.
    invalidate_supporter_attribution_cache()


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
    """Canonicalize a LinkedIn URL for fuzzy match.

    Strips: scheme (http/https), `www.` prefix, query string, anchor, trailing
    slash. Lowercases everything. So all of these compare equal:
        https://www.linkedin.com/in/orencharnoff/
        https://linkedin.com/in/orencharnoff
        http://www.linkedin.com/in/orencharnoff?utm_source=email
        linkedin.com/in/orencharnoff#anchor
    Result: "linkedin.com/in/orencharnoff".

    The `www.`-stripping matters because Apollo, Google CSE, and LinkedIn
    itself emit URLs with and without the prefix interchangeably. Without
    this, the supporter-badge cross-reference silently misses ~half its
    real matches when one side has `www.` and the other doesn't.
    """
    if not url:
        return ""
    u = url.strip().lower()
    u = u.split("?", 1)[0].split("#", 1)[0]
    # Strip scheme (handles http://, https://, schema-relative //)
    if u.startswith("https://"):
        u = u[8:]
    elif u.startswith("http://"):
        u = u[7:]
    elif u.startswith("//"):
        u = u[2:]
    # Strip leading www.
    if u.startswith("www."):
        u = u[4:]
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


# Six-state intro funnel. Kept compact so the UI dropdown stays usable.
# Order matters: rollup uses the FIRST hit on this priority list for a
# target's badge (so "intro_made" beats "requested" beats nothing).
INTRO_STATUS_ORDER = [
    "intro_made",          # success — intro happened
    "in_progress",         # connector replied / intro in flight
    "requested",           # user asked the connector, awaiting reply
    "no_reply",            # connector ignored / gave up
    "connector_rejected",  # connector said no
    "prospect_rejected",   # target said no via connector
]
INTRO_STATUS_VALID = set(INTRO_STATUS_ORDER)

# Display metadata for badges + dropdown labels. Kept in Python (not the
# template) so the connector card + target row + filter UI all stay in
# sync without three copies of the same dict.
INTRO_STATUS_META = {
    "requested":          {"label": "Requested",        "icon": "📨",
                            "badge_class": "bg-indigo-50 text-indigo-800 border-indigo-200",
                            "active": True},
    "in_progress":        {"label": "In progress",      "icon": "🔄",
                            "badge_class": "bg-blue-50 text-blue-800 border-blue-200",
                            "active": True},
    "intro_made":         {"label": "Intro made",       "icon": "✓",
                            "badge_class": "bg-emerald-50 text-emerald-800 border-emerald-200",
                            "active": False},
    "no_reply":           {"label": "No reply",         "icon": "🤐",
                            "badge_class": "bg-slate-50 text-slate-700 border-slate-200",
                            "active": False},
    "connector_rejected": {"label": "Connector passed", "icon": "✕",
                            "badge_class": "bg-rose-50 text-rose-800 border-rose-200",
                            "active": False},
    "prospect_rejected":  {"label": "Prospect passed",  "icon": "✕",
                            "badge_class": "bg-rose-50 text-rose-800 border-rose-200",
                            "active": False},
}


def db_intro_request_toggle(target_id, connection_id):
    """Toggle existence of an intro request. Adds with status='requested'
    on first call, clears the row entirely on second call. Returns the
    new state (True = requested, False = cleared). Used by the legacy
    'Mark as requested' button — status mutation goes through
    db_intro_request_set_status instead."""
    now = int(time.time())
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
            "INSERT INTO intro_requests (target_id, connection_id, requested_at, status, last_updated_at) "
            "VALUES (?, ?, ?, 'requested', ?)",
            (target_id, connection_id, now, now),
        )
        conn.commit()
        return True


def db_intro_request_set_status(target_id, connection_id, status):
    """Upsert an intro request with the given status. Returns the persisted
    status string, or '' if the status was invalid. Marks `last_updated_at`
    on every change so future timeline UI can show 'requested 3 days ago,
    in progress 1 day ago'."""
    if status not in INTRO_STATUS_VALID:
        return ""
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT 1 FROM intro_requests WHERE target_id = ? AND connection_id = ?",
            (target_id, connection_id),
        )
        if cur.fetchone():
            conn.execute(
                "UPDATE intro_requests SET status = ?, last_updated_at = ? "
                "WHERE target_id = ? AND connection_id = ?",
                (status, now, target_id, connection_id),
            )
        else:
            conn.execute(
                "INSERT INTO intro_requests (target_id, connection_id, requested_at, status, last_updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (target_id, connection_id, now, status, now),
            )
        conn.commit()
    return status


def db_intro_request_clear(target_id, connection_id):
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM intro_requests WHERE target_id = ? AND connection_id = ?",
            (target_id, connection_id),
        )
        conn.commit()
        return cur.rowcount > 0


def db_intro_requests_for_target(target_id):
    """{connection_id} for backwards-compat with the legacy callers that
    only need 'is this requested at all'. Status-aware callers should use
    db_intro_requests_status_map_for_target instead."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT connection_id FROM intro_requests WHERE target_id = ?",
            (target_id,),
        )
        return {row[0] for row in cur.fetchall()}


def db_intro_requests_status_map_for_target(target_id):
    """{connection_id → status} for one target. Used by the target drawer
    so each connector card knows what to pre-select in its status dropdown."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT connection_id, status FROM intro_requests WHERE target_id = ?",
            (target_id,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def db_intro_status_rollup_map() -> dict:
    """{target_id → rollup_status} for every target with at least one
    intro_requests row. Rollup picks the FIRST hit per INTRO_STATUS_ORDER
    (so 'intro_made' beats 'requested'). Per-request cached in flask.g.
    Drives the badge on the Targets/Accounts list rows."""
    try:
        from flask import g
        cached = getattr(g, "_intro_status_rollup", None)
    except RuntimeError:
        cached = None
        g = None
    if cached is not None:
        return cached
    per_target: dict = {}
    with _db_lock, _db_connect() as conn:
        cur = conn.execute("SELECT target_id, status FROM intro_requests")
        for tid, status in cur.fetchall():
            per_target.setdefault(tid, set()).add(status)
    out: dict = {}
    for tid, statuses in per_target.items():
        for s in INTRO_STATUS_ORDER:
            if s in statuses:
                out[tid] = s
                break
    if g is not None:
        g._intro_status_rollup = out
    return out


# Per-target tag helpers. Tags are normalized to lowercase + length-capped
# so the lookup map stays small and the filter dropdown doesn't get clogged
# with case variants. user-typed tags are capped at TAG_PER_TARGET_CAP per
# target; auto-applied date tags bypass the cap.
TAG_MAX_LEN = 40
TAG_PER_TARGET_CAP = 20


def _normalize_tag(raw: str) -> str:
    s = " ".join((raw or "").split()).lower()
    return s[:TAG_MAX_LEN]


def db_add_target_tag(target_id: str, tag: str, tag_type: str = "user") -> bool:
    """INSERT a (target_id, tag) row. Returns True if added, False if it
    already existed or input was empty. Enforces TAG_PER_TARGET_CAP for
    user-typed tags only — auto-applied date tags bypass the cap so a
    target with 20 user tags still gets its date stamp."""
    tag = _normalize_tag(tag)
    if not tag or not target_id:
        return False
    if tag_type not in ("user", "upload_date"):
        tag_type = "user"
    with _db_lock, _db_connect() as conn:
        if tag_type == "user":
            cur = conn.execute(
                "SELECT COUNT(*) FROM target_tags WHERE target_id = ? AND tag_type = 'user'",
                (target_id,),
            )
            existing = cur.fetchone()[0]
            if existing >= TAG_PER_TARGET_CAP:
                return False
        try:
            conn.execute(
                "INSERT INTO target_tags (target_id, tag, created_at, tag_type) "
                "VALUES (?, ?, ?, ?)",
                (target_id, tag, int(time.time()), tag_type),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return False
    _invalidate_target_tags_cache()
    return True


def db_remove_target_tag(target_id: str, tag: str) -> bool:
    tag = _normalize_tag(tag)
    if not tag or not target_id:
        return False
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM target_tags WHERE target_id = ? AND tag = ? AND tag_type = 'user'",
            (target_id, tag),
        )
        conn.commit()
        removed = cur.rowcount > 0
    if removed:
        _invalidate_target_tags_cache()
    return removed


def db_tags_for_target(target_id: str) -> list:
    """User-typed editable tags only. Drawer chip editor reads this."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT tag FROM target_tags WHERE target_id = ? AND tag_type = 'user' "
            "ORDER BY created_at ASC",
            (target_id,),
        )
        return [row[0] for row in cur.fetchall()]


def db_target_tags_map() -> dict:
    """{target_id → [tag, ...]} for every user-tagged target. Per-request
    cached in flask.g."""
    try:
        from flask import g
        cached = getattr(g, "_target_tags_map", None)
    except RuntimeError:
        cached = None
        g = None
    if cached is not None:
        return cached
    out: dict = {}
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT target_id, tag FROM target_tags WHERE tag_type = 'user' "
            "ORDER BY created_at ASC"
        )
        for tid, tag in cur.fetchall():
            out.setdefault(tid, []).append(tag)
    if g is not None:
        g._target_tags_map = out
    return out


def _invalidate_target_tags_cache():
    try:
        from flask import g
        if hasattr(g, "_target_tags_map"):
            delattr(g, "_target_tags_map")
        if hasattr(g, "_target_upload_dates_map"):
            delattr(g, "_target_upload_dates_map")
    except RuntimeError:
        pass


def db_all_tags_with_counts() -> list:
    """Sorted [(tag, count)] across all targets (user-typed only)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT tag, COUNT(*) AS c FROM target_tags WHERE tag_type = 'user' "
            "GROUP BY tag ORDER BY c DESC, tag ASC"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def db_target_ids_with_tag(tag: str) -> set:
    """target_ids carrying a given user-typed tag (date-tag filter uses
    db_target_ids_with_upload_date instead)."""
    tag = _normalize_tag(tag)
    if not tag:
        return set()
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT target_id FROM target_tags WHERE tag = ? AND tag_type = 'user'",
            (tag,),
        )
        return {row[0] for row in cur.fetchall()}


def db_all_upload_dates_with_counts() -> list:
    """Sorted [(YYYY-MM-DD, count)] newest first. Drives the "Upload date"
    filter dropdown — when a target landed in the local cache, derived
    from db_save_targets_cache or the boot-time backfill."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT tag, COUNT(DISTINCT target_id) FROM target_tags "
            "WHERE tag_type = 'upload_date' GROUP BY tag ORDER BY tag DESC"
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def db_target_ids_with_upload_date(date_str: str) -> set:
    date_str = (date_str or "").strip()[:10]
    if not date_str:
        return set()
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT target_id FROM target_tags WHERE tag_type = 'upload_date' AND tag = ?",
            (date_str,),
        )
        return {row[0] for row in cur.fetchall()}


# Initialize DB on import + backfill the target_owners index from existing rows.
# (start_scheduled_sync() is called at the bottom of this module, after its
# definition — Python doesn't hoist.)
init_db()
backfill_target_owners()
backfill_connector_paths()


def _migrate_slack_channel_name_strip_hash():
    """One-shot migration: an earlier version stored channel_name with a
    leading '#' (e.g. "#warm-intros"). The display layer now adds '#' at
    render time, so any persisted '#'-prefixed value would render as
    "##warm-intros". Strip it once at boot."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT value FROM slack_config WHERE key = 'channel_name'"
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return
        if row[0].startswith("#"):
            cleaned = row[0].lstrip("#").strip()
            conn.execute(
                "UPDATE slack_config SET value = ?, updated_at = ? "
                "WHERE key = 'channel_name'",
                (cleaned, int(time.time())),
            )
            conn.commit()


_migrate_slack_channel_name_strip_hash()


def _backfill_target_upload_dates():
    """One-shot migration: for every cached target without an upload_date
    tag, derive a date from targets_cache.fetched_at and insert one.
    Gated on app_state['upload_date_backfill_done'] so subsequent boots
    skip the work."""
    if db_app_state_get("upload_date_backfill_done"):
        return
    import datetime as _dt
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT target_id FROM target_tags WHERE tag_type = 'upload_date'"
        )
        already_tagged = {row[0] for row in cur.fetchall()}
        cur = conn.execute("SELECT target_id, fetched_at FROM targets_cache")
        rows = cur.fetchall()
        now_ts = int(time.time())
        to_insert = []
        for tid, fetched_at in rows:
            if tid in already_tagged:
                continue
            try:
                date_str = _dt.datetime.fromtimestamp(int(fetched_at)).strftime("%Y-%m-%d")
            except (ValueError, OSError):
                date_str = _dt.datetime.now().strftime("%Y-%m-%d")
            to_insert.append((tid, date_str, now_ts, "upload_date"))
        if to_insert:
            conn.executemany(
                "INSERT OR IGNORE INTO target_tags (target_id, tag, created_at, tag_type) "
                "VALUES (?, ?, ?, ?)",
                to_insert,
            )
            conn.commit()
    db_app_state_set("upload_date_backfill_done", "1")


_backfill_target_upload_dates()


def _auth_headers():
    # Route through _current_api_key() so a /setup paste is picked up live
    # without restarting the server. The 5-second cache keeps disk I/O off
    # the hot path of every request.
    return {
        "Authorization": f"Bearer {_current_api_key()}",
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

    if not _current_api_key():
        cached, _err = _from_sqlite()
        if cached:
            _me_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        return None, "DRAFTBOARD_API_KEY not set and no cached /me data."

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
        result = _flatten_me_payload(r.json() or {})
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

    if not _current_api_key():
        cached = _from_sqlite()
        if cached is not None:
            _tags_cache.update({"data": cached, "error": None, "fetched_at": now})
            return cached, None
        return [], "Tags not yet cached and no API key set."

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
      2. If no API key → SQLite only, no API call ever
      3. SQLite cache, if present → return immediately (no API call). The
         daemon-driven sync OR a manual /sync/start (force=True) refreshes
         this. We don't want every page-load to re-paginate /targets.
      4. SQLite empty + API key set → API call, persist on success.
      5. API failure → fall back to SQLite (stale-but-better-than-nothing)
    """
    now = time.time()
    if (
        not force
        and _targets_cache["data"] is not None
        and (now - _targets_cache["fetched_at"]) < TARGETS_CACHE_TTL
    ):
        return _targets_cache["data"], _targets_cache["error"]

    # No API key: read from SQLite, no API call possible.
    if not _current_api_key():
        sqlite_targets, _ = db_load_targets_cache()
        if sqlite_targets:
            _targets_cache.update({"data": sqlite_targets, "error": None, "fetched_at": now})
            return sqlite_targets, None
        return [], "No API key set and no cached target list in SQLite. Add a key OR populate targets_cache."

    # Have API key. Prefer SQLite cache if present (avoids hammering /targets
    # on every page-load — a force=True call refreshes it).
    if not force:
        sqlite_targets, _ = db_load_targets_cache()
        if sqlite_targets:
            _targets_cache.update({"data": sqlite_targets, "error": None, "fetched_at": now})
            return sqlite_targets, None

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
    if it's within CONNECTIONS_CACHE_TTL seconds. When there's no API key,
    returns cached data even if stale (better than nothing) and never makes
    an API call. Per-target on-demand fetches are NOT gated by
    AUTO_SYNC_ENABLED — that flag only controls automatic bulk operations.
    A user clicking a target drawer should always cause a fetch when needed.
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

    # No API key: return whatever we have, even stale.
    if not _current_api_key():
        if cached_data is not None:
            return cached_data, None
        return [], "Connections not cached for this target and no API key set."

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
    # Honor a stop request — bail BEFORE making the API call. Workers in
    # the pool that haven't started yet drain harmlessly through here. The
    # ones already mid-fetch can't be cancelled, but the next batch won't
    # fire. End-to-end latency from "click Stop" to "API calls cease" is
    # bounded by SYNC_CONCURRENCY * (one in-flight request worth of time).
    with _sync_lock:
        if _sync_state["stop_requested"]:
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
    """Background worker: fetches and caches connections for targets in parallel.

    Default mode is INCREMENTAL: only fetches targets that have never been
    cached. Existing cached targets are left alone — restart cost = 0 API
    calls, post-import cost = N calls where N is the number of newly imported
    targets. `force_all=True` re-fetches every target regardless of cache
    state — used by the "Force full resync" action when the customer wants
    every connector list refreshed (rare).

    Concurrent fetches via ThreadPoolExecutor; SYNC_CONCURRENCY caps how
    many simultaneous /targets/{id}/connections calls hit the API.
    """
    targets, _err = fetch_all_targets()
    if not targets:
        with _sync_lock:
            _sync_state["running"] = False
            _sync_state["ended_at"] = int(time.time())
        return

    target_ids = [t.get("id") for t in targets if t.get("id")]
    # `db_count_fresh_connections` here counts targets we already have cached
    # data for (under the TTL). For the progress bar we treat ANY cached row
    # as already-done so the percentage reflects work-actually-needed, not
    # arbitrary TTL math.
    cached_count = db_count_fresh_connections(target_ids, ttl=10**12)  # effectively "ever cached"

    with _sync_lock:
        _sync_state["total"] = len(target_ids)
        _sync_state["completed"] = cached_count
        _sync_state["errors"] = 0
        _sync_state["started_at"] = int(time.time())
        _sync_state["ended_at"] = 0
        _sync_state["last_target_name"] = ""
        _sync_state["mode"] = "full" if force_all else "incremental"
        # NB: stop_requested is reset inside start_sync() (before this thread
        # gets scheduled), NOT here. Resetting here would lose stop clicks
        # that arrive in the window between start_sync() returning and this
        # worker thread entering its first lock.

    # Build the to_sync list:
    # - force_all=True  → every target
    # - force_all=False → only targets with NO cached connections at all
    #
    # Stale-but-cached targets are NOT re-fetched. The user can refresh a
    # specific target by opening its drawer (per-target TTL still applies
    # there), or click "Force full resync" to walk every target.
    to_sync = []
    for t in targets:
        tid = t.get("id")
        if not tid:
            continue
        if force_all:
            to_sync.append(t)
            continue
        cached_data, _fetched_at, cached_error = db_get_connections(tid)
        if cached_data is None or cached_error:
            to_sync.append(t)

    # Concurrent fetches. ThreadPoolExecutor handles the worker pool;
    # _db_lock inside fetch_target_connections serializes writes. Workers
    # check _sync_state["stop_requested"] before each API call and bail.
    with ThreadPoolExecutor(max_workers=SYNC_CONCURRENCY) as pool:
        futures = [pool.submit(_sync_one_target, t) for t in to_sync]
        for _f in as_completed(futures):
            pass  # progress is tracked inside _sync_one_target

    with _sync_lock:
        was_stopped = _sync_state["stop_requested"]
        _sync_state["running"] = False
        _sync_state["ended_at"] = int(time.time())
        _sync_state["stop_requested"] = False
        # Surfaced to the nav pill JS so the success message can branch
        # ("✓ All synced" vs "⏸ Stopped at N/M"). Cleared at the start of
        # the next sync.
        _sync_state["last_run_stopped"] = was_stopped
    # Persist the completion timestamp for the "Since last sync" filter on the
    # New Paths page. Only stamp it on a clean finish — a stop_requested run
    # didn't actually cover everything, so a partial timestamp would be a lie.
    if not was_stopped:
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
        # Reset stop_requested ATOMICALLY with running=True under the same
        # lock. Doing this in _sync_worker (after the thread is scheduled)
        # opens a race: a /sync/stop POST arriving in that window would set
        # stop_requested=True only to be clobbered to False by the worker.
        _sync_state["stop_requested"] = False
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

    # Pull user context. Empty strings if /settings/profile hasn't been
    # filled out. When set, the draft frames the ask around what the user's
    # company does instead of just "Happy to send a forwardable email."
    try:
        user_ctx = _user_context()
        user_company = user_ctx.get("user_company") or ""
        user_company_desc = user_ctx.get("user_company_description") or ""
    except (sqlite3.Error, RuntimeError):
        # DB-init race, missing app_state row, or "outside Flask context"
        # (e.g. _build_messages called from a test). Fall back to the
        # generic template rather than crash. Coding bugs in _user_context
        # itself bubble up unmodified so they stay visible.
        user_company = ""
        user_company_desc = ""

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

    # Build the "why we're reaching out" line from the user's profile. The
    # connector already knows the user (that's why we're asking them for an
    # intro), so DON'T introduce the user — instead, frame the ask around
    # what the user's company does and why the target specifically would
    # benefit. The company description, when set, trails as a "by the way"
    # parenthetical rather than a self-introduction sentence.
    why_line = ""
    if user_company:
        why_line = (
            f"Think what we're doing at {user_company} could really be useful "
            f"for {{TGT}}."
        )
    description_line = user_company_desc if (user_company and user_company_desc) else ""

    template_parts = [
        f"Hey {connector_first} - quick question: {detail}.\n\n",
        f"Any chance you'd be open to pinging {{TGT}} for a quick warm intro?",
    ]
    if why_line:
        template_parts.append(f" {why_line}")
    template_parts.append(" Happy to send a forwardable email.")
    if description_line:
        template_parts.append(f"\n\n({description_line})")
    template = "".join(template_parts)

    plain = template.replace("{TGT}", target_first)

    # Build the HTML version. Escape the surrounding text first so the
    # connector's name etc. is safe, then substitute {TGT} with an <a> tag.
    # URL-scheme guard: only emit a live link when the URL is plain http(s).
    # Manual-paths CSV upload accepts only li_person_re-matching URLs today,
    # but a future importer or restored data.db could land a `javascript:` or
    # `data:` URL in targets_cache. The Slack builder already guards this way
    # at _build_slack_assign_payload — match the pattern here too.
    if target_linkedin and target_linkedin.startswith(("http://", "https://")):
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


def _gmail_compose_url(subject, body, to=None):
    """Build a Gmail compose URL the user's browser can open in their logged-in
    account. `to` is optional; when provided, Gmail pre-fills the To: field."""
    from urllib.parse import quote
    parts = [
        "https://mail.google.com/mail/?view=cm&fs=1",
        f"&su={quote(subject)}",
        f"&body={quote(body)}",
    ]
    if to:
        parts.append(f"&to={quote(to)}")
    return "".join(parts)


def _name_from_linkedin_url(url: str) -> str:
    """Pull a human-readable name from a LinkedIn profile URL slug. Used as a
    display fallback for paste-mode targets that don't have first/last
    metadata from the API yet. Example: linkedin.com/in/yoav-oz → 'yoav oz'."""
    if not url:
        return ""
    norm = _normalize_linkedin(url)
    if "/in/" not in (norm or ""):
        return ""
    slug = norm.split("/in/", 1)[1].split("/", 1)[0]
    return slug.replace("-", " ").replace("_", " ").strip()


def _resolve_target_for_message(target: dict) -> dict:
    """Resolve a target's display fields (first/last/full/title/company/linkedin)
    using the same manual-path-metadata fallback chain as _build_messages.
    Pulled out so the bulk-intro builder produces matching wording without
    re-implementing the cascade.

    Priority order per field:
      1. target.firstName / lastName / position.title / position.companyName
         (whatever the Draftboard API gave us)
      2. manual_path_connections row matching the LinkedIn URL (when the
         target was paste-imported and someone uploaded a CSV containing
         the same URL)
      3. URL slug as last-resort first/last
    """
    first = (target.get("firstName") or "").strip()
    last = (target.get("lastName") or "").strip()
    linkedin = target.get("linkedinUrl") or ""
    company = ((target.get("position") or {}).get("companyName") or "").strip()
    title = ((target.get("position") or {}).get("title") or "").strip()

    if not (first and last and company and title):
        try:
            norm = _normalize_linkedin(linkedin)
            if norm:
                meta = _manual_path_metadata_map().get(norm)
                if meta:
                    first = first or (meta.get("first_name") or "")
                    last = last or (meta.get("last_name") or "")
                    company = company or (meta.get("company") or "")
                    title = title or (meta.get("position") or "")
        except RuntimeError:
            # Outside Flask context — skip the fallback.
            pass

    if not first:
        slug = _name_from_linkedin_url(linkedin)
        if slug:
            parts = slug.split()
            first = parts[0].title() if parts else ""
            if not last and len(parts) > 1:
                last = parts[-1].title()
    first = first or "them"
    full = f"{first} {last}".strip() if last else first
    return {
        "first": first,
        "last": last,
        "full": full,
        "title": title,
        "company": company,
        "linkedin": linkedin,
    }


def _build_bulk_intro_message(connector, targets):
    """Build a single intro-request message asking one connector for multiple
    intros at once. Returns the same shape as `_build_messages` so the drawer
    JS can drive the Compose-email + Copy buttons with the existing handlers.

    The connector is presumed to already know the user (that's why we're
    asking them for intros), so the message does NOT introduce the user.
    The user's company description, if set, lands as a parenthetical "by the
    way" footer rather than a self-introduction sentence."""
    connector_first = (connector.get("firstName") or "").strip() or "there"

    try:
        user_ctx = _user_context()
        user_company = (user_ctx.get("user_company") or "").strip()
        user_company_desc = (user_ctx.get("user_company_description") or "").strip()
    except (sqlite3.Error, RuntimeError):
        # Same narrow-catch logic as _build_messages.
        user_company = ""
        user_company_desc = ""

    resolved = [_resolve_target_for_message(t) for t in targets]

    def _descriptor(r):
        if r["title"] and r["company"]:
            return f"{r['title']} at {r['company']}"
        if r["company"]:
            return f"at {r['company']}"
        return r["title"] or ""

    plain_lines = []
    for r in resolved:
        descriptor = _descriptor(r)
        line = f"- {r['full']}"
        if descriptor:
            line += f" ({descriptor})"
        if r["linkedin"]:
            line += f": {r['linkedin']}"
        plain_lines.append(line)

    n = len(resolved)
    if n == 1:
        opener = (
            f"Hey {connector_first} - quick question: there's someone in your "
            f"network I'd love to chat with. Open to pinging them for a quick "
            f"warm intro?"
        )
    else:
        opener = (
            f"Hey {connector_first} - quick question: there are a few folks in "
            f"your network I'd love to chat with. Any of them open for a quick "
            f"warm intro?"
        )
    plain_parts = [opener, "", "\n".join(plain_lines), ""]
    if user_company:
        if n == 1:
            plain_parts.append(
                f"Think what we're doing at {user_company} could really be useful for them. "
                f"Happy to send a forwardable email if it feels like a fit - no worries "
                f"if it doesn't."
            )
        else:
            plain_parts.append(
                f"Think what we're doing at {user_company} could really be useful for them. "
                f"Happy to send forwardable emails for whichever ones feel like a fit - no "
                f"worries on any that feel awkward."
            )
    else:
        if n == 1:
            plain_parts.append(
                "Happy to send a forwardable email if it feels like a fit - no worries "
                "if it doesn't."
            )
        else:
            plain_parts.append(
                "Happy to send forwardable emails for whichever ones feel like a fit - no "
                "worries on any that feel awkward."
            )
    if user_company and user_company_desc:
        plain_parts.append("")
        plain_parts.append(f"({user_company_desc})")

    plain = "\n".join(plain_parts)

    # HTML version — URL-scheme guard on each <a href> (defense in depth
    # against a paste-imported target whose LinkedIn URL is somehow not
    # plain http(s); mirrors the per-target builder's guard).
    html_bullets = []
    for r in resolved:
        descriptor = _descriptor(r)
        descriptor_html = f" ({html.escape(descriptor)})" if descriptor else ""
        if r["linkedin"] and r["linkedin"].startswith(("http://", "https://")):
            name_html = (
                f'<a href="{html.escape(r["linkedin"], quote=True)}">'
                f"{html.escape(r['full'])}</a>"
            )
        else:
            name_html = html.escape(r["full"])
        html_bullets.append(f"<li>{name_html}{descriptor_html}</li>")

    html_intro = html.escape(opener)
    outro_pieces = []
    if user_company:
        if n == 1:
            outro_pieces.append(
                html.escape(
                    f"Think what we're doing at {user_company} could really be useful for them. "
                    f"Happy to send a forwardable email if it feels like a fit - no worries "
                    f"if it doesn't."
                )
            )
        else:
            outro_pieces.append(
                html.escape(
                    f"Think what we're doing at {user_company} could really be useful for them. "
                    f"Happy to send forwardable emails for whichever ones feel like a fit - no "
                    f"worries on any that feel awkward."
                )
            )
    else:
        if n == 1:
            outro_pieces.append(
                html.escape(
                    "Happy to send a forwardable email if it feels like a fit - no worries "
                    "if it doesn't."
                )
            )
        else:
            outro_pieces.append(
                html.escape(
                    "Happy to send forwardable emails for whichever ones feel like a fit - no "
                    "worries on any that feel awkward."
                )
            )
    if user_company and user_company_desc:
        outro_pieces.append(f"({html.escape(user_company_desc)})")

    html_body = (
        f"<p>{html_intro}</p>"
        f"<ul>{''.join(html_bullets)}</ul>"
        f"<p>{'<br><br>'.join(outro_pieces)}</p>"
    )

    if n == 1:
        subject = f"Intro to {resolved[0]['full']}?"
    else:
        subject = f"Quick intro request - {n} folks in your network"

    return {
        "plain": plain,
        "html": html_body,
        "plain_fallback": plain,
        "subject": subject,
        "count": n,
        "gmail_url": _gmail_compose_url(subject, plain),
    }


def _build_assign_to_teammate_draft(target, connection, teammate):
    """Generate a Gmail draft asking a teammate to ping the connector for an
    intro to the target. Used by the 'Assigned to' dropdown.

    Looks up the teammate's email in `team_members` (populated via the Team
    Settings page) — when present, the Gmail compose URL pre-fills the To:
    field. Otherwise the To: stays empty (existing behavior)."""
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
    teammate_id = teammate.get("id")

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

    # Look up the teammate's email + Slack ID so the Gmail compose URL can
    # pre-fill To:, and the Slack row only appears when we know the handle.
    teammate_email = ""
    teammate_slack_user_id = ""
    if teammate_id:
        member = db_get_team_member(teammate_id)
        if member:
            teammate_email = member.get("email") or ""
            teammate_slack_user_id = member.get("slack_user_id") or ""

    return {
        "teammate_id": teammate_id,
        "teammate_first": teammate_first,
        "teammate_last": (teammate.get("lastName") or "").strip(),
        "teammate_full": (
            f"{teammate_first} {(teammate.get('lastName') or '').strip()}"
        ).strip(),
        "teammate_initials": _initials(teammate.get("firstName"), teammate.get("lastName")),
        "teammate_email": teammate_email,
        "teammate_slack_user_id": teammate_slack_user_id,
        "subject": subject,
        "body": body,
        "gmail_url": _gmail_compose_url(subject, body, to=teammate_email or None),
    }


# Cache of {normalized_linkedin_url: [contributor_display, ...]} drawn from
# scanner-imported supporters that have been LinkedIn-resolved. Refreshed at
# most every _SUPPORTER_ATTRIBUTION_TTL seconds, so a drawer with hundreds of
# connections doesn't fire hundreds of identical JOIN queries.
_SUPPORTER_ATTRIBUTION_TTL = 60
_supporter_attribution_cache = {"data": None, "fetched_at": 0}


def db_supporter_attribution_map():
    """Returns dict {normalized_linkedin_url: [contributor_display, ...]}.

    For every (teammate-uploaded supporter, resolved LinkedIn URL) pair, lists
    the contributing teammates. Drives the "⭐ Supporter (Sarah's list)" badge
    on connector cards — when path data shows a connector who's also someone a
    teammate flagged via the scanner, we surface the overlap inline.

    Empty when no scanner imports have happened OR no resolutions have run.
    The data hard-depends on:
      1. Teammate ran the portable scanner → rows in `teammate_contacts`
      2. Those rows were LinkedIn-resolved (manual or batch on Supporters page)
         → matching rows in `linkedin_resolutions` with linkedin_url set

    Without (2), we can't match — the scanner gives us emails, the path API
    gives us LinkedIn URLs, and the resolver is the only bridge.
    """
    cache = _supporter_attribution_cache
    now = time.time()
    if cache["data"] is not None and (now - cache["fetched_at"]) < _SUPPORTER_ATTRIBUTION_TTL:
        return cache["data"]
    # Match the resolution-cache TTL applied by db_get_resolution so the
    # connector-card badge and the /supporters/linkedin-urls endpoint agree
    # on what's "current". Without this, a stale resolution shows the badge
    # but is silently dropped from the copy-URL list.
    cutoff = int(now) - RESOLUTION_CACHE_TTL
    out = {}
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT lr.linkedin_url, tc.contributor_name, tc.contributor_email "
            "FROM teammate_contacts tc "
            "JOIN linkedin_resolutions lr ON lr.email = tc.email "
            "WHERE lr.linkedin_url IS NOT NULL AND lr.linkedin_url != '' "
            "  AND lr.resolved_at >= ?",
            (cutoff,),
        )
        for url, contrib_name, contrib_email in cur.fetchall():
            key = _normalize_linkedin(url)
            if not key:
                continue
            display = (contrib_name or "").strip() or (contrib_email or "").strip()
            if not display:
                continue
            bucket = out.setdefault(key, [])
            if display not in bucket:
                bucket.append(display)
    cache["data"] = out
    cache["fetched_at"] = now
    return out


def invalidate_supporter_attribution_cache():
    """Drop the in-memory map so the next read rebuilds. Call this after any
    write that could change the result: a scanner import, or a successful
    LinkedIn resolution. Cheap; the rebuild is one indexed JOIN."""
    _supporter_attribution_cache["data"] = None
    _supporter_attribution_cache["fetched_at"] = 0


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
    # Normalize the legacy `requested_set` signature.
    if isinstance(requested_set, dict):
        intro_status = requested_set.get(cid, "")
    elif isinstance(requested_set, set):
        intro_status = "requested" if cid in requested_set else ""
    elif requested_set is None:
        intro_status = (
            "requested" if db_intro_request_get(target.get("id"), cid) else ""
        )
        if intro_status == "requested":
            with _db_lock, _db_connect() as conn:
                row = conn.execute(
                    "SELECT status FROM intro_requests WHERE target_id = ? AND connection_id = ?",
                    (target.get("id"), cid),
                ).fetchone()
                if row and row[0]:
                    intro_status = row[0]
    else:
        intro_status = ""
    is_requested = bool(intro_status)
    status_meta = INTRO_STATUS_META.get(intro_status) if intro_status else None

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
    # For manual-list paths, the list owner's email travels on the
    # connection dict as `_manual_list_email`. Pre-fill it as the To: so
    # the compose-email button is one click instead of one click + paste.
    manual_to = (connection.get("_manual_list_email") or "").strip()
    gmail_url = _gmail_compose_url(messages["subject"], messages["plain_fallback"], to=manual_to or None)

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

    # Slack rendering hint for the dropdown: the template only shows a
    # "💬 Slack #channel" row for a teammate when BOTH are true:
    #   1. the workspace has Slack configured (webhook URL + completed setup)
    #   2. that specific teammate has a slack_user_id mapped in team_members
    slack_on = slack_is_configured()
    slack_channel = (db_get_slack_config("channel_name") or "").strip() if slack_on else ""

    # Supporter cross-reference: is this connector someone a teammate has
    # flagged via the scanner? Match is on normalized LinkedIn URL, so it
    # only fires for resolved supporters (the scanner exports email-only).
    norm_li = _normalize_linkedin(connection.get("linkedinUrl") or "")
    supporter_attributions = (
        db_supporter_attribution_map().get(norm_li, []) if norm_li else []
    )

    return {
        "id": cid,
        "name": f"{first} {last}".strip() or "(no name)",
        "initials": _initials(first, last),
        "linkedinUrl": connection.get("linkedinUrl") or "",
        "title": pos.get("title") or "",
        "company": pos.get("companyName") or "",
        # Preserve None for manual paths (no enrichment data) so the
        # `c.score is not none` template guard hides the score pill.
        # Use direct .get() — falsy 0 from real Draftboard data is kept
        # as 0 (renders the "0" pill); None from manual paths stays None
        # (hides the pill). The previous `or 0` coalesce defeated this.
        "score": connection.get("score"),
        "score_details": connection.get("scoreDetails") or [],
        "humanized_details": humanized,
        "owners": raw_owners,
        "is_owned_by_me": is_owned_by_me,
        "other_owners": other_owners,
        "assign_drafts": assign_drafts,
        "slack_configured": slack_on,
        "slack_channel_name": slack_channel,
        "supporter_attributions": supporter_attributions,
        "draft_message": messages["plain"],
        "draft_html": messages["html"],
        "draft_plain_fallback": messages["plain_fallback"],
        "compose_subject": messages["subject"],
        "gmail_url": gmail_url,
        # Surface the To: address as a separate field so the composeEmail JS
        # can keep it when it rebuilds the URL (the JS calls preventDefault
        # and constructs a fresh URL from the data-* attrs, which dropped
        # the to= param baked into gmail_url before this field existed).
        "compose_to": manual_to,
        "requested": is_requested,
        "intro_status": intro_status,
        "intro_status_meta": status_meta,
    }


_LI_RE = re.compile(r"linkedin\.com/in/[^\s,/?#]+", re.IGNORECASE)


def normalize_linkedin_urls(raw: str):
    """Accept comma-, newline-, or tab-separated input. Normalize each entry to
    https://www.linkedin.com/in/<slug>.

    Tabs handled so that Excel / Google Sheets paste (which uses TAB as the
    cell separator) still produces one URL per cell when a row contains
    multiple LinkedIn URLs across columns. The client-side paste handler in
    import.html does this conversion in the browser; this is defense in depth
    for direct curl POSTs and older browsers."""
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[\n,\t]+", raw) if p.strip()]
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
        # Search the manual-path-derived company too, so paste-mode targets
        # whose company comes only from a CSV row still match search by
        # company name.
        _target_company_with_fallback(t),
        " ".join(t.get("tags") or []),
    ]).lower()
    return q_lower in haystack


def _enrich_target(t):
    position = t.get("position") or {}
    # Funnel rollup — the worst-case status across this target's intro
    # requests, picking 'intro_made' > 'in_progress' > 'requested' > the
    # negative outcomes. Drives the badge on the Targets/Accounts list rows.
    tid = t.get("id") or ""
    rollup_status = db_intro_status_rollup_map().get(tid, "")
    rollup_meta = INTRO_STATUS_META.get(rollup_status) if rollup_status else None
    # Merge Draftboard-API tags (read-only) with locally-added user tags
    # from target_tags. Dedup case-insensitively so an API "Investor" tag
    # doesn't double-up with a local "investor".
    api_tags = [str(x).strip() for x in (t.get("tags") or []) if str(x).strip()]
    editable_tags = db_target_tags_map().get(tid, []) if tid else []
    merged = []
    seen_lower = set()
    for tag in list(editable_tags) + api_tags:
        key = tag.lower()
        if key in seen_lower:
            continue
        seen_lower.add(key)
        merged.append(tag)
    return {
        "id": tid,
        "name": f"{t.get('firstName') or ''} {t.get('lastName') or ''}".strip() or "(no name)",
        "initials": _initials(t.get("firstName"), t.get("lastName")),
        "title": position.get("title") or "",
        # Display the manual-path-derived company when position is empty —
        # matches the bucket key used by /accounts and the connector drawer.
        "company": _target_company_with_fallback(t),
        "linkedinUrl": t.get("linkedinUrl") or "",
        "score": t.get("score") or 0,
        "connections_number": t.get("connectionsNumber") or 0,
        "tags": merged,
        "editable_tags": list(editable_tags),
        "status": t.get("status") or "",
        "updated_at": t.get("updatedAt") or "",
        "intro_status": rollup_status,
        "intro_status_meta": rollup_meta,
    }


@app.route("/", methods=["GET"])
def targets_view():
    # First-run nudge: brand-new install with no API key gets bounced to
    # /setup so they have a paste field instead of an empty Targets page.
    # The "Continue" action on /setup writes setup_dismissed in app_state.
    #
    # If the user later loses their API key (revoked, .env deleted, env var
    # unset), the dismiss flag would otherwise leave them stranded on a
    # broken Targets page with no nudge back to /setup. So: when the key
    # disappears, clear the dismiss flag and re-fire the redirect.
    if not _current_api_key():
        if db_app_state_get("setup_dismissed"):
            with _db_lock, _db_connect() as conn:
                conn.execute(
                    "DELETE FROM app_state WHERE key = ?",
                    ("setup_dismissed",),
                )
                conn.commit()
        return redirect(url_for("setup_view"))
    force_refresh = request.args.get("refresh") == "1"
    targets, error = fetch_all_targets(force=force_refresh)

    # Owner filter: ?owner=<member_id>  (or "me" to mean the current user)
    owner_filter = (request.args.get("owner") or "").strip()
    me_id = get_my_owner_id()
    resolved_owner_id = me_id if owner_filter == "me" else owner_filter
    if resolved_owner_id:
        owner_target_ids = db_target_ids_for_owner(resolved_owner_id)
        targets = [t for t in targets if t.get("id") in owner_target_ids]

    # "Has manual list match" filter — narrow to only targets where at least
    # one uploaded list has a connection with this target's LinkedIn URL.
    manual_only = request.args.get("manual_only") == "1"
    if manual_only:
        manual_summary = manual_path_match_summary()
        matched_ids = manual_summary["matched_target_ids"]
        targets = [t for t in targets if t.get("id") in matched_ids]

    # Local tag filter — show only targets carrying the given user-typed
    # tag from the target_tags table. Draftboard-API tags are intentionally
    # NOT in scope here (this is the personal-overlay filter).
    tag_filter = _normalize_tag(request.args.get("tag") or "")
    if tag_filter:
        tagged_ids = db_target_ids_with_tag(tag_filter)
        targets = [t for t in targets if t.get("id") in tagged_ids]

    # Upload-date filter — auto-applied YYYY-MM-DD tag per target.
    upload_date_filter = (request.args.get("upload_date") or "").strip()[:10]
    if upload_date_filter:
        dated_ids = db_target_ids_with_upload_date(upload_date_filter)
        targets = [t for t in targets if t.get("id") in dated_ids]

    # Intro-status filter. Special pseudo-statuses:
    #   "any"   → any tracked status (requested, in_progress, made, rejected)
    #   "open"  → still in flight (requested + in_progress)
    #   "none"  → no intro request row at all
    # Otherwise must be a valid INTRO_STATUS value.
    status_filter = (request.args.get("intro_status") or "").strip()
    if status_filter:
        rollup = db_intro_status_rollup_map()
        if status_filter == "any":
            targets = [t for t in targets if rollup.get(t.get("id"))]
        elif status_filter == "open":
            targets = [t for t in targets if rollup.get(t.get("id")) in ("requested", "in_progress")]
        elif status_filter == "none":
            targets = [t for t in targets if not rollup.get(t.get("id"))]
        elif status_filter in INTRO_STATUS_VALID:
            targets = [t for t in targets if rollup.get(t.get("id")) == status_filter]
        else:
            status_filter = ""

    # Search filter (case-insensitive substring across name/title/company/tags)
    q = (request.args.get("q") or "").strip()
    q_lower = q.lower()
    if q_lower:
        targets = [t for t in targets if _matches_query(t, q_lower)]

    # Sort. `?sort=score` (default) or `?sort=paths` — desc on both.
    # Secondary key keeps ordering stable when the primary ties.
    sort_by = (request.args.get("sort") or "score").strip().lower()
    if sort_by not in ("score", "paths"):
        sort_by = "score"
    if sort_by == "paths":
        targets.sort(
            key=lambda t: (t.get("connectionsNumber") or 0, t.get("score") or 0),
            reverse=True,
        )
    else:
        targets.sort(
            key=lambda t: (t.get("score") or 0, t.get("connectionsNumber") or 0),
            reverse=True,
        )

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
        manual_only=manual_only,
        manual_match_total=len(manual_path_match_summary()["matched_target_ids"]),
        intro_status_filter=status_filter,
        intro_status_meta=INTRO_STATUS_META,
        all_tags=db_all_tags_with_counts(),
        tag_filter=tag_filter,
        all_upload_dates=db_all_upload_dates_with_counts(),
        upload_date_filter=upload_date_filter,
        sort_by=sort_by,
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

    # Local tag filter (same as targets_view).
    tag_filter = _normalize_tag(request.args.get("tag") or "")
    if tag_filter:
        tagged_ids = db_target_ids_with_tag(tag_filter)
        targets = [t for t in targets if t.get("id") in tagged_ids]

    # Upload-date filter — auto-applied per target on first-seen.
    upload_date_filter = (request.args.get("upload_date") or "").strip()[:10]
    if upload_date_filter:
        dated_ids = db_target_ids_with_upload_date(upload_date_filter)
        targets = [t for t in targets if t.get("id") in dated_ids]

    # Intro-status filter — same semantics as targets_view (pseudo + real
    # values). Filters AT THE TARGET LEVEL pre-grouping so accounts with
    # zero surviving targets disappear.
    status_filter = (request.args.get("intro_status") or "").strip()
    if status_filter:
        rollup = db_intro_status_rollup_map()
        if status_filter == "any":
            targets = [t for t in targets if rollup.get(t.get("id"))]
        elif status_filter == "open":
            targets = [t for t in targets if rollup.get(t.get("id")) in ("requested", "in_progress")]
        elif status_filter == "none":
            targets = [t for t in targets if not rollup.get(t.get("id"))]
        elif status_filter in INTRO_STATUS_VALID:
            targets = [t for t in targets if rollup.get(t.get("id")) == status_filter]
        else:
            status_filter = ""

    # Group by company name (case-insensitive key, preserve display case).
    # Use the manual-path metadata fallback so paste-mode-added targets with
    # an empty `position.companyName` still bucket under their real company
    # when a manual_path_connections row supplies one. Without this, the
    # listing groups them as "(unknown company)" but the account drawer's
    # lookup (which also uses the fallback) can't find them — 404.
    accounts = {}
    for t in targets:
        position = t.get("position") or {}
        company = _target_company_with_fallback(t) or "(unknown company)"
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

    # Sort each account's targets by score desc + roll up the best intro
    # status across them so the row gets a single funnel pill.
    for a in page_accounts:
        a["targets"].sort(key=lambda x: x["score"], reverse=True)
        statuses = {t.get("intro_status") for t in a["targets"] if t.get("intro_status")}
        a["intro_status"] = ""
        a["intro_status_meta"] = None
        for s in INTRO_STATUS_ORDER:
            if s in statuses:
                a["intro_status"] = s
                a["intro_status_meta"] = INTRO_STATUS_META.get(s)
                break

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
        intro_status_filter=status_filter,
        intro_status_meta=INTRO_STATUS_META,
        all_tags=db_all_tags_with_counts(),
        tag_filter=tag_filter,
        all_upload_dates=db_all_upload_dates_with_counts(),
        upload_date_filter=upload_date_filter,
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


@app.route("/import/supporters", methods=["GET"])
def import_supporters_stub():
    """Stub page for the planned 'paste LinkedIn URLs of known supporters'
    flow. The real feature isn't built yet — this just renders a
    'Coming soon' card so the nav link doesn't dead-end. When the feature
    ships, this route swaps to a real form + handler."""
    return render_template(
        "import_supporters_stub.html",
        api_key_set=bool(API_KEY),
        active="import_supporters",
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
    elif not _current_api_key():
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
    # Augment with manual-list paths (from CSV uploads). They have the same
    # shape as Draftboard-API connections and flow through the same enricher
    # + template, so they're indistinguishable in the UI except for score=0.
    connections = list(connections) + manual_paths_for_target(target)
    connections.sort(key=lambda c: c.get("score") or 0, reverse=True)
    status_map = db_intro_requests_status_map_for_target(target_id)
    enriched_conns = [_enrich_connection(target, c, status_map) for c in connections]

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
    connections = list(connections) + manual_paths_for_target(target)
    connections.sort(key=lambda c: c.get("score") or 0, reverse=True)
    status_map = db_intro_requests_status_map_for_target(target_id)
    enriched = [_enrich_connection(target, c, status_map) for c in connections]
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

    # Match company using the same manual-path-metadata fallback the
    # accounts listing uses (see `_target_company_with_fallback`). Without
    # this, paste-mode targets that the listing groups under their
    # manual-path-derived company will 404 here.
    matching = []
    for t in targets_all:
        company = _target_company_with_fallback(t) or "(unknown company)"
        if company.lower() == key_lower:
            matching.append(t)

    if not matching:
        return ("<div class='p-6 text-rose-700'>No targets found for this account in the cache.</div>"), 404

    matching.sort(key=lambda t: t.get("score") or 0, reverse=True)

    # Pre-load the highest-scoring target's connectors for the initial right column.
    initial_panel = _connectors_panel_for_target(matching[0])
    enriched_targets = [_enrich_target(t) for t in matching]
    selected_id = matching[0].get("id")

    display_name = _target_company_with_fallback(matching[0]) or "(unknown company)"

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


@app.route("/target/<target_id>/tags", methods=["POST"])
def target_tags_add(target_id):
    """Add a user-typed tag to a target. JSON body: {tag: "investor"}.
    Tag is normalized to lowercase + length-capped at 40 chars.
    Returns the updated editable tag list."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site request blocked"}), 403
    data = request.get_json(silent=True) or {}
    tag = (data.get("tag") or "").strip()
    if not tag:
        return jsonify({"error": "missing tag"}), 400
    added = db_add_target_tag(target_id, tag)
    return jsonify({
        "ok": True,
        "added": added,
        "tag": _normalize_tag(tag),
        "tags": db_tags_for_target(target_id),
    })


@app.route("/target/<target_id>/tags/delete", methods=["POST"])
def target_tags_remove(target_id):
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site request blocked"}), 403
    data = request.get_json(silent=True) or {}
    tag = (data.get("tag") or "").strip()
    if not tag:
        return jsonify({"error": "missing tag"}), 400
    removed = db_remove_target_tag(target_id, tag)
    return jsonify({
        "ok": True,
        "removed": removed,
        "tags": db_tags_for_target(target_id),
    })


@app.route("/intro_requests/status", methods=["POST"])
def set_intro_request_status():
    """Set the status for an intro request. Empty status clears the row
    (treats this like 'unmark requested'). Returns the persisted status
    so the front-end can re-render the dropdown without a reload."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site request blocked"}), 403
    data = request.get_json(silent=True) or {}
    target_id = data.get("target_id")
    connection_id = data.get("connection_id")
    status = (data.get("status") or "").strip()
    if not target_id or not connection_id:
        return jsonify({"error": "missing target_id or connection_id"}), 400
    if not status:
        cleared = db_intro_request_clear(target_id, connection_id)
        return jsonify({"ok": True, "status": "", "cleared": cleared})
    if status not in INTRO_STATUS_VALID:
        return jsonify({"error": f"invalid status '{status}'"}), 400
    persisted = db_intro_request_set_status(target_id, connection_id, status)
    return jsonify({"ok": True, "status": persisted})


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
    # Build manual-list-derived "connectors" first so we can include them in
    # the total count + interleave them with Draftboard connectors. Each
    # uploaded list with >=1 target match becomes one row, with the list
    # owner as the connector. Cheap: cached for the request via Flask's g.
    summary = manual_path_match_summary()
    manual_connectors = []
    q_lower = (q or "").lower()
    for lid, data in summary["by_list"].items():
        if not data.get("count"):
            continue
        first = data.get("owner_first", "")
        last = data.get("owner_last", "")
        title = data.get("owner_title", "")
        company = data.get("owner_company", "")
        # Apply same case-insensitive substring search as Draftboard connectors
        if q_lower and q_lower not in (
            f"{first} {last} {title} {company}".lower()
        ):
            continue
        manual_connectors.append({
            "connector_key": f"manual:{lid}",
            "first": first, "last": last,
            "name": (f"{first} {last}").strip() or data.get("label", "(unnamed list)"),
            "linkedin": data.get("owner_linkedin", ""),
            "title": title, "company": company,
            "intro_count": data["count"],
            "top_score": None,        # template hides the score pill on None
            "is_manual": True,        # template adds a small "from list" subtitle
            "list_label": data.get("label", ""),
        })

    db_total = db_count_connectors(query=q or None)
    total = db_total + len(manual_connectors)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    offset = (page - 1) * PAGE_SIZE

    # Manual connectors render at the BOTTOM of the list (after Draftboard
    # connectors, sorted by intro_count). With ~5 manual lists max in a
    # realistic workspace, they fit on the last page or two without
    # disrupting the existing pagination math.
    db_connectors = db_list_connectors(query=q or None, limit=PAGE_SIZE, offset=offset)
    db_to_show = max(0, PAGE_SIZE - len(db_connectors))  # how many slots left
    if len(db_connectors) < PAGE_SIZE and len(manual_connectors) > 0:
        # Page is partially filled by Draftboard rows; pad with manual rows.
        manual_offset = max(0, offset - db_total)
        manual_slice = manual_connectors[manual_offset:manual_offset + db_to_show]
        connectors = db_connectors + manual_slice
    else:
        connectors = db_connectors

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
    company. Re-uses the connector-card UI but each card is one TARGET.

    Manual-list connector_keys have the prefix 'manual:<list_id>' and route
    to a different code path that builds paths from manual_path_connections
    + targets_cache instead of connector_paths. The output shape is
    identical so the same template handles both."""
    if connector_key.startswith("manual:"):
        return _connector_drawer_manual(connector_key)
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
        company = _target_company_with_fallback(target) or "(unknown company)"
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
        # `or 0` keeps None scores (manual paths, null-scored API rows) from
        # tripping the sort with a TypeError.
        items.sort(key=lambda x: x["card"]["score"] or 0, reverse=True)
        sorted_groups.append({
            "company": company,
            "items": items,
            "best_score": (items[0]["card"]["score"] or 0) if items else 0,
        })
    sorted_groups.sort(key=lambda g: g["best_score"] or 0, reverse=True)

    return render_template(
        "_drawer_connector.html",
        connector=connector_info,
        groups=sorted_groups,
        total_targets=len(paths),
    )


def _connector_drawer_manual(connector_key: str):
    """Drawer for a `manual:<list_id>` connector — the list owner. Lists every
    target the list-owner's CSV had a match against, grouped by company.
    Same template + output shape as the regular connector_drawer so they
    render identically."""
    try:
        list_id = int(connector_key.split(":", 1)[1])
    except (IndexError, ValueError):
        return ("<div class='p-6 text-rose-700'>Invalid manual list reference.</div>"), 404

    summary = manual_path_match_summary()
    list_data = summary["by_list"].get(list_id)
    if not list_data or not list_data.get("count"):
        return ("<div class='p-6 text-rose-700'>This list either was deleted "
                "or has no matches against your current targets.</div>"), 404

    targets_all, _ = fetch_all_targets()
    target_map = {t.get("id"): t for t in targets_all}
    me_id = get_my_owner_id()

    of = list_data.get("owner_first", "")
    ol = list_data.get("owner_last", "")
    connector_info = {
        "key": connector_key,
        "first": of,
        "last": ol,
        "name": (f"{of} {ol}").strip() or list_data.get("label", "(unnamed list)"),
        "initials": _initials(of, ol),
        "linkedin": list_data.get("owner_linkedin", ""),
        "title": list_data.get("owner_title", ""),
        "company": list_data.get("owner_company", ""),
    }

    grouped = {}
    paths_count = 0
    for tid in list_data["target_ids"]:
        target = target_map.get(tid)
        if not target:
            continue
        # Build a Connection-shaped dict — same path manual_paths_for_target
        # uses, so _enrich_connection produces the same connector card shape
        # as the target-drawer renders.
        manual_conn_list = manual_paths_for_target(target)
        # Multiple lists could match this target; pick the one for THIS list.
        manual_conn = next(
            (m for m in manual_conn_list if m.get("_manual_list_id") == list_id),
            None,
        )
        if manual_conn is None:
            continue
        paths_count += 1
        company = _target_company_with_fallback(target) or "(unknown company)"
        grouped.setdefault(company, []).append({
            "target": _enrich_target(target),
            "target_id": tid,
            "card": _enrich_connection(target, manual_conn, my_user_id=me_id),
        })

    # Manual paths have score=None — sort companies by upload recency / name
    # since there's no score to sort by. Within company, also alphabetical.
    sorted_groups = []
    for company, items in grouped.items():
        items.sort(key=lambda x: x["card"].get("name") or "")
        sorted_groups.append({
            "company": company,
            "items": items,
            "best_score": 0,  # template only uses this for sorting
        })
    sorted_groups.sort(key=lambda g: g["company"].lower())

    return render_template(
        "_drawer_connector.html",
        connector=connector_info,
        groups=sorted_groups,
        total_targets=paths_count,
    )


@app.route("/connector/<path:connector_key>/bulk-intro", methods=["POST"])
def connector_bulk_intro(connector_key):
    """Build a single intro-request message asking ONE connector for intros to
    MULTIPLE targets. Called from the connector drawer when the user checks
    boxes on N targets and clicks "Compose bulk intro." Returns JSON in the
    same shape as the per-target draft (subject/plain/html/plain_fallback/
    gmail_url + a count) so the existing Compose-email / Copy handlers can
    drive the result with no extra branching."""
    sec_fetch = request.headers.get("Sec-Fetch-Site")
    if sec_fetch and sec_fetch not in ("same-origin", "none"):
        return jsonify({"ok": False, "error": "cross-site"}), 403

    data = request.get_json(silent=True) or {}
    target_ids_raw = data.get("target_ids")
    if not isinstance(target_ids_raw, list) or not target_ids_raw:
        return jsonify({"ok": False, "error": "no_targets"}), 400
    target_ids = []
    seen = set()
    for tid in target_ids_raw:
        if not isinstance(tid, str):
            continue
        tid = tid.strip()
        if tid and tid not in seen:
            seen.add(tid)
            target_ids.append(tid)
        if len(target_ids) >= 20:  # hard cap to keep messages readable
            break
    if not target_ids:
        return jsonify({"ok": False, "error": "no_targets"}), 400

    # Resolve connector identity. Manual lists store owner data on the
    # list row; API connectors need a connection_id lookup against the
    # cached per-target JSON.
    connector = None
    if connector_key.startswith("manual:"):
        try:
            list_id = int(connector_key.split(":", 1)[1])
        except (IndexError, ValueError):
            return jsonify({"ok": False, "error": "bad_connector_key"}), 404
        summary = manual_path_match_summary()
        list_data = summary["by_list"].get(list_id)
        if not list_data:
            return jsonify({"ok": False, "error": "connector_not_found"}), 404
        connector = {
            "firstName": list_data.get("owner_first", ""),
            "lastName": list_data.get("owner_last", ""),
        }
    else:
        paths = db_targets_for_connector(connector_key)
        if not paths:
            return jsonify({"ok": False, "error": "connector_not_found"}), 404
        target_conns, _err = fetch_target_connections(paths[0]["target_id"])
        conn_obj = next(
            (c for c in target_conns if c.get("id") == paths[0]["connection_id"]),
            None,
        )
        if not conn_obj:
            return jsonify({"ok": False, "error": "connector_data_missing"}), 404
        connector = conn_obj

    targets_all, _ = fetch_all_targets()
    target_map = {t.get("id"): t for t in targets_all}
    targets = [target_map[tid] for tid in target_ids if tid in target_map]
    if not targets:
        return jsonify({"ok": False, "error": "no_matching_targets"}), 400

    msg = _build_bulk_intro_message(connector, targets)
    return jsonify({"ok": True, **msg})


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

    # Append manual-path matches to the new-paths feed. Each list's upload
    # timestamp acts as first_seen_at — a list uploaded today shows ALL its
    # matches under "Last 24h" / "Last 7d". Manual paths bypass the
    # min_score filter entirely (they're user-curated; the customer
    # uploaded them on purpose, no score gate makes sense). Owner filter
    # also bypassed — manual paths have only the customer as owner, so a
    # specific-teammate filter would always exclude them, which would feel
    # broken when toggling owner filters.
    summary = manual_path_match_summary()
    for list_id, ldata in summary["by_list"].items():
        list_uploaded = ldata.get("uploaded_at") or 0
        if list_uploaded < since_ts:
            continue
        for tid in ldata["target_ids"]:
            target = target_map.get(tid)
            if not target:
                continue
            manual_conn_list = manual_paths_for_target(target)
            manual_conn = next(
                (m for m in manual_conn_list if m.get("_manual_list_id") == list_id),
                None,
            )
            if manual_conn is None:
                continue
            enriched_conn = _enrich_connection(target, manual_conn, my_user_id=me_id)
            enriched_paths.append({
                "target": _enrich_target(target),
                "target_id": tid,
                "connection": enriched_conn,
                "first_seen_at": list_uploaded,
                "last_seen_at": list_uploaded,
            })
    # Re-sort the combined list: score desc, then first_seen_at desc. Manual
    # paths (score=None) sort to the bottom — same convention as connector
    # cards. Within the manual block, more recent uploads come first.
    enriched_paths.sort(
        key=lambda p: (p["connection"].get("score") or 0, p["first_seen_at"]),
        reverse=True,
    )

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


@app.route("/sync/stop", methods=["POST"])
def sync_stop():
    """Request a graceful halt of the running sync.

    Sets _sync_state["stop_requested"] = True. Workers in the ThreadPoolExecutor
    check this flag before each API call and return immediately. In-flight
    requests can't be cancelled — at most SYNC_CONCURRENCY more calls will fire
    before the pool drains. Per-target latency is bounded by one fetch.
    """
    with _sync_lock:
        if not _sync_state["running"]:
            return jsonify({"stopped": False, "reason": "not_running", "state": sync_progress_snapshot()})
        _sync_state["stop_requested"] = True
    return jsonify({"stopped": True, "state": sync_progress_snapshot()})


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
      4. oauth_client.json next to app.py (committed default — Desktop OAuth
         client embedded in the public kit, per Google's "Desktop client
         secret isn't really a secret" doc). This is the "clone and run"
         path for customers; kit authors override via #1-#3 during dev.

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

    # Lowest priority: the committed oauth_client.json (Desktop client baked
    # into the kit). When the kit author has populated it, this is the path
    # that gives customers a "clone and run" experience.
    embedded_path = os.path.join(app_dir, "oauth_client.json")
    if os.path.exists(embedded_path):
        try:
            with open(embedded_path) as f:
                data = json.load(f) or {}
            cid = (data.get("client_id") or "").strip()
            cs = (data.get("client_secret") or "").strip()
            if cid and cs:
                return cid, cs, "oauth_client.json (embedded)"
        except (OSError, ValueError):
            pass

    return "", "", None


GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, _google_creds_source = _load_google_oauth_client()
if GOOGLE_CLIENT_ID:
    print(f"[draftboard-starter] Loaded Google OAuth client from: {_google_creds_source}")
else:
    print("[draftboard-starter] No Google OAuth client configured. The Candidates feature will show a 'not connected' state until one is set.")


def _reload_google_oauth_client():
    """Re-run the priority-chain loader and update the module globals so a
    just-pasted credential pair is picked up live by the next request. Called
    by the POST handler that writes oauth_client.json."""
    global GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, _google_creds_source
    cid, cs, src = _load_google_oauth_client()
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, _google_creds_source = cid, cs, src
    return cid, cs, src


def _save_google_oauth_client_to_embedded_file(client_id: str, client_secret: str) -> None:
    """Write a Desktop OAuth client_id + client_secret into oauth_client.json
    next to app.py. Atomic temp-file + rename so a crash mid-write can't
    truncate the file. Reuses the pattern from _save_resolver_keys.

    Called by the /settings/google/configure-client paste handler. NOT a
    secret store — this file is committed to the public repo when the kit
    author distributes a populated copy. Per Google's own docs, the
    client_secret on a Desktop OAuth client is not really a secret."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    target = os.path.join(app_dir, "oauth_client.json")
    # Preserve the explanatory _comment if the existing file has one.
    existing_comment = ""
    if os.path.exists(target):
        try:
            with open(target) as f:
                existing = json.load(f) or {}
            existing_comment = existing.get("_comment", "") or ""
        except (OSError, ValueError):
            existing_comment = ""
    payload = {
        "_comment": existing_comment or (
            "Desktop OAuth client for the Draftboard API starter kit. Per "
            "Google's docs, the client_secret on a Desktop OAuth client is "
            "not really a secret — it's designed to be embedded in distributed "
            "software. The 100-user test-users allowlist on the consent screen "
            "is the actual gate."
        ),
        "client_id": (client_id or "").strip(),
        "client_secret": (client_secret or "").strip(),
    }
    tmp = target + ".tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, target)


# Customer-facing destination for "I need credentials" requests from the
# /settings/google paste form. Used to build a mailto: link with prefilled
# subject + body. Override via env if you fork this kit and want the
# requests to land somewhere else.
KIT_AUTHOR_EMAIL = os.environ.get("KIT_AUTHOR_EMAIL", "zach@draftboard.com").strip()


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


def db_query_candidates(limit=200, offset=0, query="", contributor="", source="", status_filter="active", category_filter=""):
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
        # Cross-teammate signal — for each email, how many DISTINCT teammate
        # contributors have it in their scanner upload? When >= 2, surface a
        # "Known by N teammates" badge on the row. Strong signal that the
        # supporter is well-connected across the org, not just to one person.
        teammate_count_cur = conn.execute(
            "SELECT email, COUNT(DISTINCT contributor_email) "
            "FROM teammate_contacts "
            "GROUP BY email "
            "HAVING COUNT(DISTINCT contributor_email) >= 2"
        )
        teammate_count_map = {r[0]: r[1] for r in teammate_count_cur.fetchall()}
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
            "teammate_count": teammate_count_map.get(email, 0),
            # Category populated lazily after the loop (avoids per-row DB hit).
            "category": "unclassified",
            "category_source": "default",
        })
    # Hydrate categories from the cache in one pass (single DB query, not
    # per-row). Misses stay 'unclassified' until the bulk categorize-now
    # button runs OR the user manually overrides one.
    if candidates:
        emails_in_page = [c["email"] for c in candidates]
        with _db_lock, _db_connect() as conn:
            placeholders = ",".join("?" * len(emails_in_page))
            cat_cur = conn.execute(
                f"SELECT email, category, source FROM candidate_categories "
                f"WHERE email IN ({placeholders})",
                emails_in_page,
            )
            cat_map = {r[0]: (r[1], r[2]) for r in cat_cur.fetchall()}
        for c in candidates:
            cat = cat_map.get(c["email"])
            if cat:
                c["category"] = cat[0]
                c["category_source"] = cat[1]

    # Category filter applied AFTER hydration so 'unclassified' actually
    # filters everything that hasn't been categorized.
    cat_filter = (category_filter or "").strip().lower()
    if cat_filter and cat_filter in CATEGORY_VALUES:
        candidates = [c for c in candidates if c["category"] == cat_filter]

    candidates.sort(key=lambda c: (-c["score"], -c["threads_count"], c["email"]))
    total = len(candidates)
    return candidates[offset:offset + limit], total


# ---- Categorization (customer / investor / vendor / friend / coworker) ----

CATEGORY_VALUES = ("customer", "investor", "vendor", "friend", "coworker", "unclassified")
CATEGORY_DISPLAY = {
    "customer":     ("🏢", "Customer",      "bg-emerald-100 text-emerald-800"),
    "investor":     ("💰", "Investor",      "bg-amber-100 text-amber-800"),
    "vendor":       ("🤝", "Vendor",        "bg-sky-100 text-sky-800"),
    "friend":       ("👋", "Friend",        "bg-pink-100 text-pink-800"),
    "coworker":     ("💼", "Coworker",      "bg-indigo-100 text-indigo-800"),
    "unclassified": ("•",  "Unclassified",  "bg-slate-100 text-slate-600"),
}

# Personal email TLDs — used by the heuristic to default to "friend" when
# no rule matches. Conservative list; covers ~95% of personal mailboxes.
_PERSONAL_EMAIL_DOMAINS = frozenset((
    "gmail.com", "googlemail.com", "icloud.com", "me.com", "mac.com",
    "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com",
    "aol.com", "msn.com", "protonmail.com", "proton.me", "pm.me",
    "fastmail.com", "fastmail.fm", "duck.com", "tutanota.com",
))


def db_save_category_rules(category: str, rule_type: str, values: list[str]):
    """REPLACE all rules of (category, rule_type) with the new value list.
    Empty strings are skipped. Domains and emails are normalized to lower-
    case (and domains have a leading '@' stripped) before insert so the
    UNIQUE constraint catches duplicates the user typed differently —
    'ACME.COM' and '@acme.com' and 'acme.com' all become 'acme.com'.
    Names are NOT lowercased on save (we display them as typed; matching
    is case-insensitive downstream)."""
    if category not in CATEGORY_VALUES or rule_type not in ("name", "domain", "email"):
        return
    cleaned = []
    seen_norm = set()
    for v in (values or []):
        if not v:
            continue
        s = v.strip()
        if not s:
            continue
        if rule_type == "domain":
            s = s.lower().lstrip("@").strip()
        elif rule_type == "email":
            s = s.lower()
        # name: leave as typed
        if not s:
            continue
        # Dedupe within the submitted list itself (in case the user pasted
        # the same value twice with different casing).
        norm_key = s.lower() if rule_type != "name" else s
        if norm_key in seen_norm:
            continue
        seen_norm.add(norm_key)
        cleaned.append(s)
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "DELETE FROM category_rules WHERE category = ? AND rule_type = ?",
            (category, rule_type),
        )
        for v in cleaned:
            conn.execute(
                "INSERT OR IGNORE INTO category_rules (category, rule_type, value, created_at) "
                "VALUES (?, ?, ?, ?)",
                (category, rule_type, v, now),
            )
        conn.commit()


def db_get_category_rules() -> dict:
    """Returns nested dict {category: {rule_type: [values]}}. Used by both
    the settings page (to render the textareas with current values) and
    the categorizer (to match against)."""
    out = {c: {"name": [], "domain": [], "email": []} for c in CATEGORY_VALUES}
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT category, rule_type, value FROM category_rules ORDER BY category, rule_type, value"
        )
        for cat, rt, val in cur.fetchall():
            if cat in out and rt in out[cat]:
                out[cat][rt].append(val)
    return out


def db_get_candidate_category(email: str) -> dict | None:
    """Return the cached category dict for an email or None. Shape matches
    what _categorize_candidate produces (category, confidence, source,
    reasoning, classified_at)."""
    if not email:
        return None
    key = email.strip().lower()
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT category, confidence, source, reasoning, classified_at "
            "FROM candidate_categories WHERE email = ?",
            (key,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "category": row[0],
        "confidence": row[1],
        "source": row[2],
        "reasoning": row[3] or "",
        "classified_at": row[4],
    }


def db_save_candidate_category(email: str, category: str, source: str,
                                confidence: str = "medium", reasoning: str = ""):
    """Upsert a categorization. Called by the bulk categorizer AND by the
    manual-override route. Source semantics: 'manual' > 'rule' > 'heuristic'
    > 'llm' (priority for re-classification — manual is sticky and never
    overwritten by automated runs)."""
    if not email or category not in CATEGORY_VALUES:
        return
    key = email.strip().lower()
    with _db_lock, _db_connect() as conn:
        conn.execute(
            "INSERT INTO candidate_categories (email, category, confidence, source, reasoning, classified_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(email) DO UPDATE SET "
            "  category = excluded.category, confidence = excluded.confidence, "
            "  source = excluded.source, reasoning = excluded.reasoning, "
            "  classified_at = excluded.classified_at",
            (key, category, confidence, source, reasoning, int(time.time())),
        )
        conn.commit()


def _categorize_by_rules(email: str, name: str, rules: dict) -> tuple[str, str] | None:
    """Match a contact against the user-uploaded category rules.
    Returns (category, reasoning) on match, None if nothing matched.

    Match precedence within a category:
      1. exact email match (highest)
      2. domain match
      3. case-insensitive substring name match (lowest)
    Across categories, the first matching category wins (in CATEGORY_VALUES order).
    """
    em = (email or "").strip().lower()
    nm = (name or "").strip().lower()
    domain = em.rsplit("@", 1)[-1] if "@" in em else ""
    for category in CATEGORY_VALUES[:-1]:  # skip 'unclassified'
        cat_rules = rules.get(category, {})
        # Exact email match
        for rule_email in cat_rules.get("email", []):
            if rule_email.strip().lower() == em:
                return category, f"email matches '{rule_email}'"
        # Domain match
        for rule_domain in cat_rules.get("domain", []):
            rd = rule_domain.strip().lower().lstrip("@")
            if rd and domain == rd:
                return category, f"domain matches '{rd}'"
        # Name substring match (case-insensitive). Match ONLY against the
        # name field — not the email — so a rule "name: acme" doesn't
        # surprise-match `acme@gmail.com` (where the user really meant the
        # company "Acme") or `nick.macme@x.com` (where "acme" is a random
        # substring inside another word). Use the domain rule type for
        # email/domain matching.
        for rule_name in cat_rules.get("name", []):
            rn = rule_name.strip().lower()
            if rn and rn in nm:
                return category, f"name contains '{rule_name}'"
    return None


def _categorize_by_heuristic(email: str) -> tuple[str, str] | None:
    """Fallback heuristic. Currently just: personal-email domain → friend.
    Returns (category, reasoning) or None.

    Conservative on purpose — heuristics that guess wrong are worse than
    leaving things 'unclassified' (which the user can manually override
    or feed via category rules)."""
    em = (email or "").strip().lower()
    if "@" not in em:
        return None
    domain = em.rsplit("@", 1)[-1]
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return "friend", f"{domain} is a personal-email provider"
    return None


def _categorize_by_llm(email: str, name: str, linkedin_url: str = "") -> tuple[str, str, str] | None:
    """Optional LLM fallback. Calls gpt-4o-mini if OPENAI_API_KEY is
    configured (via the resolver-keys file). Returns (category, confidence,
    reasoning) or None when no key / no answer.

    Prompt asks the model to pick from the 5 categories or 'unclassified'.
    Cheap (~$0.0001 per call) but only worth running once per email — the
    result is cached in candidate_categories with source='llm'."""
    keys = _load_resolver_keys()
    openai_key = keys.get("openai_api_key", "").strip()
    if not openai_key:
        return None
    try:
        from openai import OpenAI
    except ImportError:
        return None
    em = (email or "").strip()
    nm = (name or "").strip()
    if not em:
        return None
    user_prompt = (
        f"Classify this contact's likely relationship to the person who has them in their address book.\n\n"
        f"Name: {nm or '(unknown)'}\n"
        f"Email: {em}\n"
        f"LinkedIn: {linkedin_url or '(not resolved)'}\n\n"
        "Pick exactly one category:\n"
        "  customer     - they pay (or work for a company that pays) the address-book owner\n"
        "  investor     - they're a VC, angel, or board member\n"
        "  vendor       - they sell something to the address-book owner, or are a partner/agency\n"
        "  friend       - personal connection, not a work relationship\n"
        "  coworker     - they work or worked at the same company as the address-book owner\n"
        "  unclassified - genuinely can't tell\n\n"
        "Respond with ONLY the category word on the first line, then a one-sentence reason on the second line."
    )
    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You classify B2B contacts. Be conservative — pick 'unclassified' over guessing wrong."},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0,
            max_tokens=80,
            timeout=15,
        )
        text = (resp.choices[0].message.content or "").strip()
    except Exception:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    cat_word = lines[0].lower().strip().rstrip(".,:;")
    if cat_word not in CATEGORY_VALUES:
        return None
    reasoning = (lines[1] if len(lines) > 1 else "").strip()
    return cat_word, "low", f"LLM ({cat_word}): {reasoning}"


def _categorize_candidate(email: str, name: str = "", linkedin_url: str = "",
                           rules: dict | None = None,
                           use_llm: bool = False) -> dict:
    """The full categorization pipeline. Priority:
      1. Manual override in candidate_categories (source='manual') — sticky
      2. User-uploaded category rules
      3. Built-in heuristics (personal email domains)
      4. LLM fallback (only when use_llm=True AND OpenAI key configured)
      5. Default: unclassified

    Persists the result (except for source='manual', which is preserved as-is)
    and returns the dict. `rules` is passed in by the bulk-categorize path
    so a single rule load covers many candidates.
    """
    em = (email or "").strip().lower()
    if not em:
        return {"category": "unclassified", "confidence": "low", "source": "default", "reasoning": ""}

    existing = db_get_candidate_category(em)
    if existing and existing.get("source") == "manual":
        # Sticky — never overwrite a manual override.
        return existing

    if rules is None:
        rules = db_get_category_rules()

    # 2. User-uploaded rules
    matched = _categorize_by_rules(em, name, rules)
    if matched:
        cat, why = matched
        db_save_candidate_category(em, cat, "rule", "high", why)
        return {"category": cat, "confidence": "high", "source": "rule", "reasoning": why}

    # 3. Built-in heuristics
    matched = _categorize_by_heuristic(em)
    if matched:
        cat, why = matched
        db_save_candidate_category(em, cat, "heuristic", "medium", why)
        return {"category": cat, "confidence": "medium", "source": "heuristic", "reasoning": why}

    # 4. LLM fallback (opt-in, gated on OpenAI key)
    if use_llm:
        llm_match = _categorize_by_llm(em, name, linkedin_url)
        if llm_match:
            cat, conf, why = llm_match
            db_save_candidate_category(em, cat, "llm", conf, why)
            return {"category": cat, "confidence": conf, "source": "llm", "reasoning": why}

    # 5. Default
    db_save_candidate_category(em, "unclassified", "default", "low", "no rule, heuristic, or LLM match")
    return {"category": "unclassified", "confidence": "low", "source": "default", "reasoning": ""}


# ---- Manual path uploads (CSV → connector cards on target drawer) ---------
#
# A customer's investor exports their LinkedIn connections (or any tool's
# equivalent). Customer drops the CSV here. Each row that has a LinkedIn URL
# matching one of the customer's targets surfaces as an additional path on
# that target's drawer — same connector card as Draftboard-derived paths,
# just with score=0 (we don't have enrichment data) and bullets describing
# the list source.

# Header aliases for column detection. Values are searched as case-
# insensitive substrings; first match wins. The order within each list
# matters when two aliases could both match (e.g., "URL" beats "Profile URL").
MANUAL_PATH_COLUMN_ALIASES = {
    "linkedin_url": ["linkedin url", "profile url", "linkedin profile", "linkedin", "url", "profile"],
    "first_name":   ["first name", "firstname", "first", "given name", "given"],
    "last_name":    ["last name", "lastname", "last", "surname", "family name", "family"],
    "full_name":    ["full name", "name"],
    "email":        ["email address", "e-mail", "email"],
    "company":      ["current company", "company", "organization", "organisation", "employer"],
    "position":     ["current position", "job title", "position", "title", "role", "job"],
    "connected_on": ["connected on", "connection date", "date connected", "date added", "since", "connected"],
}


def _detect_csv_columns(header_row):
    """Map field-of-interest to column index by header text. Returns dict
    {field_name: column_index}. Fields that don't map are absent from the
    output — caller should handle missing fields gracefully.

    Designed to handle: LinkedIn export, Apollo export, any tool with
    reasonably-named columns, hand-rolled CSVs.

    Defensive: only considers a cell as a possible header if it's short
    (≤50 chars) — real CSV headers are short labels like "URL" or
    "Email Address", never multi-sentence prose. This avoids matching
    "url" or "email" as a substring inside a giant disclaimer paragraph
    (LinkedIn's Connections.csv preamble does exactly this)."""
    if not header_row:
        return {}
    header_lower = [(h or "").strip().lower() for h in header_row]
    # Treat any "cell" longer than 50 chars as prose, not a header label.
    # This filters out LinkedIn's preamble disclaimer row which lands as
    # a single ~500-char cell.
    header_lower = [h if len(h) <= 50 else "" for h in header_lower]
    out = {}
    for field, aliases in MANUAL_PATH_COLUMN_ALIASES.items():
        for alias in aliases:
            for i, h in enumerate(header_lower):
                if not h:
                    continue
                if h == alias or alias in h:
                    if field not in out:
                        out[field] = i
                        break
            if field in out:
                break
    return out


def _split_full_name(full):
    """Split 'John Smith' into ('John', 'Smith'). Single-word names go to
    first_name. Empty input → ('', '')."""
    parts = (full or "").strip().split(None, 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    if len(parts) == 1:
        return parts[0], ""
    return "", ""


def parse_manual_path_csv(file_obj) -> tuple[list[dict], dict, int]:
    """Read a CSV from `file_obj` (Flask's request.files['file']) and
    extract contact rows. Returns (rows, detected_columns, skipped_count).

    Each row is a dict with keys matching MANUAL_PATH_COLUMN_ALIASES. Rows
    without a LinkedIn URL are skipped (we can't match them against
    targets without one). Rows with a URL but no name are kept — we'll
    show the URL as a fallback identity.

    Encoding: utf-8-sig (handles LinkedIn's BOM-prefixed exports cleanly,
    matches the kit's csv convention)."""
    import csv, io
    raw = file_obj.read()
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("latin-1")
            except UnicodeDecodeError:
                return [], {}, 0
    else:
        text = raw
    # LinkedIn's standard Connections.csv export starts with:
    #   line 1: "Notes:"
    #   line 2: a quoted multi-line disclaimer (which itself contains commas
    #           inside the quoted string — so the prior line-strip-on-comma
    #           heuristic mistook the disclaimer for the header)
    #   line 3: blank
    #   line 4: real header (First Name,Last Name,URL,Email,Company,Position,Connected On)
    # Use a proper CSV reader from the start and ITERATE rows looking for
    # the first row that _detect_csv_columns can recognize as a header. Skip
    # any earlier rows (preamble, blank, disclaimer).
    reader = csv.reader(io.StringIO(text))
    cols = {}
    header = None
    rows_consumed = 0
    for candidate in reader:
        rows_consumed += 1
        if rows_consumed > 30:
            # Defensive — don't scan the entire file looking for a header
            # if every row is junk. Bail out and report no_url_column.
            break
        if not candidate or all(not (c or "").strip() for c in candidate):
            continue
        # A real header must have at least one cell that the alias map
        # recognizes AND a URL column specifically (since URL is required).
        candidate_cols = _detect_csv_columns(candidate)
        if "linkedin_url" in candidate_cols:
            header = candidate
            cols = candidate_cols
            break
    if header is None or "linkedin_url" not in cols:
        # Caller surfaces this to the user via no_url_column.
        return [], cols, 0

    def _g(row, field):
        idx = cols.get(field)
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    # Strict-ish LinkedIn person-profile pattern. Catches:
    #   - non-LinkedIn URLs that just happen to contain "linkedin.com" in a
    #     query string (e.g. github.com/?ref=linkedin.com)
    #   - LinkedIn company / school / showcase URLs that can never match a
    #     person target (e.g. linkedin.com/company/draftboard)
    # Accepts any host on the linkedin.com domain (www, region subdomains
    # like uk.linkedin.com) followed by /in/<slug>.
    li_person_re = re.compile(
        r"^https?://([a-z0-9-]+\.)?linkedin\.com/in/[^\s/?#]+",
        re.IGNORECASE,
    )
    rows = []
    skipped = 0
    seen_norm = set()  # within-CSV dedup — same URL twice → import once
    for raw_row in reader:
        if not raw_row or all(not (c or "").strip() for c in raw_row):
            continue  # blank line
        url = _g(raw_row, "linkedin_url")
        if not url or not li_person_re.match(url.strip()):
            skipped += 1
            continue
        norm = _normalize_linkedin(url)
        if not norm:
            skipped += 1
            continue
        if norm in seen_norm:
            skipped += 1
            continue
        seen_norm.add(norm)
        first = _g(raw_row, "first_name")
        last = _g(raw_row, "last_name")
        if not first and not last and "full_name" in cols:
            first, last = _split_full_name(_g(raw_row, "full_name"))
        # Per-cell length cap — cosmetic for the connector card and a
        # defense-in-depth against pathological CSVs with massive cells.
        def _cap(s, n=240):
            return (s or "")[:n]
        rows.append({
            "linkedin_url": _cap(url),
            "linkedin_url_normalized": norm,
            "first_name": _cap(first, 80),
            "last_name": _cap(last, 80),
            "email": _cap(_g(raw_row, "email"), 200),
            "company": _cap(_g(raw_row, "company"), 200),
            "position": _cap(_g(raw_row, "position"), 200),
            "connected_on": _cap(_g(raw_row, "connected_on"), 40),
        })
    return rows, cols, skipped


def db_save_manual_path_list(meta: dict, rows: list[dict], detected_cols: dict, skipped: int) -> int:
    """Persist a parsed CSV. Returns the new list_id. `meta` is the
    upload-form metadata about the LIST OWNER (the investor / external
    person whose CSV this is)."""
    now = int(time.time())
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO manual_path_lists "
            "(label, owner_first, owner_last, owner_email, owner_title, owner_company, owner_linkedin, "
            " detected_columns, row_count, skipped_count, uploaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                meta.get("label", "").strip(),
                meta.get("owner_first", "").strip(),
                meta.get("owner_last", "").strip(),
                meta.get("owner_email", "").strip(),
                meta.get("owner_title", "").strip(),
                meta.get("owner_company", "").strip(),
                meta.get("owner_linkedin", "").strip(),
                json.dumps(detected_cols),
                len(rows),
                skipped,
                now,
            ),
        )
        list_id = cur.lastrowid
        for r in rows:
            conn.execute(
                "INSERT INTO manual_path_connections "
                "(list_id, first_name, last_name, email, company, position, connected_on, "
                " linkedin_url, linkedin_url_normalized) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    list_id,
                    r["first_name"], r["last_name"], r["email"],
                    r["company"], r["position"], r["connected_on"],
                    r["linkedin_url"], r["linkedin_url_normalized"],
                ),
            )
        conn.commit()
    return list_id


def db_list_manual_path_lists() -> list[dict]:
    """Return all uploaded lists with their metadata (newest first)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT id, label, owner_first, owner_last, owner_email, owner_title, "
            "       owner_company, row_count, skipped_count, uploaded_at "
            "FROM manual_path_lists ORDER BY uploaded_at DESC"
        )
        return [
            {
                "id": r[0], "label": r[1],
                "owner_first": r[2], "owner_last": r[3], "owner_email": r[4],
                "owner_title": r[5], "owner_company": r[6],
                "row_count": r[7], "skipped_count": r[8],
                "uploaded_at": r[9],
            }
            for r in cur.fetchall()
        ]


def db_delete_manual_path_list(list_id: int) -> int:
    """Delete one list + cascade its connections. Returns rowcount."""
    with _db_lock, _db_connect() as conn:
        # Manual cascade — SQLite FOREIGN KEY isn't enforced unless PRAGMA
        # foreign_keys=ON is set, which isn't a kit-wide guarantee.
        conn.execute("DELETE FROM manual_path_connections WHERE list_id = ?", (list_id,))
        cur = conn.execute("DELETE FROM manual_path_lists WHERE id = ?", (list_id,))
        conn.commit()
        return cur.rowcount


def manual_path_match_summary() -> dict:
    """Compute manual-path matches across the customer's full target set.
    Returns a dict the integration views can read without re-running this:
        {
          "by_list":           {list_id: {label, owner_first/last/email/title/company/linkedin,
                                           uploaded_at, target_ids: set, count}, ...},
          "by_target":         {target_id: [list_id, ...]},
          "matched_target_ids": set of target_ids with >=1 manual match,
          "lists":             [list_meta, ...],  # all uploaded lists, even no-match
        }

    Single full-table scan + Python set intersection. For a customer with
    4k targets and 5 lists × 500 contacts each, this is sub-50ms.
    Cached in the request via Flask's `g`-object pattern so multi-render
    pages (e.g. /connections renders + paginates) don't recompute."""
    from flask import g
    cached = getattr(g, "_manual_path_match_summary", None)
    if cached is not None:
        return cached

    targets, _ = fetch_all_targets()
    target_url_to_id = {}
    for t in targets:
        url = (t.get("linkedinUrl") or "").strip()
        if url:
            norm = _normalize_linkedin(url)
            if norm:
                target_url_to_id[norm] = t.get("id")

    by_list = {}
    by_target = {}
    matched_target_ids = set()
    lists_all = []

    if not target_url_to_id:
        out = {
            "by_list": {}, "by_target": {}, "matched_target_ids": set(),
            "lists": [],
        }
        g._manual_path_match_summary = out
        return out

    with _db_lock, _db_connect() as conn:
        # Pull all list metadata once
        list_cur = conn.execute(
            "SELECT id, label, owner_first, owner_last, owner_email, "
            "       owner_title, owner_company, owner_linkedin, uploaded_at "
            "FROM manual_path_lists"
        )
        for row in list_cur.fetchall():
            (lid, label, of, ol, oe, ot, oc, olu, ua) = row
            lists_all.append({
                "id": lid, "label": label,
                "owner_first": of or "", "owner_last": ol or "",
                "owner_email": oe or "", "owner_title": ot or "",
                "owner_company": oc or "", "owner_linkedin": olu or "",
                "uploaded_at": ua,
            })
        # Pull all manual connections in one go; intersect against target URLs.
        conn_cur = conn.execute(
            "SELECT list_id, linkedin_url_normalized "
            "FROM manual_path_connections WHERE linkedin_url_normalized != ''"
        )
        for list_id, norm_url in conn_cur.fetchall():
            tid = target_url_to_id.get(norm_url)
            if not tid:
                continue
            matched_target_ids.add(tid)
            by_target.setdefault(tid, []).append(list_id)
            bucket = by_list.setdefault(list_id, {"target_ids": set()})
            bucket["target_ids"].add(tid)

    # Hydrate each by_list entry with the list metadata
    list_by_id = {l["id"]: l for l in lists_all}
    for lid, data in by_list.items():
        meta = list_by_id.get(lid, {})
        data.update(meta)
        data["count"] = len(data["target_ids"])

    out = {
        "by_list": by_list,
        "by_target": by_target,
        "matched_target_ids": matched_target_ids,
        "lists": lists_all,
    }
    g._manual_path_match_summary = out
    return out


def _manual_path_metadata_map() -> dict:
    """Per-request map of {normalized_linkedin_url → {first_name, last_name,
    company, position, email}} aggregated across ALL manual_path_connections
    rows. Used as a metadata fallback for targets whose own
    `position.companyName` is empty — relevant when the target was added via
    a paste-mode URL and someone else's manual list happens to include the
    same LinkedIn URL with company/title attached.

    Cached on flask.g so a single request that walks many targets (accounts
    listing, account drawer) doesn't issue one SELECT per target."""
    from flask import g
    g_key = "_manual_path_metadata_map_cache"
    cached = getattr(g, g_key, None)
    if cached is not None:
        return cached
    out: dict = {}
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT linkedin_url_normalized, first_name, last_name, "
            "       company, position, email "
            "FROM manual_path_connections "
            "WHERE linkedin_url_normalized != ''"
        )
        for norm, fn, ln, comp, pos, em in cur.fetchall():
            if not norm or norm in out:
                continue
            out[norm] = {
                "first_name": fn or "",
                "last_name": ln or "",
                "company": comp or "",
                "position": pos or "",
                "email": em or "",
            }
    setattr(g, g_key, out)
    return out


def _target_company_with_fallback(t: dict) -> str:
    """Target's display company, with a manual-path-metadata fallback.

    When the target's `position.companyName` is empty (typical for
    paste-mode-added targets), look up the matching manual_path_connections
    row by normalized LinkedIn URL and use its `company` instead. Preserves
    the existing "(unknown company)" bucket only when neither source has a
    company.

    Use this anywhere the accounts grouping or the account drawer keys off
    company name — otherwise the listing groups a paste-mode target into
    "(unknown company)" while the drawer fails to find it because the
    drawer's lookup uses the manual-path-derived display company."""
    pos = (t.get("position") or {})
    company = (pos.get("companyName") or "").strip()
    if company:
        return company
    norm = _normalize_linkedin(t.get("linkedinUrl") or "")
    if not norm:
        return ""
    meta = _manual_path_metadata_map().get(norm)
    if meta:
        return (meta.get("company") or "").strip()
    return ""


def manual_paths_for_target(target: dict) -> list[dict]:
    """Find manual-list connections that match this target's LinkedIn URL.
    Returns a list of Connection-shaped dicts (same shape as
    /targets/{id}/connections returns), so they can flow through
    _enrich_connection alongside Draftboard-derived paths.

    Match: normalize both sides via _normalize_linkedin (handles www/
    trailing-slash/scheme variations). Only fires when target has a
    LinkedIn URL — without one, no match is possible."""
    target_url = (target.get("linkedinUrl") or "").strip()
    if not target_url:
        return []
    norm = _normalize_linkedin(target_url)
    if not norm:
        return []
    target_first = (target.get("firstName") or "").strip()
    me_id = get_my_owner_id()
    me_data = {}
    raw = db_app_state_get("me_data")
    if raw:
        try:
            me_data = json.loads(raw) or {}
        except (ValueError, TypeError):
            me_data = {}
    # me_owner.id MUST equal my_user_id when /me is loaded, AND must equal
    # None when /me is NOT loaded (so _enrich_connection's `o.get("id") ==
    # my_user_id` check evaluates True in both cases). Using a sentinel
    # like "_self" would break the comparison on fresh installs.
    me_owner = {
        "id": me_id,  # None when /me hasn't run yet — matches my_user_id=None
        "firstName": (me_data.get("user_first") or "").strip(),
        "lastName": (me_data.get("user_last") or "").strip(),
        "linkedinUrl": (me_data.get("user_linkedin") or "").strip(),
        "score": 0,
    }
    out = []
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "SELECT l.id, l.label, l.owner_first, l.owner_last, l.owner_email, "
            "       l.owner_title, l.owner_company, l.owner_linkedin, l.uploaded_at, "
            "       c.id, c.first_name, c.last_name, c.email, c.company, c.position, "
            "       c.connected_on, c.linkedin_url "
            "FROM manual_path_connections c "
            "JOIN manual_path_lists l ON c.list_id = l.id "
            "WHERE c.linkedin_url_normalized = ?",
            (norm,),
        )
        for row in cur.fetchall():
            (lid, label, of, ol, oe, ot, oc, olu, uploaded_at,
             cid, _cf, _cl, _ce, c_company, c_position, connected_on, _curl) = row
            uploaded_str = time.strftime("%Y-%m-%d", time.localtime(uploaded_at)) if uploaded_at else ""
            details = []
            label_text = label or f"{of} {ol}".strip() or "your manual list"
            details.append(
                f"Has {target_first or 'them'} in their LinkedIn connections "
                f"(from {label_text}, uploaded {uploaded_str})"
            )
            if connected_on:
                details.append(f"Connected since {connected_on}")
            if c_position or c_company:
                listed = " at ".join(p for p in (c_position, c_company) if p)
                if listed:
                    details.append(f"Listed as {listed}")
            out.append({
                "id": f"manual:{lid}:{cid}",
                "firstName": of,
                "lastName": ol,
                "linkedinUrl": olu or "",
                "position": {"title": ot or "", "companyName": oc or ""},
                # score=None (NOT 0) so the template's `score is not none`
                # guard hides the pill cleanly. Setting to 0 would show "0"
                # which reads as "weak relationship" — wrong signal.
                "score": None,
                "scoreDetails": details,
                "owners": [me_owner],
                # Carry-through metadata for the compose-email pre-fill,
                # not required by Connection schema but read by enricher.
                "_manual_list_email": oe or "",
                "_manual_list_id": lid,
            })
    return out


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
    # Scanner import changed which supporters exist — drop the badge cache.
    invalidate_supporter_attribution_cache()
    return count, contributor_email, None


def db_remove_teammate_contributor(contributor_email):
    """Wipe all rows for one contributor (the 'remove a teammate' button)."""
    with _db_lock, _db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM teammate_contacts WHERE contributor_email = ?",
            (contributor_email.strip().lower(),),
        )
        conn.commit()
    # Removing a contributor changes the badge map.
    invalidate_supporter_attribution_cache()
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

# ---- Onboarding wizard ------------------------------------------------------
# Single-page menu of the four integrations, each with a status pill +
# "Set up" button that links to the existing per-feature wizard. The only
# new UI is the Draftboard-API-key paste form (everything else is already
# in /settings/slack, /settings/google, /settings/linkedin-resolver).

def _onboarding_status():
    """Status snapshot driving the four cards on /setup. Read-only —
    composes existing helpers (slack_is_configured, _resolver_status,
    google_status, db_get_app_state). Cheap to call on every render."""
    api_key = _current_api_key()
    api_key_me = None
    if api_key:
        # Use the cached /me result (populated whenever fetch_me() runs).
        # This is a display-only value — the "you're connected as X" line.
        # If the cache is empty we just show the key without the name.
        raw = db_app_state_get("me_data")
        if raw:
            try:
                api_key_me = json.loads(raw)
            except (ValueError, TypeError):
                api_key_me = None

    google_email = (db_app_state_get("google_account_email") or "").strip()

    resolver_keys = _load_resolver_keys()
    rstatus = _resolver_status(resolver_keys)
    # Detect "partial" — at least one key pasted but no method actually ready.
    # E.g., user added google_cse_api_key but not google_cse_id, or added
    # openai_api_key without the CSE pair.
    any_key = any(
        bool(resolver_keys.get(k)) for k in (
            "apollo_api_key", "google_cse_api_key", "google_cse_id", "openai_api_key"
        )
    )
    resolver_partial = any_key and not rstatus["any_ready"]

    slack_done = slack_is_configured()
    slack_channel = (db_get_slack_config("channel_name") or "").strip() if slack_done else ""

    # Signup-status nudge: when the customer has no API key yet AND hasn't
    # answered "are you already a Draftboard customer?", the wizard shows a
    # banner asking. Persisted in app_state so the question only fires once.
    # Values: "" (unset), "customer" (skip nudge, show normal API-key paste),
    # "new" (show signup CTA instead of paste form).
    signup_status = (db_app_state_get("setup_signup_status") or "").strip().lower()

    return {
        "api_key": {
            "done": bool(api_key),
            "source": _api_key_source if api_key else "",
            "me": api_key_me,  # dict with user_first/user_last/customer_name
            "auto_sync_off": (not AUTO_SYNC_ENABLED) and bool(api_key),
        },
        "signup_status": signup_status,
        "draftboard_signup_url": "https://intros.draftboard.com",
        "google": {
            "done": bool(google_email),
            "email": google_email,
            "skipped": bool(db_app_state_get("setup_skipped_google")),
        },
        "resolver": {
            "done": rstatus["any_ready"],
            "partial": resolver_partial,
            "apollo_ready": rstatus["apollo_ready"],
            "cse_ready": rstatus["cse_ready"],
            # For partial-state copy, we need to know which side is "almost done".
            "has_apollo_key": bool(resolver_keys.get("apollo_api_key")),
            "has_cse_key": bool(resolver_keys.get("google_cse_api_key")),
            "has_cse_id": bool(resolver_keys.get("google_cse_id")),
            "has_openai_key": bool(resolver_keys.get("openai_api_key")),
            "skipped": bool(db_app_state_get("setup_skipped_resolver")),
        },
        "slack": {
            "done": slack_done,
            "channel": slack_channel,
            "skipped": bool(db_app_state_get("setup_skipped_slack")),
        },
    }


@app.route("/setup", methods=["GET"])
def setup_view():
    """First-run / onboarding menu. Lists the four integrations with their
    current status and a 'Set up' button per row. Auto-redirected to from
    `/` when no API key is configured (unless `setup_dismissed` is set)."""
    return render_template(
        "setup_wizard.html",
        status=_onboarding_status(),
        # Pass through any flash-style query param so the API-key form can
        # show inline validation errors after a paste.
        api_key_error=request.args.get("api_key_error", ""),
        api_key_override_source=request.args.get("api_key_override_source", ""),
        api_key_unverified=request.args.get("api_key_unverified") == "1",
        active="setup",
    )


@app.route("/setup/api-key", methods=["POST"])
def setup_save_api_key():
    """Validate a pasted Draftboard API key, write it to
    ~/.draftboard-secrets/draftboard-api-starter, and refresh the in-process
    cache so the next request uses it without a server restart.

    Validation: GET /me with the new key. Persist only on 200, except if the
    user opted into `save_unverified=1` (escape hatch for network/rate-limit
    failures — typically corporate-proxy or offline development cases).

    Replace flow: this same handler powers both first-set and replace.
    A "Replace API key" expandable on /setup re-renders this same form;
    the route doesn't care which one submitted it.

    Cross-origin defense: refuse requests where the browser's Sec-Fetch-Site
    header indicates a cross-origin form submission. Localhost dev tools
    have low realistic exposure but the API key is high-value, and the
    existing /settings/linkedin-resolver endpoint already defends similarly.
    """
    # Reject cross-site form submissions — a malicious page the user visits
    # in another tab could otherwise overwrite their key via a hidden <form>
    # POST. Allow `same-origin` (real form submits) and `none` (direct
    # navigation / curl) and absent header (older browsers).
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403

    pasted = (request.form.get("api_key") or "").strip()
    save_unverified = request.form.get("save_unverified") == "1"

    if not pasted:
        return redirect(url_for("setup_view", api_key_error="empty"))
    if not pasted.startswith("db-api_"):
        # Cheap shape check — Draftboard keys all start with this prefix.
        # Saves a round-trip when the user pastes something obviously wrong.
        return redirect(url_for("setup_view", api_key_error="bad_shape"))
    if len(pasted) > 256:
        return redirect(url_for("setup_view", api_key_error="too_long"))

    def _persist_and_check_override(persist_key):
        """Write the key, refresh cache, then verify _load_api_key would
        actually return what we just wrote. If env / .env wins over the
        secrets file, the paste is silently a no-op — flag it loudly."""
        _save_api_key_to_secrets(persist_key)
        _current_api_key(force=True)
        loaded_key, loaded_source = _load_api_key()
        if loaded_key != persist_key:
            return loaded_source  # the source that's overriding us
        return None

    if save_unverified:
        # Network failed earlier; user explicitly chose to save anyway.
        override = _persist_and_check_override(pasted)
        if override:
            return redirect(url_for(
                "setup_view", api_key_error="env_override",
                api_key_override_source=override,
            ))
        return redirect(url_for("setup_view", api_key_unverified="1"))

    ok, payload = _validate_api_key(pasted)
    if not ok:
        if payload == "network":
            # Don't persist — bounce back with a flag that lets the form show
            # a "save without verifying" follow-up button.
            return redirect(url_for("setup_view", api_key_error="network"))
        return redirect(url_for("setup_view", api_key_error=payload))

    # 200 OK — persist and refresh the in-process cache.
    override = _persist_and_check_override(pasted)
    if override:
        return redirect(url_for(
            "setup_view", api_key_error="env_override",
            api_key_override_source=override,
        ))
    # Cache the /me payload (FLATTENED to the same shape fetch_me writes —
    # owner-filter / greeting / slack-test-message all read flat keys).
    try:
        db_app_state_set("me_data", json.dumps(_flatten_me_payload(payload)))
    except (TypeError, ValueError):
        pass
    return redirect(url_for("setup_view"))


@app.route("/setup/skip", methods=["POST"])
def setup_skip():
    """Mark one optional integration as skipped so it doesn't nag the user.
    Reversible: clicking 'Set up anyway' on the skipped card just navigates
    to the integration's setup page. Configured-state always trumps skip."""
    which = (request.form.get("which") or "").strip()
    if which not in ("google", "resolver", "slack"):
        return redirect(url_for("setup_view"))
    db_app_state_set(f"setup_skipped_{which}", str(int(time.time())))
    return redirect(url_for("setup_view"))


@app.route("/setup/unskip", methods=["POST"])
def setup_unskip():
    """Clear the skip flag on an integration so it re-renders as Not set up.
    Doesn't navigate the user to the integration page — the card will offer
    the 'Set up' button again on next render."""
    which = (request.form.get("which") or "").strip()
    if which not in ("google", "resolver", "slack"):
        return redirect(url_for("setup_view"))
    with _db_lock, _db_connect() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (f"setup_skipped_{which}",))
        conn.commit()
    return redirect(url_for("setup_view"))


@app.route("/setup/signup-status", methods=["POST"])
def setup_signup_status():
    """First-question step zero on /setup: 'are you already a Draftboard
    customer?' Persists the answer in app_state.setup_signup_status so the
    wizard doesn't re-ask. Reversible via the signup-CTA banner.

    Values:
      - 'customer'  → user has a key (or will paste one); render normal flow
      - 'new'       → user is new; render signup CTA + link to draftboard.com
      - ''          → unset; render the question
    """
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403
    status = (request.form.get("status") or "").strip().lower()
    if status not in ("customer", "new", ""):
        return redirect(url_for("setup_view"))
    if status == "":
        with _db_lock, _db_connect() as conn:
            conn.execute(
                "DELETE FROM app_state WHERE key = ?",
                ("setup_signup_status",),
            )
            conn.commit()
    else:
        db_app_state_set("setup_signup_status", status)
    return redirect(url_for("setup_view"))


@app.route("/setup/dismiss", methods=["POST"])
def setup_dismiss():
    """Permanent dismiss — used by the 'Continue' button at the bottom of
    /setup. Sets app_state.setup_dismissed=1 so the auto-redirect from `/`
    stops firing on subsequent visits. The user can return to /setup any
    time via the nav link to manage integrations."""
    db_app_state_set("setup_dismissed", str(int(time.time())))
    return redirect(url_for("targets_view"))


@app.route("/settings/google", methods=["GET"])
def settings_google_view():
    """Status page: 'Connect Google' button OR last-synced banner + Re-sync.
    When no OAuth client is configured, also renders a paste form + a
    mailto link to the kit author for requesting credentials."""
    status = google_status()
    sync_state = google_sync_progress_snapshot()
    # Build a prefilled mailto: link the customer can click to request
    # credentials. Fills in the body with their /me identity (customer
    # name + first/last) so the kit author has enough context to add them
    # to the GCP test-users allowlist before replying with credentials.
    me_data = {}
    raw = db_app_state_get("me_data")
    if raw:
        try:
            me_data = json.loads(raw) or {}
        except (ValueError, TypeError):
            me_data = {}
    customer_name = (me_data.get("customer_name") or "").strip()
    user_first = (me_data.get("user_first") or "").strip()
    user_last = (me_data.get("user_last") or "").strip()
    full_name = (f"{user_first} {user_last}").strip()
    subject = "Draftboard API starter — OAuth credentials request"
    body_lines = [
        "Hi Zach,",
        "",
        "I'm setting up the Draftboard API starter kit and need the Google OAuth credentials so the Connect Google flow works.",
        "",
        f"Draftboard customer: {customer_name or '(not loaded yet — paste your API key on the /setup page first if you want this prefilled)'}",
        f"My name on the account: {full_name or '(not loaded yet)'}",
        "My email (paste here so I have a canonical address even if my From: differs):",
        "",
        "Please add my email to the test-users allowlist on your Google Cloud consent screen, then reply with the client_id + client_secret values (they'll go into two paste fields on my end).",
        "",
        "Thanks!",
    ]
    from urllib.parse import quote
    # quote() the email address so an attacker-controlled KIT_AUTHOR_EMAIL
    # env value can't inject a second `&body=` param. Same fix as the
    # feedback_mailto in the global context_processor.
    safe_email = quote(KIT_AUTHOR_EMAIL, safe="@")
    mailto = (
        f"mailto:{safe_email}"
        f"?subject={quote(subject)}"
        f"&body={quote(chr(10).join(body_lines))}"
    )
    return render_template(
        "settings_google.html",
        status=status,
        sync_state=sync_state,
        request_credentials_mailto=mailto,
        kit_author_email=KIT_AUTHOR_EMAIL,
        configure_error=request.args.get("configure_error", ""),
        configure_saved=request.args.get("configure_saved") == "1",
        active="settings_google",
        api_key_set=bool(API_KEY),
    )


@app.route("/settings/google/configure-client", methods=["POST"])
def settings_google_configure_client():
    """Accept a pasted Desktop OAuth client_id + client_secret, validate
    shape, persist to oauth_client.json, refresh the in-process globals.

    No /me-style external validation — there's no Google API to "check if
    these credentials are valid" that doesn't require an OAuth flow. So we
    just shape-check (client_id ends with .apps.googleusercontent.com,
    client_secret starts with GOCSPX-) and trust the kit author sent good
    values. If they're wrong, the next "Connect Google" click will fail
    with Google's own error UI (which we already render at /settings/google).

    Cross-origin defense: refuse Sec-Fetch-Site=cross-site, mirroring the
    /setup/api-key handler.
    """
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403

    cid = (request.form.get("client_id") or "").strip()
    cs = (request.form.get("client_secret") or "").strip()
    if not cid or not cs:
        return redirect(url_for("settings_google_view", configure_error="empty"))
    # Real Google client_ids are typically 70+ chars and have a numeric+hex
    # prefix before the .apps.googleusercontent.com suffix. Reject the bare
    # suffix or anything obviously short — `.endswith` alone would accept
    # ".apps.googleusercontent.com" by itself and write it to disk.
    if not cid.endswith(".apps.googleusercontent.com") or len(cid) < 40:
        return redirect(url_for("settings_google_view", configure_error="bad_client_id"))
    # Real Google client_secrets after the GOCSPX- prefix are ~28 chars.
    # Reject the bare prefix.
    if not cs.startswith("GOCSPX-") or len(cs) < 20:
        return redirect(url_for("settings_google_view", configure_error="bad_client_secret"))
    if len(cid) > 256 or len(cs) > 128:
        return redirect(url_for("settings_google_view", configure_error="too_long"))

    try:
        _save_google_oauth_client_to_embedded_file(cid, cs)
    except OSError as e:
        return redirect(url_for("settings_google_view",
                                configure_error=f"write_failed_{type(e).__name__}"))
    _reload_google_oauth_client()
    return redirect(url_for("settings_google_view", configure_saved="1"))


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
    category_filter = (request.args.get("category") or "").strip().lower()
    candidates, total = db_query_candidates(
        limit=per_page, offset=(page - 1) * per_page,
        query=query, contributor=contributor, source=source,
        status_filter=status_filter, category_filter=category_filter,
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
    # Cheap count of resolutions that have a non-empty linkedin_url, scoped
    # to the same TTL the JOIN map and /supporters/linkedin-urls use.
    # Drives the "Copy LinkedIn URLs" bar visibility — we only show it once
    # there's at least one URL to actually copy. Otherwise the bar dead-ends.
    with _db_lock, _db_connect() as conn:
        cutoff = int(time.time()) - RESOLUTION_CACHE_TTL
        resolved_count = conn.execute(
            "SELECT COUNT(*) FROM linkedin_resolutions "
            "WHERE linkedin_url IS NOT NULL AND linkedin_url != '' "
            "  AND resolved_at >= ?",
            (cutoff,),
        ).fetchone()[0]
    # Category filter dropdown options for the template.
    category_options = [(c, CATEGORY_DISPLAY[c][1]) for c in CATEGORY_VALUES]
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
        category_filter=category_filter,
        category_options=category_options,
        category_display=CATEGORY_DISPLAY,
        unresolved_count=unresolved_count,
        resolved_count=resolved_count,
        status=status,
        sync_state=sync_state,
        resolver_status=resolver_status,
        active="candidates",
        api_key_set=bool(API_KEY),
    )


@app.route("/supporters/linkedin-urls", methods=["GET"])
def supporters_linkedin_urls():
    """Return the resolved LinkedIn URLs for ALL rows matching the current
    Supporters-page filter (across pages, not just the rendered one).

    Drives the "Copy LinkedIn URLs to clipboard" button. The customer
    pastes the result into Draftboard's production "Add Supporters" form
    or DMs the list to a teammate.

    Accepts the same query params as /supporters/candidates so the button
    can pass `window.location.search` straight through and get the same
    filter set the user is currently looking at.

    Returns: {"urls": [...], "count": N, "total_filtered": M, "scanned": K, "truncated": bool}
        - urls: only rows that have a resolved linkedin_url (deduped)
        - count: len(urls)
        - total_filtered: total filtered candidates (including unresolved),
          so the UI can say "47 of 120 are resolved — copying 47"
        - scanned: how many candidates we actually inspected (= min(total_filtered, cap))
        - truncated: True if total_filtered > scanned (cap was hit)
    """
    query = (request.args.get("q") or "").strip()
    contributor = (request.args.get("contributor") or "").strip()
    source = (request.args.get("source") or "").strip()
    status_filter = (request.args.get("status_filter") or "active").strip()
    category_filter = (request.args.get("category") or "").strip().lower()
    cap = 10000  # sane upper bound; surfaced via `truncated` when hit
    candidates, total = db_query_candidates(
        limit=cap, offset=0,
        query=query, contributor=contributor, source=source,
        status_filter=status_filter, category_filter=category_filter,
    )
    urls = []
    seen = set()
    for c in candidates:
        cached = db_get_resolution(c["email"])
        if not cached:
            continue
        url = (cached.get("linkedin_url") or "").strip()
        if not url:
            continue
        norm = _normalize_linkedin(url)
        if norm in seen:
            continue
        seen.add(norm)
        urls.append(url)
    scanned = len(candidates)
    return jsonify({
        "urls": urls,
        "count": len(urls),
        "total_filtered": total,
        "scanned": scanned,
        "truncated": total > scanned,
    })


# ---- Categorization routes -------------------------------------------------

# ---- Manual path uploads ---------------------------------------------------

@app.route("/settings/manual-paths", methods=["GET"])
def manual_paths_view():
    """Upload page: list existing uploads + form to add a new one."""
    lists = db_list_manual_path_lists()
    # Format dates and split detected_columns JSON for display.
    for l in lists:
        l["uploaded_display"] = (
            time.strftime("%Y-%m-%d %H:%M", time.localtime(l["uploaded_at"]))
            if l["uploaded_at"] else ""
        )
    # Defensive int parse — query strings come from the user's URL bar and
    # could be anything. A non-numeric value would throw a 500 on the upload-
    # success redirect view, which is a user-hostile place to surface the bug.
    def _qs_int(name: str) -> int:
        try:
            return int(request.args.get(name) or 0)
        except (TypeError, ValueError):
            return 0
    return render_template(
        "manual_paths.html",
        lists=lists,
        upload_error=request.args.get("upload_error", ""),
        upload_warning=request.args.get("upload_warning", ""),
        upload_success=request.args.get("upload_success", ""),
        upload_imported=_qs_int("imported"),
        upload_skipped=_qs_int("skipped"),
        upload_match_count=_qs_int("match_count"),
        # Round-trip form values on validation error so the user doesn't
        # have to re-type 6 fields after a typo / missing file.
        upload_label=request.args.get("label", ""),
        upload_owner_first=request.args.get("owner_first", ""),
        upload_owner_last=request.args.get("owner_last", ""),
        upload_owner_email=request.args.get("owner_email", ""),
        upload_owner_title=request.args.get("owner_title", ""),
        upload_owner_company=request.args.get("owner_company", ""),
        upload_owner_linkedin=request.args.get("owner_linkedin", ""),
        active="settings_manual_paths",
    )


@app.route("/settings/manual-paths", methods=["POST"])
def manual_paths_upload():
    """Accept a CSV upload + the list-owner metadata. Parse, persist, redirect.

    Flexible CSV: the parser auto-detects which columns hold the LinkedIn
    URL, name, email, etc. via header-text aliases — works with the
    LinkedIn export format, Apollo exports, or any CSV with reasonably-
    named columns. Rows without a LinkedIn URL are silently skipped (we
    can't match them against targets), and the response surfaces the
    skip count so the user knows."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403

    label = (request.form.get("label") or "").strip()
    owner_first = (request.form.get("owner_first") or "").strip()
    owner_last = (request.form.get("owner_last") or "").strip()
    owner_email = (request.form.get("owner_email") or "").strip()
    owner_title = (request.form.get("owner_title") or "").strip()
    owner_company = (request.form.get("owner_company") or "").strip()
    owner_linkedin = (request.form.get("owner_linkedin") or "").strip()

    # Pack the form values into the redirect so a validation bounce
    # doesn't blank the customer's 6 typed fields.
    form_passthrough = {
        "label": label[:200], "owner_first": owner_first[:80],
        "owner_last": owner_last[:80], "owner_email": owner_email[:200],
        "owner_title": owner_title[:200], "owner_company": owner_company[:200],
        "owner_linkedin": owner_linkedin[:400],
    }
    if not label or not owner_first or not owner_email:
        return redirect(url_for(
            "manual_paths_view", upload_error="missing_required", **form_passthrough,
        ))
    # Stricter email shape: rejects junk like "foo@b.co&body=injected" that
    # the prior cheap "@... in last segment" check let through. Excludes
    # URL-special chars (& ? = #) since those are the mailto/URL injection
    # vectors we care about. Standard local-part chars allowed in the local
    # part; standard domain chars + dots allowed after @.
    if not re.match(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$", owner_email):
        return redirect(url_for("manual_paths_view", upload_error="bad_email", **form_passthrough))
    # Server-side length caps. The form has maxlength but a curl/scripted POST
    # can submit anything; defense-in-depth.
    if (len(label) > 200 or len(owner_first) > 80 or len(owner_last) > 80
            or len(owner_email) > 200 or len(owner_title) > 200
            or len(owner_company) > 200 or len(owner_linkedin) > 400):
        return redirect(url_for("manual_paths_view", upload_error="too_long", **form_passthrough))

    file_obj = request.files.get("csv_file")
    if not file_obj or not file_obj.filename:
        return redirect(url_for("manual_paths_view", upload_error="no_file", **form_passthrough))

    try:
        rows, detected_cols, skipped = parse_manual_path_csv(file_obj)
    except Exception as e:
        return redirect(url_for(
            "manual_paths_view",
            upload_error=f"parse_failed_{type(e).__name__}",
            **form_passthrough,
        ))

    if "linkedin_url" not in detected_cols:
        return redirect(url_for("manual_paths_view", upload_error="no_url_column", **form_passthrough))
    if not rows:
        return redirect(url_for("manual_paths_view", upload_error="no_valid_rows", **form_passthrough))

    list_id = db_save_manual_path_list(
        meta={
            "label": label,
            "owner_first": owner_first,
            "owner_last": owner_last,
            "owner_email": owner_email,
            "owner_title": owner_title,
            "owner_company": owner_company,
            "owner_linkedin": owner_linkedin,
        },
        rows=rows,
        detected_cols=detected_cols,
        skipped=skipped,
    )
    # Match-preview: count how many of the JUST-IMPORTED rows match a
    # current target. Cheap inline lookup, no need to round-trip through
    # manual_path_match_summary (which is per-request-cached anyway).
    targets, _ = fetch_all_targets()
    target_url_set = set()
    for t in targets:
        url = (t.get("linkedinUrl") or "").strip()
        if url:
            n = _normalize_linkedin(url)
            if n:
                target_url_set.add(n)
    match_count = sum(
        1 for r in rows if r["linkedin_url_normalized"] in target_url_set
    )
    return redirect(url_for(
        "manual_paths_view",
        upload_success="1",
        imported=len(rows),
        skipped=skipped,
        match_count=match_count,
        list_id=list_id,
    ))


@app.route("/settings/manual-paths/delete", methods=["POST"])
def manual_paths_delete():
    """Delete one uploaded list (and its rows). Form field: list_id."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403
    try:
        list_id = int(request.form.get("list_id") or "0")
    except ValueError:
        list_id = 0
    if list_id > 0:
        db_delete_manual_path_list(list_id)
    return redirect(url_for("manual_paths_view"))


@app.route("/settings/categorization", methods=["GET"])
def settings_categorization_view():
    """Render the rules editor — three textareas per category (names,
    domains, emails) so the kit user can tell the categorizer who counts
    as a customer / investor / vendor / friend / coworker."""
    rules = db_get_category_rules()
    return render_template(
        "settings_categorization.html",
        rules=rules,
        category_display=CATEGORY_DISPLAY,
        # Skip 'unclassified' on the editor — no rules needed for the
        # default state.
        editable_categories=[c for c in CATEGORY_VALUES if c != "unclassified"],
        saved=request.args.get("saved") == "1",
        active="settings_categorization",
    )


@app.route("/settings/categorization", methods=["POST"])
def settings_categorization_save():
    """Save category rules. Form field names: `<category>__<rule_type>`
    e.g. `customer__domain`. Each value is a textarea — newlines split
    the values."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403
    # Defensive: only DELETE+REPLACE rule_types whose form field is actually
    # present in the POST body. A blank-but-present field IS a save intent
    # (user wants to clear rules); a missing field is NOT (mangled form,
    # browser extension, manual curl with empty body) and should not blow
    # away existing rules. Without this guard, an empty POST wipes everything.
    for category in CATEGORY_VALUES:
        if category == "unclassified":
            continue
        for rule_type in ("name", "domain", "email"):
            field = f"{category}__{rule_type}"
            if field not in request.form:
                continue  # field absent → don't touch existing rules
            raw = request.form.get(field, "")
            values = [line.strip() for line in raw.splitlines() if line.strip()]
            db_save_category_rules(category, rule_type, values)
    return redirect(url_for("settings_categorization_view", saved="1"))


# ---------------------------------------------------------------------------
# Settings → Your profile
# ---------------------------------------------------------------------------
# User-context fields drive personalization of every intro-request draft.
# Set once on /settings/profile, picked up by _build_messages thereafter.
# Without this, drafts say "Hey Yuval, saw you're connected to Jean-David..."
# with no signal about who's asking or why. With it, the draft mentions what
# the user's company does and frames the ask around the target.

_USER_CONTEXT_KEYS = [
    "user_first_name",
    "user_last_name",
    "user_email",
    "user_company",
    "user_company_description",
    "user_linkedin",
]


def _user_context() -> dict:
    """Read the user's profile from app_state. Returns dict with all
    _USER_CONTEXT_KEYS as stripped strings (empty if unset), plus
    `user_full_name` for convenience.

    Per-request cached on flask.g — `_build_messages` calls this for every
    rendered connector card, which on a page with 100 targets × ~5
    connectors each would otherwise issue ~3,000 locked DB roundtrips per
    page load. Same pattern as `_manual_path_metadata_map`."""
    try:
        from flask import g
        cached = getattr(g, "_user_context_cache", None)
        if cached is not None:
            return cached
    except RuntimeError:
        g = None  # outside Flask context — skip cache, fall through to read
    out = {}
    for key in _USER_CONTEXT_KEYS:
        out[key] = (db_app_state_get(key) or "").strip()
    out["user_full_name"] = f"{out['user_first_name']} {out['user_last_name']}".strip()
    if g is not None:
        try:
            g._user_context_cache = out
        except RuntimeError:
            pass
    return out


@app.route("/settings/profile", methods=["GET"])
def settings_profile_view():
    """Render the user-context form. Drives personalization in intro drafts —
    the user fills in their name + company once and every Compose-email body
    picks it up from then on."""
    return render_template(
        "settings_profile.html",
        user=_user_context(),
        saved=request.args.get("saved") == "1",
        active="settings_profile",
    )


@app.route("/settings/profile", methods=["POST"])
def settings_profile_save():
    """Persist the user-context form. CSRF-guarded the same way as the other
    settings POSTs. Honeypot bails the request without mutating state — the
    public-facing kit ships with a `honey` input that real users never see."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403

    # Honeypot — real users never see the hidden `honey` field. Bots that
    # blindly fill every input do. Bail without mutating state. Matters once
    # the kit is hosted past localhost (Tailscale / ngrok / a cheap VPS),
    # where drive-by bots could poison user_company_description (which lands
    # in every outgoing intro draft as a "by the way" footer).
    if request.form.get("honey"):
        return redirect(url_for("settings_profile_view", saved="1"))

    # Length caps — defense-in-depth against scripted POSTs. Description gets
    # 500 chars (a paragraph); everything else is short labels.
    caps = {
        "user_first_name": 80,
        "user_last_name": 80,
        "user_email": 200,
        "user_company": 120,
        "user_company_description": 500,
        "user_linkedin": 200,
    }
    for key, max_len in caps.items():
        val = (request.form.get(key) or "").strip()[:max_len]
        db_app_state_set(key, val)
    return redirect(url_for("settings_profile_view", saved="1"))


@app.route("/supporters/categorize-one", methods=["POST"])
def supporters_categorize_one():
    """Manual override OR reset-to-auto for a single supporter.

    JSON body shapes:
      {email, category}            → manual override (sticky)
      {email, category: "__auto"}  → clear the override; on next render
                                     /bulk-categorize the row will be
                                     classified by the normal pipeline

    Manual overrides are stored with source='manual', which the categorizer
    short-circuits on. Sending category='__auto' DELETEs the row from
    candidate_categories, releasing it back into automatic classification."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    category = (data.get("category") or "").strip().lower()
    if not email:
        return jsonify({"error": "email required"}), 400
    if category == "__auto":
        # Clear the override so the next categorize-all run re-classifies it.
        with _db_lock, _db_connect() as conn:
            conn.execute("DELETE FROM candidate_categories WHERE email = ?", (email,))
            conn.commit()
        # Re-classify it right now via the normal pipeline so the page
        # shows something more useful than 'unclassified' on the next render.
        rules = db_get_category_rules()
        result = _categorize_candidate(email, "", rules=rules, use_llm=False)
        return jsonify({"ok": True, "email": email, "category": result["category"], "source": result["source"]})
    if category not in CATEGORY_VALUES:
        return jsonify({"error": "valid category or '__auto' required"}), 400
    db_save_candidate_category(email, category, "manual", "high", "manually overridden by user")
    return jsonify({"ok": True, "email": email, "category": category, "source": "manual"})


@app.route("/supporters/categorize-all", methods=["POST"])
def supporters_categorize_all():
    """Run the categorizer over every candidate that doesn't have a manual
    override yet. Manual overrides are preserved (the categorizer skips
    rows where source='manual'). Heuristics + user rules first; LLM
    fallback gated on use_llm=1 query param (off by default — costs API
    credits per call)."""
    sec_site = request.headers.get("Sec-Fetch-Site")
    if sec_site and sec_site not in ("same-origin", "none"):
        return jsonify({"error": "cross-site form submission blocked"}), 403
    use_llm = request.args.get("use_llm") == "1"
    rules = db_get_category_rules()
    # Pull all candidates from the union view, no filter — so re-categorize
    # is comprehensive. Cap at 5000 for safety; should cover any realistic
    # workspace size.
    candidates, _total = db_query_candidates(
        limit=5000, offset=0, status_filter="all",
    )
    n_classified = 0
    n_changed = 0
    by_category = {c: 0 for c in CATEGORY_VALUES}
    for c in candidates:
        prev = c.get("category", "unclassified")
        result = _categorize_candidate(
            c["email"], c.get("name", ""),
            "",  # linkedin_url — we'd need to look it up separately; leave empty for v1
            rules=rules, use_llm=use_llm,
        )
        n_classified += 1
        if result["category"] != prev:
            n_changed += 1
        by_category[result["category"]] = by_category.get(result["category"], 0) + 1
    return jsonify({
        "ok": True,
        "classified": n_classified,
        "changed": n_changed,
        "by_category": by_category,
        "use_llm": use_llm,
    })


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
    """Return up to 50 (name, email) pairs the bulk-resolve button can feed
    into /candidates/resolve/batch. The JS pulls a chunk, posts it, then
    pulls the next chunk until the count drops to 0.

    Chunk size sized so the user sees progress every ~30-60 seconds even on
    free-tier resolver keys (where the throttle keeps each batch around 1
    req/sec). Larger chunks mean the browser sees no UI update for minutes
    at a time and the customer assumes the page is broken — which is worse
    than the rate-limit problem the throttle solved."""
    rows = db_unresolved_candidate_emails(limit=50)
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
# Slack assignment + Team Settings (from main; opt-in feature, Gmail-default)
# =====================================================================
# Flat editable map of every owner that has appeared in your workspace's
# connection data. Pulls owner identity from `target_owners` (already
# populated by the existing sync), lets the user paste a Slack user ID and
# email per teammate. The saved values feed:
#   - Slack assign feature (uses slack_user_id for @-mentions)
#   - Compose-email-to-teammate flow (uses email to pre-fill Gmail's To: field)
# Independent of Slack — useful immediately just for the email map.
# Flat editable map of every owner that has appeared in your workspace's
# connection data. Pulls owner identity from `target_owners` (already
# populated by the existing sync), lets the user paste a Slack user ID and
# email per teammate. The saved values feed:
#   - Slack assign feature (uses slack_user_id for @-mentions)
#   - Compose-email-to-teammate flow (uses email to pre-fill Gmail's To: field)
# Independent of Slack — useful immediately just for the email map.

@app.route("/settings/slack", methods=["GET"])
def settings_slack_view():
    """Slack-setup wizard. Driven by ?step=N URL param, but also infers the
    next step automatically from saved state so users can resume mid-flow.

    Steps:
      1. Intro / "want to set up?"
      2. Pick a Slack channel display name
      3. Install Incoming Webhook (next iteration)
      4. Paste webhook URL (next iteration)
      5. Map teammates → Slack IDs (links to /settings/team — next iteration)
      6. Test message (next iteration)
      7. Done (overview + reset)
    """
    webhook_url = db_get_slack_config("webhook_url")
    channel_name = db_get_slack_config("channel_name")
    completed_at = db_get_slack_config("setup_completed_at")

    # Defensive: a config can be "completed" only when it actually has a
    # valid webhook AND a channel saved. If something cleared one of them,
    # don't show the "Slack connected ✓" page lying about state.
    is_done = bool(
        completed_at
        and webhook_url
        and channel_name
        and _SLACK_WEBHOOK_RE.match(webhook_url)
    )

    requested = request.args.get("step")
    if is_done and not requested:
        current_step = "done"
    elif requested:
        try:
            current_step = max(1, min(7, int(requested)))
        except ValueError:
            current_step = 1
    else:
        # Auto-resume based on what's been saved so far.
        if not channel_name:
            current_step = 1
        elif not webhook_url:
            current_step = 3  # channel picked, need webhook install
        else:
            current_step = 5  # webhook done, need teammate map

    # Counts used by step 5 to surface "you have N teammates discovered, M
    # mapped" inline. Cheap to compute and the data is on every wizard
    # render — keeps templates simple.
    teammates = db_unique_owners()
    members = db_all_team_members()
    mapped_slack = sum(1 for m in members.values() if m.get("slack_user_id"))
    me, _ = fetch_me()

    # Format the completion timestamp for display — raw Unix seconds is
    # confusing in the "Set up" field on step 7.
    completed_at_display = ""
    if completed_at:
        try:
            completed_at_display = time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(int(completed_at))
            )
        except (TypeError, ValueError):
            completed_at_display = ""

    return render_template(
        "settings_slack.html",
        current_step=current_step,
        channel_name=channel_name or "",
        webhook_url=webhook_url or "",
        completed_at=completed_at or "",
        completed_at_display=completed_at_display,
        error=request.args.get("error", ""),
        total_teammates=len(teammates),
        mapped_slack=mapped_slack,
        me=me,
        active="settings_slack",
    )


_SLACK_WEBHOOK_PREFIX = "https://hooks.slack.com/services/"

# Strict shape for a real Slack incoming-webhook URL, e.g.
# https://hooks.slack.com/services/T01ABCDEF/B099XYZ/abc123def456...
# Catches pasted gibberish AND prevents @-host SSRF tricks like
# https://hooks.slack.com/services/x@evil.example/x/x.
_SLACK_WEBHOOK_RE = re.compile(
    r"^https://hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{16,}$"
)

# Slack user IDs are like U01ABCDEF or W0... (workspace IDs). Accept both.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{6,}$")

# Reasonable channel name: alphanumerics, dashes, underscores, dots. Slack's
# own rules are stricter (lowercase, max 80 chars) but we're permissive on
# case for the display label.
_SLACK_CHANNEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


def _normalize_slack_channel(raw):
    """Normalize a user-typed channel name. Strips leading '#', strips
    whitespace, validates against the channel-name regex. Returns the clean
    name (no '#') on success, or None if invalid."""
    if not raw:
        return None
    s = raw.strip().lstrip("#").strip()
    if not s or not _SLACK_CHANNEL_RE.match(s):
        return None
    return s


def _normalize_slack_user_id(raw):
    """Normalize a user-typed Slack user ID. Strips '<@...>', '@', whitespace.
    Returns the clean ID on success, '' if input was empty/whitespace, or None
    if input was non-empty but malformed."""
    if raw is None:
        return ""
    s = raw.strip()
    if not s:
        return ""
    # Strip <@U123> and <@U123|sarah> mention syntax
    if s.startswith("<@") and s.endswith(">"):
        s = s[2:-1]
        if "|" in s:
            s = s.split("|", 1)[0]
    s = s.lstrip("@").strip()
    if _SLACK_USER_ID_RE.match(s):
        return s
    return None


def _slack_mrkdwn_escape(text):
    """Escape a name/string for safe embedding in a Slack mrkdwn block.

    Slack's mrkdwn parser treats `*` `_` `~` as formatting and `<...>` as
    links/mentions. `&` `<` `>` MUST be HTML-entity escaped per Slack's docs.
    Names like `Tom*Bold*` or `<John>` would otherwise break the message.
    """
    if not text:
        return ""
    s = str(text)
    # Slack-required HTML entity escapes (must come first)
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Strip mrkdwn formatters from name fields. We strip rather than escape
    # because there's no canonical escape for *_~ — Slack's recommendation
    # is to remove them when they aren't intended as formatting.
    for ch in ("*", "_", "~", "`"):
        s = s.replace(ch, "")
    return s


@app.route("/settings/slack", methods=["POST"])
def settings_slack_save():
    """Wizard advance handler. Each step posts its form data + a hidden `step`
    field; we persist what we got and redirect to the next step (or stay if
    the user picked 'skip')."""
    step = request.form.get("step", "")

    if step == "1":
        choice = request.form.get("choice", "")
        if choice == "yes":
            return redirect(url_for("settings_slack_view", step=2))
        # skip-for-now → bounce back to Targets
        return redirect("/")

    if step == "2":
        # Stored WITHOUT a leading '#' — display layer adds the '#' prefix.
        # Avoids the "##warm-intros" double-prefix bug when concatenated.
        normalized = _normalize_slack_channel(request.form.get("channel_name") or "")
        if not normalized:
            return redirect(url_for("settings_slack_view", step=2, error="bad_channel"))
        db_set_slack_config("channel_name", normalized)
        return redirect(url_for("settings_slack_view", step=3))

    if step == "3":
        # Informational step — nothing to save, just advance.
        return redirect(url_for("settings_slack_view", step=4))

    if step == "4":
        webhook = (request.form.get("webhook_url") or "").strip()
        # Strict shape check — catches typos AND blocks SSRF-flavored URLs
        # like https://hooks.slack.com/services/x@127.0.0.1/x/x.
        if len(webhook) > 256 or not _SLACK_WEBHOOK_RE.match(webhook):
            return redirect(url_for("settings_slack_view", step=4, error="bad_url"))
        db_set_slack_config("webhook_url", webhook)
        return redirect(url_for("settings_slack_view", step=5))

    if step == "5":
        # Teammate-map step is informational — the actual mapping happens on
        # /settings/team (a separate page that's been live since iteration 2).
        # Just advance.
        return redirect(url_for("settings_slack_view", step=6))

    # Step 6 (test message) is handled by /settings/slack/test, not here.
    return redirect(url_for("settings_slack_view"))


@app.route("/settings/slack/test", methods=["POST"])
def settings_slack_test():
    """Send a real Slack test message via the configured webhook. On success,
    record `last_test_at` + `setup_completed_at` and bounce the user to the
    "Slack connected ✓" overview (step 7). On failure, bounce back to step 6
    with a descriptive ?error= param so the user can see what went wrong."""
    webhook = db_get_slack_config("webhook_url")
    if not webhook:
        return redirect(url_for("settings_slack_view", step=4, error="no_webhook"))

    me, _ = fetch_me()
    user_full = ""
    if me:
        user_full = (
            f"{(me.get('user_first') or '').strip()} "
            f"{(me.get('user_last') or '').strip()}"
        ).strip()

    channel_name = db_get_slack_config("channel_name")
    channel_display = f"#{channel_name}" if channel_name else "this channel"
    sender = f" from {_slack_mrkdwn_escape(user_full)}" if user_full else ""
    text = (
        f":dart: Draftboard is connected — this is a test message{sender}.\n"
        f"{channel_display} will receive warm-intro alerts when you or a "
        f"teammate clicks _Assign to teammate_ → :speech_balloon: *Slack* on "
        f"a connector card."
    )

    try:
        # allow_redirects=False so a fake/dead webhook (which Slack 302s away
        # from) doesn't masquerade as success when the redirect target 200s.
        r = requests.post(
            webhook, json={"text": text}, timeout=10, allow_redirects=False
        )
    except requests.RequestException:
        return redirect(url_for("settings_slack_view", step=6, error="network"))

    # Canonical Slack-incoming-webhook success: HTTP 200 + body == "ok".
    # Anything else (other status codes, empty body, body with an error code
    # like "no_service") is a failure we report to the user.
    body = (r.text or "").strip()
    if r.status_code != 200 or body != "ok":
        reason = body or f"http_{r.status_code}"
        # Sanitize for URL: spaces/punct out, max 32 chars.
        reason = re.sub(r"[^A-Za-z0-9_-]+", "_", reason)[:32] or f"http_{r.status_code}"
        return redirect(url_for(
            "settings_slack_view", step=6, error=f"slack_{reason}"
        ))

    now = int(time.time())
    db_set_slack_config("last_test_at", str(now))
    db_set_slack_config("setup_completed_at", str(now))
    return redirect(url_for("settings_slack_view", step=7))


def _slack_score_label(score):
    """Plain-language strength label for an owner→connection score (0-100ish).
    The Draftboard API doesn't expose per-owner scoreDetails ('the story') —
    only a number — so this is the best we can do for the 'why this teammate'
    line in the Slack message until that API gap closes."""
    score = int(score or 0)
    if score >= 90:
        return "strong"
    if score >= 70:
        return "decent"
    if score >= 50:
        return "moderate"
    return "weak"


def _build_slack_assign_payload(target, connection, owner, slack_user_id):
    """Build the Block Kit + fallback-text payload for a teammate-assignment
    Slack post. Two-perspective 'why' as discussed: scoreDetails-derived
    sentence for connector→target, score-based label for teammate→connector."""
    target_first = (target.get("firstName") or "").strip()
    target_last = (target.get("lastName") or "").strip()
    target_full = f"{target_first} {target_last}".strip() or "this prospect"
    target_pos = target.get("position") or {}
    target_title = (target_pos.get("title") or "").strip()
    target_company = (target_pos.get("companyName") or "").strip()
    target_linkedin = (target.get("linkedinUrl") or "").strip()

    connector_first = (connection.get("firstName") or "").strip()
    connector_last = (connection.get("lastName") or "").strip()
    connector_full = f"{connector_first} {connector_last}".strip() or "your connection"
    connector_linkedin = (connection.get("linkedinUrl") or "").strip()

    # Sanitize every name/string that ends up inside a mrkdwn section. Slack
    # treats *_~<>& as formatting / link / entity characters; a connector
    # named "Tom*Bold*" or "<John>" would otherwise corrupt the message
    # rendering. plain_text fields (button labels) only need <>& escaping,
    # which Slack accepts as HTML entities — _slack_mrkdwn_escape is more
    # aggressive than needed for plain_text but doesn't break it.
    target_first_s = _slack_mrkdwn_escape(target_first)
    target_full_s = _slack_mrkdwn_escape(target_full)
    target_title_s = _slack_mrkdwn_escape(target_title)
    target_company_s = _slack_mrkdwn_escape(target_company)
    connector_first_s = _slack_mrkdwn_escape(connector_first)
    connector_full_s = _slack_mrkdwn_escape(connector_full)

    # Why connector → target — reuse the existing third-person humanizer that
    # already powers connector-card bullets ("Mindy worked with Bogdan at
    # Microsoft for 26 months, most recently in 2009").
    raw_details = connection.get("scoreDetails") or []
    humanized_subject = connector_first or "they"
    humanized_object = target_first or "them"
    humanized = [
        _humanize_for_card(d, humanized_subject, humanized_object)
        for d in raw_details
    ]
    humanized = [h for h in humanized if h]
    why_connector_target = _slack_mrkdwn_escape(humanized[0]) if humanized else ""

    owner_score = owner.get("score")
    score_strength = _slack_score_label(owner_score)

    # ---- Build the message body (mrkdwn). One section, multi-line. ----
    prospect_line = f"*Prospect:* {target_full_s}"
    sub_bits = []
    if target_title_s:
        sub_bits.append(target_title_s)
    if target_company_s:
        sub_bits.append(f"at {target_company_s}")
    if sub_bits:
        prospect_line += "  _" + " ".join(sub_bits) + "_"

    connection_line = f"*Connection:* {connector_full_s}"
    # Drop the parenthetical when the score is unknown — "score 0 — weak" is
    # more confusing than helpful when it just means "no signal yet."
    if owner_score and owner_score > 0:
        connection_line += (
            f"  _(your score with {connector_first_s or 'them'}: "
            f"{owner_score} — {score_strength})_"
        )

    why_line = ""
    if why_connector_target:
        # E.g., "*Why Adrian → Yoav:* Adrian worked with Yoav at Microsoft
        # for 26 months, most recently in 2009"
        why_line = (
            f"*Why {connector_first_s or 'this connection'} → "
            f"{target_first_s or 'them'}:* {why_connector_target}"
        )

    # `slack_user_id` is normalized at write-time in db_set_team_member,
    # so it's safe to interpolate raw — but defense-in-depth: re-validate
    # here in case the row was written before normalization shipped.
    safe_uid = slack_user_id
    if not _SLACK_USER_ID_RE.match(safe_uid or ""):
        # Fallback to the connector's first name to avoid a broken @-mention.
        # The route caller has already gated on a non-empty mapped ID, so
        # this branch is paranoia.
        safe_uid = ""
    mention = f"<@{safe_uid}>" if safe_uid else "the assigned teammate"
    headline = f":dart: Hey {mention}, you can make a warm intro"

    body_lines = [headline, "", prospect_line, connection_line]
    if why_line:
        body_lines.append(why_line)
    body_text = "\n".join(body_lines)

    # Slack's section-text limit is 3000 chars. Realistic messages are well
    # under, but a target with a paragraph-long company name + scoreDetails
    # could trip it. Truncate with a clear marker rather than letting Slack
    # reject the whole message.
    if len(body_text) > 2900:
        body_text = body_text[:2890].rstrip() + "\n_…(truncated)_"

    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": body_text}},
    ]

    # Action buttons: LinkedIn for the target (always real) + LinkedIn for the
    # connector when available. Slack rejects buttons with non-http(s) URLs,
    # so guard against pasted-in javascript: or data: schemes (extremely
    # unlikely for Draftboard data, but the kit is a reference for customers
    # who will plug in their own data sources).
    action_elements = []
    if target_linkedin and target_linkedin.startswith(("http://", "https://")):
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"View {target_first or 'prospect'} on LinkedIn"[:75]},
            "url": target_linkedin,
        })
    if connector_linkedin and connector_linkedin.startswith(("http://", "https://")):
        action_elements.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"View {connector_first or 'connector'} on LinkedIn"[:75]},
            "url": connector_linkedin,
        })
    if action_elements:
        blocks.append({"type": "actions", "elements": action_elements})

    # Notification-tray preview text — plain text, no mrkdwn parsing.
    fallback_text = f"Warm intro path: {connector_full} -> {target_full}"
    return {"blocks": blocks, "text": fallback_text}


@app.route("/slack/assign", methods=["POST"])
def slack_assign():
    """Post a teammate-assignment message to the configured Slack webhook.

    Request JSON: {target_id, connection_id, owner_id}

    Looks up:
      - target metadata from cached /targets list
      - the specific Connection (by connection_id) from cached
        /targets/{id}/connections JSON
      - the specific owner inside connection.owners[] (so we get their score)
      - the owner's Slack user ID from team_members
      - the webhook URL from slack_config

    Posts a Block Kit message tagging the teammate. Returns JSON
    {ok: bool, error?: str} so the click handler can show a toast.
    """
    data = request.get_json(silent=True) or {}
    target_id = (data.get("target_id") or "").strip()
    connection_id = (data.get("connection_id") or "").strip()
    owner_id = (data.get("owner_id") or "").strip()

    if not (target_id and connection_id and owner_id):
        return jsonify({"ok": False, "error": "missing target_id, connection_id, or owner_id"}), 400

    webhook = db_get_slack_config("webhook_url")
    if not webhook:
        return jsonify({"ok": False, "error": "Slack isn't configured yet — visit Settings → Slack"}), 400

    member = db_get_team_member(owner_id) or {}
    slack_user_id = (member.get("slack_user_id") or "").strip()
    if not slack_user_id:
        return jsonify({"ok": False, "error": "this teammate has no Slack ID mapped — visit Settings → Team to add one"}), 400

    targets, _ = fetch_all_targets()
    target = next((t for t in targets if t.get("id") == target_id), None)
    if not target:
        return jsonify({"ok": False, "error": "target not found in local cache"}), 404

    target_connections, _err = fetch_target_connections(target_id)
    connection = next((c for c in target_connections if c.get("id") == connection_id), None)
    if not connection:
        return jsonify({"ok": False, "error": "connection not found for this target"}), 404

    owner = next((o for o in (connection.get("owners") or []) if o.get("id") == owner_id), None)
    if not owner:
        return jsonify({"ok": False, "error": "this owner isn't on the connection's owners list"}), 400

    payload = _build_slack_assign_payload(target, connection, owner, slack_user_id)

    try:
        r = requests.post(webhook, json=payload, timeout=10, allow_redirects=False)
    except requests.RequestException as e:
        return jsonify({"ok": False, "error": f"network error posting to Slack: {e}"}), 502

    body = (r.text or "").strip()
    if r.status_code != 200 or body != "ok":
        return jsonify({
            "ok": False,
            "error": f"Slack returned {r.status_code}: {body[:128]}",
        }), 502

    return jsonify({"ok": True, "channel": db_get_slack_config("channel_name") or ""})


@app.route("/settings/slack/reset", methods=["POST"])
def settings_slack_reset():
    """Wipe Slack config (webhook URL, channel name, completion timestamps)
    and bounce back to step 1 so the user can re-run the wizard. Leaves
    team_members intact since email mappings are still useful even when
    Slack is offline."""
    db_clear_slack_config()
    return redirect(url_for("settings_slack_view", step=1))


@app.route("/settings/team", methods=["GET"])
def settings_team_view():
    owners = db_unique_owners()  # [{id, first, last, name, linkedin, target_count}, ...]
    members = db_all_team_members()  # owner_id -> {slack_user_id, email}
    rows = []
    for o in owners:
        m = members.get(o["id"]) or {"slack_user_id": "", "email": ""}
        rows.append({
            "owner_id": o["id"],
            "name": o["name"],
            "first": o["first"],
            "linkedin": o["linkedin"],
            "target_count": o["target_count"],
            "slack_user_id": m["slack_user_id"],
            "email": m["email"],
        })
    me, _ = fetch_me()
    invalid_slack = (request.args.get("invalid_slack") or "").strip()
    invalid_slack_list = [s for s in invalid_slack.split(",") if s] if invalid_slack else []
    return render_template(
        "settings_team.html",
        rows=rows,
        total_owners=len(rows),
        mapped_emails=sum(1 for r in rows if r["email"]),
        mapped_slack=sum(1 for r in rows if r["slack_user_id"]),
        saved=request.args.get("saved") == "1",
        invalid_slack_list=invalid_slack_list,
        slack_configured=slack_is_configured(),
        slack_webhook_saved=bool(db_get_slack_config("webhook_url")),
        me=me,
        active="settings",
    )


@app.route("/settings/team", methods=["POST"])
def settings_team_save():
    """Receive the entire team-mapping form and upsert each row.

    Form field names: `slack_user_id__<owner_id>` and `email__<owner_id>`.
    Empty-string values are treated as "clear the field" (legitimate).

    Validation:
      - owner_id must be non-empty AND exist in target_owners (rejects
        crafted form-field names like `slack_user_id__` with empty PK
        or arbitrary IDs not yet known to the app)
      - slack_user_id is normalized via _normalize_slack_user_id which
        strips '@', '<@U…>' mention syntax, and rejects malformed input
    """
    valid_owner_ids = {o["id"] for o in db_unique_owners()}
    invalid_slack = []  # (owner_first_or_id, raw_value)
    members_by_id = {o["id"]: o for o in db_unique_owners()}

    for key, value in request.form.items():
        if key.startswith("slack_user_id__"):
            owner_id = key[len("slack_user_id__"):]
            if not owner_id or owner_id not in valid_owner_ids:
                continue  # silently skip — likely a stale form
            normalized = _normalize_slack_user_id(value)
            if normalized is None:
                # Non-empty but malformed — collect for inline error
                friendly = members_by_id.get(owner_id, {}).get("first") or owner_id
                invalid_slack.append(friendly)
                continue
            db_set_team_member(owner_id, slack_user_id=normalized)
        elif key.startswith("email__"):
            owner_id = key[len("email__"):]
            if not owner_id or owner_id not in valid_owner_ids:
                continue
            db_set_team_member(owner_id, email=value)

    if invalid_slack:
        # Pass back the names of teammates whose Slack IDs were rejected so
        # the page can show an inline error instead of silently dropping.
        return redirect(url_for(
            "settings_team_view",
            saved="1",
            invalid_slack=",".join(invalid_slack[:5]),
        ))
    return redirect(url_for("settings_team_view", saved="1"))


# =====================================================================
# LinkedIn resolver wiring (tied to linkedin_resolver.py)
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
        active="settings_linkedin_resolver",
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


def _safe_int_env(name: str, default: int, floor: int = 1) -> int:
    """Read an int env var with a try/except so a fat-fingered value
    (e.g. RESOLVE_BATCH_WORKERS=foo) doesn't crash app boot. Floors at
    `floor` to guard against ThreadPoolExecutor(max_workers=0) raising
    at first request — silent foot-gun otherwise."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, int(raw))
    except ValueError:
        print(f"[draftboard-starter] {name}={raw!r} isn't an int — using default {default}.")
        return default


def _safe_float_env(name: str, default: float, floor: float = 0.0) -> float:
    """Same as _safe_int_env but for floats. Floors at `floor` so a
    negative value can't make time.sleep raise."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(floor, float(raw))
    except ValueError:
        print(f"[draftboard-starter] {name}={raw!r} isn't a float — using default {default}.")
        return default


# Default to a politer pace than the original 5-workers-no-delay: 2 workers,
# 0.5s delay between calls per worker. Cache hits skip the delay (no network
# call → no need to throttle). The original could blow Google CSE's free
# 100/day quota in seconds and trip Apollo's per-minute throttle on free
# tiers. Tunable via env if your keys can take more — both clamp to safe
# values on malformed input.
RESOLVE_BATCH_WORKERS = _safe_int_env("RESOLVE_BATCH_WORKERS", 2, floor=1)
RESOLVE_BATCH_DELAY_SEC = _safe_float_env("RESOLVE_BATCH_DELAY_SEC", 0.5, floor=0.0)


def _resolve_one_for_batch(name: str, email: str, keys: dict, force: bool) -> dict:
    """Single-row resolver used by the batch worker pool. Mirrors the
    cache-first logic of /candidates/resolve. Always returns the same shape
    so the caller can build a uniform response regardless of which branch
    fired (cache hit, malformed input, fresh resolution).

    After a fresh network call, sleeps RESOLVE_BATCH_DELAY_SEC so the worker
    paces itself — this is the rate-limit guard for Apollo + Google CSE +
    OpenAI. Cache hits and input-validation early-returns skip the sleep
    (no network call happened, no need to throttle)."""
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
            return cached  # already shape-matched — no network, no throttle

    result = resolve_linkedin(
        name, email,
        apollo_key=keys.get("apollo_api_key") or None,
        cse_key=keys.get("google_cse_api_key") or None,
        cse_id=keys.get("google_cse_id") or None,
        openai_key=keys.get("openai_api_key") or None,
    )
    db_put_resolution(email, name, result)
    result.pop("_transient", None)
    # Per-worker pacing — runs AFTER the network call, so the next call from
    # this worker has to wait. With 2 workers, peak rate is ~2/(latency+delay)
    # which for ~1s latency means ~1.3 req/sec — gentle enough for free tiers.
    if RESOLVE_BATCH_DELAY_SEC > 0:
        time.sleep(RESOLVE_BATCH_DELAY_SEC)
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
