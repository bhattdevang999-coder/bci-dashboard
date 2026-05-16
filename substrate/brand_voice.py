"""Atlas Brand Voice substrate.

Wraps the brand_profile table with read/write helpers that:
  1. Surface a normalized voice payload (tone, hero adjectives, banned
     words, signature phrases, like/unlike examples, etc.) for editor UIs
     and prompt builders.
  2. Auto-bump profile_version on every save so the audit-trail link
     (decision_event.brand_profile_version → brand_profile row) finally
     points to a specific version of the voice that was in force when
     the decision was made.
  3. Log every voice edit as a decision_event with module='brand_voice'.

Design rationale (see substrate/BRAND_VOICE.md for the full audit):

  - There used to be two stores: brand_configs/<Brand>.json AND
    brand_profile. The JSON files keep their place for OPERATIONAL
    DEFAULTS (default_care, default_upf, vendor_code, etc.) — never
    voice. This module is the single canonical store for voice.

  - voice_rules is a list of {type, value, ...} entries. Types we
    formally name:
        tone_descriptor       ("warm", "plainspoken")
        hero_adjective        ("buttery", "all-day")
        banned_phrasing       ("limited time only")   (compare: banned_words for single words)
        like_example          a short sentence that exemplifies the voice
        unlike_example        a short sentence that violates the voice
    Storing these as typed entries (not a free-text blob) makes the
    drift detector tractable later.

  - profile_version is monotonically bumped (vN -> vN+1) on every save.
    Postgres rows are cheap; the audit trail needs the version chain.
    History is append-only: old versions stay, new versions are added.

  - read_voice() returns a friendly normalized dict for both editor UI
    and prompt builders to consume. Both should call this — never reach
    into voice_rules directly.

  - save_voice() is upsert-style: takes the user's submitted payload,
    bumps the version, inserts a new brand_profile row, AND writes a
    decision_event with module='brand_voice' so the change lands in
    Memory. The Memory module column already has the new value.

Never raises. Best-effort, like every other substrate write.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from substrate.db import get_pool

logger = logging.getLogger("atlas.substrate.brand_voice")


# ---------------------------------------------------------------------------
# Voice-entry types we formally name. Anything else falls under `custom`
# in the returned payload (preserved on save, just not in the formal map).
# ---------------------------------------------------------------------------

_VOICE_RULE_TYPES = (
    "tone_descriptor",
    "hero_adjective",
    "banned_phrasing",
    "like_example",
    "unlike_example",
)


# ---------------------------------------------------------------------------
# Version helpers
# ---------------------------------------------------------------------------


_VERSION_RE = re.compile(r"^(?P<workspace>.+)_v(?P<major>\d+)\.(?P<minor>\d+)$")


def _bump_version(current: Optional[str], workspace_id: str) -> str:
    """Return the next version string after `current`.

    Conventions:
      - Auto-bumped versions only increase the minor (workspace_v1.0 -> v1.1).
      - If `current` is missing or unparseable (legacy 'novelle_legacy'),
        return `<workspace>_v1.0` as the first formal version.
      - Major bumps are reserved for explicit operator action (not yet
        wired in v1; placeholder for later).
    """
    if not current:
        return f"{workspace_id}_v1.0"
    m = _VERSION_RE.match(current)
    if not m:
        # legacy / hand-rolled string -> start the formal chain.
        return f"{workspace_id}_v1.0"
    major = int(m.group("major"))
    minor = int(m.group("minor")) + 1
    return f"{workspace_id}_v{major}.{minor}"


def _latest_version(workspace_id: str) -> Optional[dict[str, Any]]:
    """Return the most-recent brand_profile row for a workspace, or None."""
    pool = get_pool()
    if pool is None:
        return None
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT workspace_id, profile_version, created_at, brand_name,
                           category_scope, tier_scope, stage_scope,
                           voice_rules, banned_words, required_words,
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
                cols = [d[0] for d in cur.description]
                return dict(zip(cols, row))
    except Exception as exc:
        logger.warning("brand_profile latest read failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def read_voice(workspace_id: str) -> dict[str, Any]:
    """Return the normalized voice payload for a workspace.

    Shape:
      {
        ok, workspace_id, profile_version, created_at, brand_name,
        category_scope, tier_scope, stage_scope,

        tone_descriptors: list[str],
        hero_adjectives:  list[str],
        banned_words:     list[str],     # single words
        banned_phrasings: list[str],     # multi-word phrasings
        required_words:   list[str],
        signature_phrases: list[str],
        like_examples:    list[str],
        unlike_examples:  list[str],

        target_customer:  str,           # from custom
        competitor_set:   list[str],     # from custom
        custom_raw:       dict,          # everything else in 'custom' verbatim
      }

    Empty voice surfaces as empty lists, not None. Always returns a
    well-formed dict.
    """
    row = _latest_version(workspace_id)
    out: dict[str, Any] = {
        "ok": True,
        "workspace_id": workspace_id,
        "profile_version": None,
        "created_at": None,
        "brand_name": None,
        "category_scope": None,
        "tier_scope": None,
        "stage_scope": None,
        "tone_descriptors": [],
        "hero_adjectives": [],
        "banned_words": [],
        "banned_phrasings": [],
        "required_words": [],
        "signature_phrases": [],
        "like_examples": [],
        "unlike_examples": [],
        "target_customer": "",
        "competitor_set": [],
        "custom_raw": {},
    }
    if row is None:
        return out

    out["profile_version"] = row.get("profile_version")
    ca = row.get("created_at")
    out["created_at"] = ca.isoformat() if hasattr(ca, "isoformat") else ca
    for k in ("brand_name", "category_scope", "tier_scope", "stage_scope"):
        out[k] = row.get(k)

    # Flat lists already in their own columns
    out["banned_words"] = list(row.get("banned_words") or [])
    out["required_words"] = list(row.get("required_words") or [])
    out["signature_phrases"] = list(row.get("signature_phrases") or [])

    # voice_rules: typed entries — bucket by type
    rules = row.get("voice_rules") or []
    if isinstance(rules, list):
        for r in rules:
            if not isinstance(r, dict):
                continue
            if r.get("_placeholder"):
                # legacy seeded placeholder; ignore for normalized output
                continue
            t = r.get("type")
            v = r.get("value") if "value" in r else r.get("text")
            if not t or v is None:
                continue
            v = str(v).strip()
            if not v:
                continue
            if t == "tone_descriptor":
                out["tone_descriptors"].append(v)
            elif t == "hero_adjective":
                out["hero_adjectives"].append(v)
            elif t == "banned_phrasing":
                out["banned_phrasings"].append(v)
            elif t == "like_example":
                out["like_examples"].append(v)
            elif t == "unlike_example":
                out["unlike_examples"].append(v)

    # custom dict: pull out named fields, keep the rest in custom_raw
    custom = row.get("custom") or {}
    if isinstance(custom, dict):
        out["target_customer"] = str(custom.get("target_customer") or "")
        cset = custom.get("competitor_set") or []
        if isinstance(cset, list):
            out["competitor_set"] = [str(x) for x in cset if x]
        # custom_raw = custom minus the keys we surfaced
        named = {"target_customer", "competitor_set"}
        out["custom_raw"] = {k: v for k, v in custom.items() if k not in named}

    return out


# ---------------------------------------------------------------------------
# Write API
# ---------------------------------------------------------------------------


def _coerce_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        # accept comma-newline-separated input from the editor textarea
        parts = re.split(r"[\n,]+", v)
        return [p.strip() for p in parts if p.strip()]
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return []


def save_voice(
    workspace_id: str,
    payload: dict[str, Any],
    *,
    operator_id: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a new brand_profile row + write a decision_event.

    Behavior:
      - Bumps profile_version to vN+1 of the latest existing row.
      - Inserts a NEW row (history is append-only; no UPDATE).
      - Writes a decision_event with module='brand_voice',
        field_name='profile_revision', atlas_output = the saved payload.

    `payload` keys (all optional, missing keys preserve previous values
    from the latest row):
      brand_name, category_scope, tier_scope, stage_scope,
      tone_descriptors, hero_adjectives,
      banned_words, banned_phrasings, required_words, signature_phrases,
      like_examples, unlike_examples,
      target_customer, competitor_set,
      custom_raw (free-form merged into custom)

    Returns:
      {ok, profile_version, event_id, error?}
    """
    if not workspace_id:
        return {"ok": False, "error": "workspace_id required"}

    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}

    prev = _latest_version(workspace_id) or {}
    new_version = _bump_version(prev.get("profile_version"), workspace_id)

    # Merge: payload wins where present; previous row fills the rest.
    def pick(key: str, default: Any = None) -> Any:
        if key in payload and payload[key] is not None:
            return payload[key]
        return prev.get(key) if prev else default

    brand_name = pick("brand_name", workspace_id.title())
    category_scope = pick("category_scope")
    tier_scope = pick("tier_scope")
    stage_scope = pick("stage_scope")

    # Single-word and phrase lists go to their own columns
    banned_words = _coerce_list(payload.get("banned_words", prev.get("banned_words")))
    required_words = _coerce_list(payload.get("required_words", prev.get("required_words")))
    signature_phrases = _coerce_list(payload.get("signature_phrases", prev.get("signature_phrases")))

    # voice_rules: typed entries. Rebuild from the structured payload fields.
    # If a payload key is missing entirely, preserve the previous typed entries
    # of that type.
    prev_rules = prev.get("voice_rules") or []
    prev_by_type: dict[str, list[str]] = {t: [] for t in _VOICE_RULE_TYPES}
    if isinstance(prev_rules, list):
        for r in prev_rules:
            if not isinstance(r, dict) or r.get("_placeholder"):
                continue
            t = r.get("type")
            v = r.get("value") if "value" in r else r.get("text")
            if t in prev_by_type and v:
                prev_by_type[t].append(str(v).strip())

    def merged_rule_list(payload_key: str, rule_type: str) -> list[str]:
        if payload_key in payload and payload[payload_key] is not None:
            return _coerce_list(payload[payload_key])
        return prev_by_type[rule_type]

    tone = merged_rule_list("tone_descriptors", "tone_descriptor")
    hero = merged_rule_list("hero_adjectives", "hero_adjective")
    banned_phr = merged_rule_list("banned_phrasings", "banned_phrasing")
    like_ex = merged_rule_list("like_examples", "like_example")
    unlike_ex = merged_rule_list("unlike_examples", "unlike_example")

    voice_rules: list[dict[str, Any]] = []
    for v in tone:
        voice_rules.append({"type": "tone_descriptor", "value": v})
    for v in hero:
        voice_rules.append({"type": "hero_adjective", "value": v})
    for v in banned_phr:
        voice_rules.append({"type": "banned_phrasing", "value": v})
    for v in like_ex:
        voice_rules.append({"type": "like_example", "text": v})
    for v in unlike_ex:
        voice_rules.append({"type": "unlike_example", "text": v})

    # custom: merge target_customer + competitor_set + custom_raw
    prev_custom = prev.get("custom") or {}
    if not isinstance(prev_custom, dict):
        prev_custom = {}
    new_custom: dict[str, Any] = dict(prev_custom)
    if "target_customer" in payload:
        new_custom["target_customer"] = str(payload.get("target_customer") or "")
    if "competitor_set" in payload:
        new_custom["competitor_set"] = _coerce_list(payload.get("competitor_set"))
    if "custom_raw" in payload and isinstance(payload["custom_raw"], dict):
        for k, v in payload["custom_raw"].items():
            new_custom[k] = v
    # drop legacy placeholder note if present
    new_custom.pop("_voice_status", None)

    import json
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO brand_profile (
                        workspace_id, profile_version, brand_name,
                        category_scope, tier_scope, stage_scope,
                        voice_rules, banned_words, required_words,
                        signature_phrases, custom
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s::jsonb,
                        %s::jsonb, %s::jsonb
                    )
                    """,
                    (
                        workspace_id, new_version, brand_name,
                        category_scope, tier_scope, stage_scope,
                        json.dumps(voice_rules),
                        json.dumps(banned_words),
                        json.dumps(required_words),
                        json.dumps(signature_phrases),
                        json.dumps(new_custom),
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("brand_profile save failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    # Audit-trail decision_event. Best-effort.
    event_id = None
    try:
        from substrate.logger import log_field_decision
        from substrate.schema import Module
        event_id = log_field_decision(
            workspace_id=workspace_id,
            session_id=None,
            module=Module.BRAND_VOICE,
            field_name="profile_revision",
            atlas_output={
                "profile_version": new_version,
                "tone_descriptors": tone,
                "hero_adjectives": hero,
                "banned_words": banned_words,
                "banned_phrasings": banned_phr,
                "required_words": required_words,
                "signature_phrases": signature_phrases,
                "like_examples": like_ex,
                "unlike_examples": unlike_ex,
                "target_customer": new_custom.get("target_customer", ""),
                "competitor_set": new_custom.get("competitor_set", []),
            },
            overall_confidence=1.0,
            rules_injected=[],
            brand_profile_version=new_version,
            enforce_filter=False,
        )
    except Exception as exc:
        logger.warning("brand_voice decision_event write skipped: %s", exc)

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "profile_version": new_version,
        "event_id": event_id,
    }


# ---------------------------------------------------------------------------
# Prompt block builder — consumed by NIS / Image-NIS / Marketing prompts
# ---------------------------------------------------------------------------


def voice_prompt_block(workspace_id: str) -> str:
    """Return a structured BRAND VOICE prompt block (text).

    Used by generate_content_llm and Image → NIS generator. Falls back
    to a minimal block when no voice has been edited yet, so the prompt
    is still valid (Day-1 brands shouldn't crash).
    """
    v = read_voice(workspace_id)
    lines: list[str] = ["=== BRAND VOICE ==="]
    if v.get("brand_name"):
        lines.append(f"Brand: {v['brand_name']}")
    scope_bits = []
    for k in ("category_scope", "tier_scope", "stage_scope"):
        if v.get(k):
            scope_bits.append(f"{k.replace('_scope','')}={v[k]}")
    if scope_bits:
        lines.append("Positioning: " + " · ".join(scope_bits))
    if v.get("target_customer"):
        lines.append(f"Target customer: {v['target_customer']}")
    if v["tone_descriptors"]:
        lines.append("Tone: " + ", ".join(v["tone_descriptors"]))
    if v["hero_adjectives"]:
        lines.append("Hero adjectives (use these often, sparingly each): "
                     + ", ".join(v["hero_adjectives"]))
    if v["signature_phrases"]:
        lines.append("Signature phrases (work in at least one when natural): "
                     + " | ".join(v["signature_phrases"]))
    if v["required_words"]:
        lines.append("Required words (must appear somewhere): "
                     + ", ".join(v["required_words"]))
    if v["banned_words"]:
        lines.append("Never use these words: " + ", ".join(v["banned_words"]))
    if v["banned_phrasings"]:
        lines.append("Never use these phrasings: "
                     + " | ".join(v["banned_phrasings"]))
    if v["like_examples"]:
        lines.append("Examples that EXEMPLIFY this voice:")
        for ex in v["like_examples"]:
            lines.append(f"  ✓ {ex}")
    if v["unlike_examples"]:
        lines.append("Examples that VIOLATE this voice:")
        for ex in v["unlike_examples"]:
            lines.append(f"  ✗ {ex}")
    if len(lines) == 1:
        lines.append("(No brand voice defined yet — write generic, neutral copy.)")
    return "\n".join(lines)


__all__ = ["read_voice", "save_voice", "voice_prompt_block"]
