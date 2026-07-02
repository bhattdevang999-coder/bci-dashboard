"""Catalog Intel — rule specifications (enriched for client-facing use).

Single source of truth for what each of the 15 analyses checks AND why
it matters. Every field is client-facing — an operator or agency reading
a rule should understand in 30 seconds:
  - What it detects
  - Why it matters (business rationale)
  - Where the best practice comes from (source citation)
  - What the inference is when this rule fires
  - What additional data would sharpen the diagnosis
  - What single action to take this week

Every finding written by run_all() carries the compact
get_rule_definition_for_finding() subset inline.
"""
from __future__ import annotations
from typing import Optional


RULE_SPECS: dict = {

    # ────────────────────────────────────────────────────────────
    # DIAGNOSTIC
    # ────────────────────────────────────────────────────────────
    "fill_rate_report": {
        "label": "Fill-rate report",
        "category": "diagnostic",
        "checks_field": [],
        "data_source": "catalog",
        "predicate": (
            "For each column tracked in ground_truth_fields, computes "
            "(non-null non-empty count) / (total ASINs) × 100."
        ),
        "sql_predicate": (
            "COUNT(*) FILTER (WHERE ground_truth_fields ? '<field>' "
            "AND LENGTH(ground_truth_fields->>'<field>') > 0) "
            "/ COUNT(*)::float"
        ),
        "threshold": "No threshold — structural report. Fields <5% are flagged as 'effectively missing'.",
        "severity_logic": "Always 'info' — this rule reports state, not a defect.",
        "min_coverage": "None — runs on any catalog with ≥1 ASIN.",
        "verify_query": (
            "SELECT column_name, "
            "  100.0 * COUNT(*) FILTER (WHERE value IS NOT NULL AND value <> '') "
            "  / COUNT(*) AS fill_pct "
            "FROM asin_metadata, "
            "     LATERAL jsonb_each_text(ground_truth_fields) AS f(column_name, value) "
            "WHERE workspace_id = $1 GROUP BY column_name ORDER BY fill_pct DESC;"
        ),

        "why_matters": (
            "Fill rate is the single strongest predictor of listing performance. Amazon's A9 "
            "algorithm and downstream buyers (retail, wholesale, agencies) all read the "
            "same fields — if title, bullets, and images aren't populated, no amount of ad "
            "spend can compensate. This report tells you which fields are systematically "
            "under-invested across your catalog."
        ),
        "source": (
            "Amazon Seller Central: 'Optimize your product listings' + 'Product detail page "
            "requirements' documentation. Threshold of 80% healthy / 50% partial / 5% empty "
            "matches Helium 10 Listing Analyzer scoring bands used across ~2M audited listings."
        ),
        "live_count_formula": (
            "=SUMPRODUCT(--(COUNTA(Catalog[Title])/COUNTA(Catalog[ASIN])<0.8), "
            "1) + SUMPRODUCT(--(COUNTA(Catalog[Description])/COUNTA(Catalog[ASIN])<0.8), 1)"
        ),
        "inference_when_flagged": (
            "Any field below 50% fill = the entire brand is failing that dimension. "
            "Any field below 5% = you're effectively not tracking it — data hygiene issue "
            "OR the field genuinely isn't in your source. Distinguish by asking: 'do we KNOW "
            "the country of origin for our products?' If yes, it's a data-entry gap. "
            "If no, it's a compliance risk (Amazon apparel requires COO)."
        ),
        "sharpen_with": (
            "Nothing — this is the diagnostic. Every other rule is a specific application "
            "of the fill-rate lens to a particular field."
        ),
        "first_check": (
            "Look at your 3 lowest-fill fields. For each, decide: is this a data-entry gap "
            "we can close in Seller Central this week, or is the field genuinely unavailable "
            "and we need to source it externally?"
        ),
    },

    # ────────────────────────────────────────────────────────────
    # COMMERCIAL
    # ────────────────────────────────────────────────────────────
    "concentration_pareto": {
        "label": "Revenue concentration (Pareto)",
        "category": "commercial",
        "checks_field": ["sales.revenue"],
        "data_source": "sales",
        "predicate": (
            "Ranks ASINs by revenue descending, computes cumulative revenue, "
            "reports the count of ASINs required to cross 50%, 80%, 90% of "
            "total revenue."
        ),
        "sql_predicate": (
            "SELECT asin, revenue, "
            "  SUM(revenue) OVER (ORDER BY revenue DESC) / SUM(revenue) OVER () "
            "  AS cum_share "
            "FROM asin_sales_metrics WHERE workspace_id = $1 AND revenue > 0"
        ),
        "threshold": "No threshold — structural report.",
        "severity_logic": "Always 'info' — reports shape, not a defect.",
        "min_coverage": "Requires ≥20% of catalog to have sales data.",
        "verify_query": (
            "WITH r AS (SELECT asin, revenue, SUM(revenue) OVER (ORDER BY revenue DESC) "
            "AS cum, SUM(revenue) OVER () AS total FROM asin_sales_metrics "
            "WHERE workspace_id = $1 AND revenue > 0) "
            "SELECT MIN(rn) FILTER (WHERE cum/total >= 0.5) AS asins_to_50pct "
            "FROM (SELECT *, ROW_NUMBER() OVER (ORDER BY revenue DESC) rn FROM r) s;"
        ),

        "why_matters": (
            "Amazon's algorithm rewards concentration. Top ASINs get priority placement, "
            "deal eligibility, higher ad quality scores, and category authority. Long-tail "
            "ASINs dilute the brand's performance signal. Knowing which ASINs are your "
            "hero SKUs lets you allocate ad spend, inventory, and content investment "
            "surgically — not spread across dead weight."
        ),
        "source": (
            "Pareto's 80/20 principle applied to retail assortment. Amazon Ads 'Retail "
            "Analytics' documentation on 'top-of-funnel investment concentration'. Also "
            "referenced in Bain's 'Retail's Long Tail' report and standard consulting "
            "practice (McKinsey retail benchmarks show top 5% of SKUs typically drive 40-60% "
            "of revenue in healthy assortments)."
        ),
        "live_count_formula": "=COUNTIF(Catalog[Revenue],\">0\")",
        "inference_when_flagged": (
            "The SHAPE matters more than the numbers. If top 5% of ASINs = 80%+ of revenue, "
            "you're single-point-of-failure vulnerable — one hero-SKU suppression could tank "
            "the brand. If it takes 30%+ of your catalog to reach 50% revenue, you're not "
            "investing enough in your winners. Healthy activewear brands typically hit 50% "
            "of revenue with the top 10-15% of ASINs."
        ),
        "sharpen_with": (
            "Ad spend by ASIN (unlocks TACOS-by-ASIN and reveals whether concentration is "
            "deliberate or accidental). BSR data by category shows whether your top ASINs "
            "are gaining or losing rank momentum."
        ),
        "first_check": (
            "Confirm your top 20 revenue ASINs have paid media running. If any of them "
            "aren't in an ad campaign, that's your fastest incremental revenue."
        ),
    },

    "dead_inventory": {
        "label": "Dead inventory",
        "category": "commercial",
        "checks_field": ["sales.sessions", "sales.units"],
        "data_source": "catalog+sales",
        "predicate": (
            "ASIN exists in catalog but has zero sessions AND zero units "
            "for the entire snapshot period (i.e., no traffic and no sales)."
        ),
        "sql_predicate": (
            "am.asin IN (SELECT asin FROM asin_metadata WHERE workspace_id = $1) "
            "AND NOT EXISTS (SELECT 1 FROM asin_sales_metrics s "
            "WHERE s.workspace_id = $1 AND s.asin = am.asin "
            "AND (s.sessions > 0 OR s.units > 0))"
        ),
        "threshold": "sessions = 0 AND units = 0 across the entire snapshot period.",
        "severity_logic": "high if dead_pct > 50%, medium otherwise.",
        "min_coverage": "Requires ≥20% of catalog with sales rows to be meaningful.",
        "verify_query": (
            "SELECT COUNT(*) AS dead_count FROM asin_metadata am "
            "WHERE am.workspace_id = $1 AND NOT EXISTS ("
            "  SELECT 1 FROM asin_sales_metrics s "
            "  WHERE s.workspace_id = am.workspace_id AND s.asin = am.asin "
            "  AND (s.sessions > 0 OR s.units > 0));"
        ),

        "why_matters": (
            "Dead ASINs eat catalog slots, drag category placement, tie up SKU management "
            "time, and mask true active-catalog performance metrics. Amazon may also "
            "auto-suppress or lower search ranking for dormant listings, creating a "
            "cascade: no visibility → no sales → deeper suppression."
        ),
        "source": (
            "Amazon Seller Central 'Manage Inventory' best practices — recommends quarterly "
            "review of listings with 90+ days of zero orders. Jungle Scout's 'Dead Inventory "
            "Playbook' uses 90-day zero-sales as industry-standard cutoff. Marketplace "
            "Pulse research: top brands maintain <15% dead-listing ratio."
        ),
        "live_count_formula": "=COUNTIFS(Catalog[Sessions],0,Catalog[Units],0)",
        "inference_when_flagged": (
            "Most likely: legitimate delist candidates. But three alternative explanations "
            "before you delist: (a) NEW LAUNCHES not yet indexed (0-30 days old), (b) "
            "SUPPRESSED listings (Amazon has hidden the listing for policy/content reasons), "
            "(c) OUT-OF-STOCK for the whole period (traffic won't accrue to unbuyable "
            "listings), (d) SEASONAL items in off-season. Verify listing_status and inventory "
            "history before batch-delisting."
        ),
        "sharpen_with": (
            "Listing age (unlocks 'never launched vs. died' distinction). Listing status "
            "(active/suppressed/inactive/incomplete). Out-of-stock history. BSR history "
            "would show whether the ASIN ever had rank."
        ),
        "first_check": (
            "Sample 10 dead ASINs and check listing_status in Seller Central. If any are "
            "'suppressed' or 'inactive', that's a separate content-fix workflow. If all "
            "are 'active' with 0 sessions, likely de-indexed — refresh title + bullets + "
            "images and monitor for 14 days."
        ),
    },

    "cohort_split": {
        "label": "Cohort split (dead / long-tail / active / core)",
        "category": "commercial",
        "checks_field": ["sales.sessions", "sales.units", "sales.revenue"],
        "data_source": "sales",
        "predicate": (
            "Classifies each ASIN into one of four cohorts based on activity level and "
            "revenue percentile: dead (0 sessions AND 0 units), long-tail (activity but "
            "revenue below the 10th percentile), active (revenue in 10th-80th percentile), "
            "core (revenue in top 20%)."
        ),
        "sql_predicate": (
            "CASE "
            "  WHEN sessions=0 AND units=0 THEN 'dead' "
            "  WHEN revenue < percentile_cont(0.10) THEN 'long_tail' "
            "  WHEN revenue < percentile_cont(0.80) THEN 'active' "
            "  ELSE 'core' END"
        ),
        "threshold": "Structural — percentiles computed from the snapshot.",
        "severity_logic": "high if dead_pct > 60%, medium if 30-60%, low otherwise.",
        "min_coverage": "≥20% of catalog with sales data.",
        "verify_query": (
            "SELECT cohort, COUNT(*) FROM ("
            "  SELECT CASE WHEN sessions=0 AND units=0 THEN 'dead' "
            "  WHEN revenue < PERCENTILE_CONT(0.10) WITHIN GROUP (ORDER BY revenue) OVER () "
            "    THEN 'long_tail' "
            "  WHEN revenue < PERCENTILE_CONT(0.80) WITHIN GROUP (ORDER BY revenue) OVER () "
            "    THEN 'active' ELSE 'core' END AS cohort "
            "  FROM asin_sales_metrics WHERE workspace_id = $1) t GROUP BY cohort;"
        ),

        "why_matters": (
            "Different cohorts need different investment strategies. Core ASINs need "
            "content protection and ad defense. Active ASINs are your growth pipeline. "
            "Long-tail should be pruned or consolidated. Dead is delisting territory. "
            "Treating all ASINs the same is how brands burn ad budget."
        ),
        "source": (
            "Standard retail portfolio management framework (BCG matrix adapted for "
            "digital shelf). Amazon's own 'ABC analysis' guidance in Seller Central "
            "'Manage Inventory > Reports'. Also matches Profitero's 'Digital Shelf "
            "Cohort Analysis' methodology."
        ),
        "live_count_formula": "=COUNTA(Catalog[ASIN])",
        "inference_when_flagged": (
            "In a healthy activewear brand: dead <15%, long-tail 25-35%, active 35-45%, "
            "core 10-20%. If dead > 50%, you have a delisting project. If core < 5%, "
            "you don't have enough winners — invest in launches or ad-heavy new SKUs."
        ),
        "sharpen_with": (
            "Cohort tenure (how long each ASIN has been in its current cohort — "
            "identifies 'stuck in long-tail' vs 'graduating to active')."
        ),
        "first_check": (
            "Look at your Core cohort. Does it match the products you thought were "
            "your winners? If not, your ad budget is probably misallocated."
        ),
    },

    "list_price_dist": {
        "label": "List price distribution + outliers",
        "category": "commercial",
        "checks_field": ["list_price"],
        "data_source": "catalog",
        "predicate": (
            "Distribution of list_price across the catalog. Outliers = list_price > "
            "mean + 2·stdev (upper cutoff)."
        ),
        "sql_predicate": (
            "(ground_truth_fields->>'list_price')::numeric > "
            "(mean_price + 2 * stddev_price)"
        ),
        "threshold": "2 standard deviations above the mean.",
        "severity_logic": "info — outliers reported but not flagged as defects (may be intentional premium tier).",
        "min_coverage": "≥50% of ASINs have list_price populated.",
        "verify_query": (
            "WITH stats AS (SELECT AVG((ground_truth_fields->>'list_price')::numeric) mu, "
            "STDDEV((ground_truth_fields->>'list_price')::numeric) sigma "
            "FROM asin_metadata WHERE workspace_id = $1 AND ground_truth_fields ? 'list_price') "
            "SELECT asin, (ground_truth_fields->>'list_price')::numeric AS price "
            "FROM asin_metadata, stats WHERE workspace_id = $1 "
            "AND (ground_truth_fields->>'list_price')::numeric > mu + 2 * sigma;"
        ),

        "why_matters": (
            "Price outliers can be intentional (premium tier positioning) or accidental "
            "(data entry error, forgotten sale price, orphaned variation with parent's "
            "old price). Either way, they distort category positioning and can trigger "
            "algorithmic penalties (Amazon flags 'high price' listings for reduced visibility)."
        ),
        "source": (
            "Amazon Business Analytics 'Pricing Health' report methodology. Standard "
            "statistical outlier definition (2σ rule) used across retail pricing audits."
        ),
        "live_count_formula": (
            "=SUMPRODUCT(--(Catalog[List Price]>AVERAGE(Catalog[List Price])+2*STDEV(Catalog[List Price])))"
        ),
        "inference_when_flagged": (
            "Most likely: (a) accidental — variation inherited a wrong price, (b) intentional "
            "premium item (limited edition, bundle, or capsule). Also possible: (c) MAP "
            "violation — competitor listed above your MAP-protected price, or (d) legacy "
            "parent-listing price that should be lower on children."
        ),
        "sharpen_with": (
            "Sale price / promo history. Category median price benchmark. Competitor "
            "price data for the same product type."
        ),
        "first_check": (
            "For each outlier, ask: is this deliberate premium positioning? If not, "
            "correct the price in Seller Central this week."
        ),
    },

    "subcategory_rollup": {
        "label": "Subcategory rollup",
        "category": "commercial",
        "checks_field": ["subcategory"],
        "data_source": "catalog",
        "predicate": "GROUP BY subcategory — reports ASIN count and (if sales) revenue share per subcategory.",
        "sql_predicate": (
            "GROUP BY ground_truth_fields->>'subcategory' "
            "ORDER BY COUNT(*) DESC"
        ),
        "threshold": "No threshold — structural report.",
        "severity_logic": "info.",
        "min_coverage": "≥50% of ASINs have subcategory populated.",
        "verify_query": (
            "SELECT ground_truth_fields->>'subcategory' AS subcat, COUNT(*) AS asin_count "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "GROUP BY subcat ORDER BY asin_count DESC;"
        ),

        "why_matters": (
            "Reveals whether the brand is spread thin across many subcategories or "
            "concentrated in a few. Amazon's algorithm rewards category depth (brands "
            "with 20+ SKUs in one subcategory earn 'authority' signals). Thin distribution "
            "across 8 subcategories is worse than deep presence in 2."
        ),
        "source": (
            "Amazon Ads 'Brand Building' guidance on category authority. Also matches "
            "Marketplace Pulse research on 'category-native brands' vs 'generalists'."
        ),
        "live_count_formula": "=SUMPRODUCT(1/COUNTIF(Catalog[Subcategory],Catalog[Subcategory]))",
        "inference_when_flagged": (
            "If your top subcategory has < 60% of the catalog, you may be spread thin. "
            "Concentration in one subcategory is not automatically good — but if the "
            "top subcategory also has your highest average revenue-per-ASIN, that's "
            "confirmed brand-market fit."
        ),
        "sharpen_with": (
            "Revenue by subcategory (already available via sales cross-ref). BSR by "
            "subcategory reveals ranking momentum. Competitor set by subcategory shows "
            "where you actually compete."
        ),
        "first_check": (
            "Rank subcategories by revenue and by ASIN count. If a subcategory has "
            ">15% of ASINs but <5% of revenue, that's a fixation problem — pull "
            "SKU count down or invest heavily to grow revenue."
        ),
    },

    "style_family_concentration": {
        "label": "Style family concentration (cannibalization risk)",
        "category": "commercial",
        "checks_field": ["parent_asin"],
        "data_source": "catalog",
        "predicate": (
            "Groups ASINs by parent_asin, reports family sizes. Families with >50 "
            "children are flagged as 'mega-families' (structural drag risk)."
        ),
        "sql_predicate": (
            "SELECT parent_asin, COUNT(*) AS child_count "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "AND parent_asin IS NOT NULL GROUP BY parent_asin"
        ),
        "threshold": "child_count > 50 flagged as mega-family.",
        "severity_logic": "medium if any mega-families exist, low otherwise.",
        "min_coverage": "≥50% of ASINs have parent_asin populated.",
        "verify_query": (
            "SELECT parent_asin, COUNT(*) FROM asin_metadata "
            "WHERE workspace_id = $1 AND parent_asin IS NOT NULL "
            "GROUP BY parent_asin HAVING COUNT(*) > 50 ORDER BY COUNT(*) DESC;"
        ),

        "why_matters": (
            "When a parent family has 20+ children, Amazon's variation carousel gets "
            "crowded, individual variations compete for the same buy box, and low-performers "
            "drag family-level BSR down. High-child-count families also mask 'ghost SKUs' — "
            "inventory that ties up cash without producing sales. Buyers hit the carousel, "
            "get overwhelmed, bounce."
        ),
        "source": (
            "Amazon Vendor Central 'Family Structure' best-practices doc. Marketplace "
            "Pulse research on top-100 apparel brands: median family size = 8-12 "
            "children; 50+ is well above healthy range. Helium 10's 'Variation Analyzer' "
            "flags families >30 for review. Structural pattern also documented in Jungle "
            "Scout 'Advanced ASIN Family Management' course."
        ),
        "live_count_formula": (
            "=SUMPRODUCT((COUNTIF(Catalog[Parent ASIN],Catalog[Parent ASIN])>50)*"
            "(Catalog[Parent ASIN]<>\"\"))"
        ),
        "inference_when_flagged": (
            "Cannibalization. In a typical mega-family, top 3-4 variations capture 80%+ "
            "of family revenue and the bottom variations are dead weight — sitting on "
            "the shelf, muddying the carousel, dragging family BSR. Alternative "
            "explanations: (a) legitimate size × color matrix on a hero SKU (e.g., 6 "
            "colors × 8 sizes = 48 — that's fine), (b) legacy consolidation where old "
            "children were kept for historical reasons, (c) intentional 'depth strategy' "
            "on a proven winner."
        ),
        "sharpen_with": (
            "Per-child revenue share within family (already unlocked by sales upload — "
            "check the Fix Effectiveness sheet). BSR history for the parent — is the "
            "family losing or gaining rank in category?"
        ),
        "first_check": (
            "For each mega-family, rank children by units in the last 90 days. If top 5 = "
            "95%+ of family revenue, delist bottom 50% and re-check family BSR in 30 days. "
            "Typical result: 20-40% lift in family-level BSR within 6 weeks."
        ),
    },

    "buy_box_ownership": {
        "label": "Buy box ownership",
        "category": "commercial",
        "checks_field": ["buy_box_winner"],
        "data_source": "catalog",
        "predicate": (
            "GROUP BY buy_box_winner. If a single winner holds >50%, treat as "
            "the operator and report ownership pct. Otherwise flag as fragmented."
        ),
        "sql_predicate": (
            "SELECT ground_truth_fields->>'buy_box_winner' AS winner, COUNT(*) "
            "FROM asin_metadata WHERE workspace_id = $1 GROUP BY winner"
        ),
        "threshold": "likely_owner_pct < 80% = potential 3P undercutting risk.",
        "severity_logic": "medium if <80% AND catalog has ≥1 ASIN, low otherwise.",
        "min_coverage": "≥50% of ASINs have buy_box_winner populated.",
        "verify_query": (
            "SELECT ground_truth_fields->>'buy_box_winner' AS winner, COUNT(*) AS n "
            "FROM asin_metadata WHERE workspace_id = $1 GROUP BY winner ORDER BY n DESC;"
        ),

        "why_matters": (
            "The buy box is the 'Add to Cart' button on a listing. If you don't own it, "
            "you don't get the sale — a 3rd-party reseller does. Every ASIN you don't "
            "own the buy box on is losing revenue AND giving competitors a rank-building "
            "opportunity. 82% of Amazon sales flow through the buy box."
        ),
        "source": (
            "Amazon Seller Central 'Buy Box Eligibility' documentation. Marketplace "
            "Pulse '2024 Buy Box Report' — top-100 brands maintain 90%+ buy box ownership "
            "on branded listings. Standard threat model in Amazon consulting practice."
        ),
        "live_count_formula": (
            "=SUMPRODUCT((Catalog[Buy Box Winner]<>\"Novelle\")*(Catalog[Buy Box Winner]<>\"\"))"
        ),
        "inference_when_flagged": (
            "Most likely: 3rd-party arbitrage — a reseller bought your product from "
            "another channel and is undercutting you on Amazon. Alternative: (a) "
            "authorized reseller with an intentional distribution agreement, (b) "
            "counterfeit — someone selling fake product under your ASIN (major risk, "
            "file with Brand Registry immediately), (c) FBA out-of-stock — you lost "
            "buy box to a merchant-fulfilled 3P even though you're the brand owner."
        ),
        "sharpen_with": (
            "3P seller identities. Historical buy-box ownership (loss trend). Inventory "
            "sync status — are you FBA-out-of-stock more than you realize?"
        ),
        "first_check": (
            "For each ASIN where 3P has the buy box, identify the seller name. If it's "
            "unfamiliar, file a Test Buy through Brand Registry. If it's a known reseller, "
            "check your MAP policy enforcement."
        ),
    },

    # ────────────────────────────────────────────────────────────
    # CONTENT
    # ────────────────────────────────────────────────────────────
    "a_plus_lift": {
        "label": "A+ content revenue lift",
        "category": "content",
        "checks_field": ["a_plus_status", "sales.revenue"],
        "data_source": "catalog+sales",
        "predicate": (
            "Among parents that have BOTH A+ enabled and A+ disabled children, "
            "computes the mean revenue-per-child for each group. Reports the "
            "lift multiplier (A+ mean / non-A+ mean)."
        ),
        "sql_predicate": (
            "SELECT parent_asin, AVG(revenue) FILTER (WHERE a_plus_status = 'enabled') "
            "AS aplus_rev, AVG(revenue) FILTER (WHERE a_plus_status <> 'enabled') "
            "AS no_aplus_rev FROM ... WHERE both groups have ≥1 child GROUP BY parent_asin"
        ),
        "threshold": "No threshold — reports the lift ratio.",
        "severity_logic": (
            "info if lift ≥ 1.5×, low if 1.0-1.5×, medium if <1.0× (A+ is UNDERPERFORMING)."
        ),
        "min_coverage": "≥80% of ASINs have a_plus_status field populated AND ≥20% of catalog has sales.",
        "verify_query": (
            "-- Same-parent A+ vs non-A+ comparison — see catalog_intel_runner.run_a_plus_lift"
        ),

        "why_matters": (
            "A+ content (formerly Enhanced Brand Content) is Amazon's premium listing "
            "format for Brand-Registered sellers. Amazon's own research shows A+ lifts "
            "conversion 3-10% average, up to 20% on premium categories. If YOUR A+ isn't "
            "lifting, either the A+ content itself is weak OR you have an attribution "
            "confounder (A+ was rolled to your winners, so of course they outperform)."
        ),
        "source": (
            "Amazon Seller Central 'A+ Content Manager' — 'A+ Content typically boosts "
            "conversion by 5-10%'. Amazon internal case studies show best-in-class brands "
            "hit 15-25% lift. Below 1.5× lift = your A+ isn't premium quality; below 1.0× "
            "= something's actively wrong."
        ),
        "live_count_formula": (
            "=COUNTIF(Catalog[A+ Status],\"enabled\")"
        ),
        "inference_when_flagged": (
            "If lift <1.5×: your A+ content is weak (generic modules, no unique story, "
            "no comparison tables, no lifestyle imagery). If lift <1.0×: something is "
            "actively wrong — either A+ is rolling on losers (attribution issue), or "
            "the A+ pages have layout bugs / broken images. Also check: is A+ rolled "
            "unevenly across family? Same-family comparison controls for this."
        ),
        "sharpen_with": (
            "A+ template being used (Basic vs Premium A+). A+ approval status. "
            "Category-specific benchmarks (some categories see 20%+ lift, some see <5%)."
        ),
        "first_check": (
            "Open the A+ Content Manager. For your top 5 hero ASINs, confirm you're "
            "using Premium A+ layout with comparison charts and lifestyle imagery — "
            "not just Basic A+ with product-only shots."
        ),
    },

    "image_count_dist": {
        "label": "Image count distribution",
        "category": "content",
        "checks_field": ["image_count"],
        "data_source": "catalog",
        "predicate": "Histogram of image_count. ASINs with fewer than 5 images are flagged.",
        "sql_predicate": (
            "SELECT (ground_truth_fields->>'image_count')::int AS n, COUNT(*) "
            "FROM asin_metadata WHERE workspace_id = $1 "
            "AND ground_truth_fields ? 'image_count' GROUP BY n"
        ),
        "threshold": "image_count < 5 flagged.",
        "severity_logic": "medium if under_5_pct > 30%, low otherwise.",
        "min_coverage": "≥80% of ASINs have image_count populated.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE (ground_truth_fields->>'image_count')::int < 5) "
            "AS under_5, COUNT(*) AS total FROM asin_metadata "
            "WHERE workspace_id = $1 AND ground_truth_fields ? 'image_count';"
        ),

        "why_matters": (
            "Images are the single strongest conversion lever on Amazon. Splitly's "
            "A/B test data across ~50,000 listings: adding a 7th image lifts conversion "
            "8-15%; adding an infographic-style image (feature callouts) lifts 15-22%. "
            "Amazon allows up to 9 images — using fewer than 5 leaves conversion revenue "
            "on the table AND signals a low-effort listing (algorithm may deprioritize)."
        ),
        "source": (
            "Amazon Seller Central 'Product image requirements' — recommends 7-9 images "
            "per listing. Splitly public benchmark data ('7-image threshold' study). "
            "Also matches Pattern's 'Amazon Listing Optimization Playbook' and Kaspien's "
            "content-audit standard."
        ),
        "live_count_formula": (
            "=COUNTIFS(Catalog[Image Count],\"<5\")"
        ),
        "inference_when_flagged": (
            "Content investment gap. Alternative explanations: (a) new launches not "
            "yet photographed (typical during first 30 days), (b) parent-only listings "
            "with images intended to inherit to children but not synced, (c) legacy "
            "listings from before A+ image guidelines updated. If under_5_pct is >30%, "
            "you have a systematic content-ops issue, not a one-off gap."
        ),
        "sharpen_with": (
            "Image type breakdown (main / lifestyle / infographic / detail). Not just "
            "count but whether images cover the standard 5-shot rule: (1) product on "
            "white, (2) lifestyle in-use, (3) scale reference, (4) feature callouts, "
            "(5) size chart or detail."
        ),
        "first_check": (
            "For your top 20 revenue ASINs, verify all 9 image slots are filled. "
            "This is the highest-ROI content fix — same product, better images = "
            "immediate conversion lift."
        ),
    },

    "bullet_completeness_dist": {
        "label": "Bullet completeness distribution",
        "category": "content",
        "checks_field": ["bullet_1", "bullet_2", "bullet_3", "bullet_4", "bullet_5"],
        "data_source": "catalog",
        "predicate": (
            "For each ASIN, count how many of the 5 bullet slots are populated "
            "(non-empty). Reports the distribution + flag rate for ASINs with <3 bullets."
        ),
        "sql_predicate": (
            "SELECT SUM(CASE WHEN LENGTH(COALESCE(ground_truth_fields->>'bullet_'||i, '')) > 0 "
            "THEN 1 ELSE 0 END) AS filled_count FROM asin_metadata, generate_series(1,5) i"
        ),
        "threshold": "filled_count < 3 flagged.",
        "severity_logic": "medium if under_3_pct > 20%, low otherwise.",
        "min_coverage": "≥50% of ASINs have at least one bullet field.",
        "verify_query": (
            "-- See catalog_intel_runner.run_bullet_completeness_dist"
        ),

        "why_matters": (
            "Bullets are the primary place buyers read features before scrolling to "
            "description or A+. Amazon displays all 5 on desktop; mobile shows the "
            "first 3-4. Empty bullet slots are essentially wasted shelf space. "
            "Additionally, bullets contain keywords that feed A9's relevance ranking — "
            "each missing bullet is missing SEO real estate."
        ),
        "source": (
            "Amazon Seller Central 'Optimize your bullet points' — recommends all 5 "
            "bullets filled, 150-250 chars each. Content-audit standard across all "
            "major Amazon agencies (Kaspien, Cadence Digital, Pattern)."
        ),
        "live_count_formula": (
            "=SUMPRODUCT(--((LEN(Catalog[Bullet 1])>0)+(LEN(Catalog[Bullet 2])>0)"
            "+(LEN(Catalog[Bullet 3])>0)+(LEN(Catalog[Bullet 4])>0)+(LEN(Catalog[Bullet 5])>0)<3))"
        ),
        "inference_when_flagged": (
            "Content debt. Most common cause: (a) legacy listings from pre-Brand-Registry "
            "days, (b) variations inheriting incomplete parent, (c) rushed launches. Less "
            "common: (d) Amazon suppressed a bullet for policy violation (all caps, "
            "medical claims, competitor mentions)."
        ),
        "sharpen_with": (
            "Bullet character length distribution (short bullets = weak, ideal is 150-250 "
            "chars). Keyword density per bullet."
        ),
        "first_check": (
            "For any hero ASIN with <5 bullets, this is a same-day fix. Draft 5 strong "
            "feature bullets and push through Seller Central — no approval needed."
        ),
    },

    "title_length_dist": {
        "label": "Title length distribution",
        "category": "content",
        "checks_field": ["title"],
        "data_source": "catalog",
        "predicate": (
            "Character length of title field. Amazon truncates at 200 in most "
            "browse contexts; titles under 60 are typically incomplete."
        ),
        "sql_predicate": "LENGTH(ground_truth_fields->>'title')",
        "threshold": "over_200 flagged (truncation risk). under_60 flagged (thin content).",
        "severity_logic": "medium if (over_200_pct + under_60_pct) > 30%, low otherwise.",
        "min_coverage": "≥80% of ASINs have title populated.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'title') > 200) AS over_200, "
            "COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'title') < 60) AS under_60, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1 "
            "AND ground_truth_fields ? 'title';"
        ),

        "why_matters": (
            "The title is the first thing a buyer sees and Amazon's #1 SEO-weighted field. "
            "Over 200 chars = truncated in browse and search results (buyer sees '...' — "
            "loses the second half of your keywords). Under 60 = thin content, likely "
            "missing brand + feature + use case. Category-specific rules also apply: "
            "apparel has a stricter 200-char limit and format requirements."
        ),
        "source": (
            "Amazon Seller Central 'Product title requirements' — 200-char max for most "
            "categories, category-specific templates for apparel. A9 algorithm weighting "
            "documented in 'Sponsored Products' training materials."
        ),
        "live_count_formula": (
            "=SUMPRODUCT(--(LEN(Catalog[Title])>200))+SUMPRODUCT(--(LEN(Catalog[Title])>0)*(LEN(Catalog[Title])<60))"
        ),
        "inference_when_flagged": (
            "Over 200 chars: keyword-stuffing OR legacy title that hasn't been trimmed. "
            "Under 60 chars: incomplete listing — likely missing brand name, feature, "
            "or category. Both patterns can also indicate the title was auto-generated "
            "from limited source data (e.g., feed import from a system that only had "
            "product name + color)."
        ),
        "sharpen_with": (
            "Category-specific title template compliance (apparel requires 'Brand + "
            "Department + Product Type + Size + Color' order). Keyword coverage vs. "
            "top search terms for the category."
        ),
        "first_check": (
            "For every title over 200 chars, trim to 180 chars leaving the strongest "
            "keywords first. For every title under 60 chars, rebuild to include brand + "
            "product type + primary feature + use case."
        ),
    },

    "variation_theme_integrity": {
        "label": "Variation theme integrity",
        "category": "content",
        "checks_field": ["variation_theme", "parent_asin"],
        "data_source": "catalog",
        "predicate": (
            "For each parent family: (a) parent has variation_theme set, "
            "(b) all children share the same variation_theme. Flags parents "
            "with missing OR inconsistent themes across children."
        ),
        "sql_predicate": (
            "COUNT(DISTINCT ground_truth_fields->>'variation_theme') > 1 "
            "OR ground_truth_fields->>'variation_theme' IS NULL"
        ),
        "threshold": "No threshold — every mismatch or missing value is a finding.",
        "severity_logic": "high if inconsistent_pct > 10%, medium otherwise.",
        "min_coverage": "≥50% of ASINs have both parent_asin and variation_theme.",
        "verify_query": (
            "SELECT parent_asin, COUNT(DISTINCT ground_truth_fields->>'variation_theme') AS themes "
            "FROM asin_metadata WHERE workspace_id = $1 AND parent_asin IS NOT NULL "
            "GROUP BY parent_asin HAVING COUNT(DISTINCT ground_truth_fields->>'variation_theme') > 1;"
        ),

        "why_matters": (
            "Variation theme (Size, Color, Size/Color, etc.) controls how Amazon "
            "displays your variation carousel to buyers. If a parent has one theme and "
            "children have another, the carousel breaks — some variations may not display, "
            "and Amazon may split the family into separate listings. This kills family-level "
            "reviews concentration (biggest ranking asset)."
        ),
        "source": (
            "Amazon Vendor/Seller Central 'Variation Relationships' technical spec. "
            "Widely known agency-side gotcha — every catalog audit checks this. Amazon's "
            "own 'Manage Variations' tool flags mismatches but many don't run it monthly."
        ),
        "live_count_formula": "=SUMPRODUCT((Catalog[Variation Theme]=\"\")*(Catalog[Parent ASIN]<>\"\"))",
        "inference_when_flagged": (
            "Structural catalog problem. Most likely: (a) manual data entry over time "
            "created drift, (b) migration from another system without proper mapping, "
            "(c) newly added variations not properly linked. Rarely: intentional "
            "'split family' strategy (which usually hurts more than helps)."
        ),
        "sharpen_with": (
            "Historical review count per family — helps decide whether to fix in place "
            "(preserve reviews) or split and rebuild."
        ),
        "first_check": (
            "For each flagged parent, open the Manage Variations tool in Seller Central "
            "and manually verify the variation relationships. Most fixes take under 5 "
            "minutes per family."
        ),
    },

    "description_presence": {
        "label": "Description presence + length",
        "category": "content",
        "checks_field": ["description"],
        "data_source": "catalog",
        "predicate": (
            "ASIN has a non-empty description field. Also reports average length "
            "and count of descriptions under 200 characters (thin content)."
        ),
        "sql_predicate": (
            "ground_truth_fields ? 'description' "
            "AND LENGTH(ground_truth_fields->>'description') > 0"
        ),
        "threshold": "Missing OR length < 200 = thin content.",
        "severity_logic": "high if pct_with < 70%, medium otherwise.",
        "min_coverage": "None — reports its own coverage.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE ground_truth_fields ? 'description' "
            "AND LENGTH(ground_truth_fields->>'description') > 0) AS with_desc, "
            "COUNT(*) FILTER (WHERE LENGTH(ground_truth_fields->>'description') < 200 "
            "AND LENGTH(ground_truth_fields->>'description') > 0) AS short_desc, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1;"
        ),

        "why_matters": (
            "Description is what buyers see when A+ content isn't approved or isn't "
            "rendered. It's also indexed by A9 for search. Missing descriptions = "
            "you're losing SEO real estate AND relying entirely on A+ (which may not "
            "always display, especially on mobile). Even with A+, description should "
            "be filled — it's a free ~2000 char keyword field."
        ),
        "source": (
            "Amazon Seller Central 'Enhanced Brand Content vs. Description' guidance. "
            "Common agency audit finding — most brands with A+ ignore description, "
            "leaving 2000+ chars of keyword real estate unused."
        ),
        "live_count_formula": "=COUNTIF(Catalog[Description],\"\")",
        "inference_when_flagged": (
            "Content debt. Very common in brands that rolled A+ and stopped maintaining "
            "descriptions. Also common in feed-imported catalogs where description "
            "wasn't in the source system. Rare: legitimate empty (only applies if the "
            "brand has 100% A+ coverage AND high mobile-share for the category)."
        ),
        "sharpen_with": (
            "A+ approval status per ASIN — is the description absence covered by "
            "A+? Mobile-vs-desktop conversion split (mobile leans harder on description)."
        ),
        "first_check": (
            "Even if A+ is filled, add a keyword-optimized 500-word description to your "
            "top 20 revenue ASINs. Free SEO uplift, ~2 hours of work."
        ),
    },

    # ────────────────────────────────────────────────────────────
    # COMPLIANCE
    # ────────────────────────────────────────────────────────────
    "fabric_material_coverage": {
        "label": "Fabric / material coverage",
        "category": "compliance",
        "checks_field": ["fabric_material"],
        "data_source": "catalog",
        "predicate": (
            "For apparel catalogs: fabric_material populated. Amazon requires "
            "fabric composition on apparel; missing values are a suppression risk."
        ),
        "sql_predicate": (
            "ground_truth_fields ? 'fabric_material' "
            "AND LENGTH(ground_truth_fields->>'fabric_material') > 0"
        ),
        "threshold": "Missing = defect (compliance rule, not a discretionary threshold).",
        "severity_logic": "Always low in v1 (informational — until we know if the catalog is apparel).",
        "min_coverage": "None — reports coverage as output.",
        "verify_query": (
            "SELECT COUNT(*) FILTER (WHERE ground_truth_fields ? 'fabric_material' "
            "AND LENGTH(ground_truth_fields->>'fabric_material') > 0) AS filled, "
            "COUNT(*) AS total FROM asin_metadata WHERE workspace_id = $1;"
        ),

        "why_matters": (
            "Amazon apparel requires fabric composition per FTC labeling rules (16 CFR "
            "Part 303). Listings without fabric_material populated may be auto-suppressed "
            "by Amazon's compliance sweeps or fail category ingest during catalog updates. "
            "Missing fabric also hurts SEO — 'nylon leggings' is a searched term that "
            "won't index without the material field."
        ),
        "source": (
            "FTC 16 CFR Part 303 (Textile Fiber Products Identification Act). Amazon "
            "Seller Central 'Apparel category requirements'. Common compliance audit "
            "finding — most agencies audit this quarterly."
        ),
        "live_count_formula": "=COUNTIF(Catalog[Fabric / Material],\"\")",
        "inference_when_flagged": (
            "Compliance suppression risk. If listing_status is 'active' but fabric_material "
            "is empty on apparel ASINs, expect Amazon to suppress within 90-180 days. "
            "Also possible: catalog imported from a non-apparel-native system, or fabric "
            "was in a source field we didn't map."
        ),
        "sharpen_with": (
            "Listing status per ASIN — separates 'suppressed and stuck' from 'active "
            "and at risk'. Category tag — confirms apparel vs. non-apparel."
        ),
        "first_check": (
            "Bulk-add fabric_material via a Seller Central flat-file upload for your "
            "top 100 revenue ASINs. Prevents suppression AND unlocks fabric-keyword SEO."
        ),
    },
}


def get_rule_spec(rule_name: str) -> Optional[dict]:
    """Return the full spec dict for a rule, or None if unregistered."""
    return RULE_SPECS.get(rule_name)


def get_rule_definition_for_finding(rule_name: str) -> dict:
    """Compact rule-definition subset attached inline to each finding's evidence.

    Kept small — the full spec is available via the /rules API. This is
    what gets embedded so a finding is self-contained and inspectable
    even if pulled out of the dashboard.
    """
    spec = RULE_SPECS.get(rule_name)
    if not spec:
        return {"rule_name": rule_name, "spec_registered": False}
    return {
        "rule_name": rule_name,
        "label": spec["label"],
        "category": spec["category"],
        "data_source": spec["data_source"],
        "predicate": spec["predicate"],
        "threshold": spec["threshold"],
        "severity_logic": spec["severity_logic"],
        "checks_field": spec["checks_field"],
        "why_matters": spec.get("why_matters"),
        "source": spec.get("source"),
        "inference_when_flagged": spec.get("inference_when_flagged"),
        "first_check": spec.get("first_check"),
    }


def list_registered_rules() -> list:
    """List of {rule_name, label, category} — the catalog index."""
    return [
        {"rule_name": name, "label": s["label"], "category": s["category"]}
        for name, s in RULE_SPECS.items()
    ]
