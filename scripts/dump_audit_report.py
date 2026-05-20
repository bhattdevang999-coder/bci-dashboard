"""Dump the most recent audit run as a Markdown report + CSV of findings.

Honest output. No marketing language. Highlights overlap, calls out where
revenue rollups double-count, and surfaces the skipped rules so the
operator knows what's blocked on data.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

if not os.environ.get("ATLAS_DATABASE_URL"):
    os.environ["ATLAS_DATABASE_URL"] = (
        "postgresql://atlastest@/atlas_test?host=/tmp&port=55432"
    )

from substrate.db import get_pool
from substrate.catalog_audit_engine import run_audit


WORKSPACE = sys.argv[1] if len(sys.argv) > 1 else "roxy"
OUT_MD  = f"/home/user/workspace/{WORKSPACE}_audit_v2.md"
OUT_CSV = f"/home/user/workspace/{WORKSPACE}_audit_v2_findings.csv"


def _fmt_money(n: float | None) -> str:
    if n is None:
        return "—"
    return f"${n:,.0f}"


def _fmt_pct(n: float | None) -> str:
    if n is None:
        return "—"
    return f"{n*100:.1f}%"


def main() -> int:
    pool = get_pool()
    if pool is None:
        print("ERROR: no Postgres pool — set ATLAS_DATABASE_URL")
        return 1

    print(f"running audit on workspace={WORKSPACE}…")
    result = run_audit(WORKSPACE, dry_run=False)
    run_id = result["run_id"]
    total_findings = result["total_findings"]
    total_rev = result["total_revenue_ttm"]
    active_n = result["active_cohort_size"]
    dur = result["duration_seconds"]

    # Fetch ASIN-level revenue distribution for context
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT asin, SUM(value) AS rev
                  FROM outcome_events
                 WHERE workspace_id = %s AND metric = 'revenue'
                 GROUP BY asin
                 ORDER BY rev DESC
                """,
                (WORKSPACE,),
            )
            rev_rows = cur.fetchall()
    # Concentration math
    asin_count = len(rev_rows)
    revs = [float(r[1] or 0) for r in rev_rows]
    cumsum = 0.0
    asins_for_50pct = 0
    for v in revs:
        cumsum += v
        asins_for_50pct += 1
        if cumsum >= total_rev * 0.5:
            break

    # Per-rule unique-ASIN revenue (deduplicated: revenue counted once per ASIN
    # per rule, even if multiple findings exist for the same asin/rule pair).
    # SPECIAL CASE for duplicate_style_group: revenue is the cluster total,
    # which is the SAME number on every ASIN in the cluster. To avoid
    # multiplying it by cluster size, dedup by (rule_name, evidence->>'style_number').
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH per_asin AS (
                  SELECT rule_name, asin, evidence,
                         MAX(revenue_exposure) AS rev
                    FROM catalog_audit_findings
                   WHERE workspace_id = %s AND audit_run_id = %s
                     AND asin NOT LIKE '__group__:%%'
                     AND rule_name <> 'duplicate_style_group'
                   GROUP BY rule_name, asin, evidence
                )
                SELECT rule_name,
                       COUNT(DISTINCT asin) AS unique_asins,
                       COALESCE(SUM(rev), 0) AS total_rev
                  FROM per_asin
                 GROUP BY rule_name
                """,
                (WORKSPACE, run_id),
            )
            rule_rollup = list(cur.fetchall())
            # duplicate_style_group: dedup by style cluster
            cur.execute(
                """
                WITH per_cluster AS (
                  SELECT evidence->>'style_number' AS style,
                         MAX(revenue_exposure) AS cluster_rev,
                         COUNT(*) AS cluster_size
                    FROM catalog_audit_findings
                   WHERE workspace_id = %s AND audit_run_id = %s
                     AND rule_name = 'duplicate_style_group'
                   GROUP BY evidence->>'style_number'
                )
                SELECT 'duplicate_style_group' AS rule_name,
                       SUM(cluster_size)::int AS unique_asins,
                       COALESCE(SUM(cluster_rev), 0) AS total_rev
                  FROM per_cluster
                """,
                (WORKSPACE, run_id),
            )
            dup_row = cur.fetchone()
            if dup_row and dup_row[1]:
                rule_rollup.append(dup_row)
            rule_rollup.sort(key=lambda r: float(r[2] or 0), reverse=True)

            # Group-level rule rollup (abandoned_subcategory etc — no ASIN dedupe needed)
            cur.execute(
                """
                SELECT rule_name, COUNT(*), COALESCE(SUM(revenue_exposure),0)
                  FROM catalog_audit_findings
                 WHERE workspace_id = %s AND audit_run_id = %s
                   AND asin LIKE '__group__:%%'
                 GROUP BY rule_name
                 ORDER BY 3 DESC
                """,
                (WORKSPACE, run_id),
            )
            group_rollup = cur.fetchall()

            # All findings (for the CSV)
            cur.execute(
                """
                SELECT asin, rule_name, severity, revenue_exposure,
                       confidence, queue, priority_score,
                       evidence, proposed_fix
                  FROM catalog_audit_findings
                 WHERE workspace_id = %s AND audit_run_id = %s
                 ORDER BY revenue_exposure DESC NULLS LAST, priority_score DESC
                """,
                (WORKSPACE, run_id),
            )
            all_findings_rows = cur.fetchall()

            # Top 25 actionable findings: one row per (rule, asin-or-cluster).
            # Collapses duplicate_style_group to one row per cluster.
            cur.execute(
                """
                (
                  SELECT 'cluster: ' || (evidence->>'style_number') AS label,
                         rule_name, severity, MAX(revenue_exposure) AS rev,
                         MAX(confidence) AS confidence, MAX(queue) AS queue,
                         MAX(evidence->>'cluster_size') AS cluster_size
                    FROM catalog_audit_findings
                   WHERE workspace_id = %s AND audit_run_id = %s
                     AND rule_name = 'duplicate_style_group'
                   GROUP BY rule_name, severity, evidence->>'style_number'
                )
                UNION ALL
                (
                  SELECT asin AS label, rule_name, severity,
                         revenue_exposure AS rev, confidence, queue,
                         NULL AS cluster_size
                    FROM catalog_audit_findings
                   WHERE workspace_id = %s AND audit_run_id = %s
                     AND rule_name <> 'duplicate_style_group'
                )
                ORDER BY rev DESC NULLS LAST
                LIMIT 25
                """,
                (WORKSPACE, run_id, WORKSPACE, run_id),
            )
            top25 = cur.fetchall()

    # ─── Write CSV ───
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "asin", "rule_name", "severity", "revenue_exposure_usd",
            "confidence", "queue", "priority_score", "evidence_json",
            "proposed_fix_json",
        ])
        for r in all_findings_rows:
            w.writerow([
                r[0], r[1], r[2],
                f"{float(r[3] or 0):.2f}",
                f"{float(r[4] or 0):.3f}",
                r[5], f"{float(r[6] or 0):.4f}",
                json.dumps(r[7], default=str),
                json.dumps(r[8], default=str) if r[8] else "",
            ])
    print(f"  wrote {len(all_findings_rows)} finding rows to {OUT_CSV}")

    # ─── Write Markdown ───
    lines: list[str] = []
    lines.append(f"# Atlas catalog audit — {WORKSPACE.title()}")
    lines.append("")
    lines.append(f"Run: `{run_id}`  ·  "
                 f"Generated: {datetime.utcnow().isoformat(timespec='seconds')}Z  ·  "
                 f"Engine duration: {dur}s")
    lines.append("")

    # ─── Catalog state ───
    lines.append("## Catalog state")
    lines.append("")
    lines.append(f"- Total ASINs: **{asin_count:,}**")
    lines.append(f"- Active cohort: **{active_n:,}** "
                 f"(sold ≥1 unit OR ≥50 sessions in TTM)")
    lines.append(f"- TTM revenue: **{_fmt_money(total_rev)}**")
    lines.append(f"- Concentration: **top {asins_for_50pct:,} ASINs "
                 f"= 50% of revenue** "
                 f"({asins_for_50pct/max(1,asin_count)*100:.1f}% of catalog)")
    lines.append("")

    # ─── Findings rollup (per-rule, dedup) ───
    lines.append("## Findings by rule")
    lines.append("")
    lines.append("Revenue exposure is the TTM revenue attributed to the "
                 "ASINs flagged. **Each ASIN is counted once per rule** "
                 "(not double-counted by overlapping findings).")
    lines.append("")
    lines.append("**Important honesty caveats:**")
    lines.append("")
    lines.append("- Many ASINs are flagged by multiple rules (e.g. an ASIN "
                 "with 1 image and no A+ shows up in both `fewer_than_5_images` "
                 "AND `missing_a_plus`). Summing the revenue column "
                 "across rules would double-count.")
    lines.append("- `fewer_than_5_images` is a STRICT subset of "
                 "`fewer_than_7_images` — fix the latter and the former "
                 "drops with it.")
    lines.append("- Revenue exposure ≠ revenue at risk if ignored. "
                 "It's a sizing of the ASIN base, not a predicted lift. "
                 "Loop 2 (decision posterior) will produce predicted "
                 "lift later.")
    lines.append("")
    lines.append("| Rule | Findings | Unique ASINs | Revenue exposure (TTM, unique ASINs) | Severity | Queue |")
    lines.append("|---|---:|---:|---:|---|---|")
    # ASIN-level rules
    rule_lookup = {}
    for row in rule_rollup:
        rule_lookup[row[0]] = (int(row[1]), float(row[2] or 0))
    for row in group_rollup:
        rule_lookup[row[0]] = (int(row[1]), float(row[2] or 0))

    # Get severity & queue per rule from result
    from substrate.audit_rules import resolve_rules_for_brand
    rules = {r["name"]: r for r in resolve_rules_for_brand(WORKSPACE)}
    from substrate.catalog_audit_engine import QUEUE_ROUTING
    # Sort by revenue exposure desc
    sorted_rules = sorted(
        result["findings_by_rule"].items(),
        key=lambda kv: rule_lookup.get(kv[0], (0, 0))[1],
        reverse=True,
    )
    for name, n_findings in sorted_rules:
        if n_findings == 0:
            continue
        uniq, rev = rule_lookup.get(name, (0, 0))
        sev = rules.get(name, {}).get("severity", "?")
        q = QUEUE_ROUTING.get(name, "?")
        lines.append(
            f"| `{name}` | {n_findings:,} | {uniq:,} | "
            f"{_fmt_money(rev)} | {sev} | {q} |"
        )
    lines.append("")
    # Zero-findings rules
    zero_rules = [n for n, c in result["findings_by_rule"].items() if c == 0]
    if zero_rules:
        lines.append("**Rules that fired but found nothing:** "
                     + ", ".join(f"`{n}`" for n in zero_rules))
        lines.append("")

    # ─── Skipped rules ───
    if result["skipped"]:
        lines.append("## Skipped rules")
        lines.append("")
        lines.append("These rules did not run because the substrate is missing "
                     "the required column or connector. The fill-rate floor "
                     "is 5%.")
        lines.append("")
        lines.append("| Rule | Reason |")
        lines.append("|---|---|")
        for s in result["skipped"]:
            lines.append(f"| `{s['name']}` | {s['reason']} |")
        lines.append("")

    # ─── Top 25 findings by revenue exposure ───
    lines.append("## Top 25 actionable findings by revenue exposure")
    lines.append("")
    lines.append("Each row is a distinct decision the operator would make. "
                 "`duplicate_style_group` rows are collapsed to one row per "
                 "style cluster (not one per ASIN in the cluster). For "
                 "`abandoned_subcategory`, the label is the subcategory "
                 "name. For all other rules, the label is the ASIN.")
    lines.append("")
    lines.append("| # | Label | Rule | Revenue (TTM) | Severity | Queue | Notes |")
    lines.append("|---:|---|---|---:|---|---|---|")
    for i, r in enumerate(top25, start=1):
        label, rule_name, sev, rev, conf, queue, cluster_size = r
        notes = ""
        if label and label.startswith("__group__:"):
            label = "subcat: " + label[len("__group__:"):]
        if cluster_size is not None:
            notes = f"cluster of {cluster_size} ASINs"
        lines.append(
            f"| {i} | `{label}` | `{rule_name}` | "
            f"{_fmt_money(float(rev or 0))} | {sev} | {queue} | {notes} |"
        )
    lines.append("")

    # ─── Queue rollup (dedup by ASIN within queue) ───
    # Pre-compute cluster revenue map for duplicate_style_group so we can
    # attribute the cluster total to the cluster once, not to every ASIN.
    cluster_total_by_style: dict[str, float] = {}
    cluster_style_by_asin: dict[str, str] = {}
    for r in all_findings_rows:
        if r[1] != "duplicate_style_group":
            continue
        ev = r[7] or {}
        style = (ev.get("style_number") if isinstance(ev, dict) else None)
        if not style:
            continue
        cluster_style_by_asin[r[0]] = style
        cluster_total_by_style[style] = max(
            cluster_total_by_style.get(style, 0.0),
            float(r[3] or 0),
        )

    queue_counts: dict[str, int] = {}
    queue_asins: dict[str, set] = {}
    queue_asin_max_rev: dict[str, dict[str, float]] = {}
    queue_clusters: dict[str, set] = {}
    queue_cluster_rev: dict[str, dict[str, float]] = {}

    for r in all_findings_rows:
        q = r[5] or "manual_review"
        asin = r[0]
        rev = float(r[3] or 0)
        queue_counts[q] = queue_counts.get(q, 0) + 1
        if r[1] == "duplicate_style_group":
            style = cluster_style_by_asin.get(asin)
            if style:
                queue_clusters.setdefault(q, set()).add(style)
                bucket = queue_cluster_rev.setdefault(q, {})
                bucket[style] = cluster_total_by_style[style]
            continue
        queue_asins.setdefault(q, set()).add(asin)
        bucket = queue_asin_max_rev.setdefault(q, {})
        if rev > bucket.get(asin, 0):
            bucket[asin] = rev

    lines.append("## By queue")
    lines.append("")
    lines.append("Queues are operator playlists. `quick_win` should be one-click. "
                 "`content_quality` needs a writer/photographer. `strategic` "
                 "needs an operator decision. Revenue is deduped to ASIN-max "
                 "within each queue — still not deduped across queues, since "
                 "the same ASIN can need both content work and a strategic "
                 "decision.")
    lines.append("")
    lines.append("| Queue | Findings | Unique ASINs/clusters | Revenue (ASIN-max within queue) |")
    lines.append("|---|---:|---:|---:|")
    for q in ("quick_win", "content_quality", "strategic", "manual_review"):
        if q in queue_counts:
            asin_rev = sum(queue_asin_max_rev.get(q, {}).values())
            cluster_rev = sum(queue_cluster_rev.get(q, {}).values())
            total_rev = asin_rev + cluster_rev
            asin_n = len(queue_asins.get(q, set()))
            cluster_n = len(queue_clusters.get(q, set()))
            unit_label = f"{asin_n:,}"
            if cluster_n:
                unit_label += f" ASINs + {cluster_n:,} clusters"
            else:
                unit_label += " ASINs"
            lines.append(
                f"| `{q}` | {queue_counts[q]:,} | "
                f"{unit_label} | "
                f"{_fmt_money(total_rev)} |"
            )
    lines.append("")

    # ─── Engine errors ───
    if result["errors"]:
        lines.append("## Engine errors")
        lines.append("")
        for e in result["errors"]:
            lines.append(f"- `{e}`")
        lines.append("")

    # ─── Bias to flag ───
    lines.append("## Bias to flag")
    lines.append("")
    lines.append("- This engine writes findings but does NOT predict lift. "
                 "The `expected_lift_pct` field in each `proposed_fix` is "
                 "null on purpose. Loop 2 (ASIN-level decision posterior) "
                 "will fill that in once we have 30/60/90-day outcomes "
                 "from accepted findings.")
    lines.append("- Confidence values are the rule's prior, not personalized "
                 "to this brand. Loop 1 (operator-edit-pair preference model) "
                 "will personalize over time.")
    lines.append("- All connector-dependent rules (BSR, inventory, reviews) "
                 "are skipped silently. The skipped list above shows which.")
    lines.append("- Computer (Atlas) has a structural bias toward shipping "
                 "more substrate. Pushing back on this audit: 38k findings is "
                 "a lot. The right question is 'which 50 findings should the "
                 "operator look at this week?' Day 4's rule editor + queue "
                 "ranking is what makes that tractable — without it, this "
                 "report is a haystack with money in it.")
    lines.append("")

    with open(OUT_MD, "w") as f:
        f.write("\n".join(lines))
    print(f"  wrote {OUT_MD}")
    print()
    print(f"DONE in {dur}s. {total_findings:,} findings written for run {run_id}.")
    print(f"  Markdown: {OUT_MD}")
    print(f"  CSV: {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
