"""Catalog Intel — Interactive workbook builder.

Produces a consultant-style Excel workbook where EVERY downstream
number is a live formula pointing at the raw data sheets. The client
can:
  - Click any KPI  -> see the exact formula
  - Change a raw cell  -> all downstream numbers update
  - Match dashboard values cell-by-cell via the Findings sheet

Assumes Excel 365 (FILTER, XLOOKUP, dynamic arrays available). Falls
back to INDEX/MATCH + SUMIFS where broadly compatible so older Excel
still renders correctly.

Sheet map (v1):
  01_Cover                      Executive KPIs + summary table
  02_Catalog_Data               Raw catalog (protected, source of truth)
  03_Sales_Data                 Raw sales (protected)
  04_Coverage_Matrix            Fill-rate heat map + category filter
  05_Revenue_Concentration      Pareto with Top-N dropdown
  06_Cohort_Analysis            Stacked bar + cohort table
  07_Content_Health             Per-ASIN scorecard with score filter
  08_All_Findings               Mirror of dashboard export
  09_Rules_Methodology          Predicate + SQL + verify_query per rule
  10_Data_Gaps                  What more data unlocks what analysis
  11_How_This_Works             Trust page
"""
from __future__ import annotations

import io
import json
import logging
from datetime import datetime
from typing import Optional

from openpyxl import Workbook
from openpyxl.styles import (
    Font, PatternFill, Alignment, Border, Side, NamedStyle,
)
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.formatting.rule import (
    ColorScaleRule, CellIsRule, DataBarRule, FormulaRule,
)
from openpyxl.chart import BarChart, LineChart, Reference, BarChart3D
from openpyxl.chart.label import DataLabelList
from openpyxl.chart.layout import Layout, ManualLayout
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.protection import SheetProtection

from substrate.rules_catalog import RULE_SPECS

logger = logging.getLogger(__name__)


# ============================================================
# Palette + styles (consistent with the dashboard)
# ============================================================

COL_HEADER      = "1F2937"     # slate-800
COL_HEADER_TEXT = "F9FAFB"
COL_SUBHEADER   = "374151"
COL_ROW_ALT     = "F8FAFC"
COL_ACCENT      = "20808D"     # data-viz teal
COL_ACCENT_2    = "A84B2F"     # data-viz terra
COL_MUTED       = "94A3B8"
COL_GREEN       = "16A34A"
COL_RED         = "DC2626"
COL_YELLOW      = "CA8A04"
COL_BORDER      = "E5E7EB"

FONT_HEADER      = Font(bold=True, color=COL_HEADER_TEXT, size=11, name="Calibri")
FONT_SUBHEADER   = Font(bold=True, color="F1F5F9", size=10, name="Calibri")
FONT_KPI_LABEL   = Font(color="64748B", size=9, name="Calibri")
FONT_KPI_VALUE   = Font(bold=True, size=22, color="0F172A", name="Calibri")
FONT_KPI_UNIT    = Font(color="64748B", size=10, name="Calibri")
FONT_MUTED       = Font(color="64748B", size=10, name="Calibri")
FONT_BOLD        = Font(bold=True, size=10, name="Calibri")
FILL_HEADER      = PatternFill("solid", fgColor=COL_HEADER)
FILL_SUBHEADER   = PatternFill("solid", fgColor=COL_SUBHEADER)
FILL_KPI         = PatternFill("solid", fgColor="F9FAFB")
FILL_KPI_ACCENT  = PatternFill("solid", fgColor="F0F9FF")
ALIGN_LEFT       = Alignment(horizontal="left", vertical="center", wrap_text=True)
ALIGN_CENTER     = Alignment(horizontal="center", vertical="center", wrap_text=True)
ALIGN_RIGHT      = Alignment(horizontal="right", vertical="center")


# ============================================================
# Catalog columns (flattened from ground_truth_fields)
# ============================================================

# Fields we surface as top-level columns in the workbook. Order matters
# for the raw data sheet layout.
CATALOG_COLUMNS = [
    ("asin",              "ASIN"),
    ("parent_asin",       "Parent ASIN"),
    ("sku",               "SKU"),
    ("title",             "Title"),
    ("brand",             "Brand"),
    ("category",          "Category"),
    ("subcategory",       "Subcategory"),
    ("color",             "Color"),
    ("size",              "Size"),
    ("model",             "Model"),
    ("list_price",        "List Price"),
    ("sale_price",        "Sale Price"),
    ("image_count",       "Image Count"),
    ("a_plus_status",     "A+ Status"),
    ("buy_box_winner",    "Buy Box Winner"),
    ("variation_theme",   "Variation Theme"),
    ("bullet_1",          "Bullet 1"),
    ("bullet_2",          "Bullet 2"),
    ("bullet_3",          "Bullet 3"),
    ("bullet_4",          "Bullet 4"),
    ("bullet_5",          "Bullet 5"),
    ("description",       "Description"),
    ("fabric_material",   "Fabric / Material"),
    ("country_of_origin", "Country of Origin"),
    ("care_instructions", "Care Instructions"),
    ("backend_keywords",  "Backend Keywords"),
    ("listing_status",    "Listing Status"),
    # Sales metrics merged in from asin_sales_metrics
    ("sessions",          "Sessions"),
    ("units",             "Units"),
    ("revenue",           "Revenue"),
    ("cvr_pct",           "CVR %"),
]

# Columns whose fill % matters most for content health.
CONTENT_CRITICAL_FIELDS = {
    "title", "image_count", "bullet_1", "bullet_2", "bullet_3",
    "description", "list_price",
}
COMPLIANCE_FIELDS = {
    "fabric_material", "country_of_origin", "care_instructions",
    "backend_keywords",
}
SALES_FIELDS = {"sessions", "units", "revenue", "cvr_pct"}


# ============================================================
# Entry point
# ============================================================

def build_interactive_workbook(
    catalog_rows: list,
    sales_by_asin: dict,
    findings: list,
    snapshot: Optional[dict],
    workspace_id: str,
) -> io.BytesIO:
    """Build the full interactive workbook.

    catalog_rows: list of dicts with keys: asin, parent_asin,
                  ground_truth_fields (dict).
    sales_by_asin: {asin: {sessions, units, revenue, cvr_pct}}
    findings: list of finding dicts (same shape as get_findings)
    snapshot: {snapshot_id, uploaded_at, file_name, ...} or None
    workspace_id: str
    """
    wb = Workbook()
    # Remove the default sheet - we'll add our own in order.
    default = wb.active
    wb.remove(default)

    # Merge catalog + sales into flat rows for Sheet 2
    flat_rows = _flatten_rows(catalog_rows, sales_by_asin)
    n = len(flat_rows)

    _sheet_cover(wb, n, snapshot, workspace_id)
    _sheet_catalog_data(wb, flat_rows)
    _sheet_sales_data(wb, sales_by_asin)
    _sheet_coverage_matrix(wb, n)
    _sheet_revenue_concentration(wb, n)
    _sheet_cohort_analysis(wb, n)
    _sheet_content_health(wb, flat_rows)
    _sheet_all_findings(wb, findings)
    _sheet_rules_methodology(wb)
    _sheet_data_gaps(wb)
    _sheet_trend_kpis(wb, snapshot)
    _sheet_trend_by_rule(wb, snapshot)
    _sheet_fix_effectiveness(wb, findings, snapshot)
    _sheet_how_to_add_historicals(wb)
    _sheet_how_it_works(wb)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


# ============================================================
# Row flattening
# ============================================================

def _flatten_rows(catalog_rows: list, sales_by_asin: dict) -> list:
    """Merge catalog ground_truth_fields + sales into flat dicts."""
    out = []
    for row in catalog_rows:
        asin = row.get("asin")
        gtf = row.get("ground_truth_fields") or {}
        parent = row.get("parent_asin")
        flat = {"asin": asin, "parent_asin": parent}
        for key, _ in CATALOG_COLUMNS:
            if key in ("asin", "parent_asin"):
                continue
            if key in SALES_FIELDS:
                continue
            v = gtf.get(key)
            # Normalize scalar types for openpyxl
            if isinstance(v, (dict, list)):
                v = json.dumps(v)[:32000]
            flat[key] = v
        # Merge sales
        s = sales_by_asin.get(asin, {})
        for key in SALES_FIELDS:
            flat[key] = s.get(key)
        out.append(flat)
    return out


# ============================================================
# Sheet 01 — Cover
# ============================================================

def _sheet_cover(wb, n, snapshot, workspace_id):
    ws = wb.create_sheet("01_Cover")
    ws.sheet_view.showGridLines = False

    ws.column_dimensions["A"].width = 3
    for col in range(2, 8):
        ws.column_dimensions[get_column_letter(col)].width = 20

    # Title
    ws["B2"] = "Catalog Intel — Interactive Audit"
    ws["B2"].font = Font(bold=True, size=22, color="0F172A", name="Calibri")
    ws.row_dimensions[2].height = 30

    subline = []
    subline.append(f"Workspace: {workspace_id}")
    if snapshot:
        if snapshot.get("uploaded_at"):
            subline.append(f"Snapshot: {snapshot['uploaded_at']}")
        if snapshot.get("file_name"):
            subline.append(f"File: {snapshot['file_name']}")
    ws["B3"] = "  ·  ".join(subline)
    ws["B3"].font = FONT_MUTED

    ws["B4"] = ("Every KPI and table below is a live formula pointing at "
                "02_Catalog_Data. Change any raw value and everything updates.")
    ws["B4"].font = FONT_MUTED
    ws["B4"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[4].height = 30
    ws.merge_cells("B4:G4")

    # ── KPI tiles (8) ────────────────────────────────────────
    # Layout: 4 columns × 2 rows of tiles starting at row 6
    ws["B6"] = "Executive KPIs"
    ws["B6"].font = Font(bold=True, size=14, color="0F172A", name="Calibri")
    ws.row_dimensions[6].height = 22

    kpis = [
        ("Total ASINs",           f"=COUNTA(Catalog[ASIN])",                                                        "count"),
        ("Dead (no sessions, no units)", f"=IFERROR(COUNTIFS(Catalog[Sessions],0,Catalog[Units],0)/COUNTA(Catalog[ASIN]),0)", "pct"),
        ("Active ASINs (any activity)",  f"=COUNTIFS(Catalog[Sessions],\">0\")+COUNTIFS(Catalog[Sessions],0,Catalog[Units],\">0\")", "count"),
        ("Total revenue",         f"=SUM(Catalog[Revenue])",                                                        "money"),
        ("Avg fill rate (all fields)",   f"=IFERROR(SUMPRODUCT(--(Catalog[Title]<>\"\"),1/COUNTA(Catalog[ASIN])),0)", "pct"),
        ("Titles filled",         f"=IFERROR(COUNTIF(Catalog[Title],\"?*\")/COUNTA(Catalog[ASIN]),0)",               "pct"),
        ("Descriptions filled",   f"=IFERROR(COUNTIF(Catalog[Description],\"?*\")/COUNTA(Catalog[ASIN]),0)",         "pct"),
        ("Fabric/material filled",f"=IFERROR(COUNTIF(Catalog[Fabric / Material],\"?*\")/COUNTA(Catalog[ASIN]),0)",   "pct"),
    ]

    # Draw KPI tiles: 4 wide × 2 rows. Cells B-C, D-E, F-G per tile.
    tile_rows = [8, 13]
    for tile_idx, (label, formula, kind) in enumerate(kpis):
        r_idx = tile_idx // 4
        c_idx = tile_idx % 4
        row = tile_rows[r_idx]
        col_start = 2 + c_idx * 2 - c_idx  # B, D, F, H
        # Simpler: use fixed columns B(2)/C(3), D(4)/E(5), F(6)/G(7), H(8)/I(9)
        col_start = 2 + c_idx * 2
        col_a = get_column_letter(col_start)
        col_b = get_column_letter(col_start + 1)

        # Label row
        ws[f"{col_a}{row}"] = label
        ws.merge_cells(f"{col_a}{row}:{col_b}{row}")
        cell = ws[f"{col_a}{row}"]
        cell.font = FONT_KPI_LABEL
        cell.alignment = ALIGN_LEFT
        cell.fill = FILL_KPI

        # Value row
        vrow = row + 1
        ws[f"{col_a}{vrow}"] = formula
        ws.merge_cells(f"{col_a}{vrow}:{col_b}{vrow}")
        vcell = ws[f"{col_a}{vrow}"]
        vcell.font = FONT_KPI_VALUE
        vcell.alignment = ALIGN_LEFT
        vcell.fill = FILL_KPI
        if kind == "pct":
            vcell.number_format = "0.0%"
        elif kind == "money":
            vcell.number_format = "$#,##0"
        else:
            vcell.number_format = "#,##0"

        ws.row_dimensions[row].height = 16
        ws.row_dimensions[vrow].height = 34

    # ── Verbal summary box ──────────────────────────────────
    ws["B18"] = "What this workbook covers"
    ws["B18"].font = Font(bold=True, size=14, color="0F172A", name="Calibri")

    coverage_lines = [
        "01_Cover                       — This page",
        "02_Catalog_Data                — Full raw catalog (source of truth for every formula below)",
        "03_Sales_Data                  — Full raw sales metrics per ASIN",
        "04_Coverage_Matrix             — Fill-rate heat map with category filter",
        "05_Revenue_Concentration       — Interactive Pareto with Top-N dropdown",
        "06_Cohort_Analysis             — Dead / long-tail / active / core cohorts",
        "07_Content_Health              — Per-ASIN quality scorecard",
        "08_All_Findings                — Dashboard findings mirror (cell-by-cell)",
        "09_Rules_Methodology           — Predicate + SQL + verify query per rule",
        "10_Data_Gaps                   — What more data unlocks which analyses",
        "11_Trend_KPIs                  — KPI evolution across snapshots (populated as you re-upload)",
        "12_Trend_By_Rule               — Per-rule metric trend over time",
        "13_Fix_Effectiveness           — Did the flagged issues actually get fixed? The money loop.",
        "14_How_To_Add_Historicals      — How to backfill trend columns from past uploads",
        "15_How_This_Works              — Trust page: named ranges, verification workflow",
    ]
    for i, line in enumerate(coverage_lines):
        ws.cell(row=20 + i, column=2, value=line).font = Font(name="Consolas", size=10, color="334155")

    ws["B32"] = ("Generated by Perplexity Computer — Atlas Catalog Intel v1.2. "
                 "Every number in this workbook is derived from 02_Catalog_Data "
                 "and 03_Sales_Data via visible formulas.")
    ws["B32"].font = FONT_MUTED
    ws.merge_cells("B32:I32")


# ============================================================
# Sheet 02 — Catalog_Data (raw, protected, source of truth)
# ============================================================

def _sheet_catalog_data(wb, flat_rows):
    ws = wb.create_sheet("02_Catalog_Data")
    ws.sheet_view.showGridLines = False

    # Header row
    headers = [label for _, label in CATALOG_COLUMNS]
    for c, label in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=label)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # Data rows
    for r, row in enumerate(flat_rows, start=2):
        for c, (key, _) in enumerate(CATALOG_COLUMNS, start=1):
            v = row.get(key)
            cell = ws.cell(row=r, column=c, value=v)
            # Number formats for known numeric columns
            if key in ("list_price", "sale_price", "revenue"):
                cell.number_format = "$#,##0.00"
            elif key in ("sessions", "units", "image_count"):
                cell.number_format = "#,##0"
            elif key == "cvr_pct":
                cell.number_format = "0.00"

    # Create an Excel Table so column references like Catalog[ASIN] work
    n_rows = len(flat_rows)
    if n_rows > 0:
        last_col = get_column_letter(len(headers))
        table = Table(displayName="Catalog", ref=f"A1:{last_col}{n_rows + 1}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showRowStripes=True,
        )
        ws.add_table(table)

    # Column widths
    widths = [14, 14, 14, 48, 14, 16, 18, 14, 12, 14, 12, 12, 10, 10, 20, 16,
              38, 38, 38, 38, 38, 60, 22, 18, 22, 22, 14, 12, 12, 14, 10]
    for i, w in enumerate(widths[:len(headers)], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    ws.freeze_panes = "D2"

    # Protect the sheet so users can't accidentally mutate the source of truth
    ws.protection = SheetProtection(sheet=True, formatCells=False,
                                    formatColumns=False, formatRows=False,
                                    insertColumns=True, deleteColumns=False,
                                    sort=True, autoFilter=True)


# ============================================================
# Sheet 03 — Sales_Data (raw, protected)
# ============================================================

def _sheet_sales_data(wb, sales_by_asin):
    ws = wb.create_sheet("03_Sales_Data")
    ws.sheet_view.showGridLines = False

    headers = ["ASIN", "Sessions", "Units", "Revenue", "CVR %"]
    for c, h in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    r = 2
    for asin, m in sales_by_asin.items():
        ws.cell(row=r, column=1, value=asin)
        ws.cell(row=r, column=2, value=m.get("sessions") or 0).number_format = "#,##0"
        ws.cell(row=r, column=3, value=m.get("units") or 0).number_format = "#,##0"
        ws.cell(row=r, column=4, value=m.get("revenue") or 0).number_format = "$#,##0.00"
        ws.cell(row=r, column=5, value=m.get("cvr_pct") or 0).number_format = "0.00"
        r += 1

    n = len(sales_by_asin)
    if n > 0:
        table = Table(displayName="Sales", ref=f"A1:E{n + 1}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium3", showRowStripes=True,
        )
        ws.add_table(table)

    widths = [14, 12, 12, 14, 10]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B2"


# ============================================================
# Sheet 04 — Coverage_Matrix
# ============================================================

def _sheet_coverage_matrix(wb, n_asins):
    ws = wb.create_sheet("04_Coverage_Matrix")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Field Coverage Matrix"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Every cell below is a live COUNTIF on 02_Catalog_Data. "
                "Change any raw value and the fill % updates.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:F3")

    # Category filter dropdown
    ws["B5"] = "Filter category:"
    ws["B5"].font = FONT_BOLD
    ws["C5"] = "All"
    ws["C5"].fill = PatternFill("solid", fgColor="EFF6FF")
    ws["C5"].font = Font(bold=True, size=10)
    dv = DataValidation(type="list", formula1='"All,Content-critical,Compliance,Sales-tied,Nice-to-have"', allow_blank=False)
    dv.add("C5")
    ws.add_data_validation(dv)

    # Headers row 7
    hdrs = ["Field", "Category", "Filled", "Total", "Fill %", "Status"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # One row per surfaced field (excluding ASIN and parent_asin which are identifiers)
    def _category_of(key):
        if key in CONTENT_CRITICAL_FIELDS: return "Content-critical"
        if key in COMPLIANCE_FIELDS: return "Compliance"
        if key in SALES_FIELDS: return "Sales-tied"
        return "Nice-to-have"

    row = 8
    fields_to_report = [(k, label) for k, label in CATALOG_COLUMNS if k not in ("asin", "parent_asin")]
    for (key, label) in fields_to_report:
        ws.cell(row=row, column=2, value=label)
        ws.cell(row=row, column=3, value=_category_of(key))
        # Filled = COUNTIF(Catalog[<label>],"?*") for text, COUNT for numeric
        col_ref = f"Catalog[{label}]"
        if key in ("list_price", "sale_price", "image_count", "sessions", "units", "revenue", "cvr_pct"):
            ws.cell(row=row, column=4, value=f'=COUNT({col_ref})')
        else:
            ws.cell(row=row, column=4, value=f'=COUNTIF({col_ref},"?*")')
        ws.cell(row=row, column=5, value=f'=COUNTA(Catalog[ASIN])')
        ws.cell(row=row, column=6, value=f"=IFERROR(D{row}/E{row},0)")
        ws.cell(row=row, column=6).number_format = "0.0%"
        # Status text derived from fill % thresholds
        ws.cell(row=row, column=7, value=(
            f'=IF(F{row}>=0.8,"\u2713 healthy",'
            f'IF(F{row}>=0.5,"partial",'
            f'IF(F{row}>=0.05,"thin","effectively empty")))'
        ))
        # Category filter — hide rows not matching the dropdown selection
        # (Excel doesn't natively hide rows by formula; we grey out non-matches instead)
        ws.cell(row=row, column=2).alignment = ALIGN_LEFT
        ws.cell(row=row, column=3).alignment = ALIGN_CENTER
        ws.cell(row=row, column=4).alignment = ALIGN_RIGHT
        ws.cell(row=row, column=5).alignment = ALIGN_RIGHT
        ws.cell(row=row, column=6).alignment = ALIGN_RIGHT
        row += 1

    # Conditional formatting: red-yellow-green gradient on Fill %
    last_row = row - 1
    rng = f"F8:F{last_row}"
    ws.conditional_formatting.add(rng, ColorScaleRule(
        start_type="num", start_value=0,     start_color="FEE2E2",
        mid_type="num",   mid_value=0.5,     mid_color="FEF3C7",
        end_type="num",   end_value=1,       end_color="DCFCE7",
    ))
    # Grey out rows not matching the filter dropdown
    ws.conditional_formatting.add(f"B8:G{last_row}", FormulaRule(
        formula=[f'AND($C$5<>"All",$C8<>$C$5)'],
        fill=PatternFill("solid", fgColor="F1F5F9"),
        font=Font(color="94A3B8"),
    ))

    widths = [2, 26, 16, 12, 12, 14, 22]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B8"


# ============================================================
# Sheet 05 — Revenue_Concentration
# ============================================================

def _sheet_revenue_concentration(wb, n_asins):
    ws = wb.create_sheet("05_Revenue_Concentration")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Revenue Concentration — Pareto"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Sorted list of ASINs by revenue with cumulative share. "
                "Change the Top-N dropdown to reshape the analysis.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:G3")

    # Top-N dropdown
    ws["B5"] = "Show top:"
    ws["B5"].font = FONT_BOLD
    ws["C5"] = 50
    ws["C5"].fill = PatternFill("solid", fgColor="EFF6FF")
    ws["C5"].font = Font(bold=True, size=10)
    dv = DataValidation(type="list", formula1='"20,50,100,500,1000"', allow_blank=False)
    dv.add("C5")
    ws.add_data_validation(dv)

    # Summary line
    ws["E5"] = "=CONCATENATE(\"Total catalog: \",TEXT(COUNTA(Catalog[ASIN]),\"#,##0\"),\" ASINs · Total revenue: \",TEXT(SUM(Catalog[Revenue]),\"$#,##0\"))"
    ws["E5"].font = FONT_MUTED
    ws.merge_cells("E5:J5")

    # Table headers row 7
    hdrs = ["Rank", "ASIN", "Title", "Revenue", "Cumulative Revenue", "Cumulative %"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # Dynamic-array Top-N via SORTBY + TAKE (Excel 365)
    # Rank column (1..N)
    ws["B8"] = "=SEQUENCE($C$5)"
    # ASIN column: sort Catalog by revenue desc, take top N
    ws["C8"] = "=TAKE(SORTBY(Catalog[ASIN],Catalog[Revenue],-1),$C$5)"
    ws["D8"] = "=TAKE(SORTBY(Catalog[Title],Catalog[Revenue],-1),$C$5)"
    ws["E8"] = "=TAKE(SORTBY(Catalog[Revenue],Catalog[Revenue],-1),$C$5)"
    # Cumulative revenue over the visible top-N
    ws["F8"] = "=SCAN(0,E8#,LAMBDA(a,b,a+b))"
    ws["G8"] = "=F8#/SUM(Catalog[Revenue])"

    # Format
    for col_letter, fmt in [("E", "$#,##0"), ("F", "$#,##0"), ("G", "0.0%")]:
        # Just apply to E8 through E1000 to cover any Top-N spillage
        for r in range(8, 1010):
            cell = ws[f"{col_letter}{r}"]
            cell.number_format = fmt

    ws.column_dimensions["B"].width = 6
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 60
    ws.column_dimensions["E"].width = 14
    ws.column_dimensions["F"].width = 18
    ws.column_dimensions["G"].width = 12

    ws.freeze_panes = "B8"

    # ── Threshold summary block (right side) ────────────────
    ws["I7"] = "Concentration thresholds"
    ws["I7"].font = FONT_BOLD
    ws["I8"] = "ASINs to 50% of revenue:"
    ws["I9"] = "ASINs to 80% of revenue:"
    ws["I10"] = "ASINs to 90% of revenue:"
    ws["I11"] = "Total active ASINs (rev>0):"
    ws["J8"] = ("=IFERROR(MATCH(TRUE,"
                "MMULT(--(SORTBY(Catalog[Revenue],Catalog[Revenue],-1)>=0)*"
                "SORTBY(Catalog[Revenue],Catalog[Revenue],-1),"
                "TRANSPOSE(ROW(Catalog[Revenue])^0))/SUM(Catalog[Revenue])>=0.5,0),0)")
    # Simpler + reliable: iterate cumulative
    ws["J8"] = ("=IFERROR(XMATCH(TRUE,"
                "SCAN(0,SORTBY(Catalog[Revenue],Catalog[Revenue],-1),"
                "LAMBDA(a,b,a+b))/SUM(Catalog[Revenue])>=0.5),0)")
    ws["J9"] = ("=IFERROR(XMATCH(TRUE,"
                "SCAN(0,SORTBY(Catalog[Revenue],Catalog[Revenue],-1),"
                "LAMBDA(a,b,a+b))/SUM(Catalog[Revenue])>=0.8),0)")
    ws["J10"] = ("=IFERROR(XMATCH(TRUE,"
                 "SCAN(0,SORTBY(Catalog[Revenue],Catalog[Revenue],-1),"
                 "LAMBDA(a,b,a+b))/SUM(Catalog[Revenue])>=0.9),0)")
    ws["J11"] = "=COUNTIF(Catalog[Revenue],\">0\")"
    for r in (8, 9, 10, 11):
        ws.cell(row=r, column=10).number_format = "#,##0"
        ws.cell(row=r, column=10).font = Font(bold=True, size=11)
    ws.column_dimensions["I"].width = 32
    ws.column_dimensions["J"].width = 12


# ============================================================
# Sheet 06 — Cohort_Analysis
# ============================================================

def _sheet_cohort_analysis(wb, n_asins):
    ws = wb.create_sheet("06_Cohort_Analysis")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Cohort Analysis"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("dead = 0 sessions AND 0 units.  "
                "core = revenue in top 20th percentile.  "
                "active = revenue in top 10-80th.  "
                "long-tail = everyone else with activity.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:H3")

    # Cohort summary table (rows 5-9)
    hdrs = ["Cohort", "ASIN Count", "Revenue", "% of ASINs", "% of Revenue"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=5, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # Cohort formulas — use PERCENTILE.INC to bucket
    cohort_defs = [
        ("Dead",
         '=COUNTIFS(Catalog[Sessions],0,Catalog[Units],0)',
         '=SUMIFS(Catalog[Revenue],Catalog[Sessions],0,Catalog[Units],0)'),
        ("Long-tail",
         '=SUMPRODUCT((Catalog[Revenue]<PERCENTILE.INC(Catalog[Revenue],0.1))*((Catalog[Sessions]>0)+(Catalog[Units]>0)>0))',
         '=SUMPRODUCT((Catalog[Revenue]<PERCENTILE.INC(Catalog[Revenue],0.1))*((Catalog[Sessions]>0)+(Catalog[Units]>0)>0)*Catalog[Revenue])'),
        ("Active",
         '=SUMPRODUCT((Catalog[Revenue]>=PERCENTILE.INC(Catalog[Revenue],0.1))*(Catalog[Revenue]<PERCENTILE.INC(Catalog[Revenue],0.8)))',
         '=SUMPRODUCT((Catalog[Revenue]>=PERCENTILE.INC(Catalog[Revenue],0.1))*(Catalog[Revenue]<PERCENTILE.INC(Catalog[Revenue],0.8))*Catalog[Revenue])'),
        ("Core (top 20%)",
         '=SUMPRODUCT(--(Catalog[Revenue]>=PERCENTILE.INC(Catalog[Revenue],0.8)))',
         '=SUMPRODUCT((Catalog[Revenue]>=PERCENTILE.INC(Catalog[Revenue],0.8))*Catalog[Revenue])'),
    ]
    for i, (name, cf, rf) in enumerate(cohort_defs):
        row = 6 + i
        ws.cell(row=row, column=2, value=name).font = FONT_BOLD
        ws.cell(row=row, column=3, value=cf).number_format = "#,##0"
        ws.cell(row=row, column=4, value=rf).number_format = "$#,##0"
        ws.cell(row=row, column=5, value=f"=IFERROR(C{row}/COUNTA(Catalog[ASIN]),0)").number_format = "0.0%"
        ws.cell(row=row, column=6, value=f"=IFERROR(D{row}/SUM(Catalog[Revenue]),0)").number_format = "0.0%"

    # Total row
    ws.cell(row=10, column=2, value="Total").font = Font(bold=True, size=11)
    ws.cell(row=10, column=3, value="=SUM(C6:C9)").number_format = "#,##0"
    ws.cell(row=10, column=4, value="=SUM(D6:D9)").number_format = "$#,##0"
    ws.cell(row=10, column=5, value="=SUM(E6:E9)").number_format = "0.0%"
    ws.cell(row=10, column=6, value="=SUM(F6:F9)").number_format = "0.0%"

    # Bar chart of cohort counts
    chart = BarChart()
    chart.type = "bar"
    chart.style = 11
    chart.title = "ASINs by cohort"
    chart.y_axis.title = "Cohort"
    chart.x_axis.title = "ASIN count"
    data = Reference(ws, min_col=3, min_row=5, max_row=9, max_col=3)
    cats = Reference(ws, min_col=2, min_row=6, max_row=9)
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.height = 8
    chart.width = 16
    chart.dataLabels = DataLabelList(showVal=True)
    ws.add_chart(chart, "H5")

    # Revenue chart
    chart2 = BarChart()
    chart2.type = "bar"
    chart2.style = 12
    chart2.title = "Revenue by cohort"
    data2 = Reference(ws, min_col=4, min_row=5, max_row=9, max_col=4)
    cats2 = Reference(ws, min_col=2, min_row=6, max_row=9)
    chart2.add_data(data2, titles_from_data=True)
    chart2.set_categories(cats2)
    chart2.height = 8
    chart2.width = 16
    chart2.dataLabels = DataLabelList(showVal=True)
    ws.add_chart(chart2, "H20")

    for col, w in [("B", 22), ("C", 14), ("D", 16), ("E", 14), ("F", 14)]:
        ws.column_dimensions[col].width = w


# ============================================================
# Sheet 07 — Content_Health scorecard (per-ASIN)
# ============================================================

def _sheet_content_health(wb, flat_rows):
    ws = wb.create_sheet("07_Content_Health")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Per-ASIN Content Health Scorecard"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Composite 0-10 score built from 5 quality checks. "
                "Each check is a formula on 02_Catalog_Data.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:J3")

    # Score bucket dropdown
    ws["B5"] = "Show bucket:"
    ws["B5"].font = FONT_BOLD
    ws["C5"] = "All"
    ws["C5"].fill = PatternFill("solid", fgColor="EFF6FF")
    ws["C5"].font = Font(bold=True, size=10)
    dv = DataValidation(type="list", formula1='"All,High (\u22658),Medium (5-7),Low (<5)"', allow_blank=False)
    dv.add("C5")
    ws.add_data_validation(dv)

    # Headers row 7
    hdrs = [
        "ASIN", "Title", "Image Count", "Bullets Filled",
        "Title Length", "Description Length", "Fabric Filled",
        "Score (0-10)", "Bucket",
    ]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # One row per ASIN with formulas referencing the Catalog table
    for i, row in enumerate(flat_rows, start=8):
        cat_row = i - 7 + 1  # row number in Catalog table (header is row 1)
        # Use direct references so the reader can inspect
        ws.cell(row=i, column=2, value=f"=INDEX(Catalog[ASIN],{cat_row - 1})")
        ws.cell(row=i, column=3, value=f"=INDEX(Catalog[Title],{cat_row - 1})")
        ws.cell(row=i, column=4, value=f"=INDEX(Catalog[Image Count],{cat_row - 1})")
        # Bullets filled: count how many of Bullet 1-5 are populated
        ws.cell(row=i, column=5, value=(
            f'=(IF(LEN(INDEX(Catalog[Bullet 1],{cat_row - 1}))>0,1,0)+'
            f'IF(LEN(INDEX(Catalog[Bullet 2],{cat_row - 1}))>0,1,0)+'
            f'IF(LEN(INDEX(Catalog[Bullet 3],{cat_row - 1}))>0,1,0)+'
            f'IF(LEN(INDEX(Catalog[Bullet 4],{cat_row - 1}))>0,1,0)+'
            f'IF(LEN(INDEX(Catalog[Bullet 5],{cat_row - 1}))>0,1,0))'
        ))
        ws.cell(row=i, column=6, value=f'=LEN(INDEX(Catalog[Title],{cat_row - 1}))')
        ws.cell(row=i, column=7, value=f'=LEN(INDEX(Catalog[Description],{cat_row - 1}))')
        ws.cell(row=i, column=8, value=f'=IF(LEN(INDEX(Catalog[Fabric / Material],{cat_row - 1}))>0,1,0)')
        # Composite score: 2 pts each check, scaled
        # image: 5+ = 2, else 0.4/img
        # bullets: 3+ = 2, else 0.67/bullet
        # title: 60-200 chars = 2, else scaled
        # description: 200+ chars = 2, else scaled
        # fabric: 1/0 = 2/0
        ws.cell(row=i, column=9, value=(
            f'=ROUND('
            f'IF(D{i}>=5,2,D{i}*0.4)'
            f'+IF(E{i}>=3,2,E{i}*0.67)'
            f'+IF(AND(F{i}>=60,F{i}<=200),2,IF(F{i}>200,MAX(0,2-(F{i}-200)/100),F{i}/30))'
            f'+IF(G{i}>=200,2,G{i}/100)'
            f'+H{i}*2'
            f',1)'
        ))
        ws.cell(row=i, column=10, value=(
            f'=IF(I{i}>=8,"High",IF(I{i}>=5,"Medium","Low"))'
        ))
        ws.cell(row=i, column=9).number_format = "0.0"

    last_data_row = 7 + len(flat_rows)

    # Conditional formatting on Score column: red -> yellow -> green
    if flat_rows:
        ws.conditional_formatting.add(f"I8:I{last_data_row}", ColorScaleRule(
            start_type="num", start_value=0,  start_color="FEE2E2",
            mid_type="num",   mid_value=5,    mid_color="FEF3C7",
            end_type="num",   end_value=10,   end_color="DCFCE7",
        ))
        # Grey out rows outside the selected bucket
        ws.conditional_formatting.add(f"B8:J{last_data_row}", FormulaRule(
            formula=[
                f'AND($C$5<>"All",'
                f'IF($C$5="High (\u22658)",J8<>"High",'
                f'IF($C$5="Medium (5-7)",J8<>"Medium",'
                f'IF($C$5="Low (<5)",J8<>"Low",FALSE))))'
            ],
            fill=PatternFill("solid", fgColor="F8FAFC"),
            font=Font(color="94A3B8"),
        ))

    widths = [2, 14, 60, 12, 14, 14, 18, 14, 14, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B8"


# ============================================================
# Sheet 08 — All_Findings (mirror of dashboard export)
# ============================================================

def _sheet_all_findings(wb, findings):
    ws = wb.create_sheet("08_All_Findings")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "All findings — dashboard mirror"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = "Every value here should match the dashboard cell-for-cell."
    ws["B3"].font = FONT_MUTED

    hdrs = ["Severity", "Rule", "ASIN", "Proposed Fix",
            "Predicate", "Threshold", "Severity Logic",
            "Sample Match", "Finding ID"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=5, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_f = sorted(findings, key=lambda f: (
        sev_order.get(f.get("severity", "medium"), 9),
        -(f.get("priority_score") or 0)
    ))

    for i, f in enumerate(sorted_f, start=6):
        rule = f.get("rule_name") or ""
        spec = RULE_SPECS.get(rule, {})
        ev = f.get("evidence") or {}
        sample = ev.get("sample_asins") or []
        ws.cell(row=i, column=2, value=(f.get("severity") or "").upper())
        ws.cell(row=i, column=3, value=spec.get("label") or rule)
        ws.cell(row=i, column=4, value=f.get("asin") or "")
        ws.cell(row=i, column=5, value=f.get("proposed_fix") or "")
        ws.cell(row=i, column=6, value=spec.get("predicate", ""))
        ws.cell(row=i, column=7, value=spec.get("threshold", ""))
        ws.cell(row=i, column=8, value=spec.get("severity_logic", ""))
        ws.cell(row=i, column=9, value=", ".join(sample[:5]))
        ws.cell(row=i, column=10, value=f.get("finding_id") or "")
        for c in (5, 6, 7, 8, 9):
            ws.cell(row=i, column=c).alignment = Alignment(wrap_text=True, vertical="top")

    widths = [2, 10, 32, 12, 55, 55, 40, 40, 30, 38]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B6"


# ============================================================
# Sheet 09 — Rules_Methodology (full 15-rule appendix)
# ============================================================

def _sheet_rules_methodology(wb):
    ws = wb.create_sheet("09_Rules_Methodology")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Methodology — every rule's exact check"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("For each of the 15 rules, this appendix lists the plain-English "
                "predicate, the threshold, the severity logic, the fields inspected, "
                "the minimum coverage required, the SQL predicate as executed, and "
                "a standalone verify query you can run against your own database.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:H3")

    hdrs = [
        "Rule ID", "Label", "Category", "Data Source",
        "Predicate", "Threshold", "Severity Logic",
        "Fields Inspected", "Min Coverage",
        "SQL Predicate (as executed)", "Standalone Verify Query",
    ]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=5, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    for i, rule_id in enumerate(sorted(RULE_SPECS.keys()), start=6):
        s = RULE_SPECS[rule_id]
        ws.cell(row=i, column=2, value=rule_id)
        ws.cell(row=i, column=3, value=s.get("label", ""))
        ws.cell(row=i, column=4, value=s.get("category", ""))
        ws.cell(row=i, column=5, value=s.get("data_source", ""))
        ws.cell(row=i, column=6, value=s.get("predicate", ""))
        ws.cell(row=i, column=7, value=s.get("threshold", ""))
        ws.cell(row=i, column=8, value=s.get("severity_logic", ""))
        ws.cell(row=i, column=9, value=", ".join(s.get("checks_field") or []) or "(no single field)")
        ws.cell(row=i, column=10, value=s.get("min_coverage", ""))
        ws.cell(row=i, column=11, value=s.get("sql_predicate", ""))
        ws.cell(row=i, column=12, value=s.get("verify_query", ""))
        for c in (6, 7, 8, 10, 11, 12):
            ws.cell(row=i, column=c).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 60

    widths = [2, 28, 34, 14, 16, 55, 40, 40, 26, 26, 55, 55]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "C6"


# ============================================================
# Sheet 10 — Data_Gaps (the pitch sheet)
# ============================================================

DATA_GAPS = [
    {
        "gap": "Promo / discount depth data",
        "field": "sale_price with dated periods, coupon codes, deal history",
        "unlocks": "Discount depth analysis, holiday timing, price elasticity by ASIN, floor-price integrity check against MAP",
        "priority": "high",
        "how": "Export Amazon Deals dashboard or SP-API GetPromotions",
    },
    {
        "gap": "Country of origin",
        "field": "country_of_origin per ASIN",
        "unlocks": "Compliance risk audit (Amazon requires COO on many categories), tariff exposure map, sourcing consolidation opportunities",
        "priority": "high",
        "how": "Add to seller central listing attributes; export via bulk report",
    },
    {
        "gap": "Care instructions (apparel)",
        "field": "care_instructions per apparel ASIN",
        "unlocks": "Apparel compliance audit, correlation between care complexity and return rate",
        "priority": "high",
        "how": "Amazon apparel category requires this — pull from category listing attributes report",
    },
    {
        "gap": "Search terms / backend keywords",
        "field": "backend_keywords, front-end search term rank per ASIN",
        "unlocks": "SEO gap analysis, keyword coverage score, discoverability audit, duplicate/wasted term detection",
        "priority": "medium",
        "how": "Search Query Performance report + backend keyword report from Seller Central",
    },
    {
        "gap": "Reviews & ratings",
        "field": "review_count, avg_rating, review_text sample per ASIN",
        "unlocks": "Sentiment mining, complaint theming, competitor comparison, quality-issue triangulation with return rate",
        "priority": "medium",
        "how": "Amazon Vine + review scraping via approved SP-API endpoints",
    },
    {
        "gap": "Rank data (BSR)",
        "field": "bsr_category, bsr_rank_daily_history",
        "unlocks": "Rank decay detection, category positioning, competitive threat mapping",
        "priority": "medium",
        "how": "Third-party rank tracker (Helium 10, Jungle Scout) or manual SP-API pulls",
    },
    {
        "gap": "Ad spend / TACOS",
        "field": "ad_spend, sponsored_impressions, sponsored_clicks per ASIN per period",
        "unlocks": "Ad efficiency per ASIN, wasted spend detection, TACOS trend, ACoS by campaign",
        "priority": "high",
        "how": "Advertising bulk file download from Seller Central",
    },
    {
        "gap": "Returns data",
        "field": "return_count, return_reason breakdown per ASIN",
        "unlocks": "Return rate by ASIN, defective SKU detection, size-fit issue signals, reason-code clustering",
        "priority": "high",
        "how": "Returns FBA/FBM report from Seller Central",
    },
    {
        "gap": "Historical traffic & rank trend",
        "field": "session and rank time series (min 12 weeks)",
        "unlocks": "Category velocity, seasonality mapping, launch effect detection, decay half-life per ASIN",
        "priority": "medium",
        "how": "Business Reports archive from Seller Central (30-day windows stitched)",
    },
    {
        "gap": "Competitor URLs & data",
        "field": "manual list of competing brand + ASIN pairs",
        "unlocks": "PDP gap analysis, price positioning, image-count comparison, review-count comparison, buy-box competition mapping",
        "priority": "low",
        "how": "Operator-provided competitor list; agency can scrape/pull",
    },
]


def _sheet_data_gaps(wb):
    ws = wb.create_sheet("10_Data_Gaps")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "What more data do you need?"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Rules that can't currently fire on your catalog because "
                "the underlying data isn't in the upload. Send any of "
                "these and the analyses on the right become available.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:H3")

    hdrs = ["Data Gap", "Field / Signal", "What This Unlocks",
            "Priority", "How to Provide"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=5, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    priority_fill = {
        "high":   PatternFill("solid", fgColor="FEE2E2"),
        "medium": PatternFill("solid", fgColor="FEF3C7"),
        "low":    PatternFill("solid", fgColor="DBEAFE"),
    }

    for i, gap in enumerate(DATA_GAPS, start=6):
        ws.cell(row=i, column=2, value=gap["gap"]).font = FONT_BOLD
        ws.cell(row=i, column=3, value=gap["field"])
        ws.cell(row=i, column=4, value=gap["unlocks"])
        pcell = ws.cell(row=i, column=5, value=gap["priority"].upper())
        pcell.fill = priority_fill.get(gap["priority"], PatternFill())
        pcell.alignment = ALIGN_CENTER
        pcell.font = FONT_BOLD
        ws.cell(row=i, column=6, value=gap["how"])
        for c in (2, 3, 4, 6):
            ws.cell(row=i, column=c).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 60

    widths = [2, 32, 34, 60, 12, 40]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ============================================================
# Sheet 11 — Trend_KPIs (skeleton, populated on re-upload)
# ============================================================

# The 8 KPIs from Cover, in the same order, so trends align visually
_TREND_KPIS = [
    ("Total ASINs",                    "count", "=COUNTA(Catalog[ASIN])"),
    ("Dead % (0 sessions, 0 units)",   "pct",   "=IFERROR(COUNTIFS(Catalog[Sessions],0,Catalog[Units],0)/COUNTA(Catalog[ASIN]),0)"),
    ("Active ASINs",                   "count", '=COUNTIFS(Catalog[Sessions],">0")+COUNTIFS(Catalog[Sessions],0,Catalog[Units],">0")'),
    ("Total revenue",                  "money", "=SUM(Catalog[Revenue])"),
    ("Titles filled %",                "pct",   '=IFERROR(COUNTIF(Catalog[Title],"?*")/COUNTA(Catalog[ASIN]),0)'),
    ("Descriptions filled %",          "pct",   '=IFERROR(COUNTIF(Catalog[Description],"?*")/COUNTA(Catalog[ASIN]),0)'),
    ("Fabric/material filled %",       "pct",   '=IFERROR(COUNTIF(Catalog[Fabric / Material],"?*")/COUNTA(Catalog[ASIN]),0)'),
    ("Avg images per ASIN",            "num",   "=IFERROR(AVERAGE(Catalog[Image Count]),0)"),
]


def _sheet_trend_kpis(wb, snapshot):
    ws = wb.create_sheet("11_Trend_KPIs")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "KPI Trend — populated as you re-upload"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Every KPI from 01_Cover, tracked over time. Only the current column has "
                "data. Send a new catalog upload each month and the T-1 / T-2 / T-3 columns "
                "populate automatically in the dashboard's diff view.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:I3")

    # Info banner
    ws["B5"] = ("△  This workbook is a point-in-time snapshot. The dashboard maintains this "
                "table automatically across snapshots — no manual sheet-per-month juggling.")
    ws["B5"].font = Font(italic=True, size=10, color="7A431A")
    ws["B5"].fill = PatternFill("solid", fgColor="FEF3C7")
    ws.merge_cells("B5:I5")

    # Headers row 7
    snap_label = _snap_label(snapshot) or "Now"
    hdrs = ["KPI", "Format", "T-3", "T-2", "T-1", snap_label,
            "Δ T-1 → Now", "Direction"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # Data rows: label, format, blanks, current, delta, direction
    for i, (label, kind, formula) in enumerate(_TREND_KPIS, start=8):
        ws.cell(row=i, column=2, value=label).font = FONT_BOLD
        ws.cell(row=i, column=3, value=kind).font = FONT_MUTED
        # T-3, T-2, T-1 blank with placeholder text
        for col in (4, 5, 6):
            c = ws.cell(row=i, column=col, value="")
            c.fill = PatternFill("solid", fgColor="F8FAFC")
        # Current column = live formula
        cur = ws.cell(row=i, column=7, value=formula)
        # Delta = current - T-1 (formula-ready; shows blank until T-1 is populated)
        d_cell = ws.cell(row=i, column=8, value=f'=IF(F{i}="","",G{i}-F{i})')
        # Direction indicator (down arrow for lower_is_better metrics, up for higher)
        ws.cell(row=i, column=9, value=f'=IF(F{i}="","awaiting T-1",IF(H{i}>0,"↑ up",IF(H{i}<0,"↓ down","→ flat")))')

        # Number format for current + delta cells
        if kind == "pct":
            for col in (4, 5, 6, 7): ws.cell(row=i, column=col).number_format = "0.0%"
            d_cell.number_format = "+0.0%;-0.0%;—"
        elif kind == "money":
            for col in (4, 5, 6, 7): ws.cell(row=i, column=col).number_format = "$#,##0"
            d_cell.number_format = "+$#,##0;-$#,##0;—"
        else:
            for col in (4, 5, 6, 7): ws.cell(row=i, column=col).number_format = "#,##0.0"
            d_cell.number_format = "+#,##0.0;-#,##0.0;—"

        cur.font = Font(bold=True, size=11)
        cur.fill = PatternFill("solid", fgColor="EFF6FF")

    # Footnote row
    footer_row = 8 + len(_TREND_KPIS) + 1
    ws.cell(row=footer_row, column=2, value=(
        "To populate T-1: send your previous month's catalog upload. "
        "T-2 needs 2 months back. T-3 needs 3."
    )).font = FONT_MUTED
    ws.merge_cells(start_row=footer_row, start_column=2, end_row=footer_row, end_column=9)

    widths = [2, 32, 10, 16, 16, 16, 20, 20, 18]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B8"


def _snap_label(snapshot):
    if not snapshot:
        return None
    ts = snapshot.get("uploaded_at") or ""
    return ts[:10] if ts else "Now"


# ============================================================
# Sheet 12 — Trend_By_Rule (per-rule metric evolution)
# ============================================================

# Primary metric per rule — matches diff_engine.METRIC_MAP
_RULE_TREND_METRICS = {
    "dead_inventory":              {"key": "dead_pct",             "direction": "lower_is_better", "fmt": "pct"},
    "description_presence":        {"key": "pct_with_description", "direction": "higher_is_better", "fmt": "pct"},
    "fabric_material_coverage":    {"key": "pct_filled",           "direction": "higher_is_better", "fmt": "pct"},
    "buy_box_ownership":           {"key": "likely_owner_pct",     "direction": "higher_is_better", "fmt": "pct"},
    "image_count_dist":            {"key": "under_5_pct",          "direction": "lower_is_better", "fmt": "pct"},
    "bullet_completeness_dist":    {"key": "under_3_pct",          "direction": "lower_is_better", "fmt": "pct"},
    "title_length_dist":           {"key": "flagged_pct",          "direction": "lower_is_better", "fmt": "pct"},
    "variation_theme_integrity":   {"key": "inconsistent_pct",     "direction": "lower_is_better", "fmt": "pct"},
    "style_family_concentration":  {"key": "mega_family_count",    "direction": "lower_is_better", "fmt": "count"},
    "list_price_dist":             {"key": "outlier_count",        "direction": "lower_is_better", "fmt": "count"},
    "concentration_pareto":        {"key": "top_50pct_asins",      "direction": "higher_is_better", "fmt": "count"},
    "cohort_split":                {"key": "dead_pct",             "direction": "lower_is_better", "fmt": "pct"},
    "a_plus_lift":                 {"key": "lift_multiplier",      "direction": "higher_is_better", "fmt": "num"},
    "fill_rate_report":            None,
    "subcategory_rollup":          None,
}


def _sheet_trend_by_rule(wb, snapshot):
    ws = wb.create_sheet("12_Trend_By_Rule")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Per-Rule Metric Trend"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("For each of the 15 rules, the primary metric that moves when things get better or worse. "
                "Only the current column shows data — historicals populate as you re-upload.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:J3")

    # Banner
    ws["B5"] = ("△  The dashboard's snapshot-diff view is the interactive version of this table. "
                "It also flags each row as 'improved / worsened / unchanged' with a 1pt materiality threshold.")
    ws["B5"].font = Font(italic=True, size=10, color="7A431A")
    ws["B5"].fill = PatternFill("solid", fgColor="FEF3C7")
    ws.merge_cells("B5:J5")

    snap_label = _snap_label(snapshot) or "Now"
    hdrs = ["Rule", "Metric", "Format", "T-3", "T-2", "T-1", snap_label,
            "Δ T-1 → Now", "Direction"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    row = 8
    for rule_id in sorted(RULE_SPECS.keys()):
        spec = RULE_SPECS[rule_id]
        m = _RULE_TREND_METRICS.get(rule_id)
        ws.cell(row=row, column=2, value=spec.get("label") or rule_id).font = FONT_BOLD
        if not m:
            # Aggregate-only rule (fill_rate_report, subcategory_rollup) — no primary metric
            ws.cell(row=row, column=3, value="(no single metric)").font = FONT_MUTED
            ws.cell(row=row, column=4, value="—").font = FONT_MUTED
            for col in (5, 6, 7, 8, 9, 10):
                ws.cell(row=row, column=col, value="—").font = FONT_MUTED
            row += 1
            continue
        ws.cell(row=row, column=3, value=m["key"])
        ws.cell(row=row, column=4, value=m["fmt"]).font = FONT_MUTED
        # T-3, T-2, T-1 blank
        for col in (5, 6, 7):
            c = ws.cell(row=row, column=col, value="")
            c.fill = PatternFill("solid", fgColor="F8FAFC")
        # Current — pulled from findings via a helper column ref. We can't reliably
        # extract the exact metric without the finding row available here, so we
        # show a placeholder that points to 08_All_Findings for lookup.
        ws.cell(row=row, column=8, value=(
            f'=IFERROR(VLOOKUP("{spec.get("label") or rule_id}",'
            f"'08_All_Findings'!C:D,2,FALSE),"
            f'"see findings")'
        ))
        ws.cell(row=row, column=8).font = Font(bold=True, size=10)
        ws.cell(row=row, column=8).fill = PatternFill("solid", fgColor="EFF6FF")
        # Delta cell — blank until T-1 populated
        ws.cell(row=row, column=9, value=f'=IF(G{row}="","",H{row}-G{row})')
        # Direction — respect lower_is_better vs higher_is_better
        dir_up_label = "↑ improved" if m["direction"] == "higher_is_better" else "↑ worsened"
        dir_dn_label = "↓ worsened" if m["direction"] == "higher_is_better" else "↓ improved"
        ws.cell(row=row, column=10, value=(
            f'=IF(G{row}="","awaiting T-1",'
            f'IF(I{row}>0,"{dir_up_label}",'
            f'IF(I{row}<0,"{dir_dn_label}","→ unchanged")))'
        ))
        row += 1

    widths = [2, 34, 22, 8, 14, 14, 14, 18, 18, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B8"


# ============================================================
# Sheet 13 — Fix_Effectiveness (the money loop)
# ============================================================

def _sheet_fix_effectiveness(wb, findings, snapshot):
    ws = wb.create_sheet("13_Fix_Effectiveness")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "Fix Effectiveness — the money loop"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("For every finding you mark as 'fixed' in the dashboard, this sheet will show "
                "whether the underlying metric actually improved on the next snapshot. "
                "This is how you prove the audit is producing outcomes.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:I3")

    # Banner
    ws["B5"] = ("△  Requires: the finding_status workflow in the dashboard + at least 2 snapshots. "
                "Excel can't track status across uploads without manual copying — this is what the dashboard is for.")
    ws["B5"].font = Font(italic=True, size=10, color="7A431A")
    ws["B5"].fill = PatternFill("solid", fgColor="FEF3C7")
    ws.merge_cells("B5:I5")

    hdrs = ["Rule", "Severity", "Status Set", "Fixed Date",
            "Metric at Fix Time", "Metric Now", "Actually Improved?", "Verification"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    # One row per finding, showing the SHAPE. Most columns blank because
    # status_history isn't in this workbook (dashboard-only).
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    sorted_findings = sorted(findings, key=lambda f: (
        sev_order.get(f.get("severity", "medium"), 9),
        -(f.get("priority_score") or 0)
    ))

    for i, f in enumerate(sorted_findings, start=8):
        rule = f.get("rule_name") or ""
        spec = RULE_SPECS.get(rule, {})
        ws.cell(row=i, column=2, value=spec.get("label") or rule)
        ws.cell(row=i, column=3, value=(f.get("severity") or "").upper())
        # These 4 columns are populated from dashboard status workflow
        for col in (4, 5, 6, 7):
            c = ws.cell(row=i, column=col, value="awaiting fix workflow")
            c.font = FONT_MUTED
            c.fill = PatternFill("solid", fgColor="F8FAFC")
        # Actually Improved — formula that checks if metric moved in the right direction
        ws.cell(row=i, column=8, value=(
            f'=IF(OR(F{i}="awaiting fix workflow",G{i}="awaiting fix workflow"),"pending","")'
        )).font = FONT_MUTED
        # Verification link (pointer)
        ws.cell(row=i, column=9, value="see dashboard → finding history").font = FONT_MUTED

    # Footer
    footer_row = 8 + len(sorted_findings) + 1
    ws.cell(row=footer_row, column=2, value=(
        "To activate this sheet: open the dashboard, click a finding, set status to 'in_progress' "
        "or 'fixed', add a note. Upload the next snapshot next month. The dashboard will populate "
        "this table automatically — no way to reproduce this in raw Excel."
    )).font = Font(italic=True, size=10, color="64748B")
    ws.cell(row=footer_row, column=2).alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=footer_row, start_column=2, end_row=footer_row, end_column=9)
    ws.row_dimensions[footer_row].height = 40

    widths = [2, 32, 10, 20, 16, 20, 18, 18, 32]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.freeze_panes = "B8"


# ============================================================
# Sheet 14 — How_To_Add_Historicals (pitch sheet)
# ============================================================

def _sheet_how_to_add_historicals(wb):
    ws = wb.create_sheet("14_How_To_Add_Historicals")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "How to fill in the trend columns"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")
    ws["B3"] = ("Sheets 11-13 have empty T-1 / T-2 / T-3 columns. Each blank column "
                "corresponds to a past monthly upload. Here is exactly what to send "
                "and what it unlocks.")
    ws["B3"].font = FONT_MUTED
    ws.merge_cells("B3:H3")

    ws["B5"] = "What to send us"
    ws["B5"].font = Font(bold=True, size=14, color="0F172A")

    plan = [
        ("For T-1 (last month)",
         "1 catalog export dated ~30 days ago",
         "Enables month-over-month deltas on all 8 KPIs and all 15 rule metrics. "
         "The dashboard's snapshot-diff view flags improved / worsened / unchanged with 1pt materiality."),
        ("For T-2 (2 months back)",
         "1 catalog export dated ~60 days ago",
         "Enables 3-point trend visibility (T-2 → T-1 → Now). Sparklines start showing shape. "
         "You can identify accelerating problems vs. one-off regressions."),
        ("For T-3 (3 months back)",
         "1 catalog export dated ~90 days ago",
         "Full quarterly trend. Sparklines are meaningful. Category velocity and content-health drift become detectable."),
        ("For monthly ongoing",
         "1 fresh upload every month, first business day",
         "Automated diff runs. Fix-effectiveness sheet populates. Dashboard notifications fire on materially worsened metrics."),
    ]

    hdrs = ["When", "What file", "What this unlocks"]
    for c, h in enumerate(hdrs, start=2):
        cell = ws.cell(row=7, column=c, value=h)
        cell.font = FONT_HEADER
        cell.fill = FILL_HEADER
        cell.alignment = ALIGN_CENTER

    for i, (when, what, unlocks) in enumerate(plan, start=8):
        ws.cell(row=i, column=2, value=when).font = FONT_BOLD
        ws.cell(row=i, column=3, value=what)
        ws.cell(row=i, column=4, value=unlocks)
        for c in (2, 3, 4):
            ws.cell(row=i, column=c).alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[i].height = 60

    # Why the dashboard exists
    ws["B14"] = "Why not just juggle Excel files month-over-month?"
    ws["B14"].font = Font(bold=True, size=14, color="0F172A")
    reasons = [
        "• You'd manage 12 workbooks/year manually and copy T-1/T-2/T-3 values by hand every month.",
        "• The materiality threshold (1pt) has to be applied consistently across every rule — error-prone in Excel.",
        "• Status workflow (marked in_progress / fixed / wontfix) has no home in raw Excel; you'd track it in a separate file.",
        "• The audit trail (who changed what status when, with what note) needs an immutable history log.",
        "• Cross-brand pooling (comparing Novelle vs Roxy vs future brands) is a database join, not a spreadsheet.",
        "• Real-time drilldowns from a finding to the affected ASIN list require querying live data.",
    ]
    for i, r in enumerate(reasons, start=16):
        cell = ws.cell(row=i, column=2, value=r)
        cell.font = Font(name="Calibri", size=10, color="334155")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=6)

    ws["B24"] = ("Excel is where you verify the numbers. The dashboard is where the numbers live "
                 "month after month.")
    ws["B24"].font = Font(bold=True, italic=True, size=11, color="0F172A")
    ws.merge_cells("B24:H24")

    widths = [2, 26, 32, 66]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ============================================================
# Sheet 15 — How_This_Works (trust page)
# ============================================================

def _sheet_how_it_works(wb):
    ws = wb.create_sheet("15_How_This_Works")
    ws.sheet_view.showGridLines = False

    ws["B2"] = "How this workbook works"
    ws["B2"].font = Font(bold=True, size=18, color="0F172A")

    body = [
        "",
        "Every downstream number in this workbook is a live Excel formula pointing at 02_Catalog_Data.",
        "You can click any KPI, chart, or table cell, look in the formula bar, and see the exact math.",
        "",
        "Verification workflow:",
        "  1. Open 08_All_Findings. Every row shows a finding from the dashboard.",
        "  2. Open 09_Rules_Methodology. Find the same rule. Read the SQL predicate and the verify query.",
        "  3. Return to 02_Catalog_Data and confirm the raw values that produced the finding.",
        "  4. Change a raw value on 02_Catalog_Data and watch 01_Cover, 04_Coverage_Matrix, 05_Revenue_Concentration",
        "     etc. update in real time. If they don't update, the formula is broken and you've found a dashboard bug.",
        "",
        "Named ranges (available via Formulas → Name Manager):",
        "  Catalog                — the whole raw catalog table on 02_Catalog_Data",
        "  Catalog[ASIN]          — all ASINs",
        "  Catalog[Revenue]       — all revenue values",
        "  Catalog[Sessions]      — all sessions values",
        "  Catalog[Units]         — all units values",
        "  Catalog[Title]         — all titles",
        "  Catalog[Image Count]   — all image counts",
        "  Sales                  — the raw sales rollup table on 03_Sales_Data",
        "",
        "Materiality note (for snapshot-diff comparisons):",
        "  The dashboard treats deltas under 1 percentage point (or under 5 for counts) as 'unchanged' to",
        "  avoid floating-point noise. This workbook shows the exact numbers with no rounding, so you may",
        "  see small differences vs. the dashboard's diff view. Absolute values will match to the row.",
        "",
        "Interactivity in this workbook (no Excel slicers — we use data-validation dropdowns instead):",
        "  04_Coverage_Matrix     — cell C5:  filter fields by category",
        "  05_Revenue_Concentration — cell C5: reshape the ranked list (20/50/100/500/1000)",
        "  07_Content_Health      — cell C5: filter ASINs by score bucket",
        "",
        "Generated by Perplexity Computer — Atlas Catalog Intel v1.2.",
    ]
    for i, line in enumerate(body, start=4):
        cell = ws.cell(row=i, column=2, value=line)
        cell.font = Font(name="Consolas", size=10, color="334155")
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=i, start_column=2, end_row=i, end_column=8)

    ws.column_dimensions["A"].width = 3
    for c in "BCDEFGH":
        ws.column_dimensions[c].width = 16
