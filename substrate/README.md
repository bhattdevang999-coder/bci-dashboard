# Atlas Substrate

The locked data foundation. Every operator decision Atlas captures flows through
these schemas. This is the company's most durable artifact — everything else in
Atlas is built on top.

## What's in this folder

- `schema.py` — the canonical schema. Three tables, 20 foundational fields, full
  Python dataclasses + JSON Schema validators.
- `test_schema.py` — minimal round-trip tests for the three tables.
- `__init__.py` — package marker.

## The 20 locked fields

### `decision_event` (17 fields)
The atom. One per operator-touching decision Atlas makes.

| # | Field | Why it has to be right at launch |
|---|---|---|
| 1 | `event_id` | Primary key of the company. Every downstream system references it. |
| 2 | `workspace_id` | Mandatory from day one. Retrofitting multi-tenant is brutal. |
| 3 | `session_id` | Links decisions to batches. Required for replay. |
| 4 | `module` | NIS today, more later. Without it we can't tell modules apart. |
| 5 | `field_name` | Foundation for per-field analytics and rule scoping. |
| 6 | `timestamp` | Order is critical for pattern detection and replay. |
| 7 | `rules_injected` | Without this, no decision is replayable or debuggable. |
| 8 | `brand_profile_version` | Makes every event reproducible against a known state. |
| 9 | `atlas_output` | What Atlas produced. The training target. |
| 10 | `overall_confidence` | Computed by Atlas, not asked from the LLM. Powers confidence-mismatch detection. |
| 11 | `operator_action` | The core training signal. |
| 12 | `operator_value` | The supervised label. |
| 13 | `operator_scope` | Converts an edit into a typed feedback signal. |
| 14 | `operator_time_to_decision_ms` | Sub-second accept ≠ 30-second accept. Unrecoverable retroactively. |
| 15 | `operator_comment` | Free-text reasoning. The highest-value training data when present. |
| 16 | `private_scope` | Gates whether content leaves the workspace. Mandatory before any aggregation. |
| 17 | `contributable_scope` | Gates whether statistics aggregate cross-workspace. |

### `session_object` (2 of 9 fields locked at launch)
Wraps a batch of work. Most fields here are operational; only these two are
hard to retrofit:

| # | Field | Why it has to be right at launch |
|---|---|---|
| 18 | `operator_notes` | Running context written in the moment. High-signal, often skipped in similar products. |
| 19 | `exemplar` | Operator marks the batch as exemplary. Weights it heavier in learning. |

### `judgment_moment_event` (1 of 6 fields locked at launch)
Fires when Atlas's detection layer flags a decision worth asking about.

| # | Field | Why it has to be right at launch |
|---|---|---|
| 20 | `trigger_type` | Which detection signal fired. Without it, we can never tune which prompts work. |

## Design principles

- **Workspace-scoped from day one.** Every record carries `workspace_id` even
  when TLG is the only workspace.
- **Append-only.** Operator-response and outcome fields are nullable and filled
  in after the fact. Never mutate.
- **Privacy at log time.** `private_scope` and `contributable_scope` are
  computed when the event is written, not retroactively.
- **Computed, not asked.** `overall_confidence` is Atlas's own computation, not
  the LLM's self-report.

## Running the tests

From the `nis-wizard-server` directory:

```bash
python -m substrate.test_schema
```

(Or `pytest substrate/test_schema.py` if pytest is installed.)

## Schema versioning

Current version: **1.0.0**

The 20 fields above are locked. Adding new fields is a minor version bump
(1.1.0). Renaming or removing a field is a major version bump (2.0.0) and
requires a migration plan. The whole point of locking the schema is to make
these latter events rare.
