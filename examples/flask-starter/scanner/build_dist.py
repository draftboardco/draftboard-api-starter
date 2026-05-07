#!/usr/bin/env python3
"""Build the distributable supporter_scan.py.

Reads your Desktop OAuth client_id + client_secret from
~/.draftboard-secrets/google.env (looking for DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID
+ DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET, with plain GOOGLE_CLIENT_ID/_SECRET
as fallbacks), substitutes them into the template scanner/supporter_scan.py,
and writes the result to scanner/dist/supporter_scan.py.

The dist/ directory is .gitignored — the populated file is meant for direct
distribution to teammates (Slack DM, email, gist), not committed to a public
repo. Anyone with the populated file can use your OAuth client to consent on
their own Google account.

Usage:
    cd examples/flask-starter
    python scanner/build_dist.py

Then send `scanner/dist/supporter_scan.py` to your teammate.
"""

import os
import shutil
import sys


HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "supporter_scan.py")
DIST_DIR = os.path.join(HERE, "dist")
OUT = os.path.join(DIST_DIR, "supporter_scan.py")


def _parse_env(path):
    out = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].strip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _load_credentials():
    """Look in env vars first, then ~/.draftboard-secrets/google.env."""
    cid = os.environ.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID", "").strip()
    cs = os.environ.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET", "").strip()
    if cid and cs:
        return cid, cs, "environment"

    secrets = os.path.expanduser("~/.draftboard-secrets/google.env")
    if os.path.exists(secrets):
        vals = _parse_env(secrets)
        cid = (vals.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID")
               or vals.get("GOOGLE_CLIENT_ID") or "").strip()
        cs = (vals.get("DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET")
              or vals.get("GOOGLE_CLIENT_SECRET") or "").strip()
        if cid and cs:
            return cid, cs, secrets

    return "", "", None


def main():
    if not os.path.exists(TEMPLATE):
        sys.stderr.write(f"Template not found at {TEMPLATE}\n")
        sys.exit(1)

    cid, cs, source = _load_credentials()
    if not cid or not cs:
        sys.stderr.write(
            "\nNo Desktop OAuth credentials found.\n\n"
            "Set DRAFTBOARD_SCANNER_GOOGLE_CLIENT_ID + DRAFTBOARD_SCANNER_GOOGLE_CLIENT_SECRET\n"
            "as env vars, OR add them to ~/.draftboard-secrets/google.env, then re-run.\n\n"
            "These should come from a Desktop-type OAuth client in your Google Cloud project\n"
            "(not the Web client used by the Flask app — different type, different file).\n"
        )
        sys.exit(1)

    with open(TEMPLATE) as f:
        body = f.read()

    if "__CLIENT_ID__" not in body or "__CLIENT_SECRET__" not in body:
        sys.stderr.write(
            "\nTemplate doesn't contain the expected placeholders __CLIENT_ID__ /\n"
            "__CLIENT_SECRET__. Either it's already been populated, or the template\n"
            "format has drifted.\n"
        )
        sys.exit(1)

    populated = body.replace("__CLIENT_ID__", cid).replace("__CLIENT_SECRET__", cs)

    os.makedirs(DIST_DIR, exist_ok=True)
    with open(OUT, "w") as f:
        f.write(populated)
    try:
        os.chmod(OUT, 0o755)
    except OSError:
        pass

    print(f"✓ Built {OUT}")
    print(f"  Credentials source: {source}")
    print(f"  client_id: {cid[:30]}…")
    print()
    print("Next: DM this file directly to a teammate (Slack attachment, email,")
    print("private gist). They run it on their laptop with `python3 supporter_scan.py`,")
    print("OAuth in their browser, get a JSON file, and send it back to you.")
    print()
    print("Do NOT commit dist/ to git — it's already gitignored.")


if __name__ == "__main__":
    main()
