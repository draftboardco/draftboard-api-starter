"""Auto-prospecting prototype: find similar people at an account.

Given a set of existing targets at Account X with various titles
(e.g., 'Director of Marketing', 'VP of Marketing'), this module:

  1. Asks gpt-4o-mini to expand those into ~12 adjacent titles
     (peer / one level up / one level down / synonyms).
  2. Hits Apollo's mixed_people/search endpoint filtered by that
     company name + those titles to surface real candidates.

Pitch: drip a steady stream of "people you might also want to reach"
to users so they don't have to do prospecting themselves. Common ask
since launch.

Reuses the same Apollo + OpenAI API keys the LinkedIn resolver uses
(see `linkedin_resolver.py` + `_load_resolver_keys` in app.py).
"""

import json
import sys

import requests

# Apollo deprecated /mixed_people/search for API callers — it now returns 422
# with a "use the new mixed_people/api_search endpoint" message. The /api_search
# variant is the documented replacement.
# Ref: https://docs.apollo.io/reference/people-api-search
APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_people/api_search"
APOLLO_ORG_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"
OPENAI_MODEL = "gpt-4o-mini"

# How many adjacent titles to ask gpt-4o-mini for. Bigger = wider net but more
# Apollo cost. Apollo charges per call AND per result, so we want focused.
DEFAULT_TITLE_COUNT = 12

# Apollo page size — we only need ~10 candidates per call. The endpoint
# returns up to 100 but we keep it tight to limit cost during the prototype.
APOLLO_PER_PAGE = 10


def _log(msg: str) -> None:
    print(f"[prospecting] {msg}", file=sys.stderr, flush=True)


def generate_adjacent_titles(
    existing_titles: list[str],
    company: str,
    openai_key: str,
    n: int = DEFAULT_TITLE_COUNT,
) -> tuple[list[str], str | None]:
    """Ask gpt-4o-mini to expand a list of seed titles to N adjacent titles.

    Returns (titles, error). On success, error is None and titles is a
    list of strings. On failure, returns ([], error_message).

    The model is instructed to avoid echoing the seeds verbatim and to
    stay within a similar seniority band — no CEOs unless that's the
    seed pattern, no interns either.
    """
    if not openai_key:
        return [], "openai key not configured"
    if not existing_titles:
        return [], "no existing titles to expand from"
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
        f'Output JSON only, no commentary: {{"titles": ["title 1", "title 2", ...]}}'
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
        # De-dup vs the seeds (case-insensitive) — model sometimes echoes.
        seed_lower = {t.lower().strip() for t in existing_titles if t}
        titles = [t for t in titles if t.lower().strip() not in seed_lower]
        return titles[:n], None
    except json.JSONDecodeError as e:
        _log(f"openai returned non-JSON: {e}")
        return [], "openai returned non-JSON"
    except Exception as e:  # noqa: BLE001
        _log(f"openai error: {type(e).__name__}: {e}")
        return [], f"openai error: {type(e).__name__}"


def _apollo_find_org_domain(company_name: str, apollo_key: str) -> tuple[str | None, str | None]:
    """Look up Apollo's known primary domain for a given company name.

    We filter the people search by `q_organization_domains_list` (the
    Apollo-documented preferred way to scope a search to a company),
    NOT by `organization_ids`. Apollo's internal IDs returned from
    mixed_companies/search aren't always the "master organization IDs"
    the people-search endpoint accepts — passing them gets HTTP 422.

    Returns (domain, error).
    """
    if not apollo_key or not company_name:
        return None, "missing key or name"
    body = {
        "q_organization_name": company_name,
        "page": 1,
        "per_page": 1,
    }
    try:
        r = requests.post(
            APOLLO_ORG_SEARCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        return None, f"apollo network error (org search): {type(e).__name__}"
    if r.status_code == 401:
        return None, "apollo auth failed (check APOLLO_API_KEY)"
    if r.status_code == 429:
        return None, "apollo rate-limited"
    if r.status_code != 200:
        body_snip = (r.text or "").strip()[:300]
        _log(f"apollo org search HTTP {r.status_code}: {body_snip}")
        return None, f"apollo org search HTTP {r.status_code} — {body_snip or '(no body)'}"
    try:
        data = r.json()
    except ValueError:
        return None, "apollo org search returned non-JSON"

    orgs = data.get("organizations") or data.get("accounts") or []
    if not orgs:
        return None, f"apollo found no organization matching '{company_name}'"
    org = orgs[0]
    domain = (
        (org.get("primary_domain") or "").strip().lower()
        or (org.get("website_url") or "").strip().lower().replace("https://", "").replace("http://", "").rstrip("/")
        or ""
    )
    if not domain:
        return None, f"apollo matched '{org.get('name')}' but the row has no domain — try a more specific account name"
    return domain, None


def apollo_search_people_at_account(
    company_name: str,
    titles: list[str],
    apollo_key: str,
    per_page: int = APOLLO_PER_PAGE,
) -> tuple[list[dict], str | None]:
    """Find Apollo's organization_id for the given company, then search
    its people filtered by the provided titles.

    Returns (candidates, error). Each candidate dict has:
        first_name, last_name, name, title, linkedin_url,
        organization_name, apollo_id, email_status
    """
    if not apollo_key:
        return [], "apollo key not configured"
    if not titles:
        return [], "no titles to search"
    if not company_name:
        return [], "no company name"

    domain, org_err = _apollo_find_org_domain(company_name, apollo_key)
    if org_err:
        return [], org_err

    body = {
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "page": 1,
        "per_page": per_page,
    }
    try:
        r = requests.post(
            APOLLO_SEARCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        _log(f"apollo network error: {type(e).__name__}")
        return [], f"apollo network error: {type(e).__name__}"
    if r.status_code == 401:
        return [], "apollo auth failed (check APOLLO_API_KEY)"
    if r.status_code == 429:
        return [], "apollo rate-limited"
    if r.status_code != 200:
        # Surface Apollo's actual response body in the user-facing error
        # (and the server log) so future 4xx/5xx don't need a terminal dive.
        body_snip = (r.text or "").strip()[:400]
        _log(f"apollo people search HTTP {r.status_code}: {body_snip}")
        return [], f"apollo people search HTTP {r.status_code} — {body_snip or '(no body)'}"
    try:
        data = r.json()
    except ValueError:
        return [], "apollo returned non-JSON"

    # `mixed_people/search` returns people in `people` (and sometimes
    # `contacts` for already-saved contacts). Take whichever has rows.
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
    """Light local normalization for dedup. Lowercases, strips scheme +
    www + trailing slash. Mirrors what app.py's _normalize_linkedin does
    for `/in/foo` style URLs.
    """
    u = (url or "").strip().lower()
    if not u:
        return ""
    for prefix in ("https://", "http://"):
        if u.startswith(prefix):
            u = u[len(prefix):]
            break
    if u.startswith("www."):
        u = u[4:]
    if u.endswith("/"):
        u = u[:-1]
    return u
