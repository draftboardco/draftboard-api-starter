"""
LinkedIn profile resolver — finds a person's LinkedIn URL given (name, email).

Two methods, tried in order:

  1. Apollo /people/match  — fast, single API call, returns the LinkedIn URL
                             directly when Apollo knows the person.
  2. Google CSE + gpt-4o-mini — searches the web for "{first_name} {company}
                                linkedin", recovers /in/ profile URLs (also
                                from /posts/{slug}_... URLs), dedupes with
                                snippet merging, and asks gpt-4o-mini to pick
                                the right candidate.

All three keys (Apollo, Google CSE, OpenAI) are optional. The resolver tries
whichever is configured. With nothing configured it returns a "no keys" result
without raising.

Reference:
  - TS port (older version): ~/Desktop/Projects/experiments/draftboard-lead-magnets/
      apps/company-prospector/src/app/api/enrich-people-google/route.ts
  - Improvement spec: ~/Downloads/linkedin-post-slug-extraction (1).html —
      post-URL slug recovery, snippet merging, and the rewritten LLM prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any

import requests

APOLLO_MATCH_URL = "https://api.apollo.io/api/v1/people/match"
CSE_URL = "https://www.googleapis.com/customsearch/v1"
OPENAI_MODEL = "gpt-4o-mini"

# Generic mailbox providers — when the email domain is one of these, we can't
# infer a company from it, so we drop the company term from the CSE query.
FREE_MAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "me.com", "proton.me", "protonmail.com", "aol.com", "msn.com", "live.com",
}

# Recovers a profile slug from a LinkedIn post URL.
# /posts/{slug}_{post-content}-activity-{id}-{hash}
_POST_SLUG_RE = re.compile(r"linkedin\.com/posts/([a-zA-Z0-9-]+?)_")

# LinkedIn cookie-consent boilerplate Google sometimes returns instead of real
# post snippet content. When we see this on a duplicate result, replace it with
# the real content from another result if available.
_BOILERPLATE_MARKERS = ("Agree & Join", "Agree &amp; Join", "User Agreement", "By clicking Continue")
_BOILERPLATE_MIN_LEN = 20


def _split_name(name: str) -> tuple[str, str]:
    """'Bogdan Cojanu' -> ('Bogdan', 'Cojanu'). Last name may be empty."""
    parts = (name or "").strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _company_from_email(email: str) -> str:
    """'bogdan@nestor.com' -> 'nestor.com'. Returns '' if domain is a free
    mailbox provider — caller should drop company from the CSE query in that
    case so we search by name only."""
    if not email or "@" not in email:
        return ""
    domain = email.split("@", 1)[1].strip().lower()
    domain = domain.removeprefix("www.")
    if domain in FREE_MAIL_DOMAINS:
        return ""
    return domain


def _is_boilerplate(snippet: str) -> bool:
    if not snippet or len(snippet) < _BOILERPLATE_MIN_LEN:
        return True
    return any(marker in snippet for marker in _BOILERPLATE_MARKERS)


def _extract_profile_url(url: str) -> str | None:
    """Recover a /in/{slug} URL from a LinkedIn URL of any shape.

    Direct profile URL -> pass through.
    Post URL -> rebuild from the author slug captured before the underscore.
    Anything else (company pages, articles, schools, jobs) -> None.
    """
    if not url:
        return None
    if "linkedin.com/in/" in url:
        return url
    m = _POST_SLUG_RE.search(url)
    if m:
        return f"https://www.linkedin.com/in/{m.group(1)}"
    return None


def _extract_profile_candidates(items: list[dict]) -> list[dict]:
    """Filter + dedupe Google CSE results into LinkedIn profile candidates.

    Multiple posts by the same person collapse to one candidate. When the first
    occurrence has a boilerplate snippet ("Agree & Join LinkedIn...") and a
    later one has real content, swap in the real content — otherwise the LLM
    can't see the company signal in the snippet and false-negatives a real
    match.

    Returns up to 5 candidates with shape {title, link, snippet, thumbnail}.
    """
    by_url: dict[str, dict] = {}
    for item in items or []:
        link = item.get("link") or ""
        profile_url = _extract_profile_url(link)
        if not profile_url:
            continue
        snippet = item.get("snippet") or ""
        thumbnail = (
            (item.get("pagemap") or {}).get("cse_thumbnail") or [{}]
        )[0].get("src", "") if item.get("pagemap") else ""

        existing = by_url.get(profile_url)
        if existing is None:
            by_url[profile_url] = {
                "title": item.get("title") or "",
                "link": profile_url,
                "snippet": snippet,
                "thumbnail": thumbnail,
            }
            continue

        # Dedupe: try to enrich the existing candidate with this dupe's data.
        new_is_boilerplate = _is_boilerplate(snippet)
        existing_is_boilerplate = _is_boilerplate(existing["snippet"])

        if not new_is_boilerplate and existing_is_boilerplate:
            existing["snippet"] = snippet
        elif not new_is_boilerplate and not existing_is_boilerplate:
            # Both real — merge if the new one adds anything.
            if snippet[:40] not in existing["snippet"]:
                existing["snippet"] = f"{existing['snippet']} | {snippet}"

        if not existing.get("thumbnail") and thumbnail:
            existing["thumbnail"] = thumbnail

    return list(by_url.values())[:5]


def _try_apollo(name: str, email: str, apollo_key: str) -> dict | None:
    """POST to Apollo /people/match. Returns a result dict on hit, None on miss
    or error (caller falls through to CSE)."""
    first, last = _split_name(name)
    body = {"email": email, "first_name": first, "last_name": last}
    try:
        r = requests.post(
            APOLLO_MATCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json() or {}
    except ValueError:
        return None
    person = data.get("person") or {}
    li = (person.get("linkedin_url") or "").strip()
    if not li:
        return None
    full = (person.get("name") or name).strip()
    return {
        "linkedin_url": li,
        "full_name": full,
        "confidence": "high",
        "source": "apollo",
        "reasoning": "Apollo /people/match returned the LinkedIn URL directly.",
        "query": email,
        "error": None,
    }


def _build_cse_prompt(name: str, email: str, company: str, candidates: list[dict]) -> str:
    """Construct the user prompt for gpt-4o-mini. Reflects the rewritten rules:
    name primary, company confirming-only, trust Google's ranking, treat
    boilerplate snippets as missing data."""
    indexed = [{"index": i, **c} for i, c in enumerate(candidates)]
    company_line = f"Likely company (from email domain): {company}" if company else "Likely company: unknown (free-mail address)"
    return f"""You are an expert at analyzing LinkedIn search results.

Searching for: {name}
Email: {email}
{company_line}

Google search results (already ranked by relevance to a query that included
the name and company):
{json.dumps(indexed, indent=2)}

MATCHING RULES (priority order):
1. NAME is the primary criterion. The person's first AND last name must
   appear in the result's title, snippet, OR LinkedIn URL slug. The slug
   (e.g. /in/priya-gill-56774020) is a strong name signal — use it.
2. COMPANY is a confirming signal, not a hard requirement. If a result
   mentions the target company, that's strong confirmation. If the snippet
   is LinkedIn boilerplate ("Agree & Join...", "User Agreement...") or
   sparse, treat that as missing data, not evidence against — trust
   Google's ranking.
3. RANKING matters. The first result is most likely correct. When multiple
   results match the name, prefer the one that mentions the company. If
   none mention the company, prefer the highest-ranked.
4. Only return a null bestMatchIndex when the name clearly doesn't match
   any result, or when results clearly belong to a different person.

Respond with JSON ONLY, in this exact shape:
{{
  "bestMatchIndex": <integer or null>,
  "fullName":       "<full name extracted from the title, not constructed from parts>",
  "linkedinUrl":    "<the link from the chosen result>",
  "confidence":     "high" | "medium" | "low" | "none",
  "reasoning":      "<one sentence>"
}}"""


def _try_cse(
    name: str,
    email: str,
    cse_key: str,
    cse_id: str,
    openai_key: str,
) -> dict:
    """Run the Google-CSE-then-LLM-rank path. Always returns a result dict
    (never None) — `linkedin_url` may be None when nothing matched."""
    first, _last = _split_name(name)
    company = _company_from_email(email)
    query_parts = [first, company, "linkedin"]
    query = " ".join(p for p in query_parts if p)

    base = {
        "linkedin_url": None,
        "full_name": None,
        "confidence": "none",
        "source": "cse",
        "reasoning": "",
        "query": query,
        "error": None,
    }

    try:
        r = requests.get(
            CSE_URL,
            params={"key": cse_key, "cx": cse_id, "q": query, "num": 10},
            timeout=15,
        )
    except requests.RequestException as e:
        return {**base, "error": f"CSE request failed: {e}", "reasoning": "CSE request failed."}
    if r.status_code != 200:
        return {**base, "error": f"CSE returned {r.status_code}", "reasoning": "CSE call failed."}
    try:
        data = r.json() or {}
    except ValueError:
        return {**base, "error": "CSE returned non-JSON", "reasoning": "CSE returned non-JSON."}

    candidates = _extract_profile_candidates(data.get("items") or [])
    if not candidates:
        return {**base, "reasoning": "No LinkedIn profiles in CSE results."}

    # Lazy-import OpenAI so the module is importable without the package
    # installed (e.g., for the no-keys / Apollo-only paths in tests).
    try:
        from openai import OpenAI
    except ImportError:
        return {**base, "error": "openai package not installed", "reasoning": "openai package missing."}

    prompt = _build_cse_prompt(name, email, company, candidates)
    try:
        client = OpenAI(api_key=openai_key)
        completion = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": "You are an expert at analyzing LinkedIn search results. Always respond with valid JSON only."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = completion.choices[0].message.content or "{}"
        analysis = json.loads(content)
    except Exception as e:  # network, auth, JSON, etc.
        return {**base, "error": f"OpenAI call failed: {e}", "reasoning": "OpenAI ranking failed."}

    idx = analysis.get("bestMatchIndex")
    li = analysis.get("linkedinUrl") or ""
    full = analysis.get("fullName") or ""
    confidence = analysis.get("confidence") or "none"
    reasoning = analysis.get("reasoning") or ""

    if idx is None or not li or not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        return {**base, "confidence": "none", "reasoning": reasoning or "LLM did not pick a match."}

    return {
        "linkedin_url": li,
        "full_name": full or name,
        "confidence": confidence if confidence in ("high", "medium", "low", "none") else "low",
        "source": "cse",
        "reasoning": reasoning,
        "query": query,
        "error": None,
    }


def resolve_linkedin(
    name: str,
    email: str,
    *,
    apollo_key: str | None = None,
    cse_key: str | None = None,
    cse_id: str | None = None,
    openai_key: str | None = None,
) -> dict[str, Any]:
    """Resolve a LinkedIn URL for (name, email). Never raises.

    Returns:
      {
        "linkedin_url":  str | None,
        "full_name":     str | None,
        "confidence":    "high" | "medium" | "low" | "none",
        "source":        "apollo" | "cse" | "none",
        "reasoning":     str,
        "query":         str,
        "error":         str | None,
      }

    Source semantics:
      - "apollo" — Apollo gave us the URL directly (high confidence).
      - "cse"    — Google CSE + LLM ranking found it (confidence from LLM).
      - "none"   — couldn't resolve. Caller should ask the user to paste a
                   LinkedIn URL manually.
    """
    if not (name or "").strip() or not (email or "").strip():
        return {
            "linkedin_url": None, "full_name": None, "confidence": "none",
            "source": "none", "reasoning": "Name and email are required.",
            "query": "", "error": "missing input",
        }

    # 1. Apollo first (when configured).
    if apollo_key:
        hit = _try_apollo(name, email, apollo_key)
        if hit:
            return hit

    # 2. CSE + OpenAI fallback (when both fully configured).
    if cse_key and cse_id and openai_key:
        return _try_cse(name, email, cse_key, cse_id, openai_key)

    # 3. Nothing worked.
    keys_configured = bool(apollo_key or (cse_key and cse_id and openai_key))
    return {
        "linkedin_url": None,
        "full_name": None,
        "confidence": "none",
        "source": "none",
        "reasoning": "No match found." if keys_configured else "No keys configured.",
        "query": "",
        "error": None if keys_configured else "no keys configured",
    }
