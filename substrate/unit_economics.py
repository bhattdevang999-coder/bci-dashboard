"""Atlas substrate — unit economics writer.

Phase B of UNIT_ECONOMICS.md.

Purpose: take parsed business-report rows from `/api/catalog/upload-sales`
and push them into the substrate's `outcome_events` table, one row per
(asin, metric) per period. This is what makes the sales-side numbers
reachable by Memory, pre-change snapshots, and any future attribution work.

Why this lives in its own module and not in `substrate/marketing.py`:
    - The marketing writer is keyword-attached (one row per (keyword, asin,
      metric)). Sales observations are ASIN-attached with NO keyword. Mixing
      them in one writer makes the call signatures fight each other.
    - The marketing writer's metric set is now widened (see _OUTCOME_METRICS
      in marketing.py) to share the same vocabulary so reads stay uniform.

Contract:
    record_sales_observations(workspace_id, rows, sales_fields, *, source_kind,
                              source_file_hash=None, observed_at=None)
        -> { "rows_written": int, "outcome_rows": int, "skipped": int }

    rows           — the list of dicts returned by read_file_to_rows()
    sales_fields   — the {logical_field: header_name} map returned by
                     detect_columns(headers, SALES_FIELD_MAP). Comes straight
                     from app.py so we don't re-implement column detection.
    source_kind    — 'business_report' (the only producer today)

Never raises. Substrate writes are best-effort by design.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from .db import get_pool

logger = logging.getLogger("atlas.substrate.unit_economics")


# Subset of SALES_FIELD_MAP (app.py:8063) that we want as outcome_events
# metrics. Excludes pure identity/dimension fields (asin, period_*, month).
# Keep in sync with _OUTCOME_METRICS in marketing.py.
_SALES_METRIC_FIELDS = {
    "sessions":    "sessions",
    "units":       "units_sold",
    "revenue":     "revenue",
    "returns":     "returns",
    "return_rate": "return_rate",
    "cvr":         "cvr",
    "buy_box_pct": "buy_box_pct",
    "ad_spend":    "ad_spend",
    "ad_revenue":  "ad_revenue",
    "acos":        "acos",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_float(raw: Any) -> Optional[float]:
    """Parse a sales-cell value into a float, tolerating $, %, commas, blanks.

    Returns None if the cell is empty or unparseable. Callers must skip None.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Strip common report formatting
    s = s.replace("$", "").replace(",", "").replace("%", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _coerce_ts(raw: Any) -> Optional[datetime]:
    """Best-effort parse of a period-start/end cell into a datetime.

    Business reports use a handful of formats: ISO 8601, 'YYYY-MM-DD',
    'MM/DD/YYYY', 'Mon DD YYYY', etc. We try the common shapes and return
    None on anything we can't recognise. None is the correct fallback —
    outcome_events.period_start is nullable; observed_at remains populated.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    fmts = (
        "%Y-%m-%d",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
        "%m/%d/%Y",
        "%m/%d/%y",
        "%b %d %Y",
        "%b %d, %Y",
        "%B %d %Y",
        "%B %d, %Y",
    )
    for fmt in fmts:
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def record_sales_observations(
    workspace_id: str,
    rows: Iterable[dict[str, Any]],
    sales_fields: dict[str, str],
    *,
    source_kind: str = "business_report",
    source_file_hash: Optional[str] = None,
    observed_at: Optional[datetime] = None,
) -> dict[str, int]:
    """Bulk-write business-report rows into outcome_events.

    For each input row we emit one outcome_events row per (asin, metric)
    where the metric is in _SALES_METRIC_FIELDS AND the cell parses as a
    number. Empty or non-numeric cells are skipped, not zero-filled.

    Returns counts:
        rows_written  — input rows that produced at least one outcome row
        outcome_rows  — total outcome_events rows inserted
        skipped       — input rows with no usable ASIN or no parseable metrics

    Never raises. On a database error returns whatever counts accrued plus
    a skip count covering the unsent batch — the caller logs and moves on.
    """
    pool = get_pool()
    if pool is None:
        return {"rows_written": 0, "outcome_rows": 0, "skipped": 0}

    asin_col = sales_fields.get("asin")
    if not asin_col:
        logger.warning("record_sales_observations: no ASIN column detected; skipping")
        return {"rows_written": 0, "outcome_rows": 0, "skipped": 0}

    ts = observed_at or _now()
    ps_col = sales_fields.get("period_start")
    pe_col = sales_fields.get("period_end")

    to_insert: list[tuple] = []
    rows_with_data = 0
    skipped = 0

    for row in rows:
        asin = str(row.get(asin_col, "")).strip()
        if not asin:
            skipped += 1
            continue
        period_start = _coerce_ts(row.get(ps_col)) if ps_col else None
        period_end   = _coerce_ts(row.get(pe_col)) if pe_col else None

        emitted_for_row = 0
        for logical, metric in _SALES_METRIC_FIELDS.items():
            col = sales_fields.get(logical)
            if not col:
                continue
            val = _coerce_float(row.get(col))
            if val is None:
                continue
            to_insert.append((
                str(uuid.uuid4()),
                workspace_id,
                asin,
                ts,                     # observed_at (when we ingested)
                period_start,           # period_start (when the metric applies)
                period_end,             # period_end
                metric,
                val,
                source_file_hash,
                source_kind,
                None,                   # keyword — not applicable for sales
                None,                   # campaign_id
                None,                   # ad_group_id
                json.dumps({"module": "unit_economics"}),
            ))
            emitted_for_row += 1

        if emitted_for_row:
            rows_with_data += 1
        else:
            skipped += 1

    if not to_insert:
        return {"rows_written": rows_with_data,
                "outcome_rows": 0,
                "skipped": skipped}

    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO outcome_events (
                        outcome_id, workspace_id, asin, observed_at,
                        period_start, period_end,
                        metric, value, source_file_hash, source_kind,
                        keyword, campaign_id, ad_group_id, meta
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb
                    )
                    """,
                    to_insert,
                )
            conn.commit()
    except Exception as exc:
        logger.warning("record_sales_observations write failed: %s", exc)
        return {"rows_written": rows_with_data,
                "outcome_rows": 0,
                "skipped": skipped + len(to_insert)}

    return {"rows_written": rows_with_data,
            "outcome_rows": len(to_insert),
            "skipped": skipped}


__all__ = ["record_sales_observations"]
