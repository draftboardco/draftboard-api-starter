"""Auto-prospecting prototype: find similar people at an account.

Given a set of existing targets at Account X with various titles
(e.g., 'Director of Marketing', 'VP of Marketing'), this module:

  1. Asks gpt-4o-mini to expand those into ~12 adjacent titles
     (peer / one level up / one level down / synonyms).
  2. Resolves the account's canonical Apollo record (and its domain)
     via Apollo's company-search endpoint, with a LinkedIn-URL
     post-filter so generic names like 'Equals' pick the right co.
  3. Hits Apollo's people-search filtered by that company's domain
     and the title list, with `include_similar_titles=true` so
     "Director of Sales" also matches "Sales Director", "VP Sales",
     etc.

Pitch: drip a steady stream of "people you might also want to reach"
to users so they don't have to do prospecting themselves.

Reuses the same Apollo + OpenAI API keys the LinkedIn resolver uses
(see `linkedin_resolver.py` + `_load_resolver_keys` in app.py).
"""

import json
import sys

import requests

# Apollo: still used for company-name → canonical-domain resolution. We
# tried Apollo for people-search too, but their `mixed_people/api_search`
# returns `linkedin_url: None` on rows for plans without paid contact-reveal
# credits — which is what most kit users will have. We pivoted the people
# step to Google CSE (`_cse_search_people_at_company` below): CSE returns
# real LinkedIn profile URLs natively, no reveal credits required.
APOLLO_COMPANIES_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
APOLLO_PEOPLE_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
CSE_URL = "https://www.googleapis.com/customsearch/v1"
OPENAI_MODEL = "gpt-4o-mini"

DEFAULT_TITLE_COUNT = 12
APOLLO_PER_PAGE = 20

# CSE returns up to 10 results per query. We do one query per title, so
# `results_per_title` caps how many LinkedIn URLs we ask for per title.
CSE_RESULTS_PER_TITLE = 5


def _log(msg: str) -> None:
    print(f"[prospecting] {msg}", file=sys.stderr, flush=True)


# ---------- Title generation ---------------------------------------------

def generate_adjacent_titles(
    existing_titles: list[str],
    company: str,
    openai_key: str,
    n: int = DEFAULT_TITLE_COUNT,
) -> tuple[list[str], str | None]:
    """Ask gpt-4o-mini to expand a list of seed titles to N adjacent titles.

    Returns (titles, error). The model is instructed to avoid echoing the
    seeds verbatim and to stay within a similar seniority band — no CEOs
    unless that's the seed pattern, no interns either.
    """
    if not openai_key:
        return [], "openai key not configured"
    if not existing_titles:
        return [], "no seed titles to expand from"
    try:
        from openai import OpenAI
    except ImportError:
        return [], "openai package not installed"

    seed_lines = "\n".join(f"- {t}" for t in existing_titles if t)
    prompt = (
        f"At the company '{company}', a salesperson has identified these targets:\n"
        f"{seed_lines}\n\n"
        f"Return a JSON list of exactly {n} adjacent job titles a salesperson would "
        f"also want to reach at the same company. Aim for adjacent roles, adjacent "
        f"seniority (one level up, peer, or one level down), and common synonyms.\n\n"
        f"Rules:\n"
        f"- Do NOT repeat the input titles verbatim. Return alternatives.\n"
        f"- Avoid wildly senior roles (CEO, founder) unless the seed pattern is clearly C-suite.\n"
        f"- Avoid wildly junior roles (intern, coordinator) unless the seed pattern is clearly entry-level.\n"
        f"- Prefer titles that exist at real B2B companies — no made-up combos.\n\n"
        f'Output JSON only: {{"titles": ["title 1", "title 2", ...]}}'
    )

    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are a B2B sales prospecting expert. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        content = resp.choices[0].message.content or ""
        data = json.loads(content)
        titles = [str(t).strip() for t in (data.get("titles") or []) if str(t).strip()]
        # De-dup vs seeds (case-insensitive) — model sometimes echoes them.
        seed_lower = {t.lower().strip() for t in existing_titles if t}
        titles = [t for t in titles if t.lower().strip() not in seed_lower]
        return titles[:n], None
    except json.JSONDecodeError as e:
        _log(f"openai returned non-JSON: {e}")
        return [], "openai returned non-JSON"
    except Exception as e:  # noqa: BLE001
        _log(f"openai error: {type(e).__name__}: {e}")
        return [], f"openai error: {type(e).__name__}"


# ---------- Apollo company resolution ------------------------------------

def _strip_li_url(url: str) -> str:
    """Lowercase a LinkedIn URL and strip scheme/www/query/fragment/trailing
    slash so two equivalent URLs compare equal. Apollo's records may store
    `http://www.linkedin.com/company/x` while Draftboard sends
    `https://www.linkedin.com/company/x/` — they should match.
    """
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = u.split("?", 1)[0].split("#", 1)[0]
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


def apollo_find_org(
    company_name: str,
    apollo_key: str,
    company_linkedin_url: str = "",
) -> tuple[dict | None, str | None]:
    """Look up Apollo's canonical record for a company.

    Pattern borrowed from the hiringmanagers.app `/apollo-company` route:
    - Send BOTH `q_organization_name` and (if available) `q_organization_domains`
      to maximize precision.
    - Apollo's API can't pre-filter by LinkedIn URL — its filter params either
      422 or silently return default top results. But Apollo DOES return
      `linkedin_url` on each match, so we post-filter the response client-side.
    - Apollo splits results into `accounts` (higher-quality CRM-backed) and
      `organizations` (scraped). Check accounts first.

    Returns (org_dict, error). On success org_dict has:
        {id, name, primary_domain, website_url, linkedin_url, num_contacts}

    NOTE: When Draftboard's API eventually exposes `position.companyDomain`,
    much of this can collapse to a direct domain pass-through. TODO marked.
    """
    if not apollo_key:
        return None, "apollo key not configured"
    if not company_name and not company_linkedin_url:
        return None, "missing company name and LinkedIn URL"

    body: dict = {"page": 1, "per_page": 5}
    if company_name:
        body["q_organization_name"] = company_name
    # Note: q_organization_domains accepts a single domain string per Apollo's
    # docs (not a list, despite the plural name). We don't have a domain to
    # send from Draftboard data here, but leaving the slot for when we do.
    try:
        r = requests.post(
            APOLLO_COMPANIES_SEARCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"apollo network error (company search): {type(e).__name__}"
    if r.status_code == 401:
        return None, "apollo auth failed (check APOLLO_API_KEY)"
    if r.status_code == 429:
        return None, "apollo rate-limited"
    if r.status_code != 200:
        body_snip = (r.text or "").strip()[:400]
        _log(f"apollo company search HTTP {r.status_code}: {body_snip}")
        return None, f"apollo company search HTTP {r.status_code} — {body_snip or '(no body)'}"
    try:
        data = r.json()
    except ValueError:
        return None, "apollo company search returned non-JSON"

    accounts = data.get("accounts") or []
    organizations = data.get("organizations") or []
    all_companies = accounts + organizations
    if not all_companies:
        return None, f"apollo found no company matching '{company_name}'"

    chosen = None
    # 1. LinkedIn URL post-filter — most precise way to pick the right co
    # when the name is ambiguous (e.g. 'Equals' has multiple matches).
    if company_linkedin_url:
        want = _strip_li_url(company_linkedin_url)
        for c in all_companies:
            apollo_li = _strip_li_url(c.get("linkedin_url") or "")
            if apollo_li and apollo_li == want:
                chosen = c
                break

    # 2. Exact name match in accounts (higher-quality records)
    if not chosen and company_name:
        cn = company_name.lower()
        for c in accounts:
            if (c.get("name") or "").lower() == cn:
                chosen = c
                break

    # 3. Exact name match in organizations
    if not chosen and company_name:
        cn = company_name.lower()
        for c in organizations:
            if (c.get("name") or "").lower() == cn:
                chosen = c
                break

    # 4. Fallback: take the first account if any, else first org
    if not chosen:
        chosen = accounts[0] if accounts else organizations[0]

    # Normalize domain. Apollo may have `primary_domain` populated OR may
    # only have `website_url` — derive from website if domain is missing.
    domain = (chosen.get("primary_domain") or "").strip().lower()
    if not domain:
        website = (chosen.get("website_url") or "").strip().lower()
        if website:
            for prefix in ("https://", "http://", "www."):
                if website.startswith(prefix):
                    website = website[len(prefix):]
            domain = website.split("/", 1)[0].rstrip("/")

    return {
        "id": chosen.get("id"),
        "name": chosen.get("name") or "",
        "primary_domain": domain,
        "website_url": chosen.get("website_url") or "",
        "linkedin_url": chosen.get("linkedin_url") or "",
        "num_contacts": chosen.get("num_contacts") or 0,
    }, None


# ---------- Apollo people search -----------------------------------------

def apollo_search_people_at_account(
    domain: str,
    titles: list[str],
    apollo_key: str,
    per_page: int = APOLLO_PER_PAGE,
    company_name: str = "",
) -> tuple[list[dict], str | None]:
    """Search Apollo's people DB for the given titles at the given domain.

    Critical flag: `include_similar_titles=True` — without it, Apollo does
    exact-string matching on `person_titles[]` and rejects anyone with the
    slightest title variation. With it, "Director of Sales" also matches
    "VP Sales", "Sales Director", "Director, Sales EMEA", etc. That single
    flag is the difference between Stripe returning 5 candidates vs 0+.
    """
    if not apollo_key:
        return [], "apollo key not configured"
    if not titles:
        return [], "no titles to search"
    if not domain:
        return [], "no domain to scope the search"

    body = {
        "q_organization_domains": domain,
        "person_titles": titles,
        "page": 1,
        "per_page": per_page,
        "include_similar_titles": True,
    }
    try:
        r = requests.post(
            APOLLO_PEOPLE_SEARCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        _log(f"apollo people search network error: {type(e).__name__}")
        return [], f"apollo network error: {type(e).__name__}"
    if r.status_code == 401:
        return [], "apollo auth failed (check APOLLO_API_KEY)"
    if r.status_code == 429:
        return [], "apollo rate-limited"
    if r.status_code != 200:
        body_snip = (r.text or "").strip()[:400]
        _log(f"apollo people search HTTP {r.status_code}: {body_snip}")
        return [], f"apollo people search HTTP {r.status_code} — {body_snip or '(no body)'}"
    try:
        data = r.json()
    except ValueError:
        return [], "apollo returned non-JSON"

    people = data.get("people") or data.get("contacts") or []
    out: list[dict] = []
    for p in people:
        first = (p.get("first_name") or "").strip()
        last = (p.get("last_name") or "").strip()
        title = (p.get("title") or "").strip()
        linkedin = (p.get("linkedin_url") or "").strip()
        org = ((p.get("organization") or {}).get("name") or company_name).strip()
        if not linkedin:
            # Without a LinkedIn URL we can't dedupe or hand off to /import,
            # so skip the row entirely.
            continue
        out.append({
            "first_name": first,
            "last_name": last,
            "name": (f"{first} {last}".strip() or "(unknown)"),
            "title": title,
            "linkedin_url": linkedin,
            "organization_name": org,
            "apollo_id": p.get("id"),
            "email_status": p.get("email_status") or "",
        })
    return out, None


# ---------- Dedup --------------------------------------------------------

def dedupe_against_existing(
    candidates: list[dict],
    existing_linkedin_urls: set,
) -> list[dict]:
    """Drop candidates whose normalized LinkedIn URL already exists as a
    target in the workspace. `existing_linkedin_urls` is expected to be
    pre-normalized via the caller's `_normalize_linkedin` helper.
    """
    out = []
    for c in candidates:
        norm = _normalize_linkedin_local(c.get("linkedin_url") or "")
        if norm and norm in existing_linkedin_urls:
            continue
        out.append(c)
    return out


def _normalize_linkedin_local(url: str) -> str:
    """Light local normalization for dedup. Mirrors app.py's
    `_normalize_linkedin` for `linkedin.com/in/foo` style URLs."""
    u = (url or "").strip().lower()
    if not u:
        return ""
    u = u.split("?", 1)[0].split("#", 1)[0]
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("www."):
        u = u[4:]
    return u.rstrip("/")


# ---------- CSE people search (the actual prospector) --------------------

# Country-specific LinkedIn subdomains we accept as profile URLs. We don't
# strip them (linkedin.com/in/x vs il.linkedin.com/in/x are technically
# different profiles for the same person in some cases) — but we recognize
# them as real LinkedIn profile pages and not language/help pages.
_LI_PROFILE_RE = r"linkedin\.com/in/"


def _candidate_at_company(name_text: str, title_text: str, company_name: str) -> bool:
    """Heuristic: does the CSE-parsed (name, title) actually belong to
    someone CURRENTLY at `company_name`?

    LinkedIn page <title>s tend to follow `"First Last - Job Title - Company"`,
    but CSE truncation often collapses to two parts. We use the parsed title
    string to detect "wrong company" signals — `" at OtherCo"`, `" @ OtherCo"`,
    `" | OtherCo"` followed by a name other than ours.

    Returns True (keep) by default. Returns False (drop) only when we have
    strong evidence the row is at a different company.
    """
    if not company_name:
        return True
    cn = company_name.strip().lower()
    if not cn:
        return True
    tt = (title_text or "").lower()
    nt = (name_text or "").lower()
    if cn in tt or cn in nt:
        # The company name appears somewhere in the page title — assume match.
        return True
    # Look for "<role> at <other company>" / "<role> @ <other company>" /
    # "<role> | <other company>" patterns. If the substring AFTER the
    # marker doesn't contain our company, the row is at someone else.
    for marker in (" at ", " @ ", " | "):
        if marker in tt:
            tail = tt.split(marker, 1)[1].strip()
            if tail and cn not in tail:
                return False
    return True


def cse_search_people_at_company(
    company_name: str,
    titles: list[str],
    cse_key: str,
    cse_id: str,
    domain: str = "",
    results_per_title: int = CSE_RESULTS_PER_TITLE,
) -> tuple[list[dict], str | None]:
    """For each title, query Google CSE for LinkedIn profiles at this company.

    Query shape: `site:linkedin.com/in/ "{company}" "{title}"`. Each result
    is already a LinkedIn profile URL — no enrichment step needed.

    We try the company name first, then fall back to the company domain
    (with `intext:`) if the name query returns nothing. Domain-bound
    queries tend to find current employees more reliably for companies
    whose name is a generic word.

    Returns (candidates, error). Each candidate dict has:
        first_name, last_name, name, title, linkedin_url,
        organization_name, source ('cse')
    """
    import re
    if not cse_key or not cse_id:
        return [], "google CSE not configured (need GOOGLE_CSE_API_KEY + GOOGLE_CSE_ID)"
    if not titles:
        return [], "no titles to search"
    if not company_name and not domain:
        return [], "no company name or domain"

    seen_urls: set = set()
    out: list[dict] = []
    profile_re = re.compile(_LI_PROFILE_RE, re.IGNORECASE)

    # Build the per-query company clause. We prefer a quoted name; if no
    # name (rare), fall back to a domain-anchored phrase.
    if company_name:
        company_clause = f'"{company_name}"'
    else:
        company_clause = f'"{domain}"'

    for title in titles:
        if not title:
            continue
        title_clean = title.strip()
        if not title_clean:
            continue
        query = f'site:linkedin.com/in/ {company_clause} "{title_clean}"'
        try:
            r = requests.get(
                CSE_URL,
                params={"key": cse_key, "cx": cse_id, "q": query, "num": results_per_title},
                timeout=15,
            )
        except requests.RequestException as e:
            _log(f"cse network error: {type(e).__name__}")
            # Hard fail — if CSE is down/misconfigured, surface immediately
            # rather than silently returning 0 candidates.
            return [], f"cse network error: {type(e).__name__}"
        if r.status_code == 429:
            _log("cse rate-limited / quota exceeded")
            return out, "cse rate-limited (free tier is 100 queries/day)"
        if r.status_code != 200:
            _log(f"cse HTTP {r.status_code}: {r.text[:300]}")
            return out, f"cse HTTP {r.status_code}"
        try:
            data = r.json() or {}
        except ValueError:
            return out, "cse returned non-JSON"

        for item in data.get("items") or []:
            link = (item.get("link") or "").strip()
            if not link or not profile_re.search(link):
                continue
            norm = _normalize_linkedin_local(link)
            if norm in seen_urls:
                continue
            seen_urls.add(norm)

            # Parse "Name - Title - Company | LinkedIn" from the CSE title.
            # CSE's `title` field is the LinkedIn page <title>, which is
            # remarkably consistent: "FirstName LastName - JobTitle - CompanyName".
            raw_title = (item.get("title") or "").strip()
            # Some pages append " | LinkedIn" or " | <country>" suffix.
            for suffix in (" | LinkedIn", " - LinkedIn"):
                if raw_title.endswith(suffix):
                    raw_title = raw_title[: -len(suffix)].rstrip()
            parts = [p.strip() for p in raw_title.split(" - ")]
            full_name = parts[0] if parts else ""
            found_title = parts[1] if len(parts) >= 2 else ""
            found_org = parts[2] if len(parts) >= 3 else (company_name or "")

            # Drop candidates that the CSE-parsed title strongly suggests
            # are at a DIFFERENT company. CSE search is loose; many
            # results are "people who mention {company} in their profile"
            # rather than "current employees of {company}".
            if not _candidate_at_company(full_name, raw_title, company_name):
                continue

            first, _, last = full_name.partition(" ")
            out.append({
                "first_name": first.strip(),
                "last_name": last.strip(),
                "name": full_name or "(unknown)",
                "title": found_title,
                "linkedin_url": link,
                "organization_name": found_org or (company_name or ""),
                "source": "cse",
                "matched_title_query": title_clean,
                "snippet": (item.get("snippet") or "").strip(),
            })

    return out, None


def search_people_at_account(
    company_name: str,
    titles: list[str],
    domain: str,
    keys: dict,
    apollo_first: bool = False,
) -> tuple[list[dict], str | None]:
    """High-level prospector: CSE-first (default), Apollo-fallback. Returns
    `(candidates, error)`.

    `keys` is a dict with `google_cse_api_key`, `google_cse_id`,
    `apollo_api_key` (any subset). We prefer CSE because most users on
    Apollo's free/starter tier get `linkedin_url: None` back from Apollo's
    people search — making those rows useless for /import handoff.
    """
    cse_key = (keys.get("google_cse_api_key") or "").strip()
    cse_id = (keys.get("google_cse_id") or "").strip()
    apollo_key = (keys.get("apollo_api_key") or "").strip()

    use_cse = bool(cse_key and cse_id)
    use_apollo = bool(apollo_key)

    if apollo_first and use_apollo:
        cands, err = apollo_search_people_at_account(
            domain or "", titles, apollo_key, company_name=company_name,
        )
        # Apollo candidates with linkedin_url=None are useless — filter.
        cands = [c for c in cands if c.get("linkedin_url")]
        if cands:
            return cands, None
        # Apollo returned nothing usable; fall through to CSE.

    if use_cse:
        return cse_search_people_at_company(
            company_name, titles, cse_key, cse_id, domain=domain,
        )

    if use_apollo:
        cands, err = apollo_search_people_at_account(
            domain or "", titles, apollo_key, company_name=company_name,
        )
        cands = [c for c in cands if c.get("linkedin_url")]
        return cands, err

    return [], "no search engine configured (need Google CSE keys OR Apollo key)"
