"""Atlas brand_profile seeder.

Idempotent. Run on app boot to ensure every workspace we expect to operate
on has a baseline brand_profile row. The voice fields are deliberately
under-populated \u2014 a real voice is a writing exercise the operator owns,
not a dev-side default.

To add or revise a profile, edit the SEED_PROFILES dict here. ON CONFLICT
DO NOTHING means existing profiles are never overwritten by re-running
this; promote profile_version when intentional updates are made.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("atlas.substrate.brand_profile_seed")

# Each entry seeds one (workspace_id, profile_version) row. The voice
# fields are placeholders \u2014 the structure is correct, the content needs
# the operator's worksheet.
SEED_PROFILES = [
    {
        "workspace_id": "novelle",
        "profile_version": "novelle_v1.0",
        "brand_name": "Novelle",
        "category_scope": "activewear",
        "tier_scope": "premium",
        "stage_scope": "launch",
        "voice_rules": [
            # Placeholders. Will be replaced by content from the operator's
            # voice worksheet. Until then, these are NOT used in NIS prompts.
            {"_placeholder": True, "_note": "needs operator input"},
        ],
        "banned_words": [
            "cheap",
            "fast fashion",
            "knockoff",
            "basic",
        ],
        "required_words": [],
        "signature_phrases": [],
        "custom": {
            "target_customer": "young adults, 18\u201340, aspirational",
            "competitor_set": ["CRZ Yoga"],
            "competitor_set_note": (
                "Preliminary. Should expand to span Novelle's positioning "
                "(e.g. Vuori, Outdoor Voices, Beyond Yoga, Halara). Set "
                "during May 16 strategy session; revisit after launch."
            ),
            "current_product_focus": "leggings only",
            "stage_note": "Pre-launch. First inventory arriving soon.",
            "_voice_status": "placeholder \u2014 awaits operator worksheet",
        },
    },
]


def seed_brand_profiles() -> int:
    """Insert any missing seed profiles into the brand_profile table.

    Returns the number of rows inserted. No-ops when DB is unavailable
    (best-effort, matches substrate philosophy).
    """
    from substrate.db import get_pool

    pool = get_pool()
    if pool is None:
        return 0

    inserted = 0
    sql = """
        INSERT INTO brand_profile (
            workspace_id, profile_version, brand_name,
            category_scope, tier_scope, stage_scope,
            voice_rules, banned_words, required_words,
            signature_phrases, custom
        ) VALUES (
            %(workspace_id)s, %(profile_version)s, %(brand_name)s,
            %(category_scope)s, %(tier_scope)s, %(stage_scope)s,
            %(voice_rules)s::jsonb, %(banned_words)s::jsonb, %(required_words)s::jsonb,
            %(signature_phrases)s::jsonb, %(custom)s::jsonb
        )
        ON CONFLICT (workspace_id, profile_version) DO NOTHING
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for p in SEED_PROFILES:
                params = {
                    "workspace_id": p["workspace_id"],
                    "profile_version": p["profile_version"],
                    "brand_name": p.get("brand_name"),
                    "category_scope": p.get("category_scope"),
                    "tier_scope": p.get("tier_scope"),
                    "stage_scope": p.get("stage_scope"),
                    "voice_rules": json.dumps(p.get("voice_rules") or []),
                    "banned_words": json.dumps(p.get("banned_words") or []),
                    "required_words": json.dumps(p.get("required_words") or []),
                    "signature_phrases": json.dumps(p.get("signature_phrases") or []),
                    "custom": json.dumps(p.get("custom") or {}),
                }
                cur.execute(sql, params)
                if cur.rowcount > 0:
                    inserted += 1
        conn.commit()
    return inserted


__all__ = ["SEED_PROFILES", "seed_brand_profiles"]
