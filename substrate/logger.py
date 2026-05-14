"""Atlas decision logger — writes decision_events to JSONL on disk.

The logger is the single write path into the substrate. Application code does not
write to the substrate directly; it calls log_field_decision() or log_session_event()
and the logger handles schema validation, file routing, and append safety.

Storage layout:
    decision_log/<workspace_id>/<YYYY-MM>.jsonl       — decision_event records
    decision_log/<workspace_id>/sessions/<YYYY-MM>.jsonl — session_event records
    sessions/<workspace_id>/<session_id>.json          — session_object metadata

When we eventually move to Postgres, only this module's internals change. Every
caller of log_*() remains untouched.

Filter rule (locked at v1.0.0):
    A field generation is logged as a decision_event if ANY of:
      (a) operator interacts with the field (edit, reject, comment, view-with-action)
      (b) field is one of the 41 strategic decisions
      (c) overall_confidence < 0.7
      (d) two or more competing rules fired (real choice happened)

    Otherwise the generation is counted into session-level aggregates but no per-field
    event is written. This keeps the log signal-rich and queryable.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

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

# Storage root — relative to nis-wizard-server. Override via ATLAS_SUBSTRATE_ROOT.
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
    """Append a single JSON line, holding the write lock so concurrent generations
    do not interleave bytes."""
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with _WRITE_LOCK:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


# ---------------------------------------------------------------------------
# Filter rule — what counts as a logged decision
# ---------------------------------------------------------------------------

# The 41 strategic decision field names. When generation produces any of these,
# the event is always logged regardless of confidence or rule density.
STRATEGIC_FIELDS: set[str] = {
    # Setup
    "marketplace",
    "brand_registry",
    "product_type",
    "category",
    "gtin_strategy",
    "item_type_keyword",
    "sku_convention",
    "parent_child_structure",
    "variation_theme",
    # Identity
    "brand_name",
    "manufacturer",
    "country_of_origin",
    "model_name",
    "unit_count",
    "merchant_suggested_asin",
    # Content strategy
    "item_name",  # title
    "title_length_target",
    "bullet_structure",
    "bullet_1",
    "bullet_2",
    "bullet_3",
    "bullet_4",
    "bullet_5",
    "description",
    "backend_keywords",
    "main_image",
    "secondary_images",
    "a_plus_content",
    "brand_story",
    # Compliance
    "ghs_classification",
    "hazmat",
    "battery_handling",
    "age_range",
    "target_gender",
    "prop_65",
    "sds_url",
    "material_regulations",
    # Variation & ops
    "default_child",
    "swatch_images",
    "price_strategy",
    "fulfillment_channel",
    "shipping_template",
    "max_order_quantity",
    "restock_policy",
}

CONFIDENCE_LOG_THRESHOLD = 0.7


def should_log_field(
    field_name: str,
    overall_confidence: Optional[float],
    rules_injected: list[dict[str, Any]],
    operator_acted: bool = False,
) -> bool:
    """Return True if a field decision meets the launch-locked filter rule.

    Per the filter rule:
        (a) operator interacted -> log
        (b) field is strategic -> log
        (c) confidence below threshold -> log
        (d) two or more rules fired -> log
    """
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
# Public write API
# ---------------------------------------------------------------------------


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
    enforce_filter: bool = True,
) -> Optional[str]:
    """Write a generation-time decision_event. Returns the event_id, or None if
    the filter rule excluded the event.

    Use enforce_filter=False only for explicit always-log writes (e.g., judgment
    moments tied to a specific decision).
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

    # Validate the v1.0.0 locked payload BEFORE attaching forward-compat metadata.
    # _meta is intentionally outside the validated schema so we can promote fields
    # in later versions without breaking existing readers or invalidating writes.
    try:
        validate_decision_event(payload)
    except Exception as exc:
        # Log validation failures to stderr but never block generation.
        print(f"[substrate] validation failed for {field_name}: {exc}", flush=True)
        return None

    if style_id or decision_number is not None:
        payload["_meta"] = {}
        if style_id:
            payload["_meta"]["style_id"] = style_id
        if decision_number is not None:
            payload["_meta"]["decision_number"] = decision_number

    _append_jsonl(_decision_log_path(workspace_id), payload)
    return ev.event_id


def update_field_decision_with_operator_response(
    workspace_id: str,
    event_id: str,
    operator_action: OperatorAction,
    operator_value: Any = None,
    operator_scope: OperatorScope = OperatorScope.NONE,
    operator_time_to_decision_ms: Optional[int] = None,
    operator_comment: Optional[str] = None,
) -> None:
    """Append an operator-response delta event linked to a prior decision_event.

    JSONL is append-only — to "update" a row we write a new event_kind='response'
    record that references the original event_id. Readers reconstruct the
    current state by replaying events in order. This preserves immutability
    while still letting operator responses attach.
    """
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
    }
    _append_jsonl(_decision_log_path(workspace_id), payload)


def log_judgment_moment(
    workspace_id: str,
    decision_event_id: str,
    trigger_type: TriggerType,
    session_id: Optional[str] = None,
) -> str:
    """Write a judgment_moment_event triggered by the detection layer.

    Returns the moment_id. Used later when the operator responds to the prompt
    (response gets appended as a delta event referencing this moment_id).
    """
    m = JudgmentMomentEvent(
        workspace_id=workspace_id,
        decision_event_id=decision_event_id,
        session_id=session_id,
        trigger_type=trigger_type,
    )
    payload = m.to_dict()
    validate_judgment_moment(payload)
    _append_jsonl(_decision_log_path(workspace_id), payload)
    return m.moment_id


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def open_session(
    workspace_id: str,
    operator_id: str,
    module: Module = Module.NIS,
) -> SessionObject:
    """Create and persist a new SessionObject. Returns the live object."""
    s = SessionObject(
        workspace_id=workspace_id,
        operator_id=operator_id,
        module=module,
    )
    write_session(s)
    _append_jsonl(
        _session_log_path(workspace_id),
        {
            "event_kind": "session_started",
            "session_id": s.session_id,
            "workspace_id": workspace_id,
            "operator_id": operator_id,
            "module": module.value,
            "started_at": s.started_at,
        },
    )
    return s


def write_session(s: SessionObject) -> None:
    """Persist (or overwrite) a session_object's metadata file."""
    payload = s.to_dict()
    validate_session_object(payload)
    path = _session_meta_path(s.workspace_id, s.session_id)
    with _WRITE_LOCK:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def submit_session(
    s: SessionObject,
    operator_notes: Optional[str] = None,
    exemplar: bool = False,
) -> None:
    """Mark a session as submitted. Writes a session_completed event and
    rewrites the session metadata file with the final state."""
    s.ended_at = datetime.now(timezone.utc).isoformat()
    s.state = "submitted"
    if operator_notes is not None:
        s.operator_notes = operator_notes
    s.exemplar = exemplar
    write_session(s)
    _append_jsonl(
        _session_log_path(s.workspace_id),
        {
            "event_kind": "session_completed",
            "session_id": s.session_id,
            "workspace_id": s.workspace_id,
            "ended_at": s.ended_at,
            "exemplar": exemplar,
        },
    )


# ---------------------------------------------------------------------------
# Convenience read helpers (used by future Memory tab and tests)
# ---------------------------------------------------------------------------


def read_session(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    path = _session_meta_path(workspace_id, session_id)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def stream_decisions(workspace_id: str, month: Optional[str] = None):
    """Yield decision events for a workspace in chronological append order.

    `month` defaults to the current YYYY-MM. Caller can iterate to filter or
    reconstruct state.
    """
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
