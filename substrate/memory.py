"""Atlas Memory tab — reads from substrate_sessions + substrate_events.

Two read paths exposed:

  list_sessions(workspace_id, limit, offset, state=None, operator_id=None)
      Returns most-recent-first list of session summaries. Each row carries
      counts (decisions logged, operator responses, accepts/edits/rejects)
      so the UI can render the list without a second query per row.

  get_session_detail(workspace_id, session_id)
      Returns the full session object plus its event timeline (decisions,
      operator responses, judgment moments, started/completed markers)
      in chronological order. Each decision row carries its operator
      response (if any) folded in, so the UI renders one card per
      decision without doing the join client-side.

Falls back to JSONL when Postgres pool is None. Read-only — never writes.
"""
from __future__ import annotations

import os
import json
from datetime import datetime, timezone
from typing import Any, Optional

from substrate.db import get_pool


def _use_postgres() -> bool:
    return get_pool() is not None


def _iso(v: Any) -> Optional[str]:
    if v is None:
        return None
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


# ---------------------------------------------------------------------------
# Postgres backend
# ---------------------------------------------------------------------------


def _pg_list_sessions(
    workspace_id: str,
    limit: int,
    offset: int,
    state: Optional[str],
    operator_id: Optional[str],
) -> list[dict[str, Any]]:
    pool = get_pool()
    where = ["s.workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if state:
        where.append("s.state = %s")
        params.append(state)
    if operator_id:
        where.append("s.operator_id = %s")
        params.append(operator_id)
    where_sql = " AND ".join(where)

    sql = f"""
        SELECT s.session_id, s.workspace_id, s.operator_id, s.module,
               s.started_at, s.ended_at, s.state, s.operator_notes,
               s.exemplar,
               COALESCE(o.display_name, s.operator_id) AS operator_display,
               c.decisions_count,
               c.responses_count,
               c.accepts_count,
               c.edits_count,
               c.rejects_count,
               c.dismissed_count
        FROM substrate_sessions s
        LEFT JOIN operators o
            ON o.workspace_id = s.workspace_id AND o.operator_id = s.operator_id
        -- Counts: decisions are filtered by session_id directly. Operator
        -- responses don't carry session_id; we follow links_to_event_id
        -- back to decisions that DO belong to this session.
        LEFT JOIN LATERAL (
            SELECT
                (SELECT COUNT(*) FROM substrate_events d
                   WHERE d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id
                     AND d.event_kind = 'decision_event')              AS decisions_count,
                (SELECT COUNT(*) FROM substrate_events r
                   JOIN substrate_events d
                     ON d.event_id = r.links_to_event_id
                    AND d.event_kind = 'decision_event'
                   WHERE r.workspace_id = s.workspace_id
                     AND r.event_kind = 'operator_response'
                     AND d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id)                  AS responses_count,
                (SELECT COUNT(*) FROM substrate_events r
                   JOIN substrate_events d
                     ON d.event_id = r.links_to_event_id
                    AND d.event_kind = 'decision_event'
                   WHERE r.workspace_id = s.workspace_id
                     AND r.event_kind = 'operator_response'
                     AND r.operator_action = 'accept'
                     AND d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id)                  AS accepts_count,
                (SELECT COUNT(*) FROM substrate_events r
                   JOIN substrate_events d
                     ON d.event_id = r.links_to_event_id
                    AND d.event_kind = 'decision_event'
                   WHERE r.workspace_id = s.workspace_id
                     AND r.event_kind = 'operator_response'
                     AND r.operator_action = 'edit'
                     AND d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id)                  AS edits_count,
                (SELECT COUNT(*) FROM substrate_events r
                   JOIN substrate_events d
                     ON d.event_id = r.links_to_event_id
                    AND d.event_kind = 'decision_event'
                   WHERE r.workspace_id = s.workspace_id
                     AND r.event_kind = 'operator_response'
                     AND r.operator_action = 'reject'
                     AND d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id)                  AS rejects_count,
                (SELECT COUNT(*) FROM substrate_events r
                   JOIN substrate_events d
                     ON d.event_id = r.links_to_event_id
                    AND d.event_kind = 'decision_event'
                   WHERE r.workspace_id = s.workspace_id
                     AND r.event_kind = 'operator_response'
                     AND r.operator_action = 'dismiss'
                     AND d.workspace_id = s.workspace_id
                     AND d.session_id = s.session_id)                  AS dismissed_count
        ) c ON TRUE
        WHERE {where_sql}
        ORDER BY s.started_at DESC, s.session_id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    rows: list[dict[str, Any]] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            cols = [d[0] for d in cur.description]
            for r in cur:
                d = dict(zip(cols, r))
                d["started_at"] = _iso(d.get("started_at"))
                d["ended_at"] = _iso(d.get("ended_at"))
                # Coerce None counts (no events yet) to zero for cleaner UI.
                for k in (
                    "decisions_count", "responses_count", "accepts_count",
                    "edits_count", "rejects_count", "dismissed_count",
                ):
                    d[k] = int(d.get(k) or 0)
                rows.append(d)
    return rows


def _pg_count_sessions(
    workspace_id: str,
    state: Optional[str],
    operator_id: Optional[str],
) -> int:
    pool = get_pool()
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if state:
        where.append("state = %s")
        params.append(state)
    if operator_id:
        where.append("operator_id = %s")
        params.append(operator_id)
    sql = f"SELECT COUNT(*) FROM substrate_sessions WHERE {' AND '.join(where)}"
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            row = cur.fetchone()
    return int(row[0]) if row else 0


def _pg_session_detail(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    pool = get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # 1. Session header
            cur.execute(
                """
                SELECT s.session_id, s.workspace_id, s.operator_id, s.module,
                       s.started_at, s.ended_at, s.state, s.operator_notes,
                       s.exemplar,
                       COALESCE(o.display_name, s.operator_id) AS operator_display
                FROM substrate_sessions s
                LEFT JOIN operators o
                    ON o.workspace_id = s.workspace_id AND o.operator_id = s.operator_id
                WHERE s.workspace_id = %s AND s.session_id = %s
                """,
                (workspace_id, session_id),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cols = [d[0] for d in cur.description]
            session = dict(zip(cols, row))
            session["started_at"] = _iso(session.get("started_at"))
            session["ended_at"] = _iso(session.get("ended_at"))

            # 2. Every event in this session, chronological. Decisions and
            # session lifecycle rows are pinned by session_id directly.
            # Operator responses don't carry session_id, so they're pulled
            # via links_to_event_id back to decisions in this session.
            cur.execute(
                """
                SELECT event_kind, event_id, timestamp,
                       module, field_name, rules_injected, brand_profile_version,
                       atlas_output, overall_confidence, private_scope, contributable_scope,
                       links_to_event_id, operator_action, operator_value, operator_scope,
                       operator_time_to_decision_ms, operator_comment, operator_viewed_case,
                       decision_event_id, trigger_type, surfaced_at,
                       operator_id, started_at, ended_at, exemplar,
                       meta, pre_change_snapshot
                FROM substrate_events
                WHERE workspace_id = %s AND session_id = %s
                UNION ALL
                SELECT r.event_kind, r.event_id, r.timestamp,
                       r.module, r.field_name, r.rules_injected, r.brand_profile_version,
                       r.atlas_output, r.overall_confidence, r.private_scope, r.contributable_scope,
                       r.links_to_event_id, r.operator_action, r.operator_value, r.operator_scope,
                       r.operator_time_to_decision_ms, r.operator_comment, r.operator_viewed_case,
                       r.decision_event_id, r.trigger_type, r.surfaced_at,
                       r.operator_id, r.started_at, r.ended_at, r.exemplar,
                       r.meta, r.pre_change_snapshot
                FROM substrate_events r
                JOIN substrate_events d
                  ON d.event_id = r.links_to_event_id
                 AND d.event_kind = 'decision_event'
                 AND d.workspace_id = r.workspace_id
                WHERE r.workspace_id = %s
                  AND r.event_kind = 'operator_response'
                  AND d.session_id = %s
                ORDER BY 3 ASC, 2 ASC
                """,
                (workspace_id, session_id, workspace_id, session_id),
            )
            ev_cols = [d[0] for d in cur.description]
            raw_events: list[dict[str, Any]] = []
            for r in cur:
                d = dict(zip(ev_cols, r))
                for k in ("timestamp", "surfaced_at", "started_at", "ended_at"):
                    if d.get(k) is not None:
                        d[k] = _iso(d[k])
                for k in ("event_id", "links_to_event_id", "decision_event_id"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                # Fold meta into _meta for parity with JSONL.
                meta = d.pop("meta", None) or {}
                d["_meta"] = meta
                # Empty snapshot -> None (truthy checks).
                snap = d.get("pre_change_snapshot")
                if snap == {} or snap is None:
                    d["pre_change_snapshot"] = None
                raw_events.append(d)

    # 3. Fold operator_response rows into their parent decision_event so the
    #    UI can render a single card per decision. Unlinked responses (rare)
    #    remain as standalone events.
    decisions_by_id: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for ev in raw_events:
        if ev["event_kind"] == "decision_event":
            ev["operator_responses"] = []
            decisions_by_id[ev["event_id"]] = ev
            timeline.append(ev)
        elif ev["event_kind"] == "operator_response":
            parent = decisions_by_id.get(ev.get("links_to_event_id") or "")
            if parent is not None:
                parent["operator_responses"].append(ev)
            else:
                timeline.append(ev)
        else:
            timeline.append(ev)

    session["timeline"] = timeline
    # Convenience flat counts on the detail too.
    session["decisions_count"] = sum(
        1 for e in raw_events if e["event_kind"] == "decision_event"
    )
    session["responses_count"] = sum(
        1 for e in raw_events if e["event_kind"] == "operator_response"
    )
    return session


# ---------------------------------------------------------------------------
# JSONL fallback (best-effort; production runs on Postgres)
# ---------------------------------------------------------------------------


def _jsonl_root() -> str:
    return os.environ.get(
        "ATLAS_SUBSTRATE_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "substrate"),
    )


def _jsonl_sessions_root() -> str:
    return os.environ.get(
        "ATLAS_SESSIONS_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "sessions"),
    )


def _jsonl_iter_events(workspace_id: str) -> list[dict[str, Any]]:
    """Read every monthly .jsonl for a workspace and return all events."""
    root = os.path.join(_jsonl_root(), workspace_id)
    if not os.path.isdir(root):
        return []
    events: list[dict[str, Any]] = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".jsonl"):
            continue
        try:
            with open(os.path.join(root, fn), "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return events


def _jsonl_list_sessions(
    workspace_id: str,
    limit: int,
    offset: int,
    state: Optional[str],
    operator_id: Optional[str],
) -> list[dict[str, Any]]:
    root = os.path.join(_jsonl_sessions_root(), workspace_id)
    if not os.path.isdir(root):
        return []
    sessions: list[dict[str, Any]] = []
    for fn in os.listdir(root):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, fn), "r", encoding="utf-8") as fh:
                sessions.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue

    if state:
        sessions = [s for s in sessions if s.get("state") == state]
    if operator_id:
        sessions = [s for s in sessions if s.get("operator_id") == operator_id]

    sessions.sort(key=lambda s: s.get("started_at") or "", reverse=True)
    page = sessions[offset:offset + limit]

    # Annotate counts per session. Decisions are pinned by session_id;
    # operator_responses are linked back to decisions via links_to_event_id
    # (they don't carry session_id themselves).
    events = _jsonl_iter_events(workspace_id)
    decisions_by_session: dict[str, list[str]] = {}
    decision_to_session: dict[str, str] = {}
    for ev in events:
        if ev.get("event_kind") == "decision_event":
            sid = ev.get("session_id")
            eid = ev.get("event_id")
            if sid and eid:
                decisions_by_session.setdefault(sid, []).append(eid)
                decision_to_session[eid] = sid
    responses_by_session: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        if ev.get("event_kind") != "operator_response":
            continue
        parent = ev.get("links_to_event_id")
        sid = decision_to_session.get(parent)
        if sid:
            responses_by_session.setdefault(sid, []).append(ev)
    for s in page:
        sid = s.get("session_id") or ""
        dec_ids = decisions_by_session.get(sid, [])
        resps = responses_by_session.get(sid, [])
        s["decisions_count"] = len(dec_ids)
        s["responses_count"] = len(resps)
        s["accepts_count"] = sum(1 for e in resps if e.get("operator_action") == "accept")
        s["edits_count"] = sum(1 for e in resps if e.get("operator_action") == "edit")
        s["rejects_count"] = sum(1 for e in resps if e.get("operator_action") == "reject")
        s["dismissed_count"] = sum(1 for e in resps if e.get("operator_action") == "dismiss")
        s["operator_display"] = s.get("operator_id")
    return page


def _jsonl_count_sessions(
    workspace_id: str,
    state: Optional[str],
    operator_id: Optional[str],
) -> int:
    root = os.path.join(_jsonl_sessions_root(), workspace_id)
    if not os.path.isdir(root):
        return 0
    n = 0
    for fn in os.listdir(root):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, fn), "r", encoding="utf-8") as fh:
                s = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        if state and s.get("state") != state:
            continue
        if operator_id and s.get("operator_id") != operator_id:
            continue
        n += 1
    return n


def _jsonl_session_detail(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    path = os.path.join(_jsonl_sessions_root(), workspace_id, f"{session_id}.json")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        session = json.load(fh)
    all_events = _jsonl_iter_events(workspace_id)
    # Build decision_id -> session_id map so we can resolve operator_responses
    # that don't carry session_id directly.
    decision_ids_in_session: set[str] = {
        e.get("event_id") for e in all_events
        if e.get("event_kind") == "decision_event"
        and e.get("session_id") == session_id and e.get("event_id")
    }
    events: list[dict[str, Any]] = []
    for e in all_events:
        if e.get("event_kind") == "operator_response":
            if e.get("links_to_event_id") in decision_ids_in_session:
                events.append(e)
        elif e.get("session_id") == session_id:
            events.append(e)
    events.sort(key=lambda e: e.get("timestamp") or "")
    decisions_by_id: dict[str, dict[str, Any]] = {}
    timeline: list[dict[str, Any]] = []
    for ev in events:
        if ev.get("event_kind") == "decision_event":
            ev["operator_responses"] = []
            decisions_by_id[ev.get("event_id") or ""] = ev
            timeline.append(ev)
        elif ev.get("event_kind") == "operator_response":
            parent = decisions_by_id.get(ev.get("links_to_event_id") or "")
            if parent is not None:
                parent["operator_responses"].append(ev)
            else:
                timeline.append(ev)
        else:
            timeline.append(ev)
    session["timeline"] = timeline
    session["decisions_count"] = sum(1 for e in events if e.get("event_kind") == "decision_event")
    session["responses_count"] = sum(1 for e in events if e.get("event_kind") == "operator_response")
    session["operator_display"] = session.get("operator_id")
    return session


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def list_sessions(
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
    state: Optional[str] = None,
    operator_id: Optional[str] = None,
) -> dict[str, Any]:
    """Return a page of session summaries plus the total count.

    Shape:
      { "sessions": [ ... ], "total": int }
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if _use_postgres():
        return {
            "sessions": _pg_list_sessions(workspace_id, limit, offset, state, operator_id),
            "total": _pg_count_sessions(workspace_id, state, operator_id),
        }
    return {
        "sessions": _jsonl_list_sessions(workspace_id, limit, offset, state, operator_id),
        "total": _jsonl_count_sessions(workspace_id, state, operator_id),
    }


def get_session_detail(workspace_id: str, session_id: str) -> Optional[dict[str, Any]]:
    """Return one session's full detail with timeline, or None if not found."""
    if _use_postgres():
        result = _pg_session_detail(workspace_id, session_id)
        if result is not None:
            return result
    return _jsonl_session_detail(workspace_id, session_id)


# ---------------------------------------------------------------------------
# Cross-session decisions feed.
#
# Used by the Memory tab's "Decisions" sub-view. A flat list of every
# decision_event for the workspace, with each one carrying:
#   - the operator's terminal response (accept/edit/reject/dismiss/view)
#     folded in, if any. "view" rows are surfaced but de-prioritised
#     when an accept/edit/reject exists later.
#   - the parent session_id + operator_id so the UI can deep-link back.
#   - the pre_change_snapshot captured at decision time.
#
# Filters supported (all optional, AND-combined):
#   field          substring match on field_name (ILIKE)
#   asin           exact match on meta->>'asin'
#   action         'accept' | 'edit' | 'reject' | 'comment' | 'no_response'
#                  ('no_response' = decisions with zero operator_response rows)
#   operator_id    decisions whose session belongs to this operator
#   session_id     decisions in a single session (link from Sessions view)
#   start / end    ISO timestamp window on the decision's timestamp
# ---------------------------------------------------------------------------

_TERMINAL_ACTIONS = ("accept", "edit", "reject", "comment")


def _terminal_response(responses: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pick the operator's terminal response from a list of responses.

    Order of precedence: latest terminal action wins; if there are only
    view/intermediate rows, returns the latest one.
    """
    if not responses:
        return None
    terminals = [r for r in responses if r.get("operator_action") in _TERMINAL_ACTIONS]
    if terminals:
        terminals.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
        return terminals[0]
    responses_sorted = sorted(responses, key=lambda r: r.get("timestamp") or "", reverse=True)
    return responses_sorted[0]


def _pg_list_decisions(
    workspace_id: str,
    limit: int,
    offset: int,
    field: Optional[str],
    asin: Optional[str],
    action: Optional[str],
    operator_id: Optional[str],
    session_id: Optional[str],
    start: Optional[str],
    end: Optional[str],
) -> dict[str, Any]:
    pool = get_pool()
    where = ["d.workspace_id = %s", "d.event_kind = 'decision_event'"]
    params: list[Any] = [workspace_id]
    if field:
        where.append("d.field_name ILIKE %s")
        params.append(f"%{field}%")
    if asin:
        where.append("d.meta->>'asin' = %s")
        params.append(asin)
    if session_id:
        where.append("d.session_id = %s")
        params.append(session_id)
    if start:
        where.append("d.timestamp >= %s")
        params.append(start)
    if end:
        where.append("d.timestamp <= %s")
        params.append(end)
    if operator_id:
        # operator_id lives on substrate_sessions; join.
        where.append(
            "d.session_id IN (SELECT session_id FROM substrate_sessions "
            "WHERE workspace_id = d.workspace_id AND operator_id = %s)"
        )
        params.append(operator_id)

    where_sql = " AND ".join(where)

    # Aggregate the operator's terminal response per decision in SQL using
    # DISTINCT ON. Action filter (including 'no_response') applies after.
    sql_decisions = f"""
        WITH base AS (
            SELECT d.event_id, d.session_id, d.timestamp,
                   d.module, d.field_name,
                   d.rules_injected, d.atlas_output, d.overall_confidence,
                   d.private_scope, d.contributable_scope,
                   d.brand_profile_version,
                   d.meta, d.pre_change_snapshot
            FROM substrate_events d
            WHERE {where_sql}
        ),
        resp AS (
            SELECT DISTINCT ON (r.links_to_event_id)
                   r.links_to_event_id AS decision_id,
                   r.event_id    AS response_event_id,
                   r.timestamp   AS response_ts,
                   r.operator_action,
                   r.operator_value,
                   r.operator_scope,
                   r.operator_time_to_decision_ms,
                   r.operator_comment,
                   r.operator_viewed_case
            FROM substrate_events r
            JOIN base b ON b.event_id = r.links_to_event_id
            WHERE r.event_kind = 'operator_response'
              AND r.workspace_id = %s
            ORDER BY r.links_to_event_id,
                     CASE WHEN r.operator_action IN ('accept','edit','reject','comment') THEN 0 ELSE 1 END,
                     r.timestamp DESC
        )
        SELECT b.event_id, b.session_id, b.timestamp,
               b.module, b.field_name,
               b.rules_injected, b.atlas_output, b.overall_confidence,
               b.private_scope, b.contributable_scope,
               b.brand_profile_version,
               b.meta, b.pre_change_snapshot,
               s.operator_id,
               COALESCE(o.display_name, s.operator_id) AS operator_display,
               resp.response_event_id, resp.response_ts,
               resp.operator_action, resp.operator_value, resp.operator_scope,
               resp.operator_time_to_decision_ms, resp.operator_comment,
               resp.operator_viewed_case
        FROM base b
        LEFT JOIN resp ON resp.decision_id = b.event_id
        LEFT JOIN substrate_sessions s
            ON s.session_id = b.session_id AND s.workspace_id = %s
        LEFT JOIN operators o
            ON o.workspace_id = s.workspace_id AND o.operator_id = s.operator_id
    """
    action_clause = ""
    extra_params: list[Any] = []
    if action == "no_response":
        action_clause = " WHERE resp.response_event_id IS NULL"
    elif action in _TERMINAL_ACTIONS:
        action_clause = " WHERE resp.operator_action = %s"
        extra_params.append(action)

    sql_full = (
        sql_decisions
        + action_clause
        + " ORDER BY b.timestamp DESC, b.event_id DESC LIMIT %s OFFSET %s"
    )
    sql_count = (
        f"SELECT COUNT(*) FROM ( {sql_decisions} {action_clause} ) sub"
    )

    # Build params: base WHERE params appear in both queries. The CTE has
    # `params` for `base`, then [workspace_id] for `resp`, then
    # [workspace_id] for the session join, then action_clause params.
    cte_params = tuple(params) + (workspace_id, workspace_id) + tuple(extra_params)

    rows: list[dict[str, Any]] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql_count, cte_params)
            total = int(cur.fetchone()[0])
            cur.execute(sql_full, cte_params + (limit, offset))
            cols = [d[0] for d in cur.description]
            for r in cur:
                d = dict(zip(cols, r))
                d["timestamp"] = _iso(d.get("timestamp"))
                d["response_ts"] = _iso(d.get("response_ts"))
                for k in ("event_id", "response_event_id"):
                    if d.get(k) is not None:
                        d[k] = str(d[k])
                meta = d.pop("meta", None) or {}
                d["_meta"] = meta
                d["asin"] = meta.get("asin") if isinstance(meta, dict) else None
                snap = d.get("pre_change_snapshot")
                if snap == {} or snap is None:
                    d["pre_change_snapshot"] = None
                rows.append(d)
    return {"decisions": rows, "total": total}


def _jsonl_list_decisions(
    workspace_id: str,
    limit: int,
    offset: int,
    field: Optional[str],
    asin: Optional[str],
    action: Optional[str],
    operator_id: Optional[str],
    session_id: Optional[str],
    start: Optional[str],
    end: Optional[str],
) -> dict[str, Any]:
    """In-memory filter over JSONL events. Test/dev path only."""
    events = _jsonl_iter_events(workspace_id)

    # Build decision_id -> [responses] map.
    responses_by_decision: dict[str, list[dict[str, Any]]] = {}
    decision_rows: list[dict[str, Any]] = []
    for ev in events:
        kind = ev.get("event_kind")
        if kind == "decision_event":
            decision_rows.append(ev)
        elif kind == "operator_response":
            parent = ev.get("links_to_event_id")
            if parent:
                responses_by_decision.setdefault(parent, []).append(ev)

    # Sessions → operator_id (for operator_id filter)
    sessions_map: dict[str, dict[str, Any]] = {}
    try:
        root = os.path.join(_jsonl_sessions_root(), workspace_id)
        if os.path.isdir(root):
            for fn in os.listdir(root):
                if not fn.endswith(".json"):
                    continue
                with open(os.path.join(root, fn), "r", encoding="utf-8") as fh:
                    s = json.load(fh)
                if s.get("session_id"):
                    sessions_map[s["session_id"]] = s
    except OSError:
        pass

    filtered: list[dict[str, Any]] = []
    for d in decision_rows:
        if field and field.lower() not in (d.get("field_name") or "").lower():
            continue
        meta = d.get("_meta") or {}
        if asin and meta.get("asin") != asin:
            continue
        if session_id and d.get("session_id") != session_id:
            continue
        ts = d.get("timestamp") or ""
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        sid = d.get("session_id") or ""
        session_row = sessions_map.get(sid, {})
        if operator_id and session_row.get("operator_id") != operator_id:
            continue

        responses = responses_by_decision.get(d.get("event_id") or "", [])
        resp = _terminal_response(responses)
        if action == "no_response":
            if resp is not None:
                continue
        elif action in _TERMINAL_ACTIONS:
            if resp is None or resp.get("operator_action") != action:
                continue

        row = {
            "event_id": d.get("event_id"),
            "session_id": sid,
            "timestamp": ts,
            "module": d.get("module"),
            "field_name": d.get("field_name"),
            "rules_injected": d.get("rules_injected") or [],
            "atlas_output": d.get("atlas_output"),
            "overall_confidence": d.get("overall_confidence"),
            "private_scope": d.get("private_scope"),
            "contributable_scope": d.get("contributable_scope"),
            "brand_profile_version": d.get("brand_profile_version"),
            "_meta": meta,
            "asin": meta.get("asin") if isinstance(meta, dict) else None,
            "pre_change_snapshot": d.get("pre_change_snapshot"),
            "operator_id": session_row.get("operator_id"),
            "operator_display": session_row.get("operator_id"),
            "response_event_id": resp.get("event_id") if resp else None,
            "response_ts": resp.get("timestamp") if resp else None,
            "operator_action": resp.get("operator_action") if resp else None,
            "operator_value": resp.get("operator_value") if resp else None,
            "operator_scope": resp.get("operator_scope") if resp else None,
            "operator_time_to_decision_ms": resp.get("operator_time_to_decision_ms") if resp else None,
            "operator_comment": resp.get("operator_comment") if resp else None,
            "operator_viewed_case": resp.get("operator_viewed_case") if resp else None,
        }
        filtered.append(row)

    filtered.sort(key=lambda r: r.get("timestamp") or "", reverse=True)
    page = filtered[offset:offset + limit]
    return {"decisions": page, "total": len(filtered)}


def list_decisions(
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
    field: Optional[str] = None,
    asin: Optional[str] = None,
    action: Optional[str] = None,
    operator_id: Optional[str] = None,
    session_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> dict[str, Any]:
    """Cross-session flat decisions feed.

    Shape: { decisions: [ ... ], total: int }
    """
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    if action and action not in _TERMINAL_ACTIONS + ("no_response",):
        action = None
    if _use_postgres():
        return _pg_list_decisions(
            workspace_id, limit, offset,
            field, asin, action, operator_id, session_id, start, end,
        )
    return _jsonl_list_decisions(
        workspace_id, limit, offset,
        field, asin, action, operator_id, session_id, start, end,
    )


__all__ = ["list_sessions", "get_session_detail", "list_decisions"]
