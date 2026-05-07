#!/usr/bin/env python3
"""Paced bootstrap of the local SQLite cache.

Hits the Draftboard API with explicit delays so we don't burst the
key. Default behavior:

    1. /me               (1 call, no delay) — validates the API key
    2. /targets          (paginated, ~10-20 back-to-back calls) — list
                         endpoint, populates targets_cache
    3. /targets/{id}/connections — for the top N targets by score, one
                         at a time with --delay seconds between calls.
                         Populates connections + target_owners +
                         discovered_paths + connector_paths via the
                         app's existing db_put_connections helper.

Usage:
    ./bootstrap_cache.py [N] [DELAY_SEC]

    N         number of top-scoring targets to fully sync (default 50)
    DELAY_SEC seconds between /targets/{id}/connections calls (default 1.5)

After it finishes, run the app with AUTO_SYNC_ENABLED=false so nothing
hits the API again unless you ask it to:

    AUTO_SYNC_ENABLED=false ./.venv/bin/python app.py
"""
import os
import sys
import time

# Prevent the scheduled-sync daemon from starting at app-module import.
# We'll flip the flag back on inside this process to make API calls
# explicitly — but we never want a background thread doing it for us.
os.environ["AUTO_SYNC_ENABLED"] = "false"

import app  # noqa: E402  (env var must be set before this import)

# Re-enable API calls in this process only (the daemon never started, but
# the fetch_* functions check this same flag).
app.AUTO_SYNC_ENABLED = True


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    delay = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5

    if not app.API_KEY:
        print("FAIL: no API key found. Drop one into "
              "~/.draftboard-secrets/draftboard-api-starter and retry.")
        sys.exit(1)

    print(f"Bootstrap: top {n} targets, {delay}s between connection calls.\n")

    # ---- 1. /me ----------------------------------------------------------
    print("[1/3] GET /me ... ", end="", flush=True)
    me, err = app.fetch_me(force=True)
    if err or not me:
        print(f"FAIL ({err})")
        sys.exit(1)
    user_full = (
        f"{me.get('user_first', '') or ''} "
        f"{me.get('user_last', '') or ''}"
    ).strip() or "(no name)"
    print(f"OK — {user_full}")

    # ---- 2. /targets list (paginated) ------------------------------------
    print("[2/3] GET /targets (paginated) ... ", end="", flush=True)
    t0 = time.time()
    targets, err = app.fetch_all_targets(force=True)
    if err and not targets:
        print(f"FAIL ({err})")
        sys.exit(1)
    print(f"OK — {len(targets)} targets in {time.time() - t0:.1f}s")
    if not targets:
        print("No targets returned. Nothing to sync.")
        return

    # ---- 3. /targets/{id}/connections for top N --------------------------
    targets.sort(key=lambda t: (t.get("score") or 0), reverse=True)
    top = targets[:n]
    print(f"[3/3] GET /targets/{{id}}/connections for top {len(top)} "
          f"(delay={delay}s):\n")

    success = 0
    fail = 0
    for i, t in enumerate(top, 1):
        tid = t.get("id")
        first = (t.get("firstName") or "").strip()
        last = (t.get("lastName") or "").strip()
        company = ((t.get("position") or {}).get("companyName") or "").strip()
        score = t.get("score") or 0
        label = f"{first} {last}".strip() or "(unnamed)"
        if company:
            label += f" @ {company}"
        prefix = f"  [{i:>3}/{len(top)}] (score {score}) {label[:60]}"
        print(f"{prefix:<82}", end="", flush=True)

        conns, err = app.fetch_target_connections(tid, force=True)
        if err:
            print(f"  FAIL ({err})")
            fail += 1
        else:
            print(f"  OK ({len(conns)} connections)")
            success += 1

        if i < len(top):
            time.sleep(delay)

    print("\nDone.")
    print(f"  Successfully synced: {success} / {len(top)}")
    print(f"  Failed:              {fail}")
    print()
    print("Run the app with auto-sync OFF so nothing else hits the API:")
    print("  AUTO_SYNC_ENABLED=false ./.venv/bin/python app.py")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n^C — interrupted. Partial data is in data.db (whatever "
              "completed before the interrupt).")
        sys.exit(130)
