"""Atlas substrate — field_suggest.

Reads field_schema.yml and serves the four-mode entry framework:

  substrate_read  — auto-fill from substrate; operator confirms/overrides
  q_and_a         — Atlas asks; operator types; reasoning required
  llm_suggest     — LLM proposes 2-3 options with reasoning; operator picks
  manual_only     — no LLM; operator types; consistency check at save

Contract:
    load_field_schema()                       -> dict
    get_field_spec(table, field)              -> dict | None
    suggest_for_field(...)                    -> dict
        Returns {ok, mode, label, hint, options?, question?, prompt_text?}

The suggest_for_field call never raises. If the LLM is unavailable or the
field has no suggester, it falls back to manual entry instructions.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

logger = logging.getLogger("atlas.substrate.field_suggest")

_SCHEMA_CACHE: Optional[dict[str, Any]] = None
_SCHEMA_MTIME: Optional[float] = None


def _schema_path() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "field_schema.yml")


def load_field_schema(force_reload: bool = False) -> dict[str, Any]:
    """Load field_schema.yml and return as dict. Cached by mtime."""
    global _SCHEMA_CACHE, _SCHEMA_MTIME
    path = _schema_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}

    if (not force_reload
            and _SCHEMA_CACHE is not None
            and _SCHEMA_MTIME == mtime):
        return _SCHEMA_CACHE

    try:
        import yaml  # PyYAML; already present in repo deps for marketing
    except ImportError:
        logger.warning("PyYAML missing; cannot load field_schema.yml")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        _SCHEMA_CACHE = data
        _SCHEMA_MTIME = mtime
        return data
    except Exception as exc:
        logger.warning("load_field_schema failed: %s", exc)
        return {}


def get_field_spec(
    table: str, field: str
) -> Optional[dict[str, Any]]:
    """Get the spec dict for a single (table, field). None if absent."""
    schema = load_field_schema()
    table_spec = schema.get(table) or {}
    return table_spec.get(field)


def _fill_template(template: str, context: dict[str, Any]) -> str:
    """Replace {key} placeholders in template with context[key].
    Unknown keys are replaced with their literal label (e.g., '[unknown]')."""
    def _replace(m: re.Match[str]) -> str:
        key = m.group(1)
        val = context.get(key)
        if val is None or val == "":
            return f"[{key}]"
        return str(val)
    return re.sub(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}", _replace, template)


def _claude_client():
    """Resolve the existing Anthropic client lazily from app.py."""
    try:
        import app  # type: ignore
        return getattr(app, "_anthropic_client", None)
    except Exception:
        return None


def _call_llm_for_suggestions(
    prompt: str,
    *,
    expected_count: int = 3,
    max_tokens: int = 800,
) -> list[dict[str, Any]]:
    """Call the LLM and parse JSON suggestions list.

    Always returns a list (possibly empty). Never raises.
    Expected schema:
      [{"value": <string|list>, "reasoning": <string>}]
    """
    client = _claude_client()
    if client is None:
        return []
    instruction = (
        prompt
        + "\n\nReturn a JSON array of "
        + f"{expected_count} objects in the form "
        + '[{"value": "...", "reasoning": "one short sentence"}].'
        + " No prose around the JSON. No markdown fences."
    )
    try:
        message = client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": instruction}],
        )
        raw = (message.content[0].text or "").strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"```\s*$", "", raw, flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        out = []
        for item in parsed[:expected_count]:
            if not isinstance(item, dict):
                continue
            out.append({
                "value": item.get("value"),
                "reasoning": item.get("reasoning") or "",
            })
        return out
    except Exception as exc:
        logger.warning("_call_llm_for_suggestions failed: %s", exc)
        return []


def suggest_for_field(
    table: str,
    field: str,
    *,
    context: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Resolve the field's mode + payload.

    Return shape:
      {
        "ok":    bool,
        "mode":  one of substrate_read|q_and_a|llm_suggest|manual_only,
        "label": str,
        "hint":  str|None,
        # mode-specific:
        "question":    str (q_and_a)
        "options":     list[dict] (llm_suggest)
        "fallback":    str (when substrate_read with missing data)
        "consistency_check": list[str] (manual_only)
      }
    """
    context = context or {}
    spec = get_field_spec(table, field)
    if not spec:
        return {
            "ok": False,
            "error": f"unknown field {table}.{field}",
        }

    mode = spec.get("mode") or "manual_only"
    label = spec.get("label") or field
    hint = spec.get("hint")

    out: dict[str, Any] = {
        "ok": True,
        "table": table,
        "field": field,
        "mode": mode,
        "label": label,
        "hint": hint,
    }

    if mode == "substrate_read":
        out["read_from"] = spec.get("read_from")
        if "options" in spec:  # enum-style
            out["options"] = spec["options"]
        if "fallback" in spec:
            out["fallback"] = spec["fallback"]
        return out

    if mode == "q_and_a":
        question = spec.get("question") or ""
        out["question"] = _fill_template(question, context).strip()
        return out

    if mode == "manual_only":
        if "consistency_check" in spec:
            out["consistency_check"] = spec["consistency_check"]
        if "source_required" in spec:
            out["source_required"] = spec["source_required"]
        return out

    if mode == "llm_suggest":
        prompt_template = spec.get("suggest_prompt") or ""
        prompt = _fill_template(prompt_template, context).strip()
        n = int(spec.get("suggest_count") or 3)
        options = _call_llm_for_suggestions(prompt, expected_count=n)
        out["options"] = options
        out["prompt_text"] = prompt
        out["llm_available"] = bool(options) or _claude_client() is not None
        return out

    out["ok"] = False
    out["error"] = f"unknown mode {mode} for {table}.{field}"
    return out


def list_fields(table: str) -> list[str]:
    """All declared fields for a table."""
    schema = load_field_schema()
    return list((schema.get(table) or {}).keys())


def list_tables() -> list[str]:
    """All tables declared in field_schema.yml."""
    return list(load_field_schema().keys())


__all__ = [
    "load_field_schema",
    "get_field_spec",
    "suggest_for_field",
    "list_fields",
    "list_tables",
]
