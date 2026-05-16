"""Atlas Unit Economics — operator-supplied cost inputs.

Wraps the `cost_inputs` and `brand_overhead` tables with read/write
helpers used by the Unit Economics UI and margin rollup. Every save
also writes a decision_event with module='unit_economics' so the cost
history lands in Memory.

Design decisions (locked in UNIT_ECONOMICS.md):

  - Cost allocation Model 1: variable costs are per-ASIN; fixed overhead
    lives at the brand level and NEVER gets pushed into per-unit margin.
  - Costs are operator-supplied. Atlas does NOT infer. Empty fields are
    nullable, not zero. A missing landed_cost displays as "not on file",
    never as "$0".
  - Per-ASIN granularity belongs in its own table, not in
    `brand_profile.custom`. This module is the canonical store.
  - referral_pct defaults to 0.15 (15%) because Amazon's standard fee
    covers most categories; the operator can override.

Never raises. Best-effort, like every other substrate write.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.cost_inputs")

# Default Amazon referral fee. Most categories sit at 15%. Apparel is 17%,
# Electronics 8%, etc. — operator override exists, this is just the seed.
_DEFAULT_REFERRAL_PCT = 0.15


# Numeric fields stored on cost_inputs. Used by save_costs(), read_costs(),
# and the audit-trail payload.
_COST_FIELDS = (
    "landed_cost",
    "fba_fee",
    "third_pl_fee",
    "referral_pct",
    "map_price",
)


def _coerce_num(raw: Any) -> Optional[float]:
    """Tolerant parse of an operator-entered cost cell.

    Strips $, commas, %. Empty string -> None (which means "not on file").
    Operator-typed zero stays zero (a real-zero cost is legal: e.g. a
    digital product with no landed_cost).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Per-ASIN cost inputs
# ---------------------------------------------------------------------------


def read_cost_input(workspace_id: str, asin: str) -> dict[str, Any]:
    """Return the cost row for one ASIN, or a well-formed empty payload.

    Empty payload still carries the workspace_id + asin so the editor UI
    can render the form without a null guard.
    """
    empty = {
        "ok": True,
        "workspace_id": workspace_id,
        "asin": asin,
        "landed_cost": None,
        "fba_fee": None,
        "third_pl_fee": None,
        "referral_pct": None,
        "map_price": None,
        "notes": "",
        "revision": 0,
        "set_at": None,
        "set_by": None,
    }
    pool = get_pool()
    if pool is None:
        return empty
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT landed_cost, fba_fee, third_pl_fee, referral_pct,
                           map_price, notes, revision, set_at, set_by
                    FROM cost_inputs
                    WHERE workspace_id = %s AND asin = %s
                    """,
                    (workspace_id, asin),
                )
                row = cur.fetchone()
        if row is None:
            return empty
        landed_cost, fba_fee, third_pl_fee, referral_pct, map_price, notes, revision, set_at, set_by = row
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "asin": asin,
            "landed_cost": float(landed_cost) if landed_cost is not None else None,
            "fba_fee":     float(fba_fee)     if fba_fee     is not None else None,
            "third_pl_fee": float(third_pl_fee) if third_pl_fee is not None else None,
            "referral_pct": float(referral_pct) if referral_pct is not None else None,
            "map_price":   float(map_price)   if map_price   is not None else None,
            "notes": notes or "",
            "revision": int(revision),
            "set_at": set_at.isoformat() if hasattr(set_at, "isoformat") else set_at,
            "set_by": set_by,
        }
    except Exception as exc:
        logger.warning("cost_inputs read failed: %s", exc)
        return empty


def list_cost_inputs(workspace_id: str) -> list[dict[str, Any]]:
    """Return all cost rows for a workspace, ordered by asin."""
    pool = get_pool()
    if pool is None:
        return []
    rows: list[dict[str, Any]] = []
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT asin, landed_cost, fba_fee, third_pl_fee, referral_pct,
                           map_price, notes, revision, set_at, set_by
                    FROM cost_inputs
                    WHERE workspace_id = %s
                    ORDER BY asin ASC
                    """,
                    (workspace_id,),
                )
                for r in cur.fetchall():
                    asin, lc, fba, tpl, ref, mp, notes, revision, set_at, set_by = r
                    rows.append({
                        "asin": asin,
                        "landed_cost": float(lc) if lc is not None else None,
                        "fba_fee":     float(fba) if fba is not None else None,
                        "third_pl_fee": float(tpl) if tpl is not None else None,
                        "referral_pct": float(ref) if ref is not None else None,
                        "map_price":   float(mp) if mp is not None else None,
                        "notes": notes or "",
                        "revision": int(revision),
                        "set_at": set_at.isoformat() if hasattr(set_at, "isoformat") else set_at,
                        "set_by": set_by,
                    })
    except Exception as exc:
        logger.warning("cost_inputs list failed: %s", exc)
    return rows


def save_cost_input(
    workspace_id: str,
    asin: str,
    payload: dict[str, Any],
    *,
    operator_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert a per-ASIN cost row and write the decision_event.

    Behavior:
      - All numeric fields tolerant-parsed (empty string -> None).
      - Missing payload keys preserve existing values (partial updates ok).
      - Revision bumps by 1 on every save (audit-friendly).
      - Writes decision_event with module='unit_economics',
        field_name='cost_input'.

    Returns:
      { ok, workspace_id, asin, revision, event_id, error? }
    """
    if not workspace_id or not asin:
        return {"ok": False, "error": "workspace_id and asin required"}

    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}

    prev = read_cost_input(workspace_id, asin)

    def merged(field: str) -> Optional[float]:
        if field in payload:
            return _coerce_num(payload[field])
        return prev.get(field)

    landed = merged("landed_cost")
    fba    = merged("fba_fee")
    tpl    = merged("third_pl_fee")
    ref    = merged("referral_pct")
    mp     = merged("map_price")
    notes  = payload.get("notes", prev.get("notes", "")) or ""
    new_rev = int(prev.get("revision") or 0) + 1

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO cost_inputs (
                        workspace_id, asin, landed_cost, fba_fee, third_pl_fee,
                        referral_pct, map_price, notes, revision, set_by, meta
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s::jsonb
                    )
                    ON CONFLICT (workspace_id, asin) DO UPDATE SET
                        landed_cost  = EXCLUDED.landed_cost,
                        fba_fee      = EXCLUDED.fba_fee,
                        third_pl_fee = EXCLUDED.third_pl_fee,
                        referral_pct = EXCLUDED.referral_pct,
                        map_price    = EXCLUDED.map_price,
                        notes        = EXCLUDED.notes,
                        revision     = EXCLUDED.revision,
                        set_at       = NOW(),
                        set_by       = EXCLUDED.set_by,
                        meta         = EXCLUDED.meta
                    """,
                    (
                        workspace_id, asin, landed, fba, tpl,
                        ref, mp, notes, new_rev, operator_id,
                        json.dumps({"module": "unit_economics"}),
                    ),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("cost_inputs save failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    # Audit trail. Best-effort.
    event_id = None
    try:
        from .logger import log_field_decision
        from .schema import Module
        from .brand_voice import read_voice
        try:
            profile_version = (read_voice(workspace_id) or {}).get("profile_version") or f"{workspace_id}_legacy"
        except Exception:
            profile_version = f"{workspace_id}_legacy"
        event_id = log_field_decision(
            workspace_id=workspace_id,
            session_id=None,
            module=Module.UNIT_ECONOMICS,
            field_name="cost_input",
            asin=asin,
            atlas_output={
                "asin": asin,
                "landed_cost": landed,
                "fba_fee": fba,
                "third_pl_fee": tpl,
                "referral_pct": ref,
                "map_price": mp,
                "notes": notes,
                "revision": new_rev,
            },
            overall_confidence=1.0,
            rules_injected=[],
            brand_profile_version=profile_version,
            enforce_filter=False,
        )
    except Exception as exc:
        logger.warning("cost_input decision_event write skipped: %s", exc)

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "asin": asin,
        "revision": new_rev,
        "event_id": event_id,
    }


# ---------------------------------------------------------------------------
# Brand-level fixed overhead
# ---------------------------------------------------------------------------


def read_overhead(workspace_id: str) -> dict[str, Any]:
    """Return the brand-level overhead row, or empty payload."""
    empty = {
        "ok": True,
        "workspace_id": workspace_id,
        "fixed_overhead_monthly": None,
        "notes": "",
        "revision": 0,
        "set_at": None,
        "set_by": None,
    }
    pool = get_pool()
    if pool is None:
        return empty
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT fixed_overhead_monthly, notes, revision, set_at, set_by
                    FROM brand_overhead WHERE workspace_id = %s
                    """,
                    (workspace_id,),
                )
                row = cur.fetchone()
        if row is None:
            return empty
        fom, notes, revision, set_at, set_by = row
        return {
            "ok": True,
            "workspace_id": workspace_id,
            "fixed_overhead_monthly": float(fom) if fom is not None else None,
            "notes": notes or "",
            "revision": int(revision),
            "set_at": set_at.isoformat() if hasattr(set_at, "isoformat") else set_at,
            "set_by": set_by,
        }
    except Exception as exc:
        logger.warning("brand_overhead read failed: %s", exc)
        return empty


def save_overhead(
    workspace_id: str,
    payload: dict[str, Any],
    *,
    operator_id: Optional[str] = None,
) -> dict[str, Any]:
    """Upsert brand-level overhead + decision_event."""
    if not workspace_id:
        return {"ok": False, "error": "workspace_id required"}

    pool = get_pool()
    if pool is None:
        return {"ok": False, "error": "substrate unavailable"}

    prev = read_overhead(workspace_id)
    fom = _coerce_num(payload.get("fixed_overhead_monthly")) \
        if "fixed_overhead_monthly" in payload else prev.get("fixed_overhead_monthly")
    notes = payload.get("notes", prev.get("notes", "")) or ""
    new_rev = int(prev.get("revision") or 0) + 1

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO brand_overhead (
                        workspace_id, fixed_overhead_monthly, notes,
                        revision, set_by, meta
                    ) VALUES (%s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (workspace_id) DO UPDATE SET
                        fixed_overhead_monthly = EXCLUDED.fixed_overhead_monthly,
                        notes                  = EXCLUDED.notes,
                        revision               = EXCLUDED.revision,
                        set_at                 = NOW(),
                        set_by                 = EXCLUDED.set_by,
                        meta                   = EXCLUDED.meta
                    """,
                    (workspace_id, fom, notes, new_rev, operator_id,
                     json.dumps({"module": "unit_economics"})),
                )
            conn.commit()
    except Exception as exc:
        logger.warning("brand_overhead save failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200]}

    event_id = None
    try:
        from .logger import log_field_decision
        from .schema import Module
        from .brand_voice import read_voice
        try:
            profile_version = (read_voice(workspace_id) or {}).get("profile_version") or f"{workspace_id}_legacy"
        except Exception:
            profile_version = f"{workspace_id}_legacy"
        event_id = log_field_decision(
            workspace_id=workspace_id,
            session_id=None,
            module=Module.UNIT_ECONOMICS,
            field_name="fixed_overhead_monthly",
            atlas_output={
                "fixed_overhead_monthly": fom,
                "notes": notes,
                "revision": new_rev,
            },
            overall_confidence=1.0,
            rules_injected=[],
            brand_profile_version=profile_version,
            enforce_filter=False,
        )
    except Exception as exc:
        logger.warning("brand_overhead decision_event write skipped: %s", exc)

    return {
        "ok": True,
        "workspace_id": workspace_id,
        "revision": new_rev,
        "event_id": event_id,
    }


__all__ = [
    "read_cost_input", "list_cost_inputs", "save_cost_input",
    "read_overhead", "save_overhead",
    "_DEFAULT_REFERRAL_PCT", "_COST_FIELDS",
]
