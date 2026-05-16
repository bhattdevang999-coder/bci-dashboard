"""Atlas Marketing substrate.

Lives alongside substrate/inputs.py and substrate/memory.py. Provides:

  - normalise_keyword(s): canonical form for the keyword_library primary key
  - upsert_keyword(...): idempotent write into keyword_library, dedupes against
    existing rows by (workspace_id, keyword_norm), unions ASINs.
  - record_keyword_observations(rows): bulk-merge a PPC bulk / search-term
    parse into keyword_library, updating last_* metrics and writing one
    outcome_event row per (keyword, asin, metric) so we keep a time series.
  - list_keywords(...): paginated reader for the Marketing tab.
  - get_keyword_direction(decision_event_id): plain-language direction
    summary built from the outcome_events that followed a decision.

Design notes:
- keyword_library is the rolled-up *current state*; outcome_events is the
  *append-only history*. We never overwrite history.
- Every write is best-effort: a failure logs a warning but never blocks
  the higher-level operation (file upload, decision write).
- Falls back to JSONL stubs when no Postgres pool — useful for tests but
  the production path is always Postgres.

This module deliberately does NOT call the LLM. Generation lives in
app.py / a follow-up wizard module. Substrate stays pure.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from substrate.db import get_pool

logger = logging.getLogger("atlas.substrate.marketing")


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalise_keyword(s: str) -> str:
    """Canonical key for the keyword_library primary key.

    - lowercased
    - whitespace collapsed
    - trims punctuation we don't care about for dedup
    """
    if not s:
        return ""
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    # Strip leading/trailing non-alphanum (operator typos like " yoga pants,")
    s = re.sub(r"^[^\w]+|[^\w]+$", "", s)
    return s


# ---------------------------------------------------------------------------
# keyword_library writes
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_keyword(
    workspace_id: str,
    keyword: str,
    *,
    match_type: Optional[str] = None,
    asins: Optional[list[str]] = None,
    search_volume: Optional[int] = None,
    organic_rank: Optional[int] = None,
    acos: Optional[float] = None,
    spend: Optional[float] = None,
    impressions: Optional[int] = None,
    clicks: Optional[int] = None,
    orders: Optional[int] = None,
    source_kind: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Optional[str]:
    """Upsert a row into keyword_library. Returns the keyword_norm or None on failure.

    On conflict (existing keyword_norm), updates last_* fields, unions the
    asins JSONB array, refreshes last_seen_at + last_source_kind. Does NOT
    overwrite created_at or first_source_kind.
    """
    norm = normalise_keyword(keyword)
    if not norm:
        return None
    pool = get_pool()
    if pool is None:
        return norm  # JSONL test env: just return the key
    asin_list = list(dict.fromkeys(asins or []))  # dedup, preserve order
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO keyword_library (
                        workspace_id, keyword_norm, keyword, match_type,
                        asins, last_search_volume, last_organic_rank,
                        last_acos, last_spend, last_impressions, last_clicks, last_orders,
                        first_source_kind, last_source_kind, meta
                    ) VALUES (
                        %s, %s, %s, %s,
                        %s::jsonb, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s::jsonb
                    )
                    ON CONFLICT (workspace_id, keyword_norm) DO UPDATE SET
                        last_seen_at = NOW(),
                        keyword = COALESCE(NULLIF(EXCLUDED.keyword, ''), keyword_library.keyword),
                        match_type = COALESCE(EXCLUDED.match_type, keyword_library.match_type),
                        asins = (
                            SELECT to_jsonb(array_agg(DISTINCT a))
                            FROM jsonb_array_elements_text(
                                COALESCE(keyword_library.asins, '[]'::jsonb) ||
                                COALESCE(EXCLUDED.asins, '[]'::jsonb)
                            ) AS a
                        ),
                        last_search_volume = COALESCE(EXCLUDED.last_search_volume, keyword_library.last_search_volume),
                        last_organic_rank  = COALESCE(EXCLUDED.last_organic_rank,  keyword_library.last_organic_rank),
                        last_acos          = COALESCE(EXCLUDED.last_acos,          keyword_library.last_acos),
                        last_spend         = COALESCE(EXCLUDED.last_spend,         keyword_library.last_spend),
                        last_impressions   = COALESCE(EXCLUDED.last_impressions,   keyword_library.last_impressions),
                        last_clicks        = COALESCE(EXCLUDED.last_clicks,        keyword_library.last_clicks),
                        last_orders        = COALESCE(EXCLUDED.last_orders,        keyword_library.last_orders),
                        last_source_kind   = COALESCE(EXCLUDED.last_source_kind,   keyword_library.last_source_kind),
                        meta               = keyword_library.meta || COALESCE(EXCLUDED.meta, '{}'::jsonb)
                    """,
                    (
                        workspace_id, norm, keyword, match_type,
                        json.dumps(asin_list), search_volume, organic_rank,
                        acos, spend, impressions, clicks, orders,
                        source_kind, source_kind, json.dumps(meta or {}),
                    ),
                )
            conn.commit()
        return norm
    except Exception as exc:
        logger.warning("keyword upsert failed for %r: %s", keyword, exc)
        return None


# ---------------------------------------------------------------------------
# outcome_events writes (daily observations)
# ---------------------------------------------------------------------------


_OUTCOME_METRICS = (
    # Marketing observations (keyword-attached or campaign-attached)
    "search_volume", "organic_rank", "acos", "spend",
    "impressions", "clicks", "orders", "ctr", "cvr",
    # Sales / unit-economics observations (ASIN-attached, no keyword required).
    # Written by substrate/unit_economics.py via record_sales_observations().
    # Included in this whitelist because keyword-observation paths may also
    # surface these metrics; the sales path uses its own writer but shares
    # the same outcome_events table and metric vocabulary.
    "revenue", "units_sold", "sessions", "returns", "return_rate",
    "buy_box_pct", "ad_spend", "ad_revenue",
)


def record_keyword_observations(
    workspace_id: str,
    observations: Iterable[dict[str, Any]],
    *,
    source_kind: str,
    source_file_hash: Optional[str] = None,
    observed_at: Optional[datetime] = None,
) -> dict[str, int]:
    """Bulk-write keyword observations into outcome_events.

    Each item in `observations` is a dict with at least:
        keyword (str)        — required, will be normalised
        asin (str)           — the ASIN the metric is attached to (or "" if none)
        metrics (dict)       — { metric_name: value }, only known metrics are kept

    Returns a counts dict: { keywords_written, outcome_rows, skipped }.

    Side effects:
    - upserts keyword_library for every distinct keyword (best-effort)
    - inserts one outcome_events row per (keyword, asin, metric) tuple

    Never raises. Failures count toward `skipped`.
    """
    pool = get_pool()
    if pool is None:
        return {"keywords_written": 0, "outcome_rows": 0, "skipped": 0}
    ts = observed_at or _now()
    rows_to_insert: list[tuple] = []
    kw_to_asins: dict[str, set[str]] = {}
    skipped = 0
    for obs in observations:
        kw = (obs.get("keyword") or "").strip()
        asin = (obs.get("asin") or "").strip()
        metrics = obs.get("metrics") or {}
        norm = normalise_keyword(kw)
        if not norm:
            skipped += 1
            continue
        if asin:
            kw_to_asins.setdefault(norm, set()).add(asin)
        else:
            kw_to_asins.setdefault(norm, set())
        for metric, value in metrics.items():
            if metric not in _OUTCOME_METRICS:
                continue
            if value is None:
                continue
            try:
                value_f = float(value)
            except (TypeError, ValueError):
                skipped += 1
                continue
            rows_to_insert.append((
                str(uuid.uuid4()), workspace_id, asin or "_unattached", ts,
                metric, value_f, source_file_hash, source_kind,
                kw, None, None, json.dumps({"keyword_norm": norm}),
            ))
    keywords_written = 0
    try:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # 1) Upsert keyword_library rows. Use the last observation's
                #    metrics for the last_* fields (best-effort).
                for obs in observations:
                    norm = normalise_keyword(obs.get("keyword") or "")
                    if not norm:
                        continue
                    metrics = obs.get("metrics") or {}
                    asin = (obs.get("asin") or "").strip()
                    asins_for_kw = list(kw_to_asins.get(norm, set()))
                    res = upsert_keyword(
                        workspace_id=workspace_id,
                        keyword=obs.get("keyword") or norm,
                        match_type=obs.get("match_type"),
                        asins=asins_for_kw,
                        search_volume=metrics.get("search_volume"),
                        organic_rank=metrics.get("organic_rank"),
                        acos=metrics.get("acos"),
                        spend=metrics.get("spend"),
                        impressions=metrics.get("impressions"),
                        clicks=metrics.get("clicks"),
                        orders=metrics.get("orders"),
                        source_kind=source_kind,
                    )
                    if res:
                        keywords_written += 1
                # 2) Bulk-insert outcome_events. Dedup at the SQL layer would
                #    require a uniqueness constraint we don't have, so we just
                #    append. Re-uploading the same file twice will duplicate.
                if rows_to_insert:
                    cur.executemany(
                        """
                        INSERT INTO outcome_events (
                            outcome_id, workspace_id, asin, observed_at,
                            metric, value, source_file_hash, source_kind,
                            keyword, campaign_id, ad_group_id, meta
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                        """,
                        rows_to_insert,
                    )
            conn.commit()
    except Exception as exc:
        logger.warning("keyword observations write failed: %s", exc)
        return {"keywords_written": keywords_written,
                "outcome_rows": 0, "skipped": skipped + len(rows_to_insert)}
    return {"keywords_written": keywords_written,
            "outcome_rows": len(rows_to_insert),
            "skipped": skipped}


# ---------------------------------------------------------------------------
# Read API
# ---------------------------------------------------------------------------


def list_keywords(
    workspace_id: str,
    limit: int = 50,
    offset: int = 0,
    *,
    q: Optional[str] = None,
    asin: Optional[str] = None,
    order_by: str = "last_seen_at",
) -> dict[str, Any]:
    """Paginated keyword_library reader.

    order_by: 'last_seen_at' | 'last_acos' | 'last_spend' | 'keyword'
    """
    pool = get_pool()
    if pool is None:
        return {"keywords": [], "total": 0}
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    where = ["workspace_id = %s"]
    params: list[Any] = [workspace_id]
    if q:
        where.append("keyword_norm ILIKE %s")
        params.append(f"%{normalise_keyword(q)}%")
    if asin:
        where.append("asins @> %s::jsonb")
        params.append(json.dumps([asin]))
    where_sql = " AND ".join(where)
    order_col = {
        "last_seen_at": "last_seen_at DESC NULLS LAST",
        "last_acos":    "last_acos DESC NULLS LAST",
        "last_spend":   "last_spend DESC NULLS LAST",
        "keyword":      "keyword ASC",
    }.get(order_by, "last_seen_at DESC NULLS LAST")
    rows: list[dict[str, Any]] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM keyword_library WHERE {where_sql}",
                        tuple(params))
            total = int(cur.fetchone()[0])
            cur.execute(
                f"""
                SELECT keyword_norm, keyword, match_type,
                       created_at, last_seen_at,
                       asins,
                       last_search_volume, last_organic_rank, last_acos,
                       last_spend, last_impressions, last_clicks, last_orders,
                       first_source_kind, last_source_kind, meta
                FROM keyword_library
                WHERE {where_sql}
                ORDER BY {order_col}
                LIMIT %s OFFSET %s
                """,
                tuple(params) + (limit, offset),
            )
            cols = [d[0] for d in cur.description]
            for r in cur:
                d = dict(zip(cols, r))
                for k in ("created_at", "last_seen_at"):
                    if d.get(k) is not None and hasattr(d[k], "isoformat"):
                        d[k] = d[k].isoformat()
                rows.append(d)
    return {"keywords": rows, "total": total}


def _trend_summary(values_with_ts: list[tuple[datetime, float]]) -> dict[str, Any]:
    """Compute a plain-language direction summary from a time series.

    Returns:
        { n, first, last, delta, pct_delta, direction, confidence_label }

    confidence_label:
      'no_data'       — n == 0
      'prior_only'    — n == 1 (just the baseline)
      'too_early'     — n < 3
      'directional'   — 3 <= n < 8
      'established'   — 8 <= n < 14
      'statistical'   — n >= 14
    """
    if not values_with_ts:
        return {"n": 0, "direction": "no_data", "confidence_label": "no_data"}
    values_with_ts = sorted(values_with_ts, key=lambda p: p[0])
    n = len(values_with_ts)
    first = values_with_ts[0][1]
    last = values_with_ts[-1][1]
    delta = last - first
    pct = (delta / first) * 100.0 if first not in (0, None) else None
    if n == 1:
        direction = "single_point"
        confidence = "prior_only"
    elif abs(delta) < 1e-9:
        direction = "flat"
        confidence = "directional" if n >= 3 else "too_early"
    else:
        direction = "up" if delta > 0 else "down"
        if n >= 14:    confidence = "statistical"
        elif n >= 8:   confidence = "established"
        elif n >= 3:   confidence = "directional"
        else:          confidence = "too_early"
    return {
        "n": n,
        "first": first, "last": last,
        "delta": delta,
        "pct_delta": pct,
        "direction": direction,
        "confidence_label": confidence,
        "first_observed_at": values_with_ts[0][0].isoformat() if hasattr(values_with_ts[0][0], "isoformat") else str(values_with_ts[0][0]),
        "last_observed_at":  values_with_ts[-1][0].isoformat() if hasattr(values_with_ts[-1][0], "isoformat") else str(values_with_ts[-1][0]),
    }


def get_keyword_direction(
    workspace_id: str,
    *,
    keyword: Optional[str] = None,
    keyword_norm: Optional[str] = None,
    asin: Optional[str] = None,
    since: Optional[datetime] = None,
    metrics: tuple[str, ...] = ("acos", "spend", "organic_rank", "clicks", "orders"),
) -> dict[str, Any]:
    """Return a per-metric direction summary for a keyword (+ optional ASIN).

    Pulls from outcome_events, groups by metric, and runs _trend_summary
    on each metric independently. Honest about data density (n=1 => prior_only).
    """
    pool = get_pool()
    if pool is None:
        return {"metrics": {}, "n_observations": 0}
    if not keyword and not keyword_norm:
        return {"metrics": {}, "n_observations": 0}
    norm = keyword_norm or normalise_keyword(keyword or "")
    if not norm:
        return {"metrics": {}, "n_observations": 0}
    where = ["workspace_id = %s",
             "(meta->>'keyword_norm' = %s OR LOWER(keyword) = %s)"]
    params: list[Any] = [workspace_id, norm, norm]
    if asin:
        where.append("asin = %s")
        params.append(asin)
    if since:
        where.append("observed_at >= %s")
        params.append(since)
    if metrics:
        placeholders = ",".join(["%s"] * len(metrics))
        where.append(f"metric IN ({placeholders})")
        params.extend(metrics)
    series_by_metric: dict[str, list[tuple[datetime, float]]] = {}
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT metric, observed_at, value
                FROM outcome_events
                WHERE {' AND '.join(where)}
                ORDER BY observed_at ASC
                """,
                tuple(params),
            )
            for metric, observed_at, value in cur:
                if value is None:
                    continue
                series_by_metric.setdefault(metric, []).append((observed_at, float(value)))
    summaries = {m: _trend_summary(pts) for m, pts in series_by_metric.items()}
    n_obs = sum(len(pts) for pts in series_by_metric.values())
    return {
        "keyword_norm": norm,
        "asin": asin,
        "metrics": summaries,
        "n_observations": n_obs,
    }


__all__ = [
    "normalise_keyword",
    "upsert_keyword",
    "record_keyword_observations",
    "list_keywords",
    "get_keyword_direction",
]
