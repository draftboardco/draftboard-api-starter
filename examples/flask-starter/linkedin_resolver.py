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
import sys
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
    "gmx.com", "mail.com", "yandex.com", "qq.com", "pm.me", "fastmail.com",
    "fastmail.fm", "zoho.com", "tutanota.com",
}

# Anchored to require an actual LinkedIn host. Without anchoring, an attacker
# page indexed by Google with a URL like `evil.com/?ref=linkedin.com/posts/x_y`
# would be turned into a fake `/in/x` profile URL.
_POST_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?linkedin\.com/posts/([a-zA-Z0-9-]+?)_",
    re.IGNORECASE,
)
_PROFILE_URL_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?linkedin\.com/in/[a-zA-Z0-9._%-]+",
    re.IGNORECASE,
)

# LinkedIn cookie-consent boilerplate Google sometimes returns instead of real
# post snippet content. When we see this on a duplicate result, replace it with
# the real content from another result if available. 12-char floor — real
# terse snippets ("CEO at Acme.") shouldn't be flagged.
_BOILERPLATE_MARKERS = ("Agree & Join", "Agree &amp; Join", "User Agreement", "By clicking Continue")
_BOILERPLATE_MIN_LEN = 12


def _log(msg: str) -> None:
    """Tagged stderr log. Used for error types that we don't want echoed back
    to the API client (proxies sometimes embed credentials in their error
    repr)."""
    print(f"[linkedin-resolver] {msg}", file=sys.stderr, flush=True)


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

    Both forms require the URL to actually be on a linkedin.com host — an
    unanchored substring match could be tricked by `evil.com/?x=linkedin.com/in/...`.
    """
    if not url:
        return None
    if _PROFILE_URL_RE.match(url):
        return url
    m = _POST_URL_RE.match(url)
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


def _try_apollo(name: str, email: str, apollo_key: str) -> tuple[dict | None, str | None]:
    """POST to Apollo /people/match. Returns (result, transient_error) where:
      - (result_dict, None)  on hit  → caller returns this
      - (None, None)         on definitive miss → caller falls through and
                                                  caches "tried, none found"
      - (None, "...reason")  on transient error (network, 5xx, 429, auth) →
                             caller falls through but DOES NOT cache the
                             negative result, since adding/fixing a key later
                             should retry.
    """
    first, last = _split_name(name)
    body = {"email": email, "first_name": first, "last_name": last}
    try:
        r = requests.post(
            APOLLO_MATCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=15,
        )
    except requests.RequestException as e:
        _log(f"apollo network error: {type(e).__name__}")
        return None, "apollo network error"
    if r.status_code in (401, 403):
        _log(f"apollo auth failed: HTTP {r.status_code}")
        return None, "apollo auth failed"
    if r.status_code == 429:
        return None, "apollo rate-limited"
    if r.status_code >= 500:
        return None, f"apollo {r.status_code}"
    if r.status_code == 404:
        # Apollo's documented "we don't have this person" response — safe to
        # cache as a definitive miss.
        return None, None
    if r.status_code != 200:
        # 400/422/etc — could be malformed input on our side, schema drift on
        # Apollo's side, or a transient validation hiccup. Don't cache it as
        # a definitive miss — let the next call retry.
        _log(f"apollo HTTP {r.status_code}")
        return None, f"apollo {r.status_code}"
    try:
        data = r.json() or {}
    except ValueError:
        _log("apollo returned non-JSON")
        return None, "apollo returned non-JSON"
    person = data.get("person") or {}
    li_raw = person.get("linkedin_url")
    if not isinstance(li_raw, str):
        return None, None
    li = li_raw.strip()
    if not _PROFILE_URL_RE.match(li):
        # Apollo returned something but it's not a /in/ profile URL — treat
        # as a miss (definitive).
        return None, None
    full_raw = person.get("name") or name
    full = full_raw.strip() if isinstance(full_raw, str) else name
    return {
        "linkedin_url": li,
        "full_name": full,
        "confidence": "high",
        "source": "apollo",
        "reasoning": "Apollo /people/match returned the LinkedIn URL directly.",
        "query": email,
        "error": None,
    }, None


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
    (never None) — `linkedin_url` may be None when nothing matched.

    Result includes a private `_transient` flag (popped before being returned
    from `resolve_linkedin`) telling the caller whether the failure was a
    transient API error (don't cache the miss) vs a definitive "tried, didn't
    find them" (safe to cache)."""
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
        "_transient": False,
    }

    try:
        r = requests.get(
            CSE_URL,
            params={"key": cse_key, "cx": cse_id, "q": query, "num": 10},
            timeout=15,
        )
    except requests.RequestException as e:
        _log(f"cse network error: {type(e).__name__}")
        return {**base, "error": "CSE request failed", "reasoning": "CSE request failed.", "_transient": True}
    if r.status_code != 200:
        # 4xx auth/quota/rate-limit and 5xx are all transient from the
        # customer's perspective — they could fix the key or wait and retry.
        _log(f"cse returned HTTP {r.status_code}")
        return {**base, "error": f"CSE returned {r.status_code}", "reasoning": "CSE call failed.", "_transient": True}
    try:
        data = r.json() or {}
    except ValueError:
        _log("cse returned non-JSON")
        return {**base, "error": "CSE returned non-JSON", "reasoning": "CSE returned non-JSON.", "_transient": True}

    candidates = _extract_profile_candidates(data.get("items") or [])
    if not candidates:
        # Definitive: CSE worked, just had no LinkedIn-shaped results for this query.
        return {**base, "reasoning": "No LinkedIn profiles in CSE results."}

    # Lazy-import OpenAI so the module is importable without the package
    # installed (e.g., for the no-keys / Apollo-only paths in tests).
    try:
        from openai import OpenAI
    except ImportError:
        return {**base, "error": "openai package not installed", "reasoning": "openai package missing.", "_transient": True}

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
        # Don't include str(e) — proxy errors can include URLs with query
        # params that may contain the API key.
        _log(f"openai error: {type(e).__name__}")
        return {**base, "error": "OpenAI call failed", "reasoning": "OpenAI ranking failed.", "_transient": True}

    idx = analysis.get("bestMatchIndex")
    confidence = analysis.get("confidence") or "none"
    reasoning_raw = analysis.get("reasoning") or ""
    reasoning = reasoning_raw if isinstance(reasoning_raw, str) else ""

    if not isinstance(idx, int) or idx < 0 or idx >= len(candidates):
        # Definitive: LLM saw the candidates and rejected all of them.
        return {**base, "confidence": "none", "reasoning": reasoning or "LLM did not pick a match."}

    # CRITICAL: never trust the LLM's `linkedinUrl` — index back into the
    # candidate set. The CSE titles/snippets are attacker-controllable (anyone
    # can put text on a webpage Google indexes) and a prompt-injected snippet
    # could otherwise make us return `linkedin.com/in/attacker` with high
    # confidence.
    chosen = candidates[idx]
    li = chosen["link"]
    if not _PROFILE_URL_RE.match(li):
        # Shouldn't happen — _extract_profile_candidates only emits /in/ URLs —
        # but defense in depth.
        return {**base, "confidence": "none", "reasoning": "Chosen candidate URL was not a profile URL."}

    full_raw = analysis.get("fullName")
    full = full_raw.strip() if isinstance(full_raw, str) else name

    return {
        "linkedin_url": li,
        "full_name": full or name,
        "confidence": confidence if confidence in ("high", "medium", "low", "none") else "low",
        "source": "cse",
        "reasoning": reasoning,
        "query": query,
        "error": None,
        "_transient": False,
    }


def is_cacheable(result: dict) -> bool:
    """Should this resolver result be persisted to the SQLite cache?

    Cache only definitive outcomes:
      - apollo hit
      - cse hit (LLM picked a candidate)
      - definitive miss (Apollo + CSE were tried and reported no match)

    Don't cache:
      - "no keys configured" (customer hasn't pasted keys yet — caching this
        would lock every contact to a 30-day no-match even after they fix the
        config)
      - transient API failures (rate limit, network, auth — fixable by the
        customer; should retry on next call, not be cached)
    """
    if result.get("_transient"):
        return False
    if result.get("error") == "no keys configured":
        return False
    if result.get("error") == "missing input":
        return False
    return True


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

    Callers that want to cache should check `is_cacheable(result)` first —
    a result with a transient API error (rate limit, network) returns False
    so the cache doesn't get poisoned with a stale "no match".
    """
    if not (name or "").strip() or not (email or "").strip():
        return {
            "linkedin_url": None, "full_name": None, "confidence": "none",
            "source": "none", "reasoning": "Name and email are required.",
            "query": "", "error": "missing input",
        }

    apollo_transient: str | None = None
    cse_transient = False

    # 1. Apollo first (when configured).
    if apollo_key:
        hit, apollo_transient = _try_apollo(name, email, apollo_key)
        if hit:
            return hit

    # 2. CSE + OpenAI fallback (when both fully configured).
    if cse_key and cse_id and openai_key:
        cse_result = _try_cse(name, email, cse_key, cse_id, openai_key)
        cse_transient = bool(cse_result.pop("_transient", False))
        # If Apollo had a transient error too, surface that detail in the
        # result so the customer knows both sides hiccupped.
        if cse_transient and apollo_transient:
            cse_result["reasoning"] = f"{cse_result['reasoning']} (apollo also failed: {apollo_transient})"
        # Re-flag the result so the caller sees the transient bit.
        if cse_transient:
            cse_result["_transient"] = True
        return cse_result

    # 3. Nothing worked. Build a "tried but missed" or "no keys" response.
    keys_configured = bool(apollo_key or (cse_key and cse_id and openai_key))
    if not keys_configured:
        return {
            "linkedin_url": None, "full_name": None, "confidence": "none",
            "source": "none", "reasoning": "No keys configured.",
            "query": "", "error": "no keys configured",
        }
    # Apollo was the only configured method, and it didn't find them.
    return {
        "linkedin_url": None,
        "full_name": None,
        "confidence": "none",
        "source": "none",
        "reasoning": (
            f"Apollo tried and could not find this person ({apollo_transient})"
            if apollo_transient
            else "Apollo tried and could not find this person."
        ),
        "query": email,
        "error": None,
        "_transient": bool(apollo_transient),
    }
