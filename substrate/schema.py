"""Atlas substrate schema — 20 foundational fields, locked.

Three tables:
    decision_event     — the atom. Every operator-touching decision Atlas makes.
    session_object     — wraps a batch of work (e.g., one NIS upload).
    judgment_moment_event — fires when Atlas's detection layer flags a decision worth asking about.

Field count:
    decision_event           : 15 core + 2 privacy = 17
    session_object           : 2
    judgment_moment_event    : 1
    Total locked at launch   : 20

Design principles:
    - Workspace-scoped from day one (workspace_id mandatory even when TLG is the only workspace).
    - Append-only by default. Operator-response fields are nullable and filled in after the fact.
    - Outcomes (suppression, conversion, A/B winner) attach retroactively when SP-API lands.
    - Privacy scope is computed at log time, not later. Cross-workspace aggregation is impossible
      without this discipline.
    - Time-to-decision is captured for every action because sub-second accept differs structurally
      from 30-second accept, and that signal is gone forever if we don't capture it now.

This file is the canonical contract. Anything that writes to or reads from the substrate must
import from here. Schema changes are versioned and append-only.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import uuid4

SCHEMA_VERSION = "1.1.0"


# ----------------------------------------------------------------------
# Enumerations
# ----------------------------------------------------------------------


class Module(str, Enum):
    """Which Atlas module produced this decision.

    NIS is live today. Others are placeholder values reserved so future modules
    can write to the same substrate without schema migration.
    """

    NIS = "nis"
    CATALOG_HEALTH = "catalog_health"
    EXPERIMENTS = "experiments"
    LEAK = "leak"
    OTHER = "other"


class OperatorAction(str, Enum):
    """What the operator did with Atlas's output.

    ACCEPT  — kept Atlas's value as-is (implicit positive signal).
    EDIT    — changed the value (strong signal; operator_value populated).
    VIEW    — opened the case but did not act (weak signal; tracks attention).
    REJECT  — explicitly marked the output as wrong without supplying a replacement.
    COMMENT — added a note without editing the value.
    """

    ACCEPT = "accept"
    EDIT = "edit"
    VIEW = "view"
    REJECT = "reject"
    COMMENT = "comment"


class OperatorScope(str, Enum):
    """How an edit should propagate.

    JUST_THIS     — one-time override; do not learn from it.
    BATCH         — apply across the current session only.
    BRAND_ALWAYS  — promote to a brand rule going forward.
    PROPOSE_RULE  — surface a candidate rule for confirmation later.
    NONE          — operator did not select a scope (default for non-edits).
    """

    JUST_THIS = "just_this"
    BATCH = "batch"
    BRAND_ALWAYS = "brand_always"
    PROPOSE_RULE = "propose_rule"
    NONE = "none"


class TriggerType(str, Enum):
    """Which judgment-detection signal fired.

    These map 1:1 to the six signals defined in the Judgment Detection Engine spec.
    Adding a new trigger only requires extending this enum, not changing the schema.
    """

    CONFIDENCE_MISMATCH = "confidence_mismatch"
    IN_SESSION_PATTERN = "in_session_pattern"
    BRAND_DRIFT = "brand_drift"
    RULE_OVERRIDE = "rule_override"
    LOW_CONFIDENCE = "low_confidence"
    PROPOSED_RULE_RESPONSE = "proposed_rule_response"


# ----------------------------------------------------------------------
# decision_event — the atom
# ----------------------------------------------------------------------


@dataclass
class DecisionEvent:
    """The atomic record of one decision Atlas made on one field for one operator.

    Every NIS generation, every operator action, every rule firing produces or
    updates one of these. This is the single most important data structure in Atlas.

    Field policy:
        - event_id, workspace_id, timestamp are write-once.
        - atlas_output and the context block (rules_injected, brand_profile_version,
          overall_confidence) are write-once at generation time.
        - operator_* fields are nullable at generation time and filled in when the
          operator acts. They are write-once-then-immutable after that.
        - The schema is intentionally narrow at v1.0.0 to enforce discipline; new
          fields are added by minor version bumps, never by mutating existing fields.
    """

    # ----- Identity and routing ----------------------------------------
    event_id: str = field(default_factory=lambda: str(uuid4()))
    """Unique identifier for this decision. UUID4. Write-once.

    This is effectively the primary key of the company. Every downstream system
    (rule library, brand profile, training pipeline, audit trail) references events
    by event_id. Never reuse, never mutate, never delete.
    """

    workspace_id: str = ""
    """The workspace this decision belongs to. MANDATORY at launch.

    Even though TLG is the only workspace today, this field MUST be populated.
    Adding workspace scoping retroactively when an agency or a second brand
    onboards would require migrating every existing event. Plan for multi-tenant
    from event #1.
    """

    session_id: Optional[str] = None
    """The session this decision was made in. Nullable for standalone decisions
    (e.g., a rule edited directly from the Rule Library outside any batch).

    For NIS generations, session_id is always populated.
    """

    module: Module = Module.NIS
    """Which Atlas module produced this decision.

    NIS is the only live module today, but the field exists so future Catalog
    Health / Experiments / Leak events route through the same substrate.
    """

    field_name: str = ""
    """The Amazon listing field this decision targets.

    Examples: 'item_name', 'bullet_1', 'main_image_locator', 'product_type'.
    Used for per-field analytics, rule scoping, and replay.
    """

    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """When Atlas generated this decision. ISO 8601 with timezone, UTC.

    Microsecond resolution. Order is critical for pattern detection across a
    session and for replaying a decision against the exact state of the world
    at the moment it was made.
    """

    # ----- Context block — what Atlas knew at generation time ----------
    rules_injected: list[dict[str, Any]] = field(default_factory=list)
    """The list of rules that fired into Atlas's generation prompt.

    Each entry: {"rule_id": "amazon.title.max_chars", "version": "2025.01",
                 "value": 200} or similar shape per rule class.

    Without this, no decision is replayable or debuggable. With it, every
    Atlas output can be traced back to the exact constraint set that produced it.
    """

    brand_profile_version: str = ""
    """The version of the brand profile active when this decision was generated.

    Points to a specific BrandProfile snapshot. Lets us replay any generation
    against the exact brand state at the time, even after the profile has
    evolved through 50+ subsequent versions.
    """

    # ----- Output block — what Atlas produced --------------------------
    atlas_output: Any = None
    """The value Atlas generated for this field.

    Can be a string (title, bullet), a dict (variation theme structure), a URL
    (image), or any JSON-serializable value. The training target.
    """

    overall_confidence: Optional[float] = None
    """Atlas's computed confidence in its own output. Range [0.0, 1.0].

    Critical: this is COMPUTED by Atlas (length checks, rule satisfaction,
    similarity to baseline), NOT asked from the underlying LLM. LLM
    self-reported confidence is unreliable and miscalibrated. Atlas's own
    computation is the only honest signal.

    Powers the confidence_mismatch detector: high confidence + operator
    override = high-signal training event.
    """

    # ----- Operator response — nullable until operator acts ------------
    operator_action: Optional[OperatorAction] = None
    """What the operator did with this output. Nullable until they act.

    Once set, write-once. The core training signal.
    """

    operator_value: Any = None
    """The value the operator supplied if they edited. None otherwise.

    For ACCEPT actions, this stays None (atlas_output is the operator-approved
    value). For EDIT actions, this is the supervised label Atlas trains against.
    """

    operator_scope: OperatorScope = OperatorScope.NONE
    """How the operator wants this edit to propagate.

    Determines whether the edit becomes a one-time exception, a session-wide
    pattern, a permanent brand rule, or a proposal for later review. The single
    most important UX field — it converts an edit into a typed feedback signal.
    """

    operator_time_to_decision_ms: Optional[int] = None
    """Milliseconds between Atlas showing the output and the operator acting.

    Sub-second accept ≠ 30-second accept. Capture this from day one because
    we cannot reconstruct attention quality retroactively. The single most
    underrated training signal in operator-AI products.
    """

    operator_comment: Optional[str] = None
    """Free-text reasoning the operator chose to provide. Always optional.

    When present, this is the highest-value training data Atlas captures. The
    operator's actual words describing why they made the decision they made.
    Never required, prominently easy to provide.
    """

    operator_viewed_case: bool = False
    """True if the operator opened the 'Why this' panel before acting.

    Added in v1.1.0. Distinguishes verified accepts (opened the case, read the
    reasoning, then approved) from reflex accepts (glanced and clicked).
    Same operator_action='accept' value, structurally different training signal.

    Cheap to capture (single boolean from the UI). Without it, every accept
    looks identical in the training data even though the underlying behavior
    is dramatically different.
    """

    # ----- Privacy and contribution ------------------------------------
    private_scope: bool = True
    """True if this event's content (especially atlas_output and operator_value)
    must never leave the workspace. Default True (safer).

    Set at log time based on content type. Free-text content is always private.
    Statistical patterns (edit frequencies, confidence distributions) can be
    contributable. Mis-classifying this once is a privacy incident.
    """

    contributable_scope: bool = False
    """True if anonymized derivative statistics from this event can flow into
    the cross-workspace base model. Default False (safer).

    The anonymization is structural: only aggregate patterns ever leave the
    workspace, never raw values. This flag gates whether the event participates
    in that aggregation at all. Set at log time.
    """

    # ----- Serialization ----------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for JSONL persistence."""
        d = asdict(self)
        # Enums serialize as their string values
        d["module"] = self.module.value if self.module else None
        d["operator_action"] = (
            self.operator_action.value if self.operator_action else None
        )
        d["operator_scope"] = self.operator_scope.value if self.operator_scope else None
        return d


# ----------------------------------------------------------------------
# session_object — wraps a batch
# ----------------------------------------------------------------------


@dataclass
class SessionObject:
    """A batch of decisions, wrapped as a first-class entity.

    For NIS today, one session = one template upload + its full review and ship cycle.
    Future modules will produce sessions of their own shape (one catalog-health daily
    run = one session, one experiment review = one session, etc.).

    Minimum fields locked at v1.0.0 to enforce session as a real boundary. Computed
    fields (auto_summary, decision counts) are derived from decision_event queries at
    summary time, not stored here.
    """

    session_id: str = field(default_factory=lambda: str(uuid4()))
    """Unique identifier for this session. UUID4."""

    workspace_id: str = ""
    """Workspace this session belongs to. Mandatory."""

    operator_id: str = ""
    """Who ran this session. For TLG today, this is Devang/Nate/Sheik."""

    module: Module = Module.NIS
    """Which Atlas module this session belongs to."""

    started_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """When the session began."""

    ended_at: Optional[str] = None
    """When the session was submitted. Null while live."""

    state: str = "live"
    """Session lifecycle state: 'live', 'submitted', 'archived'."""

    operator_notes: Optional[str] = None
    """Free-text notes the operator types during the session.

    Editable throughout the session. Captures the running context an operator
    has in their head while working. High-signal because it is written in the
    moment, with full context fresh. Often skipped in similar products; not here.
    """

    exemplar: bool = False
    """True if the operator marked this session as exemplary.

    Sessions tagged exemplar are weighted heavier in pattern detection and
    brand-voice learning. Lets the operator explicitly say "this batch
    represents how we want Atlas to behave." Costs nothing to add at v1.0.0;
    invaluable downstream.
    """

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["module"] = self.module.value if self.module else None
        return d


# ----------------------------------------------------------------------
# judgment_moment_event — the detection layer's output
# ----------------------------------------------------------------------


@dataclass
class JudgmentMomentEvent:
    """Fired when the Judgment Detection Engine flags a decision as worth surfacing
    to the operator as a conversational prompt.

    Locked at v1.0.0 with the minimum field needed to learn whether prompts work:
    which trigger fired. Additional fields (prompt text, response, engagement time)
    are appended by later schema versions as the detection layer matures.
    """

    moment_id: str = field(default_factory=lambda: str(uuid4()))
    """Unique identifier for this judgment moment."""

    workspace_id: str = ""
    """Workspace this moment belongs to. Mandatory."""

    decision_event_id: str = ""
    """The decision_event that triggered this moment. Foreign key."""

    session_id: Optional[str] = None
    """The session this moment was surfaced in."""

    surfaced_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    """When Atlas prompted the operator."""

    trigger_type: TriggerType = TriggerType.CONFIDENCE_MISMATCH
    """Which judgment-detection signal fired. The single locked field needed to
    learn over time which prompt types produce engagement vs friction.

    Adding a new detection signal only requires extending the TriggerType enum,
    not changing the schema.
    """

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["trigger_type"] = self.trigger_type.value
        return d


# ----------------------------------------------------------------------
# JSON Schema validators
# ----------------------------------------------------------------------

DECISION_EVENT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Atlas decision_event",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_id",
        "workspace_id",
        "module",
        "field_name",
        "timestamp",
        "rules_injected",
        "brand_profile_version",
        "atlas_output",
        "private_scope",
        "contributable_scope",
    ],
    "properties": {
        "event_id": {"type": "string", "format": "uuid"},
        "workspace_id": {"type": "string", "minLength": 1},
        "session_id": {"type": ["string", "null"]},
        "module": {"type": "string", "enum": [m.value for m in Module]},
        "field_name": {"type": "string", "minLength": 1},
        "timestamp": {"type": "string", "format": "date-time"},
        "rules_injected": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["rule_id"],
                "properties": {
                    "rule_id": {"type": "string"},
                    "version": {"type": ["string", "integer", "null"]},
                    "value": {},
                },
            },
        },
        "brand_profile_version": {"type": "string"},
        "atlas_output": {},
        "overall_confidence": {
            "type": ["number", "null"],
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "operator_action": {
            "type": ["string", "null"],
            "enum": [a.value for a in OperatorAction] + [None],
        },
        "operator_value": {},
        "operator_scope": {
            "type": "string",
            "enum": [s.value for s in OperatorScope],
        },
        "operator_time_to_decision_ms": {
            "type": ["integer", "null"],
            "minimum": 0,
        },
        "operator_comment": {"type": ["string", "null"]},
        "operator_viewed_case": {"type": "boolean"},
        "private_scope": {"type": "boolean"},
        "contributable_scope": {"type": "boolean"},
    },
}


SESSION_OBJECT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Atlas session_object",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "session_id",
        "workspace_id",
        "operator_id",
        "module",
        "started_at",
        "state",
        "exemplar",
    ],
    "properties": {
        "session_id": {"type": "string", "format": "uuid"},
        "workspace_id": {"type": "string", "minLength": 1},
        "operator_id": {"type": "string", "minLength": 1},
        "module": {"type": "string", "enum": [m.value for m in Module]},
        "started_at": {"type": "string", "format": "date-time"},
        "ended_at": {"type": ["string", "null"], "format": "date-time"},
        "state": {"type": "string", "enum": ["live", "submitted", "archived"]},
        "operator_notes": {"type": ["string", "null"]},
        "exemplar": {"type": "boolean"},
    },
}


JUDGMENT_MOMENT_JSON_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Atlas judgment_moment_event",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "moment_id",
        "workspace_id",
        "decision_event_id",
        "surfaced_at",
        "trigger_type",
    ],
    "properties": {
        "moment_id": {"type": "string", "format": "uuid"},
        "workspace_id": {"type": "string", "minLength": 1},
        "decision_event_id": {"type": "string", "format": "uuid"},
        "session_id": {"type": ["string", "null"]},
        "surfaced_at": {"type": "string", "format": "date-time"},
        "trigger_type": {
            "type": "string",
            "enum": [t.value for t in TriggerType],
        },
    },
}


# ----------------------------------------------------------------------
# Validation entry points
# ----------------------------------------------------------------------


class SchemaValidationError(ValueError):
    """Raised when a record fails substrate schema validation."""


def _validate_against(schema: dict[str, Any], payload: dict[str, Any]) -> None:
    """Validate a payload against a JSON Schema.

    Uses jsonschema if available; falls back to a minimal in-house validator
    that enforces required fields and enum membership. The fallback is enough
    to catch the most common drift mistakes during early development; production
    deployments should install jsonschema for full draft 2020-12 coverage.
    """
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance=payload, schema=schema)
        return
    except ImportError:
        pass
    except Exception as exc:  # jsonschema.ValidationError or similar
        raise SchemaValidationError(str(exc)) from exc

    # Minimal fallback: check required keys and enum membership.
    for required in schema.get("required", []):
        if required not in payload:
            raise SchemaValidationError(f"missing required field: {required}")
    props = schema.get("properties", {})
    for k, v in payload.items():
        if k not in props:
            if not schema.get("additionalProperties", True):
                raise SchemaValidationError(f"unexpected field: {k}")
            continue
        prop = props[k]
        if "enum" in prop and v not in prop["enum"]:
            raise SchemaValidationError(
                f"field {k}={v!r} not in allowed enum {prop['enum']}"
            )


def validate_decision_event(payload: dict[str, Any]) -> None:
    """Validate a decision_event payload. Raises SchemaValidationError on drift."""
    _validate_against(DECISION_EVENT_JSON_SCHEMA, payload)


def validate_session_object(payload: dict[str, Any]) -> None:
    """Validate a session_object payload. Raises SchemaValidationError on drift."""
    _validate_against(SESSION_OBJECT_JSON_SCHEMA, payload)


def validate_judgment_moment(payload: dict[str, Any]) -> None:
    """Validate a judgment_moment_event payload. Raises SchemaValidationError on drift."""
    _validate_against(JUDGMENT_MOMENT_JSON_SCHEMA, payload)


__all__ = [
    "SCHEMA_VERSION",
    "Module",
    "OperatorAction",
    "OperatorScope",
    "TriggerType",
    "DecisionEvent",
    "SessionObject",
    "JudgmentMomentEvent",
    "DECISION_EVENT_JSON_SCHEMA",
    "SESSION_OBJECT_JSON_SCHEMA",
    "JUDGMENT_MOMENT_JSON_SCHEMA",
    "SchemaValidationError",
    "validate_decision_event",
    "validate_session_object",
    "validate_judgment_moment",
]
