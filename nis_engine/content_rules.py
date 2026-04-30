"""Permanent baseline content rules for Amazon NIS — applies across ALL brands.

Rules are loaded from `content_rules.json` (sibling file). This module exposes
helpers for title/bullet/description/backend-keyword composition that respect
the dashboard-always-wins principle:

   pre-upload value > brand override > AI generated baseline

Title hard limit: 120 chars (Vendor Central apparel).
Bullets: <=256 chars each, ALL-CAPS headline + em-dash + benefit sentence.
Description: <=2000.
Backend keywords: <=249 bytes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional, Sequence

_RULES_PATH = Path(__file__).parent / "content_rules.json"
_RULES_CACHE: Optional[dict] = None


def get_rules() -> dict:
    """Load (and cache) the content_rules.json file."""
    global _RULES_CACHE
    if _RULES_CACHE is None:
        with open(_RULES_PATH, "r") as f:
            _RULES_CACHE = json.load(f)
    return _RULES_CACHE


def reload_rules() -> dict:
    """Force-reload from disk (used after operator edits)."""
    global _RULES_CACHE
    _RULES_CACHE = None
    return get_rules()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_ANTI_PATTERN_RE = None


def is_garbage_value(s: Optional[str]) -> bool:
    """Return True if `s` looks like a parsing artefact (style number alone,
    #N/A, null, etc.) and should NOT be used as content."""
    global _ANTI_PATTERN_RE
    if not s:
        return True
    s = str(s).strip()
    if not s:
        return True
    if _ANTI_PATTERN_RE is None:
        rules = get_rules()
        pats = rules.get("item_name_anti_patterns", [])
        _ANTI_PATTERN_RE = re.compile("|".join(pats), re.IGNORECASE)
    return bool(_ANTI_PATTERN_RE.search(s))


def gender_word_for(department: Optional[str], age_range: Optional[str] = None,
                    target_gender: Optional[str] = None) -> str:
    """Return the human-readable gender word ('Women's', 'Men's', 'Girls'', etc.).

    Priority: explicit department → age_range hint → target_gender → ''.
    """
    rules = get_rules()
    table = rules.get("department_to_gender_word", {})
    dept = (department or "").strip().lower()
    if dept in table:
        return table[dept]
    if dept and "men" in dept:
        return "Men's" if "wo" not in dept else "Women's"
    age = (age_range or "").strip().lower()
    if "girls" in age:
        return "Girls'"
    if "boys" in age:
        return "Boys'"
    tg = (target_gender or "").strip().lower()
    if tg in ("female", "f", "women"):
        return "Women's"
    if tg in ("male", "m", "men"):
        return "Men's"
    return ""


def truncate_at_word(s: str, max_len: int) -> str:
    """Truncate at last whitespace before max_len; strip trailing punctuation."""
    if not s or len(s) <= max_len:
        return s or ""
    cut = s[:max_len]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,;-:")


def clean_extra_spaces(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r",\s*,", ",", s)
    s = re.sub(r"\s*([,;])\s*", r"\1 ", s)
    s = re.sub(r",\s*$", "", s)
    return s.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Title composition (rule-driven)
# ─────────────────────────────────────────────────────────────────────────────

def compose_title(
    brand: str,
    gender_word: str,
    style_name: str,
    item_type_name: str,
    feature_phrases: Optional[Sequence[str]] = None,
    color: str = "",
    size: str = "",
    max_length: Optional[int] = None,
) -> str:
    """Compose an Amazon-NIS-compliant title.

    - Prefix: '{brand} {gender_word}'
    - Body: clean style_name with item_type appended only if missing
    - Tail: top 1-2 feature phrases, then optionally color/size
    - Hard cap: 120 chars (or rules.title.max_length)

    Never returns a title containing the style number alone or #N/A.
    """
    rules = get_rules()
    cap = max_length or rules.get("title", {}).get("max_length", 120)

    brand_part = (brand or "").strip()
    gender_part = (gender_word or "").strip()

    # If style_name is empty/garbage, fall back to item_type
    sn = (style_name or "").strip()
    if is_garbage_value(sn):
        sn = ""

    itn = (item_type_name or "").strip()

    # Avoid duplicating item_type_name if its key word is already in style_name.
    # 'Wool Coat' should not be appended if 'Coat' is already there.
    def _last_word(s):
        toks = re.findall(r"[A-Za-z]+", s or "")
        return toks[-1].lower() if toks else ""
    itn_last = _last_word(itn)
    if itn and sn and (itn.lower() in sn.lower() or (itn_last and itn_last in sn.lower().split())):
        body = sn
    elif sn and itn:
        body = f"{sn} {itn}"
    elif sn:
        body = sn
    elif itn:
        body = itn
    else:
        body = ""

    tail_bits: List[str] = []
    for f in (feature_phrases or []):
        if not f:
            continue
        f = str(f).strip()
        if f and f.lower() not in body.lower() and f.lower() not in " ".join(tail_bits).lower():
            tail_bits.append(f)

    # Color/size are usually parent-level title additions for child variation titles
    if color and color.lower() not in body.lower() and color.lower() not in " ".join(tail_bits).lower():
        tail_bits.append(color)
    if size:
        tail_bits.append(size)

    parts = [brand_part, gender_part, body]
    title = " ".join(p for p in parts if p)
    if tail_bits:
        title += " - " + ", ".join(tail_bits)

    title = clean_extra_spaces(title)

    # Enforce cap: strip tail features one at a time, then truncate at word
    while len(title) > cap and tail_bits:
        tail_bits.pop()
        title = " ".join(p for p in parts if p)
        if tail_bits:
            title += " - " + ", ".join(tail_bits)
        title = clean_extra_spaces(title)
    if len(title) > cap:
        title = truncate_at_word(title, cap)
    return title


# ─────────────────────────────────────────────────────────────────────────────
# Bullets (file wins, AI fills gaps)
# ─────────────────────────────────────────────────────────────────────────────

def slot_topics() -> List[str]:
    rules = get_rules()
    return [s["topic"] for s in rules.get("bullets", {}).get("slot_intent", [])]


def normalize_bullet(text: str, max_len: int = 256) -> str:
    """Ensure each bullet has an ALL-CAPS headline + em-dash + body.

    If `text` already has the pattern, leave it alone. If not, derive a
    headline from the first 3-4 words, uppercase them, and inject an em-dash.
    """
    if not text:
        return ""
    s = str(text).strip()
    if not s:
        return ""
    # Already has em-dash separator?
    if " — " in s or " - " in s:
        head, sep, rest = s.partition(" — ")
        if not sep:
            head, sep, rest = s.partition(" - ")
        head = head.strip()
        rest = rest.strip()
        # If the headline isn't already ALL CAPS, uppercase it
        if not head.isupper():
            head = head.upper()
        s = f"{head} — {rest}" if rest else head
    else:
        # Derive a headline from first few words
        words = s.split()
        head_words = words[: min(4, max(2, len(words) // 4))]
        head = " ".join(head_words).upper()
        rest = " ".join(words[len(head_words):]).strip()
        if rest:
            s = f"{head} — {rest}"
        else:
            s = head
    s = clean_extra_spaces(s)
    if len(s) > max_len:
        s = truncate_at_word(s, max_len)
    return s


def merge_bullets(
    pre_upload_bullets: Sequence[str],
    ai_generated_bullets: Sequence[str],
) -> List[str]:
    """Slot-by-slot merge: pre-upload wins where present, AI fills the gaps.

    Always returns exactly 5 bullets. Each bullet is normalized to the
    ALL-CAPS-headline + em-dash + body format and capped at 256 chars.
    """
    rules = get_rules()
    cap = rules.get("bullets", {}).get("max_length_each", 256)
    pu = list(pre_upload_bullets) + [""] * 5
    ai = list(ai_generated_bullets) + [""] * 5
    out = []
    for i in range(5):
        chosen = pu[i].strip() if pu[i] else ""
        if not chosen and ai[i]:
            chosen = ai[i]
        out.append(normalize_bullet(chosen, max_len=cap))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Backend keywords
# ─────────────────────────────────────────────────────────────────────────────

def compose_backend_keywords(
    seed_keywords: Sequence[str],
    title: str = "",
    bullets: Optional[Sequence[str]] = None,
    max_bytes: int = 249,
) -> str:
    """Build backend search-term string (lowercase, space-sep, deduped, no
    overlap with title/bullets, capped at 249 bytes)."""
    used = set()
    text = " ".join(filter(None, [title or ""] + list(bullets or []))).lower()
    used.update(re.findall(r"[a-z0-9]+", text))

    out_terms: List[str] = []
    out_set = set()
    for kw in seed_keywords:
        if not kw:
            continue
        for tok in re.split(r"[,/\n]+", str(kw).lower()):
            tok = tok.strip()
            if not tok or tok in out_set:
                continue
            words = tok.split()
            # Skip if every word already appears in title/bullets
            if words and all(w in used for w in words):
                continue
            out_terms.append(tok)
            out_set.add(tok)

    result = " ".join(out_terms)
    # Truncate by bytes
    enc = result.encode("utf-8")
    while len(enc) > max_bytes and out_terms:
        out_terms.pop()
        result = " ".join(out_terms)
        enc = result.encode("utf-8")
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Validators (used by QA panel + content-rules tests)
# ─────────────────────────────────────────────────────────────────────────────

def qa_check(content: dict) -> List[dict]:
    """Lightweight QA against content_rules.json. Returns list of issues."""
    rules = get_rules()
    issues: List[dict] = []
    title = (content.get("title") or "").strip()
    cap_t = rules.get("title", {}).get("max_length", 120)
    if not title:
        issues.append({"field": "title", "severity": "error", "msg": "Title is empty."})
    if len(title) > cap_t:
        issues.append({"field": "title", "severity": "error",
                       "msg": f"Title exceeds {cap_t} chars ({len(title)})."})
    if is_garbage_value(title):
        issues.append({"field": "title", "severity": "error",
                       "msg": "Title is the style number or contains parse artefacts."})
    bullets = content.get("bullets") or []
    cap_b = rules.get("bullets", {}).get("max_length_each", 256)
    for i, b in enumerate(bullets, 1):
        if not b:
            issues.append({"field": f"bullet_{i}", "severity": "warning",
                           "msg": f"Bullet {i} is empty."})
            continue
        if len(b) > cap_b:
            issues.append({"field": f"bullet_{i}", "severity": "error",
                           "msg": f"Bullet {i} exceeds {cap_b} chars ({len(b)})."})
        if " — " not in b and " - " not in b:
            issues.append({"field": f"bullet_{i}", "severity": "warning",
                           "msg": f"Bullet {i} missing ALL-CAPS headline + em-dash format."})
    desc = (content.get("description") or "")
    cap_d = rules.get("description", {}).get("max_length", 2000)
    if len(desc) > cap_d:
        issues.append({"field": "description", "severity": "error",
                       "msg": f"Description exceeds {cap_d} chars ({len(desc)})."})
    bk = (content.get("backend_keywords") or "").encode("utf-8")
    cap_k = rules.get("backend_keywords", {}).get("max_bytes", 249)
    if len(bk) > cap_k:
        issues.append({"field": "backend_keywords", "severity": "error",
                       "msg": f"Backend keywords exceed {cap_k} bytes ({len(bk)})."})
    return issues
