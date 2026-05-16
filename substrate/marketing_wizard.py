"""Day-1 Keyword Setup Wizard.

Generates a candidate keyword list for a new (or existing) ASIN.

Flow:
  1. open_keyword_session(workspace_id, operator_id, asin) → SessionObject
     Creates a substrate_sessions row with module=marketing.
  2. generate_candidates(workspace_id, asin, n=40, brand_profile_version=...)
     Reads brand_profile + sibling-ASIN keywords from keyword_library +
     calls Anthropic to expand. Returns a list of candidate dicts:
       { keyword, match_type, suggested_bid_low, suggested_bid_high,
         theme, confidence, rationale, has_history }
  3. log_candidate_decision(session_id, candidate_data, asin) writes a
     decision_event (module=marketing, field_name=keyword_candidate).
     Wraps substrate.logger.log_field_decision; the pre_change_snapshot
     picks up any existing keyword_library metrics if present.

Inheritance policy ('b'): when generating candidates for ASIN X, we read
keywords from sibling ASINs in the same workspace from keyword_library.
We do NOT cross-pollinate across brands (workspace_id filter is hard).

Suggested-bid logic ('c→a fallback'):
  - If a sibling keyword has prior CPC/ACOS data, suggest a bid range
    anchored at the existing CPC ± 30%.
  - Otherwise, fall back to a category-default range with a clear
    'starting bid, no data' marker.

LLM is best-effort: if Anthropic is unavailable, returns a rule-based
candidate list (smaller, less varied, no rationale) so the operator can
still complete the wizard.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from substrate.db import get_pool
from substrate.marketing import normalise_keyword

logger = logging.getLogger("atlas.substrate.marketing_wizard")


# ---------------------------------------------------------------------------
# Brand profile + sibling lookup
# ---------------------------------------------------------------------------


def _read_brand_profile(workspace_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT profile_version, brand_name, category_scope, tier_scope,
                       stage_scope, voice_rules, banned_words, required_words,
                       signature_phrases, custom
                FROM brand_profile
                WHERE workspace_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (workspace_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "profile_version":  row[0],
        "brand_name":       row[1],
        "category_scope":   row[2],
        "tier_scope":       row[3],
        "stage_scope":      row[4],
        "voice_rules":      row[5] or [],
        "banned_words":     row[6] or [],
        "required_words":   row[7] or [],
        "signature_phrases": row[8] or [],
        "custom":           row[9] or {},
    }


def _read_sibling_keywords(
    workspace_id: str,
    asin: Optional[str],
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read sibling-ASIN keywords from keyword_library.

    'Siblings' = any other keyword in the same workspace. We deliberately
    do NOT try to infer product variations from the catalog yet — the
    workspace itself is the brand scope, and Novelle is single-brand by
    design. Returns the top-`limit` lowest-ACOS keywords for the inspiration
    set, plus any keyword already linked to this exact ASIN.
    """
    pool = get_pool()
    if pool is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT keyword, keyword_norm, match_type, asins,
                           last_acos, last_spend, last_clicks, last_orders,
                           last_search_volume, last_organic_rank
                    FROM keyword_library
                    WHERE workspace_id = %s
                    ORDER BY
                      CASE WHEN last_acos IS NULL THEN 1 ELSE 0 END,
                      last_acos ASC NULLS LAST,
                      last_clicks DESC NULLS LAST
                    LIMIT %s
                    """,
                    (workspace_id, limit),
                )
                cols = [d[0] for d in cur.description]
                for r in cur:
                    rows.append(dict(zip(cols, r)))
    except Exception as exc:
        logger.warning("sibling read failed: %s", exc)
    return rows


# ---------------------------------------------------------------------------
# Bid logic
# ---------------------------------------------------------------------------


# Cold-start bid ranges by tier_scope. Conservative defaults; explicit.
_COLD_BID_RANGES: dict[str, tuple[float, float]] = {
    "premium":  (0.85, 1.40),
    "mid":      (0.60, 1.00),
    "value":    (0.40, 0.75),
}
_DEFAULT_COLD_BID = (0.75, 1.20)


def _suggest_bid_range(
    existing_keyword: Optional[dict[str, Any]],
    tier_scope: Optional[str],
) -> tuple[float, float, bool]:
    """Return (low, high, has_history).

    Anchors on existing CPC when available (last_spend / last_clicks),
    otherwise uses a tier-scoped cold-start range.
    """
    if existing_keyword:
        spend = existing_keyword.get("last_spend")
        clicks = existing_keyword.get("last_clicks")
        if spend and clicks and clicks > 0:
            cpc = spend / clicks
            return (round(cpc * 0.7, 2), round(cpc * 1.3, 2), True)
    rng = _COLD_BID_RANGES.get((tier_scope or "").lower(), _DEFAULT_COLD_BID)
    return (rng[0], rng[1], False)


# ---------------------------------------------------------------------------
# LLM generation
# ---------------------------------------------------------------------------


def _build_generation_prompt(
    asin: str,
    product_type: Optional[str],
    style_name: Optional[str],
    brand_profile: dict[str, Any],
    siblings: list[dict[str, Any]],
    target_count: int,
) -> str:
    bp = brand_profile or {}
    custom = bp.get("custom") or {}
    sibling_lines: list[str] = []
    for s in siblings[:25]:
        cells = [
            s.get("keyword") or s.get("keyword_norm"),
            f"match={s.get('match_type')}" if s.get("match_type") else None,
            f"ACOS={s['last_acos']:.0%}" if s.get("last_acos") is not None else None,
            f"clicks={s['last_clicks']}" if s.get("last_clicks") else None,
        ]
        sibling_lines.append("  - " + " | ".join(c for c in cells if c))
    sibling_block = "\n".join(sibling_lines) or "  (no prior keywords in this brand's library)"
    banned = ", ".join((bp.get("banned_words") or [])[:20]) or "(none)"
    target_customer = custom.get("target_customer") or "general"
    competitor_set = ", ".join(custom.get("competitor_set") or []) or "(none)"
    return f"""You are an Amazon PPC strategist generating a day-1 keyword candidate list for a new product.

Brand context:
- Brand: {bp.get('brand_name') or 'unknown'}
- Category: {bp.get('category_scope') or 'unknown'}
- Tier: {bp.get('tier_scope') or 'unknown'}
- Stage: {bp.get('stage_scope') or 'unknown'}
- Target customer: {target_customer}
- Competitor set: {competitor_set}
- Banned words (do NOT include any keyword containing these): {banned}

Product:
- ASIN: {asin}
- Product type: {product_type or '(not specified)'}
- Style / model name: {style_name or '(not specified)'}

Existing brand keywords (top performers, lowest ACOS first):
{sibling_block}

TASK
Produce {target_count} candidate search-keyword recommendations for sponsoring this ASIN on Amazon.
Each candidate must be a real Amazon search term a customer would type — not a brand slogan, not a
description. Cover three themes:
  - branded / category-anchored (e.g. exact category + tier descriptor)
  - feature / use-case (what problem does it solve)
  - competitor / comparison (target competitor customers)

For each candidate, also choose ONE recommended match type:
  - "exact"   when the term is high-intent and short (2-3 words)
  - "phrase"  when the term is 3-5 words and contains a clear theme
  - "broad"   when the term is exploratory or long-tail

OUTPUT FORMAT
Return STRICT JSON, no markdown, no commentary. The top-level value is an array of objects
with these keys exactly:
  keyword (string), match_type ("exact"|"phrase"|"broad"),
  theme ("branded"|"feature"|"competitor"), confidence (0.0-1.0),
  rationale (string, max 140 chars)

Do NOT include suggested_bid — that gets computed downstream.
Do NOT include any keyword containing a banned word.
Keep each keyword between 2 and 60 characters.
"""


def _parse_llm_candidates(raw: str) -> list[dict[str, Any]]:
    """Parse the LLM's JSON output, tolerating code fences and partial output."""
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
    raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()
    # Find the first JSON array.
    m = re.search(r"\[\s*\{.*\}\s*\]", raw, flags=re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kw = (item.get("keyword") or "").strip()
        if not kw or len(kw) > 80:
            continue
        mt = (item.get("match_type") or "").strip().lower()
        if mt not in ("exact", "phrase", "broad"):
            mt = "broad"
        theme = (item.get("theme") or "").strip().lower()
        if theme not in ("branded", "feature", "competitor"):
            theme = "feature"
        try:
            conf = float(item.get("confidence") or 0.5)
        except (TypeError, ValueError):
            conf = 0.5
        conf = max(0.0, min(1.0, conf))
        rationale = (item.get("rationale") or "")[:160]
        out.append({
            "keyword": kw,
            "match_type": mt,
            "theme": theme,
            "confidence": conf,
            "rationale": rationale,
        })
    return out


def _rule_based_fallback(
    asin: str,
    product_type: Optional[str],
    style_name: Optional[str],
    brand_profile: dict[str, Any],
    siblings: list[dict[str, Any]],
    target_count: int,
) -> list[dict[str, Any]]:
    """No-LLM fallback. Produces a small, honest candidate list from siblings only.

    This is what the wizard returns when Anthropic is unavailable. Clearly
    inferior to the LLM path but lets the operator proceed.
    """
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Pull sibling keywords first (these have real data).
    for s in siblings:
        kw = s.get("keyword") or s.get("keyword_norm") or ""
        n = normalise_keyword(kw)
        if not n or n in seen:
            continue
        seen.add(n)
        candidates.append({
            "keyword": kw,
            "match_type": s.get("match_type") or "broad",
            "theme": "feature",
            "confidence": 0.5,
            "rationale": "from existing brand library (no LLM available)",
        })
        if len(candidates) >= target_count:
            break
    # If we still don't have enough, synthesise from product_type / style.
    if len(candidates) < target_count and (product_type or style_name):
        seeds = [s for s in [product_type, style_name] if s]
        for s in seeds:
            for kw in (s, s + " womens", s + " mens", s + " for women"):
                n = normalise_keyword(kw)
                if not n or n in seen:
                    continue
                seen.add(n)
                candidates.append({
                    "keyword": kw, "match_type": "broad",
                    "theme": "feature", "confidence": 0.35,
                    "rationale": "synthesised from product_type (no LLM available)",
                })
                if len(candidates) >= target_count:
                    break
    return candidates


def generate_candidates(
    workspace_id: str,
    asin: str,
    *,
    product_type: Optional[str] = None,
    style_name: Optional[str] = None,
    target_count: int = 40,
    anthropic_client: Any = None,
) -> dict[str, Any]:
    """Generate a candidate keyword list. Returns a dict with shape:

        {
            "asin": asin,
            "brand_profile_version": str | None,
            "candidates": [ ... ],
            "source": "llm" | "fallback",
            "siblings_used": int,
        }

    Never raises. On LLM failure, falls back to rule-based generation.
    """
    # Read DB context with bounded blast radius — LLM call must NOT happen
    # while we hold a connection. Both helpers open their own short-lived
    # connection and release it before returning.
    try:
        brand_profile = _read_brand_profile(workspace_id) or {}
    except Exception as exc:
        logger.warning("brand_profile read failed: %s", exc)
        brand_profile = {}
    try:
        siblings = _read_sibling_keywords(workspace_id, asin)
    except Exception as exc:
        logger.warning("sibling read failed: %s", exc)
        siblings = []
    candidates: list[dict[str, Any]] = []
    source = "fallback"
    if anthropic_client is not None:
        try:
            prompt = _build_generation_prompt(
                asin=asin, product_type=product_type, style_name=style_name,
                brand_profile=brand_profile, siblings=siblings,
                target_count=target_count,
            )
            message = anthropic_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=3000,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text
            candidates = _parse_llm_candidates(raw)
            if candidates:
                source = "llm"
        except Exception as exc:
            logger.warning("llm candidate generation failed: %s", exc)
    if not candidates:
        candidates = _rule_based_fallback(
            asin=asin, product_type=product_type, style_name=style_name,
            brand_profile=brand_profile, siblings=siblings,
            target_count=min(target_count, 12),
        )
    # Dedup + enrich with bid suggestions + existing-history flag
    sibling_by_norm: dict[str, dict[str, Any]] = {
        s.get("keyword_norm") or normalise_keyword(s.get("keyword") or ""): s
        for s in siblings
    }
    seen: set[str] = set()
    enriched: list[dict[str, Any]] = []
    banned = set((brand_profile.get("banned_words") or []))
    for c in candidates:
        kw = (c.get("keyword") or "").strip()
        norm = normalise_keyword(kw)
        if not norm or norm in seen:
            continue
        if any(b.lower() in norm for b in banned if isinstance(b, str)):
            continue
        seen.add(norm)
        existing = sibling_by_norm.get(norm)
        low, high, has_history = _suggest_bid_range(existing, brand_profile.get("tier_scope"))
        enriched.append({
            "keyword": kw,
            "keyword_norm": norm,
            "match_type": c.get("match_type", "broad"),
            "theme": c.get("theme", "feature"),
            "confidence": float(c.get("confidence") or 0.5),
            "rationale": c.get("rationale", ""),
            "suggested_bid_low": low,
            "suggested_bid_high": high,
            "has_history": has_history,
            "existing_acos": existing.get("last_acos") if existing else None,
            "existing_clicks": existing.get("last_clicks") if existing else None,
        })
    return {
        "asin": asin,
        "brand_profile_version": brand_profile.get("profile_version"),
        "candidates": enriched,
        "source": source,
        "siblings_used": len(siblings),
    }


# ---------------------------------------------------------------------------
# CSV export (Campaign Manager bulk format)
# ---------------------------------------------------------------------------


def candidates_to_bulk_csv(
    asin: str,
    accepted: list[dict[str, Any]],
    *,
    campaign_name: str = "Atlas day-1",
    ad_group_name: Optional[str] = None,
) -> str:
    """Render accepted candidates as a minimal Campaign Manager bulk CSV.

    Columns are a defensible subset of Amazon's SP bulk format; the operator
    will paste into the real bulk template as needed.
    """
    ad_group_name = ad_group_name or f"day-1 · {asin}"
    headers = [
        "Campaign Name", "Ad Group Name", "ASIN", "Keyword Text",
        "Match Type", "Bid", "State",
    ]
    lines = [",".join(headers)]
    for c in accepted:
        kw = (c.get("keyword") or "").replace(",", " ")
        mt = (c.get("match_type") or "broad").lower()
        low = c.get("suggested_bid_low") or 0.75
        high = c.get("suggested_bid_high") or 1.20
        # Use the mid-point as the suggested bid; operator can override
        # before they upload to Amazon.
        bid = round((float(low) + float(high)) / 2.0, 2)
        lines.append(",".join([
            campaign_name, ad_group_name, asin, kw, mt, f"{bid:.2f}", "enabled",
        ]))
    return "\n".join(lines) + "\n"


__all__ = [
    "generate_candidates",
    "candidates_to_bulk_csv",
]
