"""Sample data generator + loader + clearer for the Draftboard API Starter.

What it does
------------
Seeds a fresh install with a plausible-looking workspace so first-run users
can poke around the product before they have an API key or real data.
~100 targets across 25 well-known SaaS companies, ~40 connectors drawn from
real VCs and operator-investors, with score-driven scoreDetails so the
bullets on each connector card actually match the numerical score.

When does it run
----------------
On every boot, the app checks three signals:
  1. `targets_cache` has zero rows (fresh install / wiped db).
  2. `app_state.sample_data_seeded` is unset (never seeded before).
  3. `app_state.sample_data_cleared` is unset (real sync hasn't run yet).
  4. env var `LOAD_SAMPLE_DATA` is not "0".

All four must hold — otherwise we skip the seed. After seeding,
`sample_data_seeded='1'` is written so we don't seed again on the next
boot.

When does it get cleared
------------------------
The first time `db_save_targets_cache` writes real data (i.e. the user
has set up an API key + triggered a sync), the clearer runs:
  DELETE FROM <table> WHERE _is_sample = 1
across every table the seeder populated, then writes
`sample_data_cleared='1'`. CLAUDE.md explicitly allows this because the
WHERE is scoped to rows our branch owns.

A one-time green banner on /targets confirms the clear; dismissable.

Determinism
-----------
random.seed(SEED) at the top of `generate_sample_data` makes the output
identical across runs and machines. Predictable for screenshots, docs,
and your own muscle memory.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Dict, List, Tuple

SEED = 42

# ---------------------------------------------------------------------------
# Pools
# ---------------------------------------------------------------------------

# Real, recognizable VCs + operator-investors. Some overlap with lean-intros'
# pool intentionally — kit users moving between the two products see
# familiar names. Each is a *connector* in our sample data, meaning they can
# intro the user to N prospects.
CONNECTORS: List[Dict] = [
    {"first": "Sarah", "last": "Guo", "title": "Founder & Managing Partner", "company": "Conviction",
     "linkedin": "https://www.linkedin.com/in/sarahguo", "school": "Stanford"},
    {"first": "Garry", "last": "Tan", "title": "President & CEO", "company": "Y Combinator",
     "linkedin": "https://www.linkedin.com/in/garrytan", "school": "Stanford"},
    {"first": "Olivia", "last": "Moore", "title": "Partner", "company": "a16z",
     "linkedin": "https://www.linkedin.com/in/oliviamoore0", "school": "Stanford GSB"},
    {"first": "Elad", "last": "Gil", "title": "Solo Capitalist", "company": "Color Genomics",
     "linkedin": "https://www.linkedin.com/in/eladgil", "school": "MIT"},
    {"first": "Lenny", "last": "Rachitsky", "title": "Author", "company": "Lenny's Newsletter",
     "linkedin": "https://www.linkedin.com/in/lennyrachitsky", "school": "Carnegie Mellon"},
    {"first": "Auren", "last": "Hoffman", "title": "CEO", "company": "SafeGraph",
     "linkedin": "https://www.linkedin.com/in/aurenhoffman", "school": "UC Berkeley"},
    {"first": "Tomasz", "last": "Tunguz", "title": "Founder", "company": "Theory Ventures",
     "linkedin": "https://www.linkedin.com/in/tomasztunguz", "school": "Dartmouth"},
    {"first": "Lulu Cheng", "last": "Meservey", "title": "Founder & CEO", "company": "Rostra",
     "linkedin": "https://www.linkedin.com/in/lulumeservey", "school": "Princeton"},
    {"first": "Packy", "last": "McCormick", "title": "Founder", "company": "Not Boring Capital",
     "linkedin": "https://www.linkedin.com/in/packymccormick", "school": "Notre Dame"},
    {"first": "Lulu", "last": "Cheng", "title": "VP Comms", "company": "Substack",
     "linkedin": "https://www.linkedin.com/in/lulucheng2", "school": "Princeton"},
    {"first": "Naval", "last": "Ravikant", "title": "Founder", "company": "AngelList",
     "linkedin": "https://www.linkedin.com/in/navalravikant", "school": "Dartmouth"},
    {"first": "Harry", "last": "Stebbings", "title": "Founder", "company": "20VC",
     "linkedin": "https://www.linkedin.com/in/harrystebbings", "school": "UCL"},
    {"first": "Ryan", "last": "Hoover", "title": "Founder", "company": "Product Hunt",
     "linkedin": "https://www.linkedin.com/in/ryanhoover", "school": "Cal Poly"},
    {"first": "Andrew", "last": "Chen", "title": "General Partner", "company": "a16z",
     "linkedin": "https://www.linkedin.com/in/andrewchen", "school": "UC Berkeley"},
    {"first": "Hunter", "last": "Walk", "title": "Partner", "company": "Homebrew",
     "linkedin": "https://www.linkedin.com/in/hunterwalk", "school": "Vassar"},
    {"first": "Semil", "last": "Shah", "title": "GP", "company": "Haystack",
     "linkedin": "https://www.linkedin.com/in/semilshah", "school": "UVA"},
    {"first": "Charles", "last": "Hudson", "title": "Managing Partner", "company": "Precursor Ventures",
     "linkedin": "https://www.linkedin.com/in/charleshudson", "school": "Stanford"},
    {"first": "Kanyi", "last": "Maqubela", "title": "Managing Partner", "company": "Kindred Ventures",
     "linkedin": "https://www.linkedin.com/in/kanyimaqubela", "school": "Stanford"},
    {"first": "Soraya", "last": "Darabi", "title": "Co-Founder", "company": "TMV",
     "linkedin": "https://www.linkedin.com/in/sorayadarabi", "school": "Georgetown"},
    {"first": "Niko", "last": "Bonatsos", "title": "Managing Director", "company": "General Catalyst",
     "linkedin": "https://www.linkedin.com/in/nikobonatsos", "school": "Stanford GSB"},
    {"first": "Sahil", "last": "Lavingia", "title": "Founder & CEO", "company": "Gumroad",
     "linkedin": "https://www.linkedin.com/in/sahillavingia", "school": "USC"},
    {"first": "Roxanne", "last": "Petraeus", "title": "CEO", "company": "Ethena",
     "linkedin": "https://www.linkedin.com/in/roxannepetraeus", "school": "Harvard"},
    {"first": "Tobi", "last": "Lütke", "title": "CEO", "company": "Shopify",
     "linkedin": "https://www.linkedin.com/in/tobilutke", "school": "Ottawa"},
    {"first": "Calvin", "last": "French-Owen", "title": "Co-Founder", "company": "Segment",
     "linkedin": "https://www.linkedin.com/in/calvinfrenchowen", "school": "MIT"},
    {"first": "Suhail", "last": "Doshi", "title": "Founder", "company": "Playground AI",
     "linkedin": "https://www.linkedin.com/in/suhaildoshi", "school": "Arizona State"},
    {"first": "Patrick", "last": "Collison", "title": "CEO", "company": "Stripe",
     "linkedin": "https://www.linkedin.com/in/patrickcollison", "school": "MIT"},
    {"first": "Aaron", "last": "Levie", "title": "CEO", "company": "Box",
     "linkedin": "https://www.linkedin.com/in/aaronlevie", "school": "USC"},
    {"first": "Mathilde", "last": "Collin", "title": "CEO", "company": "Front",
     "linkedin": "https://www.linkedin.com/in/mathildecollin", "school": "ESCP"},
    {"first": "Kat", "last": "Manalac", "title": "Partner", "company": "Y Combinator",
     "linkedin": "https://www.linkedin.com/in/katmanalac", "school": "Amherst"},
    {"first": "Pete", "last": "Koomen", "title": "Co-Founder", "company": "Optimizely",
     "linkedin": "https://www.linkedin.com/in/petekoomen", "school": "Stanford"},
    {"first": "Jason", "last": "Lemkin", "title": "Founder", "company": "SaaStr",
     "linkedin": "https://www.linkedin.com/in/jasonlemkin", "school": "Berkeley"},
    {"first": "Anu", "last": "Hariharan", "title": "Founder", "company": "Avra",
     "linkedin": "https://www.linkedin.com/in/anuhariharan", "school": "NYU"},
    {"first": "Tomer", "last": "London", "title": "Co-Founder", "company": "Gusto",
     "linkedin": "https://www.linkedin.com/in/tomerlondon", "school": "Stanford"},
    {"first": "Eric", "last": "Yuan", "title": "CEO", "company": "Zoom",
     "linkedin": "https://www.linkedin.com/in/ericsyuan", "school": "Stanford"},
    {"first": "Henrique", "last": "Dubugras", "title": "Co-CEO", "company": "Brex",
     "linkedin": "https://www.linkedin.com/in/henriquedubugras", "school": "Stanford"},
    {"first": "Pedro", "last": "Franceschi", "title": "Co-CEO", "company": "Brex",
     "linkedin": "https://www.linkedin.com/in/pedrofranceschi", "school": "Stanford"},
    {"first": "Brianne", "last": "Kimmel", "title": "Founder", "company": "Worklife Ventures",
     "linkedin": "https://www.linkedin.com/in/briannekimmel", "school": "Lehigh"},
    {"first": "Immad", "last": "Akhund", "title": "CEO", "company": "Mercury",
     "linkedin": "https://www.linkedin.com/in/immad", "school": "Cambridge"},
    {"first": "Dimitri", "last": "Sirota", "title": "CEO", "company": "BigID",
     "linkedin": "https://www.linkedin.com/in/dimitrisirota", "school": "McGill"},
    {"first": "Mihika", "last": "Kapoor", "title": "Product", "company": "Figma",
     "linkedin": "https://www.linkedin.com/in/mihikakapoor", "school": "Wharton"},
]

# Target prospects live at well-known SaaS / AI companies. Each company gets
# 3-5 prospects to feel real (a real workspace would have a VP, a CMO, a
# Head of GTM each separately tracked at the same account).
TARGET_COMPANIES = [
    "Notion", "Linear", "Vercel", "Ramp", "Retool", "Plaid", "Brex",
    "Mercury", "Modern Treasury", "Cresta", "Persona", "Anthropic", "Figma",
    "Stripe", "Datadog", "Snowflake", "Databricks", "OpenAI", "Hugging Face",
    "Scale AI", "Replicate", "Glean", "Harvey", "Cursor", "Webflow",
]

TARGET_TITLES = [
    "VP Sales", "Head of Sales", "Chief Revenue Officer", "VP Revenue",
    "Head of Revenue Operations", "VP Marketing", "CMO", "Head of Growth",
    "VP Customer Success", "Director of Sales", "Sales Operations Lead",
    "Head of GTM", "VP GTM Strategy", "Director of Marketing",
    "Head of Enterprise Sales", "Director of Demand Generation",
    "VP Business Development", "Head of Partnerships",
    "Director of Revenue Operations", "Head of Sales Development",
    "VP Marketing Operations", "Head of Customer Marketing",
    "Director of Product Marketing", "VP Field Marketing",
    "Director of Sales Enablement",
]

# Pool for the prospect names. Mixed-gender, mixed-origin so the sample
# workspace doesn't read like a directory of one demographic.
FIRST_NAMES = [
    "Maya", "Jean-David", "Yuki", "Marcus", "Sasha", "Priya", "Tomás",
    "Aisha", "Liam", "Zoe", "Kenji", "Amara", "Jonas", "Léa", "Ravi",
    "Hannah", "Diego", "Saoirse", "Nikolai", "Mei", "Ezra", "Anya",
    "Felix", "Tara", "Bilal", "Frida", "Mateo", "Iris", "Noor", "Cyrus",
    "Camille", "Arjun", "Sofia", "Theo", "Yara", "Bastien", "Aida",
    "Ezra", "Mira", "Hugo", "Naomi", "Cassidy", "Pablo", "Mikhail",
    "Sienna", "Reza", "Bea", "Damir", "Léon", "Tova",
]
LAST_NAMES = [
    "Patel", "Bismuth", "Tanaka", "Chen", "Reyes", "Rao", "Silva",
    "Khan", "O'Brien", "Park", "Yoshida", "Okonkwo", "Müller", "Dubois",
    "Sharma", "Cohen", "Hernandez", "Murphy", "Sokolov", "Wong",
    "Goldberg", "Volkov", "Schmidt", "Kapoor", "Ahmed", "Lindqvist",
    "Garcia", "O'Sullivan", "Hassan", "Esfahani", "Lefèvre", "Iyer",
    "Romano", "Becker", "Mansour", "Caron", "El-Sayed", "Levin",
    "Stein", "Tanaka", "Watanabe", "Choi", "Costa", "Petrov", "Nilsson",
    "Tehrani", "Akhtar", "Babic", "Bertrand", "Ben-David",
]

# Pool for "worked together at" scoreDetails. These are the well-known
# career stops that LinkedIn relationships often build through.
WORK_COMPANIES = [
    "Stripe", "Google", "Meta", "Airbnb", "Uber", "Twitter (X)", "Block",
    "Square", "Slack", "Salesforce", "Dropbox", "LinkedIn", "Amazon",
    "Apple", "Microsoft", "Y Combinator", "Sequoia Capital", "a16z",
    "First Round", "Bessemer", "Greylock", "Index", "OpenAI", "Anthropic",
    "Notion", "Linear", "Vercel", "Plaid", "Figma", "Brex", "Mercury",
    "Doordash", "Instacart", "Pinterest", "Snap",
]

SCHOOLS = [
    "Stanford", "MIT", "Harvard", "Wharton", "UC Berkeley", "Carnegie Mellon",
    "Princeton", "Columbia", "Yale", "Dartmouth", "Tel Aviv University",
    "INSEAD", "London Business School", "University of Pennsylvania",
    "USC", "NYU Stern",
]

# User-typed tags applied to ~25 sample targets to demo the tag filter.
SAMPLE_TAGS = ["warm-q3", "saas-CRO", "from-summit-2024", "high-priority", "needs-research"]


# ---------------------------------------------------------------------------
# Score-driven scoreDetails generator
# ---------------------------------------------------------------------------

def _format_duration(months: int) -> str:
    """Match the existing `_DURATION_WRAPPER_RE` shape so the runtime UI
    can re-extract `months` from these strings. Bins by length so the
    wrapper word feels realistic (4 months = short period, 60 = long time)."""
    if months <= 6:
        bucket = "a short period"
    elif months <= 18:
        bucket = "a short time"
    elif months <= 36:
        bucket = "a little while"
    else:
        bucket = "a long time"
    return f"{bucket} ({months} months)"


def _detail_work_overlap(company: str, months: int, year: int) -> str:
    """Match `_OVERLAP_RE` shape:
       "They overlapped for <duration> @ <company> in <year>, most recently in <year>"
    """
    duration = _format_duration(months)
    return f"They overlapped for {duration} @ {company} in {year}, most recently in {year}"


def _detail_school(school: str) -> str:
    """Match `_SCHOOL_RE`: "They went to <school> together"."""
    return f"They went to {school} together"


def _detail_mutuals(n: int) -> str:
    """Match `_MUTUALS_RE`: "They have N mutual connections"."""
    if n >= 25:
        return f"They have a lot ({n}) of mutual connections"
    return f"They have {n} mutual connections"


def _detail_both_worked(company: str, note: str = "") -> str:
    """Match `_BOTH_WORKED_RE`: "They both worked @ <company> (note)"."""
    if note:
        return f"They both worked @ {company} ({note})"
    return f"They both worked @ {company}"


def _generate_details_for_score(rng: random.Random, score: int) -> List[str]:
    """Return a list of scoreDetails strings whose strength MATCHES the
    numerical score. Score-source consistency: a 92 reads
        - long work overlap
        - both went to <school>
        - lots of mutuals
    while a 35 reads
        - 5 mutual connections
    No more "55 looks the same as 93" surprise.
    """
    details: List[str] = []
    if score >= 80:
        # Strong: long work overlap + school + many mutuals
        details.append(_detail_work_overlap(
            rng.choice(WORK_COMPANIES),
            rng.randint(24, 60),
            rng.randint(2018, 2024),
        ))
        if rng.random() < 0.75:
            details.append(_detail_school(rng.choice(SCHOOLS)))
        details.append(_detail_mutuals(rng.randint(35, 95)))
    elif score >= 65:
        # Medium-strong: one strong signal + mutuals
        if rng.random() < 0.55:
            details.append(_detail_work_overlap(
                rng.choice(WORK_COMPANIES),
                rng.randint(12, 30),
                rng.randint(2015, 2022),
            ))
        else:
            details.append(_detail_school(rng.choice(SCHOOLS)))
        details.append(_detail_mutuals(rng.randint(15, 45)))
    elif score >= 45:
        # Medium: brief work overlap OR mutuals only
        if rng.random() < 0.40:
            details.append(_detail_both_worked(
                rng.choice(WORK_COMPANIES),
                "no overlap" if rng.random() < 0.5 else "",
            ))
        details.append(_detail_mutuals(rng.randint(6, 22)))
    else:
        # Weak: a few mutuals, that's it. Honest about why the score is low.
        details.append(_detail_mutuals(rng.randint(2, 8)))
    return details


def _score_distribution(rng: random.Random) -> int:
    """Realistic score distribution — most paths cluster in 40-70, a small
    number above 80 (the "this one is special" tail), a tail below 30 that
    explains why the user wouldn't bother."""
    return rng.choices(
        population=[
            rng.randint(18, 35),
            rng.randint(36, 55),
            rng.randint(56, 75),
            rng.randint(76, 92),
        ],
        weights=[12, 38, 36, 14],
    )[0]


# ---------------------------------------------------------------------------
# Identity helpers
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    return name.lower().replace(" ", "-").replace("'", "").replace(".", "")


def _connection_id(target_id: str, connector_first: str, connector_last: str) -> str:
    """Stable per-pair ID so re-generation produces identical rows. Hash so
    it looks like an API-issued UUID-ish blob."""
    raw = f"{target_id}::{connector_first}::{connector_last}".encode("utf-8")
    return "sample_conn_" + hashlib.sha256(raw).hexdigest()[:24]


def _initials(first: str, last: str) -> str:
    return ((first[:1] or "?") + (last[:1] or "")).upper()


def _compute_connector_key_local(connection: dict) -> str:
    """Stand-in for the app's `_compute_connector_key`. Same logic — use the
    LinkedIn URL when available, otherwise hash the name. We can't import
    from app.py because this module is imported FROM app.py."""
    li = (connection.get("linkedinUrl") or "").strip().lower()
    if li:
        return "li:" + li
    name = ((connection.get("firstName") or "") + " " + (connection.get("lastName") or "")).strip()
    return "name:" + hashlib.sha1(name.lower().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------

def generate_sample_data() -> Dict:
    """Return a dict of all the rows to insert. Pure function — no DB access.

    Output shape:
      {
        "targets":          [target_dict, ...],
        "connections":      {target_id: [connection_dict, ...]},
        "connector_paths":  [(connector_key, target_id, connection_id, first, last, linkedin, title, company, score), ...],
        "discovered_paths": [(target_id, connection_id, score, first_seen_at, last_seen_at), ...],
        "manual_list":      {label, owner_first, owner_last, owner_email, owner_title, owner_company, owner_linkedin, owner_slack_id, uploaded_at},
        "manual_rows":      [{first_name, last_name, email, company, position, connected_on, linkedin_url, linkedin_url_normalized}, ...],
        "intro_requests":   [(target_id, connection_id, status), ...],
        "target_tags":      [(target_id, tag), ...],
        "supporters":       [{email, name, source, contributor, resolved_linkedin}, ...],
      }
    """
    rng = random.Random(SEED)

    # 100 targets — 4 per company on average across 25 companies.
    targets: List[Dict] = []
    used_names: set = set()
    for company in TARGET_COMPANIES:
        n_for_company = rng.randint(3, 5)
        for _ in range(n_for_company):
            # Avoid duplicate (first, last) within sample data so the URL
            # slug stays unique.
            for _ in range(20):
                first = rng.choice(FIRST_NAMES)
                last = rng.choice(LAST_NAMES)
                if (first, last) not in used_names:
                    used_names.add((first, last))
                    break
            title = rng.choice(TARGET_TITLES)
            slug = f"{_slug(first)}-{_slug(last)}-{rng.randint(100, 999)}"
            target_id = f"sample_tgt_{slug}"
            linkedin_url = f"https://www.linkedin.com/in/{slug}"
            targets.append({
                "id": target_id,
                "firstName": first,
                "lastName": last,
                "linkedinUrl": linkedin_url,
                "position": {"title": title, "companyName": company, "companyLinkedinUrl": ""},
                "score": 0,  # filled below from max(connection scores)
                "connectionsNumber": 0,  # filled below
                "tags": [],
                "status": "",
                "updatedAt": "",
            })
    # Truncate to 100 (if loop overshoots) for predictability.
    targets = targets[:100]

    # ~150 connector_paths: each connector → 3-5 targets, average ~3.75 to
    # land on 150. Sample WITHOUT replacement within a connector so the same
    # connector doesn't double-back to the same target.
    connections_by_tid: Dict[str, List[Dict]] = {t["id"]: [] for t in targets}
    connector_paths: List[Tuple] = []
    discovered_paths: List[Tuple] = []
    now = int(time.time())

    for connector in CONNECTORS:
        n_targets = rng.randint(3, 5)
        chosen = rng.sample(targets, k=min(n_targets, len(targets)))
        for t in chosen:
            score = _score_distribution(rng)
            cid = _connection_id(t["id"], connector["first"], connector["last"])
            details = _generate_details_for_score(rng, score)
            conn_obj = {
                "id": cid,
                "firstName": connector["first"],
                "lastName": connector["last"],
                "linkedinUrl": connector["linkedin"],
                "position": {"title": connector["title"], "companyName": connector["company"]},
                "score": score,
                "scoreDetails": details,
                "owners": [],  # api-mode owners — empty for sample
                # Sample marker — flows through `_enrich_connection` to the
                # SAMPLE watermark on the connector card. Survives the JSON
                # round-trip (read back from connections.connections_json).
                "_is_sample": True,
            }
            connections_by_tid[t["id"]].append(conn_obj)
            key = _compute_connector_key_local(conn_obj)
            connector_paths.append((
                key, t["id"], cid,
                connector["first"], connector["last"], connector["linkedin"],
                connector["title"], connector["company"], score,
            ))
            # Stagger first_seen_at over the last 30 days so /new-paths
            # has data to render under "Last 24h" / "Last 7d" / "Last 30d".
            days_ago = rng.randint(0, 30)
            first_seen = now - days_ago * 86400
            discovered_paths.append((t["id"], cid, score, first_seen, first_seen))

    # Roll up target.score = max(connection scores), connectionsNumber = count.
    # Also drop targets with zero connectors (~5-10 will have none — that's
    # realistic, not every prospect has a path).
    final_targets: List[Dict] = []
    for t in targets:
        conns = connections_by_tid.get(t["id"], [])
        t["connectionsNumber"] = len(conns)
        t["score"] = max((c["score"] for c in conns), default=0)
        final_targets.append(t)

    # Manual list — Sarah Chen's network export. 18 contacts, 5 of which
    # match real sample targets (so the cross-feature shows). The 13
    # fillers are plausible-looking names that don't match anything.
    overlap_pool = rng.sample(final_targets, k=5)
    manual_rows: List[Dict] = []
    for t in overlap_pool:
        manual_rows.append({
            "first_name": t["firstName"],
            "last_name": t["lastName"],
            "email": "",
            "company": t["position"]["companyName"],
            "position": t["position"]["title"],
            "connected_on": "15 Mar 2024",
            "linkedin_url": t["linkedinUrl"],
        })
    # Fillers
    for _ in range(13):
        f = rng.choice(FIRST_NAMES)
        l = rng.choice(LAST_NAMES)
        slug = f"{_slug(f)}-{_slug(l)}-{rng.randint(10, 999)}"
        manual_rows.append({
            "first_name": f,
            "last_name": l,
            "email": "",
            "company": rng.choice(WORK_COMPANIES),
            "position": rng.choice(TARGET_TITLES),
            "connected_on": "08 Jan 2024",
            "linkedin_url": f"https://www.linkedin.com/in/{slug}",
        })
    rng.shuffle(manual_rows)

    manual_list = {
        "label": "[Sample] Sarah Chen's network export",
        "owner_first": "Sarah",
        "owner_last": "Chen",
        "owner_email": "sarah@example.com",
        "owner_title": "Founder & CEO",
        "owner_company": "Acme (Series A)",
        "owner_linkedin": "https://www.linkedin.com/in/sarahchen-sample",
        "owner_slack_id": "",
        "uploaded_at": now - 12 * 86400,
    }

    # intro_requests — spread across the funnel so the status pills/rollups
    # have data on day 1. Pick 8 random paths.
    funnel = ["requested", "in_progress", "intro_made", "no_reply", "connector_rejected", "requested", "in_progress", "intro_made"]
    paths_for_intro = rng.sample(connector_paths, k=min(8, len(connector_paths)))
    intro_requests: List[Tuple] = []
    for status, path in zip(funnel, paths_for_intro):
        _key, tid, cid, *_rest = path
        intro_requests.append((tid, cid, status))

    # Target tags — assign each of the 5 sample tags to ~5 random targets.
    target_tags: List[Tuple] = []
    for tag in SAMPLE_TAGS:
        for t in rng.sample(final_targets, k=5):
            target_tags.append((t["id"], tag))

    # Supporters — 6 scanner-imported supporters; 3 LinkedIn-resolved to
    # connectors we already have in the sample, so the ⭐ Supporters filter
    # has something to filter and the badge cross-reference works.
    # The other 3 are name+email only ("scanner picked them up but they
    # haven't been resolved yet") — drives the unresolved-supporters count
    # on /supporters/candidates.
    resolved_to_connectors = rng.sample(CONNECTORS, k=3)
    supporters = []
    for c in resolved_to_connectors:
        supporters.append({
            "email": f"{_slug(c['first'])}@{_slug(c['company'])}.example",
            "name": f"{c['first']} {c['last']}",
            "source": "scanner",
            "contributor": "you",
            "resolved_linkedin": c["linkedin"],
        })
    # Unresolved fillers
    for _ in range(3):
        f = rng.choice(FIRST_NAMES)
        l = rng.choice(LAST_NAMES)
        supporters.append({
            "email": f"{_slug(f)}.{_slug(l)}@example.com",
            "name": f"{f} {l}",
            "source": "scanner",
            "contributor": "you",
            "resolved_linkedin": "",
        })

    return {
        "targets": final_targets,
        "connections": connections_by_tid,
        "connector_paths": connector_paths,
        "discovered_paths": discovered_paths,
        "manual_list": manual_list,
        "manual_rows": manual_rows,
        "intro_requests": intro_requests,
        "target_tags": target_tags,
        "supporters": supporters,
    }


# ---------------------------------------------------------------------------
# Loader + clearer — INSERTs into the api-starter DB with _is_sample=1
# ---------------------------------------------------------------------------

# Tables that hold sample data. Order matters for clear (children before
# parents on FK dependencies — but most of these use ON DELETE CASCADE
# anyway, so the order is forgiving).
SAMPLE_TABLES = [
    "manual_path_connections",
    "manual_path_lists",
    "intro_requests",
    "target_tags",
    "discovered_paths",
    "connector_paths",
    "connections",
    "targets_cache",
    "teammate_contacts",
    "linkedin_resolutions",
]


def _normalize_linkedin(url: str) -> str:
    """Stand-in for the app's `_normalize_linkedin`. Strips scheme/host
    variants and trailing slashes so URLs match across the supporter map
    + connector paths."""
    if not url:
        return ""
    u = url.strip().lower()
    if u.startswith("http://"):
        u = u[7:]
    elif u.startswith("https://"):
        u = u[8:]
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/").split("?")[0].split("#")[0]


def load_sample_data(db_connect, db_lock, db_app_state_set) -> Dict[str, int]:
    """Insert generated sample data into the DB. Returns {table: rows_inserted}.

    Called from boot, behind the 4-signal guard documented at the top of
    this module. Every INSERT carries `_is_sample=1` so the clearer can
    surgically remove our rows without touching real data.
    """
    data = generate_sample_data()
    inserted: Dict[str, int] = {}
    now = int(time.time())

    with db_lock, db_connect() as conn:
        # targets_cache — schema column is `data_json` not `target_json`
        for t in data["targets"]:
            conn.execute(
                "INSERT OR REPLACE INTO targets_cache "
                "(target_id, data_json, fetched_at, _is_sample) "
                "VALUES (?, ?, ?, 1)",
                (t["id"], json.dumps(t), now),
            )
        inserted["targets_cache"] = len(data["targets"])

        # connections (per-target JSON blob)
        for tid, conns in data["connections"].items():
            conn.execute(
                "INSERT OR REPLACE INTO connections "
                "(target_id, connections_json, fetched_at, error, _is_sample) "
                "VALUES (?, ?, ?, NULL, 1)",
                (tid, json.dumps(conns), now),
            )
        inserted["connections"] = len(data["connections"])

        # connector_paths
        for row in data["connector_paths"]:
            conn.execute(
                "INSERT OR REPLACE INTO connector_paths "
                "(connector_key, target_id, connection_id, connector_first, connector_last, "
                " connector_linkedin, connector_title, connector_company, score, _is_sample) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                row,
            )
        inserted["connector_paths"] = len(data["connector_paths"])

        # discovered_paths
        for row in data["discovered_paths"]:
            conn.execute(
                "INSERT OR REPLACE INTO discovered_paths "
                "(target_id, connection_id, score, first_seen_at, last_seen_at, _is_sample) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                row,
            )
        inserted["discovered_paths"] = len(data["discovered_paths"])

        # manual list — schema has no owner_slack_id column
        ml = data["manual_list"]
        cur = conn.execute(
            "INSERT INTO manual_path_lists "
            "(label, owner_first, owner_last, owner_email, owner_title, owner_company, "
            " owner_linkedin, uploaded_at, _is_sample) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (ml["label"], ml["owner_first"], ml["owner_last"], ml["owner_email"],
             ml["owner_title"], ml["owner_company"], ml["owner_linkedin"],
             ml["uploaded_at"]),
        )
        list_id = cur.lastrowid
        for r in data["manual_rows"]:
            conn.execute(
                "INSERT INTO manual_path_connections "
                "(list_id, first_name, last_name, email, company, position, connected_on, "
                " linkedin_url, linkedin_url_normalized, _is_sample) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                (list_id, r["first_name"], r["last_name"], r["email"], r["company"],
                 r["position"], r["connected_on"], r["linkedin_url"],
                 _normalize_linkedin(r["linkedin_url"])),
            )
        inserted["manual_path_lists"] = 1
        inserted["manual_path_connections"] = len(data["manual_rows"])

        # intro_requests — schema has requested_at (legacy) + status + last_updated_at
        for tid, cid, status in data["intro_requests"]:
            conn.execute(
                "INSERT OR REPLACE INTO intro_requests "
                "(target_id, connection_id, requested_at, status, last_updated_at, _is_sample) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                (tid, cid, now, status, now),
            )
        inserted["intro_requests"] = len(data["intro_requests"])

        # target_tags — needs created_at + tag_type
        for tid, tag in data["target_tags"]:
            conn.execute(
                "INSERT OR REPLACE INTO target_tags "
                "(target_id, tag, created_at, tag_type, _is_sample) VALUES (?, ?, ?, 'user', 1)",
                (tid, tag, now),
            )
        inserted["target_tags"] = len(data["target_tags"])

        # supporters → teammate_contacts (schema uses contributor_email +
        # contributor_name, plus per-contact aggregate counters). For sample
        # data we leave the counters at default 0; the badge logic only
        # cares about the row existing + the LinkedIn resolution.
        for s in data["supporters"]:
            conn.execute(
                "INSERT OR IGNORE INTO teammate_contacts "
                "(contributor_email, contributor_name, email, name, imported_at, _is_sample) "
                "VALUES (?, ?, ?, ?, ?, 1)",
                ("you@example.com", "You", s["email"], s["name"], now),
            )
            if s["resolved_linkedin"]:
                conn.execute(
                    "INSERT OR REPLACE INTO linkedin_resolutions "
                    "(email, linkedin_url, source, confidence, resolved_at, _is_sample) "
                    "VALUES (?, ?, 'sample', 'high', ?, 1)",
                    (s["email"], s["resolved_linkedin"], now),
                )
        inserted["teammate_contacts"] = len(data["supporters"])
        inserted["linkedin_resolutions"] = sum(1 for s in data["supporters"] if s["resolved_linkedin"])

        conn.commit()

    db_app_state_set("sample_data_seeded", "1")
    return inserted


def clear_sample_data(db_connect, db_lock, db_app_state_set) -> Dict[str, int]:
    """DELETE FROM <table> WHERE _is_sample = 1 across every seeded table.
    Allowed because the WHERE clause is strictly scoped to rows our branch
    inserted (per the api-starter CLAUDE.md's "tables you may wipe IF your
    branch added them" rule).

    Returns {table: rows_deleted}.
    """
    deleted: Dict[str, int] = {}
    with db_lock, db_connect() as conn:
        for table in SAMPLE_TABLES:
            try:
                cur = conn.execute(f"DELETE FROM {table} WHERE _is_sample = 1")
                deleted[table] = cur.rowcount
            except Exception:
                # Table might not exist on a very old install — skip rather
                # than crash the first-sync hook.
                deleted[table] = 0
        conn.commit()

    db_app_state_set("sample_data_cleared", "1")
    return deleted


def should_seed(db_connect, db_lock, db_app_state_get) -> bool:
    """4-signal guard: only seed when ALL of these hold.

    1. Env opt-out NOT set (`LOAD_SAMPLE_DATA` != "0").
    2. `sample_data_seeded` is unset (never seeded before).
    3. `sample_data_cleared` is unset (real sync hasn't run yet).
    4. `targets_cache` has zero rows (no real data).
    """
    import os
    # Accept any common falsy spelling, not just literal "0". A customer
    # setting `LOAD_SAMPLE_DATA=false` (or `no` / `off` / `False`) on their
    # public-internet VPS to skip the seed shouldn't get sample data
    # because their env var didn't happen to match a single magic string.
    if os.environ.get("LOAD_SAMPLE_DATA", "").strip().lower() in ("0", "false", "no", "off"):
        return False
    if db_app_state_get("sample_data_seeded") == "1":
        return False
    if db_app_state_get("sample_data_cleared") == "1":
        return False
    with db_lock, db_connect() as conn:
        cur = conn.execute("SELECT COUNT(*) FROM targets_cache")
        if cur.fetchone()[0] > 0:
            return False
    return True
