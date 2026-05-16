"""Atlas decision logger \u2014 v2 (Postgres with JSONL fallback).

The logger is the single write path into the substrate. Application code does
NOT write to the substrate directly; it calls log_field_decision() /
log_judgment_moment() / open_session() / submit_session() and the logger
handles validation, routing, and durability.

Two storage backends:

  Postgres (production):  ATLAS_DATABASE_URL or DATABASE_URL is set and the
                          connection pool comes up healthy. All writes go
                          to substrate_events / substrate_sessions tables.

  JSONL (tests, fallback): No DATABASE_URL or pool unavailable. Writes go
                           to decision_log/<workspace>/<YYYY-MM>.jsonl and
                           sessions/<workspace>/<session_id>.json on disk.

The two backends are wire-compatible: the same row shapes, the same field
semantics, the same v1.1.1 event_kind discriminator. Tests run against
JSONL because tmp-dir isolation is cheap; production runs against Postgres
because Render's filesystem is ephemeral.

Filter rule (locked at v1.0.0):
    A field generation is logged as a decision_event if ANY of:
      (a) operator interacts with the field
      (b) field is one of the 41 strategic decisions
      (c) overall_confidence < 0.7
      (d) two or more competing rules fired (real choice happened)
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from substrate.db import get_pool, is_postgres_active
from substrate.schema import (
    DecisionEvent,
    JudgmentMomentEvent,
    Module,
    OperatorAction,
    OperatorScope,
    SessionObject,
    TriggerType,
    validate_decision_event,
    validate_judgment_moment,
    validate_session_object,
)

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------


def _use_postgres() -> bool:
    """Return True if we have a healthy Postgres pool, False otherwise.

    Calls get_pool() once so the first invocation also triggers init.
    Subsequent invocations are essentially free (already-cached pool).
    """
    return get_pool() is not None


# ---------------------------------------------------------------------------
# JSONL fallback storage (unchanged from v1)
# ---------------------------------------------------------------------------

_DEFAULT_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "decision_log")
_SESSIONS_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sessions")

_WRITE_LOCK = threading.Lock()


def _root() -> str:
    return os.environ.get("ATLAS_SUBSTRATE_ROOT", _DEFAULT_ROOT)


def _sessions_root() -> str:
    return os.environ.get("ATLAS_SESSIONS_ROOT", _SESSIONS_ROOT)


def _month_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _decision_log_path(workspace_id: str) -> str:
    d = os.path.join(_root(), workspace_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_month_stamp()}.jsonl")


def _session_log_path(workspace_id: str) -> str:
    d = os.path.join(_root(), workspace_id, "sessions")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{_month_stamp()}.jsonl")


def _session_meta_path(workspace_id: str, session_id: str) -> str:
    d = os.path.join(_sessions_root(), workspace_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{session_id}.json")


def _append_jsonl(path: str, payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------


def _pg_insert_event(payload: dict[str, Any]) -> None:
    """Insert one event row into substrate_events.

    Maps the validated v1.1.1 payload into the columnar table. Columns
    that don't apply to the given event_kind are left NULL.
    """
    pool = get_pool()
    if pool is None:
        # Pool died between selection and insert. Caller should have checked
        # _use_postgres() first \u2014 if we got here, fall back to JSONL.
        raise RuntimeError("Postgres pool unavailable mid-write")

    kind = payload.get("event_kind")
    rules = payload.get("rules_injected")
    atlas_output = payload.get("atlas_output")
    operator_value = payload.get("operator_value")

    sql = """
        INSERT INTO substrate_events (
            event_kind, event_id, workspace_id, session_id, timestamp,
            module, field_name, rules_injected, brand_profile_version,
            atlas_output, overall_confidence, private_scope, contributable_scope,
            links_to_event_id, operator_action, operator_value, operator_scope,
            operator_time_to_decision_ms, operator_comment, operator_viewed_case,
            decision_event_id, trigger_type, surfaced_at,
            operator_id, started_at, ended_at, exemplar,
            meta, pre_change_snapshot
        ) VALUES (
            %(event_kind)s, %(event_id)s, %(workspace_id)s, %(session_id)s, %(timestamp)s,
            %(module)s, %(field_name)s, %(rules_injected)s::jsonb, %(brand_profile_version)s,
            %(atlas_output)s::jsonb, %(overall_confidence)s, %(private_scope)s, %(contributable_scope)s,
            %(links_to_event_id)s, %(operator_action)s, %(operator_value)s::jsonb, %(operator_scope)s,
            %(operator_time_to_decision_ms)s, %(operator_comment)s, %(operator_viewed_case)s,
            %(decision_event_id)s, %(trigger_type)s, %(surfaced_at)s,
            %(operator_id)s, %(started_at)s, %(ended_at)s, %(exemplar)s,
            %(meta)s::jsonb, %(pre_change_snapshot)s::jsonb
        )
        ON CONFLICT (event_id, event_kind) DO NOTHING
    """
    # Generate a synthetic event_id for kinds that don't carry one (session_*).
    event_id = payload.get("event_id") or payload.get("moment_id")
    if not event_id:
        import uuid
        event_id = str(uuid.uuid4())

    params = {
        "event_kind": kind,
        "event_id": event_id,
        "workspace_id": payload.get("workspace_id"),
        "session_id": payload.get("session_id"),
        "timestamp": payload.get("timestamp") or payload.get("surfaced_at") or payload.get("started_at"),
        "module": payload.get("module"),
        "field_name": payload.get("field_name"),
        "rules_injected": json.dumps(rules) if rules is not None else None,
        "brand_profile_version": payload.get("brand_profile_version"),
        "atlas_output": json.dumps(atlas_output, default=str) if atlas_output is not None else None,
        "overall_confidence": payload.get("overall_confidence"),
        "private_scope": payload.get("private_scope"),
        "contributable_scope": payload.get("contributable_scope"),
        "links_to_event_id": payload.get("links_to_event_id"),
        "operator_action": payload.get("operator_action"),
        "operator_value": json.dumps(operator_value, default=str) if operator_value is not None else None,
        "operator_scope": payload.get("operator_scope"),
        "operator_time_to_decision_ms": payload.get("operator_time_to_decision_ms"),
        "operator_comment": payload.get("operator_comment"),
        "operator_viewed_case": payload.get("operator_viewed_case"),
        "decision_event_id": payload.get("decision_event_id"),
        "trigger_type": payload.get("trigger_type"),
        "surfaced_at": payload.get("surfaced_at"),
        "operator_id": payload.get("operator_id"),
        "started_at": payload.get("started_at"),
        "ended_at": payload.get("ended_at"),
        "exemplar": payload.get("exemplar"),
        "meta": json.dumps(payload.get("_meta") or {}, default=str),
        "pre_change_snapshot": json.dumps(
            payload.get("pre_change_snapshot") or {}, default=str
        ),
    }
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _pg_upsert_session(s: SessionObject) -> None:
    pool = get_pool()
    if pool is None:
        raise RuntimeError("Postgres pool unavailable mid-write")
    sql = """
        INSERT INTO substrate_sessions (
            session_id, workspace_id, operator_id, module,
            started_at, ended_at, state, operator_notes, exemplar
        ) VALUES (
            %(session_id)s, %(workspace_id)s, %(operator_id)s, %(module)s,
            %(started_at)s, %(ended_at)s, %(state)s, %(operator_notes)s, %(exemplar)s
        )
        ON CONFLICT (session_id) DO UPDATE SET
            ended_at = EXCLUDED.ended_at,
            state = EXCLUDED.state,
            operator_notes = EXCLUDED.operator_notes,
            exemplar = EXCLUDED.exemplar
    """
    params = {
        "session_id": s.session_id,
        "workspace_id": s.workspace_id,
        "operator_id": s.operator_id,
        "module": s.module.value if s.module else None,
        "started_at": s.started_at,
        "ended_at": s.ended_at,
        "state": s.state,
        "operator_notes": s.operator_notes,
        "exemplar": s.exemplar,
    }
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()


def _pg_read_session(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    if pool is None:
        return None
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT session_id, workspace_id, operator_id, module,
                       started_at, ended_at, state, operator_notes, exemplar
                FROM substrate_sessions
                WHERE workspace_id = %s AND session_id = %s
                """,
                (workspace_id, session_id),
            )
            row = cur.fetchone()
    if row is None:
        return None
    return {
        "session_id": row[0],
        "workspace_id": row[1],
        "operator_id": row[2],
        "module": row[3],
        "started_at": row[4].isoformat() if row[4] else None,
        "ended_at": row[5].isoformat() if row[5] else None,
        "state": row[6],
        "operator_notes": row[7],
        "exemplar": row[8],
    }


def _pg_stream_decisions(workspace_id: str, month: Optional[str] = None) -> Iterator[dict[str, Any]]:
    """Yield substrate_events rows for a workspace in chronological order.

    The shape of each yielded dict matches what the JSONL reader produces,
    so callers (judgment.py, the future Memory tab) don't need to branch.
    """
    pool = get_pool()
    if pool is None:
        return
    # Month filter: events whose timestamp falls inside the YYYY-MM window.
    sql = """
        SELECT event_kind, event_id, workspace_id, session_id, timestamp,
               module, field_name, rules_injected, brand_profile_version,
               atlas_output, overall_confidence, private_scope, contributable_scope,
               links_to_event_id, operator_action, operator_value, operator_scope,
               operator_time_to_decision_ms, operator_comment, operator_viewed_case,
               decision_event_id, trigger_type, surfaced_at,
               operator_id, started_at, ended_at, exemplar,
               meta, pre_change_snapshot
        FROM substrate_events
        WHERE workspace_id = %s
    """
    params: list[Any] = [workspace_id]
    if month:
        sql += " AND to_char(timestamp, 'YYYY-MM') = %s"
        params.append(month)
    sql += " ORDER BY timestamp ASC, event_id ASC"

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            for row in cur:
                d = dict(zip(cols, row))
                # Normalise types so consumers see the same shape as JSONL:
                # - timestamps as ISO strings
                # - JSONB cols as their parsed objects (psycopg already does this)
                for k in ("timestamp", "surfaced_at", "started_at", "ended_at"):
                    if d.get(k) is not None:
                        d[k] = d[k].isoformat() if hasattr(d[k], "isoformat") else d[k]
                # UUIDs come back as uuid.UUID; stringify for consumers.
                for k in ("event_id", "links_to_event_id", "decision_event_id"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                # Provide moment_id alias for judgment_moment rows so callers
                # written for the old shape still work.
                if d.get("event_kind") == "judgment_moment_event":
                    d["moment_id"] = d["event_id"]
                # The DB column is `meta` but JSONL writers store it as
                # `_meta`. Expose `_meta` so consumers see one shape.
                meta = d.pop("meta", None)
                if meta:
                    d["_meta"] = meta
                # pre_change_snapshot is empty-dict for most events;
                # surface it as None when empty so callers can use
                # truthiness checks naturally.
                snap = d.get("pre_change_snapshot")
                if snap == {} or snap is None:
                    d["pre_change_snapshot"] = None
                yield d


# ---------------------------------------------------------------------------
# Filter rule \u2014 what counts as a logged decision (unchanged)
# ---------------------------------------------------------------------------

STRATEGIC_FIELDS: set[str] = {
    "marketplace", "brand_registry", "product_type", "category",
    "gtin_strategy", "item_type_keyword", "sku_convention",
    "parent_child_structure", "variation_theme",
    "brand_name", "manufacturer", "country_of_origin", "model_name",
    "unit_count", "merchant_suggested_asin",
    "item_name", "title_length_target", "bullet_structure",
    "bullet_1", "bullet_2", "bullet_3", "bullet_4", "bullet_5",
    "description", "backend_keywords", "main_image", "secondary_images",
    "a_plus_content", "brand_story",
    "ghs_classification", "hazmat", "battery_handling", "age_range",
    "target_gender", "prop_65", "sds_url", "material_regulations",
    "default_child", "swatch_images", "price_strategy",
    "fulfillment_channel", "shipping_template", "max_order_quantity",
    "restock_policy",
    # Variations module (parent/child reconciliation). Every parentage
    # correction is structurally high-stakes — reassigning an ASIN's
    # parent can suppress it from search for hours and changes its
    # variation-theme display permanently. Always logged.
    "parentage_correction",
}

CONFIDENCE_LOG_THRESHOLD = 0.7


def should_log_field(
    field_name: str,
    overall_confidence: Optional[float],
    rules_injected: list[dict[str, Any]],
    operator_acted: bool = False,
) -> bool:
    if operator_acted:
        return True
    if field_name in STRATEGIC_FIELDS:
        return True
    if overall_confidence is not None and overall_confidence < CONFIDENCE_LOG_THRESHOLD:
        return True
    if rules_injected and len(rules_injected) >= 2:
        return True
    return False


# ---------------------------------------------------------------------------
# Public write API \u2014 unchanged signatures, dual-backend internals.
# ---------------------------------------------------------------------------


def _write_event(workspace_id: str, payload: dict[str, Any]) -> None:
    """Route a payload to Postgres if available, otherwise JSONL.

    On Postgres failure mid-write, fall through to JSONL so we never lose
    the event. Logs a warning but does not raise.
    """
    if _use_postgres():
        try:
            _pg_insert_event(payload)
            return
        except Exception as exc:
            print(f"[substrate] pg insert failed, falling back to JSONL: {exc}", flush=True)
    _append_jsonl(_decision_log_path(workspace_id), payload)


def log_field_decision(
    workspace_id: str,
    session_id: Optional[str],
    module: Module,
    field_name: str,
    atlas_output: Any,
    overall_confidence: Optional[float],
    rules_injected: list[dict[str, Any]],
    brand_profile_version: str,
    private_scope: bool = True,
    contributable_scope: bool = False,
    decision_number: Optional[int] = None,
    style_id: Optional[str] = None,
    asin: Optional[str] = None,
    pre_change_snapshot: Optional[dict[str, Any]] = None,
    enforce_filter: bool = True,
) -> Optional[str]:
    """Log a generation-time decision_event.

    Phase 1 additions:
      asin                  — the ASIN this decision targets, used to
                              auto-build the snapshot if not supplied.
      pre_change_snapshot   — explicit before-state dict. If omitted and
                              an asin is supplied, the logger calls
                              build_snapshot_for_asin() to capture
                              whatever fresh outcome data we have.

    The snapshot is the architecturally-irreversible piece. Once a
    decision is in the log without one, we can't reconstruct it later.
    Capture is best-effort: a snapshot failure logs a warning but never
    blocks the decision write.
    """
    if enforce_filter and not should_log_field(
        field_name=field_name,
        overall_confidence=overall_confidence,
        rules_injected=rules_injected,
        operator_acted=False,
    ):
        return None

    ev = DecisionEvent(
        workspace_id=workspace_id,
        session_id=session_id,
        module=module,
        field_name=field_name,
        rules_injected=rules_injected,
        brand_profile_version=brand_profile_version,
        atlas_output=atlas_output,
        overall_confidence=overall_confidence,
        private_scope=private_scope,
        contributable_scope=contributable_scope,
    )
    payload = ev.to_dict()

    try:
        validate_decision_event(payload)
    except Exception as exc:
        print(f"[substrate] validation failed for {field_name}: {exc}", flush=True)
        return None

    if style_id or decision_number is not None or asin is not None:
        payload["_meta"] = {}
        if style_id:
            payload["_meta"]["style_id"] = style_id
        if decision_number is not None:
            payload["_meta"]["decision_number"] = decision_number
        if asin:
            payload["_meta"]["asin"] = asin

    # Pre-change snapshot. Caller-supplied wins; otherwise derive from ASIN.
    if pre_change_snapshot is not None:
        payload["pre_change_snapshot"] = pre_change_snapshot
    elif asin:
        try:
            from substrate.snapshot import build_snapshot_for_asin
            payload["pre_change_snapshot"] = build_snapshot_for_asin(
                workspace_id=workspace_id,
                asin=asin,
            )
        except Exception as exc:
            print(f"[substrate] snapshot build skipped for {asin}: {exc}", flush=True)

    _write_event(workspace_id, payload)
    return ev.event_id


def update_field_decision_with_operator_response(
    workspace_id: str,
    event_id: str,
    operator_action: OperatorAction,
    operator_value: Any = None,
    operator_scope: OperatorScope = OperatorScope.NONE,
    operator_time_to_decision_ms: Optional[int] = None,
    operator_comment: Optional[str] = None,
    operator_viewed_case: bool = False,
) -> None:
    payload = {
        "event_kind": "operator_response",
        "links_to_event_id": event_id,
        "workspace_id": workspace_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operator_action": operator_action.value,
        "operator_value": operator_value,
        "operator_scope": operator_scope.value,
        "operator_time_to_decision_ms": operator_time_to_decision_ms,
        "operator_comment": operator_comment,
        "operator_viewed_case": operator_viewed_case,
    }
    _write_event(workspace_id, payload)


def log_judgment_moment(
    workspace_id: str,
    decision_event_id: str,
    trigger_type: TriggerType,
    session_id: Optional[str] = None,
) -> str:
    m = JudgmentMomentEvent(
        workspace_id=workspace_id,
        decision_event_id=decision_event_id,
        session_id=session_id,
        trigger_type=trigger_type,
    )
    payload = m.to_dict()
    validate_judgment_moment(payload)
    # judgment_moment_event uses moment_id, not event_id; the pg layer
    # promotes moment_id into event_id transparently.
    _write_event(workspace_id, payload)
    return m.moment_id


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def open_session(
    workspace_id: str,
    operator_id: str,
    module: Module = Module.NIS,
) -> SessionObject:
    s = SessionObject(
        workspace_id=workspace_id,
        operator_id=operator_id,
        module=module,
    )
    write_session(s)
    # Emit a session_started event so timelines reconstruct cleanly.
    _write_event(workspace_id, {
        "event_kind": "session_started",
        "session_id": s.session_id,
        "workspace_id": workspace_id,
        "operator_id": operator_id,
        "module": module.value,
        "started_at": s.started_at,
        "timestamp": s.started_at,
    })
    return s


def write_session(s: SessionObject) -> None:
    payload = s.to_dict()
    validate_session_object(payload)
    if _use_postgres():
        try:
            _pg_upsert_session(s)
            return
        except Exception as exc:
            print(f"[substrate] pg session upsert failed, falling back: {exc}", flush=True)
    # JSONL fallback
    path = _session_meta_path(s.workspace_id, s.session_id)
    with _WRITE_LOCK:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def submit_session(
    s: SessionObject,
    operator_notes: Optional[str] = None,
    exemplar: bool = False,
) -> None:
    s.ended_at = datetime.now(timezone.utc).isoformat()
    s.state = "submitted"
    if operator_notes is not None:
        s.operator_notes = operator_notes
    s.exemplar = exemplar
    write_session(s)
    _write_event(s.workspace_id, {
        "event_kind": "session_completed",
        "session_id": s.session_id,
        "workspace_id": s.workspace_id,
        "ended_at": s.ended_at,
        "exemplar": exemplar,
        "timestamp": s.ended_at,
    })


# ---------------------------------------------------------------------------
# Read helpers (used by judgment detection + future Memory tab)
# ---------------------------------------------------------------------------


def read_session(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    if _use_postgres():
        result = _pg_read_session(workspace_id, session_id)
        if result is not None:
            return result
        # Fall through to JSONL only if pg returned nothing (legacy session)
    path = _session_meta_path(workspace_id, session_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def stream_decisions(workspace_id: str, month: Optional[str] = None) -> Iterator[dict[str, Any]]:
    if _use_postgres():
        yield from _pg_stream_decisions(workspace_id, month)
        return
    # JSONL fallback
    month = month or _month_stamp()
    path = os.path.join(_root(), workspace_id, f"{month}.jsonl")
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


__all__ = [
    "STRATEGIC_FIELDS",
    "CONFIDENCE_LOG_THRESHOLD",
    "should_log_field",
    "log_field_decision",
    "update_field_decision_with_operator_response",
    "log_judgment_moment",
    "open_session",
    "write_session",
    "submit_session",
    "read_session",
    "stream_decisions",
]
