"""Atlas substrate \u2014 one-shot JSONL \u2192 Postgres migration.

Reads any surviving .jsonl files under ATLAS_SUBSTRATE_ROOT (or the default
`decision_log/`) and inserts each row into substrate_events. Idempotent
because substrate_events has PRIMARY KEY (event_id, event_kind) with
ON CONFLICT DO NOTHING semantics in the logger.

Best-effort throughout:
  - No files? No-op, return 0.
  - DB unavailable? No-op, return 0.
  - Bad lines? Skip and log; don't abort.

Called from app.py at boot. Also runnable as a script for manual recovery:

    python -m substrate.migrate_jsonl
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from typing import Iterator

logger = logging.getLogger("atlas.substrate.migrate_jsonl")


def _decision_log_root() -> str:
    return os.environ.get(
        "ATLAS_SUBSTRATE_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "decision_log"),
    )


def _sessions_meta_root() -> str:
    return os.environ.get(
        "ATLAS_SESSIONS_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "sessions"),
    )


def _walk_jsonl_files(root: str) -> Iterator[tuple[str, str]]:
    """Yield (workspace_id, file_path) for every .jsonl under the root.

    Workspace_id is derived from the directory layout:
        decision_log/<workspace>/<YYYY-MM>.jsonl
    Skips files inside a `sessions/` subdir (those are session events, which
    we re-emit from substrate_sessions rows instead).
    """
    if not os.path.isdir(root):
        return
    for workspace_dir in sorted(os.listdir(root)):
        wpath = os.path.join(root, workspace_dir)
        if not os.path.isdir(wpath):
            continue
        for entry in sorted(os.listdir(wpath)):
            if entry.endswith(".jsonl"):
                yield workspace_dir, os.path.join(wpath, entry)


_DETERMINISTIC_UUID_NS = uuid.UUID("f47ac10b-58cc-4372-a567-0e02b2c3d479")


def _deterministic_event_id(payload: dict) -> str:
    """Generate a stable UUID for events that lack a natural primary key.

    operator_response and session_started/session_completed rows in legacy
    JSONL did not carry an event_id. Re-running the migration must not
    duplicate them, so we hash the payload's identifying fields into a
    deterministic UUID. Same payload → same UUID → ON CONFLICT DO NOTHING.
    """
    kind = payload.get("event_kind", "")
    parts = [
        kind,
        str(payload.get("workspace_id", "")),
        str(payload.get("timestamp", "")),
        str(payload.get("links_to_event_id", "")),
        str(payload.get("session_id", "")),
        str(payload.get("operator_action", "")),
        str(payload.get("started_at", "")),
        str(payload.get("ended_at", "")),
    ]
    seed = "|".join(parts)
    return str(uuid.uuid5(_DETERMINISTIC_UUID_NS, seed))


def _migrate_jsonl_file(path: str, workspace_id: str) -> tuple[int, int, int]:
    """Migrate one .jsonl file. Returns (inserted, skipped, errors).

    Uses _pg_insert_event() under the hood; that path uses
    ON CONFLICT (event_id, event_kind) DO NOTHING so re-runs are safe.
    For events that arrive without event_id/moment_id, we synthesise a
    deterministic UUID from payload content so re-runs hit the same row.
    """
    from substrate.logger import _pg_insert_event  # type: ignore

    inserted = 0
    skipped = 0
    errors = 0
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                errors += 1
                continue
            # Ensure workspace_id is present (older rows may have omitted it)
            payload.setdefault("workspace_id", workspace_id)
            kind = payload.get("event_kind")
            if not kind:
                # Heuristic: shape it from known fields
                if "trigger_type" in payload:
                    payload["event_kind"] = "judgment_moment_event"
                elif "links_to_event_id" in payload:
                    payload["event_kind"] = "operator_response"
                elif "field_name" in payload and "atlas_output" in payload:
                    payload["event_kind"] = "decision_event"
                else:
                    skipped += 1
                    continue
            # Stamp a deterministic event_id when one is missing so the
            # ON CONFLICT path actually dedupes on re-runs.
            if not payload.get("event_id") and not payload.get("moment_id"):
                payload["event_id"] = _deterministic_event_id(payload)
            try:
                _pg_insert_event(payload)
                inserted += 1
            except Exception as exc:
                logger.warning("row insert failed in %s: %s", path, exc)
                errors += 1
    return inserted, skipped, errors


def _migrate_session_files(root: str) -> int:
    """Migrate session_object JSON files into substrate_sessions.

    Layout: sessions/<workspace>/<session_id>.json
    Idempotent via INSERT ... ON CONFLICT DO UPDATE in _pg_upsert_session.
    """
    if not os.path.isdir(root):
        return 0
    from substrate.logger import _pg_upsert_session  # type: ignore
    from substrate.schema import Module, SessionObject

    migrated = 0
    for workspace_dir in sorted(os.listdir(root)):
        wpath = os.path.join(root, workspace_dir)
        if not os.path.isdir(wpath):
            continue
        for entry in sorted(os.listdir(wpath)):
            if not entry.endswith(".json"):
                continue
            fpath = os.path.join(wpath, entry)
            try:
                with open(fpath, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
                # Hydrate a SessionObject so the upsert path stays consistent.
                module = d.get("module", "nis")
                s = SessionObject(
                    session_id=d.get("session_id"),
                    workspace_id=d.get("workspace_id", workspace_dir),
                    operator_id=d.get("operator_id", "unknown"),
                    module=Module(module) if module in [m.value for m in Module] else Module.NIS,
                    started_at=d.get("started_at", ""),
                    ended_at=d.get("ended_at"),
                    state=d.get("state", "live"),
                    operator_notes=d.get("operator_notes"),
                    exemplar=bool(d.get("exemplar", False)),
                )
                _pg_upsert_session(s)
                migrated += 1
            except Exception as exc:
                logger.warning("session migration skipped for %s: %s", fpath, exc)
    return migrated


def migrate_all() -> dict:
    """Run the full migration. Safe to call repeatedly.

    Returns a summary dict with counts. No-op (zeros) when DB unavailable.
    """
    from substrate.db import get_pool

    if get_pool() is None:
        return {"status": "no_postgres", "inserted": 0, "skipped": 0, "errors": 0,
                "sessions_migrated": 0, "files": 0}

    root = _decision_log_root()
    sess_root = _sessions_meta_root()

    summary = {
        "status": "ok",
        "root": root,
        "files": 0,
        "inserted": 0,
        "skipped": 0,
        "errors": 0,
        "sessions_migrated": 0,
    }

    for workspace_id, fpath in _walk_jsonl_files(root):
        summary["files"] += 1
        try:
            ins, skp, err = _migrate_jsonl_file(fpath, workspace_id)
            summary["inserted"] += ins
            summary["skipped"] += skp
            summary["errors"] += err
            logger.info("migrated %s (ws=%s): +%d ~%d !%d", fpath, workspace_id, ins, skp, err)
        except Exception as exc:
            logger.warning("file migration failed for %s: %s", fpath, exc)
            summary["errors"] += 1

    try:
        summary["sessions_migrated"] = _migrate_session_files(sess_root)
    except Exception as exc:
        logger.warning("session migration failed: %s", exc)

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = migrate_all()
    print(json.dumps(result, indent=2))
