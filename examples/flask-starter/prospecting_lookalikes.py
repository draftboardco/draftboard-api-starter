"""
ICP-driven account lookalike engine.

Inputs:
  - Free-text ICP description (industries, sizes, signals, geographies)
  - List of example customers (company names, domains, or LinkedIn URLs)

Pipeline:
  1. LLM extracts a structured search criteria + ranking rubric from
     the ICP description + customer examples.
  2. Apollo's mixed_companies/search returns candidate companies that
     match the structured criteria.
  3. LLM scores each candidate against the rubric (1-10) with a brief
     rationale, so the user sees WHY a company was surfaced.

Why this shape (LLM → Apollo → LLM):
  - Apollo provides grounded, structured company data. No hallucinations.
  - LLM extraction lets the user describe ICP in plain English ("mid-
    market fintech with compliance teams in NA/EU") instead of clicking
    through Apollo's filter UI.
  - LLM scoring gives a relevance signal richer than "matches all 6
    filters." Apollo's filters are coarse; the rubric step lets us
    reward companies that match the *spirit* of the ICP (e.g. example
    customer adjacency).

What's NOT here (deferred to v2+):
  - Vector-embedding similarity vs. the example customers (would need
    a pre-indexed company embeddings DB).
  - Iterative refinement loop (LLM proposes filters, sees results,
    proposes tighter filters).
  - Direct write-back to Draftboard as targets (these are accounts, not
    people; user has to pick people within each account separately).
"""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

OPENAI_MODEL_FAST = "gpt-4o-mini"
APOLLO_COMPANIES_SEARCH_URL = "https://api.apollo.io/api/v1/mixed_companies/search"

# Apollo employee-count ranges accept a string like "11,20" representing
# min,max. The full set Apollo supports:
APOLLO_HEADCOUNT_BANDS = [
    "1,10", "11,20", "21,50", "51,100", "101,200",
    "201,500", "501,1000", "1001,2000", "2001,5000",
    "5001,10000", "10001+",
]


def _normalize_customer_examples(raw: str) -> list[str]:
    """Split a free-text list of customer examples into trimmed, non-empty
    strings. Accepts comma- or newline-separated input."""
    if not raw:
        return []
    pieces: list[str] = []
    for line in raw.replace(",", "\n").split("\n"):
        s = line.strip()
        if s:
            pieces.append(s)
    # De-dupe case-insensitively while preserving order.
    seen: set = set()
    out: list[str] = []
    for s in pieces:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out[:50]  # cap so the LLM prompt stays bounded


def extract_search_criteria(
    icp_description: str,
    customer_examples: list[str],
    openai_key: str,
) -> tuple[dict, str | None]:
    """Ask gpt-4o-mini to turn the ICP description + customer examples
    into a structured Apollo search criteria + a scoring rubric. Returns
    (criteria_dict, error).

    Returned shape:
      {
        "keywords": ["...", "..."],         # for q_keywords + tags
        "industries": ["...", "..."],       # human-readable, used in keywords too
        "headcount_bands": ["51,100", ...], # Apollo employee bands
        "locations": ["United States", ...],
        "rubric": "one paragraph the scoring step reuses",
      }
    """
    if not openai_key:
        return {}, "openai key not configured"
    if not icp_description.strip():
        return {}, "ICP description is empty"

    try:
        from openai import OpenAI
    except ImportError:
        return {}, "openai package not installed"

    example_lines = "\n".join(f"- {ex}" for ex in customer_examples[:20]) or "(none provided)"
    bands_list = ", ".join(APOLLO_HEADCOUNT_BANDS)
    prompt = (
        f"A B2B salesperson wants to find lookalike accounts. Here is "
        f"their Ideal Customer Profile and example customers.\n\n"
        f"ICP description:\n{icp_description}\n\n"
        f"Example current customers:\n{example_lines}\n\n"
        f"Extract a structured Apollo search criteria that will surface "
        f"similar companies. Be specific but not over-narrow — Apollo "
        f"returns top matches per call, so prefer 3-8 strong keywords "
        f"over 30 weak ones. Pick headcount bands ONLY from this exact "
        f"list (use the literal strings):\n{bands_list}\n\n"
        f"Also write a one-paragraph scoring rubric (~80 words) that a "
        f"second LLM call will use to score each Apollo result 1-10 on "
        f"ICP fit. Reference the customer-example traits explicitly.\n\n"
        f"Output JSON only, no prose. Shape:\n"
        f'{{"keywords": ["..."], "industries": ["..."], "headcount_bands": '
        f'["..."], "locations": ["..."], "rubric": "..."}}'
    )

    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_FAST,
            messages=[
                {"role": "system",
                 "content": "You are a B2B sales operations expert. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
    except json.JSONDecodeError as e:
        return {}, f"openai returned non-JSON: {e}"
    except Exception as e:  # noqa: BLE001
        return {}, f"openai error: {type(e).__name__}: {e}"

    # Light validation + normalization. We trust the LLM but defend against
    # weird shapes so a single bad call doesn't 500 the route.
    out: dict[str, Any] = {
        "keywords": [str(x).strip() for x in (data.get("keywords") or []) if str(x).strip()][:12],
        "industries": [str(x).strip() for x in (data.get("industries") or []) if str(x).strip()][:8],
        "headcount_bands": [
            b for b in (data.get("headcount_bands") or [])
            if isinstance(b, str) and b in APOLLO_HEADCOUNT_BANDS
        ][:5],
        "locations": [str(x).strip() for x in (data.get("locations") or []) if str(x).strip()][:5],
        "rubric": str(data.get("rubric") or "").strip()[:1500],
    }
    if not out["keywords"] and not out["industries"]:
        return out, "LLM extracted no usable keywords — try a more descriptive ICP"
    return out, None


def apollo_search_companies(
    criteria: dict,
    apollo_key: str,
    per_page: int = 25,
    page: int = 1,
) -> tuple[list[dict], str | None]:
    """Call Apollo's mixed_companies/search with the LLM-extracted
    criteria. Returns (companies, error). Each company is normalized
    to the same keys we expose downstream.

    We send keywords + industries combined as q_keywords (free-text
    search) because Apollo's structured industry filter requires their
    internal industry IDs which we don't have a mapping for. Free-text
    works well enough as a v1.
    """
    if not apollo_key:
        return [], "apollo key not configured"

    keyword_phrases = []
    if criteria.get("keywords"):
        keyword_phrases.extend(criteria["keywords"])
    if criteria.get("industries"):
        keyword_phrases.extend(criteria["industries"])
    q_keywords = " ".join(keyword_phrases)[:300]  # apollo caps this

    body: dict[str, Any] = {
        "page": page,
        "per_page": max(1, min(100, int(per_page))),
    }
    if q_keywords:
        body["q_keywords"] = q_keywords
    if criteria.get("headcount_bands"):
        body["organization_num_employees_ranges"] = criteria["headcount_bands"]
    if criteria.get("locations"):
        body["organization_locations"] = criteria["locations"]

    try:
        r = requests.post(
            APOLLO_COMPANIES_SEARCH_URL,
            headers={"Content-Type": "application/json", "x-api-key": apollo_key},
            json=body,
            timeout=30,
        )
    except requests.RequestException as e:
        return [], f"apollo network error: {type(e).__name__}"
    if r.status_code == 401:
        return [], "apollo auth failed (check APOLLO_API_KEY)"
    if r.status_code == 429:
        return [], "apollo rate-limited — wait a minute and retry"
    if r.status_code != 200:
        snippet = (r.text or "").strip()[:300]
        return [], f"apollo HTTP {r.status_code}: {snippet}"
    try:
        data = r.json()
    except ValueError:
        return [], "apollo returned non-JSON"

    accounts = data.get("accounts") or []
    orgs = data.get("organizations") or []
    raw = accounts + orgs

    out: list[dict] = []
    seen_domains: set = set()
    for c in raw:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        domain = (c.get("primary_domain") or "").strip().lower()
        if not domain:
            website = (c.get("website_url") or "").strip().lower()
            if website.startswith("https://"): website = website[8:]
            elif website.startswith("http://"): website = website[7:]
            if website.startswith("www."): website = website[4:]
            domain = website.split("/", 1)[0]
        if domain and domain in seen_domains:
            continue  # dedupe — accounts + orgs can have overlapping entries
        if domain:
            seen_domains.add(domain)

        out.append({
            "name": name,
            "domain": domain,
            "linkedin_url": (c.get("linkedin_url") or "").strip(),
            "description": (c.get("short_description") or c.get("description") or "").strip()[:400],
            "industry": (c.get("industry") or "").strip(),
            "employee_count": c.get("estimated_num_employees") or None,
            "location": " · ".join(filter(None, [
                (c.get("city") or "").strip(),
                (c.get("state") or "").strip(),
                (c.get("country") or "").strip(),
            ])),
            "website_url": (c.get("website_url") or "").strip(),
        })
    return out, None


def score_candidates(
    candidates: list[dict],
    icp_description: str,
    customer_examples: list[str],
    rubric: str,
    openai_key: str,
) -> tuple[list[dict], str | None]:
    """Score each candidate 1-10 with a short rationale, in one batch
    LLM call. Returns the candidates with two extra keys (`score`,
    `rationale`) and sorted descending by score.

    Why batch and not per-candidate: 25 LLM calls would cost 25x more
    than one batched call and run 25x slower. gpt-4o-mini can read
    25 small candidate blurbs in one prompt without issue.
    """
    if not openai_key:
        # Without OpenAI we still return the candidates, just unscored,
        # sorted by Apollo's default order.
        for c in candidates:
            c["score"] = None
            c["rationale"] = ""
        return candidates, None
    if not candidates:
        return [], None

    try:
        from openai import OpenAI
    except ImportError:
        return candidates, "openai package not installed"

    example_lines = "\n".join(f"- {ex}" for ex in customer_examples[:15]) or "(none)"
    candidate_lines = []
    for i, c in enumerate(candidates):
        bits = [c["name"]]
        if c.get("industry"): bits.append(c["industry"])
        if c.get("employee_count"): bits.append(f"~{c['employee_count']} employees")
        if c.get("location"): bits.append(c["location"])
        header = " · ".join(bits)
        desc = c.get("description") or ""
        candidate_lines.append(f"{i + 1}. {header}\n   {desc}")

    prompt = (
        f"Score each candidate company 1-10 on how closely it matches "
        f"this ICP and resembles the example current customers. Include "
        f"a one-sentence rationale (max 20 words) tying the score to "
        f"concrete attributes.\n\n"
        f"ICP description:\n{icp_description}\n\n"
        f"Example current customers:\n{example_lines}\n\n"
        f"Scoring rubric:\n{rubric or '(use your judgment based on the ICP description above)'}\n\n"
        f"Candidates:\n" + "\n".join(candidate_lines) + "\n\n"
        f"Output JSON only, one entry per candidate by index:\n"
        f'{{"scores": [{{"index": 1, "score": 8, "rationale": "..."}}, ...]}}'
    )

    try:
        client = OpenAI(api_key=openai_key)
        resp = client.chat.completions.create(
            model=OPENAI_MODEL_FAST,
            messages=[
                {"role": "system",
                 "content": "You are a B2B sales prospecting analyst. Output only valid JSON."},
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        content = resp.choices[0].message.content or "{}"
        data = json.loads(content)
        scores = data.get("scores") or []
        # Index scores by candidate position; default to 0 if model
        # skipped any (it sometimes does on long lists).
        by_idx: dict[int, dict] = {}
        for s in scores:
            if not isinstance(s, dict):
                continue
            try:
                idx = int(s.get("index"))
            except (TypeError, ValueError):
                continue
            # Coerce score per-row. LLMs sometimes return "high" / "8/10"
            # / "N/A" instead of an int. Skip the bad row rather than
            # aborting the whole batch, so a single bad entry doesn't
            # wipe out scoring for the other 24.
            raw_score = s.get("score")
            try:
                score = max(1, min(10, int(raw_score) if raw_score is not None else 5))
            except (TypeError, ValueError):
                continue
            by_idx[idx] = {
                "score": score,
                "rationale": str(s.get("rationale") or "").strip()[:200],
            }
        for i, c in enumerate(candidates):
            entry = by_idx.get(i + 1, {"score": 5, "rationale": "(no rationale returned)"})
            c["score"] = entry["score"]
            c["rationale"] = entry["rationale"]
    except json.JSONDecodeError as e:
        return candidates, f"openai scoring returned non-JSON: {e}"
    except Exception as e:  # noqa: BLE001
        return candidates, f"openai scoring error: {type(e).__name__}"

    candidates.sort(key=lambda c: (c.get("score") or 0), reverse=True)
    return candidates, None


def find_lookalikes(
    icp_description: str,
    customer_examples: list[str],
    apollo_key: str,
    openai_key: str,
    *,
    per_page: int = 25,
) -> tuple[dict, str | None]:
    """End-to-end pipeline. Returns ({criteria, candidates}, error).

    On partial failure we still return what we have — the page renders
    whichever stages succeeded with a warning banner for the rest.
    """
    criteria, crit_err = extract_search_criteria(icp_description, customer_examples, openai_key)
    if crit_err and not criteria:
        return {"criteria": {}, "candidates": []}, crit_err

    candidates, search_err = apollo_search_companies(criteria, apollo_key, per_page=per_page)
    if search_err:
        return {"criteria": criteria, "candidates": []}, search_err

    if not candidates:
        return {"criteria": criteria, "candidates": []}, (
            "Apollo returned 0 companies for the LLM-derived filters. "
            "Try a more descriptive ICP or different example customers."
        )

    scored, score_err = score_candidates(
        candidates,
        icp_description,
        customer_examples,
        criteria.get("rubric") or "",
        openai_key,
    )
    if score_err:
        # Surface the warning but still return Apollo results in original order.
        return {"criteria": criteria, "candidates": candidates, "warning": score_err}, None

    return {"criteria": criteria, "candidates": scored}, None
